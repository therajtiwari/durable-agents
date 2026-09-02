"""A worker: the process that makes runs actually happen by themselves.

This is the piece that turns "durable" into "self-healing". Without it,
a run created over the API sits in the log doing nothing until a human
types `durable-agents resume`, and a run whose process was killed
mid-tool-call stays half-finished forever. With this running, both get
picked up automatically.

Run it alongside the API server to see the whole thing work:

    # terminal 1
    docker compose up -d
    durable-agents init-db
    python examples/run_api_server.py

    # terminal 2
    $env:LLM_API_KEY = "gsk_..."
    python examples/run_worker.py

    # terminal 3 — create a run; the worker picks it up within a second
    curl -X POST http://localhost:8000/runs -H "Content-Type: application/json" \
         -d "{\"goal\": \"What is 47 plus 89?\", \"max_steps\": 5}"
    durable-agents replay <the run_id it returns>

Deliberately runs with NO tools, because this is a generic worker and
has no way to know what functions a given deployment wants wired up.
Your own worker would pass tools=[...] to Runtime, exactly as
examples/live_offboarding.py does — everything else here stays the same.
"""

import asyncio
import logging
import os
import sys
from decimal import Decimal

from durable_agents import PostgresEventStore, Runtime, Worker, create_schema
from durable_agents.llm.openai_compatible import OpenAICompatibleClient

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"
)
BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        print("Set LLM_API_KEY first (a free Groq key works).")
        sys.exit(1)

    await create_schema(DSN)
    store = await PostgresEventStore.connect(DSN)
    llm = OpenAICompatibleClient(base_url=BASE_URL, model=MODEL, api_key=api_key)

    runtime = Runtime(store=store, llm=llm, model=MODEL, max_cost_usd=Decimal("0.50"))
    worker = Worker(
        runtime,
        # Short so the demo is responsive. In production set this
        # comfortably above your slowest single LLM or tool call —
        # a run quiet for less than this is assumed to have a live
        # worker on it. Too low just means two workers occasionally
        # race, which is safe but doubles the model spend for that run.
        stale_after_seconds=30.0,
        poll_interval_seconds=1.0,
    )

    try:
        await worker.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        await llm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
