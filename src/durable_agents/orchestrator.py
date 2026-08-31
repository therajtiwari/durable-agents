import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, assert_never
from uuid import UUID

from durable_agents.events import (
    ApprovalRequested,
    Event,
    LLMCallCompleted,
    LLMCallRequested,
    RunCompleted,
    RunFailed,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallInvocation,
    ToolCallRequested,
)
from durable_agents.llm.protocol import LLMClient
from durable_agents.state import RunState, rebuild_state
from durable_agents.storage.protocol import EventStore
from durable_agents.tools.registry import Tool, idempotency_key


@dataclass(frozen=True)
class CallLLM:
    pass


@dataclass(frozen=True)
class ExecuteTool:
    tool_call: ToolCallInvocation


@dataclass(frozen=True)
class Finish:
    final_answer: str


Decision = CallLLM | ExecuteTool | Finish


def decide_next_action(state: RunState) -> Decision:
    """Pure: given the current state, what should happen next?

    No I/O, no clock, no randomness — kept separate from the impure
    orchestrator so this stays as easy to unit test as rebuild_state. Only ever called
    when state.in_flight is None — the caller (Orchestrator.run) reconciles
    any in-flight operation before this is reached.
    """

    if state.status == "not_started":
        raise ValueError("cannot decide an action for a run with no RunStarted event yet")

    last = state.messages[-1]
    if last.role == "assistant":
        if last.tool_calls:
            return ExecuteTool(tool_call=last.tool_calls[0])
        return Finish(final_answer=last.content or "")
    return CallLLM()


def _tool_schemas(tools: dict[str, Tool]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters}
        for t in tools.values()
    ]


def _estimate_tokens(state: RunState) -> int:
    """Rough pre-call estimate only — the real count comes back with the
    response. ~4 characters per token is a common English-text
    approximation, good enough for a pre-flight cap check.
    """

    total_chars = sum(len(m.content or "") for m in state.messages)
    return total_chars // 4


