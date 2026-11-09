"""find_variants: list a product's variants, filtered by what the customer wants.

The model turns words into a request:  "blue t-shirt in large"
    -> find_variants(product_name="t shirt", options={"color": "blue", "size": "large"})
Code does the exact part: resolve the product, check every option name/value
against the real ones, filter, split available / not available, count.

Model gives:   product_id (labelled) OR product_name;  options (optional)
Checks:        customer verified; product ID real and seen  (no store call)
Store calls:   [list_all_product_types if by name] -> get_product_details
Model sees:    counts, all option values, matching variants (available first),
               and when nothing available matches: the closest available ones.
Notes:         product and item IDs shown are remembered as seen.

Why: baseline tasks 2 and 4 counted unavailable T-shirts (said 12, really 10);
task 1 missed that the requested keyboard was out of stock; task 7 picked the
wrong desk lamp.
"""

import json
import re

from fde_agent import format as fmt
from fde_agent.errors import ToolError
from fde_agent.ids import ITEM, PRODUCT, label, unlabel
from fde_agent.notes import Notes
from fde_agent.store import StoreCall, ToolSteps

NAME = "find_variants"

DESCRIPTION = (
    "Find the variants (item options) of a product, e.g. all T-shirts, or only blue ones "
    "in size L. Give product_id (from get_order) or product_name. Optionally give the "
    "options the customer wants, e.g. {\"color\": \"blue\", \"size\": \"L\"}. Shows which "
    "variants are available, their item IDs and prices, and the closest alternatives "
    "when nothing available matches."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "product_id": {"type": "string", "description": "Product ID, e.g. product_9523456873."},
        "product_name": {"type": "string", "description": "Product name, e.g. T-Shirt."},
        "options": {"type": "object", "description": "Option name -> wanted value.",
                    "additionalProperties": {"type": "string"}},
    },
    "required": [],
}


def norm(text: str) -> str:
    """Compare loosely: 'T Shirt' == 't-shirt', 'Full Size' == 'full-size'."""
    return re.sub(r"[^a-z0-9%]", "", str(text).lower())


# ---------- 1. checks (no store call) ----------

def _check(notes: Notes, product_id: str, product_name: str, options) -> None:
    if notes.customer is None:
        raise ToolError("Verify the customer first (verify_customer).", fact=False)
    if not product_id.strip() and not product_name.strip():
        raise ToolError("Give a product_id (from get_order) or a product_name.", fact=False)
    if options is not None and not isinstance(options, dict):
        raise ToolError('options must be an object of option name -> value, e.g. {"color": "blue"}.', fact=False)


# ---------- 2. fetch ----------

def find_variants(notes: Notes, product_id: str = "", product_name: str = "",
                  options: dict | None = None) -> ToolSteps:
    notes.check_open()
    product_id, product_name = (product_id or "").strip(), (product_name or "").strip()
    _check(notes, product_id, product_name, options)
    wanted = {k: str(v) for k, v in (options or {}).items() if str(v).strip()}

    if product_id:
        raw_id = unlabel(PRODUCT, product_id, notes.seen)  # IdError -> no store call
    else:
        [types] = yield [StoreCall("list_all_product_types", {})]
        if types.error:
            raise ToolError(f"Could not list products ({types.content}).", fact=False)
        raw_id = _product_by_name(product_name, json.loads(types.content))

    [r] = yield [StoreCall("get_product_details", {"product_id": raw_id})]
    if r.error:
        raise ToolError(f"Could not load product {label(PRODUCT, raw_id)} ({r.content}).", fact=False)
    product = json.loads(r.content)
    variants = list(product["variants"].values())

    wanted = resolve_options(product["name"], variants, wanted)  # ToolError if unknown
    result = match(variants, wanted)

    notes.seen.remember(PRODUCT, raw_id)
    for v in result["available"] + result["unavailable"] + [v for v, _ in result["closest"]]:
        notes.seen.remember(ITEM, v["item_id"])

    return shape(product, variants, wanted, result)


