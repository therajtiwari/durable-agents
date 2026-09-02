"""find_awaiting_approval against real Postgres.

The heuristic itself is covered by tests/unit/test_find_awaiting_approval.py
against the in-memory store. What can only be checked here is that the
SQL actually implements the same rule — a DISTINCT ON query has plenty
of room to disagree with the Python version while still returning
*something*.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from durable_agents.events import (
    ApprovalDenied,
    ApprovalGranted,
    ApprovalRequested,
    Event,
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
async def test_run_parked_on_approval_is_returned(event_store: PostgresEventStore) -> None:
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
                arguments={"order_id": "A-1", "amount_inr": 500},
                reason="issue_refund requires approval for these arguments",
            ),
        ],
    )

    found = await event_store.find_awaiting_approval()

    assert len(found) == 1
    found_run_id, event = found[0]
    assert found_run_id == run_id
    assert event.tool == "issue_refund"
    assert event.arguments == {"order_id": "A-1", "amount_inr": 500}


@pytest.mark.asyncio
async def test_run_not_yet_parked_is_excluded(event_store: PostgresEventStore) -> None:
    await _seed(event_store, [_run_started(_now())])
    assert await event_store.find_awaiting_approval() == []


@pytest.mark.asyncio
async def test_granted_or_denied_runs_are_no_longer_awaiting(
    event_store: PostgresEventStore,
) -> None:
    old = _now() - timedelta(seconds=3600)
    await _seed(
        event_store,
        [
            _run_started(old),
            ApprovalRequested(
                seq=1, created_at=old, step=1, tool="issue_refund", arguments={}, reason="x"
            ),
            ApprovalGranted(seq=2, created_at=old, approver="dana"),
        ],
    )
    await _seed(
        event_store,
        [
            _run_started(old),
            ApprovalRequested(
                seq=1, created_at=old, step=1, tool="issue_refund", arguments={}, reason="x"
            ),
            ApprovalDenied(seq=2, created_at=old, approver="dana", reason="too large"),
        ],
    )

    assert await event_store.find_awaiting_approval() == []


@pytest.mark.asyncio
async def test_ordering_is_oldest_first_and_limit_applies(
    event_store: PostgresEventStore,
) -> None:
    def _parked(created_at: datetime) -> list[Event]:
        return [
            _run_started(created_at),
            ApprovalRequested(
                seq=1, created_at=created_at, step=1, tool="issue_refund", arguments={}, reason="x"
            ),
        ]

    oldest = await _seed(event_store, _parked(_now() - timedelta(seconds=300)))
    middle = await _seed(event_store, _parked(_now() - timedelta(seconds=200)))
    await _seed(event_store, _parked(_now() - timedelta(seconds=100)))

    found = await event_store.find_awaiting_approval(limit=2)
    assert [run_id for run_id, _ in found] == [oldest, middle]
