"""Fixes A-H from the practice rerun and the unseen run (8 Nov). No model: replayed.

A (F20)  only confirm_change while a clear yes waits          practice task 11
B (F12)  a yes naming another payment method isn't clear      unseen task 21
C (F22)  code tells the customer a change is done             unseen tasks 21, 16
D (F23)  identity values must come from the customer          practice task 4
E (F24)  the same product in other orders is pointed out      practice task 4
F (F25)  re-verifying the same customer reloads nothing       unseen task 20
G (F26)  the model sees a clean history; facts are kept       unseen tasks 16, 20
H (F27)  an identical successful call is not run again        unseen tasks 16, 20
"""
import ast
from dataclasses import replace
from pathlib import Path

from fde_agent.driver import Decision, Driver, Say, model_view
from fde_agent.store import run_now
from fde_agent.tests.test_confirm_change import cancel_tshirt, env, read, writes, yusuf  # noqa: F401
from fde_agent.tests.test_driver import YUSUF, ScriptedModel, run_store, until_say
from fde_agent.tests.test_yes_check import FakeChecker, driver_at_summary
from fde_agent.tools.confirm_change import (confirm_change, is_clear_yes, is_clear_yes_to,
                                            names_other_payment)
from fde_agent.tools.find_replacement import find_replacement
from fde_agent.tools.prepare_change import ASK, NOT_YET, core_lines, prepare_change
from fde_agent.tools.verify_customer import verify_customer

TASK_21_SUMMARY = (
    f"{NOT_YET}\nChange items in order #W9911714:\n"
    "- Running Shoes: color yellow -> black ($147.05 -> $155.33)\n"
    "You pay $41.92 more, charged to your PayPal.\n"
    "You can also choose: your Gift card (balance $86.00).\n"
    f"{ASK}")
TASK_21_REPLY = ("Yeah, that looks right. Go ahead and use my gift card to cover those extra charges. "
                 "And just let me know what my balance will be once everything is finished up.")


def kinds(d, kind):
    return [e for e in d.log if e["kind"] == kind]


# ---------- A: only confirm_change while a clear yes waits ----------

def test_a_false_done_is_never_sent(env):
    """Practice task 11: after "Yes." the model re-verified and said "Your return has been
    requested". Now neither runs nor reaches the customer until the change is confirmed."""
    d, _ = driver_at_summary(
        env, FakeChecker("YES"),
        Decision.call("verify_customer", **YUSUF),               # the task 11 mistake
        Decision.reply("Your cancellation has been requested."),  # the false "done"
        Decision.call("confirm_change", change_number=1))
    said, rounds = until_say(d, env, d.on_customer("Yes."))
    assert rounds == [["cancel_pending_order"]]                    # nothing else reached the store
    assert said.text.startswith("Done. This change is made:")      # written by code, after the write
    assert [e["tried"] for e in kinds(d, "yes_waiting")] == ["verify_customer", "reply"]
    sent = [e["text"] for e in kinds(d, "agent")]
    assert not any("has been requested" in t for t in sent)        # the false claim never went out


def test_a_no_guard_without_a_clear_yes(env):
    d, _ = driver_at_summary(env, FakeChecker("NO"), Decision.reply("Of course, take your time."))
    said, _ = until_say(d, env, d.on_customer("Hmm, let me think about it."))
    assert said.text == "Of course, take your time." and not kinds(d, "yes_waiting")


# ---------- B: a yes naming another payment method is not a clear yes ----------

def test_b_task_21_reply_is_not_a_clear_yes():
    first = "Yeah, that looks right. Go ahead and use my gift card to cover those extra charges."
    assert is_clear_yes(first)                               # the word rule alone says yes...
    assert names_other_payment(TASK_21_SUMMARY, first)       # ...but it names the gift card
    assert not is_clear_yes(TASK_21_REPLY)                   # (the full reply also has "just")


