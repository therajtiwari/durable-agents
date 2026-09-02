import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, assert_never
from uuid import UUID

from durable_agents.events import (
    ApprovalRequested,
    Event,
    GuardrailLayer,
    GuardrailTriggered,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallRequested,
    RunCompleted,
    RunFailed,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallInvocation,
    ToolCallRequested,
)
from durable_agents.guardrails.decisions import GuardrailProfile, decide, get_profile
from durable_agents.guardrails.input_scan import scan_input
from durable_agents.guardrails.output_validate import validate_output
from durable_agents.guardrails.patterns import scan_pii
from durable_agents.guardrails.run_level import detect_escalation, detect_loop
from durable_agents.guardrails.tool_result_scan import scan_tool_result, wrap_untrusted
from durable_agents.guardrails.types import GuardMatch
from durable_agents.llm.protocol import LLMClient
from durable_agents.state import Message, RunState, rebuild_state
from durable_agents.storage.protocol import ConcurrencyConflict, EventStore
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

# No handler is attached here on purpose: configuring output is the
# consuming application's job, not a library's.
logger = logging.getLogger(__name__)


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

    Known simplification, not yet built:
    - No guardrails (Week 5) — nothing runs between decide and act yet.
    """

    def __init__(
        self,
        store: EventStore,
        llm: LLMClient,
        tools: dict[str, Tool],
        max_llm_attempts: int = 3,
        max_tool_attempts: int = 3,
        retry_base_delay_seconds: float = 1.0,
        kill_after_seq: int | None = None,
        kill_after_tool_execution_seq: int | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._tools = tools
        # Retry budgets are per-operation, counted from the event log
        # (RunState.in_flight.attempts) rather than a local variable, so
        # a resumed process inherits the budget a dead one had already
        # spent instead of starting over.
        #
        # Every exception is treated as retryable: distinguishing a
        # transient 429 from a permanent 400 needs provider-specific
        # knowledge this layer deliberately doesn't have. A bounded
        # budget makes retrying a non-transient error merely wasteful
        # rather than unsafe, and tool retries reuse the original
        # idempotency_key so a repeat can't double-charge.
        self._max_llm_attempts = max_llm_attempts
        self._max_tool_attempts = max_tool_attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds
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

    async def _backoff(self, attempts: int) -> None:
        """Exponential, applied before a retry rather than after a
        failure — so a process that resumes someone else's half-retried
        operation still waits, instead of hammering a provider that was
        already failing. No jitter yet: worth adding once many runs
        retry concurrently, but it would only add nondeterminism to
        tests today.
        """

        if attempts <= 0 or self._retry_base_delay_seconds <= 0:
            return
        delay = self._retry_base_delay_seconds * (2 ** (attempts - 1))
        logger.info("backing off %.2fs before retry attempt %d", delay, attempts + 1)
        await asyncio.sleep(delay)

    def _maybe_chaos_kill(self) -> None:
        # SIGKILL doesn't exist on Windows. os.kill() there calls
        # TerminateProcess for anything except Ctrl+C/Ctrl+Break — a
        # genuinely abrupt kill (verified: no code after this line runs),
        # just not literally named SIGKILL on this platform.
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        os.kill(os.getpid(), kill_signal)

    def _sanitize_for_llm(self, messages: list[Message]) -> list[Message]:
        """Applied only to what's sent to the LLM, never to state.messages
        itself — the stored history stays the raw ground truth an
        auditor can trust; this exists purely at the provider boundary.

        Recomputed fresh on every call rather than redacted once and
        persisted: a tool result from step 1 still needs to be delimited
        when the full history is resent at step 5, and scan_pii/
        wrap_untrusted are pure and cheap enough that recomputing costs
        nothing worth caching.
        """

        sanitized: list[Message] = []
        for i, message in enumerate(messages):
            if i == 0 and message.role == "user" and message.content is not None:
                _matches, redacted = scan_pii(message.content)
                sanitized.append(replace(message, content=redacted))
            elif message.role == "tool" and message.content is not None:
                _matches, redacted = scan_pii(message.content)
                wrapped = wrap_untrusted(message.tool_name or "unknown", redacted)
                sanitized.append(replace(message, content=wrapped))
            else:
                sanitized.append(message)
        return sanitized

    async def _append_guardrail_hits(
        self,
        run_id: UUID,
        seq: int,
        now: datetime,
        layer: GuardrailLayer,
        step: int,
        matches: list[GuardMatch],
        profile: GuardrailProfile,
    ) -> tuple[int, str]:
        """Appends one GuardrailTriggered per match, in the order given.
        Returns (next unused seq, worst action) — one BLOCK is enough to
        stop regardless of what any other match alone decided, so the
        caller only needs the single worst verdict, not the whole list.
        """

        severity = {"ALLOW": 0, "REDACT": 1, "ESCALATE": 2, "BLOCK": 3}
        worst = "ALLOW"
        for match in matches:
            action = decide(match, profile)
            if action in ("BLOCK", "ESCALATE"):
                logger.warning(
                    "guardrail %s on run %s: %s rule=%s detail=%s",
                    action,
                    run_id,
                    layer,
                    match.rule,
                    match.detail,
                )
            else:
                logger.debug("guardrail %s on run %s: %s", action, run_id, match.rule)
            await self._append(
                run_id,
                seq,
                GuardrailTriggered(
                    seq=seq,
                    created_at=now,
                    layer=layer,
                    rule=match.rule,
                    action=action,
                    step=step,
                    detail=match.detail,
                    latency_ms=0,
                ),
            )
            seq += 1
            if severity[action] > severity[worst]:
                worst = action
        return seq, worst

    async def _append(self, run_id: UUID, seq: int, event: Event) -> None:
        try:
            await self._store.append(run_id, seq, event)
        except ConcurrencyConflict:
            # Another worker on the same run_id already claimed this seq.
            # Whatever they appended is now the truth; every caller of
            # _append falls straight through to the top of run()'s while
            # loop regardless of what happens here, which re-reads events
            # fresh and re-decides from there. Nothing else to do.
            return
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
                logger.info(
                    "run %s returning with status=%s at step %d (%d events)",
                    run_id,
                    state.status,
                    state.step,
                    len(events),
                )
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
                if recovered:
                    logger.info(
                        "run %s recovering in-flight %s op from seq %d (%d prior attempts)",
                        run_id,
                        state.in_flight.kind,
                        state.in_flight.seq,
                        state.in_flight.attempts,
                    )
                await self._reconcile(run_id, next_seq, now, state, recovered)
                continue

            decision = decide_next_action(state)
            match decision:
                case CallLLM():
                    seq = next_seq
                    if state.step == 0:
                        # L1 — runs exactly once per run, on the very
                        # first LLM call. state.step stays 0 until the
                        # first LLMCallRequested is appended, so this
                        # gate can't fire twice.
                        profile = get_profile(state.guardrail_profile)
                        goal = state.messages[0].content or ""
                        scan_result = await scan_input(goal)
                        seq, worst = await self._append_guardrail_hits(
                            run_id, seq, now, "L1_input", state.step, scan_result.matches, profile
                        )
                        if worst in ("BLOCK", "ESCALATE"):
                            # No tool call exists yet at this point to
                            # attach a human-approval request to (that
                            # event shape assumes a tool + arguments) —
                            # until that's designed, an L1 escalation
                            # fails closed instead of parking.
                            await self._append(
                                run_id,
                                seq,
                                RunFailed(
                                    seq=seq,
                                    created_at=now,
                                    reason="guardrail_block",
                                    detail="L1 input scan blocked this run",
                                ),
                            )
                            continue
                    # Tracked for the same reason tool requests are: an
                    # op already dangling before this invocation started
                    # is a genuine crash recovery, one this invocation
                    # itself requested is not. Only ToolCallCompleted
                    # records the flag, but keeping the bookkeeping
                    # uniform stops "is this a recovery?" from silently
                    # answering yes for every LLM call ever made.
                    self._requested_this_run.add(seq)
                    await self._append(
                        run_id,
                        seq,
                        LLMCallRequested(
                            seq=seq,
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

        # L3 (schema/allowlist/policy/PII-in-output) + L4 loop detection.
        # tool_obj is already known non-None here, so validate_output's
        # own allowlist check can never fire through this path — the
        # unknown-tool case above already handled that non-adversarially.
        seq = next_seq
        profile = get_profile(state.guardrail_profile)
        output_result = await validate_output(tool_call, self._tools, policy_caps=profile.policy_caps)
        matches = list(output_result.matches)
        loop_match = detect_loop(state, tool_call, threshold=profile.loop_threshold)
        if loop_match is not None:
            matches.append(loop_match)
        seq, worst = await self._append_guardrail_hits(
            run_id, seq, now, "L3_output", state.step, matches, profile
        )
        if worst == "BLOCK":
            await self._append(
                run_id,
                seq,
                RunFailed(
                    seq=seq,
                    created_at=now,
                    reason="guardrail_block",
                    detail=f"blocked before executing {tool_call.name}",
                ),
            )
            return

        # L4 escalation — N guardrail hits this run so far → force human
        # review regardless of what the tool's own requires_approval
        # predicate says. Reuses the exact same ApprovalRequested/grant/
        # deny machinery as an ordinary approval, since a real tool call
        # is already in context here.
        escalation_match = detect_escalation(state, threshold=profile.escalation_threshold)
        forced_by_escalation = False
        if escalation_match is not None:
            seq, escalation_action = await self._append_guardrail_hits(
                run_id, seq, now, "L4_run_level", state.step, [escalation_match], profile
            )
            forced_by_escalation = escalation_action == "ESCALATE"

        already_approved = state.approved_step == state.step
        if not already_approved and (tool_obj.requires_approval(tool_call.arguments) or forced_by_escalation):
            await self._append(
                run_id,
                seq,
                ApprovalRequested(
                    seq=seq,
                    created_at=now,
                    step=state.step,
                    tool=tool_call.name,
                    arguments=tool_call.arguments,
                    reason=f"{tool_call.name} requires approval for these arguments",
                ),
            )
            return

        key = idempotency_key(run_id, seq, tool_call.name, tool_call.arguments)
        self._requested_this_run.add(seq)
        await self._append(
            run_id,
            seq,
            ToolCallRequested(
                seq=seq,
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

        await self._backoff(op.attempts)

        if op.kind == "llm":
            try:
                response = await self._llm.call(
                    self._sanitize_for_llm(state.messages),
                    _tool_schemas(self._tools),
                    state.system_prompt,
                )
            except Exception as exc:
                attempt = op.attempts + 1
                logger.warning(
                    "LLM call failed for run %s (attempt %d/%d): %s",
                    run_id,
                    attempt,
                    self._max_llm_attempts,
                    exc,
                )
                await self._append(
                    run_id,
                    next_seq,
                    LLMCallFailed(
                        seq=next_seq,
                        created_at=now,
                        step=op.step,
                        error=str(exc),
                        attempt=attempt,
                    ),
                )
                if attempt >= self._max_llm_attempts:
                    # Nothing to hand back to the model here — the model
                    # is what's failing. Unlike a tool error, there's no
                    # other actor left to react, so the run ends.
                    logger.error(
                        "LLM call budget exhausted for run %s after %d attempts", run_id, attempt
                    )
                    await self._append(
                        run_id,
                        next_seq + 1,
                        RunFailed(
                            seq=next_seq + 1,
                            created_at=now,
                            reason="unrecoverable_error",
                            detail=f"LLM call failed {attempt} times: {exc}",
                        ),
                    )
                return
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
            try:
                result = await tool_obj.execute(**kwargs)
            except Exception as exc:
                attempt = op.attempts + 1
                final_attempt = attempt >= self._max_tool_attempts
                # A tool that raised is genuinely ambiguous: a payments
                # call that timed out may or may not have gone through.
                # Retrying reuses op.idempotency_key (the loop re-enters
                # here with the same in-flight op), so the backend's own
                # dedup makes a repeat safe. Surfacing to the model
                # instead would make it issue a fresh call at a new seq,
                # hence a new key, which nothing would deduplicate — so
                # the retry is the safer path, not just the kinder one.
                logger.warning(
                    "tool %s failed for run %s (attempt %d/%d, final=%s): %s",
                    op.tool,
                    run_id,
                    attempt,
                    self._max_tool_attempts,
                    final_attempt,
                    exc,
                )
                await self._append(
                    run_id,
                    next_seq,
                    ToolCallFailed(
                        seq=next_seq,
                        created_at=now,
                        step=op.step,
                        tool=op.tool,
                        arguments=op.arguments,
                        idempotency_key=op.idempotency_key or "",
                        error=str(exc),
                        attempt=attempt,
                        final_attempt=final_attempt,
                    ),
                )
                return
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

            # L2 — every tool result is untrusted input. Detection here
            # drives the audit log and a BLOCK backstop; the actual
            # protection (PII redaction + untrusted-data delimiting) is
            # applied unconditionally at the LLM boundary by
            # _sanitize_for_llm, regardless of whether anything matches
            # here — so a result nobody flagged is still never sent raw.
            # json.dumps, not a bare " ".join of the values: joining raw
            # values with a single space can accidentally weld two
            # unrelated fields' digits into one run a PII pattern then
            # matches for real (e.g. a random refund_id ending in digits
            # right next to amount_inr) — JSON's own quotes/colons/commas
            # reliably break that adjacency, the same serialization
            # state.py already uses to build this exact result into a
            # message.
            result_text = json.dumps(result) if isinstance(result, dict) else str(result)
            profile = get_profile(state.guardrail_profile)
            l2_result = await scan_tool_result(op.tool, result_text)
            seq, worst = await self._append_guardrail_hits(
                run_id, next_seq, now, "L2_tool_result", op.step, l2_result.matches, profile
            )
            if worst == "BLOCK":
                # The side effect already ran — BLOCK here stops the run
                # from acting on what might be poisoned data, it can't
                # undo the tool call itself. This leaves in_flight
                # dangling with no ToolCallCompleted, the same known gap
                # flagged in Iteration 12 (status is checked before
                # in_flight at the top of run()'s loop, so a terminal
                # status always wins regardless).
                await self._append(
                    run_id,
                    seq,
                    RunFailed(
                        seq=seq,
                        created_at=now,
                        reason="guardrail_block",
                        detail=f"blocked after {op.tool} returned a flagged result",
                    ),
                )
                return

            await self._append(
                run_id,
                seq,
                ToolCallCompleted(
                    seq=seq,
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
