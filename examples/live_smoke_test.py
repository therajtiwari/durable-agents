"""The first real test of OpenAICompatibleClient against an actual
server — everything before this used httpx.MockTransport, never a
live network call.

This costs real (tiny) money / API quota, so it's a script you run
deliberately, not something in the automated test suite. A formal
@pytest.mark.live tier (skipped without a key, hard cost cap, never in
CI) is separate follow-up work — this is the manual "does it actually
work" check that comes before building that scaffolding.

Usage:
    export GROQ_API_KEY=gsk_...          # (or set on Windows: set GROQ_API_KEY=...)
    python examples/live_smoke_test.py

Uses InMemoryEventStore rather than Postgres — this checks the model
connection, not durability, so there's no reason to need Docker running.
"""

import asyncio
import os
import sys
from decimal import Decimal
from typing import Any

from durable_agents import InMemoryEventStore, Runtime, tool
from durable_agents.llm.openai_compatible import OpenAICompatibleClient

# Groq's free tier is OpenAI-compatible at this base_url. Override
# either via env to point at any other compatible provider —
# api.openai.com/v1, a local Ollama/vLLM server, OpenRouter, etc.
#
# Model names are NOT stable: providers decommission them on short
# notice and usually answer a retired name with a bare 404. Run
# examples/list_models.py to see what your provider serves right now.
BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")


@tool()
async def add_numbers(a: int, b: int) -> dict[str, Any]:
    """Add two numbers together and return the result."""
    print(f"   [tool call] add_numbers(a={a}, b={b})")
    return {"sum": a + b}


async def main() -> None:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("Set GROQ_API_KEY first (get a free one at console.groq.com).")
        sys.exit(1)

    llm = OpenAICompatibleClient(base_url=BASE_URL, model=MODEL, api_key=api_key)

    runtime = Runtime(
        store=InMemoryEventStore(),
        llm=llm,
        tools=[add_numbers],
        system_prompt="You are a precise calculator assistant. Always use the add_numbers tool for arithmetic, never compute it yourself.",
        max_steps=5,
        max_cost_usd=Decimal("0.10"),  # hard ceiling, in case rates get configured later
    )

    print(f"Calling {MODEL} at {BASE_URL} for real...")
    run = await runtime.start(goal="What is 47 plus 89? Use your tool to compute it.")

    print()
    print(f"status       : {run.state.status}")
    print(f"final answer : {run.state.final_answer}")
    print(f"steps        : {run.state.step}")
    print(f"provider said the tool call worked : {'136' in (run.state.final_answer or '')}")

    if run.state.status != "completed":
        print()
        print(f"NOT a clean success — failure_reason: {run.state.failure_reason}")
        for m in run.state.messages:
            print(f"  [{m.role}] {m.content}")

    await llm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
