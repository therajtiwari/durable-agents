"""Regressions for defects found by adversarial QA rather than by design.

Each of these passed the whole suite before it was fixed, which is the
point: they are things a stranger does to the library, not things the
code was written to do.
"""

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from durable_agents import (
    InMemoryEventStore,
    LLMResponse,
    Orchestrator,
    RunStarted,
    ScriptedLLM,
    Runtime,
    ToolCallInvocation,
    Worker,
    tool,
)
from durable_agents.api.app import create_app
from durable_agents.orchestrator import normalize_tool_result
from durable_agents.events import ApprovalRequested, ToolCallCompleted, ToolCallFailed


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _started(profile: str = "validation") -> RunStarted:
    return RunStarted(
        seq=0, created_at=_now(), goal="g", model="s", max_steps=10,
        max_cost_usd=Decimal("1"), requested_by="qa", guardrail_profile=profile,
    )


def _resp(**overrides: Any) -> LLMResponse:
    d: dict[str, Any] = {"content": None, "tool_calls": [], "stop_reason": "end_turn",
                         "input_tokens": 1, "output_tokens": 1, "cost_usd": Decimal("0"),
                         "latency_ms": 1, "provider_request_id": "r"}
    d.update(overrides)
    return LLMResponse(**d)


# --- a model getting an argument wrong must not end the run ------------


def _int_tool() -> tuple[dict[str, Any], list[int]]:
    seen: list[int] = []

    @tool()
    async def needs_int(n: int) -> dict[str, Any]:
        """Wants an int."""
        seen.append(n)
        return {"got": n}

    return {needs_int.name: needs_int}, seen


async def _run_with_bad_then_good(
    profile: str, bad: dict[str, Any]
) -> tuple[Any, InMemoryEventStore, Any, list[int]]:
    """One bad tool call, then the same tool called correctly, then done.
    Returns (state, store, run_id, arguments the tool actually received).
    """

    tools, seen = _int_tool()
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _started(profile))
    llm = ScriptedLLM([
        _resp(tool_calls=[ToolCallInvocation(id="c1", name="needs_int", arguments=bad)],
              stop_reason="tool_use"),
        _resp(tool_calls=[ToolCallInvocation(id="c2", name="needs_int", arguments={"n": 5})],
              stop_reason="tool_use"),
        _resp(content="recovered"),
    ])
    state = await Orchestrator(store=store, llm=llm, tools=tools,
                               retry_base_delay_seconds=0).run(run_id)
    return state, store, run_id, seen


@pytest.mark.parametrize("profile", ["validation", "standard"])
@pytest.mark.asyncio
async def test_wrong_typed_argument_is_returned_to_the_model_not_fatal(profile: str) -> None:
    """The most common tool-calling mistake there is. It used to end the
    run under every profile except "off", which made the safety layer
    measurably worse than no safety layer.
    """

    state, store, run_id, seen = await _run_with_bad_then_good(profile, {"n": "not-a-number"})

    assert state.status == "completed"
    assert state.final_answer == "recovered"
    assert seen == [5], "the invalid call must not reach the tool; the corrected one must"

    # The block is still recorded truthfully — the call was blocked, the
    # run was not.
    events = await store.read(run_id)
    assert any(
        e.type == "GuardrailTriggered" and e.rule == "schema_invalid" and e.action == "BLOCK"
        for e in events
    )
    assert not any(e.type == "RunFailed" for e in events)


@pytest.mark.asyncio
async def test_the_model_is_told_what_was_wrong_with_its_arguments() -> None:
    tools, _seen = _int_tool()
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _started())
    llm = ScriptedLLM([
        _resp(tool_calls=[ToolCallInvocation(id="c1", name="needs_int",
                                             arguments={"n": "not-a-number"})],
              stop_reason="tool_use"),
        _resp(content="ok"),
    ])
    await Orchestrator(store=store, llm=llm, tools=tools, retry_base_delay_seconds=0).run(run_id)

    events = await store.read(run_id)
    failures = [e for e in events if isinstance(e, ToolCallFailed)]
    assert len(failures) == 1
    assert failures[0].final_attempt is True
    assert failures[0].tool_call_id == "c1", "must answer that call, or the batch stalls"
    assert "invalid arguments" in failures[0].error
    assert "n" in failures[0].error, "the model needs to know which argument"
    assert not any(e.type == "RunFailed" for e in events)


@pytest.mark.asyncio
async def test_an_unaccepted_argument_is_not_retried_three_times() -> None:
    """A wrong keyword is deterministic. It used to reach the function,
    raise TypeError, and burn the whole retry budget with backoff.
    """

    state, store, run_id, seen = await _run_with_bad_then_good(
        "validation", {"n": 1, "surprise": True}
    )

    assert state.status == "completed"
    assert seen == [5], "the call with a bogus keyword must never reach the function"

    failures = [e for e in await store.read(run_id) if isinstance(e, ToolCallFailed)]
    assert len(failures) == 1, f"one rejection, not a retry storm — got {len(failures)}"