def _product_by_name(name: str, types: dict[str, str]) -> str:
    for real_name, pid in types.items():
        if norm(real_name) == norm(name):
            return pid
    raise ToolError(f"No product called '{name}'. Products are: {', '.join(sorted(types))}.", fact=True)


# ---------- 3a. matching (pure) ----------

def option_values(variants: list[dict]) -> dict[str, list[str]]:
    """{'color': ['black', 'blue', ...], 'size': [...]}  (real names and values)"""
    values: dict[str, set[str]] = {}
    for v in variants:
        for name, value in v["options"].items():
            values.setdefault(name, set()).add(str(value))
    return {name: sorted(vals) for name, vals in values.items()}


def resolve_options(product_name: str, variants: list[dict], wanted: dict[str, str]) -> dict[str, str]:
    """Map the model's words to the real option names/values, or explain what exists.
    {'COLOR': 'Blue'} -> {'color': 'blue'};  {'size': 'large'} -> ToolError listing sizes."""
    real = option_values(variants)
    resolved = {}
    for name, value in wanted.items():
        real_name = next((n for n in real if norm(n) == norm(name)), None)
        if real_name is None:
            listing = "; ".join(f"{n} ({', '.join(vals)})" for n, vals in real.items())
            raise ToolError(f"{product_name} has no option '{name}'. Its options are: {listing}.", fact=True)
        real_value = next((x for x in real[real_name] if norm(x) == norm(value)), None)
        if real_value is None:
            raise ToolError(
                f"{product_name} has no {real_name} '{value}'. "
                f"Values for {real_name}: {', '.join(real[real_name])}.",
                fact=True,
            )
        resolved[real_name] = real_value
    return resolved


def match(variants: list[dict], wanted: dict[str, str]) -> dict:
    """The filter. Exact, after resolve_options has made the names/values real."""
    def fits(v):
        return all(v["options"].get(n) == val for n, val in wanted.items())

    matching = sorted((v for v in variants if fits(v)), key=lambda v: v["price"])
    available = [v for v in matching if v["available"]]
    unavailable = [v for v in matching if not v["available"]]

    # closest: available variants that differ in exactly ONE wanted option
    closest = []
    if not available and len(wanted) >= 2:
        for v in sorted(variants, key=lambda v: v["price"]):
            differs = [n for n, val in wanted.items() if v["options"].get(n) != val]
            if v["available"] and len(differs) == 1:
                closest.append((v, differs[0]))
    return {"available": available, "unavailable": unavailable, "closest": closest}


# ---------- 3b. shape (pure) ----------

def shape(product: dict, variants: list[dict], wanted: dict[str, str], result: dict) -> str:
    n_avail = sum(v["available"] for v in variants)

    def line(v, note=""):
        return (f"- {label(ITEM, v['item_id'])} | {fmt.money(v['price'])} | "
                f"{fmt.options(v['options'])}{note}")

    lines = [
        f"{product['name']} ({label(PRODUCT, product['product_id'])}): {len(variants)} variants "
        f"in total, {n_avail} available, {len(variants) - n_avail} not available.",
        "All option values: " + " | ".join(
            f"{n}: {', '.join(vals)}" for n, vals in option_values(variants).items()),
    ]
    avail, unavail, closest = result["available"], result["unavailable"], result["closest"]
    if wanted:
        wanted_text = ", ".join(f"{n}: {v}" for n, v in wanted.items())
        if not avail and not unavail:
            lines.append(f"No variant has {wanted_text}.")
        else:
            lines.append(f"Matching {wanted_text} -> {len(avail)} available, {len(unavail)} not available.")
        if unavail and not avail:  # it exists, but can't be ordered
            lines.append(f"No AVAILABLE variant has {wanted_text}.")
    if avail:
        lines += ["Available:", *[line(v) for v in avail]]
    if unavail:
        lines += ["Not available (cannot be ordered):", *[line(v) for v in unavail]]
    if closest:
        lines.append("Closest available (differ in one option):")
        lines += [line(v, f"  <- {n}: {v['options'][n]} instead of {wanted[n]}") for v, n in closest]
    return "\n".join(lines)
