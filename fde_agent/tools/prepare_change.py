"""prepare_change: check a database change and describe it. NEVER makes it.

The change only happens later, via confirm_change, after the customer says yes.
confirm_change runs exactly the store call stored here, so what runs is what
the customer approved. A new prepare_change replaces the old one.

One tool, 7 kinds (one checker function per kind, so it can be split later):
  cancel_order            -> cancel_pending_order
  change_order_address    -> modify_pending_order_address
  change_order_payment    -> modify_pending_order_payment
  change_order_items      -> modify_pending_order_items
  return_items            -> return_delivered_order_items
  exchange_items          -> exchange_delivered_order_items
  change_default_address  -> modify_user_address

Every rule of the rulebook and every check of the store is tested here first,
so a bad change is caught before the store is touched.
"""

import json

from fde_agent import format as fmt
from fde_agent.errors import ToolError
from fde_agent.ids import ITEM, unlabel
from fde_agent.notes import Notes
from fde_agent.store import StoreCall, ToolSteps
from fde_agent.tools.find_variants import norm
from fde_agent.tools.get_order import normalize_order_id, possible_actions

NAME = "prepare_change"

REASONS = ("no longer needed", "ordered by mistake")
ADDRESS_FIELDS = ("address1", "address2", "city", "state", "country", "zip")

# kind -> (required inputs, example call)
KINDS = {
    "cancel_order": (("order_id", "reason"),
        '{"kind": "cancel_order", "order_id": "#W6247578", "reason": "no longer needed"}'),
    "change_order_address": (("order_id", "address"),
        '{"kind": "change_order_address", "order_id": "#W6247578", "address": {"address1": "12 Main St", '
        '"address2": "Apt 4", "city": "Austin", "state": "TX", "country": "USA", "zip": "78701"}}'),
    "change_order_payment": (("order_id", "payment_method_id"),
        '{"kind": "change_order_payment", "order_id": "#W6247578", "payment_method_id": "gift_card_1234567"}'),
    "change_order_items": (("order_id", "items", "payment_method_id"),
        '{"kind": "change_order_items", "order_id": "#W6247578", "items": [{"item_id": "item_3799046073", '
        '"new_item_id": "item_2060066974"}], "payment_method_id": "credit_card_9513926"}'),
    "return_items": (("order_id", "item_ids", "payment_method_id"),
        '{"kind": "return_items", "order_id": "#W2378156", "item_ids": ["item_1151293680"], '
        '"payment_method_id": "credit_card_9513926"}'),
    "exchange_items": (("order_id", "items", "payment_method_id"),
        '{"kind": "exchange_items", "order_id": "#W2378156", "items": [{"item_id": "item_4983901480", '
        '"new_item_id": "item_7747408585"}], "payment_method_id": "credit_card_9513926"}'),
    "change_default_address": (("address",),
        '{"kind": "change_default_address", "address": {"address1": "12 Main St", "address2": "", '
        '"city": "Austin", "state": "TX", "country": "USA", "zip": "78701"}}'),
}

DESCRIPTION = (
    "Prepare a change to an order or to the customer's default address. This does NOT make "
    "the change: it checks it and returns a summary to read to the customer. Only after the "
    "customer clearly says yes, call confirm_change with the change number. "
    "Pick one kind and give only its inputs:\n"
    + "\n".join(f"- {k}: needs {', '.join(req)}. Example: {ex}" for k, (req, ex) in KINDS.items())
    + "\nFor change_order_items and exchange_items, put EVERY item in one call: each order "
    "allows this only once. item_id / new_item_id come from get_order / find_replacement / "
    "find_variants. payment_method_id comes from verify_customer."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "order_id": {"type": "string"},
        "reason": {"type": "string", "enum": list(REASONS)},
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "item_id": {"type": "string"}, "new_item_id": {"type": "string"}},
            "required": ["item_id", "new_item_id"]}},
        "item_ids": {"type": "array", "items": {"type": "string"}},
        "payment_method_id": {"type": "string"},
        "address": {"type": "object", "properties": {f: {"type": "string"} for f in ADDRESS_FIELDS}},
    },
    "required": ["kind"],
}

