from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from durable_agents.events import (
    Event,
    LLMCallFailed,
    LLMCallRequested,
    RunStarted,
    ToolCallInvocation,
    hash_system_prompt,
)
from durable_agents.llm.protocol import LLMResponse
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.storage.memory import InMemoryEventStore
from durable_agents.tools.registry import Tool, tool



def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_started() -> RunStarted:
    return RunStarted(
        seq=0,
        created_at=_now(),
        goal="Do the thing.",
        model="scripted",
        system_prompt_hash="sha256:test",
        max_steps=15,
        max_cost_usd=Decimal("2.00"),
        requested_by="test",
        guardrail_profile="financial_v1",
    )


def _llm_response(**overrides: object) -> LLMResponse:
    defaults: dict[str, object] = {
        "content": None,
        "tool_calls": [],
        "stop_reason": "end_turn",
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": Decimal("0.001"),
        "latency_ms": 500,
        "provider_request_id": "req",
    }
    defaults.update(overrides)
    return LLMResponse(**defaults)  # type: ignore[arg-type]


class FlakyBackend:
    """Records every idempotency_key it is called with, so a test can
    prove a retry reused the original key rather than minting a new one.
    """

    def __init__(self, fail_times: int) -> None:
        self._fail_times = fail_times
        self.calls: list[str] = []
        self.committed: dict[str, dict[str, Any]] = {}

    async def charge(self, idempotency_key: str) -> dict[str, Any]:
        self.calls.append(idempotency_key)
        if len(self.calls) <= self._fail_times:
            raise RuntimeError("payments API timed out")
        if idempotency_key in self.committed:
            return {**self.committed[idempotency_key], "dedup_hit": True}
        record = {"charge_id": f"CH-{len(self.committed) + 1}", "status": "ok"}
        self.committed[idempotency_key] = record
        return record


def _flaky_tools(backend: FlakyBackend) -> dict[str, Tool]:
    @tool(side_effect=True)
    async def do_charge(amount: int, idempotency_key: str) -> dict[str, Any]:
        """Charge the customer."""
        return await backend.charge(idempotency_key)

    return {do_charge.name: do_charge}


def _charge_then_finish() -> list[LLMResponse | Exception]:
    return [
        _llm_response(
            content="Charging.",
            tool_calls=[ToolCallInvocation(id="t1", name="do_charge", arguments={"amount": 100})],
            stop_reason="tool_use",
        ),
        _llm_response(content="Done.", stop_reason="end_turn"),
    ]


