from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from durable_agents.events import ApprovalDenied, ApprovalGranted, Event, RunStarted, ToolCallInvocation
from durable_agents.llm.protocol import LLMResponse
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.storage.protocol import ConcurrencyConflict, EventStore
from durable_agents.tools.refund_tools import InMemoryRefundBackend, build_refund_tools
from durable_agents.tools.registry import Tool


class InMemoryEventStore(EventStore):
    """Fake EventStore for testing orchestrator logic in isolation.

    Mirrors PostgresEventStore's concurrency semantics (append only at the
    next sequential seq) without touching a real database — these tests
    are about the orchestrator's decisions, not storage correctness
    (already covered by tests/integration/test_postgres_store.py).
    """

    def __init__(self) -> None:
        self._events: dict[UUID, list[Event]] = {}

    async def append(self, run_id: UUID, expected_seq: int, event: Event) -> None:
        events = self._events.setdefault(run_id, [])
        if expected_seq != len(events):
            raise ConcurrencyConflict(f"seq {expected_seq} already taken for run {run_id}")
        events.append(event)

    async def read(self, run_id: UUID) -> list[Event]:
        return list(self._events.get(run_id, []))

    async def read_since(self, run_id: UUID, seq: int) -> list[Event]:
        return [e for e in self._events.get(run_id, []) if e.seq > seq]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_started(max_steps: int = 15, max_cost_usd: Decimal = Decimal("2.00")) -> RunStarted:
    return RunStarted(
        seq=0,
        created_at=_now(),
        goal="Process refund for order A-8891.",
        model="scripted",
        system_prompt_hash="sha256:test",
        max_steps=max_steps,
        max_cost_usd=max_cost_usd,
        requested_by="support-queue",
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


def _tools() -> dict[str, Tool]:
    return {t.name: t for t in build_refund_tools(InMemoryRefundBackend())}


@pytest.mark.asyncio
async def test_full_run_reaches_completed() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    llm = ScriptedLLM(
        [
            _llm_response(
                content="Looking up the order.",
                tool_calls=[
                    ToolCallInvocation(id="t1", name="lookup_order", arguments={"order_id": "A-8891"})
                ],
                stop_reason="tool_use",
            ),
            _llm_response(
                content="Issuing the refund.",
                tool_calls=[
                    ToolCallInvocation(
                        id="t2",
                        name="issue_refund",
                        arguments={"order_id": "A-8891", "amount_inr": 3000, "reason": "damaged"},
                    )
                ],
                stop_reason="tool_use",
            ),
            _llm_response(content="Refund processed.", stop_reason="end_turn"),
        ]
    )

    orchestrator = Orchestrator(store=store, llm=llm, tools=_tools())
    state = await orchestrator.run(run_id)

    assert state.status == "completed"
    assert state.final_answer == "Refund processed."
    assert llm.call_count == 3


@pytest.mark.asyncio
async def test_approval_required_parks_without_looping() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    llm = ScriptedLLM(
        [
            _llm_response(
                content="Issuing the refund.",
                tool_calls=[
                    ToolCallInvocation(
                        id="t1",
                        name="issue_refund",
                        arguments={"order_id": "A-8891", "amount_inr": 6400, "reason": "damaged"},
                    )
                ],
                stop_reason="tool_use",
            ),
        ]
    )

    orchestrator = Orchestrator(store=store, llm=llm, tools=_tools())
    state = await orchestrator.run(run_id)

    assert state.status == "awaiting_approval"
    assert state.pending_approval is not None
    assert state.pending_approval.tool == "issue_refund"
    assert llm.call_count == 1

    events = await store.read(run_id)
    assert [type(e).__name__ for e in events[1:]] == ["LLMCallRequested", "LLMCallCompleted", "ApprovalRequested"]


@pytest.mark.asyncio
async def test_approval_granted_resumes_the_same_call_exactly_once() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    args = {"order_id": "A-8891", "amount_inr": 6400, "reason": "damaged"}
    llm = ScriptedLLM(
        [
            _llm_response(
                content="Issuing the refund.",
                tool_calls=[ToolCallInvocation(id="t1", name="issue_refund", arguments=args)],
                stop_reason="tool_use",
            ),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=llm, tools=_tools())
    parked = await orchestrator.run(run_id)
    assert parked.status == "awaiting_approval"

    events = await store.read(run_id)
    await store.append(
        run_id, len(events), ApprovalGranted(seq=len(events), created_at=_now(), approver="mgr")
    )

    # A resumed process gets a fresh Orchestrator and a fresh LLM client —
    # only the response for the call still ahead of it, mirroring how
    # ScriptedLLM is sliced to already-completed calls in the chaos suite.
    resumed_llm = ScriptedLLM([_llm_response(content="Refund processed.", stop_reason="end_turn")])
    resumed = Orchestrator(store=store, llm=resumed_llm, tools=_tools())
    state = await resumed.run(run_id)

    assert state.status == "completed"
    assert state.final_answer == "Refund processed."

    event_types = [type(e).__name__ for e in await store.read(run_id)]
    # Exactly one ToolCallRequested/Completed pair for issue_refund: the
    # grant must not have triggered a second ApprovalRequested, nor a
    # second attempt at the tool call.
    assert event_types.count("ApprovalRequested") == 1
    assert event_types.count("ToolCallRequested") == 1
    assert event_types.count("ToolCallCompleted") == 1


@pytest.mark.asyncio
async def test_approval_denied_feeds_reason_back_instead_of_looping() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    args = {"order_id": "A-8891", "amount_inr": 6400, "reason": "damaged"}
    llm = ScriptedLLM(
        [
            _llm_response(
                content="Issuing the refund.",
                tool_calls=[ToolCallInvocation(id="t1", name="issue_refund", arguments=args)],
                stop_reason="tool_use",
            ),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=llm, tools=_tools())
    parked = await orchestrator.run(run_id)
    assert parked.status == "awaiting_approval"

    events = await store.read(run_id)
    await store.append(
        run_id,
        len(events),
        ApprovalDenied(
            seq=len(events), created_at=_now(), approver="mgr", reason="refund too large"
        ),
    )

    resumed_llm = ScriptedLLM(
        [_llm_response(content="Understood, not issuing the refund.", stop_reason="end_turn")]
    )
    resumed = Orchestrator(store=store, llm=resumed_llm, tools=_tools())
    state = await resumed.run(run_id)

    assert state.status == "completed"
    assert resumed_llm.call_count == 1
    assert any(
        m.role == "tool" and m.content is not None and "refund too large" in m.content
        for m in state.messages
    )

    event_types = [type(e).__name__ for e in await store.read(run_id)]
    assert "ToolCallRequested" not in event_types  # denied call must never actually execute
    assert event_types.count("ApprovalRequested") == 1


@pytest.mark.asyncio
async def test_guardrail_blocks_before_executing_when_policy_cap_exceeded() -> None:
    # The exact worked example from the guardrail design discussion: an
    # injected instruction convinces the model to ask for far more than
    # the policy cap allows. L3's bounds check is deterministic, so it
    # catches this regardless of how the model got there.
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    args = {"order_id": "A-8891", "amount_inr": 500000, "reason": "damaged"}
    llm = ScriptedLLM(
        [
            _llm_response(
                content="Issuing the refund.",
                tool_calls=[ToolCallInvocation(id="t1", name="issue_refund", arguments=args)],
                stop_reason="tool_use",
            ),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=llm, tools=_tools())
    state = await orchestrator.run(run_id)

    assert state.status == "failed"
    assert state.failure_reason == "guardrail_block"

    event_types = [type(e).__name__ for e in await store.read(run_id)]
    assert "GuardrailTriggered" in event_types
    # The dangerous side effect never ran — blocked before execution,
    # not after.
    assert "ToolCallRequested" not in event_types


@pytest.mark.asyncio
async def test_guardrail_escalation_forces_approval_below_tools_own_threshold() -> None:
    # issue_refund's own requires_approval only fires above ₹5,000 — this
    # call asks for ₹3,000, which alone would never need a human. Three
    # unrelated PII hits earlier in the same run should still force
    # approval anyway, per the "guardrails and approval compose" design.
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(
        run_id,
        0,
        RunStarted(
            seq=0,
            created_at=_now(),
            goal="Refund for a@x.com, b@x.com, and c@x.com please.",
            model="scripted",
            system_prompt_hash="sha256:test",
            max_steps=15,
            max_cost_usd=Decimal("2.00"),
            requested_by="support-queue",
            guardrail_profile="financial_v1",
        ),
    )

    args = {"order_id": "A-8891", "amount_inr": 3000, "reason": "damaged"}
    llm = ScriptedLLM(
        [
            _llm_response(
                content="Issuing a small refund.",
                tool_calls=[ToolCallInvocation(id="t1", name="issue_refund", arguments=args)],
                stop_reason="tool_use",
            ),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=llm, tools=_tools())
    state = await orchestrator.run(run_id)

    assert state.status == "awaiting_approval"
    assert state.pending_approval is not None
    assert state.pending_approval.tool == "issue_refund"

    event_types = [type(e).__name__ for e in await store.read(run_id)]
    assert event_types.count("GuardrailTriggered") == 4  # 3 L1 PII hits + the L4 escalation hit
    assert "ApprovalRequested" in event_types
    # Escalated, not executed — the tool call never actually ran.
    assert "ToolCallRequested" not in event_types


@pytest.mark.asyncio
async def test_hallucinated_tool_recovers_instead_of_looping() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started())

    llm = ScriptedLLM(
        [
            _llm_response(
                content="Let me use a made-up tool.",
                tool_calls=[
                    ToolCallInvocation(id="t1", name="not_a_real_tool", arguments={})
                ],
                stop_reason="tool_use",
            ),
            _llm_response(content="Never mind, here's my answer.", stop_reason="end_turn"),
        ]
    )

    orchestrator = Orchestrator(store=store, llm=llm, tools=_tools())
    state = await orchestrator.run(run_id)

    assert state.status == "completed"
    assert state.final_answer == "Never mind, here's my answer."
    # Proves the fix: the model got called again after the failure,
    # instead of the orchestrator looping on the same bad tool name.
    assert llm.call_count == 2

    events = await store.read(run_id)
    event_types = [type(e).__name__ for e in events]
    assert "ToolCallFailed" in event_types
    assert event_types.count("LLMCallRequested") == 2


@pytest.mark.asyncio
async def test_step_cap_exceeded_fails_run() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started(max_steps=1))

    # Script keeps calling a harmless tool forever; the cap should stop it
    # long before the script runs out of responses.
    llm = ScriptedLLM(
        [
            _llm_response(
                tool_calls=[
                    ToolCallInvocation(id="t1", name="lookup_order", arguments={"order_id": "A-8891"})
                ],
                stop_reason="tool_use",
            )
            for _ in range(10)
        ]
    )

    orchestrator = Orchestrator(store=store, llm=llm, tools=_tools())
    state = await orchestrator.run(run_id)

    assert state.status == "failed"
    assert state.failure_reason == "max_steps_exceeded"


@pytest.mark.asyncio
async def test_cost_cap_exceeded_fails_run() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started(max_cost_usd=Decimal("0.001")))

    llm = ScriptedLLM([_llm_response(cost_usd=Decimal("1.00"), stop_reason="end_turn")])

    orchestrator = Orchestrator(store=store, llm=llm, tools=_tools())
    state = await orchestrator.run(run_id)

    assert state.status == "failed"
    assert state.failure_reason == "max_cost_exceeded"
