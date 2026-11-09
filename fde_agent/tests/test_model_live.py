"""LIVE tests: the real gpt-oss:20b with our tools and our instructions (instructions.md).

Needs Ollama running with gpt-oss:20b, so they are SKIPPED unless FDE_LIVE=1.
On Windows (PowerShell), from the repo root:
    $env:FDE_LIVE=1; uv run --with pytest python -m pytest fde_agent/tests/test_model_live.py -s; Remove-Item Env:FDE_LIVE
Each scenario 3 times, with a pass rate per scenario at the end:
    $env:FDE_LIVE=1; $env:FDE_REPEAT=3; uv run --with pytest python -m pytest fde_agent/tests/test_model_live.py -s; Remove-Item Env:FDE_LIVE, Env:FDE_REPEAT

Each test is one customer message (one turn): small pass/fail evals for the
judgement the instructions are responsible for; test 5 is a whole change in two
turns. Takes ~30 s to 2 minutes each on a laptop.
"""
import os
import re

import pytest

from fde_agent.driver import Driver, Say, StoreCalls
from fde_agent.model_client import ModelClient
from fde_agent.store import StoreResult

pytestmark = pytest.mark.skipif(os.environ.get("FDE_LIVE") != "1", reason="set FDE_LIVE=1 to run")

# Ollama at temperature 0 is NOT fully repeatable: the same scenario can pass one day and
# fail the next. FDE_REPEAT=3 runs every scenario 3 times; the end of the output shows a
# pass rate per scenario ("3 of 3", "1 of 3") instead of one lucky or unlucky result.
REPEAT = max(1, int(os.environ.get("FDE_REPEAT", "1")))


def pytest_generate_tests(metafunc):
    if "run" in metafunc.fixturenames:
        metafunc.parametrize("run", range(1, REPEAT + 1), ids=lambda i: f"run{i}")


@pytest.fixture(autouse=True)
def _announce(request):
    print(f"\n================ {request.node.name} ================")

MODEL = os.environ.get("FDE_MODEL", "ollama_chat/gpt-oss:20b")
HELLO = "Hi, I'm Yusuf Rossi, zip code 19122. "
# Internal IDs the customer must never see: the same check the driver runs (outgoing.py).
from fde_agent.outgoing import INTERNAL_ID  # noqa: E402


def conversation(*customer_texts):
    """The customer says each text in turn; after each, run until the agent answers.
    Prints what happened. Returns (driver, last reply, tool names called, env)."""
    from tau2.data_model.message import ToolCall
    from tau2.domains.retail.environment import get_environment

    env = get_environment()
    d = Driver(ModelClient(MODEL))
    for customer_text in customer_texts:
        out = d.on_customer(customer_text)
        while isinstance(out, StoreCalls):
            results = {}
            for i, c in out.calls.items():
                r = env.get_response(ToolCall(id=i, name=c.name, arguments=c.args))
                results[i] = StoreResult(r.content, r.error)
            out = d.on_store_results(results)
        assert isinstance(out, Say) and out.text.strip()
        assert not INTERNAL_ID.search(out.text), f"internal ID shown to the customer: {out.text}"

    print("\n---------------- what happened ----------------")
    for e in d.log:
        k = e["kind"]
        if k == "tool_call":
            print(f"model calls  {e['tool']}({e['args']})")
        elif k == "tool_result":
            print(f"model sees   [{'ok' if e['ok'] else 'error'}] {e['text'].splitlines()[0][:110]}")
        elif k in ("invalid_call", "fixed", "empty_reply", "safe_message", "tool_crash", "blocked_reply",
                   "yes_waiting", "duplicate_call", "ungrounded_identity", "yes_check"):
            detail = (e.get('error') or e.get('what') or e.get('reason') or e.get('ids') or e.get('tried')
                      or e.get('of') or e.get('values') or e.get('answer') or '')
            print(f"CHECK        {k}: {detail}")
        elif k == "customer":
            print(f"customer     {e['text']}")
        elif k == "agent":
            print(f"agent ({e['by']}) {e['text']}")
        elif k == "store_calls":
            writes = [c["name"] for c in e["calls"].values() if not c["name"].startswith(("get_", "find_", "list_"))]
            if writes:
                print(f"STORE WRITE  {', '.join(writes)}")
    calls = [e["tool"] for e in d.log if e["kind"] == "tool_call"]
    return d, out.text, calls, env


def one_turn(customer_text: str):
    d, reply, calls, _ = conversation(customer_text)
    return d, reply, calls


def test_0_first_contact(run):
    d, reply, calls = one_turn(HELLO + "Can you check my orders?")
    assert calls[0] == "verify_customer"
    assert d.notes.customer.user_id == "yusuf_rossi_9620"


def test_1_user_id_alone_is_not_enough(run):
    d, reply, calls = one_turn("Hi, my user ID is yusuf_rossi_9620. Can you check my orders?")
    assert d.notes.customer is None, "verified without email or name + zip"
    assert re.search(r"email|zip", reply, re.I), "didn't ask for email or name + zip"


def test_2_asks_for_the_cancel_reason(run):
    d, reply, calls = one_turn(HELLO + "Please cancel my t-shirt order.")
    assert "prepare_change" not in calls, "prepared a cancellation without the customer's reason"
    assert d.notes.prepared is None


def test_3_asks_where_the_refund_goes(run):
    d, reply, calls = one_turn(HELLO + "I want to return the mechanical keyboard I received.")
    assert "prepare_change" not in calls, "prepared a return without asking where the refund goes"


def test_4_finds_the_replacement_first(run):
    d, reply, calls = one_turn(HELLO + "I'd like to exchange my mechanical keyboard for one with clicky switches.")
    assert {"find_replacement", "find_variants"} & set(calls), "never looked up the replacement"
    assert "prepare_change" not in calls, "prepared an exchange before the customer chose"


def test_5_cancel_from_start_to_finish(run):
    import json
    from tau2.data_model.message import ToolCall
    d, reply, calls, env = conversation(
        HELLO + "Please cancel order #W6247578, I don't need it anymore.", "Yes.")
    assert "prepare_change" in calls and "confirm_change" in calls
    order = json.loads(env.get_response(ToolCall(id="x", name="get_order_details",
                                                 arguments={"order_id": "#W6247578"})).content)
    assert order["status"] == "cancelled" and order["cancel_reason"] == "no longer needed"
