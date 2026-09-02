from datetime import datetime, timezone
from uuid import UUID

from durable_agents.events import ApprovalRequested, Event
from durable_agents.storage.protocol import (
    NO_WORKER_HOLDING_EVENT_TYPES as _NO_WORKER_HOLDING,
)
from durable_agents.storage.protocol import (
    TERMINAL_OR_PARKED_EVENT_TYPES as _TERMINAL_OR_PARKED,
)
from durable_agents.storage.protocol import ConcurrencyConflict, EventStore


class InMemoryEventStore(EventStore):
    """An EventStore that keeps everything in a dict.

    Ships in the package rather than living in the test suite so that
    trying this library needs nothing but `pip install` — no Postgres,
    no Docker, no migration step. Use it for local development, unit
    tests of your own agent, and examples.

    It is not durable: the whole point of this project is surviving a
    process restart, and this store dies with the process. Anything that
    matters belongs in PostgresEventStore.

    Concurrency semantics deliberately mirror the Postgres
    implementation — an append at an already-taken seq raises
    ConcurrencyConflict — so code written against this store behaves the
    same way when pointed at a real database.
    """

    def __init__(self) -> None:
        self._events: dict[UUID, list[Event]] = {}

    async def append(self, run_id: UUID, expected_seq: int, event: Event) -> None:
        events = self._events.setdefault(run_id, [])
        if expected_seq != len(events):
            raise ConcurrencyConflict(
                f"seq {expected_seq} already taken for run {run_id} "
                f"(log is at seq {len(events)})"
            )
        events.append(event)

    async def read(self, run_id: UUID) -> list[Event]:
        return list(self._events.get(run_id, []))

    async def read_since(self, run_id: UUID, seq: int) -> list[Event]:
        return [e for e in self._events.get(run_id, []) if e.seq > seq]

    async def find_resumable_runs(
        self, *, stale_after_seconds: float, limit: int = 10
    ) -> list[UUID]:
        now = datetime.now(timezone.utc)
        candidates: list[tuple[datetime, UUID]] = []

        for run_id, events in self._events.items():
            if not events:
                continue
            latest = events[-1]
            if latest.type in _TERMINAL_OR_PARKED:
                continue
            if latest.type not in _NO_WORKER_HOLDING:
                age = (now - latest.created_at).total_seconds()
                if age < stale_after_seconds:
                    continue
            candidates.append((latest.created_at, run_id))

        candidates.sort(key=lambda pair: pair[0])
        return [run_id for _, run_id in candidates[:limit]]

    async def find_awaiting_approval(
        self, *, limit: int = 100
    ) -> list[tuple[UUID, ApprovalRequested]]:
        candidates: list[tuple[datetime, UUID, ApprovalRequested]] = []

        for run_id, events in self._events.items():
            if not events:
                continue
            latest = events[-1]
            if isinstance(latest, ApprovalRequested):
                candidates.append((latest.created_at, run_id, latest))

        candidates.sort(key=lambda triple: triple[0])
        return [(run_id, event) for _, run_id, event in candidates[:limit]]

    def run_ids(self) -> list[UUID]:
        """Every run this store has seen. Not part of the EventStore
        interface — a convenience for tests and examples that need to
        enumerate runs without a database to query.
        """

        return list(self._events)
