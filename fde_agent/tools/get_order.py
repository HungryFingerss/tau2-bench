"""get_order: full details of ONE of the verified customer's orders.

Model gives:   order_id  (any of "#W2378156", "W2378156", "w2378156", "2378156")
Checks:        customer verified; order belongs to them   (no store call if not)
Store call:    get_order_details  (always fresh: status may have changed)
Model sees:    status, address, items with labelled IDs + prices, payments,
               tracking, return/exchange details, and POSSIBLE ACTIONS for the
               status, written by code from the rulebook (policy.md).
Notes:         latest status; item/product IDs shown are remembered as seen.

Why: in the baseline the model guessed what it was allowed to do (it invented
`modify_delivered_order_items`, task 6) and misread raw JSON (D14).
"""

import json

from fde_agent import format as fmt
from fde_agent.errors import ToolError
from fde_agent.ids import ITEM, PRODUCT, label
from fde_agent.notes import Notes
from fde_agent.store import StoreCall, ToolSteps

NAME = "get_order"

DESCRIPTION = (
    "Show full details of one of the verified customer's orders: items with "
    "item IDs, product IDs and prices, payments, tracking, and which actions "
    "are possible for the order's current status."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "Order ID, e.g. #W2378156."},
    },
    "required": ["order_id"],
}


# ---------- 1. checks (no store call) ----------

def normalize_order_id(raw: str) -> str:
    """'w2378156' / '2378156' / ' #W2378156 ' -> '#W2378156'.  '' -> ''."""
    text = (raw or "").strip().upper().lstrip("#").strip()
    if not text:
        return ""
    if not text.startswith("W"):
        text = "W" + text
    return "#" + text


def _check(notes: Notes, order_id: str) -> None:
    if notes.customer is None:
        raise ToolError("Verify the customer first (verify_customer).")
    if not order_id:
        raise ToolError("Need an order ID. Ask the customer which order they mean.")
    if not notes.is_customers_order(order_id):
        theirs = ", ".join(notes.orders) or "none"
        raise ToolError(
            f"{order_id} is not one of {notes.customer.name}'s orders. "
            f"Their orders are: {theirs}."
        )


# ---------- 2. fetch ----------

def get_order(notes: Notes, order_id: str = "") -> ToolSteps:
    notes.check_open()
    order_id = normalize_order_id(order_id)
    _check(notes, order_id)

    [r] = yield [StoreCall("get_order_details", {"order_id": order_id})]
    if r.error:
        raise ToolError(f"Could not load order {order_id} ({r.content}).")
    order = json.loads(r.content)

    notes.record_order(order_id, order["status"])
    for it in order["items"]:
        notes.seen.remember(ITEM, it["item_id"])
        notes.seen.remember(PRODUCT, it["product_id"])
    for item_id in order.get("exchange_new_items") or []:
        notes.seen.remember(ITEM, item_id)

    return shape(order, notes.payment_methods)


# ---------- 3. shape (pure) ----------

def possible_actions(status: str) -> str:
    """The rulebook (policy.md), as code. Exact store statuses."""
    if status == "pending":
        return (
            "cancel the order (reason must be 'no longer needed' or 'ordered by mistake'); "
            "change the shipping address; change the payment method; "
            "change items to another option of the same product. "
            "Changing items can be done only ONCE, and after it the order can't be "
            "cancelled or changed in any way: so do any address or payment change first, "
            "and include every item in that one change."
        )
    if status.startswith("pending"):  # "pending (item modified)"
        return "none. Items were already changed, so this order can't be cancelled or changed any more."
    if status == "delivered":
        return (
            "return items; exchange items for another option of the same product. "
            "Exchange can be done only ONCE, so include every item in that one exchange."
        )
    if status == "processed":
        return "none. The order is already processed, so it can't be cancelled or changed."
    if status == "cancelled":
        return "none. The order is cancelled."
    if status in ("return requested", "exchange requested"):
        return f"none. A {status.split()[0]} has already been requested for this order."
    return f"none (unknown status '{status}')."


def shape(order: dict, payment_names: dict[str, str]) -> str:
    def pay(pid: str) -> str:
        name = payment_names.get(pid)
        return f"{name} (payment ID: {pid})" if name else f"payment ID: {pid}"

    lines = [
        f"Order {order['order_id']} | status: {order['status']}",
        f"Shipping address: {fmt.address(order['address'])}",
        f"Items ({len(order['items'])}):",
    ]
    for it, n in fmt.count_items(order["items"]):
        qty = f" each, x{n}" if n > 1 else ""
        lines.append(
            f"- {it['name']} | {label(ITEM, it['item_id'])} | {label(PRODUCT, it['product_id'])} | "
            f"{fmt.money(it['price'])}{qty} | {fmt.options(it['options'])}"
        )

    for t in order["payment_history"]:
        verb = "Paid" if t["transaction_type"] == "payment" else "Refunded"
        prep = "by" if verb == "Paid" else "to"
        lines.append(f"{verb}: {fmt.money(t['amount'])} {prep} {pay(t['payment_method_id'])}")

    tracking = [tid for f in order["fulfillments"] for tid in f["tracking_id"]]
    if tracking:
        lines.append(f"Tracking: {', '.join(tracking)}")
    if order.get("cancel_reason"):
        lines.append(f"Cancel reason: {order['cancel_reason']}")
    if order.get("return_items"):
        items = ", ".join(label(ITEM, i) for i in order["return_items"])
        lines.append(f"Return requested for: {items}; refund to {pay(order['return_payment_method_id'])}")
    if order.get("exchange_items"):
        old = ", ".join(label(ITEM, i) for i in order["exchange_items"])
        new = ", ".join(label(ITEM, i) for i in order["exchange_new_items"])
        diff = order.get("exchange_price_difference")
        lines.append(f"Exchange requested: {old} -> {new}"
                     + (f"; price difference {fmt.money(diff)}" if diff is not None else ""))

    lines.append(f"Possible actions (status: {order['status']}): {possible_actions(order['status'])}")
    return "\n".join(lines)