# Warnings the customer must hear (the user's rule: say clearly it can't be undone,
# and ask for everything else first). Only TRUE statements for each kind.
# Statements, not questions: every summary ends with ONE question (ASK). A second
# question ("Is there any other item...?") invites "No, nothing else. Go ahead", and the
# "no" in it made the strict yes rule refuse a real yes (smoke run, 6 Nov).
WARN_ITEMS = (
    "This can be done only once. After it, I can't cancel or change this order at all, not even "
    "the address or payment. If you want to change any other item in this order, tell me now, so "
    "I can include it. If you want an address or payment change on this order, tell me now "
    "and I'll do that first. Once I confirm it, I can't change or undo it."
)
WARN_DELIVERED = (
    "After this, nothing else in this order can be returned or exchanged. If you want to {verb} "
    "another item in this order too, tell me now, so I can include it. Once I confirm it, I can't "
    "change or undo it."
)
WARN_CANCEL = "Once I confirm it, I can't undo this."
# The first and last lines of every summary say plainly that nothing has happened yet.
# Practice run, 7 Nov: the summary read like a finished change ("You get $16.63 back..."),
# and in 4 of 9 tasks the customer said "Yes, go ahead" and ended the chat in the SAME
# message, before the agent could make the change. The baseline's customer never did.
NOT_YET = "Here's the change I'll make. Nothing has changed yet."
ASK = "Shall I go ahead? I'll make the change as soon as you say yes."


def title(customer_text: str) -> str:
    """A summary's own first line, e.g. "Cancel order #W1: ...", without the NOT_YET line."""
    return customer_text.removeprefix(NOT_YET + "\n").splitlines()[0]


def core_lines(customer_text: str) -> list[str]:
    """What the change IS, without the lines that ask for a yes: no NOT_YET, no warnings,
    no question, no other payment options. Used for the 'done' message (F22)."""
    asking = {NOT_YET, ASK, WARN_CANCEL, WARN_ITEMS,
              *(WARN_DELIVERED.format(verb=v) for v in ("return", "exchange"))}
    return [line for line in customer_text.splitlines()
            if line not in asking and not line.startswith("You can also")]


# F24: other orders that have the same product, and could need the same change.
# Practice run, 7 Nov (task 4): "make them ALL purple" -- two pending orders had a T-Shirt,
# only one was changed. Only orders in the status this kind needs are listed.
SAME_PRODUCT_STATUS = {"change_order_items": "pending", "return_items": "delivered",
                       "exchange_items": "delivered"}


def same_product_elsewhere(notes: Notes, kind: str, order: dict | None, item_ids: list[str]) -> list[str]:
    """'#W4776164 (pending) also has: T-Shirt' for every other order with the same product."""
    status = SAME_PRODUCT_STATUS.get(kind)
    if status is None or order is None:
        return []
    names = {it["product_id"]: it["name"] for it in (_order_item(order, i) for i in item_ids) if it}
    found = []
    for oid, products in notes.order_products.items():
        if oid == order["order_id"] or notes.orders.get(oid) != status:
            continue
        shared = sorted({names[p] for p in products if p in names})
        if shared:
            found.append(f"{oid} ({status}) also has: {', '.join(shared)}")
    return found


def ignored_inputs(kind: str, args: dict) -> list[str]:
    """Inputs the model sent that this kind doesn't use, e.g. an address for a return.
    The tool ignores them; the driver logs them, so the practice runs can count how
    often the model mixes up the kinds (evidence for splitting this tool into seven)."""
    if kind not in KINDS:
        return []
    needed = set(KINDS[kind][0]) | {"kind"}
    return sorted(k for k, v in (args or {}).items() if k not in needed and v is not None)


# ---------- 1. checks (no store call) ----------

def _missing(kind: str, given: dict) -> list[str]:
    required, _ = KINDS[kind]
    return [f for f in required if not given.get(f)]


