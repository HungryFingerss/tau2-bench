"""transfer_to_human against the REAL retail store. No model."""
import pytest

from fde_agent.notes import Notes
from fde_agent.store import run_now
from fde_agent.tools.confirm_change import confirm_change
from fde_agent.tools.get_order import get_order
from fde_agent.tools.prepare_change import prepare_change
from fde_agent.tools.transfer_to_human import TRANSFER_MESSAGE, transfer_to_human
from fde_agent.tools.verify_customer import verify_customer


@pytest.fixture
def env():
    from tau2.domains.retail.environment import get_environment
    return get_environment()


@pytest.fixture
def raj(env):
    n = Notes()
    assert run_now(verify_customer(n, first_name="Raj", last_name="Sanchez", zip="92147"), env).ok
    assert run_now(get_order(n, order_id="#W7736708"), env).ok
    return n


def tr(notes, env, summary="Customer wants a refund to a different card, which is not allowed."):
    out = run_now(transfer_to_human(notes, summary=summary), env)
    print("\n" + out.text)
    return out


def prepare_return(notes, env) -> int:
    out = run_now(prepare_change(notes, kind="return_items", order_id="#W7736708",
                                 item_ids=["item_6574183535"], payment_method_id="credit_card_3362387"), env)
    assert out.ok, out.text
    return notes.prepared.number


# 1. a normal transfer
def test_transfer(raj, env):
    out = tr(raj, env)
    assert out.ok
    assert [c.name for c in out.calls] == ["transfer_to_human_agents"]
    assert TRANSFER_MESSAGE in out.text and "The conversation is over." in out.text
    assert raj.transferred


# 2. only once
def test_only_once(raj, env):
    assert tr(raj, env).ok
    again = tr(raj, env)
    assert not again.ok and "transferred to a human agent" in again.text and again.calls == []


# 3. task 50: no verification needed
def test_unverified_customer(env):
    n = Notes()
    out = tr(n, env, summary="Customer wants to undo a cancelled order.")
    assert out.ok
    assert out.calls[0].args == {"summary": "Customer wants to undo a cancelled order."}


# 4. empty summary
def test_empty_summary(raj, env):
    out = tr(raj, env, summary="   ")
    assert not out.ok and "Give a short summary" in out.text and out.calls == []


# 5. verified customer is added to the summary
def test_summary_names_customer(raj, env):
    out = tr(raj, env, summary="Wants a refund to another card.")
    assert out.calls[0].args["summary"] == "Customer: Raj Sanchez (raj_sanchez_2970). Wants a refund to another card."


# 6. YOUR STORY (option A): unfinished change -> one reminder, then allowed
def test_unfinished_change_reminder_once(raj, env):
    n = prepare_return(raj, env)
    raj.mark_shown(n)
    first = tr(raj, env)
    assert not first.ok and first.calls == []
    assert "Wait: change #1 (Return from order #W7736708:) is prepared but not done." in first.text
    assert not raj.transferred and raj.prepared is not None   # nothing lost yet
    second = tr(raj, env)
    assert second.ok and "Change #1 was dropped (not done)." in second.text
    assert raj.transferred and raj.prepared is None


# 6b. ...or the customer still wants it: confirm first, then transfer with no reminder
def test_finish_then_transfer(raj, env):
    n = prepare_return(raj, env)
    raj.mark_shown(n)
    assert not tr(raj, env).ok               # the reminder
    raj.customer_said("Yes, do the return.")
    assert run_now(confirm_change(raj, change_number=n), env).ok
    out = tr(raj, env)
    assert out.ok and "dropped" not in out.text


# 7. after a transfer every tool refuses
def test_tools_refuse_after_transfer(raj, env):
    assert tr(raj, env).ok
    for out in (run_now(get_order(raj, order_id="#W7736708"), env),
                run_now(prepare_change(raj, kind="cancel_order", order_id="#W7736708",
                                       reason="no longer needed"), env)):
        assert not out.ok and "transferred to a human agent" in out.text and out.calls == []
