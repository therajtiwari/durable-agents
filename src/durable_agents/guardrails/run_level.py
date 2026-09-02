from durable_agents.events import ToolCallInvocation
from durable_agents.guardrails.types import GuardMatch
from durable_agents.state import RunState


def detect_loop(
    state: RunState, tool_call: ToolCallInvocation, threshold: int = 3
) -> GuardMatch | None:
    """Same tool + same arguments requested `threshold` times total
    (including this proposed call) → signal. Pure, over state.messages —
    every past tool call the model made is already sitting in the
    assistant messages that produced it, no separate history needed.
    """

    past_calls = [
        call
        for message in state.messages
        if message.role == "assistant" and message.tool_calls
        for call in message.tool_calls
    ]
    repeat_count = sum(
        1 for call in past_calls if call.name == tool_call.name and call.arguments == tool_call.arguments
    )
    occurrences = repeat_count + 1
    if occurrences >= threshold:
        return GuardMatch(
            rule="loop_detected",
            confidence=1.0,
            detail={"tool": tool_call.name, "arguments": tool_call.arguments, "occurrences": occurrences},
        )
    return None


def detect_escalation(state: RunState, threshold: int) -> GuardMatch | None:
    """N guardrail hits total this run → force human review regardless
    of what any single check decided on its own. This is what lets
    guardrails and the approval flow compose, per docs/THREAT_MODEL.md.
    """

    hit_count = len(state.guardrail_hits)
    if hit_count >= threshold:
        return GuardMatch(
            rule="escalation_threshold_reached",
            confidence=1.0,
            detail={"hit_count": hit_count, "threshold": threshold},
        )
    return None
