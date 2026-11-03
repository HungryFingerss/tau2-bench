"""find_replacement: "the same item, but with X changed".

The model names the customer's item and ONLY what should change.
Code fills in every other option from the current item, finds the exact
replacement, and works out the price difference against what was paid.

Model gives:   order_id, item_id (labelled, from get_order), changes {option: value}
Checks:        verified; order is theirs; item ID real + seen; changes given  (no store call)
Store calls:   get_order_details (fresh) -> get_product_details
Rules:         order must be pending (change items) or delivered (exchange);
               item must be in that order; changes must change something;
               the item being replaced is NEVER offered (the store's exchange tool
               doesn't block it, so we do); a candidate that is ANOTHER item already
               in this order is offered but marked.
Model sees:    current item, wanted options, the replacement (or that it's not
               available + closest available ones), price difference, which action.
Notes:         order status; item IDs shown remembered as seen.
"""

import json

from fde_agent import format as fmt
from fde_agent.errors import ToolError
from fde_agent.ids import ITEM, label, unlabel
from fde_agent.notes import Notes
from fde_agent.store import StoreCall, ToolSteps
from fde_agent.tools.find_variants import resolve_options
from fde_agent.tools.get_order import normalize_order_id, possible_actions

NAME = "find_replacement"

DESCRIPTION = (
    "Find the replacement for an item in one of the customer's orders, keeping everything "
    "the same except the options the customer wants changed. Give order_id, item_id "
    "(from get_order) and changes, e.g. {\"color\": \"blue\"}. Shows the replacement item ID, "
    "whether it is available, the price difference, and the closest available options if not."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "Order ID, e.g. #W2378156."},
        "item_id": {"type": "string", "description": "The item to replace, e.g. item_1151293680."},
        "changes": {"type": "object", "description": "Only the options to change: name -> new value.",
                    "additionalProperties": {"type": "string"}},
    },
    "required": ["order_id", "item_id", "changes"],
}

IN_ORDER_NOTE = "  (the customer already has this exact item in this order)"


# ---------- 1. checks (no store call) ----------

def _check(notes: Notes, order_id: str, item_id: str, changes) -> str:
    if notes.customer is None:
        raise ToolError("Verify the customer first (verify_customer).")
    if not order_id:
        raise ToolError("Need the order ID of the item to replace.")
    if not notes.is_customers_order(order_id):
        raise ToolError(f"{order_id} is not one of {notes.customer.name}'s orders. "
                        f"Their orders are: {', '.join(notes.orders)}.")
    raw_item = unlabel(ITEM, item_id, notes.seen)  # IdError
    if not isinstance(changes, dict) or not any(str(v).strip() for v in changes.values()):
        raise ToolError('Give the options to change, e.g. {"color": "blue"}. '
                        'Only what the customer wants different; the rest is kept.')
    return raw_item


# ---------- 2. fetch ----------

def find_replacement(notes: Notes, order_id: str = "", item_id: str = "",
                     changes: dict | None = None) -> ToolSteps:
    notes.check_open()
    order_id = normalize_order_id(order_id)
    current_id = _check(notes, order_id, item_id, changes)
    changes = {k: str(v) for k, v in changes.items() if str(v).strip()}

    [r] = yield [StoreCall("get_order_details", {"order_id": order_id})]
    if r.error:
        raise ToolError(f"Could not load order {order_id} ({r.content}).")
    order = json.loads(r.content)
    notes.record_order(order_id, order["status"])

    if order["status"] not in ("pending", "delivered"):
        raise ToolError(f"Order {order_id} is '{order['status']}', so its items can't be "
                        f"changed or exchanged. Possible actions: {possible_actions(order['status'])}")

    current = next((it for it in order["items"] if it["item_id"] == current_id), None)
    if current is None:
        theirs = ", ".join(f"{label(ITEM, it['item_id'])} ({it['name']})" for it in order["items"])
        raise ToolError(f"{label(ITEM, current_id)} is not in order {order_id}. Its items are: {theirs}.")

    [p] = yield [StoreCall("get_product_details", {"product_id": current["product_id"]})]
    if p.error:
        raise ToolError(f"Could not load the product ({p.content}).")
    product = json.loads(p.content)
    variants = list(product["variants"].values())

    wanted = resolve_options(product["name"], variants, changes)  # "large" -> lists sizes
    target = {**current["options"], **wanted}
    if target == current["options"]:
        raise ToolError(f"That is what the customer already has: {fmt.options(current['options'])}. "
                        f"Ask what they want different.")

    others_in_order = {it["item_id"] for it in order["items"]} - {current_id}
    result = pick(variants, current, wanted, target)

    for v in result["exact"] + result["closest"]:
        notes.seen.remember(ITEM, v["item_id"])
    return shape(order, current, wanted, result, others_in_order)


