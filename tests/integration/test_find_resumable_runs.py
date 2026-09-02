"""find_resumable_runs against real Postgres.

The heuristic itself is covered by tests/unit/test_worker.py against
the in-memory store. What can only be checked here is that the SQL
actually implements the same rules — a query using DISTINCT ON,
make_interval, and array containment has plenty of room to disagree
with the Python version while still returning *something*.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from durable_agents.events import (
    ApprovalGranted,
    ApprovalRequested,
    Event,
    LLMCallRequested,
    RunCompleted,
    RunStarted,
)
from durable_agents.storage.postgres import PostgresEventStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_started(created_at: datetime) -> RunStarted:
    return RunStarted(
        seq=0,
        created_at=created_at,
        goal="Do the thing.",
        model="scripted",
        max_steps=15,
        max_cost_usd=Decimal("2.00"),
        requested_by="test",
        guardrail_profile="standard",
    )


async def _seed(store: PostgresEventStore, events: list[Event]) -> UUID:
    run_id = uuid4()
    for i, event in enumerate(events):
        await store.append(run_id, i, event)
    return run_id


@pytest.mark.asyncio
async def test_brand_new_run_returned_immediately(event_store: PostgresEventStore) -> None:
    run_id = await _seed(event_store, [_run_started(_now())])
    found = await event_store.find_resumable_runs(stale_after_seconds=600.0)
    assert found == [run_id]


@pytest.mark.asyncio
async def test_in_progress_run_waits_for_the_threshold(
    event_store: PostgresEventStore,
) -> None:
    now = _now()
    run_id = await _seed(
        event_store,
        [
            _run_started(now),
            LLMCallRequested(seq=1, created_at=now, step=1, message_count=1, estimated_tokens=10),
        ],
    )

    # Fresh activity: assume a live worker owns it.
    assert await event_store.find_resumable_runs(stale_after_seconds=600.0) == []
    # Same row, threshold it now exceeds.
    assert await event_store.find_resumable_runs(stale_after_seconds=0.0) == [run_id]


@pytest.mark.asyncio
async def test_stale_in_progress_run_is_recovered(event_store: PostgresEventStore) -> None:
    stale = _now() - timedelta(seconds=300)
    run_id = await _seed(
        event_store,
        [
            _run_started(stale),
            LLMCallRequested(
                seq=1, created_at=stale, step=1, message_count=1, estimated_tokens=10
            ),
        ],
    )
    assert await event_store.find_resumable_runs(stale_after_seconds=60.0) == [run_id]


@pytest.mark.asyncio
async def test_completed_and_parked_runs_excluded(event_store: PostgresEventStore) -> None:
    old = _now() - timedelta(seconds=3600)
    await _seed(
        event_store,
        [
            _run_started(old),
            RunCompleted(
                seq=1,
                created_at=old,
                final_answer="done",
                total_steps=1,
                total_tokens=10,
                total_cost_usd=Decimal("0.01"),
            ),
        ],
    )
    await _seed(
        event_store,
        [
            _run_started(old),
            ApprovalRequested(
                seq=1,
                created_at=old,
                step=1,
                tool="issue_refund",
                arguments={},
                reason="needs approval",
            ),
        ],
    )

    assert await event_store.find_resumable_runs(stale_after_seconds=1.0) == []


@pytest.mark.asyncio
async def test_approval_granted_returned_immediately(
    event_store: PostgresEventStore,
) -> None:
    now = _now()
    run_id = await _seed(
        event_store,
        [
            _run_started(now),
            ApprovalRequested(
                seq=1,
                created_at=now,
                step=1,
                tool="issue_refund",
                arguments={},
                reason="needs approval",
            ),
            ApprovalGranted(seq=2, created_at=now, approver="dana"),
        ],
    )

    # A human just acted, so no worker can be mid-operation — this must
    # not wait out the staleness threshold.
    assert await event_store.find_resumable_runs(stale_after_seconds=600.0) == [run_id]


@pytest.mark.asyncio
async def test_ordering_is_oldest_first_and_limit_applies(
    event_store: PostgresEventStore,
) -> None:
    oldest = await _seed(event_store, [_run_started(_now() - timedelta(seconds=300))])
    middle = await _seed(event_store, [_run_started(_now() - timedelta(seconds=200))])
    await _seed(event_store, [_run_started(_now() - timedelta(seconds=100))])

    found = await event_store.find_resumable_runs(stale_after_seconds=0.0, limit=2)
    assert found == [oldest, middle]
