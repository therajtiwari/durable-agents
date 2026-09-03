"""Standalone script invoked as a subprocess by the chaos test suite.

Not part of the public CLI (cli.py) — this runs the canonical demo
scenario end-to-end, self-terminating after a specific event seq if
CHAOS_KILL_AFTER_SEQ (or CHAOS_KILL_AFTER_TOOL_EXECUTION_SEQ) is set in
the environment. Running it again on the same run_id resumes exactly
where the log left off: orchestrator.run() is resumable by construction,
and ScriptedLLM's position is recovered by counting how many
LLMCallCompleted events already exist in the log — a fresh ScriptedLLM
in a fresh process otherwise has no memory of which of its scripted
responses have already been consumed.
"""

import asyncio
import os
import signal
import sys
from uuid import UUID

from durable_agents.events import LLMCallCompleted
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.storage.postgres import PostgresEventStore
from refund_demo import (
    PostgresRefundBackend,
    build_refund_tools,
    canonical_run_started,
    canonical_script,
    parallel_refund_script,
    parallel_run_started,
)

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"
)

# CHAOS_SCENARIO=parallel runs the three-refunds-in-one-turn script
# instead of the canonical one-call-per-turn script, so the suite can
# kill a process partway through a batch.
SCENARIOS = {
    "canonical": (canonical_run_started, canonical_script),
    "parallel": (parallel_run_started, parallel_refund_script),
}


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value is not None else None


async def main(run_id: UUID) -> None:
    run_started, script = SCENARIOS[os.environ.get("CHAOS_SCENARIO", "canonical")]

    store = await PostgresEventStore.connect(DSN)
    events = await store.read(run_id)

    if not events:
        await store.append(run_id, 0, run_started(requested_by="chaos-test"))
        # RunStarted is appended before an Orchestrator (and its kill
        # hook) exists, so seq 0 needs its own check here — otherwise
        # CHAOS_KILL_AFTER_SEQ=0 would silently never fire.
        if _env_int("CHAOS_KILL_AFTER_SEQ") == 0:
            kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
            os.kill(os.getpid(), kill_signal)
        events = await store.read(run_id)

    already_completed = sum(1 for e in events if isinstance(e, LLMCallCompleted))
    llm = ScriptedLLM(script()[already_completed:])

    backend = await PostgresRefundBackend.connect(DSN)
    tools = {t.name: t for t in build_refund_tools(backend)}

    orchestrator = Orchestrator(
        store=store,
        llm=llm,
        tools=tools,
        kill_after_seq=_env_int("CHAOS_KILL_AFTER_SEQ"),
        kill_after_tool_execution_seq=_env_int("CHAOS_KILL_AFTER_TOOL_EXECUTION_SEQ"),
    )
    await orchestrator.run(run_id)


if __name__ == "__main__":
    asyncio.run(main(UUID(sys.argv[1])))
