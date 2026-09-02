from datetime import datetime, timezone
from decimal import Decimal

from durable_agents.events import (
    Event,
    LLMCallCompleted,
    LLMCallRequested,
    RunCompleted,
    RunStarted,
    ToolCallCompleted,
    ToolCallRequested,
)
from durable_agents.state import rebuild_state


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_events() -> list[Event]:
    return [
        RunStarted(
            seq=0,
            created_at=_now(),
            goal="Process refund for order A-8891",
            model="claude-sonnet-4-6",
            system_prompt_hash="sha256:test",
            max_steps=15,
            max_cost_usd=Decimal("2.00"),
            requested_by="support-queue",
            guardrail_profile="financial_v1",
        ),
        LLMCallRequested(seq=1, created_at=_now(), step=1, message_count=1, estimated_tokens=412),
        LLMCallCompleted(
            seq=2,
            created_at=_now(),
            step=1,
            content="Let me look up the order details first.",
            tool_calls=[],
            stop_reason="tool_use",
            input_tokens=412,
            output_tokens=67,
            cost_usd=Decimal("0.0022"),
            latency_ms=2779,
            provider_request_id="req_01HXYZ",
        ),
        ToolCallRequested(
            seq=3,
            created_at=_now(),
            step=1,
            tool="lookup_order",
            arguments={"order_id": "A-8891"},
            idempotency_key="abc123",
            requires_approval=False,
        ),
        ToolCallCompleted(
            seq=4,
            created_at=_now(),
            step=1,
            tool="lookup_order",
            idempotency_key="abc123",
            result={"amount_inr": 6400, "status": "delivered", "damage": True},
            duration_ms=431,
            recovered=False,
            provider_dedup_hit=False,
        ),
        RunCompleted(
            seq=5,
            created_at=_now(),
            final_answer="Refund RF-55012 processed.",
            total_steps=1,
            total_tokens=479,
            total_cost_usd=Decimal("0.0022"),
        ),
    ]


def test_rebuild_is_deterministic() -> None:
    events = _build_events()
    assert rebuild_state(events) == rebuild_state(events)


def test_every_prefix_is_valid() -> None:
    events = _build_events()
    for n in range(len(events) + 1):
        state = rebuild_state(events[:n])
        if state.status == "completed":
            assert state.in_flight is None


def test_empty_log_yields_not_started() -> None:
    state = rebuild_state([])
    assert state.status == "not_started"
    assert state.in_flight is None
    assert state.messages == []


def test_guardrail_profile_carries_through_from_run_started() -> None:
    state = rebuild_state(_build_events())
    assert state.guardrail_profile == "financial_v1"


def test_dangling_llm_request_becomes_in_flight() -> None:
    events = _build_events()[:2]  # RunStarted, LLMCallRequested — no Completed yet
    state = rebuild_state(events)
    assert state.in_flight is not None
    assert state.in_flight.kind == "llm"


def test_dangling_tool_request_becomes_in_flight() -> None:
    events = _build_events()[:4]  # ... up through ToolCallRequested — no Completed yet
    state = rebuild_state(events)
    assert state.in_flight is not None
    assert state.in_flight.kind == "tool"
    assert state.in_flight.tool == "lookup_order"


def test_tool_completion_clears_in_flight() -> None:
    events = _build_events()[:5]  # ... through ToolCallCompleted
    state = rebuild_state(events)
    assert state.in_flight is None


def test_run_completed_uses_authoritative_totals() -> None:
    state = rebuild_state(_build_events())
    assert state.status == "completed"
    assert state.total_tokens == 479
    assert state.final_answer == "Refund RF-55012 processed."
