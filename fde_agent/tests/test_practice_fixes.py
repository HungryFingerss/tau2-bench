"""Fixes from the practice run of 7 Nov (10 tasks). No model: the moments are replayed.

1. The summary says plainly that nothing has happened yet (test_prepare_change.py).
2. No new prepare_change while a clear yes to the shown change is waiting (tasks 1, 11).
3. A reminder to the model when the reply to a summary is not a clear yes (task 2).
"""
from fde_agent.driver import WAITING_NOTE, Decision, Say
from fde_agent.store import run_now
from fde_agent.tests.test_confirm_change import cancel_tshirt, env, read, writes, yusuf  # noqa: F401
from fde_agent.tests.test_driver import until_say
from fde_agent.tests.test_yes_check import FakeChecker, driver_at_summary
from fde_agent.tools.confirm_change import confirm_change
from fde_agent.tools.prepare_change import prepare_change

TASK_11_YES = "Yes. Now do the other one."
TASK_2_REPLY = ("I don't have anything else to add. But I still need to know how many t-shirt "
                "options you have in the store right now.")
RETURN_KEYBOARD = dict(kind="return_items", order_id="#W2378156", item_ids=["item_1151293680"],
                       payment_method_id="credit_card_9513926")


# ---------- fix 2: a waiting yes is confirmed before anything new is prepared ----------

def test_prepare_refused_while_a_yes_is_waiting(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said(TASK_11_YES)
    out = run_now(prepare_change(yusuf, **RETURN_KEYBOARD), env)
    assert not out.ok and out.calls == []             # refused before any store call
    assert 'said yes to change #1 ("Yes. Now do the other one.")' in out.text
    assert "confirm_change(change_number=1)" in out.text
    assert yusuf.prepared.number == 1                 # change #1 is NOT thrown away


def test_the_same_change_again_is_refused_too(yusuf, env):    # task 1
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("Yeah, that sounds right. Let's go ahead with that.")
    out = run_now(prepare_change(yusuf, kind="cancel_order", order_id="#W6247578",
                                 reason="no longer needed"), env)
    assert not out.ok and yusuf.prepared.number == 1


def test_a_changed_request_can_still_be_prepared(yusuf, env):  # tasks 6 and 7 did this right
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("Actually, let's not cancel. Return the keyboard instead.")
    out = run_now(prepare_change(yusuf, **RETURN_KEYBOARD), env)
    assert out.ok and yusuf.prepared.number == 2


def test_no_block_before_the_customer_answers(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)                               # shown, no reply yet
    assert run_now(prepare_change(yusuf, **RETURN_KEYBOARD), env).ok


def test_after_confirming_the_next_change_is_fine(yusuf, env):  # task 11, done right
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said(TASK_11_YES)
    assert run_now(confirm_change(yusuf, change_number=n), env).ok
    out = run_now(prepare_change(yusuf, **RETURN_KEYBOARD), env)
    assert out.ok and yusuf.prepared.number == 2
    assert read(env, "get_order_details", order_id="#W6247578")["status"] == "cancelled"


def test_driver_task_11_moment(env):
    """The model makes task 11's mistake; the refusal steers it back to confirm."""
    d, _ = driver_at_summary(
        env, FakeChecker("YES"),
        Decision.call("prepare_change", **RETURN_KEYBOARD),      # the mistake
        Decision.call("confirm_change", change_number=1))          # after the refusal
    said, rounds = until_say(d, env, d.on_customer(TASK_11_YES))
    assert rounds == [["cancel_pending_order"]]       # the refused call cost no store call
    assert said.text.startswith("Done. This change is made:")
    [refused] = [e for e in d.log if e["kind"] == "tool_result" and e["tool"] == "prepare_change"
                 and not e["ok"]]
    assert "Call confirm_change(change_number=1) now" in refused["text"]   # the driver's F20 guard
    assert [e["tried"] for e in d.log if e["kind"] == "yes_waiting"] == ["prepare_change"]


# ---------- fix 3: a reminder when the reply is not a clear yes ----------

def test_side_question_gets_the_reminder(env):        # task 2
    d, _ = driver_at_summary(env, FakeChecker("NO"),
                             Decision.reply("We have 10 T-shirt options in stock. Shall I go "
                                            "ahead with the cancellation?"))
    said, _ = until_say(d, env, d.on_customer(TASK_2_REPLY))
    assert isinstance(said, Say)
    seen = d.model.seen[-1]                            # what the model saw for that reply
    assert seen[-2] == {"role": "customer", "text": TASK_2_REPLY}
    assert seen[-1] == {"role": "note", "text": WAITING_NOTE.format(n=1)}
    assert "confirm_change(change_number=1)" in seen[-1]["text"]   # F19 may still say yes
    assert "ask again whether to go ahead with change #1" in seen[-1]["text"]


def test_no_reminder_for_a_clear_yes(env):
    d, _ = driver_at_summary(env, FakeChecker("YES"), Decision.call("confirm_change", change_number=1),
                             Decision.reply("Done."))
    until_say(d, env, d.on_customer("Yes, go ahead."))
    assert not [e for e in d.log if e["kind"] == "note" and "still waiting" in e["text"]]


def test_no_reminder_before_a_summary(env):
    from fde_agent.driver import Driver
    from fde_agent.tests.test_driver import ScriptedModel
    d = Driver(ScriptedModel(Decision.reply("Hi! Could you give me your name and zip code?")))
    until_say(d, env, d.on_customer("Hmm, I want to cancel something."))
    assert not [e for e in d.log if e["kind"] == "note"]