@pytest.mark.asyncio
async def test_a_real_policy_violation_still_ends_the_run() -> None:
    """The relaxation must not extend to violations where continuing is
    the unsafe direction.
    """

    @tool(side_effect=True)
    async def issue_refund(order_id: str, amount_inr: int, idempotency_key: str) -> dict[str, Any]:
        """Issue a refund."""
        return {"refund_id": "RF-1"}

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _started())
    llm = ScriptedLLM([
        _resp(tool_calls=[ToolCallInvocation(id="c1", name="issue_refund",
                                             arguments={"order_id": "A", "amount_inr": 500_000})],
              stop_reason="tool_use"),
    ])
    state = await Orchestrator(store=store, llm=llm,
                               tools={issue_refund.name: issue_refund}).run(run_id)
    assert state.status == "failed"
    assert state.failure_reason == "guardrail_block"


# --- tool signatures that produced broken schemas ----------------------


def test_star_args_are_rejected_at_registration() -> None:
    with pytest.raises(ValueError, match="cannot use"):
        @tool()
        async def bad(*args: int) -> dict[str, Any]:
            """No."""
            return {}


def test_pydantic_reserved_parameter_names_are_rejected() -> None:
    """Used to fail with "TypeError: 'ellipsis' object is not iterable",
    which names nothing the caller wrote.
    """

    with pytest.raises(ValueError, match="reserved by Pydantic"):
        @tool()
        async def bad(model_config: str) -> dict[str, Any]:
            """No."""
            return {}


def test_missing_type_annotation_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing a type annotation"):
        @tool()
        async def bad(x) -> dict[str, Any]:  # type: ignore[no-untyped-def]
            """No."""
            return {}


def test_schema_tells_the_provider_not_to_invent_fields() -> None:
    @tool()
    async def fine(a: int) -> dict[str, Any]:
        """Fine."""
        return {}

    assert fine.parameters["additionalProperties"] is False


# --- limits -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_negative_limit_does_not_silently_hide_approvals() -> None:
    """[:-1] quietly drops the last row. On the queue humans use to find
    work, that means a request nobody ever sees.
    """

    store = InMemoryEventStore()
    for _ in range(3):
        run_id = uuid4()
        await store.append(run_id, 0, _started())
        await store.append(run_id, 1, ApprovalRequested(
            seq=1, created_at=_now(), step=1, tool="t", arguments={}, reason="r"))

    assert len(await store.find_awaiting_approval(limit=3)) == 3
    assert await store.find_awaiting_approval(limit=-1) == []
    assert await store.find_awaiting_approval(limit=0) == []
    assert await store.find_resumable_runs(stale_after_seconds=0, limit=-1) == []


# --- API input validation ----------------------------------------------


def _client() -> TestClient:
    return TestClient(create_app(InMemoryEventStore()))


@pytest.mark.parametrize(
    "body",
    [
        {"goal": ""},
        {"goal": "g", "max_steps": -5},
        {"goal": "g", "max_steps": 0},
        {"goal": "g", "max_cost_usd": -1},
        {"goal": "g", "max_cost_usd": 0},
        {"goal": "g", "guardrail_profile": "strct"},
    ],
)
def test_runs_that_could_never_succeed_are_refused_at_submission(body: dict[str, Any]) -> None:
    """A run is recorded into an append-only log, so a value accepted here
    can never be corrected. These used to return 201 and then either fail
    on first execution with a misleading reason, or — for an unknown
    profile — be retried by the worker forever, never reaching any
    terminal state.
    """

    assert _client().post("/runs", json=body).status_code == 422


def test_a_valid_run_is_still_accepted() -> None:
    r = _client().post("/runs", json={"goal": "do the thing", "max_steps": 5,
                                      "max_cost_usd": 0.5, "guardrail_profile": "strict"})
    assert r.status_code == 201


@pytest.mark.parametrize("limit", [-1, 0, 100_000])
def test_out_of_range_approval_limits_are_refused(limit: int) -> None:
    assert _client().get("/approvals", params={"limit": limit}).status_code == 422


# --- guardrail scanning cost -------------------------------------------


def test_scanning_a_large_tool_result_is_not_quadratic() -> None:
    """The email pattern used to backtrack across long runs of identifier
    characters: 80 KB took 7.5 seconds of CPU, synchronously, inside the
    event loop — stalling every other run the worker was handling.

    Timed loosely on purpose. The bound only has to separate linear from
    quadratic, and a tight one would be flaky on a shared CI runner.
    """

    from durable_agents.guardrails.patterns import scan_patterns, scan_pii

    blob = "A" * 200_000
    start = time.perf_counter()
    scan_patterns(blob)
    scan_pii(blob)
    elapsed = time.perf_counter() - start
    assert elapsed < 3.0, f"200 KB scan took {elapsed:.1f}s — quadratic behaviour is back"


