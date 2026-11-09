"""Smoke run, task 8 (8 Nov): "Just go ahead with the desk lamp." to a lamp + water bottle
summary. The yes rule read it as a yes, A then blocked the model's correct question and its
correct lamp-only change, and both items were exchanged.

Part 1 (F12): the rule only accepts PLAIN yeses: "just", or naming an item of the change,
               sends the reply to the yes-checker instead.
Part 2 (F20): when the rule says yes but the model does something else, the checker
               decides; its verdict stands for that reply (confirm and prepare respect it).
"""
from fde_agent.driver import Decision, Driver
from fde_agent.store import run_now
from fde_agent.tests.test_confirm_change import cancel_tshirt, env, read, writes, yusuf  # noqa: F401
from fde_agent.tests.test_driver import YUSUF, ScriptedModel, until_say
from fde_agent.tests.test_yes_check import FakeChecker, driver_at_summary
from fde_agent.tools.confirm_change import confirm_change, is_clear_yes_to, item_names
from fde_agent.tools.prepare_change import ASK, NOT_YET, prepare_change

LAMP_AND_BOTTLE = (f"{NOT_YET}\nExchange in order #W6390527:\n"
                   "- Water Bottle: capacity 500ml -> 750ml ($45.13 -> $50.14)\n"
                   "- Desk Lamp: brightness medium -> high ($137.94 -> $164.28)\n"
                   f"You pay $31.35 more, charged to your PayPal.\n{ASK}")


class Shown:
    customer_text = LAMP_AND_BOTTLE


# ---------- part 1: only plain yeses pass the rule ----------

def test_item_names_come_from_the_summary():
    assert item_names(LAMP_AND_BOTTLE) == {"water bottle", "bottle", "desk lamp", "lamp"}


def test_naming_an_item_or_just_is_not_a_plain_yes():
    for reply in ["Just go ahead with the desk lamp.", "Yes, go ahead with the lamp.",
                  "Yes, just do it.", "Yeah, the bottle and the lamps look right, go ahead."]:
        assert not is_clear_yes_to(Shown, reply), reply
    for reply in ["Yes.", "Yes, go ahead.", "Sounds good, go ahead.", "Yes, both look right. Go ahead."]:
        assert is_clear_yes_to(Shown, reply), reply


# ---------- part 2: the checker decides when the rule and the model disagree ----------

def test_checker_no_lets_the_model_ask(env):
    """The rule says yes, the model wants to ask a question; the checker says it isn't a full
    yes, so the question goes out and nothing is confirmed."""
    checker = FakeChecker("NO")
    d, _ = driver_at_summary(env, checker, Decision.reply("Just to check: cancel the whole order?"))
    said, rounds = until_say(d, env, d.on_customer("Yes, go ahead."))
    assert said.text == "Just to check: cancel the whole order?" and rounds == []
    assert len(checker.asked) == 1 and d.notes.reply_is_yes is False
    assert not [e for e in d.log if e["kind"] == "yes_waiting"]


def test_checker_yes_still_blocks(env):
    """Practice task 11: a real yes, and the model wanders off: still blocked."""
    checker = FakeChecker("YES")
    d, _ = driver_at_summary(env, checker, Decision.call("verify_customer", **YUSUF),
                             Decision.call("confirm_change", change_number=1))
    said, rounds = until_say(d, env, d.on_customer("Yes, go ahead."))
    assert rounds == [["cancel_pending_order"]] and said.text.startswith("Done.")
    assert [e["tried"] for e in d.log if e["kind"] == "yes_waiting"] == ["verify_customer"]
    assert len(checker.asked) == 1                    # asked once, then remembered


def test_the_verdict_stands_for_confirm(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("Yes, go ahead.")
    yusuf.reply_is_yes = False                        # the checker said no for this reply
    out = run_now(confirm_change(yusuf, change_number=n), env)
    assert not out.ok and writes(out) == []


def test_the_verdict_stands_for_prepare(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("Yes, go ahead.")
    yusuf.reply_is_yes = False
    out = run_now(prepare_change(yusuf, kind="return_items", order_id="#W2378156",
                                 item_ids=["item_1151293680"], payment_method_id="credit_card_9513926"), env)
    assert out.ok and yusuf.prepared.number == 2      # the model's new change is allowed


def test_a_new_reply_is_judged_again(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("Yes, go ahead.")
    yusuf.reply_is_yes = False
    yusuf.customer_said("Yes.")
    assert yusuf.reply_is_yes is None


# ---------- the whole task 8 moment, through the driver ----------

def test_task_8_moment(env):
    """Two items prepared; "Just go ahead with the keyboard." -> not a plain yes -> the model
    (reminded by F21) prepares the keyboard only -> "Yes." -> only the keyboard is exchanged."""
    keyboard = {"item_id": "item_1151293680", "new_item_id": "item_6342039236"}
    thermostat = {"item_id": "item_4983901480", "new_item_id": "item_7747408585"}
    pay = dict(kind="exchange_items", order_id="#W2378156", payment_method_id="credit_card_9513926")
    model = ScriptedModel(
        Decision.call("verify_customer", **YUSUF),
        Decision.call("get_order", order_id="#W2378156"),              # shows the item IDs
        Decision.call("find_replacement", order_id="#W2378156", item_id="item_1151293680",
                      changes={"switch type": "clicky"}),
        Decision.call("find_replacement", order_id="#W2378156", item_id="item_4983901480",
                      changes={"compatibility": "Google Assistant"}),
        Decision.call("prepare_change", **pay, items=[keyboard, thermostat]),
        Decision.call("prepare_change", **pay, items=[keyboard]),      # after "Just ... keyboard"
        Decision.call("confirm_change", change_number=2))
    checker = FakeChecker("NO")
    d = Driver(model, yes_checker=checker)
    said, _ = until_say(d, env, d.on_customer("I'm Yusuf Rossi, zip 19122. Exchange my keyboard "
                                              "and thermostat in #W2378156."))
    assert "- Smart Thermostat" in said.text                         # both items shown
    said, rounds = until_say(d, env, d.on_customer("Just go ahead with the keyboard."))
    assert "- Smart Thermostat" not in said.text and "- Mechanical Keyboard" in said.text
    assert not [e for e in d.log if e["kind"] == "yes_waiting"]      # nothing forced
    assert any("still waiting" in e["text"] for e in d.log if e["kind"] == "note")   # F21
    said, rounds = until_say(d, env, d.on_customer("Yes."))
    assert rounds == [["exchange_delivered_order_items"]]
    order = read(env, "get_order_details", order_id="#W2378156")
    assert order["exchange_items"] == ["1151293680"]                 # the keyboard only
