import json
from uuid import UUID

import asyncpg
from pydantic import TypeAdapter

from durable_agents.events import ApprovalRequested, Event
from durable_agents.storage.protocol import (
    NO_WORKER_HOLDING_EVENT_TYPES,
    TERMINAL_OR_PARKED_EVENT_TYPES,
    ConcurrencyConflict,
    EventStore,
)

_event_adapter: TypeAdapter[Event] = TypeAdapter(Event)


class PostgresEventStore(EventStore):
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "PostgresEventStore":
        pool = await asyncpg.create_pool(dsn)
        return cls(pool)

    async def append(self, run_id: UUID, expected_seq: int, event: Event) -> None:
        payload = event.model_dump(mode="json", exclude={"seq", "created_at", "type"})
        try:
            await self._pool.execute(
                """
                INSERT INTO events (run_id, seq, type, payload, created_at)
                VALUES ($1, $2, $3, $4::jsonb, $5)
                """,
                run_id,
                expected_seq,
                event.type,
                json.dumps(payload),
                event.created_at,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ConcurrencyConflict(
                f"seq {expected_seq} already exists for run {run_id}"
            ) from exc

    async def read(self, run_id: UUID) -> list[Event]:
        rows = await self._pool.fetch(
            "SELECT seq, type, payload, created_at FROM events "
            "WHERE run_id = $1 ORDER BY seq",
            run_id,
        )
        return [self._row_to_event(row) for row in rows]

    async def read_since(self, run_id: UUID, seq: int) -> list[Event]:
        rows = await self._pool.fetch(
            "SELECT seq, type, payload, created_at FROM events "
            "WHERE run_id = $1 AND seq > $2 ORDER BY seq",
            run_id,
            seq,
        )
        return [self._row_to_event(row) for row in rows]

    async def find_resumable_runs(
        self, *, stale_after_seconds: float, limit: int = 10
    ) -> list[UUID]:
        # DISTINCT ON (run_id) ... ORDER BY run_id, seq DESC gives the
        # newest event per run using the (run_id, seq) primary key index.
        #
        # Note this compares the app-set created_at against the database
        # clock (now()). Clock skew between an application host and the
        # database shifts the effective staleness threshold slightly;
        # harmless here because racing workers are already safe, but
        # worth knowing before tightening the threshold.
        rows = await self._pool.fetch(
            """
            WITH latest AS (
                SELECT DISTINCT ON (run_id) run_id, type, created_at
                FROM events
                ORDER BY run_id, seq DESC
            )
            SELECT run_id
            FROM latest
            WHERE type <> ALL($1::text[])
              AND (
                  type = ANY($2::text[])
                  OR created_at < now() - make_interval(secs => $3)
              )
            ORDER BY created_at
            LIMIT $4
            """,
            list(TERMINAL_OR_PARKED_EVENT_TYPES),
            list(NO_WORKER_HOLDING_EVENT_TYPES),
            stale_after_seconds,
            limit,
        )
        return [row["run_id"] for row in rows]

    async def find_awaiting_approval(
        self, *, limit: int = 100
    ) -> list[tuple[UUID, ApprovalRequested]]:
        # Same DISTINCT ON trick as find_resumable_runs: newest row per
        # run_id off the (run_id, seq) primary key index, then keep only
        # the ones whose newest row actually is ApprovalRequested.
        rows = await self._pool.fetch(
            """
            WITH latest AS (
                SELECT DISTINCT ON (run_id) run_id, seq, type, payload, created_at
                FROM events
                ORDER BY run_id, seq DESC
            )
            SELECT run_id, seq, type, payload, created_at
            FROM latest
            WHERE type = 'ApprovalRequested'
            ORDER BY created_at
            LIMIT $1
            """,
            limit,
        )
        results: list[tuple[UUID, ApprovalRequested]] = []
        for row in rows:
            event = self._row_to_event(row)
            assert isinstance(event, ApprovalRequested)
            results.append((row["run_id"], event))
        return results

    @staticmethod
    def _row_to_event(row: asyncpg.Record) -> Event:
        payload = json.loads(row["payload"])
        merged = {
            **payload,
            "seq": row["seq"],
            "type": row["type"],
            "created_at": row["created_at"],
        }
        return _event_adapter.validate_python(merged)