class Orchestrator:
    """The agent loop.

    Reloads events and rebuilds state at the top of every iteration, on
    purpose: it proves the loop holds no hidden memory, and it means
    normal operation and crash recovery run through the exact same code
    path. A dangling Requested event looks identical whether this process
    appended it a moment ago or a different process appended it and then
    died — rebuild_state can't tell the difference, and neither does this
    loop. It just finishes whatever's in flight.

    Known Week 2 simplifications, not yet built:
    - No guardrails (Week 5) — nothing runs between decide and act yet.
    - Approval can be REQUESTED and the run correctly parks, but nothing
      here can GRANT it and resume the approved tool call — that's Week 4
      (the FastAPI endpoints + the resume entry point).
    """

    def __init__(
        self,
        store: EventStore,
        llm: LLMClient,
        tools: dict[str, Tool],
        kill_after_seq: int | None = None,
        kill_after_tool_execution_seq: int | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._tools = tools
        # Chaos-testing hooks only — both None in every normal path, since
        # nothing sets them unless a test explicitly asks for a kill point.
        # Passed explicitly rather than read from an env var here, to keep
        # this class itself unaware of any particular env var name (the
        # chaos test's own runner script owns that lookup).
        #
        # Two separate hooks, not one, because they test genuinely
        # different gaps: kill_after_seq fires right after an event is
        # durably recorded (e.g. right after ToolCallRequested — the tool
        # was never actually called yet, so a resumed reconcile calls it
        # exactly once). kill_after_tool_execution_seq fires right after
        # a tool's side effect actually ran but before its Completed is
        # recorded — the side effect already happened once; a resumed
        # reconcile calls the tool again, and only the idempotency key
        # stops that from becoming a second real refund. There is no
        # event seq that identifies that second moment on its own, since
        # it happens inside one reconcile() call between two appends.
        self._kill_after_seq = kill_after_seq
        self._kill_after_tool_execution_seq = kill_after_tool_execution_seq
        # Tracks, for the current run() call only, which seqs THIS
        # invocation itself appended a Requested for. Reset at the top of
        # every run() call — a dangling op already sitting in the log
        # before this invocation even started reading events is a real
        # recovery, whether that's because a different process died or
        # because a previous, separate run() call on this same instance
        # left it (run() only returns once in_flight is resolved, so in
        # practice this only ever means "a different process").
        self._requested_this_run: set[int] = set()

    def _maybe_chaos_kill(self) -> None:
        # SIGKILL doesn't exist on Windows. os.kill() there calls
        # TerminateProcess for anything except Ctrl+C/Ctrl+Break — a
        # genuinely abrupt kill (verified: no code after this line runs),
        # just not literally named SIGKILL on this platform.
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        os.kill(os.getpid(), kill_signal)

    async def _append(self, run_id: UUID, seq: int, event: Event) -> None:
        await self._store.append(run_id, seq, event)
        if self._kill_after_seq is not None and seq == self._kill_after_seq:
            self._maybe_chaos_kill()

    async def run(self, run_id: UUID) -> RunState:
        self._requested_this_run = set()
        while True:
            events = await self._store.read(run_id)
            state = rebuild_state(events)
            next_seq = len(events)
            now = datetime.now(timezone.utc)

            if state.status in ("completed", "failed", "awaiting_approval"):
                return state

            if state.max_steps is not None and state.step >= state.max_steps:
                await self._append(
                    run_id,
                    next_seq,
                    RunFailed(
                        seq=next_seq, created_at=now, reason="max_steps_exceeded", detail=None
                    ),
                )
                continue

            if state.max_cost_usd is not None and state.total_cost_usd >= state.max_cost_usd:
                await self._append(
                    run_id,
                    next_seq,
                    RunFailed(
                        seq=next_seq, created_at=now, reason="max_cost_exceeded", detail=None
                    ),
                )
                continue

            if state.in_flight is not None:
                recovered = state.in_flight.seq not in self._requested_this_run
                await self._reconcile(run_id, next_seq, now, state, recovered)
                continue

            decision = decide_next_action(state)
            match decision:
                case CallLLM():
                    await self._append(
                        run_id,
                        next_seq,
                        LLMCallRequested(
                            seq=next_seq,
                            created_at=now,
                            step=state.step + 1,
                            message_count=len(state.messages),
                            estimated_tokens=_estimate_tokens(state),
                        ),
                    )
                case ExecuteTool(tool_call=tool_call):
                    await self._request_tool_call(run_id, next_seq, now, state, tool_call)
                case Finish(final_answer=final_answer):
                    await self._append(
                        run_id,
                        next_seq,
                        RunCompleted(
                            seq=next_seq,
                            created_at=now,
                            final_answer=final_answer,
                            total_steps=state.step,
                            total_tokens=state.total_tokens,
                            total_cost_usd=state.total_cost_usd,
                        ),
                    )
                case _:
                    assert_never(decision)

    async def _request_tool_call(
        self,
        run_id: UUID,
        next_seq: int,
        now: datetime,
        state: RunState,
        tool_call: ToolCallInvocation,
    ) -> None:
        tool_obj = self._tools.get(tool_call.name)
        if tool_obj is None:
            await self._append(
                run_id,
                next_seq,
                ToolCallFailed(
                    seq=next_seq,
                    created_at=now,
                    step=state.step,
                    tool=tool_call.name,
                    arguments=tool_call.arguments,
                    idempotency_key="",
                    error=f"unknown tool: {tool_call.name}",
                    attempt=1,
                ),
            )
            return

        if tool_obj.requires_approval(tool_call.arguments):
            await self._append(
                run_id,
                next_seq,
                ApprovalRequested(
                    seq=next_seq,
                    created_at=now,
                    step=state.step,
                    tool=tool_call.name,
                    arguments=tool_call.arguments,
                    reason=f"{tool_call.name} requires approval for these arguments",
                ),
            )
            return

        key = idempotency_key(run_id, next_seq, tool_call.name, tool_call.arguments)
        self._requested_this_run.add(next_seq)
        await self._append(
            run_id,
            next_seq,
            ToolCallRequested(
                seq=next_seq,
                created_at=now,
                step=state.step,
                tool=tool_call.name,
                arguments=tool_call.arguments,
                idempotency_key=key,
                requires_approval=False,
            ),
        )

    async def _reconcile(
        self, run_id: UUID, next_seq: int, now: datetime, state: RunState, recovered: bool
    ) -> None:
        op = state.in_flight
        assert op is not None

        if op.kind == "llm":
            response = await self._llm.call(state.messages, _tool_schemas(self._tools))
            await self._append(
                run_id,
                next_seq,
                LLMCallCompleted(
                    seq=next_seq,
                    created_at=now,
                    step=op.step,
                    content=response.content,
                    tool_calls=response.tool_calls,
                    stop_reason=response.stop_reason,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cost_usd=response.cost_usd,
                    latency_ms=response.latency_ms,
                    provider_request_id=response.provider_request_id,
                ),
            )
        else:
            assert op.tool is not None and op.arguments is not None
            tool_obj = self._tools[op.tool]
            kwargs = dict(op.arguments)
            if tool_obj.needs_idempotency_key:
                kwargs["idempotency_key"] = op.idempotency_key
            start = time.monotonic()
            result = await tool_obj.execute(**kwargs)
            duration_ms = int((time.monotonic() - start) * 1000)
            if (
                self._kill_after_tool_execution_seq is not None
                and next_seq == self._kill_after_tool_execution_seq
            ):
                # The side effect above already happened. Killing here,
                # before its Completed is recorded, is the exact gap
                # only the idempotency key protects against.
                self._maybe_chaos_kill()
            provider_dedup_hit = bool(result.get("dedup_hit")) if isinstance(result, dict) else False
            await self._append(
                run_id,
                next_seq,
                ToolCallCompleted(
                    seq=next_seq,
                    created_at=now,
                    step=op.step,
                    tool=op.tool,
                    idempotency_key=op.idempotency_key or "",
                    result=result,
                    duration_ms=duration_ms,
                    recovered=recovered,
                    provider_dedup_hit=provider_dedup_hit,
                ),
            )
