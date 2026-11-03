"""find_replacement against the REAL retail store and database. No model."""
import pytest

from fde_agent.notes import Notes
from fde_agent.store import run_now
from fde_agent.tools.find_replacement import find_replacement
from fde_agent.tools.get_order import get_order
from fde_agent.tools.verify_customer import verify_customer


@pytest.fixture
def env():
    from tau2.domains.retail.environment import get_environment
    return get_environment()


def ready(env, first, last, zip_code, *orders) -> Notes:
    """Verify, then view orders so their item IDs are seen (as the agent would)."""
    n = Notes()
    assert run_now(verify_customer(n, first_name=first, last_name=last, zip=zip_code), env).ok
    for o in orders:
        assert run_now(get_order(n, order_id=o), env).ok
    return n


@pytest.fixture
def yusuf(env):
    return ready(env, "Yusuf", "Rossi", "19122", "#W6247578", "#W2378156", "#W9711842")


def fr(notes, env, **kw):
    out = run_now(find_replacement(notes, **kw), env)
    print("\n" + out.text)
    return out


# 1. pending T-shirt, size XXL -> XL: rest kept (black, cotton, crew neck)
def test_tshirt_size(yusuf, env):
    out = fr(yusuf, env, order_id="#W6247578", item_id="item_3799046073", changes={"size": "XL"})
    assert out.ok
    assert ("Replacement: item_2060066974 | $51.05 | color: black, size: XL, material: cotton, "
            "style: crew neck | the customer gets $2.22 back") in out.text
    assert "This order is pending, so this would be a CHANGE ITEMS" in out.text


# 2. delivered thermostat -> Google Assistant
def test_thermostat(yusuf, env):
    out = fr(yusuf, env, order_id="#W2378156", item_id="item_4983901480",
             changes={"compatibility": "google assistant"})
    assert "Replacement: item_7747408585 | $249.01 | compatibility: Google Assistant, color: black" in out.text
    assert "the customer gets $13.46 back" in out.text
    assert "This order is delivered, so this would be an EXCHANGE" in out.text


# 3. task 1: keyboard -> clicky. Exact one not available; closest keep "clicky";
#    the customer's own keyboard is NEVER offered
def test_keyboard_clicky(yusuf, env):
    out = fr(yusuf, env, order_id="#W2378156", item_id="item_1151293680",
             changes={"switch type": "clicky"})
    assert "exact replacement exists but is NOT available (cannot be ordered): item_9025753381" in out.text
    closest = out.text.split("Closest available")[1].split("This order")[0]
    assert "item_2299424241" in closest   # clicky RGB 80%
    assert "item_6342039236" in closest   # clicky white full size
    assert "item_7706410293" in closest   # clicky none full size
    assert "linear" not in closest and "tactile" not in closest   # wanted change kept
    assert "item_1151293680" not in out.text.split("Current:")[1].split("\n", 1)[1]  # YOUR RULE


# 4. asking for what they already have
def test_no_real_change(yusuf, env):
    out = fr(yusuf, env, order_id="#W6247578", item_id="item_3799046073", changes={"color": "black"})
    assert not out.ok and "That is what the customer already has" in out.text


# 5a. item not in that order
def test_item_not_in_order(yusuf, env):
    out = fr(yusuf, env, order_id="#W6247578", item_id="item_1151293680", changes={"size": "XL"})
    assert not out.ok
    assert "item_1151293680 is not in order #W6247578. Its items are: item_3799046073 (T-Shirt)." in out.text


# 5b. unseen item ID -> no store call
def test_unseen_item(yusuf, env):
    out = fr(yusuf, env, order_id="#W6247578", item_id="item_1234567890", changes={"size": "XL"})
    assert not out.ok and "has not appeared" in out.text and out.calls == []


# 6. cancelled order -> explains why
def test_cancelled_order(yusuf, env):
    out = fr(yusuf, env, order_id="#W9711842", item_id="item_4245201809", changes={"lens color": "blue"})
    assert not out.ok
    assert "Order #W9711842 is 'cancelled', so its items can't be changed or exchanged" in out.text


# 7. Option A: a candidate that is ANOTHER item in the same order is offered, but marked
def test_other_item_in_order_is_marked(env):
    n = ready(env, "Evelyn", "Wilson", "98148", "#W8042635")
    out = fr(n, env, order_id="#W8042635", item_id="item_9237024510",
             changes={"pieces": "2000", "difficulty level": "intermediate"})
    assert out.ok
    assert "Replacement: item_5645314103" in out.text
    assert "(the customer already has this exact item in this order)" in out.text


def test_marked_in_closest_too(env):
    # only "2000 pieces": exact (2000, expert) is unavailable; closest is the one already in the order
    n = ready(env, "Evelyn", "Wilson", "98148", "#W8042635")
    out = fr(n, env, order_id="#W8042635", item_id="item_9237024510", changes={"pieces": "2000"})
    assert "NOT available" in out.text and "item_7869640094" in out.text
    assert "item_5645314103" in out.text and "already has this exact item" in out.text


# extra: no store call without changes; wrong value is explained
def test_empty_changes(yusuf, env):
    out = fr(yusuf, env, order_id="#W6247578", item_id="item_3799046073", changes={})
    assert not out.ok and "Give the options to change" in out.text and out.calls == []


def test_unknown_value_explained(yusuf, env):
    out = fr(yusuf, env, order_id="#W6247578", item_id="item_3799046073", changes={"size": "large"})
    assert not out.ok and "Values for size: L, M, S, XL, XXL" in out.text
