import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
from testcontainers.community.postgres import PostgresContainer

from durable_agents.events import RunStarted
from durable_agents.llm.protocol import LLMResponse
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.storage.postgres import PostgresEventStore
from durable_agents.tools.registry import Tool


def _run_started() -> RunStarted:
    return RunStarted(
        seq=0,
        created_at=datetime.now(timezone.utc),
        goal="Say hello.",
        model="scripted",
        system_prompt_hash="sha256:test",
        max_steps=5,
        max_cost_usd=Decimal("1.00"),
        requested_by="support-queue",
        guardrail_profile="financial_v1",
    )


def _final_answer_response() -> LLMResponse:
    return LLMResponse(
        content="Hello!",
        tool_calls=[],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal("0.0001"),
        latency_ms=10,
        provider_request_id="req",
    )


@pytest.mark.asyncio
async def test_two_workers_racing_on_one_run_converge_without_crashing(
    postgres_container: PostgresContainer,
) -> None:
    """Two workers, one run — spec's own Week 4 concurrency test.

    Deliberately two separate connection pools, not one shared pool: the
    race has to be genuine contention on the same Postgres rows, not
    something simulated in-process. An in-memory fake store never
    reproduces this, since its methods have no real I/O to suspend on —
    two asyncio tasks calling it never actually interleave.
    """

    run_id = uuid4()
    dsn = postgres_container.get_connection_url(driver=None)

    pool_a = await asyncpg.create_pool(dsn)
    pool_b = await asyncpg.create_pool(dsn)
    try:
        await pool_a.execute("DELETE FROM events WHERE run_id = $1", run_id)
        store_a = PostgresEventStore(pool_a)
        store_b = PostgresEventStore(pool_b)
        await store_a.append(run_id, 0, _run_started())

        tools: dict[str, Tool] = {}
        worker_a = Orchestrator(store=store_a, llm=ScriptedLLM([_final_answer_response()]), tools=tools)
        worker_b = Orchestrator(store=store_b, llm=ScriptedLLM([_final_answer_response()]), tools=tools)

        state_a, state_b = await asyncio.gather(worker_a.run(run_id), worker_b.run(run_id))

        assert state_a.status == "completed"
        assert state_b.status == "completed"
        assert state_a.final_answer == "Hello!"
        assert state_b.final_answer == "Hello!"

        rows = await pool_a.fetch(
            "SELECT type, COUNT(*) AS n FROM events WHERE run_id = $1 GROUP BY type", run_id
        )
        counts = {row["type"]: row["n"] for row in rows}
        # The whole point: despite two workers racing on every single
        # step, exactly one of each event type was ever durably recorded
        # — the (run_id, seq) primary key made every collision impossible
        # to silently duplicate, and _append()'s ConcurrencyConflict
        # handling meant the loser backed off instead of crashing.
        assert counts["RunStarted"] == 1
        assert counts["LLMCallRequested"] == 1
        assert counts["LLMCallCompleted"] == 1
        assert counts["RunCompleted"] == 1
    finally:
        await pool_a.close()
        await pool_b.close()
