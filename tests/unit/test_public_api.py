"""What a stranger who ran `pip install durable-agents` can actually do.

These tests import only from the top-level package, never from internal
module paths — if they pass, the public surface is real.
"""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

import durable_agents
from durable_agents import (
    ConcurrencyConflict,
    InMemoryEventStore,
    LLMResponse,
    RunStarted,
    Runtime,
    ScriptedLLM,
    ToolCallInvocation,
    schema_sql,
    tool,
)


def _response(**overrides: object) -> LLMResponse:
    defaults: dict[str, object] = {
        "content": None,
        "tool_calls": [],
        "stop_reason": "end_turn",
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": Decimal("0.0001"),
        "latency_ms": 10,
        "provider_request_id": "req",
    }
    defaults.update(overrides)
    return LLMResponse(**defaults)  # type: ignore[arg-type]


def test_every_advertised_export_exists() -> None:
    missing = [name for name in durable_agents.__all__ if not hasattr(durable_agents, name)]
    assert missing == []


def test_version_is_exposed() -> None:
    assert durable_agents.__version__


@pytest.mark.asyncio
async def test_readme_shaped_example_actually_runs() -> None:
    """The five-line example from the README, executed. If this test
    ever fails, the README is lying to people.
    """

    @tool(side_effect=True)
    async def issue_refund(order_id: str, amount: int, idempotency_key: str) -> dict[str, object]:
        """Refund an order."""
        return {"refund_id": "RF-1", "order_id": order_id, "amount": amount}

    runtime = Runtime(
        store=InMemoryEventStore(),
        llm=ScriptedLLM(
            [
                _response(
                    content="Refunding.",
                    tool_calls=[
                        ToolCallInvocation(
                            id="t1",
                            name="issue_refund",
                            arguments={"order_id": "A-8891", "amount": 400},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                _response(content="Refund processed.", stop_reason="end_turn"),
            ]
        ),
        tools=[issue_refund],
    )
    run = await runtime.start(goal="Refund order A-8891, item arrived damaged.")

    assert run.state.status == "completed"
    assert run.state.final_answer == "Refund processed."
    # The id must come back too — a parked run is useless without it.
    assert await runtime.get_state(run.id) == run.state


@pytest.mark.asyncio
async def test_create_records_without_executing_then_resume_finishes_it() -> None:
    runtime = Runtime(
        store=InMemoryEventStore(),
        llm=ScriptedLLM([_response(content="Done.", stop_reason="end_turn")]),
    )

    run_id = await runtime.create(goal="Do the thing.", requested_by="queue")
    parked = await runtime.get_state(run_id)
    assert parked.status == "running"
    assert parked.step == 0, "create() must not execute anything"

    finished = await runtime.resume(run_id)
    assert finished.status == "completed"


@pytest.mark.asyncio
async def test_runtime_defaults_are_recorded_on_the_run() -> None:
    runtime = Runtime(
        store=InMemoryEventStore(),
        llm=ScriptedLLM([_response(content="ok", stop_reason="end_turn")]),
        model="test-model",
        system_prompt="Be careful.",
        max_steps=7,
        max_cost_usd="0.50",
        guardrail_profile="strict",
    )
    run_id = await runtime.create(goal="g")
    state = await runtime.get_state(run_id)

    assert state.system_prompt == "Be careful."
    assert state.max_steps == 7
    assert state.max_cost_usd == Decimal("0.50")
    assert state.guardrail_profile == "strict"


@pytest.mark.asyncio
async def test_per_run_overrides_beat_runtime_defaults() -> None:
    runtime = Runtime(
        store=InMemoryEventStore(),
        llm=ScriptedLLM([_response(content="ok", stop_reason="end_turn")]),
        max_steps=7,
    )
    run_id = await runtime.create(goal="g", max_steps=99, system_prompt="Override.")
    state = await runtime.get_state(run_id)

    assert state.max_steps == 99
    assert state.system_prompt == "Override."


def test_duplicate_tool_names_are_rejected_at_construction() -> None:
    @tool()
    async def thing() -> dict[str, object]:
        """A tool."""
        return {}

    with pytest.raises(ValueError, match="duplicate tool names"):
        Runtime(store=InMemoryEventStore(), llm=ScriptedLLM([]), tools=[thing, thing])


@pytest.mark.asyncio
async def test_in_memory_store_mirrors_postgres_concurrency_semantics() -> None:
    store = InMemoryEventStore()
    run_id = uuid4()
    started = RunStarted(
        seq=0,
        created_at=datetime.now(timezone.utc),
        goal="g",
        model="m",
        max_steps=1,
        max_cost_usd=Decimal("1"),
        requested_by="t",
        guardrail_profile="standard",
    )
    await store.append(run_id, 0, started)

    # Same rejection a real (run_id, seq) primary key gives you.
    with pytest.raises(ConcurrencyConflict):
        await store.append(run_id, 0, started)

    assert await store.read(run_id) == [started]
    assert store.run_ids() == [run_id]


@pytest.mark.asyncio
async def test_approve_then_resume_without_hand_building_events() -> None:
    """The whole point of the facade: a parked run should be approvable
    without the caller knowing about event seqs at all.
    """

    @tool(requires_approval=True, side_effect=True)
    async def wipe_laptop(asset_tag: str, idempotency_key: str) -> dict[str, object]:
        """Wipe a laptop."""
        return {"wiped": asset_tag}

    plan = [
        _response(
            content="Wiping.",
            tool_calls=[
                ToolCallInvocation(id="t1", name="wipe_laptop", arguments={"asset_tag": "MBP-1"})
            ],
            stop_reason="tool_use",
        ),
        _response(content="Done.", stop_reason="end_turn"),
    ]
    store = InMemoryEventStore()
    runtime = Runtime(store=store, llm=ScriptedLLM(plan), tools=[wipe_laptop])

    run = await runtime.start(goal="Wipe MBP-1.")
    assert run.state.status == "awaiting_approval"

    await runtime.approve(run.id, approver="dana@example.com")

    resumed = Runtime(store=store, llm=ScriptedLLM(plan[1:]), tools=[wipe_laptop])
    final = await resumed.resume(run.id)
    assert final.status == "completed"


@pytest.mark.asyncio
async def test_deny_feeds_the_reason_back_to_the_model() -> None:
    @tool(requires_approval=True, side_effect=True)
    async def wipe_laptop(asset_tag: str, idempotency_key: str) -> dict[str, object]:
        """Wipe a laptop."""
        return {"wiped": asset_tag}

    store = InMemoryEventStore()
    runtime = Runtime(
        store=store,
        llm=ScriptedLLM(
            [
                _response(
                    content="Wiping.",
                    tool_calls=[
                        ToolCallInvocation(
                            id="t1", name="wipe_laptop", arguments={"asset_tag": "MBP-1"}
                        )
                    ],
                    stop_reason="tool_use",
                )
            ]
        ),
        tools=[wipe_laptop],
    )
    run = await runtime.start(goal="Wipe MBP-1.")
    await runtime.deny(run.id, approver="dana@example.com", reason="device already returned")

    resumed = Runtime(
        store=store,
        llm=ScriptedLLM([_response(content="Understood, skipping.", stop_reason="end_turn")]),
        tools=[wipe_laptop],
    )
    final = await resumed.resume(run.id)

    assert final.status == "completed"
    assert any(
        m.role == "tool" and m.content is not None and "already returned" in m.content
        for m in final.messages
    )


@pytest.mark.asyncio
async def test_approving_a_run_that_is_not_parked_is_rejected() -> None:
    runtime = Runtime(
        store=InMemoryEventStore(),
        llm=ScriptedLLM([_response(content="done", stop_reason="end_turn")]),
    )
    run = await runtime.start(goal="g")

    with pytest.raises(ValueError, match="not awaiting approval"):
        await runtime.approve(run.id, approver="dana@example.com")


def test_schema_sql_ships_with_the_package() -> None:
    sql = schema_sql()
    assert "CREATE TABLE IF NOT EXISTS events" in sql
    assert "PRIMARY KEY (run_id, seq)" in sql
