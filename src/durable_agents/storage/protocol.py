from abc import ABC, abstractmethod
from uuid import UUID

from durable_agents.events import ApprovalRequested, Event


class ConcurrencyConflict(Exception):
    """Raised by EventStore.append when expected_seq is already taken."""


# Shared by every EventStore implementation so they classify runs
# identically — a worker must not get different answers depending on
# which store it happens to be pointed at.

TERMINAL_OR_PARKED_EVENT_TYPES = frozenset(
    {"RunCompleted", "RunFailed", "ApprovalRequested"}
)
"""Newest event says this run wants nothing from a worker: it's over, or
it's waiting on a human."""

NO_WORKER_HOLDING_EVENT_TYPES = frozenset(
    {"RunStarted", "ApprovalGranted", "ApprovalDenied"}
)
"""Newest event proves no worker can be mid-operation — nobody has begun
the run, or a human just acted on it. Safe to pick up immediately
without waiting out a staleness threshold."""


class EventStore(ABC):
    @abstractmethod
    async def append(self, run_id: UUID, expected_seq: int, event: Event) -> None:
        """Append event at expected_seq. Raises ConcurrencyConflict if taken."""
        raise NotImplementedError

    @abstractmethod
    async def read(self, run_id: UUID) -> list[Event]:
        """Read all events for run_id, ordered by seq."""
        raise NotImplementedError

    @abstractmethod
    async def read_since(self, run_id: UUID, seq: int) -> list[Event]:
        """Read events for run_id with seq strictly greater than seq, ordered by seq."""
        raise NotImplementedError

    @abstractmethod
    async def find_resumable_runs(
        self, *, stale_after_seconds: float, limit: int = 10
    ) -> list[UUID]:
        """Runs a worker should call resume() on, oldest activity first.

        Excludes runs that are finished (RunCompleted/RunFailed) or
        parked for a human (ApprovalRequested).

        The hard part is that a run being actively worked by a live
        process looks identical in the log to one whose worker died
        mid-operation — nothing records "a worker is holding this".
        Resolved by looking at what the newest event actually is:

        - RunStarted, ApprovalGranted, ApprovalDenied mean no worker can
          be mid-operation (nobody has begun it, or a human just acted),
          so these are returned immediately regardless of age. This is
          what lets a newly created run start without waiting.
        - Anything else means a worker may be in the middle of an LLM
          call, a tool call, or a retry backoff, so it is only returned
          once `stale_after_seconds` has passed with no new event.

        Set stale_after_seconds comfortably above the slowest single
        operation you expect. Getting it wrong is safe but wasteful:
        two workers race, which (run_id, seq) already makes correct —
        see tests/integration/test_concurrent_workers.py — at the cost
        of duplicated model spend, never corruption.
        """
        raise NotImplementedError

    @abstractmethod
    async def find_awaiting_approval(
        self, *, limit: int = 100
    ) -> list[tuple[UUID, ApprovalRequested]]:
        """Runs currently parked on a human decision, oldest first.

        A run is awaiting approval exactly when its newest event is
        ApprovalRequested: the state machine never appends anything
        else while parked (the next event is always ApprovalGranted or
        ApprovalDenied). That event already carries tool/arguments/
        reason, so answering "what is this run asking permission for"
        needs no rebuild_state() — just the newest row per run.

        This is what lets a human-approval dashboard list its queue
        without already knowing a run_id for every pending request.
        """
        raise NotImplementedError
