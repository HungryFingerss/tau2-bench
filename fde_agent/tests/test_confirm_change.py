"""confirm_change against the REAL retail store and database. No model.
'Shown' and the customer's reply are simulated in the notes until the agent loop exists."""
import json

import pytest

from fde_agent.notes import Notes
from fde_agent.store import run_now
from fde_agent.tools.confirm_change import confirm_change, is_clear_yes
from fde_agent.tools.find_replacement import find_replacement
from fde_agent.tools.get_order import get_order
from fde_agent.tools.prepare_change import prepare_change
from fde_agent.tools.verify_customer import verify_customer

WRITES = {"cancel_pending_order", "modify_pending_order_address", "modify_pending_order_payment",
          "modify_pending_order_items", "return_delivered_order_items",
          "exchange_delivered_order_items", "modify_user_address"}
NEW_ADDRESS = {"address1": "12 Main St", "address2": "Apt 4", "city": "Austin",
               "state": "TX", "country": "USA", "zip": "78701"}


@pytest.fixture
def env():
    from tau2.domains.retail.environment import get_environment
    return get_environment()


@pytest.fixture
def yusuf(env):
    n = Notes()
    assert run_now(verify_customer(n, first_name="Yusuf", last_name="Rossi", zip="19122"), env).ok
    for o in ("#W6247578", "#W2378156"):
        assert run_now(get_order(n, order_id=o), env).ok
    return n


def prepare(notes, env, **kw) -> int:
    out = run_now(prepare_change(notes, **kw), env)
    assert out.ok, out.text
    return notes.prepared.number


def cancel_tshirt(notes, env) -> int:
    return prepare(notes, env, kind="cancel_order", order_id="#W6247578", reason="no longer needed")


def confirm(notes, env, number):
    out = run_now(confirm_change(notes, change_number=number), env)
    print("\n" + out.text)
    return out


def writes(out):
    return [c for c in out.calls if c.name in WRITES]


def read(env, name, **args):
    from tau2.data_model.message import ToolCall
    return json.loads(env.get_response(ToolCall(id="x", name=name, arguments=args)).content)


# 1. the happy path
def test_cancel_confirmed(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("Yes.")
    out = confirm(yusuf, env, n)
    assert out.ok
    assert out.text == "Done: change #1 on order #W6247578. Order status is now 'cancelled'. Tell the customer it's done."
    assert [c.name for c in out.calls] == ["cancel_pending_order"]
    assert read(env, "get_order_details", order_id="#W6247578")["status"] == "cancelled"
    assert yusuf.orders["#W6247578"] == "cancelled"
    assert yusuf.prepared is None and 1 in yusuf.done


# 2. not shown yet
def test_not_shown(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.customer_said("yes")   # said before seeing it: doesn't count
    out = confirm(yusuf, env, n)
    assert not out.ok and "hasn't seen change #1" in out.text and out.calls == []


# 3. shown, no answer yet
def test_no_reply_yet(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    out = confirm(yusuf, env, n)
    assert not out.ok and "hasn't answered change #1" in out.text and out.calls == []


# 4. the task 6 reply: narrowed, not a yes
def test_narrowed_reply_refused(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("I'll take the lamp, but I'm not doing the water bottle. Just the lamp.")
    out = confirm(yusuf, env, n)
    assert not out.ok and "was not a clear yes" in out.text and "Just the lamp" in out.text
    assert out.calls == []
    assert read(env, "get_order_details", order_id="#W6247578")["status"] == "pending"


# 4b. the LATEST reply counts: "yes" then "wait..." -> refused
def test_latest_reply_counts(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("Yes")
    yusuf.customer_said("Wait, actually let me think")
    assert not confirm(yusuf, env, n).ok


# 5. an older number after a new prepare
def test_replaced_change(yusuf, env):
    first = cancel_tshirt(yusuf, env)
    second = prepare(yusuf, env, kind="change_order_address", order_id="#W6247578", address=NEW_ADDRESS)
    yusuf.mark_shown(second)
    yusuf.customer_said("yes")
    out = confirm(yusuf, env, first)
    assert not out.ok and "Change #1 was replaced by change #2" in out.text and out.calls == []


# 6. confirming twice -> ONE write
def test_no_double_write(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("yes please")
    assert confirm(yusuf, env, n).ok
    again = confirm(yusuf, env, n)
    assert not again.ok and "already done" in again.text and again.calls == []


# 7. exchange
def test_exchange_confirmed(yusuf, env):
    run_now(find_replacement(yusuf, order_id="#W2378156", item_id="item_4983901480",
                             changes={"compatibility": "Google Assistant"}), env)
    n = prepare(yusuf, env, kind="exchange_items", order_id="#W2378156", payment_method_id="credit_card_9513926",
                items=[{"item_id": "item_4983901480", "new_item_id": "item_7747408585"}])
    yusuf.mark_shown(n)
    yusuf.customer_said("Yes, go ahead.")
    out = confirm(yusuf, env, n)
    assert out.ok and "Order status is now 'exchange requested'" in out.text
    order = read(env, "get_order_details", order_id="#W2378156")
    assert order["exchange_items"] == ["4983901480"] and order["exchange_new_items"] == ["7747408585"]


# 8. default address
def test_default_address_confirmed(yusuf, env):
    n = prepare(yusuf, env, kind="change_default_address", address=NEW_ADDRESS)
    yusuf.mark_shown(n)
    yusuf.customer_said("ok")
    out = confirm(yusuf, env, n)
    assert out.ok and "default address is now 12 Main St, Apt 4, Austin, TX 78701, USA" in out.text
    assert read(env, "get_user_details", user_id="yusuf_rossi_9620")["address"]["city"] == "Austin"


# 9. the order changed between prepare and confirm -> store refuses, change dropped
def test_store_refuses(yusuf, env):
    n = prepare(yusuf, env, kind="change_order_address", order_id="#W6247578", address=NEW_ADDRESS)
    read(env, "cancel_pending_order", order_id="#W6247578", reason="ordered by mistake")  # elsewhere
    yusuf.mark_shown(n)
    yusuf.customer_said("yes")
    out = confirm(yusuf, env, n)
    assert not out.ok and "The store did not make change #1" in out.text
    assert yusuf.prepared is None and 1 not in yusuf.done
    assert len(writes(out)) == 1   # tried once, not retried


# number given as text
def test_number_as_text(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("Yes")
    assert confirm(yusuf, env, "#1").ok


# 10. the yes rule on its own
@pytest.mark.parametrize("reply", [
    "Yes.", "yes", "Yes please.", "Yes, go ahead.", "Okay.", "Sure, go ahead with it.",
    "Yes, that's correct.", "Please do.", "Confirm.", "Yep, sounds good!", "Yes, that’s correct.",
])
def test_clear_yes(reply):
    assert is_clear_yes(reply)


@pytest.mark.parametrize("reply", [
    "", None, "No.", "Not yet.", "I'll take the lamp, but not the water bottle.",
    "Yes, but only the lamp.", "Yes, how long does the refund take?", "Wait, let me check.",
    "Actually, change the size instead.", "I don't want that.", "I don’t want that.", "Hmm, let me think",
    "No problem, go ahead",   # false refusal, on purpose: the model asks again
])
def test_not_clear_yes(reply):
    assert not is_clear_yes(reply)
