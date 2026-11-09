"""A: the summary lists the other VALID payment options, so the customer's yes is a real choice.
B: inputs prepare_change doesn't use are logged (evidence for splitting the tool).
Real store, no model."""
import pytest

from fde_agent.driver import Decision, Driver
from fde_agent.notes import Notes
from fde_agent.store import run_now
from fde_agent.tests.test_driver import YUSUF, ScriptedModel, until_say
from fde_agent.tools.get_order import get_order
from fde_agent.tools.prepare_change import _difference_alternatives, ignored_inputs, prepare_change
from fde_agent.tools.verify_customer import verify_customer


@pytest.fixture
def env():
    from tau2.domains.retail.environment import get_environment
    return get_environment()


def ready(env, first, last, zip_code, *orders):
    n = Notes()
    assert run_now(verify_customer(n, first_name=first, last_name=last, zip=zip_code), env).ok
    for o in orders:
        assert run_now(get_order(n, order_id=o), env).ok
    return n


def summary(notes, env, **kw):
    out = run_now(prepare_change(notes, **kw), env)
    assert out.ok, out.text
    return out.text.split("---")[1]          # what the customer will see


# A1. Raj returns to his card -> his gift card is named as the other option
def test_return_names_the_gift_card(env):
    raj = ready(env, "Raj", "Sanchez", "92147", "#W7736708")
    text = summary(raj, env, kind="return_items", order_id="#W7736708",
                   item_ids=["item_6574183535"], payment_method_id="credit_card_3362387")
    assert "You can also choose: your Gift card (balance $30.00)." in text


# A2. ...and the other way round
def test_return_to_gift_card_names_the_card(env):
    raj = ready(env, "Raj", "Sanchez", "92147", "#W7736708")
    text = summary(raj, env, kind="return_items", order_id="#W7736708",
                   item_ids=["item_6574183535"], payment_method_id="gift_card_2259499")
    assert "You can also choose: your" in text and "credit card ending" in text


# A3. Yusuf has only one card -> nothing to add
def test_single_option_adds_nothing(env):
    yusuf = ready(env, "Yusuf", "Rossi", "19122", "#W2378156")
    text = summary(yusuf, env, kind="return_items", order_id="#W2378156",
                   item_ids=["item_1151293680"], payment_method_id="credit_card_9513926")
    assert "You can also choose" not in text


# A4. a refund can't go to a non-original card -> Noah's card is NOT offered
def test_invalid_options_are_never_offered(env):
    noah = ready(env, "Noah", "Brown", "80279", "#W7678072")
    text = summary(noah, env, kind="return_items", order_id="#W7678072",
                   item_ids=["item_2323972008"], payment_method_id="paypal_5727330")
    assert "You can also choose" not in text


# A5. price differences: a gift card is offered only if it can pay; refunds can go anywhere
def test_price_difference_options():
    user = {"payment_methods": {
        "credit_card_1": {"source": "credit_card", "id": "credit_card_1", "brand": "visa", "last_four": "1111"},
        "gift_card_2": {"source": "gift_card", "id": "gift_card_2", "balance": 30.0},
        "paypal_3": {"source": "paypal", "id": "paypal_3"}}}
    assert _difference_alternatives(user, "credit_card_1", 50.0) == ["You can also choose: your PayPal."]
    assert _difference_alternatives(user, "credit_card_1", -20.0) == [
        "You can also choose: your Gift card (balance $30.00); your PayPal."]
    assert _difference_alternatives(user, "credit_card_1", 0.0) == []


# B1. the exact call the real model made in the live run: an address, items and a reason for a return
LIVE_CALL = {"address": {"address1": "763 Broadway", "address2": "Suite 135", "city": "Philadelphia",
                         "country": "USA", "state": "PA", "zip": "19122"},
             "item_ids": ["item_1151293680"], "items": [], "kind": "return_items",
             "order_id": "#W2378156", "payment_method_id": "credit_card_9513926",
             "reason": "return mechanical keyboard"}


def test_ignored_inputs_are_found():
    assert ignored_inputs("return_items", LIVE_CALL) == ["address", "items", "reason"]
    assert ignored_inputs("cancel_order", {"kind": "cancel_order", "order_id": "#W1", "reason": "x"}) == []
    assert ignored_inputs("no_such_kind", LIVE_CALL) == []


def test_driver_logs_ignored_inputs(env):
    model = ScriptedModel(Decision.call("verify_customer", **YUSUF),
                          Decision.call("get_order", order_id="#W2378156"),
                          Decision.call("prepare_change", **LIVE_CALL))
    d = Driver(model)
    until_say(d, env, d.on_customer("Yusuf Rossi 19122, return my keyboard"))
    logged = [e for e in d.log if e["kind"] == "ignored_inputs"]
    assert logged == [{**logged[0], "kind": "ignored_inputs", "tool": "prepare_change",
                       "change_kind": "return_items", "inputs": ["address", "items", "reason"]}]
    assert d.notes.prepared is not None          # the tool still worked: they were only ignored
