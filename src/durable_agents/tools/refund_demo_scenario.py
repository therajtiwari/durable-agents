"""The one fixed, canonical refund scenario used across this project —
by the real CLI's demo (start/resume) and the chaos test suite's
scenario runner alike. Kept in one place so both stay in sync; this is
demo content, not part of the generic runtime (same category as
tools/refund_tools.py — see the Week 6 packaging note about moving both
out of the shipped package before publishing).
"""

from datetime import datetime, timezone
from decimal import Decimal

from durable_agents.events import RunStarted, ToolCallInvocation
from durable_agents.llm.protocol import LLMResponse


def canonical_script() -> list[LLMResponse | Exception]:
    return [
        LLMResponse(
            content="Looking up the order.",
            tool_calls=[
                ToolCallInvocation(id="t1", name="lookup_order", arguments={"order_id": "A-8891"})
            ],
            stop_reason="tool_use",
            input_tokens=400,
            output_tokens=50,
            cost_usd=Decimal("0.002"),
            latency_ms=5,
            provider_request_id="r1",
        ),
        LLMResponse(
            content="Checking refund policy.",
            tool_calls=[
                ToolCallInvocation(
                    id="t2",
                    name="check_refund_policy",
                    arguments={"order_id": "A-8891", "reason": "damaged"},
                )
            ],
            stop_reason="tool_use",
            input_tokens=450,
            output_tokens=40,
            cost_usd=Decimal("0.002"),
            latency_ms=5,
            provider_request_id="r2",
        ),
        LLMResponse(
            content="Issuing the refund.",
            tool_calls=[
                ToolCallInvocation(
                    id="t3",
                    name="issue_refund",
                    arguments={"order_id": "A-8891", "amount_inr": 3000, "reason": "damaged"},
                )
            ],
            stop_reason="tool_use",
            input_tokens=500,
            output_tokens=60,
            cost_usd=Decimal("0.003"),
            latency_ms=5,
            provider_request_id="r3",
        ),
        LLMResponse(
            content="Refund processed.",
            tool_calls=[],
            stop_reason="end_turn",
            input_tokens=550,
            output_tokens=30,
            cost_usd=Decimal("0.002"),
            latency_ms=5,
            provider_request_id="r4",
        ),
    ]


def canonical_run_started(requested_by: str) -> RunStarted:
    return RunStarted(
        seq=0,
        created_at=datetime.now(timezone.utc),
        goal="Process refund for order A-8891.",
        model="scripted",
        system_prompt=(
            "You are a customer support agent handling refunds. "
            "Look up the order and check the refund policy before issuing any refund. "
            "Never refund more than the order's value."
        ),
        max_steps=15,
        max_cost_usd=Decimal("2.00"),
        requested_by=requested_by,
        guardrail_profile="financial_v1",
    )
