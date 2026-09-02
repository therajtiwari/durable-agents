"""Production incident triage — a task where the model's judgment is
genuinely load-bearing, not decoration.

The offboarding example is a fixed checklist: a for-loop would do it
better, cheaper, and deterministically. This one is different. The
investigation path is not knowable in advance, because what you look at
next depends entirely on what you just found.

THE TRAP, deliberately built into the data:

  An alert fires on checkout-service. checkout-service HAS a recent
  deploy (v2.31, 20 minutes ago). The obvious, scriptable response —
  "latency alert -> roll back that service's latest deploy" — is WRONG.

  The real cause is a config change to payments-service 35 minutes ago
  that cut its DB connection pool from 50 to 5. checkout-service is
  merely the loudest victim. Rolling back checkout v2.31 would cause a
  second outage while leaving the actual fault in place.

To get this right the model must chain evidence: read the alert, search
logs, notice the timeouts are on an outbound call, check that
dependency, pull ITS deploy history, correlate timestamps, and only
then propose a remediation — against a different service than the one
that alerted.

Then, because rolling back production is destructive, the run PARKS for
human approval, and resumes in a fresh process after sign-off.

Usage:
    docker compose up -d
    durable-agents init-db
    $env:GROQ_API_KEY = "gsk_..."
    python examples/live_incident_triage.py
    durable-agents replay <run_id>
"""

import asyncio
import os
import sys
from decimal import Decimal
from typing import Any

from durable_agents import PostgresEventStore, Runtime, create_schema, tool
from durable_agents.llm.openai_compatible import OpenAICompatibleClient

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"
)
BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")


# --------------------------------------------------------------------
# The fake production estate. Note that the evidence is deliberately
# arranged so the obvious answer is the wrong one.
# --------------------------------------------------------------------

LOGS: dict[str, list[str]] = {
    "checkout-service": [
        "14:12:03 ERROR  upstream call failed: payments-service timeout after 3000ms",
        "14:12:03 ERROR  upstream call failed: payments-service timeout after 3000ms",
        "14:11:58 WARN   retrying payments-service call (attempt 2/3)",
        "14:11:41 ERROR  upstream call failed: payments-service timeout after 3000ms",
        "14:10:22 INFO   checkout v2.31 healthy, serving traffic",
    ],
    "payments-service": [
        "14:12:02 ERROR  could not acquire connection from pool within 3000ms (active=5, idle=0)",
        "14:11:55 ERROR  could not acquire connection from pool within 3000ms (active=5, idle=0)",
        "14:11:40 WARN   connection pool saturated: 5/5 in use, 47 requests queued",
        "13:57:10 INFO   applied config change: db.pool.max_size 50 -> 5",
    ],
}

DEPLOYS: dict[str, list[dict[str, Any]]] = {
    "checkout-service": [
        {
            "version": "v2.31",
            "deployed_at": "13:52:00",
            "minutes_ago": 20,
            "change": "copy tweak on the order confirmation page",
            "previous_version": "v2.30",
        },
        {
            "version": "v2.30",
            "deployed_at": "11:20:00",
            "minutes_ago": 172,
            "change": "add structured logging",
            "previous_version": "v2.29",
        },
    ],
    "payments-service": [
        {
            "version": "cfg-881",
            "deployed_at": "13:57:00",
            "minutes_ago": 15,
            "change": "reduce db.pool.max_size from 50 to 5 (cost-saving experiment)",
            "previous_version": "cfg-880",
        },
        {
            "version": "v4.02",
            "deployed_at": "09:05:00",
            "minutes_ago": 307,
            "change": "support partial refunds",
            "previous_version": "v4.01",
        },
    ],
}

METRICS: dict[str, dict[str, str]] = {
    "checkout-service": {
        "p99_latency_ms": "4200 (baseline 180) — began climbing at 14:11",
        "error_rate": "23% (baseline 0.1%)",
        "cpu_percent": "31 (baseline 28) — normal",
    },
    "payments-service": {
        "p99_latency_ms": "3050 (baseline 95) — began climbing at 13:58",
        "error_rate": "41% (baseline 0.2%)",
        "db_pool_utilisation": "100% saturated since 13:58",
    },
}

DEPENDENCIES: dict[str, list[str]] = {
    "checkout-service": ["payments-service", "inventory-service"],
    "payments-service": ["postgres-primary"],
}


class Estate:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.actions: dict[str, dict[str, Any]] = {}


estate = Estate()


@tool()
async def get_alert() -> dict[str, Any]:
    """Fetch the details of the incident alert that is currently firing."""
    estate.calls.append("get_alert")
    print("   -> get_alert()")
    return {
        "alert_id": "ALERT-9912",
        "fired_at": "14:12",
        "service": "checkout-service",
        "summary": "p99 latency 4200ms exceeds 500ms threshold; error rate 23%",
        "severity": "P1",
    }


@tool()
async def search_logs(service: str) -> dict[str, Any]:
    """Search recent log lines for a service. Use this to find out what is actually failing."""
    estate.calls.append(f"search_logs:{service}")
    print(f"   -> search_logs({service})")
    lines = LOGS.get(service)
    if lines is None:
        return {"error": f"unknown service: {service}", "known": sorted(LOGS)}
    return {"service": service, "lines": lines}


