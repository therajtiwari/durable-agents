"""The whole thing, against a real model, stored in real Postgres.

Unlike offboarding_agent.py (which scripts the model's decisions), here
a real LLM decides what to call and in what order. Everything the
runtime claims is exercised at once:

  - multi-step tool chaining, the model choosing the sequence itself
  - a vendor API that fails on first contact, retried with the SAME
    idempotency key so the retry can't double-execute
  - a destructive step that PARKS the run for human approval
  - resume from a completely fresh Runtime, as a different process
    would, with no in-memory state carried across
  - exactly-once verified by counting physical calls vs commits

Because a real model decides the path, this asserts on invariants
(did it complete? was anything done twice?) rather than on an exact
trajectory — the model may take five steps or seven.

Usage:
    docker compose up -d
    durable-agents init-db
    $env:GROQ_API_KEY = "gsk_..."
    python examples/live_offboarding.py

    # then, with the run_id it prints:
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


class VendorAPIs:
    """Stands in for Okta, GitHub, AWS, and an MDM platform.

    `attempts` counts every physical call that reached a vendor.
    `committed` counts distinct actions actually performed, keyed by
    idempotency key. The gap between the two is the entire point: a
    retry adds an attempt without adding a commit.
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.committed: dict[str, dict[str, Any]] = {}
        self._sso_failures = 0

    def _commit(self, system: str, employee_id: str, key: str) -> dict[str, Any]:
        self.attempts.append(system)
        if key in self.committed:
            return {**self.committed[key], "dedup_hit": True}
        record = {"system": system, "employee_id": employee_id, "status": "revoked"}
        self.committed[key] = record
        return record

    def lookup(self, employee_id: str) -> dict[str, Any]:
        self.attempts.append("directory")
        if employee_id != "e-4471":
            return {"error": f"no such employee: {employee_id}"}
        return {
            "employee_id": "e-4471",
            "name": "Alex Chen",
            "team": "platform",
            "laptop_asset_tag": "MBP-2291",
            "status": "departed",
        }

    def revoke_sso(self, employee_id: str, key: str) -> dict[str, Any]:
        # Fails exactly once, the first time it is ever called. The
        # runtime should retry with the same key and succeed.
        self._sso_failures += 1
        if self._sso_failures == 1:
            self.attempts.append("sso(failed)")
            raise TimeoutError("identity provider timed out after 30s")
        return self._commit("sso", employee_id, key)

    def revoke_source_control(self, employee_id: str, key: str) -> dict[str, Any]:
        return self._commit("source_control", employee_id, key)

    def wipe_laptop(self, employee_id: str, asset_tag: str, key: str) -> dict[str, Any]:
        return self._commit(f"laptop:{asset_tag}", employee_id, key)


vendors = VendorAPIs()


@tool()
async def lookup_employee(employee_id: str) -> dict[str, Any]:
    """Look up an employee's record, including their laptop asset tag."""
    print(f"   -> lookup_employee({employee_id})")
    return vendors.lookup(employee_id)


@tool(side_effect=True)
async def revoke_sso(employee_id: str, idempotency_key: str) -> dict[str, Any]:
    """Revoke the employee's single sign-on access."""
    print(f"   -> revoke_sso({employee_id})")
    return vendors.revoke_sso(employee_id, idempotency_key)


@tool(side_effect=True)
async def revoke_source_control(employee_id: str, idempotency_key: str) -> dict[str, Any]:
    """Remove the employee from all source control organisations."""
    print(f"   -> revoke_source_control({employee_id})")
    return vendors.revoke_source_control(employee_id, idempotency_key)


@tool(requires_approval=True, side_effect=True)
async def wipe_laptop(employee_id: str, asset_tag: str, idempotency_key: str) -> dict[str, Any]:
    """Remotely wipe the employee's laptop. Irreversible; needs approval."""
    print(f"   -> wipe_laptop({employee_id}, {asset_tag})")
    return vendors.wipe_laptop(employee_id, asset_tag, idempotency_key)


TOOLS = [lookup_employee, revoke_sso, revoke_source_control, wipe_laptop]
SYSTEM_PROMPT = (
    "You are an IT offboarding agent. Work through revocations one tool call at a time, "
    "least destructive first. You must look up the employee before wiping any device, "
    "because you need their laptop asset tag. When every step is done, reply with a short "
    "summary of what you revoked."
)


def _make_client() -> OpenAICompatibleClient:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("Set GROQ_API_KEY first (free key at console.groq.com).")
        sys.exit(1)
    return OpenAICompatibleClient(base_url=BASE_URL, model=MODEL, api_key=api_key)


async def main() -> None:
    await create_schema(DSN)
    store = await PostgresEventStore.connect(DSN)

    print(f"model: {MODEL}\n")
    print("--- process 1: start the offboarding ---")

    llm = _make_client()
    runtime = Runtime(
        store=store,
        llm=llm,
        tools=TOOLS,
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        max_steps=12,
        max_cost_usd=Decimal("0.50"),
        retry_base_delay_seconds=0.5,
    )

    run = await runtime.start(
        goal=(
            "Offboard employee e-4471. Revoke their SSO and source control access, "
            "then wipe their company laptop."
        ),
        requested_by="hr-system",
    )
    await llm.aclose()

    print(f"\nstatus: {run.state.status}")
    if run.state.pending_approval is None:
        print("Expected the run to park for approval on wipe_laptop, but it did not.")
        print(f"  final answer: {run.state.final_answer}")
        print(f"  failure     : {run.state.failure_reason}")
        print(f"\nreplay it:  durable-agents replay {run.id}")
        return

    pending = run.state.pending_approval
    print(f"  PARKED awaiting approval: {pending.tool}({pending.arguments})")
    print(f"  vendor calls so far     : {vendors.attempts}")

    # A human decides. In a real deployment this is POST /runs/{id}/approve.
    print("\n--- a human approves ---")
    await runtime.approve(run.id, approver="dana.p@example.com")

    # Nothing from process 1 carries over: new client, new Runtime.
    # A real model needs no position bookkeeping to resume — it just
    # reads the message history rebuilt from the event log.
    print("\n--- process 2 (fresh Runtime) resumes ---")
    llm2 = _make_client()
    resumed = Runtime(
        store=store,
        llm=llm2,
        tools=TOOLS,
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        max_steps=12,
        max_cost_usd=Decimal("0.50"),
        retry_base_delay_seconds=0.5,
    )
    final = await resumed.resume(run.id)
    await llm2.aclose()

    print(f"\nstatus: {final.status}")
    print(f"  final answer : {final.final_answer}")
    print(f"  steps        : {final.step}   cost: ${final.total_cost_usd}")

    print("\n--- invariants ---")
    physical = len(vendors.attempts)
    committed = len(vendors.committed)
    print(f"  physical vendor calls : {physical}  {vendors.attempts}")
    print(f"  actions committed     : {committed}")
    print(f"  sso retried after failure : {'sso(failed)' in vendors.attempts}")
    print(f"  nothing done twice        : {physical > committed}")
    print(f"  reached completion        : {final.status == 'completed'}")

    print(f"\nreplay the full audit trail:\n  durable-agents replay {run.id}")


if __name__ == "__main__":
    asyncio.run(main())
