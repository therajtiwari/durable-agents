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
    guardrail_hits: list[GuardrailHit] = field(default_factory=list)
    max_steps: int | None = None
    max_cost_usd: Decimal | None = None
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
            # anything; a retry shows up as a fresh LLMCallRequested.
            return state

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
            # Same reasoning as LLMCallFailed: the tool call stays
            # dangling until a Completed (recovered or fresh) clears it.
            return state

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
            return replace(state, status="running", pending_approval=None)

        case ApprovalDenied():
            return replace(state, status="running", pending_approval=None)

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
