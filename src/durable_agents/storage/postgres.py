import json
from uuid import UUID

import asyncpg
from pydantic import TypeAdapter

from durable_agents.events import Event
from durable_agents.storage.protocol import ConcurrencyConflict, EventStore

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
