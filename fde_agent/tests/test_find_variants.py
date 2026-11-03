"""find_variants against the REAL retail store and database. No model."""
import pytest

from fde_agent.ids import ITEM, PRODUCT, unlabel
from fde_agent.notes import Notes
from fde_agent.store import run_now
from fde_agent.tools.find_variants import find_variants
from fde_agent.tools.get_order import get_order
from fde_agent.tools.verify_customer import verify_customer


@pytest.fixture
def env():
    from tau2.domains.retail.environment import get_environment
    return get_environment()


@pytest.fixture
def notes(env):
    """Yusuf verified, and order #W2378156 viewed (so its product IDs are seen)."""
    n = Notes()
    assert run_now(verify_customer(n, first_name="Yusuf", last_name="Rossi", zip="19122"), env).ok
    assert run_now(get_order(n, order_id="#W2378156"), env).ok
    return n


def fv(notes, env, **kw):
    out = run_now(find_variants(notes, **kw), env)
    print("\n" + out.text)
    return out


# 1. counts only AVAILABLE ones (baseline tasks 2 and 4 said 12)
def test_tshirt_counts(notes, env):
    out = fv(notes, env, product_name="T-Shirt")
    assert out.ok
    assert "T-Shirt (product_9523456873): 12 variants in total, 10 available, 2 not available." in out.text
    assert out.text.count("\n- item_") == 12


# 2. filter by colour
def test_filter_blue(notes, env):
    out = fv(notes, env, product_name="T-Shirt", options={"color": "blue"})
    assert "Matching color: blue -> 2 available, 1 not available." in out.text
    assert "item_9612497925 | $50.88" in out.text   # blue M
    assert "color: red" not in out.text.split("Matching")[1]  # (not just "red": it's in "ordered")


# 3. case and spelling of names/values don't matter
def test_loose_matching(notes, env):
    a = fv(notes, env, product_name="t shirt", options={"COLOR": "Blue", "Size": "m"})
    b = fv(notes, env, product_name="T-Shirt", options={"color": "blue", "size": "M"})
    assert a.ok and a.text == b.text


# 4a. unknown option name -> real names listed
def test_unknown_option_name(notes, env):
    out = fv(notes, env, product_name="T-Shirt", options={"colour": "blue"})
    assert not out.ok
    assert "T-Shirt has no option 'colour'. Its options are: color (black, blue, purple, red)" in out.text


# 4b. unknown value -> real values listed ("large" -> L)
def test_unknown_value(notes, env):
    out = fv(notes, env, product_name="T-Shirt", options={"color": "blue", "size": "large"})
    assert not out.ok
    assert "T-Shirt has no size 'large'. Values for size: L, M, S, XL, XXL." in out.text


# 5. YOUR EXAMPLE: blue + L doesn't exist -> closest available shown
def test_blue_large_closest(notes, env):
    out = fv(notes, env, product_name="T-Shirt", options={"color": "blue", "size": "L"})
    assert out.ok
    assert "No variant has color: blue, size: L." in out.text
    assert "Closest available (differ in one option):" in out.text
    assert "item_9612497925 | $50.88 | color: blue, size: M" in out.text
    assert "<- size: M instead of L" in out.text
    assert "<- color: red instead of blue" in out.text   # red L also exists


# 6. task 1: clicky + RGB + full size exists but is NOT available
def test_keyboard_unavailable(notes, env):
    out = fv(notes, env, product_id="product_1656367028",
             options={"switch type": "clicky", "backlight": "RGB", "size": "full size"})
    assert "-> 0 available, 1 not available." in out.text
    assert "No AVAILABLE variant has switch type: clicky, backlight: RGB, size: full size." in out.text
    assert "item_9025753381" in out.text.split("Not available")[1]
    assert "Closest available" in out.text


# 7. task 7: low-brightness desk lamps, cheapest first
def test_desk_lamp_low(notes, env):
    out = fv(notes, env, product_name="desk lamp", options={"brightness": "low"})
    assert "-> 3 available, 0 not available." in out.text
    lines = [l for l in out.text.splitlines() if l.startswith("- item_")]
    assert "item_1569765161 | $143.02" in lines[0] and "AC adapter" in lines[0]
    assert "item_7453605304 | $150.01" in lines[1] and "battery" in lines[1]
    assert "item_9190635437 | $153.23" in lines[2] and "USB" in lines[2]


# 8. checks before any store call
def test_needs_verified_customer(env):
    out = run_now(find_variants(Notes(), product_name="T-Shirt"), env)
    assert not out.ok and "Verify the customer first" in out.text and out.calls == []


def test_unseen_product_id(notes, env):
    out = fv(notes, env, product_id="product_9999999999")
    assert not out.ok and "has not appeared" in out.text and out.calls == []


def test_item_id_given_as_product(notes, env):
    out = fv(notes, env, product_id="item_1151293680")
    assert not out.ok and "is an item ID, but a product ID is needed" in out.text and out.calls == []


def test_unknown_product_name(notes, env):
    out = fv(notes, env, product_name="Hoverboard")
    assert not out.ok and "No product called 'Hoverboard'. Products are: Action Camera" in out.text


# 9. by name == by ID; and IDs shown become usable
def test_name_same_as_id(notes, env):
    by_name = fv(notes, env, product_name="mechanical keyboard")
    by_id = fv(notes, env, product_id="product_1656367028")
    assert by_name.text == by_id.text
    assert [c.name for c in by_name.calls] == ["list_all_product_types", "get_product_details"]
    assert [c.name for c in by_id.calls] == ["get_product_details"]


def test_shown_ids_become_seen(notes, env):
    with pytest.raises(Exception):
        unlabel(ITEM, "item_9612497925", notes.seen)
    fv(notes, env, product_name="T-Shirt", options={"color": "blue"})
    assert unlabel(ITEM, "item_9612497925", notes.seen) == "9612497925"
    assert unlabel(PRODUCT, "product_9523456873", notes.seen) == "9523456873"
