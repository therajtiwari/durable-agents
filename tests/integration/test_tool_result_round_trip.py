"""Tool results have to survive a real JSONB column, not just Pydantic.

The in-memory store keeps Python objects, so it validates nothing about
serialisation — which is exactly how the NaN case escaped the first pass
of adversarial QA: the probe reported "completed" against the in-memory
store, while Postgres rejects NaN outright with "invalid input syntax for
type json". That rejection would land at the moment the outcome of an
already-executed side effect was being recorded, which is the one moment
this system must not fail.
"""

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from durable_agents.events import RunStarted, ToolCallCompleted
from durable_agents.storage.postgres import PostgresEventStore

pytestmark = pytest.mark.asyncio


def _started() -> RunStarted:
    return RunStarted(
        seq=0,
        created_at=datetime.now(timezone.utc),
        goal="g",
        model="scripted",
        max_steps=5,
        max_cost_usd=Decimal("1"),
        requested_by="test",
        guardrail_profile="validation",
    )


@pytest.mark.parametrize(
    "returns",
    [
        {"ok": True, "count": 3},
        "a plain string",
        None,
        [1, 2, 3],
        {"amount": Decimal("10.50")},
        {"at": datetime(2026, 9, 3, tzinfo=timezone.utc)},
        {"score": float("nan")},
        {"score": float("inf")},
        {"blob": b"\x00\x01"},
        {1: "an integer key"},
        {"nested": {"deep": [Decimal("1"), None, {"more": float("-inf")}]}},
    ],
)
async def test_every_normalised_result_survives_postgres(
    event_store: PostgresEventStore, returns: Any
) -> None:
    from durable_agents.orchestrator import normalize_tool_result

    run_id = uuid4()
    await event_store.append(run_id, 0, _started())
    await event_store.append(
        run_id,
        1,
        ToolCallCompleted(
            seq=1,
            created_at=datetime.now(timezone.utc),
            step=1,
            tool="act",
            idempotency_key="k",
            result=normalize_tool_result(returns),
            duration_ms=1,
            recovered=False,
            provider_dedup_hit=False,
            tool_call_id="c1",
        ),
    )

    stored = [e for e in await event_store.read(run_id) if isinstance(e, ToolCallCompleted)]
    assert len(stored) == 1
    # Whatever came back is a plain JSON object with no non-finite floats,
    # which is the property the column actually enforces.
    assert isinstance(stored[0].result, dict)
    for value in stored[0].result.values():
        assert not (isinstance(value, float) and not math.isfinite(value))