# ---------- 3a. picking (pure) ----------

def pick(variants: list[dict], current: dict, wanted: dict, target: dict) -> dict:
    """exact: variants with exactly the target options (never the current item).
    closest (only if no exact one is available): available variants that keep every
    WANTED change and differ from the target in exactly one of the kept options."""
    candidates = [v for v in variants if v["item_id"] != current["item_id"]]
    exact = [v for v in candidates if v["options"] == target]

    closest = []
    if not any(v["available"] for v in exact):
        for v in sorted(candidates, key=lambda v: v["price"]):
            keeps_wanted = all(v["options"].get(n) == val for n, val in wanted.items())
            differs = [n for n in target if v["options"].get(n) != target[n]]
            if v["available"] and keeps_wanted and len(differs) == 1:
                closest.append(v)
    return {"exact": exact, "closest": closest}


# ---------- 3b. shape (pure) ----------

def price_difference(old: float, new: float) -> str:
    d = round(new - old, 2)
    if d > 0:
        return f"the customer pays {fmt.money(d)} more"
    if d < 0:
        return f"the customer gets {fmt.money(-d)} back"
    return "same price, nothing to pay or refund"


def shape(order: dict, current: dict, wanted: dict, result: dict, others_in_order: set) -> str:
    def line(v, extra=""):
        note = IN_ORDER_NOTE if v["item_id"] in others_in_order else ""
        return (f"{label(ITEM, v['item_id'])} | {fmt.money(v['price'])} | {fmt.options(v['options'])}"
                f"{extra} | {price_difference(current['price'], v['price'])}{note}")

    wanted_text = ", ".join(f"{n}: {v}" for n, v in wanted.items())
    lines = [
        f"Current: {current['name']} {label(ITEM, current['item_id'])} | paid {fmt.money(current['price'])} | "
        f"{fmt.options(current['options'])}",
        f"Wanted: {wanted_text} (everything else kept the same)",
    ]
    exact_available = [v for v in result["exact"] if v["available"]]
    if exact_available:
        lines += [f"Replacement: {line(v)}" for v in exact_available]
    elif result["exact"]:
        lines.append(f"The exact replacement exists but is NOT available (cannot be ordered): "
                     f"{label(ITEM, result['exact'][0]['item_id'])}")
    else:
        lines.append("No variant of this product has exactly these options.")

    if not exact_available:
        if result["closest"]:
            lines.append(f"Closest available (keep {wanted_text}, one other option different):")
            for v in result["closest"]:
                target = {**current["options"], **wanted}
                n = next(k for k in target if v["options"].get(k) != target[k])
                why = f"  <- {n}: {v['options'][n]} instead of {target[n]}"
                lines.append("- " + line(v, why))
        else:
            lines.append(f"No available variant keeps {wanted_text} with only one other option different. "
                         f"Use find_variants to show the customer every available option.")

    if order["status"] == "pending":
        lines.append("This order is pending, so this would be a CHANGE ITEMS: only once per order, and after "
                     "it the order can't be cancelled or changed. Include every item; do address or "
                     "payment changes first.")
    else:
        lines.append("This order is delivered, so this would be an EXCHANGE: only once per order. "
                     "Include every item to exchange.")
    return "\n".join(lines)
