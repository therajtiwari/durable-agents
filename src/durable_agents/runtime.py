from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from durable_agents.events import ApprovalDenied, ApprovalGranted, RunStarted
from durable_agents.llm.protocol import LLMClient
from durable_agents.orchestrator import Orchestrator
from durable_agents.state import RunState, rebuild_state
from durable_agents.storage.protocol import EventStore
from durable_agents.tools.registry import Tool


@dataclass(frozen=True)
class Run:
    """A run's id together with its state at the moment you asked.

    RunState deliberately carries no run_id — it is derived purely from
    a list of events, and the caller already had the id to fetch them.
    But a caller who just *started* a run has no id yet, and needs one
    to approve or resume it later, so start() hands back both.
    """

    id: UUID
    state: RunState


class Runtime:
    """The front door: wire a store, a model, and some tools together
    once, then start and resume runs.

    Everything here is a thin convenience over Orchestrator and
    EventStore, which remain public and usable directly — this exists so
    the common case doesn't require hand-constructing a RunStarted event
    with the right seq, timestamp, and defaults.

    Tools are supplied once, here, rather than per-run. A resumed run
    must execute against the same tool set the original ran under, and
    nothing in the event log records which tools were registered — so
    accepting them per-run would let a resume silently run against a
    different set than the one that produced the events being replayed.
    """

    def __init__(
        self,
        store: EventStore,
        llm: LLMClient,
        tools: Sequence[Tool] = (),
        *,
        model: str = "unspecified",
        system_prompt: str = "",
        max_steps: int = 25,
        max_cost_usd: Decimal | float | str = Decimal("1.00"),
        guardrail_profile: str = "validation",
        max_llm_attempts: int = 3,
        max_tool_attempts: int = 3,
        retry_base_delay_seconds: float = 1.0,
    ) -> None:
        duplicates = [t.name for t in tools if [x.name for x in tools].count(t.name) > 1]
        if duplicates:
            raise ValueError(f"duplicate tool names: {sorted(set(duplicates))}")

        self._store = store
        self._tools = {t.name: t for t in tools}
        self._model = model
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        self._max_cost_usd = Decimal(str(max_cost_usd))
        self._guardrail_profile = guardrail_profile
        self._orchestrator = Orchestrator(
            store=store,
            llm=llm,
            tools=self._tools,
            max_llm_attempts=max_llm_attempts,
            max_tool_attempts=max_tool_attempts,
            retry_base_delay_seconds=retry_base_delay_seconds,
        )

    @property
    def store(self) -> EventStore:
        return self._store

    async def create(
        self,
        goal: str,
        *,
        requested_by: str = "unknown",
        system_prompt: str | None = None,
        max_steps: int | None = None,
        max_cost_usd: Decimal | float | str | None = None,
        guardrail_profile: str | None = None,
    ) -> UUID:
        """Record a new run without executing it, and return its id.

        Use this when something else does the executing — a worker pool
        picking runs off a queue, or a recovery sweeper. For the simple
        case where you want the run to actually happen now, use start().
        """

        run_id = uuid4()
        await self._store.append(
            run_id,
            0,
            RunStarted(
                seq=0,
                created_at=datetime.now(timezone.utc),
                goal=goal,
                model=self._model,
                system_prompt=self._system_prompt if system_prompt is None else system_prompt,
                max_steps=self._max_steps if max_steps is None else max_steps,
                max_cost_usd=(
                    self._max_cost_usd if max_cost_usd is None else Decimal(str(max_cost_usd))
                ),
                requested_by=requested_by,
                guardrail_profile=(
                    self._guardrail_profile if guardrail_profile is None else guardrail_profile
                ),
            ),
        )
        return run_id

    async def start(
        self,
        goal: str,
        *,
        requested_by: str = "unknown",
        system_prompt: str | None = None,
        max_steps: int | None = None,
        max_cost_usd: Decimal | float | str | None = None,
        guardrail_profile: str | None = None,
    ) -> Run:
        """Record a new run and execute it.

        Returns when the run reaches a terminal state (completed,
        failed) or parks awaiting human approval — the same three
        outcomes resume() returns on. Hands back the run's id alongside
        its state, since a parked run needs that id to be approved and
        resumed later, possibly by a different process entirely.
        """

        run_id = await self.create(
            goal,
            requested_by=requested_by,
            system_prompt=system_prompt,
            max_steps=max_steps,
            max_cost_usd=max_cost_usd,
            guardrail_profile=guardrail_profile,
        )
        return Run(id=run_id, state=await self.resume(run_id))

    async def resume(self, run_id: UUID) -> RunState:
        """Continue a run from wherever its event log left off.

        Safe to call on a run that is already finished (returns its
        state unchanged), one that was killed mid-flight (reconciles the
        dangling operation first), and one that was just created but
        never executed — starting really is just resuming from an almost
        empty log.
        """

        return await self._orchestrator.run(run_id)

    async def get_state(self, run_id: UUID) -> RunState:
        """Read a run's current state without executing anything."""

        return rebuild_state(await self._store.read(run_id))

    async def approve(self, run_id: UUID, approver: str) -> None:
        """Record a human's approval of a parked run.

        Records only — it does not resume. Deciding and executing are
        deliberately separate: the approval usually arrives over HTTP
        from a person, while the execution belongs to whatever process
        owns running agents. Call resume() when you want it to continue.
        """

        await self._append_approval(run_id, granted=True, approver=approver, reason="")

    async def deny(self, run_id: UUID, approver: str, reason: str) -> None:
        """Record a human's refusal of a parked run.

        The reason is fed back to the model as a tool-role message on
        the next resume(), so it can choose another action rather than
        stalling on the rejected one.
        """

        await self._append_approval(run_id, granted=False, approver=approver, reason=reason)

    async def _append_approval(
        self, run_id: UUID, *, granted: bool, approver: str, reason: str
    ) -> None:
        events = await self._store.read(run_id)
        if not events:
            raise ValueError(f"no such run: {run_id}")
        state = rebuild_state(events)
        if state.status != "awaiting_approval":
            raise ValueError(
                f"run {run_id} is not awaiting approval (status={state.status})"
            )

        seq = len(events)
        now = datetime.now(timezone.utc)
        event = (
            ApprovalGranted(seq=seq, created_at=now, approver=approver)
            if granted
            else ApprovalDenied(seq=seq, created_at=now, approver=approver, reason=reason)
        )
        await self._store.append(run_id, seq, event)
