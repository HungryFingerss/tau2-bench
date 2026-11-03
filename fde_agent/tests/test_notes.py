import pytest

from fde_agent.notes import Notes, NotesError


def test_new_notes_are_empty():
    n = Notes()
    assert n.customer is None
    assert n.orders == {}


def test_set_customer():
    n = Notes()
    n.set_customer("yusuf_rossi_9620", "Yusuf Rossi")
    assert n.customer.user_id == "yusuf_rossi_9620"
    assert n.customer.name == "Yusuf Rossi"


def test_same_customer_again_is_fine():
    # e.g. customer gives email after name+zip already worked
    n = Notes()
    n.set_customer("yusuf_rossi_9620", "Yusuf Rossi")
    n.set_customer("yusuf_rossi_9620", "Yusuf Rossi")
    assert n.customer.user_id == "yusuf_rossi_9620"


def test_different_customer_is_refused():
    n = Notes()
    n.set_customer("yusuf_rossi_9620", "Yusuf Rossi")
    with pytest.raises(NotesError, match="Yusuf Rossi is already verified"):
        n.set_customer("someone_else_1234", "Someone Else")
    assert n.customer.user_id == "yusuf_rossi_9620"  # unchanged


def test_orders_need_a_verified_customer():
    with pytest.raises(NotesError, match="Verify the customer"):
        Notes().record_order("#W6247578", "pending")


def test_record_and_update_order_status():
    n = Notes()
    n.set_customer("yusuf_rossi_9620", "Yusuf Rossi")
    n.record_order("#W6247578", "pending")
    n.record_order("#W6247578", "cancelled")  # status changes after a write
    assert n.orders == {"#W6247578": "cancelled"}
    assert n.is_customers_order("#W6247578")
    assert not n.is_customers_order("#W0000000")
