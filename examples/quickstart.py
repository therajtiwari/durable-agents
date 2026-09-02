"""The smallest thing that works. No Postgres, no Docker, no API key.

    python examples/quickstart.py

Everything here is in-memory, so the run dies with the process — that's
the one thing this library exists to prevent, and the point of starting
here is that you can see the shape of it before setting up a database.
For the durable version, see offboarding_agent.py.
"""

import asyncio
from decimal import Decimal
from typing import Any

from durable_agents import (
    InMemoryEventStore,
    LLMResponse,
    Runtime,
    ScriptedLLM,
    ToolCallInvocation,
    rebuild_state,
    tool,
)


@tool()
async def lookup_order(order_id: str) -> dict[str, Any]:
    """Look up an order by id."""
    print(f"   [tool] lookup_order({order_id})")
    return {"order_id": order_id, "amount": 400, "status": "delivered", "damaged": True}


@tool(side_effect=True)
async def issue_refund(order_id: str, amount: int, idempotency_key: str) -> dict[str, Any]:
    """Refund an order. Real money moves here."""
    print(f"   [tool] issue_refund({order_id}, {amount}) key={idempotency_key[:12]}...")
    return {"refund_id": "RF-1", "amount": amount}


def _say(content: str, *calls: ToolCallInvocation) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=list(calls),
        stop_reason="tool_use" if calls else "end_turn",
        input_tokens=50,
        output_tokens=12,
        cost_usd=Decimal("0.0004"),
        latency_ms=200,
        provider_request_id="scripted",
    )


async def main() -> None:
    store = InMemoryEventStore()

    # ScriptedLLM stands in for a real provider so this runs offline.
    # Swap in your own LLMClient and the rest is unchanged.
    runtime = Runtime(
        store=store,
        llm=ScriptedLLM(
            [
                _say(
                    "Let me look up the order.",
                    ToolCallInvocation(
                        id="c1", name="lookup_order", arguments={"order_id": "A-8891"}
                    ),
                ),
                _say(
                    "It's damaged, issuing the refund.",
                    ToolCallInvocation(
                        id="c2",
                        name="issue_refund",
                        arguments={"order_id": "A-8891", "amount": 400},
                    ),
                ),
                _say("Refunded 400 for order A-8891."),
            ]
        ),
        tools=[lookup_order, issue_refund],
        system_prompt="You are a refund agent. Check the order before refunding.",
    )

    print("running...")
    run = await runtime.start(goal="Refund order A-8891, the item arrived damaged.")

    print()
    print(f"status : {run.state.status}")
    print(f"answer : {run.state.final_answer}")
    print(f"steps  : {run.state.step}   cost: ${run.state.total_cost_usd}")

    print()
    print("the event log this was rebuilt from:")
    for event in await store.read(run.id):
        print(f"  seq={event.seq:2d}  {type(event).__name__}")

    # The core property, demonstrated: state is a pure fold over events,
    # so replaying the same log always yields the same state. This is
    # what lets a different process pick a dead one's work up.
    replayed = rebuild_state(await store.read(run.id))
    print()
    print(f"replaying the log gives an identical state: {replayed == run.state}")


if __name__ == "__main__":
    asyncio.run(main())
