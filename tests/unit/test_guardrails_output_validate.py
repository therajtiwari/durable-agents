import pytest

from durable_agents.events import ToolCallInvocation
from durable_agents.guardrails.output_validate import validate_output
from refund_demo import InMemoryRefundBackend, build_refund_tools
from durable_agents.tools.registry import Tool


def _tools() -> dict[str, Tool]:
    return {t.name: t for t in build_refund_tools(InMemoryRefundBackend())}


@pytest.mark.asyncio
async def test_valid_call_produces_no_matches() -> None:
    tools = _tools()
    call = ToolCallInvocation(id="t1", name="lookup_order", arguments={"order_id": "A-8891"})
    result = await validate_output(call, tools)
    assert result.matches == []


@pytest.mark.asyncio
async def test_unregistered_tool_is_allowlist_violation() -> None:
    tools = _tools()
    call = ToolCallInvocation(id="t1", name="delete_all_orders", arguments={})
    result = await validate_output(call, tools)
    assert any(m.rule == "allowlist_violation" for m in result.matches)


@pytest.mark.asyncio
async def test_schema_mismatch_is_flagged() -> None:
    tools = _tools()
    # amount_inr should be an int; passing a non-numeric string fails
    # the tool's own auto-derived Pydantic schema.
    call = ToolCallInvocation(
        id="t1",
        name="issue_refund",
        arguments={"order_id": "A-8891", "amount_inr": "not-a-number", "reason": "damaged"},
    )
    result = await validate_output(call, tools)
    assert any(m.rule == "schema_invalid" for m in result.matches)


@pytest.mark.asyncio
async def test_policy_cap_violation_is_flagged() -> None:
    tools = _tools()
    call = ToolCallInvocation(
        id="t1",
        name="issue_refund",
        arguments={"order_id": "A-8891", "amount_inr": 500000, "reason": "damaged"},
    )
    result = await validate_output(call, tools, policy_caps={"issue_refund": {"amount_inr": 100000}})
    violation = next(m for m in result.matches if m.rule == "policy_bounds_exceeded")
    assert violation.detail["value"] == 500000
    assert violation.detail["cap"] == 100000


@pytest.mark.asyncio
async def test_within_policy_cap_is_not_flagged() -> None:
    tools = _tools()
    call = ToolCallInvocation(
        id="t1",
        name="issue_refund",
        arguments={"order_id": "A-8891", "amount_inr": 6400, "reason": "damaged"},
    )
    result = await validate_output(call, tools, policy_caps={"issue_refund": {"amount_inr": 100000}})
    assert not any(m.rule == "policy_bounds_exceeded" for m in result.matches)


@pytest.mark.asyncio
async def test_pii_in_model_arguments_is_detected() -> None:
    tools = _tools()
    call = ToolCallInvocation(
        id="t1",
        name="issue_refund",
        arguments={"order_id": "A-8891", "amount_inr": 100, "reason": "contact jane@example.com"},
    )
    result = await validate_output(call, tools)
    assert any(m.rule == "pii_email" for m in result.matches)
