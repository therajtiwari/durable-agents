"""The Worker, and the store query it depends on.

The interesting cases are all about one question: can a worker tell a
run that a live process is actively working from one whose process
died? Nothing in the log records "a worker is holding this", so these
tests pin down the heuristic that stands in for it.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from durable_agents import InMemoryEventStore, Runtime, ScriptedLLM, Worker
from durable_agents.events import (
    ApprovalGranted,
    ApprovalRequested,
    Event,
    LLMCallRequested,
    RunCompleted,
    RunFailed,
    RunStarted,
)
from durable_agents.llm.protocol import LLMResponse


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


def _response(**overrides: object) -> LLMResponse:
    defaults: dict[str, object] = {
        "content": "Done.",
        "tool_calls": [],
        "stop_reason": "end_turn",
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": Decimal("0.0001"),
        "latency_ms": 10,
        "provider_request_id": "req",
    }
    defaults.update(overrides)
    return LLMResponse(**defaults)  # type: ignore[arg-type]


async def _seed(store: InMemoryEventStore, events: list[Event]) -> UUID:
    run_id = uuid4()
    for i, event in enumerate(events):
        await store.append(run_id, i, event)
    return run_id


# --- find_resumable_runs: the classification heuristic ----------------


@pytest.mark.asyncio
async def test_brand_new_run_is_picked_up_immediately() -> None:
    """A run whose newest event is RunStarted has provably not been
    begun, so it must not wait out the staleness threshold — this is
    what makes a run created over the API start right away rather than
    a minute later.
    """

    store = InMemoryEventStore()
    run_id = await _seed(store, [_run_started()])

    found = await store.find_resumable_runs(stale_after_seconds=60.0)
    assert found == [run_id]


@pytest.mark.asyncio
async def test_run_just_approved_by_a_human_is_picked_up_immediately() -> None:
    # ApprovalGranted means a human acted; no worker can be holding it.
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
                arguments={},
                reason="needs approval",
            ),
            ApprovalGranted(seq=2, created_at=_now(), approver="dana"),
        ],
    )

    assert await store.find_resumable_runs(stale_after_seconds=60.0) == [run_id]


@pytest.mark.asyncio
async def test_run_in_progress_is_left_alone_until_it_goes_stale() -> None:
    """The case that matters most: a worker mid-LLM-call looks exactly
    like a dead one. A fresh LLMCallRequested must be assumed live.
    """

    store = InMemoryEventStore()
    run_id = await _seed(
        store,
        [
            _run_started(),
            LLMCallRequested(
                seq=1, created_at=_now(), step=1, message_count=1, estimated_tokens=10
            ),
        ],
    )

    # Recent: assume a live worker is on it.
    assert await store.find_resumable_runs(stale_after_seconds=60.0) == []
    # Same run, threshold it now exceeds: treat as abandoned.
    assert await store.find_resumable_runs(stale_after_seconds=0.0) == [run_id]


@pytest.mark.asyncio
async def test_abandoned_run_past_the_threshold_is_recovered() -> None:
    store = InMemoryEventStore()
    stale = _now() - timedelta(seconds=300)
    run_id = await _seed(
        store,
        [
            _run_started(stale),
            LLMCallRequested(
                seq=1, created_at=stale, step=1, message_count=1, estimated_tokens=10
            ),
        ],
    )

    assert await store.find_resumable_runs(stale_after_seconds=60.0) == [run_id]


@pytest.mark.asyncio
async def test_finished_and_parked_runs_are_never_returned() -> None:
    store = InMemoryEventStore()
    old = _now() - timedelta(seconds=3600)

    await _seed(
        store,
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
        store,
        [_run_started(old), RunFailed(seq=1, created_at=old, reason="max_steps_exceeded", detail=None)],
    )
    await _seed(
        store,
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

    # All three are old enough to be "stale", but none wants a worker:
    # two are over, one is waiting on a human.
    assert await store.find_resumable_runs(stale_after_seconds=1.0) == []


@pytest.mark.asyncio
async def test_results_are_oldest_first_and_respect_the_limit() -> None:
    store = InMemoryEventStore()
    oldest = await _seed(store, [_run_started(_now() - timedelta(seconds=300))])
    middle = await _seed(store, [_run_started(_now() - timedelta(seconds=200))])
    await _seed(store, [_run_started(_now() - timedelta(seconds=100))])

    # Oldest first, so the run waiting longest gets served first.
    assert await store.find_resumable_runs(stale_after_seconds=0.0, limit=2) == [oldest, middle]


# --- Worker ------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_advances_a_run_to_completion() -> None:
    store = InMemoryEventStore()
    run_id = await _seed(store, [_run_started()])

    runtime = Runtime(store=store, llm=ScriptedLLM([_response()]))
    worked = await Worker(runtime).poll_once()

    assert worked == [run_id]
    assert (await runtime.get_state(run_id)).status == "completed"


@pytest.mark.asyncio
async def test_worker_leaves_nothing_to_do_alone() -> None:
    store = InMemoryEventStore()
    runtime = Runtime(store=store, llm=ScriptedLLM([]))
    assert await Worker(runtime).poll_once() == []


@pytest.mark.asyncio
async def test_one_failing_run_does_not_stop_the_worker() -> None:
    """A poisoned run must not become an outage for every other run.

    Note this deliberately fails at the STORE level, not the LLM level:
    the orchestrator's own retry logic catches provider
    errors and turns them into RunFailed, so an LLM failure never
    actually escapes resume() — it isn't a test of the worker's error
    isolation at all. A store that refuses to read one run is a failure
    the orchestrator genuinely cannot swallow, which is what this
    try/except in the worker exists for.
    """

    class BrokenReadStore(InMemoryEventStore):
        def __init__(self) -> None:
            super().__init__()
            self.poisoned: UUID | None = None

        async def read(self, run_id: UUID) -> list[Event]:
            if run_id == self.poisoned:
                raise RuntimeError("storage is unreachable for this run")
            return await super().read(run_id)

    store = BrokenReadStore()
    broken = await _seed(store, [_run_started(_now() - timedelta(seconds=300))])
    healthy = await _seed(store, [_run_started(_now() - timedelta(seconds=200))])
    store.poisoned = broken

    runtime = Runtime(store=store, llm=ScriptedLLM([_response()]))
    worked = await Worker(runtime, stale_after_seconds=0.0).poll_once()

    # The broken run raised and was skipped; the healthy one — which
    # sorts second, i.e. AFTER the failure — still got worked.
    assert worked == [healthy]
    assert (await runtime.get_state(healthy)).status == "completed"


@pytest.mark.asyncio
async def test_run_forever_stops_promptly_when_asked() -> None:
    store = InMemoryEventStore()
    runtime = Runtime(store=store, llm=ScriptedLLM([]))
    worker = Worker(runtime, poll_interval_seconds=30.0)

    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_forever(stop=stop))
    await asyncio.sleep(0.05)
    stop.set()

    # Waits on the stop event rather than sleeping blindly, so shutdown
    # must not take the full 30s poll interval.
    await asyncio.wait_for(task, timeout=2.0)
