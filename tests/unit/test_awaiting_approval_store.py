"""find_awaiting_approval against the in-memory store.

The Postgres SQL for the same query is checked separately in
tests/integration/test_find_awaiting_approval.py — a DISTINCT ON query
has plenty of room to disagree with the Python version while still
returning *something*.
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
from durable_agents.storage.memory import InMemoryEventStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_started(created_at: datetime | None = None) -> RunStarted:
    return RunStarted(
        seq=0,
        created_at=created_at or _now(),
        goal="Do the thing.",
        model="scripted",
        max_steps=15,
        max_cost_usd=Decimal("2.00"),
        requested_by="test",
        guardrail_profile="standard",
    )


async def _seed(store: InMemoryEventStore, events: list[Event]) -> UUID:
    run_id = uuid4()
    for i, event in enumerate(events):
        await store.append(run_id, i, event)
    return run_id


@pytest.mark.asyncio
async def test_run_parked_on_approval_is_returned() -> None:
    store = InMemoryEventStore()
    run_id = await _seed(
        store,
        [
            _run_started(),
            ApprovalRequested(
                seq=1,
                created_at=_now(),
                step=1,
                tool="issue_refund",
                arguments={"order_id": "A-1", "amount_inr": 500},
                reason="issue_refund requires approval for these arguments",
            ),
        ],
    )

    found = await store.find_awaiting_approval()

    assert len(found) == 1
    found_run_id, event = found[0]
    assert found_run_id == run_id
    assert event.tool == "issue_refund"
    assert event.arguments == {"order_id": "A-1", "amount_inr": 500}
    assert event.reason == "issue_refund requires approval for these arguments"


@pytest.mark.asyncio
async def test_run_not_yet_parked_is_excluded() -> None:
    store = InMemoryEventStore()
    await _seed(store, [_run_started()])

    assert await store.find_awaiting_approval() == []


@pytest.mark.asyncio
async def test_granted_or_denied_runs_are_no_longer_awaiting() -> None:
    store = InMemoryEventStore()
    await _seed(
        store,
        [
            _run_started(),
            ApprovalRequested(
                seq=1,
                created_at=_now(),
                step=1,
                tool="issue_refund",
                arguments={},
                reason="needs approval",
            ),
            ApprovalGranted(seq=2, created_at=_now(), approver="dana"),
        ],
    )
    await _seed(
        store,
        [
            _run_started(),
            ApprovalRequested(
                seq=1,
                created_at=_now(),
                step=1,
                tool="issue_refund",
                arguments={},
                reason="needs approval",
            ),
            ApprovalDenied(seq=2, created_at=_now(), approver="dana", reason="too large"),
        ],
    )

    assert await store.find_awaiting_approval() == []


@pytest.mark.asyncio
async def test_results_are_oldest_first_and_respect_the_limit() -> None:
    store = InMemoryEventStore()

    def _parked(created_at: datetime) -> list[Event]:
        return [
            _run_started(created_at),
            ApprovalRequested(
                seq=1,
                created_at=created_at,
                step=1,
                tool="issue_refund",
                arguments={},
                reason="needs approval",
            ),
        ]

    oldest = await _seed(store, _parked(_now() - timedelta(seconds=300)))
    middle = await _seed(store, _parked(_now() - timedelta(seconds=200)))
    await _seed(store, _parked(_now() - timedelta(seconds=100)))

    found = await store.find_awaiting_approval(limit=2)
    assert [run_id for run_id, _ in found] == [oldest, middle]