def _check(notes: Notes, kind: str, given: dict) -> dict:
    """Validate shapes and IDs. Returns cleaned inputs (store IDs, exact reason...)."""
    if notes.customer is None:
        raise ToolError("Verify the customer first (verify_customer).", fact=False)
    if kind not in KINDS:
        raise ToolError(f"Unknown kind '{kind}'. Kinds are: {', '.join(KINDS)}.", fact=False)
    missing = _missing(kind, given)
    if missing:
        raise ToolError(f"{kind} needs: {', '.join(missing)}. Example: {KINDS[kind][1]}", fact=False)

    clean: dict = {}
    if "order_id" in KINDS[kind][0]:
        oid = normalize_order_id(given["order_id"])
        if not notes.is_customers_order(oid):
            raise ToolError(f"{oid} is not one of {notes.customer.name}'s orders. "
                            f"Their orders are: {', '.join(notes.orders)}.", fact=False)
        clean["order_id"] = oid

    if kind == "cancel_order":
        reason = next((r for r in REASONS if norm(r) == norm(given["reason"])), None)
        if reason is None:
            raise ToolError(f"The reason must be '{REASONS[0]}' or '{REASONS[1]}'. "
                            f"Ask the customer which one applies. No other reason is accepted.", fact=True)
        clean["reason"] = reason

    if "items" in KINDS[kind][0]:
        pairs = given["items"]
        if not isinstance(pairs, list) or not all(
                isinstance(p, dict) and p.get("item_id") and p.get("new_item_id") for p in pairs):
            raise ToolError('items must be a list like [{"item_id": "item_...", "new_item_id": "item_..."}].', fact=False)
        clean["items"] = [(unlabel(ITEM, p["item_id"], notes.seen),
                           unlabel(ITEM, p["new_item_id"], notes.seen)) for p in pairs]

    if "item_ids" in KINDS[kind][0]:
        ids = given["item_ids"]
        if isinstance(ids, str):
            ids = [ids]
        clean["item_ids"] = [unlabel(ITEM, i, notes.seen) for i in ids]

    if "payment_method_id" in KINDS[kind][0]:
        pid = str(given["payment_method_id"]).strip()
        if pid not in notes.payment_methods:
            theirs = "; ".join(f"{name} (payment ID: {i})" for i, name in notes.payment_methods.items())
            raise ToolError(f"'{pid}' is not one of {notes.customer.name}'s payment methods. "
                            f"Theirs are: {theirs}.", fact=True)
        clean["payment_method_id"] = pid

    if "address" in KINDS[kind][0]:
        clean["address"] = _clean_address(given["address"])
    return clean


def _clean_address(a) -> dict:
    if not isinstance(a, dict):
        raise ToolError("address must be an object with: " + ", ".join(ADDRESS_FIELDS) + ".", fact=False)
    out = {f: str(a.get(f) or "").strip() for f in ADDRESS_FIELDS}
    missing = [f for f in ADDRESS_FIELDS if f != "address2" and not out[f]]
    if missing:
        raise ToolError(f"The address is missing: {', '.join(missing)}. Ask the customer for it. "
                        f"(address2 may be empty.)", fact=False)
    return out


# ---------- 2. fetch, then 3. the checker for the kind ----------

def _no_yes_waiting(notes: Notes) -> None:
    """A clear yes to the shown change must be confirmed first: a new prepare_change would
    replace that change and throw the yes away. Practice run, 7 Nov: after "Yes. Now do the
    other one." the model prepared the next return, and the first one was lost (task 11);
    after "Yeah, let's go ahead" it prepared the same change again (task 1)."""
    p = notes.prepared
    if p is None or notes.shown != p.number or notes.reply is None or notes.reply_is_yes is False:
        return
    from fde_agent.tools.confirm_change import is_clear_yes_to  # here: confirm_change imports this module
    if notes.reply_is_yes or is_clear_yes_to(p, notes.reply):
        raise ToolError(
            f'The customer said yes to change #{p.number} ("{notes.reply}"). Confirm it first: '
            f"confirm_change(change_number={p.number}). A new prepare_change now would throw "
            f"change #{p.number} away. If the customer wants something else too, prepare it after.", fact=False)


