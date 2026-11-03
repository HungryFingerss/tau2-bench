"""Small pure functions that turn store data into readable text. Shared by tools."""


def address(a: dict) -> str:
    street = ", ".join(p for p in (a["address1"], a["address2"]) if p)
    return f"{street}, {a['city']}, {a['state']} {a['zip']}, {a['country']}"


def payment_name(p: dict) -> str:
    """'Mastercard credit card ending 2478' / 'Gift card' / 'PayPal'."""
    if p["source"] == "credit_card":
        return f"{p['brand'].title()} credit card ending {p['last_four']}"
    if p["source"] == "gift_card":
        return "Gift card"
    if p["source"] == "paypal":
        return "PayPal"
    return p["source"]


def options(opts: dict) -> str:
    """Option names are kept: 'black, green' alone doesn't say frame vs lens."""
    return ", ".join(f"{k}: {v}" for k, v in opts.items())


def money(x: float) -> str:
    return f"${x:,.2f}"


def count_items(items: list[dict]) -> list[tuple[dict, int]]:
    """Group repeated items (same item_id) -> [(item, count)], first-seen order."""
    counts: dict[str, int] = {}
    first: dict[str, dict] = {}
    for it in items:
        counts[it["item_id"]] = counts.get(it["item_id"], 0) + 1
        first.setdefault(it["item_id"], it)
    return [(first[i], counts[i]) for i in first]
