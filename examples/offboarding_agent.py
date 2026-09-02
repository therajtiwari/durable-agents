"""A realistic consumer of this library: employee offboarding.

When someone leaves, their access has to be revoked across every system
they touched. That is a long sequence of side-effecting calls to
different vendors, and it is exactly the shape of problem this runtime
exists for:

  - It takes minutes, so the process WILL sometimes die halfway.
  - Re-running it must not re-do work already done (revoking twice is
    usually harmless; wiping a laptop twice, or re-sending a final
    paycheck, is not).
  - One step is destructive enough that a human should sign off.
  - Six months later, someone in compliance will ask exactly what was
    revoked, when, and who approved the destructive part.

Run it:
    docker compose up -d
    durable-agents init-db
    python examples/offboarding_agent.py

The LLM here is ScriptedLLM so this runs offline and free. In your own
application you would pass a real LLMClient — see the class at the
bottom of this file for the shape.
"""

import asyncio
from decimal import Decimal
from typing import Any

from durable_agents import (
    LLMResponse,
    PostgresEventStore,
    Runtime,
    ScriptedLLM,
    ToolCallInvocation,
    create_schema,
    tool,
)

DSN = "postgresql://durable_agents:durable_agents@localhost:5432/durable_agents"


# ---------------------------------------------------------------------
# 1. Your existing systems. Nothing about these is special — they're
#    whatever you already call today.
# ---------------------------------------------------------------------


class IdentityPlatform:
    """Stands in for Okta / Google Workspace / GitHub / AWS IAM.

    The `seen` dict is the part that matters: every real vendor API
    worth calling accepts an idempotency key, and this is what makes
    "run it again after a crash" safe. The runtime hands you the same
    key on a retry, so you pass it straight through.
    """

    def __init__(self) -> None:
        self.seen: dict[str, dict[str, Any]] = {}
        self.call_log: list[str] = []
        self._directory = {
            "e-4471": {"name": "Alex Chen", "team": "platform", "laptop": "MBP-2291"},
        }

    def lookup(self, employee_id: str) -> dict[str, Any]:
        record = self._directory.get(employee_id)
        return record or {"error": f"no such employee: {employee_id}"}

    def revoke(self, system: str, employee_id: str, idempotency_key: str) -> dict[str, Any]:
        self.call_log.append(f"{system}:{employee_id}")
        if idempotency_key in self.seen:
            # Already done in a previous attempt — return the original
            # result rather than doing it again.
            return {**self.seen[idempotency_key], "dedup_hit": True}
        result = {"system": system, "employee_id": employee_id, "revoked": True}
        self.seen[idempotency_key] = result
        return result


platform = IdentityPlatform()


# ---------------------------------------------------------------------
# 2. Declare your tools. This is the only library-specific thing you
#    write, and it's one decorator.
# ---------------------------------------------------------------------


@tool()
async def lookup_employee(employee_id: str) -> dict[str, Any]:
    """Look up an employee's record by id."""
    return platform.lookup(employee_id)


@tool(side_effect=True)
async def revoke_sso(employee_id: str, idempotency_key: str) -> dict[str, Any]:
    """Revoke the employee's single sign-on access."""
    return platform.revoke("sso", employee_id, idempotency_key)


@tool(side_effect=True)
async def revoke_source_control(employee_id: str, idempotency_key: str) -> dict[str, Any]:
    """Remove the employee from all source control organisations."""
    return platform.revoke("source_control", employee_id, idempotency_key)


# A destructive, irreversible step. `requires_approval` can be a plain
# True, or a predicate over the arguments — e.g. only require sign-off
# for production systems, or above a spend threshold.
@tool(requires_approval=True, side_effect=True)
async def wipe_laptop(employee_id: str, asset_tag: str, idempotency_key: str) -> dict[str, Any]:
    """Remotely wipe the employee's company laptop. Irreversible."""
    return platform.revoke(f"laptop:{asset_tag}", employee_id, idempotency_key)


# ---------------------------------------------------------------------
# 3. Wire it up and run.
# ---------------------------------------------------------------------


def _say(content: str | None, *calls: ToolCallInvocation) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=list(calls),
        stop_reason="tool_use" if calls else "end_turn",
        input_tokens=180,
        output_tokens=40,
        cost_usd=Decimal("0.002"),
        latency_ms=300,
        provider_request_id="scripted",
    )


def _call(name: str, **arguments: Any) -> ToolCallInvocation:
    return ToolCallInvocation(id=f"call_{name}", name=name, arguments=arguments)


def scripted_plan() -> list[LLMResponse | Exception]:
    """What a real model would decide, pre-baked so this runs offline.

    Note the deliberate `TimeoutError`: vendor APIs fail, and the point
    of the exercise is that a failure here doesn't lose the run.
    """

    return [
        _say("Looking up the employee first.", _call("lookup_employee", employee_id="e-4471")),
        _say("Revoking SSO.", _call("revoke_sso", employee_id="e-4471")),
        TimeoutError("source control API returned 503"),
        _say(
            "Removing source control access.",
            _call("revoke_source_control", employee_id="e-4471"),
        ),
        _say(
            "Requesting approval to wipe the laptop.",
            _call("wipe_laptop", employee_id="e-4471", asset_tag="MBP-2291"),
        ),
        _say("Offboarding complete: SSO, source control, and laptop all revoked."),
    ]


async def main() -> None:
    await create_schema(DSN)
    store = await PostgresEventStore.connect(DSN)

    runtime = Runtime(
        store=store,
        llm=ScriptedLLM(scripted_plan()),
        tools=[lookup_employee, revoke_sso, revoke_source_control, wipe_laptop],
        model="scripted",
        system_prompt=(
            "You are an IT offboarding agent. Revoke access in order, "
            "least destructive first. Never wipe a device you have not looked up."
        ),
        max_cost_usd=Decimal("0.50"),
        guardrail_profile="standard",
        retry_base_delay_seconds=0.1,
    )

    # --- day 1: kick it off ------------------------------------------
    run = await runtime.start(
        goal="Offboard employee e-4471. Revoke all access and wipe their laptop.",
        requested_by="hr-system",
    )

    print(f"status after start(): {run.state.status}")
    if run.state.pending_approval is not None:
        approval = run.state.pending_approval
        print(f"  waiting on: {approval.tool}({approval.arguments})")
        print(f"  reason    : {approval.reason}")
    print(f"  revocations performed so far: {platform.call_log}")

    # The run has PARKED. No thread is blocked, no timer is running —
    # this process could exit right now and the run would be unaffected,
    # because everything it knows is in Postgres. In a real deployment
    # the next line is a person clicking Approve in a UI, which hits
    # POST /runs/{id}/approve.
    await runtime.approve(run.id, approver="dana.p@example.com")

    # --- day 2: a completely different process picks it up -----------
    resumed_runtime = Runtime(
        store=store,
        # A fresh model client, positioned where the run actually left
        # off. (A real provider client needs no such bookkeeping — this
        # is only because ScriptedLLM replays a fixed list.)
        llm=ScriptedLLM(scripted_plan()[5:]),
        tools=[lookup_employee, revoke_sso, revoke_source_control, wipe_laptop],
        retry_base_delay_seconds=0.1,
    )
    final = await resumed_runtime.resume(run.id)

    print()
    print(f"status after approval + resume(): {final.status}")
    print(f"  final answer : {final.final_answer}")
    print(f"  physical API calls made : {len(platform.call_log)}")
    print(f"  distinct actions committed: {len(platform.seen)}")
    print()
    print(f"Full audit trail:  durable-agents replay {run.id}")


if __name__ == "__main__":
    asyncio.run(main())
