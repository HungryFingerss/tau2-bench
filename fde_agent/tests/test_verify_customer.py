"""verify_customer against the REAL retail store and database. No model."""
import pytest

from fde_agent.notes import Notes
from fde_agent.store import run_now
from fde_agent.tools.verify_customer import verify_customer


@pytest.fixture
def env():
    from tau2.domains.retail.environment import get_environment
    return get_environment()  # fresh copy of the real DB each test


YUSUF_ORDERS = {
    "#W6247578": "pending",
    "#W9711842": "cancelled",
    "#W4776164": "pending",
    "#W6679257": "delivered",
    "#W2378156": "delivered",
}


# 1. name + zip -> right customer, all 5 orders with correct statuses
def test_name_zip_finds_yusuf_with_all_orders(env):
    notes = Notes()
    out = run_now(verify_customer(notes, first_name="Yusuf", last_name="Rossi", zip="19122"), env)
    print("\n" + out.text)

    assert out.ok
    assert "Verified: Yusuf Rossi (user ID: yusuf_rossi_9620), by name and zip code." in out.text
    assert "Mastercard credit card ending 2478 (payment ID: credit_card_9513926)" in out.text
    for oid, status in YUSUF_ORDERS.items():
        assert f"{oid} | {status} |" in out.text
    assert "yusuf.rossi7301@example.com" not in out.text  # email left out
    # option names shown, so frame vs lens colour is clear
    assert "Sunglasses (frame color: black, lens color: green," in out.text

    assert notes.customer.user_id == "yusuf_rossi_9620"
    assert notes.orders == YUSUF_ORDERS

    # the store calls that will appear in the benchmark's trace
    assert [c.name for c in out.calls] == (
        ["find_user_id_by_name_zip", "get_user_details"] + ["get_order_details"] * 5)


# 2. email -> same customer
def test_email_finds_same_customer(env):
    notes = Notes()
    out = run_now(verify_customer(notes, email="yusuf.rossi7301@example.com"), env)
    assert out.ok
    assert "by email." in out.text
    assert notes.customer.user_id == "yusuf_rossi_9620"
    assert notes.orders == YUSUF_ORDERS
    assert out.calls[0].name == "find_user_id_by_email"


# 3. name is not case-sensitive (and spaces are trimmed)
def test_name_case_and_spaces_ignored(env):
    out = run_now(verify_customer(Notes(), first_name=" YUSUF ", last_name="rossi", zip="19122"), env)
    assert out.ok
    assert "yusuf_rossi_9620" in out.text


# 4. wrong zip -> not found, notes untouched
def test_wrong_zip_not_found(env):
    notes = Notes()
    out = run_now(verify_customer(notes, first_name="Yusuf", last_name="Rossi", zip="10001"), env)
    print("\n" + out.text)
    assert not out.ok
    assert "No customer found with that name and zip code" in out.text
    assert notes.customer is None
    assert len(out.calls) == 1  # stopped after the lookup


# 5. missing zip -> error, NO store call (so no benchmark tool error either)
def test_missing_zip_no_store_call(env):
    out = run_now(verify_customer(Notes(), first_name="Yusuf", last_name="Rossi"), env)
    print("\n" + out.text)
    assert not out.ok
    assert "Missing: zip code" in out.text
    assert out.calls == []


# 6. empty / blank strings count as missing
def test_blank_strings_are_missing(env):
    out = run_now(verify_customer(Notes(), email="  ", first_name="", last_name="", zip=""), env)
    print("\n" + out.text)
    assert not out.ok
    assert "Need the customer's email, or their first name, last name and zip code" in out.text
    assert out.calls == []


# 7. a second, different customer is refused; first stays verified
def test_second_customer_refused(env):
    notes = Notes()
    assert run_now(verify_customer(notes, first_name="Yusuf", last_name="Rossi", zip="19122"), env).ok
    out = run_now(verify_customer(notes, first_name="Noah", last_name="Brown", zip="80279"), env)
    print("\n" + out.text)
    assert not out.ok
    assert "Yusuf Rossi is already verified" in out.text
    assert notes.customer.user_id == "yusuf_rossi_9620"
    assert [c.name for c in out.calls] == ["find_user_id_by_name_zip"]  # never read Noah's account
