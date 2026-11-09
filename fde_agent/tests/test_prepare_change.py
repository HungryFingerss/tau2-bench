"""prepare_change against the REAL retail store and database. No model.
Every test also proves: NOTHING was written (only read calls were made)."""
import json

import pytest

from fde_agent.notes import Notes
from fde_agent.store import run_now
from fde_agent.tools.find_replacement import find_replacement
from fde_agent.tools.get_order import get_order
from fde_agent.tools.prepare_change import ASK, NOT_YET, prepare_change
from fde_agent.tools.verify_customer import verify_customer

READS = {"get_user_details", "get_order_details", "get_product_details"}
NEW_ADDRESS = {"address1": "12 Main St", "address2": "Apt 4", "city": "Austin",
               "state": "TX", "country": "USA", "zip": "78701"}


@pytest.fixture
def env():
    from tau2.domains.retail.environment import get_environment
    return get_environment()


def ready(env, first, last, zip_code, *orders) -> Notes:
    n = Notes()
    assert run_now(verify_customer(n, first_name=first, last_name=last, zip=zip_code), env).ok
    for o in orders:
        assert run_now(get_order(n, order_id=o), env).ok
    return n


@pytest.fixture
def yusuf(env):
    return ready(env, "Yusuf", "Rossi", "19122", "#W6247578", "#W2378156")


def pc(notes, env, **kw):
    out = run_now(prepare_change(notes, **kw), env)
    print("\n" + out.text)
    assert {c.name for c in out.calls} <= READS, "prepare_change must never write"
    return out


def store_status(env, order_id):
    from tau2.data_model.message import ToolCall
    r = env.get_response(ToolCall(id="x", name="get_order_details", arguments={"order_id": order_id}))
    return json.loads(r.content)["status"]


# 1. cancel: summary with refund; the order is STILL pending
def test_cancel(yusuf, env):
    out = pc(yusuf, env, kind="cancel_order", order_id="#W6247578", reason="no longer needed")
    assert out.ok
    assert out.text.startswith("Change #1 prepared. NOT done yet.")
    assert "Refund: $53.27 to your Mastercard credit card ending 2478, in 5 to 7 business days." in out.text
    assert "Once I confirm it, I can't undo this." in out.text
    assert yusuf.prepared.call.name == "cancel_pending_order"
    assert yusuf.prepared.call.args == {"order_id": "#W6247578", "reason": "no longer needed"}
    assert store_status(env, "#W6247578") == "pending"


# 2. a reason outside the rulebook
def test_cancel_bad_reason(yusuf, env):
    out = pc(yusuf, env, kind="cancel_order", order_id="#W6247578", reason="changed my mind")
    assert not out.ok and "'no longer needed' or 'ordered by mistake'" in out.text
    assert out.calls == [] and yusuf.prepared is None


# 3. cancel a delivered order
def test_cancel_delivered(yusuf, env):
    out = pc(yusuf, env, kind="cancel_order", order_id="#W2378156", reason="ordered by mistake")
    assert not out.ok and "is 'delivered', so it can't be cancelled" in out.text


# 4. exchange two items in one go
def test_exchange_two_items(yusuf, env):
    run_now(find_replacement(yusuf, order_id="#W2378156", item_id="item_1151293680",
                             changes={"switch type": "clicky"}), env)
    run_now(find_replacement(yusuf, order_id="#W2378156", item_id="item_4983901480",
                             changes={"compatibility": "Google Assistant"}), env)
    out = pc(yusuf, env, kind="exchange_items", order_id="#W2378156", payment_method_id="credit_card_9513926",
             items=[{"item_id": "item_1151293680", "new_item_id": "item_6342039236"},
                    {"item_id": "item_4983901480", "new_item_id": "item_7747408585"}])
    assert out.ok
    assert "- Mechanical Keyboard: switch type linear -> clicky, backlight RGB -> white ($272.33 -> $244.91)" in out.text
    assert "- Smart Thermostat: compatibility Apple HomeKit -> Google Assistant ($262.47 -> $249.01)" in out.text
    assert "You get $40.88 back, to your Mastercard credit card ending 2478." in out.text
    assert "nothing else in this order can be returned or exchanged" in out.text
    assert "item_" not in out.text.split("---")[1]  # no IDs in the customer's text
    assert yusuf.prepared.call.args["item_ids"] == ["1151293680", "4983901480"]
    assert yusuf.prepared.call.args["new_item_ids"] == ["6342039236", "7747408585"]
    assert store_status(env, "#W2378156") == "delivered"


# 5. bad replacements: same item / not available / another product
def test_exchange_bad_items(yusuf, env):
    run_now(find_replacement(yusuf, order_id="#W2378156", item_id="item_1151293680",
                             changes={"switch type": "clicky"}), env)   # shows item_9025753381 (n/a)
    run_now(find_replacement(yusuf, order_id="#W2378156", item_id="item_4983901480",
                             changes={"compatibility": "Google Assistant"}), env)
    base = dict(kind="exchange_items", order_id="#W2378156", payment_method_id="credit_card_9513926")
    same = pc(yusuf, env, **base, items=[{"item_id": "item_4983901480", "new_item_id": "item_4983901480"}])
    assert not same.ok and "is the same item the customer already has" in same.text
    na = pc(yusuf, env, **base, items=[{"item_id": "item_1151293680", "new_item_id": "item_9025753381"}])
    assert not na.ok and "is not available" in na.text
    other = pc(yusuf, env, **base, items=[{"item_id": "item_1151293680", "new_item_id": "item_7747408585"}])
    assert not other.ok and "is not a Mechanical Keyboard" in other.text
    assert yusuf.prepared is None


