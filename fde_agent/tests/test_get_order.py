"""get_order against the REAL retail store and database. No model."""
import pytest

from fde_agent.ids import ITEM, IdError, unlabel
from fde_agent.notes import Notes
from fde_agent.store import run_now
from fde_agent.tools.get_order import get_order, normalize_order_id
from fde_agent.tools.verify_customer import verify_customer


@pytest.fixture
def env():
    from tau2.domains.retail.environment import get_environment
    return get_environment()


def verified(env, first="Yusuf", last="Rossi", zip_code="19122") -> Notes:
    notes = Notes()
    assert run_now(verify_customer(notes, first_name=first, last_name=last, zip=zip_code), env).ok
    return notes


def store(env, name, **args):
    """Call a store tool directly, as if an earlier action in the chat did it."""
    from tau2.data_model.message import ToolCall
    r = env.get_response(ToolCall(id="direct", name=name, arguments=args))
    assert not r.error, r.content


# 1. not verified -> error, no store call
def test_needs_verified_customer(env):
    out = run_now(get_order(Notes(), order_id="#W2378156"), env)
    assert not out.ok
    assert "Verify the customer first" in out.text
    assert out.calls == []


# 2. someone else's order -> error listing theirs, no store call
def test_other_customers_order_refused(env):
    out = run_now(get_order(verified(env), order_id="#W1840144"), env)  # Harper Brown's
    print("\n" + out.text)
    assert not out.ok
    assert "#W1840144 is not one of Yusuf Rossi's orders" in out.text
    assert "#W6247578, #W9711842, #W4776164, #W6679257, #W2378156" in out.text
    assert out.calls == []


# 3. delivered order: labelled IDs, prices, total, tracking, return/exchange
def test_delivered_order_details(env):
    out = run_now(get_order(verified(env), order_id="#W2378156"), env)
    print("\n" + out.text)
    assert out.ok
    assert out.text.startswith("Order #W2378156 | status: delivered")
    assert ("- Mechanical Keyboard | item_1151293680 | product_1656367028 | $272.33 | "
            "switch type: linear, backlight: RGB, size: full size") in out.text
    assert "Paid: $1,819.92 by Mastercard credit card ending 2478 (payment ID: credit_card_9513926)" in out.text
    assert "Tracking: 843053632392" in out.text
    assert "Possible actions (status: delivered): return items; exchange items" in out.text
    assert [c.name for c in out.calls] == ["get_order_details"]


# 4. order ID written in different ways -> same order
@pytest.mark.parametrize("raw", ["#W2378156", "W2378156", "w2378156", "2378156", "  #w2378156 "])
def test_order_id_forms(raw):
    assert normalize_order_id(raw) == "#W2378156"


def test_order_id_without_hash_works_end_to_end(env):
    out = run_now(get_order(verified(env), order_id="2378156"), env)
    assert out.ok and "Order #W2378156" in out.text


# 5. pending order: cancel/change actions, no tracking line
def test_pending_order_actions(env):
    out = run_now(get_order(verified(env), order_id="#W6247578"), env)
    print("\n" + out.text)
    assert out.ok
    assert "status: pending" in out.text
    assert "cancel the order (reason must be 'no longer needed' or 'ordered by mistake')" in out.text
    assert "Changing items can be done only ONCE" in out.text
    assert "Tracking" not in out.text


# 6. item IDs are accepted only after a tool has shown them
def test_ids_become_seen(env):
    notes = verified(env)
    with pytest.raises(IdError, match="has not appeared"):
        unlabel(ITEM, "item_1151293680", notes.seen)  # verify_customer shows no IDs
    run_now(get_order(notes, order_id="#W2378156"), env)
    assert unlabel(ITEM, "item_1151293680", notes.seen) == "1151293680"


# 7. fresh status: cancelled earlier in the chat -> shown as cancelled, notes updated
def test_status_is_fresh(env):
    notes = verified(env)
    store(env, "cancel_pending_order", order_id="#W6247578", reason="no longer needed")
    out = run_now(get_order(notes, order_id="#W6247578"), env)
    print("\n" + out.text)
    assert "status: cancelled" in out.text
    assert "Possible actions (status: cancelled): none" in out.text
    assert "Cancel reason: no longer needed" in out.text
    assert notes.orders["#W6247578"] == "cancelled"


# 8. cancelled order shows its refund
def test_cancelled_order_shows_refund(env):
    out = run_now(get_order(verified(env), order_id="#W9711842"), env)
    print("\n" + out.text)
    assert "Refunded: $281.48 to Mastercard credit card ending 2478" in out.text


# 9. repeated item shown once with x2
def test_repeated_item(env):
    notes = verified(env, "Harper", "Kovacs", "95154")
    out = run_now(get_order(notes, order_id="#W9093821"), env)
    print("\n" + out.text)
    assert "Items (5):" in out.text
    assert out.text.count("item_3557711149") == 1
    assert "| $205.35 each, x2 |" in out.text


# 10. rulebook: after items are changed, nothing else is possible
def test_item_modified_order_allows_nothing(env):
    notes = verified(env)
    store(env, "modify_pending_order_items", order_id="#W6247578",
          item_ids=["3799046073"], new_item_ids=["9612497925"],
          payment_method_id="credit_card_9513926")
    out = run_now(get_order(notes, order_id="#W6247578"), env)
    print("\n" + out.text)
    assert "status: pending (item modified)" in out.text
    assert "Possible actions (status: pending (item modified)): none." in out.text
