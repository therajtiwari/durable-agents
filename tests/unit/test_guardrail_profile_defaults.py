"""What a run gets when nobody configured guardrails.

The old default was "standard", which blocks on injection patterns at
0.9 confidence. Since L2 scans every tool result, that killed runs on
ordinary machine output — a tool returning {"error": "system: disk
full"} matches injection_system_override and the run dies as
RunFailed(guardrail_block). In a library whose entire promise is that
runs survive, a bundled regex with a measured 20% false positive rate
was undermining the part that actually works.

The default is now "validation": everything that cannot produce a false
positive (schema, allowlist, policy caps, loop detection), and none of
the guessing. The pattern layer still exists and is opted into by name.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from durable_agents.events import GuardrailTriggered, RunFailed, RunStarted, ToolCallInvocation
from durable_agents.guardrails.decisions import get_profile
from durable_agents.llm.protocol import LLMResponse
from durable_agents.llm.scripted import ScriptedLLM
from durable_agents.orchestrator import Orchestrator
from durable_agents.state import Message
from durable_agents.storage.memory import InMemoryEventStore
from durable_agents.tools.registry import Tool, tool

# Ordinary machine output, not an attack: an error string and a support
# ticket. Both trip high-confidence injection patterns.
NOISY_ERROR = "system: disk full on node 3"
NOISY_TICKET = "Customer asks to disregard the previous quote"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_started(profile: str) -> RunStarted:
    return RunStarted(
        seq=0,
        created_at=_now(),
        goal="Check the failing node.",
        model="scripted",
        max_steps=15,
        max_cost_usd=Decimal("2.00"),
        requested_by="test",
        guardrail_profile=profile,
    )


def _llm_response(**overrides: object) -> LLMResponse:
    defaults: dict[str, object] = {
        "content": None,
        "tool_calls": [],
        "stop_reason": "end_turn",
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": Decimal("0.001"),
        "latency_ms": 500,
        "provider_request_id": "req",
    }
    defaults.update(overrides)
    return LLMResponse(**defaults)  # type: ignore[arg-type]


def _noisy_tools() -> dict[str, Tool]:
    @tool()
    async def read_logs(node: str) -> dict[str, Any]:
        """Returns real log output, which contains the word 'system:'."""
        return {"node": node, "error": NOISY_ERROR, "ticket": NOISY_TICKET}

    return {read_logs.name: read_logs}


def _script() -> ScriptedLLM:
    return ScriptedLLM(
        [
            _llm_response(
                content="Reading the logs.",
                tool_calls=[
                    ToolCallInvocation(id="c1", name="read_logs", arguments={"node": "3"})
                ],
                stop_reason="tool_use",
            ),
            _llm_response(content="Node 3 is out of disk."),
        ]
    )


async def _run(profile: str) -> tuple[str, list[GuardrailTriggered], list[RunFailed]]:
    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started(profile))
    state = await Orchestrator(store=store, llm=_script(), tools=_noisy_tools()).run(run_id)
    events = await store.read(run_id)
    return (
        state.status,
        [e for e in events if isinstance(e, GuardrailTriggered)],
        [e for e in events if isinstance(e, RunFailed)],
    )


@pytest.mark.asyncio
async def test_default_profile_does_not_kill_a_run_over_ordinary_log_output() -> None:
    status, triggered, failures = await _run("validation")

    assert status == "completed", "reading a log line must not be fatal"
    assert failures == []
    assert [e.rule for e in triggered] == [], "no injection detections under the default"


@pytest.mark.asyncio
async def test_standard_still_blocks_the_same_run_when_asked_for_by_name() -> None:
    """The pattern layer is unchanged — this is the behaviour someone
    opts into, and the false positive it produces is exactly why it is
    no longer the default.
    """

    status, triggered, failures = await _run("standard")

    assert status == "failed"
    assert [e.reason for e in failures] == ["guardrail_block"]
    assert any(e.rule.startswith("injection_") and e.action == "BLOCK" for e in triggered)


@pytest.mark.asyncio
async def test_off_records_no_guardrail_events_at_all() -> None:
    status, triggered, _failures = await _run("off")

    assert status == "completed"
    assert triggered == [], "an audit trail should show checks that ran, not checks that didn't"


@pytest.mark.asyncio
async def test_deterministic_checks_still_run_under_the_default() -> None:
    """The half worth keeping: an argument that violates a configured
    policy cap is blocked, and that is not a guess.
    """

    store = InMemoryEventStore()
    run_id = uuid4()
    await store.append(run_id, 0, _run_started("validation"))

    @tool(side_effect=True)
    async def issue_refund(order_id: str, amount_inr: int, idempotency_key: str) -> dict[str, Any]:
        """Issue a refund."""
        return {"refund_id": "RF-1"}

    llm = ScriptedLLM(
        [
            _llm_response(
                content="Refunding.",
                tool_calls=[
                    ToolCallInvocation(
                        id="c1",
                        name="issue_refund",
                        arguments={"order_id": "A-1", "amount_inr": 500_000},
                    )
                ],
                stop_reason="tool_use",
            )
        ]
    )
    state = await Orchestrator(
        store=store, llm=llm, tools={issue_refund.name: issue_refund}
    ).run(run_id)

    assert state.status == "failed"
    triggered = [e for e in await store.read(run_id) if isinstance(e, GuardrailTriggered)]
    assert any(e.rule == "policy_bounds_exceeded" and e.action == "BLOCK" for e in triggered)


@pytest.mark.asyncio
async def test_off_leaves_what_the_model_sees_untouched() -> None:
    """"Off" has to mean the library stops rewriting your prompts, or it
    is not worth having. Under every other profile the tool result is
    wrapped in untrusted-data markers before the model sees it.
    """

    orchestrator = Orchestrator(store=InMemoryEventStore(), llm=_script(), tools={})
    messages = [Message(role="tool", content='{"note": "hello"}', tool_name="read_logs")]

    off = orchestrator._sanitize_for_llm(messages, get_profile("off"))
    default = orchestrator._sanitize_for_llm(messages, get_profile("validation"))

    assert off[0].content == '{"note": "hello"}'
    assert "untrusted" in (default[0].content or ""), "the default still delimits tool results"