def prepare_change(notes: Notes, kind: str = "", order_id: str = "", reason: str = "",
                   items: list | None = None, item_ids: list | None = None,
                   payment_method_id: str = "", address: dict | None = None) -> ToolSteps:
    notes.check_open()
    _no_yes_waiting(notes)
    kind = (kind or "").strip()
    given = {"order_id": order_id, "reason": reason, "items": items, "item_ids": item_ids,
             "payment_method_id": payment_method_id, "address": address}
    c = _check(notes, kind, given)

    calls = [StoreCall("get_user_details", {"user_id": notes.customer.user_id})]
    if "order_id" in c:
        calls.append(StoreCall("get_order_details", {"order_id": c["order_id"]}))
    results = yield calls
    for r in results:
        if r.error:
            raise ToolError(f"Could not load the details ({r.content}).", fact=False)
    user = json.loads(results[0].content)
    order = json.loads(results[1].content) if len(results) > 1 else None
    if order:
        notes.record_order(order["order_id"], order["status"], order["items"])

    products = {}
    if "items" in c:
        pids = sorted({_order_item(order, old)["product_id"] for old, _ in c["items"]
                       if _order_item(order, old)})
        if pids:
            prod_results = yield [StoreCall("get_product_details", {"product_id": p}) for p in pids]
            for p, r in zip(pids, prod_results):
                if r.error:
                    raise ToolError(f"Could not load a product ({r.content}).", fact=False)
                products[p] = json.loads(r.content)

    call, text = CHECKERS[kind](c, user, order, products)
    text = f"{NOT_YET}\n{text}"
    change = notes.prepare(kind, call, text)
    out = (
        f"Change #{change.number} prepared. NOT done yet. Tell the customer this and ask for a clear yes:\n"
        f"---\n{text}\n---\n"
        f"If the customer clearly says yes to exactly this: confirm_change(change_number={change.number}).\n"
        f"If they want anything different: call prepare_change again (this replaces change #{change.number})."
    )
    old_ids = [old for old, _ in c.get("items", [])] + list(c.get("item_ids", []))
    elsewhere = same_product_elsewhere(notes, kind, order, old_ids)
    if elsewhere:   # for the model only: the customer's summary is unchanged
        out += ("\nThe same product is in other orders: " + "; ".join(elsewhere) + ". If the customer "
                "asked for all of them, prepare those after this one is done. Don't change them "
                "without asking.")
    return out


# ---------- shared helpers ----------

def _order_item(order: dict, item_id: str) -> dict | None:
    return next((it for it in order["items"] if it["item_id"] == item_id), None)


def _need_status(order: dict, status: str, doing: str) -> None:
    if order["status"] != status:
        raise ToolError(f"Order {order['order_id']} is '{order['status']}', so it can't {doing} "
                        f"(only '{status}' orders can). Possible actions: {possible_actions(order['status'])}", fact=True)


def _items_in_order(order: dict, item_ids: list[str]) -> None:
    """Each item must be in the order, as many times as it is listed (orders can have x2)."""
    have = [it["item_id"] for it in order["items"]]
    for i in set(item_ids):
        if item_ids.count(i) > have.count(i):
            names = ", ".join(f"item_{it['item_id']} ({it['name']})" for it in order["items"])
            what = "is not in" if i not in have else f"is in it only {have.count(i)} time(s) in"
            raise ToolError(f"item_{i} {what} order {order['order_id']}. Its items are: {names}.", fact=False)


def _method(user: dict, pid: str) -> dict:
    return user["payment_methods"][pid]


def _method_text(user: dict, pid: str) -> str:
    return f"your {fmt.payment_name(_method(user, pid))}"


def _refund_timing(user: dict, pid: str) -> str:
    return "immediately" if _method(user, pid)["source"] == "gift_card" else "in 5 to 7 business days"


def _alternatives(user: dict, chosen: str, allowed, lead: str = "You can also choose") -> list[str]:
    """If the customer has other VALID payment methods for this change, one line naming
    them. The model may have picked a method itself; this way the customer still sees
    every option before saying yes. allowed(payment_method) -> bool."""
    others = []
    for pid, m in user["payment_methods"].items():
        if pid == chosen or not allowed(m):
            continue
        balance = f" (balance {fmt.money(m['balance'])})" if m["source"] == "gift_card" else ""
        others.append(f"{_method_text(user, pid)}{balance}")
    return [f"{lead}: {'; '.join(others)}."] if others else []


def _difference_alternatives(user: dict, pid: str, diff: float) -> list[str]:
    """For a price difference: any method works, but a gift card must cover what's paid."""
    if round(diff, 2) == 0:
        return []
    return _alternatives(user, pid, lambda m: m["source"] != "gift_card" or diff < 0
                         or m["balance"] >= round(diff, 2))


