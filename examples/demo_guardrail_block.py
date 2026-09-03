"""Runs a scripted refund request that asks for far more than the policy
cap allows, so L3's guardrail blocks it before the tool ever executes.

Usage:
    python examples/demo_guardrail_block.py
    durable-agents replay <the run_id it prints>

Uses the same real Postgres event store and refund backend the CLI's
`start`/`resume` commands use — just with a deliberately "attacking"
scripted LLM response instead of the canonical clean demo scenario.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from durable_agents.events import RunStarted, ToolCallInvocation
from durable_agents.llm.protocol import LLMResponse
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.storage.postgres import PostgresEventStore
from refund_demo import PostgresRefundBackend, build_refund_tools

DSN = "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"


async def main() -> None:
    store = await PostgresEventStore.connect(DSN)
    backend = await PostgresRefundBackend.connect(DSN)
    tools = {t.name: t for t in build_refund_tools(backend)}

    run_id = uuid4()
    await store.append(
        run_id,
        0,
        RunStarted(
            seq=0,
            created_at=datetime.now(timezone.utc),
            goal="Process refund for order A-8891.",
            model="scripted",
            system_prompt_hash="sha256:demo",
            max_steps=10,
            max_cost_usd=Decimal("1.00"),
            requested_by="demo",
            guardrail_profile="financial_v1",
        ),
    )

    # A model that's been talked into asking for far more than the
    # ₹1,00,000 policy cap allows — same shape as an indirect-injection
    # attack succeeding, without needing a real poisoned tool result to
    # trigger it.
    llm = ScriptedLLM(
        [
            LLMResponse(
                content="Issuing the full refund as instructed.",
                tool_calls=[
                    ToolCallInvocation(
                        id="t1",
                        name="issue_refund",
                        arguments={"order_id": "A-8891", "amount_inr": 500_000, "reason": "damaged"},
                    )
                ],
                stop_reason="tool_use",
                input_tokens=100,
                output_tokens=20,
                cost_usd=Decimal("0.001"),
                latency_ms=100,
                provider_request_id="demo",
            )
        ]
    )

    orchestrator = Orchestrator(store=store, llm=llm, tools=tools)
    state = await orchestrator.run(run_id)

    print(f"Run {run_id}: {state.status} ({state.failure_reason})")
    print(f"See the full trace with: durable-agents replay {run_id}")


if __name__ == "__main__":
    asyncio.run(main())
