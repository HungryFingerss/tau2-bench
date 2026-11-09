"""Piece 4: the moments where code speaks or listens, not the model.
Scripted model + the REAL store, as in test_driver.py."""
import json

import pytest

from fde_agent.driver import Decision, Driver, Say
from fde_agent.tests.test_driver import YUSUF, ScriptedModel, run_store, until_say
from fde_agent.tools.transfer_to_human import TRANSFER_MESSAGE

HELLO = "Hi, I'm Yusuf Rossi, 19122. Please cancel order #W6247578, I don't need it anymore."
VERIFY = Decision.call("verify_customer", **YUSUF)
PREPARE = Decision.call("prepare_change", kind="cancel_order", order_id="#W6247578", reason="no longer needed")


@pytest.fixture
def env():
    from tau2.domains.retail.environment import get_environment
    return get_environment()


def status(env, order_id):
    from tau2.data_model.message import ToolCall
    r = env.get_response(ToolCall(id="x", name="get_order_details", arguments={"order_id": order_id}))
    return json.loads(r.content)["status"]


def writes(driver):
    return [c["name"] for e in driver.log if e["kind"] == "store_calls" for c in e["calls"].values()
            if c["name"] in ("cancel_pending_order", "modify_pending_order_address")]


# 1 + 2. a whole cancel conversation; after prepare, the model isn't asked until the customer answers
def test_cancel_from_start_to_finish(env):
    model = ScriptedModel(VERIFY, PREPARE, Decision.call("confirm_change", change_number=1))
    d = Driver(model)

    out, _ = until_say(d, env, d.on_customer(HELLO))
    assert out == Say(d.notes.prepared.customer_text)          # word for word, sent by code
    assert out.text.startswith("Here's the change I'll make. Nothing has changed yet.\nCancel order #W6247578")
    assert len(model.seen) == 2                                  # verify + prepare; no 3rd question
    assert d.notes.shown == 1 and d.notes.reply is None
    last_said = [e for e in d.log if e["kind"] == "agent"][-1]
    assert last_said["by"] == "code"
    assert d.log[-1]["kind"] == "note"                           # the model is told it was shown
    assert d.transcript[-1]["role"] == "note" and "was shown to the customer" in d.transcript[-1]["text"]

    out, _ = until_say(d, env, d.on_customer("Yes."))
    assert d.notes.reply is None                                 # used up by the confirmed change
    assert out.text.startswith("Done. This change is made:\nCancel order #W6247578")   # by code (F22)
    assert out.text.endswith("Order #W6247578 is now: cancelled.")
    assert len(model.seen) == 3                     # after confirm_change the model isn't asked
    assert d.transcript[-1]["role"] == "note" and "change #1 is done" in d.transcript[-1]["text"]
    assert status(env, "#W6247578") == "cancelled"
    assert writes(d) == ["cancel_pending_order"]


# 3. a narrowed yes -> confirm_change refuses, nothing written
def test_narrowed_yes_writes_nothing(env):
    model = ScriptedModel(VERIFY, PREPARE, Decision.call("confirm_change", change_number=1),
                          Decision.reply("Sorry, which item did you mean?"))
    d = Driver(model)
    until_say(d, env, d.on_customer(HELLO))
    out, _ = until_say(d, env, d.on_customer("Yes, but only the lamp"))
    assert out == Say("Sorry, which item did you mean?")
    refused = [e for e in d.transcript if e["role"] == "tool_result"][-1]
    assert not refused["ok"] and "not a clear yes" in refused["text"]
    assert writes(d) == [] and status(env, "#W6247578") == "pending"


# 4. prepare_change fails -> no summary; the model gets the error and answers itself
def test_failed_prepare_sends_no_summary(env):
    bad = Decision.call("prepare_change", kind="cancel_order", order_id="#W6247578", reason="changed my mind")
    model = ScriptedModel(VERIFY, bad, Decision.reply("Was it no longer needed, or ordered by mistake?"))
    d = Driver(model)
    out, _ = until_say(d, env, d.on_customer(HELLO))
    assert out == Say("Was it no longer needed, or ordered by mistake?")
    assert d.notes.shown is None and d.notes.prepared is None


# 5. a "yes" before any summary doesn't count
def test_early_yes_does_not_count(env):
    model = ScriptedModel(Decision.reply("Hi! Could you tell me your name and zip code?"), VERIFY, PREPARE)
    d = Driver(model)
    d.on_customer("Yes, go ahead and cancel it.")
    assert d.notes.reply is None
    until_say(d, env, d.on_customer(HELLO))
    assert d.notes.shown == 1 and d.notes.reply is None      # the earlier "yes" isn't the answer


# 6. preparing again -> summary #2, and the next reply counts for #2 only
def test_second_prepare_gets_its_own_summary(env):
    prepare2 = Decision.call("prepare_change", kind="cancel_order", order_id="#W6247578",
                             reason="ordered by mistake")
    model = ScriptedModel(VERIFY, PREPARE, prepare2)
    d = Driver(model)
    until_say(d, env, d.on_customer(HELLO))
    out, _ = until_say(d, env, d.on_customer("Actually, it was ordered by mistake."))
    assert "Reason: ordered by mistake." in out.text
    assert d.notes.shown == 2 and d.notes.reply is None
    d.notes.customer_said("Yes.")
    assert d.notes.reply == "Yes."


# 7. transfer -> the exact sentence, sent by code
def test_transfer_sentence(env):
    model = ScriptedModel(Decision.call("transfer_to_human", summary="Wants to undo a cancellation."))
    d = Driver(model)
    out, _ = until_say(d, env, d.on_customer("I want to undo a cancellation. Get me a human."))
    assert out == Say(TRANSFER_MESSAGE)
    assert len(model.seen) == 1 and d.log[-1]["by"] == "code"


# 8. a reply with an internal ID is never sent; the model rewrites it
def test_reply_with_internal_id_is_rewritten(env):
    model = ScriptedModel(VERIFY, Decision.call("get_order", order_id="#W2378156"),
                          Decision.reply("Your keyboard is item_1151293680, paid with credit_card_9513926."),
                          Decision.reply("Your linear RGB keyboard was paid with your Mastercard ending 2478."))
    d = Driver(model)
    out, _ = until_say(d, env, d.on_customer("Yusuf Rossi 19122, what keyboard did I buy?"))
    assert out == Say("Your linear RGB keyboard was paid with your Mastercard ending 2478.")
    blocked = [e for e in d.log if e["kind"] == "blocked_reply"]
    assert blocked and blocked[0]["ids"] == ["item_1151293680", "credit_card_9513926"]
    assert "Your reply was not sent" in model.seen[-1][-1]["text"]
    sent = [e["text"] for e in d.transcript if e["role"] == "agent"]
    assert all("item_" not in t for t in sent)               # the bad reply never became "said"


# 9. bold markers are removed, and logged
def test_bold_is_removed(env):
    model = ScriptedModel(Decision.reply("Option 1: **80% keyboard**, you get **$34.85** back."))
    d = Driver(model)
    assert d.on_customer("which keyboards?") == Say("Option 1: 80% keyboard, you get $34.85 back.")
    assert any(e["kind"] == "fixed" and "bold" in e["what"] for e in d.log)


def test_order_numbers_and_tracking_are_allowed(env):
    text = "Order #W2378156 shipped with tracking number 843053632392."
    d = Driver(ScriptedModel(Decision.reply(text)))
    assert d.on_customer("where is it?") == Say(text)
