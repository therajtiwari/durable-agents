from typing import Any

from durable_agents.tools.registry import tool

# In-memory demo data — this project never talks to a real payments API,
# by design; there is no version of this where a test run issues a real
# refund. No attempt-ledger tracking yet — added once idempotency/
# crash-resume testing actually needs it.
_ORDERS: dict[str, dict[str, Any]] = {
    "A-8891": {"amount_inr": 6400, "status": "delivered", "damaged": True},
}


@tool()
async def lookup_order(order_id: str) -> dict[str, Any]:
    """Look up an order's details by its id."""
    order = _ORDERS.get(order_id)
    if order is None:
        return {"error": f"no such order: {order_id}"}
    return {"order_id": order_id, **order}


@tool()
async def check_refund_policy(order_id: str, reason: str) -> dict[str, Any]:
    """Check whether an order is eligible for a refund, and for how much."""
    order = _ORDERS.get(order_id)
    if order is None:
        return {"eligible": False, "max_refund_inr": 0}
    eligible = bool(order.get("damaged"))
    return {"eligible": eligible, "max_refund_inr": order["amount_inr"] if eligible else 0}


@tool(requires_approval=lambda args: args["amount_inr"] > 5000, side_effect=True)
async def issue_refund(order_id: str, amount_inr: int, reason: str) -> dict[str, Any]:
    """Issue a refund for an order. Needs human approval above ₹5,000."""
    return {"refund_id": f"RF-{order_id}", "amount_inr": amount_inr, "status": "processed"}
