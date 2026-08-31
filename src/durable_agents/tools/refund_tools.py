from typing import Any, Protocol

from durable_agents.tools.registry import Tool, tool


class RefundBackend(Protocol):
    async def get_order(self, order_id: str) -> dict[str, Any]: ...

    async def issue_refund(
        self, order_id: str, amount_inr: int, idempotency_key: str
    ) -> dict[str, Any]: ...


class InMemoryRefundBackend:
    """The only backend this project has — no real payments API exists
    anywhere here, by design; there is no version of this where a test
    run issues a real refund.

    Tracks every physical call attempt separately from the event log
    (attempts) and the refunds actually created, deduplicated by
    idempotency key (refunds). The gap between those two is what proves
    "two attempts, one refund" in code rather than merely asserting it —
    the same idempotency_key issued twice returns the already-created
    refund instead of making a second one.
    """

    def __init__(self) -> None:
        self._orders: dict[str, dict[str, Any]] = {
            "A-8891": {"amount_inr": 6400, "status": "delivered", "damaged": True},
        }
        self.attempts: list[dict[str, Any]] = []
        self.refunds: dict[str, dict[str, Any]] = {}

    async def get_order(self, order_id: str) -> dict[str, Any]:
        order = self._orders.get(order_id)
        if order is None:
            return {"error": f"no such order: {order_id}"}
        return {"order_id": order_id, **order}

    async def issue_refund(
        self, order_id: str, amount_inr: int, idempotency_key: str
    ) -> dict[str, Any]:
        self.attempts.append({"order_id": order_id, "idempotency_key": idempotency_key})
        if idempotency_key in self.refunds:
            return {**self.refunds[idempotency_key], "dedup_hit": True}
        refund = {
            "refund_id": f"RF-{len(self.refunds) + 1}",
            "amount_inr": amount_inr,
            "status": "processed",
        }
        self.refunds[idempotency_key] = refund
        return refund


def build_refund_tools(backend: RefundBackend) -> list[Tool]:
    """Wire the three refund tools to a backend, injected rather than
    hardcoded — lets tests supply an InMemoryRefundBackend and assert
    directly on its .attempts/.refunds ledgers.
    """

    @tool()
    async def lookup_order(order_id: str) -> dict[str, Any]:
        """Look up an order's details by its id."""
        return await backend.get_order(order_id)

    @tool()
    async def check_refund_policy(order_id: str, reason: str) -> dict[str, Any]:
        """Check whether an order is eligible for a refund, and for how much."""
        order = await backend.get_order(order_id)
        if "error" in order:
            return {"eligible": False, "max_refund_inr": 0}
        eligible = bool(order.get("damaged"))
        return {"eligible": eligible, "max_refund_inr": order["amount_inr"] if eligible else 0}

    @tool(requires_approval=lambda args: args["amount_inr"] > 5000, side_effect=True)
    async def issue_refund(
        order_id: str, amount_inr: int, reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        """Issue a refund for an order. Needs human approval above ₹5,000."""
        return await backend.issue_refund(order_id, amount_inr, idempotency_key)

    return [lookup_order, check_refund_policy, issue_refund]
