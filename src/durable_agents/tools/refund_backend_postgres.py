from typing import Any
from uuid import uuid4

import asyncpg


class PostgresRefundBackend:
    """Same shape as RefundBackend, ledger persisted in Postgres instead
    of process memory.

    InMemoryRefundBackend can't survive a real process restart — its
    ledger is a plain Python dict, gone the moment the process exits. That
    makes it unsuitable for the chaos suite, which spawns two genuinely
    separate OS processes (kill, then resume) that both need to see the
    same dedup state. This mirrors reality: a real payments API's own
    database persists independently of your application process — ours
    needs to as well, for the "same key twice, one refund" guarantee to
    mean anything across a crash.

    Dedup uses INSERT ... ON CONFLICT DO NOTHING rather than a
    check-then-insert — the same "let the primary key be the concurrency
    control" pattern already used by EventStore, so two attempts racing
    on the same idempotency_key can't both create a row.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._orders: dict[str, dict[str, Any]] = {
            "A-8891": {"amount_inr": 6400, "status": "delivered", "damaged": True},
        }

    @classmethod
    async def connect(cls, dsn: str) -> "PostgresRefundBackend":
        pool = await asyncpg.create_pool(dsn)
        return cls(pool)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        order = self._orders.get(order_id)
        if order is None:
            return {"error": f"no such order: {order_id}"}
        return {"order_id": order_id, **order}

    async def issue_refund(
        self, order_id: str, amount_inr: int, idempotency_key: str
    ) -> dict[str, Any]:
        await self._pool.execute(
            "INSERT INTO refund_attempts (order_id, idempotency_key) VALUES ($1, $2)",
            order_id,
            idempotency_key,
        )

        refund_id = f"RF-{uuid4().hex[:8]}"
        inserted = await self._pool.fetchrow(
            """
            INSERT INTO refund_ledger (idempotency_key, refund_id, order_id, amount_inr)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING refund_id, amount_inr
            """,
            idempotency_key,
            refund_id,
            order_id,
            amount_inr,
        )
        if inserted is not None:
            return {
                "refund_id": inserted["refund_id"],
                "amount_inr": inserted["amount_inr"],
                "status": "processed",
            }

        # Lost the race (or this is a genuine retry of an already-issued
        # refund) — someone else's row is the real one. Fetch it.
        existing = await self._pool.fetchrow(
            "SELECT refund_id, amount_inr FROM refund_ledger WHERE idempotency_key = $1",
            idempotency_key,
        )
        assert existing is not None
        return {
            "refund_id": existing["refund_id"],
            "amount_inr": existing["amount_inr"],
            "status": "processed",
            "dedup_hit": True,
        }

    async def attempts_count(self) -> int:
        row = await self._pool.fetchrow("SELECT COUNT(*) AS n FROM refund_attempts")
        assert row is not None
        return int(row["n"])

    async def refunds_count(self) -> int:
        row = await self._pool.fetchrow("SELECT COUNT(*) AS n FROM refund_ledger")
        assert row is not None
        return int(row["n"])