def _money_line(diff: float, user: dict, pid: str) -> str:
    d = round(diff, 2)
    if d > 0:
        return f"You pay {fmt.money(d)} more, charged to {_method_text(user, pid)}."
    if d < 0:
        return f"You get {fmt.money(-d)} back, to {_method_text(user, pid)}."
    return "There is no price difference."


def _gift_card_covers(user: dict, pid: str, amount: float, what: str) -> None:
    m = _method(user, pid)
    if m["source"] == "gift_card" and m["balance"] < round(amount, 2):
        raise ToolError(f"The gift card balance ({fmt.money(m['balance'])}) is less than {what} "
                        f"({fmt.money(amount)}). Ask the customer to choose another payment method.", fact=True)


def _replacements(order: dict, pairs: list, products: dict) -> tuple[list[str], float]:
    """Check every old -> new pair; return customer lines and the total price difference."""
    _items_in_order(order, [old for old, _ in pairs])
    lines, diff = [], 0.0
    for old, new in pairs:
        cur = _order_item(order, old)
        if new == old:  # the user's rule; the store's exchange tool doesn't check this
            raise ToolError(f"item_{new} is the same item the customer already has ({cur['name']}). "
                            f"Pick a different option, e.g. with find_replacement.", fact=True)
        variant = products[cur["product_id"]]["variants"].get(new)
        if variant is None:
            raise ToolError(f"item_{new} is not a {cur['name']}. An item can only be replaced by "
                            f"another option of the same product (use find_replacement).", fact=True)
        if not variant["available"]:
            raise ToolError(f"item_{new} ({cur['name']}: {fmt.options(variant['options'])}) is not "
                            f"available. Use find_replacement to see available options.", fact=True)
        changed = ", ".join(f"{k} {cur['options'][k]} -> {v}"
                            for k, v in variant["options"].items() if cur["options"].get(k) != v)
        lines.append(f"- {cur['name']}: {changed} ({fmt.money(cur['price'])} -> {fmt.money(variant['price'])})")
        diff += variant["price"] - cur["price"]
    return lines, round(diff, 2)


def _addr_args(a: dict) -> dict:
    return {f: a[f] for f in ADDRESS_FIELDS}


# ---------- one checker per kind: (inputs, user, order, products) -> (store call, customer text) ----------

def check_cancel(c, user, order, products):
    _need_status(order, "pending", "be cancelled")
    refunds: dict[str, float] = {}
    for t in order["payment_history"]:  # mirrors the store: refunds every entry
        refunds[t["payment_method_id"]] = refunds.get(t["payment_method_id"], 0) + t["amount"]
    refund_lines = [f"Refund: {fmt.money(a)} to {_method_text(user, p)}, {_refund_timing(user, p)}."
                    for p, a in refunds.items()]
    items = "; ".join(f"{it['name']} ({fmt.options(it['options'])})" for it in order["items"])
    text = "\n".join([f"Cancel order {order['order_id']}: {items}.", f"Reason: {c['reason']}.",
                      *refund_lines, WARN_CANCEL, ASK])
    return StoreCall("cancel_pending_order", {"order_id": order["order_id"], "reason": c["reason"]}), text


def check_order_address(c, user, order, products):
    _need_status(order, "pending", "have its address changed")
    new, old = c["address"], order["address"]
    if all(str(old.get(f) or "") == new[f] for f in ADDRESS_FIELDS):
        raise ToolError("That is already the shipping address of this order.", fact=True)
    text = "\n".join([f"Change the shipping address of order {order['order_id']}.",
                      f"From: {fmt.address(old)}", f"To: {fmt.address(new)}", ASK])
    return StoreCall("modify_pending_order_address", {"order_id": order["order_id"], **_addr_args(new)}), text


