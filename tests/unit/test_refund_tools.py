import pytest

from refund_demo import InMemoryRefundBackend, build_refund_tools


@pytest.mark.asyncio
async def test_same_idempotency_key_twice_creates_one_refund() -> None:
    """The central assertion of the project, made concrete for this one
    backend: attempting the same logical refund twice must not create a
    second refund, even though the attempt itself really happened twice.
    """

    backend = InMemoryRefundBackend()
    tools = {t.name: t for t in build_refund_tools(backend)}
    issue_refund = tools["issue_refund"]

    first = await issue_refund.execute(
        order_id="A-8891", amount_inr=6400, reason="damaged", idempotency_key="key-1"
    )
    second = await issue_refund.execute(
        order_id="A-8891", amount_inr=6400, reason="damaged", idempotency_key="key-1"
    )

    assert len(backend.attempts) == 2
    assert len(backend.refunds) == 1
    assert first["refund_id"] == second["refund_id"]
    assert second["dedup_hit"] is True
    assert "dedup_hit" not in first


@pytest.mark.asyncio
async def test_different_idempotency_keys_create_separate_refunds() -> None:
    backend = InMemoryRefundBackend()
    tools = {t.name: t for t in build_refund_tools(backend)}
    issue_refund = tools["issue_refund"]

    first = await issue_refund.execute(
        order_id="A-8891", amount_inr=3000, reason="damaged", idempotency_key="key-1"
    )
    second = await issue_refund.execute(
        order_id="A-8891", amount_inr=3000, reason="damaged", idempotency_key="key-2"
    )

    assert len(backend.attempts) == 2
    assert len(backend.refunds) == 2
    assert first["refund_id"] != second["refund_id"]


def test_idempotency_key_excluded_from_llm_facing_schema() -> None:
    backend = InMemoryRefundBackend()
    tools = {t.name: t for t in build_refund_tools(backend)}
    issue_refund = tools["issue_refund"]

    assert issue_refund.needs_idempotency_key is True
    assert "idempotency_key" not in issue_refund.parameters["properties"]


@pytest.mark.asyncio
async def test_lookup_order_unknown_id_returns_error() -> None:
    backend = InMemoryRefundBackend()
    tools = {t.name: t for t in build_refund_tools(backend)}
    lookup_order = tools["lookup_order"]

    result = await lookup_order.execute(order_id="NOPE")

    assert "error" in result
