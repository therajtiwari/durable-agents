import json
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Literal, assert_never

from durable_agents.events import (
    ApprovalDenied,
    ApprovalGranted,
    ApprovalRequested,
    Event,
    GuardrailAction,
    GuardrailLayer,
    GuardrailTriggered,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallRequested,
    RunCompleted,
    RunFailed,
    RunStarted,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallInvocation,
    ToolCallRequested,
)


@dataclass(frozen=True)
class Message:
    """A single turn in the conversation sent to the LLM.

    Deliberately minimal and provider-agnostic for now. No LLM client
    exists yet (week 2), so this shape is provisional — it may need to
    change once there's a real provider API to match.
    """

    role: Literal["user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCallInvocation] | None = None
    tool_name: str | None = None


@dataclass(frozen=True)
class InFlightOp:
    """A Requested event with no matching Completed/Failed yet.

    A non-null value here on a freshly rebuilt state is the entire crash
    recovery signal: it means the process died between recording intent
    and recording an outcome.
    """

    kind: Literal["llm", "tool"]
    seq: int
    step: int
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    idempotency_key: str | None = None
    attempts: int = 0
    """Failed attempts at this operation so far. Lives here rather than
    in the orchestrator so a resumed process picks up the retry budget
    exactly where a dead one left off — a retry count held in a local
    variable would reset to zero on every crash, which for a flapping
    provider means retrying forever.
    """


@dataclass(frozen=True)
class PendingApproval:
    step: int
    tool: str
    arguments: dict[str, Any]
    reason: str
    requested_at_seq: int


@dataclass(frozen=True)
class GuardrailHit:
    layer: GuardrailLayer
    rule: str
    action: GuardrailAction
    step: int | None


RunStatus = Literal["not_started", "running", "awaiting_approval", "completed", "failed"]


@dataclass(frozen=True)
class RunState:
    """The complete state of one run, derived entirely from its event log.

    No run_id field: rebuild_state operates on a list[Event] alone, and
    Event objects never
    carry run_id (it lives only as a database column — see
    storage/postgres.py). Whoever calls rebuild_state already has the
    run_id, since they had to pass it to EventStore.read() to get these
    events in the first place. Carrying it here too would be a second
    source of truth for the same fact.
    """

    status: RunStatus = "not_started"
    messages: list[Message] = field(default_factory=list)
    step: int = 0
    in_flight: InFlightOp | None = None
    total_tokens: int = 0
    total_cost_usd: Decimal = Decimal("0")
    pending_approval: PendingApproval | None = None
    approved_step: int | None = None
    guardrail_hits: list[GuardrailHit] = field(default_factory=list)
    max_steps: int | None = None
    max_cost_usd: Decimal | None = None
    system_prompt: str = ""
    guardrail_profile: str | None = None
    final_answer: str | None = None
    failure_reason: str | None = None


def apply(state: RunState, event: Event) -> RunState:
    match event:
        case RunStarted():
            return replace(
                state,
                status="running",
                messages=[Message(role="user", content=event.goal)],
                max_steps=event.max_steps,
                max_cost_usd=event.max_cost_usd,
                system_prompt=event.system_prompt,
                guardrail_profile=event.guardrail_profile,
            )

        case LLMCallRequested():
            return replace(
                state,
                step=event.step,
                in_flight=InFlightOp(kind="llm", seq=event.seq, step=event.step),
            )

        case LLMCallCompleted():
            message = Message(
                role="assistant",
                content=event.content,
                tool_calls=event.tool_calls or None,
            )
            return replace(
                state,
                step=event.step,
                in_flight=None,
                messages=[*state.messages, message],
                total_tokens=state.total_tokens + event.input_tokens + event.output_tokens,
                total_cost_usd=state.total_cost_usd + event.cost_usd,
            )

        case LLMCallFailed():
            # The dangling LLMCallRequested stays in_flight. Only a
            # Completed clears it — a failure alone doesn't resolve
            # anything, it just means the same call gets attempted
            # again. Recording the attempt on the op is what bounds
            # that: the orchestrator reads it back to decide whether
            # another try is still within budget.
            if state.in_flight is None:
                return state
            return replace(
                state,
                in_flight=replace(state.in_flight, attempts=state.in_flight.attempts + 1),
            )

        case ToolCallRequested():
            return replace(
                state,
                step=event.step,
                in_flight=InFlightOp(
                    kind="tool",
                    seq=event.seq,
                    step=event.step,
                    tool=event.tool,
                    arguments=event.arguments,
                    idempotency_key=event.idempotency_key,
                ),
                # Consumes any pending approval grant for this step — it's
                # done its one job of letting this exact request through.
                approved_step=None,
            )

        case ToolCallCompleted():
            message = Message(
                role="tool",
                content=json.dumps(event.result),
                tool_name=event.tool,
            )
            return replace(
                state,
                step=event.step,
                in_flight=None,
                messages=[*state.messages, message],
            )

        case ToolCallFailed():
            if not event.final_attempt and state.in_flight is not None:
                # A retry of this exact call is coming, reusing the same
                # idempotency_key — so the op stays in flight and no
                # message is added. Nothing is told to the model yet
                # because nothing has been concluded yet.
                return replace(
                    state,
                    step=event.step,
                    in_flight=replace(state.in_flight, attempts=state.in_flight.attempts + 1),
                )

            # The attempt budget is spent (or there was never an op in
            # flight at all — an unknown tool name fails before any
            # ToolCallRequested is written). Clear it and surface the
            # error to the model as a tool-result message, so it decides
            # what to do next rather than the orchestrator looping on the
            # same failing call forever.
            message = Message(
                role="tool",
                content=f"Error: {event.error}",
                tool_name=event.tool,
            )
            return replace(
                state,
                step=event.step,
                in_flight=None,
                messages=[*state.messages, message],
            )

        case GuardrailTriggered():
            hit = GuardrailHit(
                layer=event.layer, rule=event.rule, action=event.action, step=event.step
            )
            return replace(state, guardrail_hits=[*state.guardrail_hits, hit])

        case ApprovalRequested():
            return replace(
                state,
                status="awaiting_approval",
                pending_approval=PendingApproval(
                    step=event.step,
                    tool=event.tool,
                    arguments=event.arguments,
                    reason=event.reason,
                    requested_at_seq=event.seq,
                ),
            )

        case ApprovalGranted():
            # decide_next_action will produce the exact same ExecuteTool
            # decision it produced before the approval was requested (the
            # assistant message that carried this tool call never
            # changed). approved_step marks that one instance as cleared
            # so the orchestrator skips requires_approval() for it,
            # rather than parking on ApprovalRequested a second time.
            approved_step = state.pending_approval.step if state.pending_approval else None
            return replace(
                state, status="running", pending_approval=None, approved_step=approved_step
            )

        case ApprovalDenied():
            # Same shape as the ToolCallFailed fix: without a message here,
            # the last message is still the assistant's tool_calls, so
            # decide_next_action would retry the identical call and hit
            # requires_approval() again — parking forever. Feeding the
            # denial back as a tool-role message makes the model react
            # instead.
            tool_name = state.pending_approval.tool if state.pending_approval else None
            message = Message(
                role="tool",
                content=f"Error: approval denied: {event.reason}",
                tool_name=tool_name,
            )
            return replace(
                state,
                status="running",
                pending_approval=None,
                messages=[*state.messages, message],
            )

        case RunCompleted():
            return replace(
                state,
                status="completed",
                step=event.total_steps,
                total_tokens=event.total_tokens,
                total_cost_usd=event.total_cost_usd,
                final_answer=event.final_answer,
            )

        case RunFailed():
            return replace(state, status="failed", failure_reason=event.reason)

        case _:
            assert_never(event)


def rebuild_state(events: list[Event]) -> RunState:
    """Fold a run's event log into its current state.

    Pure: no I/O, no clock, no randomness. Same events in, same state out,
    every time. Must remain valid for rebuild_state(events[:n]) at every
    n, including n == 0 — an empty log yields the default, not-yet-started
    state rather than raising.
    """

    state = RunState()
    for event in events:
        state = apply(state, event)
    return state
