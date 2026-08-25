from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from durable_agents.events import RunStarted
from durable_agents.storage.postgres import PostgresEventStore
from durable_agents.storage.protocol import ConcurrencyConflict


def _run_started(seq: int = 0) -> RunStarted:
    return RunStarted(
        seq=seq,
        created_at=datetime.now(timezone.utc),
        goal="Process refund for order A-8891",
        model="claude-sonnet-4-6",
        system_prompt_hash="sha256:test",
        max_steps=15,
        max_cost_usd=Decimal("2.00"),
        requested_by="support-queue",
        guardrail_profile="financial_v1",
    )


@pytest.mark.asyncio
async def test_append_and_read_round_trip(event_store: PostgresEventStore) -> None:
    run_id = uuid4()
    event = _run_started()

    await event_store.append(run_id, 0, event)
    events = await event_store.read(run_id)

    assert events == [event]


@pytest.mark.asyncio
async def test_concurrency_conflict_on_duplicate_seq(event_store: PostgresEventStore) -> None:
    run_id = uuid4()
    await event_store.append(run_id, 0, _run_started(seq=0))

    with pytest.raises(ConcurrencyConflict):
        await event_store.append(run_id, 0, _run_started(seq=0))


@pytest.mark.asyncio
async def test_read_since_excludes_events_at_or_before_seq(
    event_store: PostgresEventStore,
) -> None:
    run_id = uuid4()
    await event_store.append(run_id, 0, _run_started(seq=0))

    events = await event_store.read_since(run_id, 0)

    assert events == []


@pytest.mark.asyncio
async def test_different_runs_do_not_interfere(event_store: PostgresEventStore) -> None:
    run_a, run_b = uuid4(), uuid4()
    await event_store.append(run_a, 0, _run_started(seq=0))
    await event_store.append(run_b, 0, _run_started(seq=0))

    events_a = await event_store.read(run_a)
    events_b = await event_store.read(run_b)

    assert len(events_a) == 1
    assert len(events_b) == 1
