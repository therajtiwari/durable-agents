"""Standalone script invoked as a subprocess by the chaos test suite.

Not part of the public CLI (cli.py) — this runs one fixed, canonical
scenario end-to-end, self-terminating after a specific event seq if
CHAOS_KILL_AFTER_SEQ is set in the environment. Running it again on the
same run_id resumes exactly where the log left off: orchestrator.run()
is resumable by construction, and ScriptedLLM's position is recovered by
counting how many LLMCallCompleted events already exist in the log — a
fresh ScriptedLLM in a fresh process otherwise has no memory of which of
its scripted responses have already been consumed.
"""

import asyncio
import os
import signal
import sys
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from durable_agents.events import LLMCallCompleted, RunStarted, ToolCallInvocation
from durable_agents.llm.protocol import LLMResponse
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.storage.postgres import PostgresEventStore
from durable_agents.tools.refund_backend_postgres import PostgresRefundBackend
from durable_agents.tools.refund_tools import build_refund_tools

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"
)


def _full_script() -> list[LLMResponse | Exception]:
    return [
        LLMResponse(
            content="Looking up the order.",
            tool_calls=[ToolCallInvocation(id="t1", name="lookup_order", arguments={"order_id": "A-8891"})],
            stop_reason="tool_use",
            input_tokens=400, output_tokens=50, cost_usd=Decimal("0.002"),
            latency_ms=5, provider_request_id="r1",
        ),
        LLMResponse(
            content="Checking refund policy.",
            tool_calls=[ToolCallInvocation(id="t2", name="check_refund_policy", arguments={"order_id": "A-8891", "reason": "damaged"})],
            stop_reason="tool_use",
            input_tokens=450, output_tokens=40, cost_usd=Decimal("0.002"),
            latency_ms=5, provider_request_id="r2",
        ),
        LLMResponse(
            content="Issuing the refund.",
            tool_calls=[ToolCallInvocation(id="t3", name="issue_refund", arguments={"order_id": "A-8891", "amount_inr": 3000, "reason": "damaged"})],
            stop_reason="tool_use",
            input_tokens=500, output_tokens=60, cost_usd=Decimal("0.003"),
            latency_ms=5, provider_request_id="r3",
        ),
        LLMResponse(
            content="Refund processed.",
            tool_calls=[],
            stop_reason="end_turn",
            input_tokens=550, output_tokens=30, cost_usd=Decimal("0.002"),
            latency_ms=5, provider_request_id="r4",
        ),
    ]


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value is not None else None


async def main(run_id: UUID) -> None:
    store = await PostgresEventStore.connect(DSN)
    events = await store.read(run_id)

    if not events:
        await store.append(
            run_id,
            0,
            RunStarted(
                seq=0,
                created_at=datetime.now(timezone.utc),
                goal="Process refund for order A-8891.",
                model="scripted",
                system_prompt_hash="sha256:chaos",
                max_steps=15,
                max_cost_usd=Decimal("2.00"),
                requested_by="chaos-test",
                guardrail_profile="financial_v1",
            ),
        )
        # RunStarted is appended before an Orchestrator (and its kill
        # hook) exists, so seq 0 needs its own check here — otherwise
        # CHAOS_KILL_AFTER_SEQ=0 would silently never fire.
        if _env_int("CHAOS_KILL_AFTER_SEQ") == 0:
            kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
            os.kill(os.getpid(), kill_signal)
        events = await store.read(run_id)

    already_completed = sum(1 for e in events if isinstance(e, LLMCallCompleted))
    llm = ScriptedLLM(_full_script()[already_completed:])

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
