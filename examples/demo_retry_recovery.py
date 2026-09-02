"""A flaky LLM provider (two 429s, then success) and a flaky tool
(one timeout, then success) — proving neither crashes the run.

Before retries existed, the first raised exception propagated out of
Orchestrator.run() and killed the process, leaving the run parked until
a human noticed and ran `resume` by hand.

Usage:
    python examples/demo_retry_recovery.py
    durable-agents replay <the run_id it prints>
"""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from durable_agents.events import RunStarted, ToolCallInvocation
from durable_agents.llm.protocol import LLMResponse
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.storage.postgres import PostgresEventStore
from durable_agents.tools.registry import Tool, tool

DSN = "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"


class FlakyPaymentsAPI:
    """Fails the first call, succeeds after. Records every
    idempotency_key it sees so the demo can show the retry reused the
    original one instead of minting a new key it couldn't deduplicate.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.charges: dict[str, dict[str, Any]] = {}

    async def charge(self, amount_inr: int, idempotency_key: str) -> dict[str, Any]:
        self.calls.append(idempotency_key)
        if len(self.calls) == 1:
            raise TimeoutError("payments API timed out after 30s")
        if idempotency_key in self.charges:
            return {**self.charges[idempotency_key], "dedup_hit": True}
        record = {"charge_id": "CH-1", "amount_inr": amount_inr, "status": "processed"}
        self.charges[idempotency_key] = record
        return record


def build_tools(api: FlakyPaymentsAPI) -> dict[str, Tool]:
    @tool(side_effect=True)
    async def charge_customer(amount_inr: int, idempotency_key: str) -> dict[str, Any]:
        """Charge the customer for their order."""
        return await api.charge(amount_inr, idempotency_key)

    return {charge_customer.name: charge_customer}


def _response(**overrides: Any) -> LLMResponse:
    defaults: dict[str, Any] = {
        "content": None,
        "tool_calls": [],
        "stop_reason": "end_turn",
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": Decimal("0.001"),
        "latency_ms": 120,
        "provider_request_id": "demo",
    }
    defaults.update(overrides)
    return LLMResponse(**defaults)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    store = await PostgresEventStore.connect(DSN)
    api = FlakyPaymentsAPI()

    run_id = uuid4()
    await store.append(
        run_id,
        0,
        RunStarted(
            seq=0,
            created_at=datetime.now(timezone.utc),
            goal="Charge the customer ₹2,000 for order A-8891.",
            model="scripted",
            system_prompt="You are a billing agent. Charge exactly what you are asked to charge.",
            max_steps=10,
            max_cost_usd=Decimal("1.00"),
            requested_by="demo",
            guardrail_profile="financial_v1",
        ),
    )

    llm = ScriptedLLM(
        [
            RuntimeError("429 Too Many Requests"),
            RuntimeError("500 Internal Server Error"),
            _response(
                content="Charging the customer.",
                tool_calls=[
                    ToolCallInvocation(
                        id="t1", name="charge_customer", arguments={"amount_inr": 2000}
                    )
                ],
                stop_reason="tool_use",
            ),
            _response(content="Charge processed successfully.", stop_reason="end_turn"),
        ]
    )

    orchestrator = Orchestrator(
        store=store, llm=llm, tools=build_tools(api), retry_base_delay_seconds=0.2
    )
    state = await orchestrator.run(run_id)

    print()
    print(f"Run {run_id}: {state.status}")
    print(f"  final answer      : {state.final_answer}")
    print(f"  physical charge attempts: {len(api.calls)}")
    print(f"  distinct idempotency keys: {len(set(api.calls))}")
    print(f"  charges actually created : {len(api.charges)}")
    print()
    print(f"See the full trace with: durable-agents replay {run_id}")


if __name__ == "__main__":
    asyncio.run(main())
