"""confirm_change: the ONLY tool that writes to the database.

Runs exactly the change prepare_change stored, and only when code can see:
  1. the change exists and is the latest one
  2. code showed its summary to the customer, word for word
  3. the customer replied after that, with a clear yes  (is_clear_yes)
  4. it hasn't been done already  (the baseline sent one write 7 times)

Why: in baseline task 6 the agent called the exchange tool for both items
BEFORE asking the customer anything. Here no write can happen that way.
"""

import json
import re

from fde_agent import format as fmt
from fde_agent.errors import ToolError
from fde_agent.notes import Notes
from fde_agent.store import StoreCall, ToolSteps

NAME = "confirm_change"

DESCRIPTION = (
    "Make a prepared change, after the customer has clearly said yes to its summary. "
    "Give the change number from prepare_change. If the customer wants anything different, "
    "call prepare_change again instead."
)

PARAMETERS = {
    "type": "object",
    "properties": {"change_number": {"type": "integer", "description": "e.g. 1"}},
    "required": ["change_number"],
}

# ---------- the yes rule (pure) ----------

YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed",
             "correct", "proceed", "absolutely", "definitely"}
YES_PHRASES = ("go ahead", "please do", "do it", "sounds good")
BLOCK_WORDS = {"but", "only", "except", "instead", "wait", "not", "no", "nope", "nah",
               "actually", "don't", "dont", "hold", "rather", "however", "though", "although"}


def is_clear_yes(text: str | None) -> bool:
    """A yes-word or phrase, and nothing that narrows, delays or questions it.
    Strict on purpose: a wrong 'no' costs one more question; a wrong 'yes' changes the database."""
    if not text:
        return False
    t = text.lower().replace("’", "'")
    if "?" in t:
        return False
    words = {w.strip("'") for w in re.findall(r"[a-z']+", t)}  # "yes'" -> "yes", keeps "don't"
    if words & BLOCK_WORDS:
        return False
    return bool(words & YES_WORDS) or any(p in t for p in YES_PHRASES)


# ---------- 1. checks (no store call) ----------

def _check(notes: Notes, number) -> int:
    if notes.customer is None:
        raise ToolError("Verify the customer first (verify_customer).")
    if not isinstance(number, int) or isinstance(number, bool):
        try:
            number = int(str(number).strip().lstrip("#"))
        except ValueError:
            raise ToolError(f"change_number must be a number like 1, not '{number}'.")
    if number in notes.done:
        raise ToolError(f"Change #{number} is already done. Don't confirm it again.")
    p = notes.prepared
    if p is None:
        raise ToolError(f"There is no prepared change #{number}. Use prepare_change first.")
    if number != p.number:
        if number < p.number:
            raise ToolError(f"Change #{number} was replaced by change #{p.number}. "
                            f"Only #{p.number} can be confirmed.")
        raise ToolError(f"There is no change #{number}. The prepared change is #{p.number}.")
    if notes.shown != p.number:
        raise ToolError(f"The customer hasn't seen change #{p.number} yet.")
    if notes.reply is None:
        raise ToolError(f"The customer hasn't answered change #{p.number} yet. Wait for their reply.")
    if not is_clear_yes(notes.reply):
        raise ToolError(
            f"The customer's reply was not a clear yes: \"{notes.reply}\". "
            f"If they want something different, call prepare_change again. If they have a question, "
            f"answer it and ask again whether to go ahead with change #{p.number}."
        )
    return number


# ---------- 2. the write ----------

def confirm_change(notes: Notes, change_number=None) -> ToolSteps:
    notes.check_open()
    _check(notes, change_number)
    change = notes.prepared

    [r] = yield [change.call]   # the one and only write
    if r.error:
        notes.finish_change(confirmed=False)
        raise ToolError(
            f"The store did not make change #{change.number}: {r.content}. "
            f"Check the current state (get_order) and tell the customer."
        )
    notes.finish_change(confirmed=True)
    return shape(change.number, change.call, json.loads(r.content), notes)


# ---------- 3. shape (the write's own result is the fresh state) ----------

def shape(number: int, call: StoreCall, result: dict, notes: Notes) -> str:
    if call.name == "modify_user_address":
        return (f"Done: change #{number}. The default address is now "
                f"{fmt.address(result['address'])}. Tell the customer.")
    notes.record_order(result["order_id"], result["status"])
    return (f"Done: change #{number} on order {result['order_id']}. "
            f"Order status is now '{result['status']}'. Tell the customer it's done.")