@pytest.mark.asyncio
async def test_transient_llm_failure_is_retried_and_run_completes() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    # Two provider errors, then a real response — exactly the 429/500
    # pattern that used to crash run() outright.
    llm = ScriptedLLM(
        [
            RuntimeError("429 rate limited"),
            RuntimeError("500 internal server error"),
            _llm_response(content="Finally worked.", stop_reason="end_turn"),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=llm, tools={}, retry_base_delay_seconds=0)
    state = await orchestrator.run(run_id)

    assert state.status == "completed"
    assert state.final_answer == "Finally worked."

    event_types = [type(e).__name__ for e in await store.read(run_id)]
    assert event_types.count("LLMCallFailed") == 2
    assert event_types.count("LLMCallCompleted") == 1
    # One LLMCallRequested covered all three attempts — the intent was
    # recorded once; the retries are attempts at that same intent.
    assert event_types.count("LLMCallRequested") == 1


@pytest.mark.asyncio
async def test_persistent_llm_failure_exhausts_budget_and_fails_the_run() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    llm = ScriptedLLM([RuntimeError("provider is down") for _ in range(10)])
    orchestrator = Orchestrator(
        store=store, llm=llm, tools={}, max_llm_attempts=3, retry_base_delay_seconds=0
    )
    state = await orchestrator.run(run_id)

    assert state.status == "failed"
    assert state.failure_reason == "unrecoverable_error"

    event_types = [type(e).__name__ for e in await store.read(run_id)]
    # Bounded: exactly the budget, not an infinite retry loop.
    assert event_types.count("LLMCallFailed") == 3
    assert llm.call_count == 3


@pytest.mark.asyncio
async def test_transient_tool_failure_retries_with_the_same_idempotency_key() -> None:
    """The exactly-once claim on the error path. A tool that raises is
    retried in place, reusing the original key — so the backend can
    deduplicate. Surfacing the error to the model instead would make it
    issue a fresh call at a new seq, hence a NEW key, which nothing
    would deduplicate.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    backend = FlakyBackend(fail_times=2)
    orchestrator = Orchestrator(
        store=store,
        llm=ScriptedLLM(_charge_then_finish()),
        tools=_flaky_tools(backend),
        retry_base_delay_seconds=0,
    )
    state = await orchestrator.run(run_id)

    assert state.status == "completed"
    # Three physical attempts against the backend...
    assert len(backend.calls) == 3
    # ...all with the identical key, and exactly one charge committed.
    assert len(set(backend.calls)) == 1
    assert len(backend.committed) == 1

    event_types = [type(e).__name__ for e in await store.read(run_id)]
    assert event_types.count("ToolCallFailed") == 2
    assert event_types.count("ToolCallCompleted") == 1
    assert event_types.count("ToolCallRequested") == 1


@pytest.mark.asyncio
async def test_persistent_tool_failure_surfaces_to_the_model_after_budget() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    backend = FlakyBackend(fail_times=99)
    llm = ScriptedLLM(
        [
            _llm_response(
                content="Charging.",
                tool_calls=[
                    ToolCallInvocation(id="t1", name="do_charge", arguments={"amount": 100})
                ],
                stop_reason="tool_use",
            ),
            _llm_response(content="The charge failed, telling the customer.", stop_reason="end_turn"),
        ]
    )
    orchestrator = Orchestrator(
        store=store,
        llm=llm,
        tools=_flaky_tools(backend),
        max_tool_attempts=3,
        retry_base_delay_seconds=0,
    )
    state = await orchestrator.run(run_id)

    # The model got a chance to react rather than the run dying.
    assert state.status == "completed"
    assert state.final_answer == "The charge failed, telling the customer."
    assert len(backend.calls) == 3
    assert len(backend.committed) == 0

    events = await store.read(run_id)
    failures = [e for e in events if e.type == "ToolCallFailed"]
    assert len(failures) == 3
    assert [f.final_attempt for f in failures if f.type == "ToolCallFailed"] == [False, False, True]
    # The error reached the model as a tool-role message.
    assert any(
        m.role == "tool" and m.content is not None and "timed out" in m.content
        for m in state.messages
    )


@pytest.mark.asyncio
async def test_system_prompt_reaches_the_llm_and_survives_replay() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    prompt = "You are a careful support agent. Never refund above the order value."
    await store.append(
        run_id,
        0,
        RunStarted(
            seq=0,
            created_at=_now(),
            goal="Do the thing.",
            model="scripted",
            system_prompt=prompt,
            max_steps=15,
            max_cost_usd=Decimal("2.00"),
            requested_by="test",
            guardrail_profile="financial_v1",
        ),
    )

    llm = ScriptedLLM([_llm_response(content="Understood.", stop_reason="end_turn")])
    orchestrator = Orchestrator(store=store, llm=llm, tools={}, retry_base_delay_seconds=0)
    state = await orchestrator.run(run_id)

    assert llm.last_system_prompt == prompt
    # And it's derivable from the log alone, so a resumed process (or a
    # replay) runs the model under the same instructions.
    assert state.system_prompt == prompt


def test_system_prompt_hash_is_derived_not_hand_supplied() -> None:
    started = RunStarted(
        seq=0,
        created_at=_now(),
        goal="g",
        model="m",
        system_prompt="be careful",
        max_steps=1,
        max_cost_usd=Decimal("1"),
        requested_by="t",
        guardrail_profile="financial_v1",
    )
    assert started.system_prompt_hash == hash_system_prompt("be careful")

    # A different prompt must produce a different fingerprint — that's
    # the entire point of storing one.
    other = RunStarted(
        seq=0,
        created_at=_now(),
        goal="g",
        model="m",
        system_prompt="be reckless",
        max_steps=1,
        max_cost_usd=Decimal("1"),
        requested_by="t",
        guardrail_profile="financial_v1",
    )
    assert other.system_prompt_hash != started.system_prompt_hash


def test_events_written_before_system_prompt_existed_still_load() -> None:
    # Exactly the shape already sitting in the events table: a
    # hand-supplied hash, no system_prompt field at all.
    legacy = RunStarted.model_validate(
        {
            "seq": 0,
            "created_at": _now(),
            "goal": "g",
            "model": "m",
            "system_prompt_hash": "sha256:legacy",
            "max_steps": 1,
            "max_cost_usd": "1",
            "requested_by": "t",
            "guardrail_profile": "financial_v1",
        }
    )
    assert legacy.system_prompt == ""
    assert legacy.system_prompt_hash == "sha256:legacy", "must not clobber a stored hash"


@pytest.mark.asyncio
async def test_retry_budget_survives_a_process_restart() -> None:
    """The budget lives in the event log, not a local variable — so a
    resumed process inherits what a dead one already spent. Without
    this, a flapping provider plus a crash-loop retries forever.
    """

    store = InMemoryEventStore()
    run_id = uuid4()

    # Exactly the log a process killed mid-retry leaves behind: the call
    # is still in flight, two attempts are already spent, and no
    # RunFailed was ever written because nothing got to decide that.
    await store.append(run_id, 0, _run_started())
    await store.append(
        run_id,
        1,
        LLMCallRequested(seq=1, created_at=_now(), step=1, message_count=1, estimated_tokens=10),
    )
    await store.append(
        run_id, 2, LLMCallFailed(seq=2, created_at=_now(), step=1, error="down", attempt=1)
    )
    await store.append(
        run_id, 3, LLMCallFailed(seq=3, created_at=_now(), step=1, error="down", attempt=2)
    )

    # A fresh Orchestrator (new process) with a budget of 3 resumes. It
    # must read attempts=2 back out of state and have only ONE attempt
    # left — not a fresh 3, which is what a local counter would give it.
    llm = ScriptedLLM([RuntimeError("still down") for _ in range(5)])
    second = Orchestrator(
        store=store, llm=llm, tools={}, max_llm_attempts=3, retry_base_delay_seconds=0
    )
    state = await second.run(run_id)

    assert state.status == "failed"
    assert state.failure_reason == "unrecoverable_error"
    assert llm.call_count == 1, "resumed process should have exactly one attempt left, not three"

    all_failures = [e for e in await store.read(run_id) if e.type == "LLMCallFailed"]
    assert [f.attempt for f in all_failures if f.type == "LLMCallFailed"] == [1, 2, 3]