def check_order_payment(c, user, order, products):
    _need_status(order, "pending", "have its payment method changed")
    hist = order["payment_history"]
    if len(hist) != 1 or hist[0]["transaction_type"] != "payment":
        raise ToolError(f"Order {order['order_id']} has more than one payment entry, so its payment "
                        f"method can't be changed.", fact=True)
    old, new, amount = hist[0]["payment_method_id"], c["payment_method_id"], hist[0]["amount"]
    if new == old:
        raise ToolError(f"Order {order['order_id']} is already paid with {_method_text(user, old)}.", fact=True)
    _gift_card_covers(user, new, amount, "the order total")
    text = "\n".join([
        f"Pay for order {order['order_id']} ({fmt.money(amount)}) with {_method_text(user, new)} "
        f"instead of {_method_text(user, old)}.",
        f"{fmt.money(amount)} will be refunded to {_method_text(user, old)}, {_refund_timing(user, old)}.",
        *_alternatives(user, new, lambda m: m["id"] != old and (m["source"] != "gift_card"
                                                                or m["balance"] >= amount),
                       lead="You can also pay with"),
        ASK])
    return StoreCall("modify_pending_order_payment",
                     {"order_id": order["order_id"], "payment_method_id": new}), text


def check_order_items(c, user, order, products):
    _need_status(order, "pending", "have its items changed")
    lines, diff = _replacements(order, c["items"], products)
    pid = c["payment_method_id"]
    if diff > 0:
        _gift_card_covers(user, pid, diff, "the price difference")
    text = "\n".join([f"Change items in order {order['order_id']}:", *lines,
                      _money_line(diff, user, pid), *_difference_alternatives(user, pid, diff),
                      WARN_ITEMS, ASK])
    return StoreCall("modify_pending_order_items", {
        "order_id": order["order_id"], "item_ids": [o for o, _ in c["items"]],
        "new_item_ids": [n for _, n in c["items"]], "payment_method_id": pid}), text


def check_return(c, user, order, products):
    _need_status(order, "delivered", "be returned")
    _items_in_order(order, c["item_ids"])
    pid, original = c["payment_method_id"], order["payment_history"][0]["payment_method_id"]
    if pid != original and _method(user, pid)["source"] != "gift_card":
        raise ToolError(f"A refund can only go to the original payment method "
                        f"({fmt.payment_name(_method(user, original))}, payment ID: {original}) "
                        f"or to a gift card. Ask the customer which one.", fact=True)
    total = 0.0
    lines = []
    for i in c["item_ids"]:
        it = _order_item(order, i)
        total += it["price"]
        lines.append(f"- {it['name']} ({fmt.options(it['options'])}): {fmt.money(it['price'])}")
    text = "\n".join([f"Return from order {order['order_id']}:", *lines,
                      f"Refund: {fmt.money(total)} to {_method_text(user, pid)}.",
                      *_alternatives(user, pid, lambda m: m["id"] == original or m["source"] == "gift_card"),
                      "You'll get an email on how to send the items back.",
                      WARN_DELIVERED.format(verb="return"), ASK])
    return StoreCall("return_delivered_order_items", {
        "order_id": order["order_id"], "item_ids": c["item_ids"], "payment_method_id": pid}), text


def check_exchange(c, user, order, products):
    _need_status(order, "delivered", "be exchanged")
    lines, diff = _replacements(order, c["items"], products)
    pid = c["payment_method_id"]
    if diff > 0:
        _gift_card_covers(user, pid, diff, "the price difference")
    text = "\n".join([f"Exchange in order {order['order_id']}:", *lines, _money_line(diff, user, pid),
                      *_difference_alternatives(user, pid, diff),
                      "You'll get an email on how to send the items back. No new order is needed.",
                      WARN_DELIVERED.format(verb="exchange"), ASK])
    return StoreCall("exchange_delivered_order_items", {
        "order_id": order["order_id"], "item_ids": [o for o, _ in c["items"]],
        "new_item_ids": [n for _, n in c["items"]], "payment_method_id": pid}), text


def check_default_address(c, user, order, products):
    new, old = c["address"], user["address"]
    if all(str(old.get(f) or "") == new[f] for f in ADDRESS_FIELDS):
        raise ToolError("That is already the customer's default address.", fact=True)
    text = "\n".join(["Change your default address.", f"From: {fmt.address(old)}", f"To: {fmt.address(new)}",
                      "This doesn't change the shipping address of existing orders.", ASK])
    return StoreCall("modify_user_address", {"user_id": user["user_id"], **_addr_args(new)}), text


CHECKERS = {
    "cancel_order": check_cancel,
    "change_order_address": check_order_address,
    "change_order_payment": check_order_payment,
    "change_order_items": check_order_items,
    "return_items": check_return,
    "exchange_items": check_exchange,
    "change_default_address": check_default_address,
}
