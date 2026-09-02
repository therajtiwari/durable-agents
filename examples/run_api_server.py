"""Runs the HTTP API for real, against real Postgres, so you can curl it
or poke at it from the browser's auto-generated docs.

Usage:
    docker compose up -d
    durable-agents init-db
    python examples/run_api_server.py

Then, in another terminal:
    curl -X POST http://localhost:8000/runs -H "Content-Type: application/json" \
         -d "{\"goal\": \"Refund order A-8891\", \"requested_by\": \"me\"}"

Or open http://localhost:8000/docs — FastAPI generates an interactive
page for every endpoint, request body included, no curl needed.

This only starts and records runs (POST /runs), and reads/approves/
denies them — it does not execute anything. Nothing in this process
ever calls an LLM, matching how create_app() is actually built (see
DECISIONS.md's "API surface" section for why POST /runs records rather
than executes). To watch a run actually progress after starting it
here: `durable-agents resume <run_id>` (needs LLM_API_KEY set; runs
with no tools, so only a goal that needs pure reasoning will complete)
or a real script wired against Runtime/Orchestrator for anything that
needs tools.
"""

import asyncio
import os

import uvicorn

from durable_agents import PostgresEventStore, create_schema
from durable_agents.api.app import create_app

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"
)


async def _serve() -> None:
    # Everything from pool creation to the server's last request must
    # run on the SAME event loop, because asyncpg binds a connection
    # pool to the loop that created it. uvicorn.run() starts its own
    # fresh loop internally, which is exactly what broke this the first
    # time: building the pool via a separate asyncio.run() call first,
    # then handing it to uvicorn.run()'s own loop, produced
    # "cannot perform operation: another operation is in progress" (the
    # pool's connections belonged to a loop that had already been torn
    # down). Driving uvicorn.Server.serve() directly, awaited from
    # inside the same coroutine that built the pool, keeps it all on one
    # loop for the process's entire lifetime.
    await create_schema(DSN)
    store = await PostgresEventStore.connect(DSN)
    app = create_app(store)

    print("API running at http://localhost:8000")
    print("Interactive docs at http://localhost:8000/docs")
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    await uvicorn.Server(config).serve()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
