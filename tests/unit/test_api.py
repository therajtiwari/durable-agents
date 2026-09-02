import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from durable_agents.api.app import create_app
from durable_agents.events import (
    ApprovalRequested,
    Event,
    LLMCallCompleted,
    LLMCallRequested,
    RunStarted,
)
from durable_agents.storage.memory import InMemoryEventStore



def _run_started() -> RunStarted:
    return RunStarted(
        seq=0,
        created_at=datetime.now(timezone.utc),
        goal="Process refund for order A-8891.",
        model="scripted",
        system_prompt_hash="sha256:test",
        max_steps=15,
        max_cost_usd=Decimal("2.00"),
        requested_by="support-queue",
        guardrail_profile="financial_v1",
    )


def _park_on_approval(store: InMemoryEventStore, run_id: UUID) -> None:
    """Hand-builds a run parked on ApprovalRequested, bypassing the
    orchestrator entirely — these tests only exercise the HTTP layer's
    reaction to state, not how a run gets into that state (already
    covered by test_orchestrator.py).
    """

    now = datetime.now(timezone.utc)
    events: list[Event] = [
        _run_started(),
        LLMCallRequested(seq=1, created_at=now, step=1, message_count=1, estimated_tokens=10),
        LLMCallCompleted(
            seq=2,
            created_at=now,
            step=1,
            content="Issuing the refund.",
            tool_calls=[],
            stop_reason="tool_use",
            input_tokens=100,
            output_tokens=20,
            cost_usd=Decimal("0.001"),
            latency_ms=500,
            provider_request_id="req",
        ),
        ApprovalRequested(
            seq=3,
            created_at=now,
            step=1,
            tool="issue_refund",
            arguments={"order_id": "A-8891", "amount_inr": 6400, "reason": "damaged"},
            reason="issue_refund requires approval for these arguments",
        ),
    ]
    for i, event in enumerate(events):
        asyncio.run(store.append(run_id, i, event))


def test_start_run_records_without_executing() -> None:
    store = InMemoryEventStore()
    client = TestClient(create_app(store))

    response = client.post(
        "/runs", json={"goal": "Refund order A-8891.", "requested_by": "hr-system"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "running"
    assert body["step"] == 0
    run_id = UUID(body["run_id"])

    events = asyncio.run(store.read(run_id))
    assert len(events) == 1
    assert isinstance(events[0], RunStarted)
    assert events[0].goal == "Refund order A-8891."

    # And GET on that same id reflects the exact same state — no
    # separate source of truth between the POST response and a re-fetch.
    status = client.get(f"/runs/{run_id}").json()
    assert status == body


def test_start_run_applies_configured_defaults() -> None:
    store = InMemoryEventStore()
    client = TestClient(
        create_app(
            store,
            default_model="llama-70b",
            default_max_steps=10,
            default_max_cost_usd=Decimal("0.25"),
            default_guardrail_profile="strict",
        )
    )

    response = client.post("/runs", json={"goal": "g"})
    run_id = UUID(response.json()["run_id"])

    events = asyncio.run(store.read(run_id))
    started = events[0]
    assert isinstance(started, RunStarted)
    assert started.model == "llama-70b"
    assert started.max_steps == 10
    assert started.max_cost_usd == Decimal("0.25")
    assert started.guardrail_profile == "strict"


def test_start_run_per_request_overrides_beat_defaults() -> None:
    store = InMemoryEventStore()
    client = TestClient(create_app(store, default_max_steps=10, default_guardrail_profile="strict"))

    response = client.post(
        "/runs", json={"goal": "g", "max_steps": 99, "guardrail_profile": "lenient"}
    )
    run_id = UUID(response.json()["run_id"])

    events = asyncio.run(store.read(run_id))
    started = events[0]
    assert isinstance(started, RunStarted)
    assert started.max_steps == 99
    assert started.guardrail_profile == "lenient"


def test_status_unknown_run_returns_404() -> None:
    client = TestClient(create_app(InMemoryEventStore()))
    response = client.get(f"/runs/{uuid4()}")
    assert response.status_code == 404


def test_status_reports_awaiting_approval() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    _park_on_approval(store, run_id)

    client = TestClient(create_app(store))
    response = client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert body["pending_approval"]["tool"] == "issue_refund"
    assert body["pending_approval"]["arguments"]["amount_inr"] == 6400


def test_approve_clears_pending_approval() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    _park_on_approval(store, run_id)

    client = TestClient(create_app(store))
    response = client.post(f"/runs/{run_id}/approve", json={"approver": "priya.n"})
    assert response.status_code == 204

    status = client.get(f"/runs/{run_id}").json()
    assert status["status"] == "running"
    assert status["pending_approval"] is None


def test_deny_clears_pending_approval() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    _park_on_approval(store, run_id)

    client = TestClient(create_app(store))
    response = client.post(
        f"/runs/{run_id}/deny", json={"approver": "priya.n", "reason": "refund too large"}
    )
    assert response.status_code == 204

    status = client.get(f"/runs/{run_id}").json()
    assert status["status"] == "running"
    assert status["pending_approval"] is None


def test_approve_when_not_awaiting_approval_returns_409() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    asyncio.run(store.append(run_id, 0, _run_started()))

    client = TestClient(create_app(store))
    response = client.post(f"/runs/{run_id}/approve", json={"approver": "priya.n"})

    assert response.status_code == 409


def test_list_pending_approvals_returns_parked_runs() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    _park_on_approval(store, run_id)
    # A run that hasn't asked for anything must not show up in the queue.
    asyncio.run(store.append(uuid4(), 0, _run_started()))

    client = TestClient(create_app(store))
    response = client.get("/approvals")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["run_id"] == str(run_id)
    assert body[0]["tool"] == "issue_refund"
    assert body[0]["arguments"]["amount_inr"] == 6400


def test_list_pending_approvals_empty_queue() -> None:
    client = TestClient(create_app(InMemoryEventStore()))
    response = client.get("/approvals")
    assert response.status_code == 200
    assert response.json() == []