@tool()
async def get_metrics(service: str) -> dict[str, Any]:
    """Get current metrics for a service compared against its baseline."""
    estate.calls.append(f"get_metrics:{service}")
    print(f"   -> get_metrics({service})")
    data = METRICS.get(service)
    if data is None:
        return {"error": f"unknown service: {service}", "known": sorted(METRICS)}
    return {"service": service, "metrics": data}


@tool()
async def get_dependencies(service: str) -> dict[str, Any]:
    """List the services a given service calls downstream."""
    estate.calls.append(f"get_dependencies:{service}")
    print(f"   -> get_dependencies({service})")
    return {"service": service, "depends_on": DEPENDENCIES.get(service, [])}


@tool()
async def get_recent_deploys(service: str) -> dict[str, Any]:
    """List recent deploys and config changes for a service, newest first."""
    estate.calls.append(f"get_recent_deploys:{service}")
    print(f"   -> get_recent_deploys({service})")
    data = DEPLOYS.get(service)
    if data is None:
        return {"error": f"unknown service: {service}", "known": sorted(DEPLOYS)}
    return {"service": service, "deploys": data}


@tool(requires_approval=True, side_effect=True)
async def rollback_deploy(service: str, version: str, reason: str, idempotency_key: str) -> dict[str, Any]:
    """Roll a service back to the state before the given version.

    Destructive and production-affecting: always requires human approval.
    """
    estate.calls.append(f"rollback:{service}:{version}")
    print(f"   -> rollback_deploy({service}, {version})")
    if idempotency_key in estate.actions:
        return {**estate.actions[idempotency_key], "dedup_hit": True}
    record = {"service": service, "rolled_back": version, "status": "completed"}
    estate.actions[idempotency_key] = record
    return record


TOOLS = [
    get_alert,
    search_logs,
    get_metrics,
    get_dependencies,
    get_recent_deploys,
    rollback_deploy,
]

SYSTEM_PROMPT = (
    "You are an on-call SRE triaging a production incident. Investigate before you act. "
    "A service that is alerting is not necessarily the service that is broken — it may be "
    "a downstream victim, so follow the evidence through dependencies. Correlate the "
    "timing of any change against when symptoms actually began. Do not roll anything back "
    "until the evidence identifies a specific change as the cause. When you are confident, "
    "roll back exactly that change and explain your reasoning."
)


def _make_client() -> OpenAICompatibleClient:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("Set GROQ_API_KEY first (free key at console.groq.com).")
        sys.exit(1)
    return OpenAICompatibleClient(base_url=BASE_URL, model=MODEL, api_key=api_key)


def _runtime(store: PostgresEventStore, llm: OpenAICompatibleClient) -> Runtime:
    return Runtime(
        store=store,
        llm=llm,
        tools=TOOLS,
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        max_steps=20,
        max_cost_usd=Decimal("0.50"),
        retry_base_delay_seconds=0.5,
    )


async def main() -> None:
    await create_schema(DSN)
    store = await PostgresEventStore.connect(DSN)

    print(f"model: {MODEL}\n")
    print("--- P1 incident fires, agent begins triage ---")

    llm = _make_client()
    runtime = _runtime(store, llm)
    run = await runtime.start(
        goal=(
            "A P1 alert is firing. Investigate the incident, identify the root cause, "
            "and roll back whatever change caused it."
        ),
        requested_by="pagerduty",
    )
    await llm.aclose()

    print(f"\nstatus: {run.state.status}")

    if run.state.pending_approval is None:
        print("Run did not park for approval — the model never proposed a rollback.")
        print(f"  final answer : {run.state.final_answer}")
        print(f"  failure      : {run.state.failure_reason}")
        print(f"\nreplay:  durable-agents replay {run.id}")
        return

    proposal = run.state.pending_approval
    target_service = proposal.arguments.get("service")
    target_version = proposal.arguments.get("version")

    print(f"\n  PARKED for approval — the agent proposes:")
    print(f"    service : {target_service}")
    print(f"    version : {target_version}")
    print(f"    reason  : {proposal.arguments.get('reason')}")

    print("\n  --- did it fall for the trap? ---")
    if target_service == "payments-service":
        print("    CORRECT: identified the dependency's config change as root cause.")
    elif target_service == "checkout-service":
        print("    WRONG: rolled back the alerting service — the obvious but incorrect answer.")
    else:
        print(f"    UNEXPECTED target: {target_service}")

    print(f"\n  investigation took {len(estate.calls)} tool calls:")
    for call in estate.calls:
        print(f"    - {call}")

    print("\n--- human approves the rollback ---")
    await runtime.approve(run.id, approver="sre-oncall@example.com")

    print("\n--- fresh process resumes after approval ---")
    llm2 = _make_client()
    final = await _runtime(store, llm2).resume(run.id)
    await llm2.aclose()

    print(f"\nstatus: {final.status}")
    print(f"  final answer : {final.final_answer}")
    print(f"  steps        : {final.step}   cost: ${final.total_cost_usd}")
    print(f"  rollbacks actually executed : {len(estate.actions)}")

    print(f"\nreplay the full investigation:\n  durable-agents replay {run.id}")


if __name__ == "__main__":
    asyncio.run(main())
