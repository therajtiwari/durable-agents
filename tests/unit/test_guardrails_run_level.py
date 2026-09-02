from durable_agents.events import ToolCallInvocation
from durable_agents.guardrails.run_level import detect_escalation, detect_loop
from durable_agents.state import GuardrailHit, Message, RunState
from durable_agents.tools.refund_tools import InMemoryRefundBackend, build_refund_tools
from durable_agents.tools.registry import Tool


def _tools() -> dict[str, Tool]:
    # issue_refund: side_effect=True. lookup_order/check_refund_policy:
    # side_effect=False — exactly the read-only/side-effecting split
    # detect_loop now cares about.
    return {t.name: t for t in build_refund_tools(InMemoryRefundBackend())}


def _state_with_past_calls(calls: list[ToolCallInvocation]) -> RunState:
    messages = [Message(role="user", content="goal")]
    for call in calls:
        messages.append(Message(role="assistant", content=None, tool_calls=[call]))
        messages.append(Message(role="tool", content="{}", tool_name=call.name))
    return RunState(status="running", messages=messages)


def test_no_loop_on_first_attempt() -> None:
    state = _state_with_past_calls([])
    call = ToolCallInvocation(id="t1", name="issue_refund", arguments={"order_id": "A-8891"})
    assert detect_loop(state, call, _tools()) is None


def test_loop_detected_at_threshold_for_a_side_effecting_tool() -> None:
    call = ToolCallInvocation(id="t1", name="issue_refund", arguments={"order_id": "A-8891"})
    # Two identical past attempts + this proposed third one == threshold.
    state = _state_with_past_calls([call, call])
    match = detect_loop(state, call, _tools(), threshold=3)
    assert match is not None
    assert match.rule == "loop_detected"
    assert match.detail["occurrences"] == 3


def test_different_arguments_do_not_count_as_a_loop() -> None:
    call_a = ToolCallInvocation(id="t1", name="issue_refund", arguments={"order_id": "A-8891"})
    call_b = ToolCallInvocation(id="t2", name="issue_refund", arguments={"order_id": "B-1234"})
    state = _state_with_past_calls([call_a, call_a])
    assert detect_loop(state, call_b, _tools(), threshold=3) is None


def test_repeated_read_only_call_is_not_a_loop() -> None:
    """Regression: an incident-response agent legitimately re-checking
    read-only status (before a fix, during, and after to verify) is
    normal behavior, not a stuck agent. lookup_order has no side
    effect, so three identical calls must not be flagged.
    """

    call = ToolCallInvocation(id="t1", name="lookup_order", arguments={"order_id": "A-8891"})
    state = _state_with_past_calls([call, call])
    assert detect_loop(state, call, _tools(), threshold=3) is None


def test_unregistered_tool_is_not_flagged_as_a_loop() -> None:
    # Not this function's job — L3's allowlist check owns unregistered
    # tools. detect_loop should just decline to opine.
    call = ToolCallInvocation(id="t1", name="not_a_real_tool", arguments={})
    state = _state_with_past_calls([call, call])
    assert detect_loop(state, call, _tools(), threshold=3) is None


def test_no_escalation_below_threshold() -> None:
    state = RunState(
        status="running",
        guardrail_hits=[GuardrailHit(layer="L1_input", rule="x", action="ALLOW", step=1)],
    )
    assert detect_escalation(state, threshold=3) is None


def test_escalation_at_threshold() -> None:
    hits = [GuardrailHit(layer="L2_tool_result", rule="x", action="REDACT", step=1) for _ in range(3)]
    state = RunState(status="running", guardrail_hits=hits)
    match = detect_escalation(state, threshold=3)
    assert match is not None
    assert match.rule == "escalation_threshold_reached"
    assert match.detail["hit_count"] == 3
