"""confirm_change: the ONLY tool that writes to the database.

Runs exactly the change prepare_change stored, and only when code can see:
  1. the change exists and is the latest one
  2. code showed its summary to the customer, word for word
  3. the customer replied after that, with a clear yes  (is_clear_yes; when that strict
     rule says no, a second check by a model without the conversation: yes_check.py)
  4. it hasn't been done already  (the baseline sent one write 7 times)

Why: in baseline task 6 the agent called the exchange tool for both items
BEFORE asking the customer anything. Here no write can happen that way.
"""

import json
import re

from fde_agent import format as fmt
from fde_agent.errors import ToolError
from fde_agent.notes import Notes, PreparedChange
from fde_agent.store import StoreCall, ToolSteps
from fde_agent.tools.prepare_change import REASONS, core_lines

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
               "actually", "don't", "dont", "hold", "rather", "however", "though", "although",
               "just"}   # "Just go ahead with the desk lamp" narrows (smoke run task 8, 8 Nov)


def is_clear_yes(text: str | None) -> bool:
    """A yes-word or phrase, and nothing that narrows, delays or questions it.
    Strict on purpose: a wrong 'no' costs one more question; a wrong 'yes' changes the database."""
    if not text:
        return False
    t = text.lower().replace("’", "'")
    if "?" in t:
        return False
    # The cancel reasons themselves aren't a "no": "Yes, it's no longer needed" is a yes.
    # Only the exact phrases are removed; "No.", "no, wait" are still refused.
    for reason in REASONS:
        t = t.replace(reason, " ")
    words = {w.strip("'") for w in re.findall(r"[a-z']+", t)}  # "yes'" -> "yes", keeps "don't"
    if words & BLOCK_WORDS:
        return False
    return bool(words & YES_WORDS) or any(p in t for p in YES_PHRASES)


# A yes that names another payment method is not a clear yes (F12).
# Unseen run, 7 Nov (task 21): "Yeah, that looks right. Go ahead and use my gift card" to a
# summary charging PayPal passed the word rule, and the change was made on PayPal.
PAYMENT_WORDS = ("gift card", "paypal", "visa", "mastercard", "credit card")


def payment_mentions(text: str) -> set[str]:
    """The payment methods a text names: 'gift card', 'paypal', 'visa', 'mastercard',
    'credit card', and card endings like 'ending 2478'."""
    t = (text or "").lower().replace("’", "'")
    for a, b in (("pay pal", "paypal"), ("giftcard", "gift card"), ("master card", "mastercard")):
        t = t.replace(a, b)
    found = {w for w in PAYMENT_WORDS if re.search(rf"\b{w}\b", t)}
    found |= {f"ending {d}" for d in re.findall(r"\b(?:ending|ends)(?: in| with)?\s+(\d{4})\b", t)}
    return found


def names_other_payment(summary: str, reply: str) -> bool:
    """Does the reply name a payment method that the summary doesn't use? The summary's
    'You can also ...' lines are options, not what it uses."""
    used = "\n".join(line for line in summary.splitlines() if not line.startswith("You can also"))
    return bool(payment_mentions(reply) - payment_mentions(used))


# A yes that names part of the change is not a PLAIN yes (F12): "Just go ahead with the desk
# lamp" to a lamp + water bottle summary passed the rule and both were exchanged (smoke run,
# task 8, 8 Nov). The rule only accepts replies that don't pick anything out; the rest goes to
# the second check (F19), which reads the summary and the reply.
ITEM_NAME = re.compile(r"(?:^- |: |; )([A-Z][\w&'\- ]*?)(?: \(|: )", re.MULTILINE)


def item_names(summary: str) -> set[str]:
    """Product names in a summary, and their last word: {'desk lamp', 'lamp', ...}."""
    names = set()
    for line in core_lines(summary):
        for name in ITEM_NAME.findall(line):
            name = name.strip().lower()
            if name and not name.startswith(("order", "refund", "reason", "from", "to")):
                names |= {name, name.split()[-1]}
    return names


def names_part_of_change(summary: str, reply: str) -> bool:
    t = (reply or "").lower()
    return any(re.search(rf"\b{re.escape(n)}s?\b", t) for n in item_names(summary))