def test_b_payment_names():
    other = lambda reply: names_other_payment(TASK_21_SUMMARY, reply)
    assert not other("Yes, go ahead.")
    assert not other("Yes, charge my PayPal.")               # the summary's own method is fine
    assert other("Yes, put it on my giftcard")
    assert other("yes, use my visa ending in 7285")
    card = TASK_21_SUMMARY.replace("your PayPal", "your Mastercard credit card ending 2478")
    assert not names_other_payment(card, "Yes, use my Mastercard ending 2478.")
    assert names_other_payment(card, "Yes, but my other card, ending 7285.")


def test_b_confirm_asks_the_checker_and_refuses(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("Yes, go ahead, and refund it to my gift card.")
    assert not is_clear_yes_to(yusuf.prepared, yusuf.reply)
    asked = []
    out = run_now(confirm_change(yusuf, change_number=n,
                                 yes_checker=lambda s, r: asked.append(r) or False), env)
    assert not out.ok and writes(out) == [] and len(asked) == 1   # went to the second check


def test_b_reminder_for_a_yes_with_another_payment(env):
    d, _ = driver_at_summary(env, FakeChecker("NO"), Decision.reply("Sure: shall I refund it to "
                                                                    "your gift card instead?"))
    until_say(d, env, d.on_customer("Yes, go ahead, and refund it to my gift card."))
    assert any("still waiting" in e["text"] for e in kinds(d, "note"))   # F21 reminder
    assert not kinds(d, "yes_waiting")                                   # not treated as a yes


# ---------- C: code tells the customer a change is done ----------

def test_c_done_text_is_the_change_without_the_asking():
    lines = core_lines(TASK_21_SUMMARY)
    assert lines == ["Change items in order #W9911714:",
                     "- Running Shoes: color yellow -> black ($147.05 -> $155.33)",
                     "You pay $41.92 more, charged to your PayPal."]


def test_c_the_model_cannot_loop_on_confirm(env):
    """Unseen task 16: the model repeated confirm_change 5 times after it worked. Now code
    speaks right after the write and the model isn't asked until the customer answers."""
    d, _ = driver_at_summary(env, FakeChecker("YES"), Decision.call("confirm_change", change_number=1),
                             *[Decision.call("confirm_change", change_number=1)] * 5)
    asked_before = len(d.model.seen)
    said, rounds = until_say(d, env, d.on_customer("Yes, go ahead."))
    assert rounds == [["cancel_pending_order"]]
    assert said.text.endswith("Order #W6247578 is now: cancelled.")
    assert len(d.model.seen) == asked_before + 1                 # asked once: to confirm
    assert d.notes.announce is None


# ---------- D: identity values must come from the customer ----------

def test_d_invented_customer_is_refused(env):
    """Practice task 4: the model verified "John Doe, 90210" before the customer said a name."""
    model = ScriptedModel(Decision.call("verify_customer", first_name="John", last_name="Doe", zip="90210"),
                          Decision.reply("Could you give me your name and zip code?"))
    d = Driver(model)
    said, rounds = until_say(d, env, d.on_customer("Hi, how many t-shirt options do you have?"))
    assert rounds == []                                          # no store call
    assert said.text == "Could you give me your name and zip code?"
    [e] = kinds(d, "ungrounded_identity")
    assert e["values"] == ["this first name 'John'", "this last name 'Doe'", "this zip code '90210'"]


def test_d_what_the_customer_said_is_fine():
    d = Driver(ScriptedModel())
    d.transcript = [{"role": "customer", "text": "I'm  Yusuf ROSSI, zip 19122. Email: "
                                                 "Mia.Garcia2723@example.com"}]
    assert d._ungrounded({"first_name": "yusuf", "last_name": "Rossi", "zip": "19122",
                          "email": "mia.garcia2723@example.com"}) == []
    assert d._ungrounded({"first_name": "Yusuf", "last_name": "Rossi", "zip": "19123"}) == \
        ["this zip code '19123'"]


# ---------- E: the same product in other orders is pointed out (to the model) ----------

def test_e_other_pending_tshirt_is_pointed_out(yusuf, env):
    run_now(find_replacement(yusuf, order_id="#W6247578", item_id="item_3799046073",
                             changes={"size": "XL"}), env)
    out = run_now(prepare_change(yusuf, kind="change_order_items", order_id="#W6247578",
                                 payment_method_id="credit_card_9513926",
                                 items=[{"item_id": "item_3799046073", "new_item_id": "item_2060066974"}]), env)
    assert out.ok
    assert "The same product is in other orders: #W4776164 (pending) also has: T-Shirt." in out.text
    assert "Don't change them without asking." in out.text
    assert "#W4776164" not in yusuf.prepared.customer_text      # the customer's summary is unchanged


def test_e_nothing_when_no_other_order_has_it(yusuf, env):
    out = run_now(prepare_change(yusuf, kind="return_items", order_id="#W2378156",
                                 item_ids=["item_1151293680"], payment_method_id="credit_card_9513926"), env)
    assert out.ok and "other orders" not in out.text


# ---------- F: re-verifying the same customer reloads nothing ----------

def test_f_second_verify_loads_nothing(env):
    from fde_agent.notes import Notes
    n = Notes()
    first = run_now(verify_customer(n, **YUSUF), env)
    again = run_now(verify_customer(n, **YUSUF), env)
    assert len(first.calls) == 7                                 # lookup, account, 5 orders
    assert [c.name for c in again.calls] == ["find_user_id_by_name_zip"]   # only who it is
    assert again.text.startswith(first.text)
    assert again.text.endswith("(Already verified in this conversation; nothing changed since.)")


def test_f_after_a_change_it_reloads(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("Yes.")
    assert run_now(confirm_change(yusuf, change_number=n), env).ok
    again = run_now(verify_customer(yusuf, **YUSUF), env)
    assert len(again.calls) == 7 and "#W6247578 | cancelled" in again.text   # fresh statuses


# ---------- G: the model sees a clean history ----------

def test_g_failures_hidden_after_recovery_facts_kept():
    t = [{"role": "customer", "text": "hi"},
         {"role": "note", "text": "Error: no tool called ''.", "failed": True},
         {"role": "tool_call", "id": "tc-1", "tool": "prepare_change", "args": {}},
         {"role": "tool_result", "id": "tc-1", "tool": "prepare_change", "ok": False, "fact": True,
          "text": "Error: A refund can only go to the original payment method or to a gift card."},
         {"role": "tool_call", "id": "tc-2", "tool": "get_order", "args": {}},
         {"role": "tool_result", "id": "tc-2", "tool": "get_order", "ok": False, "fact": False,
          "text": "Error: Verify the customer first (verify_customer)."},
         {"role": "tool_call", "id": "tc-3", "tool": "get_order", "args": {"order_id": "#W1"}},
         {"role": "tool_result", "id": "tc-3", "tool": "get_order", "ok": True, "text": "Order #W1"},
         {"role": "tool_call", "id": "tc-4", "tool": "find_variants", "args": {}},
         {"role": "tool_result", "id": "tc-4", "tool": "find_variants", "ok": False, "fact": False,
          "text": "Error: Give a product_id."}]
    view, hidden = model_view(t)
    assert view == [t[0],
                    {"role": "note", "text": "Fact from an earlier failed call: A refund can only go "
                                             "to the original payment method or to a gift card."},
                    t[6], t[7], t[8], t[9]]                     # the latest failure stays: no recovery yet
    assert hidden == 4                                           # note, tc-1 call, tc-2 call + result


def test_g_customer_messages_and_code_notes_always_stay():
    t = [{"role": "tool_call", "id": "tc-1", "tool": "x", "args": {}},
         {"role": "tool_result", "id": "tc-1", "tool": "x", "ok": False, "fact": False, "text": "Error"},
         {"role": "customer", "text": "Hello?"},
         {"role": "note", "text": "The summary of change #1 was shown to the customer."},
         {"role": "agent", "text": "Hi!"}]
    view, hidden = model_view(t)
    assert view == t[2:] and hidden == 2


def test_g_the_model_sees_the_clean_view_and_the_log_counts_it(env):
    call = {"generation_time_seconds": 1.0, "usage": None, "cost": None}
    model = ScriptedModel(*[replace(x, model_call=call) for x in (
        Decision(invalid="Error: there is no tool called ''."),
        Decision.call("verify_customer", **YUSUF),
        Decision.reply("Thanks Yusuf, you're verified."))])
    d = Driver(model)
    until_say(d, env, d.on_customer("Hi, I'm Yusuf Rossi, zip 19122."))
    assert any(e.get("failed") for e in model.seen[1])          # just after the failure: shown
    assert not any(e.get("failed") for e in model.seen[2])      # after recovery: hidden
    assert [e["hidden"] for e in kinds(d, "model_call")] == [0, 0, 1]   # counted in the log
    assert any(e["kind"] == "note" for e in d.log)              # the log keeps everything


# ---------- H: an identical successful call is not run again ----------

def test_h_identical_read_is_not_run(env):
    get = Decision.call("get_order", order_id="#W2378156")
    model = ScriptedModel(Decision.call("verify_customer", **YUSUF), get, get, Decision.reply("Here it is."))
    d = Driver(model)
    _, rounds = until_say(d, env, d.on_customer("Yusuf Rossi 19122, order #W2378156 please"))
    assert len(rounds) == 3 + 1                                  # the second get_order didn't run
    [dup] = kinds(d, "duplicate_call")
    assert dup["tool"] == "get_order" and dup["of"] == "tc-2"


def test_h_empty_inputs_dont_make_a_call_different(env):
    """Unseen task 20: verify_customer again, the same plus email=""."""
    model = ScriptedModel(Decision.call("verify_customer", **YUSUF),
                          Decision.call("verify_customer", email="", **YUSUF), Decision.reply("Hi."))
    d = Driver(model)
    _, rounds = until_say(d, env, d.on_customer("Yusuf Rossi, zip 19122"))
    assert len(rounds) == 3 and len(kinds(d, "duplicate_call")) == 1


def test_h_after_a_change_reading_again_is_allowed(env):
    d, _ = driver_at_summary(env, FakeChecker("YES"), Decision.call("confirm_change", change_number=1),
                             Decision.call("get_order", order_id="#W6247578"),
                             Decision.reply("It's cancelled."))
    until_say(d, env, d.on_customer("Yes."))
    _, rounds = until_say(d, env, d.on_customer("What's the status of that order now?"))
    assert rounds == [["get_order_details"]] and not kinds(d, "duplicate_call")


def test_h_the_same_prepared_change_again(env):
    """Practice task 1: the same change prepared again; now it points to the one shown."""
    d, _ = driver_at_summary(env, FakeChecker("NO"),
                             Decision.call("prepare_change", kind="cancel_order", order_id="#W6247578",
                                           reason="no longer needed"),
                             Decision.reply("It's the summary above. Shall I go ahead?"))
    said, rounds = until_say(d, env, d.on_customer("Sorry, what was that again?"))
    assert rounds == [] and said.text.startswith("It's the summary above")
    [dup] = kinds(d, "duplicate_call")
    assert dup["tool"] == "prepare_change"


# ---------- every error says whether it is a fact ----------

def test_every_tool_error_says_fact_or_not():
    facts = 0
    for f in sorted(Path("fde_agent/tools").glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                    and getattr(node.exc.func, "id", None) == "ToolError":
                [kw] = [k for k in node.exc.keywords if k.arg == "fact"] or [None]
                assert kw is not None, f"{f.name}:{node.lineno} raises ToolError without fact="
                facts += kw.value.value is True
    assert facts == 18
