from durable_agents.events import ToolCallInvocation
from durable_agents.guardrails.types import GuardMatch
from durable_agents.state import RunState
from durable_agents.tools.registry import Tool


def detect_loop(
    state: RunState, tool_call: ToolCallInvocation, tools: dict[str, Tool], threshold: int = 3
) -> GuardMatch | None:
    """Same tool + same arguments requested `threshold` times total
    (including this proposed call) → signal. Pure, over state.messages —
    every past tool call the model made is already sitting in the
    assistant messages that produced it, no separate history needed.

    Only applies to tools with a real side effect. A repeated read-only
    call — checking status again after a fix, polling until something
    changes — is normal investigative behavior, not a stuck agent; the
    run's own step/cost caps already bound plain wasted effort from an
    unproductive loop. This check exists for the case that's actually
    harmful: redoing something with a side effect (a second refund, a
    second rollback). Found via a real false positive: an incident-
    response agent legitimately re-checked read-only metrics right
    after applying a fix, and this blocked it as if it were stuck.
    """

    tool_obj = tools.get(tool_call.name)
    if tool_obj is None or not tool_obj.side_effect:
        return None

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