def is_clear_yes_to(change: PreparedChange, reply: str | None) -> bool:
    """The yes rule for one shown change: a plain yes. It names no payment method the summary
    doesn't use, and no item of the change."""
    return (is_clear_yes(reply) and not names_other_payment(change.customer_text, reply)
            and not names_part_of_change(change.customer_text, reply))


# ---------- 1. checks (no store call) ----------

def _check(notes: Notes, number, yes_checker=None) -> int:
    if notes.customer is None:
        raise ToolError("Verify the customer first (verify_customer).", fact=False)
    if not isinstance(number, int) or isinstance(number, bool):
        try:
            number = int(str(number).strip().lstrip("#"))
        except ValueError:
            raise ToolError(f"change_number must be a number like 1, not '{number}'.", fact=False)
    if number in notes.done:
        raise ToolError(f"Change #{number} is already done. Don't confirm it again.", fact=False)
    p = notes.prepared
    if p is None:
        raise ToolError(f"There is no prepared change #{number}. Use prepare_change first.", fact=False)
    if number != p.number:
        if number < p.number:
            raise ToolError(f"Change #{number} was replaced by change #{p.number}. "
                            f"Only #{p.number} can be confirmed.", fact=False)
        raise ToolError(f"There is no change #{number}. The prepared change is #{p.number}.", fact=False)
    if notes.shown != p.number:
        raise ToolError(f"The customer hasn't seen change #{p.number} yet.", fact=False)
    if notes.reply is None:
        raise ToolError(f"The customer hasn't answered change #{p.number} yet. Wait for their reply.", fact=False)
    # The code's rule first. Only when it says no, the second check (F19): a model with
    # no conversation reads the summary and the reply. It can only turn a no into a yes.
    # Once the checker has judged this reply, its verdict stands (the driver asks it when the
    # rule and the model disagree).
    if notes.reply_is_yes is not None:
        agreed = notes.reply_is_yes
    else:
        agreed = is_clear_yes_to(p, notes.reply) or (
            yes_checker is not None and yes_checker(p.customer_text, notes.reply) is True)
    if not agreed:
        raise ToolError(
            f"The customer's reply was not a clear yes: \"{notes.reply}\". "
            f"If they want something different, call prepare_change again. If they have a question, "
            f"answer it and ask again whether to go ahead with change #{p.number}.",
            fact=False,
        )
    return number


# ---------- 2. the write ----------

def confirm_change(notes: Notes, change_number=None, yes_checker=None) -> ToolSteps:
    """`yes_checker(summary, reply) -> bool` is given by the driver, never by the model
    (it isn't in PARAMETERS). Without one, only the code's rule decides."""
    notes.check_open()
    _check(notes, change_number, yes_checker)
    change = notes.prepared

    [r] = yield [change.call]   # the one and only write
    if r.error:
        notes.finish_change(confirmed=False)
        raise ToolError(
            f"The store did not make change #{change.number}: {r.content}. "
            f"Check the current state (get_order) and tell the customer.",
            fact=True,
        )
    notes.finish_change(confirmed=True)
    result = json.loads(r.content)
    notes.announce = (change.number, done_text(change, result))   # code tells the customer (F22)
    return shape(change.number, change.call, result, notes)


def done_text(change: PreparedChange, result: dict) -> str:
    """What code tells the customer once the store made the change: the change itself, as
    it was summarised, plus the new state from the store's own answer. The model can't
    misdescribe it (unseen task 21: "charged to your PayPal and covered by your gift card")."""
    lines = ["Done. This change is made:", *core_lines(change.customer_text)]
    if "order_id" in result and "status" in result:
        lines.append(f"Order {result['order_id']} is now: {result['status']}.")
    return "\n".join(lines)


# ---------- 3. shape (the write's own result is the fresh state) ----------

def shape(number: int, call: StoreCall, result: dict, notes: Notes) -> str:
    if call.name == "modify_user_address":
        return (f"Done: change #{number}. The default address is now "
                f"{fmt.address(result['address'])}. Code has told the customer.")
    notes.record_order(result["order_id"], result["status"], result.get("items"))
    return (f"Done: change #{number} on order {result['order_id']}. "
            f"Order status is now '{result['status']}'. Code has told the customer.")
