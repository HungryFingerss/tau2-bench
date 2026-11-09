"""verify_customer: find the customer, and show the model their account at a glance.

Model gives:   email   OR   first_name + last_name + zip
Store calls:   find_user_id_by_email | find_user_id_by_name_zip
               -> get_user_details
               -> get_order_details for every order (one batch)
Model sees:    name, user ID, how verified, default address, payment methods,
               every order with its status and items.  (Email is left out.)
Notes:         verified customer; every order with its status.

Why every order up front: in the baseline the user record listed order IDs
without status, the model looked at too few orders, and missed one (task 3).
"""

import json

from fde_agent import format as fmt
from fde_agent.errors import ToolError
from fde_agent.notes import Notes
from fde_agent.store import StoreCall, ToolSteps

NAME = "verify_customer"

DESCRIPTION = (
    "Verify the customer's identity. Do this before anything else. "
    "Give EITHER their email, OR their first name, last name and zip code. "
    "Returns their account: default address, payment methods, and all orders "
    "with status and items."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "email": {"type": "string", "description": "Customer's email."},
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "zip": {"type": "string", "description": "Zip code of the customer's address."},
    },
    "required": [],
}


# ---------- 1. check inputs (no store call) ----------

def _check_inputs(email: str, first_name: str, last_name: str, zip_code: str) -> str:
    """Return "email" or "name_zip". Raise ToolError listing what is missing."""
    if email:
        return "email"
    name_fields = {"first name": first_name, "last name": last_name, "zip code": zip_code}
    missing = [label for label, value in name_fields.items() if not value]
    if not missing:
        return "name_zip"
    if len(missing) == 3:
        raise ToolError(
            "Need the customer's email, or their first name, last name and zip code. "
            "Ask the customer for one of these.",
            fact=False,
        )
    raise ToolError(
        f"Missing: {', '.join(missing)}. To verify by name, all three are needed "
        f"(first name, last name, zip code). Or use the customer's email.",
        fact=False,
    )


# ---------- 2. fetch (store calls) ----------

def verify_customer(notes: Notes, email: str = "", first_name: str = "",
                    last_name: str = "", zip: str = "") -> ToolSteps:
    notes.check_open()
    email, first_name, last_name, zip_code = (
        (v or "").strip() for v in (email, first_name, last_name, zip)
    )
    method = _check_inputs(email, first_name, last_name, zip_code)

    # 2a. who is it?
    if method == "email":
        [found] = yield [StoreCall("find_user_id_by_email", {"email": email})]
    else:
        [found] = yield [StoreCall("find_user_id_by_name_zip", {
            "first_name": first_name, "last_name": last_name, "zip": zip_code})]
    if found.error:
        raise ToolError(_lookup_error(found.content, method), fact=False)
    user_id = found.content.strip()

    # 2b. one customer per conversation (checked before reading their account)
    notes.check_same_customer(user_id)

    # 2b'. the same customer again, nothing changed since (F25): no need to reload
    if notes.verified and notes.verified[0] == user_id and notes.verified[2] == len(notes.done):
        return notes.verified[1] + "\n(Already verified in this conversation; nothing changed since.)"

    # 2c. their account
    [details] = yield [StoreCall("get_user_details", {"user_id": user_id})]
    if details.error:
        raise ToolError(f"Could not load the customer's account ({details.content}).", fact=False)
    user = json.loads(details.content)

    # 2d. every order, in one batch
    order_results = yield [StoreCall("get_order_details", {"order_id": oid})
                           for oid in user["orders"]]
    orders = []
    for i, r in enumerate(order_results):  # (not zip(): `zip` is a parameter here)
        oid = user["orders"][i]
        if r.error:
            raise ToolError(f"Could not load order {oid} ({r.content}).", fact=False)
        orders.append(json.loads(r.content))

    # 2e. write the notes
    name = f"{user['name']['first_name']} {user['name']['last_name']}"
    notes.set_customer(user_id, name)
    notes.payment_methods = {p["id"]: fmt.payment_name(p) for p in user["payment_methods"].values()}
    for o in orders:
        notes.record_order(o["order_id"], o["status"], o["items"])

    text = shape(user, orders, method)
    notes.verified = (user_id, text, len(notes.done))
    return text


def _lookup_error(content: str, method: str) -> str:
    if "ambiguous" in content.lower():
        return ("More than one customer matches that name and zip code. "
                "Ask the customer for their email instead.")
    if method == "email":
        return ("No customer found with that email. Ask the customer to check it, "
                "or verify with first name, last name and zip code instead.")
    return ("No customer found with that name and zip code. Ask the customer to "
            "check the spelling and zip code, or verify with their email instead.")


# ---------- 3. shape (pure: data -> text the model sees) ----------

def shape(user: dict, orders: list[dict], method: str) -> str:
    name = f"{user['name']['first_name']} {user['name']['last_name']}"
    how = "email" if method == "email" else "name and zip code"
    lines = [
        f"Verified: {name} (user ID: {user['user_id']}), by {how}.",
        f"Default address: {fmt.address(user['address'])}",
        "Payment methods:",
        *[f"- {_payment(p)}" for p in user["payment_methods"].values()],
        f"Orders ({len(orders)}):",
        *[f"- {o['order_id']} | {o['status']} | {_items(o['items'])}" for o in orders],
        "For an order's full details (item IDs, prices, address, payment), use get_order.",
    ]
    return "\n".join(lines)


def _payment(p: dict) -> str:
    balance = f", balance {fmt.money(p['balance'])}" if p["source"] == "gift_card" else ""
    return f"{fmt.payment_name(p)}{balance} (payment ID: {p['id']})"


def _items(items: list[dict]) -> str:
    """'T-Shirt (color: black, size: XXL, ...); Headphones (...) x2'"""
    return "; ".join(
        f"{it['name']} ({fmt.options(it['options'])})" + (f" x{n}" if n > 1 else "")
        for it, n in fmt.count_items(items)
    )