def test_pii_detection_still_works_after_the_performance_fix() -> None:
    from durable_agents.guardrails.patterns import scan_pii

    for text, expected in [
        ("jane.doe@example.com", True),
        ("Contact: x.y+z%1@sub.domain.co.uk.", True),
        ("a@b.co", True),
        ("no address here", False),
        ("user@localhost", False),
    ]:
        matches, _ = scan_pii(text)
        assert any(m.rule == "pii_email" for m in matches) is expected, text


# --- whatever a tool returns must never cost the run its outcome -------
#
# The critical one. A tool is ordinary application code returning
# ordinary Python. Every value below used to raise while building or
# serialising ToolCallCompleted — after the tool had already run — so
# nothing recorded the outcome, the log kept a dangling
# ToolCallRequested, and a Worker read that as "unfinished" and called
# the tool again on every poll, forever.


@pytest.mark.parametrize(
    ("returns", "expected"),
    [
        ({"ok": True}, {"ok": True}),
        ("refund RF-1 processed", {"result": "refund RF-1 processed"}),
        (None, {"result": None}),
        (42, {"result": 42}),
        ([1, 2, 3], {"result": [1, 2, 3]}),
        ({"amount": Decimal("10.50")}, {"amount": "10.50"}),
        # Bytes have no JSON form; their str() is the honest record.
        ({"blob": b"\x00"}, {"blob": str(b"\x00")}),
        ({1: "one"}, {"1": "one"}),
        ({"score": float("nan")}, {"score": "nan"}),
        ({"score": float("inf")}, {"score": "inf"}),
        ({"nested": {"deep": [Decimal("1"), None, True]}}, {"nested": {"deep": ["1", None, True]}}),
    ],
)
def test_normalize_tool_result_is_total(returns: Any, expected: dict[str, Any]) -> None:
    assert normalize_tool_result(returns) == expected


def test_normalize_survives_values_designed_to_break_it() -> None:
    """It runs after a side effect, so it is not allowed to raise —
    whatever someone's object does.
    """

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("no")

    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic

    assert "unrepresentable" in normalize_tool_result({"x": Hostile()})["x"]
    assert isinstance(normalize_tool_result(cyclic), dict)  # depth-capped, no recursion error
    assert json.dumps(normalize_tool_result(cyclic))  # and still serialisable


@pytest.mark.parametrize(
    "returns",
    [{"ok": 1}, "a string", None, [1, 2], 42, {"d": Decimal("1.5")}, {"score": float("nan")}],
)
@pytest.mark.asyncio
async def test_any_tool_return_still_records_exactly_one_outcome(returns: Any) -> None:
    side_effects: list[int] = []

    @tool(side_effect=True)
    async def act(x: int) -> Any:
        """Does something irreversible."""
        side_effects.append(x)
        return returns

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _started())
    llm = ScriptedLLM([
        _resp(tool_calls=[ToolCallInvocation(id="c1", name="act", arguments={"x": 1})],
              stop_reason="tool_use"),
        _resp(content="done"),
    ])
    state = await Orchestrator(store=store, llm=llm, tools={act.name: act},
                               retry_base_delay_seconds=0).run(run_id)

    events = await store.read(run_id)
    assert state.status == "completed"
    assert len(side_effects) == 1
    assert sum(1 for e in events if e.type == "ToolCallCompleted") == 1
    # And what was recorded is something a JSONB column can actually hold.
    completed = next(e for e in events if isinstance(e, ToolCallCompleted))
    json.dumps(completed.model_dump(mode="json"), allow_nan=False)


@pytest.mark.asyncio
async def test_a_worker_does_not_repeat_a_side_effect_it_could_not_record() -> None:
    """The headline failure: five polls used to mean five real emails
    from one requested action, because the outcome was never recorded and
    the run therefore always looked unfinished.
    """

    sent: list[str] = []

    @tool(side_effect=True)
    async def send_email(to: str) -> Any:
        """Sends an email and returns nothing, like most such APIs."""
        sent.append(to)
        return None

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _started())

    def fresh_llm() -> ScriptedLLM:
        return ScriptedLLM([
            _resp(tool_calls=[ToolCallInvocation(id="c1", name="send_email",
                                                 arguments={"to": "dana@example.com"})],
                  stop_reason="tool_use"),
            _resp(content="sent"),
        ])

    runtime = Runtime(store=store, llm=fresh_llm(), tools=[send_email],
                      retry_base_delay_seconds=0)
    worker = Worker(runtime, stale_after_seconds=0.0, poll_interval_seconds=0.0)

    for _ in range(5):
        runtime._orchestrator._llm = fresh_llm()  # a fresh process each poll
        await worker.poll_once()

    assert sent == ["dana@example.com"], f"one action, one email — got {len(sent)}"
    assert (await runtime.get_state(run_id)).status == "completed"
