"""The instructions and the rulebook coverage table. No model."""
import re
from pathlib import Path

from fde_agent.policy_coverage import ROWS
from fde_agent.prompt import load_instructions
from fde_agent.toolset import TOOLS

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "data" / "tau2" / "domains" / "retail" / "policy.md"
# These sections only DESCRIBE the data; they hold no rules (except the ID note).
DESCRIPTION_SECTIONS = {"Domain basic", "User", "Product", "Order"}
STORE_TOOLS = ("find_user_id_by_email", "find_user_id_by_name_zip", "get_user_details",
               "get_order_details", "get_product_details", "get_item_details", "list_all_product_types",
               "cancel_pending_order", "modify_pending_order", "return_delivered_order_items",
               "exchange_delivered_order_items", "modify_user_address", "transfer_to_human_agents",
               "calculate")


def rule_sentences():
    """Every sentence of the rulebook's rule sections, plus the ID note."""
    section, out = "", []
    for line in POLICY.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            section = line.strip("# ").strip()
            continue
        line = line.strip().lstrip("-").strip()
        if not line:
            continue
        for s in re.split(r"(?<=[.!])\s+(?=[A-Z])", line):
            if section not in DESCRIPTION_SECTIONS or s.startswith("Note: Product ID"):
                out.append(s)
    return out


def test_every_rule_is_covered():
    quotes = [q for row in ROWS for q in row.quotes]
    uncovered = [s for s in rule_sentences() if not any(q in s for q in quotes)]
    assert uncovered == [], f"rules with no row: {uncovered}"


def test_every_quote_is_really_in_the_rulebook():
    text = " ".join(rule_sentences())
    missing = [q for row in ROWS for q in row.quotes if q not in text]
    assert missing == []


def test_every_pointer_still_exists():
    instructions = load_instructions()
    broken = []
    for row in ROWS:
        assert row.code or row.instructions, f"'{row.rule}' points at nothing"
        for path, text in row.code:
            if text not in (ROOT / path).read_text(encoding="utf-8"):
                broken.append(f"{row.rule}: {path} has no {text!r}")
        for text in row.instructions:
            if text not in instructions:
                broken.append(f"{row.rule}: instructions.md has no {text!r}")
    assert broken == []


def test_instructions_name_all_our_tools_and_no_store_tools():
    text = load_instructions()
    assert [t for t in TOOLS if t not in text] == []
    assert [t for t in STORE_TOOLS if t in text] == []


def test_instructions_stay_short():
    assert len(load_instructions().split()) <= 600


def test_the_client_uses_the_instructions_by_default():
    from fde_agent.model_client import ModelClient
    c = ModelClient("fake", generate_fn=lambda **k: None)
    assert c.system_prompt == load_instructions()
