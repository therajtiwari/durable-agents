from durable_agents.events import ToolCallInvocation
from durable_agents.guardrails.run_level import detect_escalation, detect_loop
from durable_agents.state import GuardrailHit, Message, RunState


def _state_with_past_calls(calls: list[ToolCallInvocation]) -> RunState:
    messages = [Message(role="user", content="goal")]
    for call in calls:
        messages.append(Message(role="assistant", content=None, tool_calls=[call]))
        messages.append(Message(role="tool", content="{}", tool_name=call.name))
    return RunState(status="running", messages=messages)


def test_no_loop_on_first_attempt() -> None:
    state = _state_with_past_calls([])
    call = ToolCallInvocation(id="t1", name="issue_refund", arguments={"order_id": "A-8891"})
    assert detect_loop(state, call) is None


def test_loop_detected_at_threshold() -> None:
    call = ToolCallInvocation(id="t1", name="issue_refund", arguments={"order_id": "A-8891"})
    # Two identical past attempts + this proposed third one == threshold.
    state = _state_with_past_calls([call, call])
    match = detect_loop(state, call, threshold=3)
    assert match is not None
    assert match.rule == "loop_detected"
    assert match.detail["occurrences"] == 3


def test_different_arguments_do_not_count_as_a_loop() -> None:
    call_a = ToolCallInvocation(id="t1", name="issue_refund", arguments={"order_id": "A-8891"})
    call_b = ToolCallInvocation(id="t2", name="issue_refund", arguments={"order_id": "B-1234"})
    state = _state_with_past_calls([call_a, call_a])
    assert detect_loop(state, call_b, threshold=3) is None


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
