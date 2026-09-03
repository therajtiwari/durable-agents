"""The zero-setup crash-resume proof.

One fixed scripted conversation against fake refund tools: no API key, no
network call, nothing to configure beyond Postgres. Kill it partway
through and run it again with the same run id — it picks up exactly
where the log left off, and the refund is issued once regardless of how
many times you interrupt it.

    docker compose up -d
    uv run durable-agents init-db
    psql ... -f db/migrations/002_refund_ledger.sql   # the demo ledger

    uv run python examples/crash_resume_demo.py
    uv run python examples/crash_resume_demo.py <run_id>   # resume one
    uv run durable-agents replay <run_id>

This used to be `durable-agents demo`, a subcommand of the shipped CLI.
It moved here when the refund modules moved out of the package: a
library's console script should not carry a fixed demo of somebody
else's business domain, and the command could not have kept working once
the code it depended on stopped shipping. Nothing about the
demonstration changed.

Starting is just resuming from an empty log — the same property
orchestrator.run() is built around — so one code path handles both a
fresh run and one that was killed mid-flight. ScriptedLLM's position is
recovered by counting the LLMCallCompleted events already in the log,
since a fresh instance in a fresh process has no memory of which
responses were already consumed.
"""

import asyncio
import os
import sys
from uuid import UUID, uuid4

from refund_demo import (
    PostgresRefundBackend,
    build_refund_tools,
    canonical_run_started,
    canonical_script,
)

from durable_agents.events import LLMCallCompleted
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.storage.postgres import PostgresEventStore

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"
)


async def main(run_id: UUID) -> None:
    store = await PostgresEventStore.connect(DSN)
    events = await store.read(run_id)

    if not events:
        await store.append(run_id, 0, canonical_run_started(requested_by="crash-resume-demo"))
        events = await store.read(run_id)

    already_completed = sum(1 for e in events if isinstance(e, LLMCallCompleted))
    llm = ScriptedLLM(canonical_script()[already_completed:])

    backend = await PostgresRefundBackend.connect(DSN)
    tools = {t.name: t for t in build_refund_tools(backend)}

    final_state = await Orchestrator(store=store, llm=llm, tools=tools).run(run_id)

    print(f"Run {run_id}: {final_state.status}")
    print(f"(see the full trace with: durable-agents replay {run_id})")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    run_id = UUID(sys.argv[1]) if len(sys.argv) > 1 else uuid4()
    if len(sys.argv) == 1:
        print(f"Starting demo run {run_id}")
    asyncio.run(main(run_id))