# 6. change items on a pending order: the full warning
def test_change_items_warning(yusuf, env):
    run_now(find_replacement(yusuf, order_id="#W6247578", item_id="item_3799046073", changes={"size": "XL"}), env)
    out = pc(yusuf, env, kind="change_order_items", order_id="#W6247578", payment_method_id="credit_card_9513926",
             items=[{"item_id": "item_3799046073", "new_item_id": "item_2060066974"}])
    assert out.ok
    assert "- T-Shirt: size XXL -> XL ($53.27 -> $51.05)" in out.text
    assert "You get $2.22 back" in out.text
    assert "If you want an address or payment change on this order, tell me now" in out.text
    assert "If you want to change any other item in this order, tell me now" in out.text
    shown = yusuf.prepared.customer_text           # exactly what the customer will see
    assert shown.count("?") == 1 and shown.endswith(ASK)                   # ONE question
    assert shown.startswith(NOT_YET + "\n")              # says plainly nothing has happened yet


# 7. returns: refund to original or a gift card only
def test_return_refund_rules(env):
    noah = ready(env, "Noah", "Brown", "80279", "#W7678072")
    bad = pc(noah, env, kind="return_items", order_id="#W7678072", item_ids=["item_2323972008"],
             payment_method_id="credit_card_7815826")
    assert not bad.ok and "can only go to the original payment method (PayPal" in bad.text
    ok = pc(noah, env, kind="return_items", order_id="#W7678072", item_ids=["item_2323972008"],
            payment_method_id="paypal_5727330")
    assert ok.ok and "Refund:" in ok.text and "to your PayPal." in ok.text


def test_return_to_gift_card(env):
    raj = ready(env, "Raj", "Sanchez", "92147", "#W7736708")
    out = pc(raj, env, kind="return_items", order_id="#W7736708",
             item_ids=["item_6574183535", "item_1096508426"], payment_method_id="gift_card_2259499")
    assert out.ok
    assert "Refund: $74.27 to your Gift card." in out.text   # 28.14 + 46.13


# 8. payment change to a gift card that can't cover the total
def test_gift_card_too_small(env):
    raj = ready(env, "Raj", "Sanchez", "92147", "#W4566809")
    out = pc(raj, env, kind="change_order_payment", order_id="#W4566809", payment_method_id="gift_card_2259499")
    assert not out.ok
    assert "The gift card balance ($30.00) is less than the order total ($826.70)" in out.text


# 9. addresses: missing field (no store call) / same as current
def test_address_missing_zip(yusuf, env):
    a = dict(NEW_ADDRESS, zip="")
    out = pc(yusuf, env, kind="change_order_address", order_id="#W6247578", address=a)
    assert not out.ok and "The address is missing: zip" in out.text and out.calls == []


def test_address_same_as_current(yusuf, env):
    same = {"address1": "763 Broadway", "address2": "Suite 135", "city": "Philadelphia",
            "state": "PA", "country": "USA", "zip": "19122"}
    out = pc(yusuf, env, kind="change_order_address", order_id="#W6247578", address=same)
    assert not out.ok and "already the shipping address" in out.text


# 10. default address
def test_default_address(yusuf, env):
    out = pc(yusuf, env, kind="change_default_address", address=NEW_ADDRESS)
    assert out.ok
    assert "From: 763 Broadway, Suite 135, Philadelphia, PA 19122, USA" in out.text
    assert "To: 12 Main St, Apt 4, Austin, TX 78701, USA" in out.text
    assert "doesn't change the shipping address of existing orders" in out.text
    assert yusuf.prepared.call.name == "modify_user_address"
    assert yusuf.prepared.call.args["user_id"] == "yusuf_rossi_9620"


# 11. preparing again replaces the earlier change
def test_second_prepare_replaces_first(yusuf, env):
    pc(yusuf, env, kind="cancel_order", order_id="#W6247578", reason="no longer needed")
    out = pc(yusuf, env, kind="change_order_address", order_id="#W6247578", address=NEW_ADDRESS)
    assert out.text.startswith("Change #2 prepared.")
    assert yusuf.prepared.number == 2 and yusuf.prepared.kind == "change_order_address"


# 12. inputs: unknown kind / missing inputs (with example) / not verified / listed twice
def test_unknown_kind(yusuf, env):
    out = pc(yusuf, env, kind="refund_order", order_id="#W6247578")
    assert not out.ok and "Kinds are: cancel_order, change_order_address" in out.text


def test_missing_inputs_show_example(yusuf, env):
    out = pc(yusuf, env, kind="return_items", order_id="#W2378156")
    assert not out.ok and "return_items needs: item_ids, payment_method_id. Example:" in out.text
    assert out.calls == []


def test_not_verified(env):
    out = pc(Notes(), env, kind="cancel_order", order_id="#W6247578", reason="no longer needed")
    assert not out.ok and "Verify the customer first" in out.text


def test_item_listed_more_times_than_in_order(yusuf, env):
    out = pc(yusuf, env, kind="return_items", order_id="#W2378156", payment_method_id="credit_card_9513926",
             item_ids=["item_1151293680", "item_1151293680"])
    assert not out.ok and "is in it only 1 time(s)" in out.text


# The warnings are statements: the only question in a summary is the one in ASK.
# A second question invites "No, nothing else. Go ahead", which the yes rule refuses.
def test_warnings_ask_no_question():
    from fde_agent.tools.prepare_change import ASK, WARN_CANCEL, WARN_DELIVERED, WARN_ITEMS
    for warning in (WARN_ITEMS, WARN_DELIVERED.format(verb="exchange"), WARN_CANCEL):
        assert "?" not in warning
    assert ASK.count("?") == 1 and ASK.startswith("Shall I go ahead?")
