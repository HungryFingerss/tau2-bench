"""The driver, with a SCRIPTED model and a fake benchmark that uses the REAL store.
No AI model: the script plays the model, the fake benchmark plays the orchestrator."""
import pytest

from fde_agent.driver import (MAX_FAILED_TRIES, SAFE_MESSAGE, Decision, Driver,
                              DriverError, Say, StoreCalls)
from fde_agent.store import StoreResult


@pytest.fixture
def env():
    from tau2.domains.retail.environment import get_environment
    return get_environment()


class ScriptedModel:
    """Plays the model: returns the scripted decisions in order, remembers what it saw."""
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.seen = []                     # a copy of the transcript at every decision

    def decide(self, transcript):
        self.seen.append([dict(e) for e in transcript])
        return self.decisions.pop(0)


def run_store(env, out: StoreCalls) -> dict:
    """What the benchmark does with our store calls: run each one on the real store."""
    from tau2.data_model.message import ToolCall
    results = {}
    for call_id, c in out.calls.items():
        r = env.get_response(ToolCall(id=call_id, name=c.name, arguments=c.args))
        results[call_id] = StoreResult(r.content, r.error)
    return results


def until_say(driver, env, out):
    """Keep answering store calls until the driver speaks. Returns (Say, rounds)."""
    rounds = []
    while isinstance(out, StoreCalls):
        rounds.append([c.name for c in out.calls.values()])
        out = driver.on_store_results(run_store(env, out))
    return out, rounds


YUSUF = dict(first_name="Yusuf", last_name="Rossi", zip="19122")


# 1. verify_customer = 3 round trips; the 5 orders go out together
def test_verify_takes_three_round_trips(env):
    model = ScriptedModel(Decision.call("verify_customer", **YUSUF),
                          Decision.reply("Thanks Yusuf, I found your account."))
    d = Driver(model)
    out, rounds = until_say(d, env, d.on_customer("Hi, I'm Yusuf Rossi, zip 19122."))
    assert rounds == [["find_user_id_by_name_zip"], ["get_user_details"], ["get_order_details"] * 5]
    assert out == Say("Thanks Yusuf, I found your account.")
    # the model saw the tool's text before it replied
    last_seen = model.seen[-1][-1]
    assert last_seen["role"] == "tool_result" and last_seen["ok"]
    assert "Orders (5):" in last_seen["text"]
    assert d.notes.customer.user_id == "yusuf_rossi_9620"


# 2. a tool that fails before the store costs no benchmark step
def test_missing_zip_costs_no_step(env):
    model = ScriptedModel(Decision.call("verify_customer", first_name="Yusuf", last_name="Rossi"),
                          Decision.reply("Could you tell me your zip code?"))
    d = Driver(model)
    out = d.on_customer("I'm Yusuf Rossi.")
    assert out == Say("Could you tell me your zip code?")          # no StoreCalls at all
    assert "Missing: zip code" in model.seen[-1][-1]["text"]


# 3. failing again and again -> the safe message, no endless loop
def test_failing_calls_stop_at_the_limit(env):
    bad = [Decision.call("verify_customer") for _ in range(MAX_FAILED_TRIES + 3)]
    model = ScriptedModel(*bad)
    d = Driver(model)
    out = d.on_customer("hello")
    assert out == Say(SAFE_MESSAGE)
    assert len(model.seen) == MAX_FAILED_TRIES                     # asked 5 times, not 8
    assert d.log[-2]["kind"] == "safe_message"


# 4. results are matched by call ID, not by position
def test_results_matched_by_id(env):
    model = ScriptedModel(Decision.call("verify_customer", **YUSUF), Decision.reply("ok"))
    d = Driver(model)
    out = d.on_customer("Yusuf Rossi 19122")
    while isinstance(out, StoreCalls):
        results = run_store(env, out)
        reversed_order = dict(reversed(list(results.items())))      # arrive in the opposite order
        out = d.on_store_results(reversed_order)
    statuses = d.notes.orders
    assert statuses["#W6247578"] == "pending" and statuses["#W2378156"] == "delivered"
    assert statuses["#W9711842"] == "cancelled"


# 5. misuse is a loud error, never a guess
def test_customer_message_while_a_tool_waits(env):
    d = Driver(ScriptedModel(Decision.call("verify_customer", **YUSUF)))
    assert isinstance(d.on_customer("Yusuf Rossi 19122"), StoreCalls)
    with pytest.raises(DriverError, match="waiting for store results"):
        d.on_customer("hello?")


def test_results_when_nothing_waits():
    with pytest.raises(DriverError, match="no tool is waiting"):
        Driver(ScriptedModel()).on_store_results({"fde-1": StoreResult("x", False)})


def test_results_for_the_wrong_ids(env):
    d = Driver(ScriptedModel(Decision.call("verify_customer", **YUSUF)))
    d.on_customer("Yusuf Rossi 19122")
    with pytest.raises(DriverError, match="waits for"):
        d.on_store_results({"fde-99": StoreResult("x", False)})


# 6. the log has every internal step, in order
def test_log_records_every_step(env):
    model = ScriptedModel(Decision.call("verify_customer", first_name="Yusuf", last_name="Rossi"),
                          Decision.call("verify_customer", **YUSUF),
                          Decision.reply("Found you."))
    d = Driver(model)
    until_say(d, env, d.on_customer("Yusuf Rossi, 19122"))
    kinds = [e["kind"] for e in d.log]
    assert kinds == ["customer",
                     "tool_call", "tool_result",                       # missing zip, no store
                     "tool_call", "store_calls", "store_results",      # lookup
                     "store_calls", "store_results",                   # account
                     "store_calls", "store_results",                   # 5 orders
                     "tool_result", "agent"]
    assert [e["n"] for e in d.log] == list(range(1, len(d.log) + 1))


# 7. a customer with NO orders: no empty turn is ever sent
def test_customer_without_orders(env):
    model = ScriptedModel(Decision.call("verify_customer", first_name="Yusuf", last_name="Khan", zip="78242"),
                          Decision.reply("Found you."))
    d = Driver(model)
    out, rounds = until_say(d, env, d.on_customer("Yusuf Khan, 78242"))
    assert rounds == [["find_user_id_by_name_zip"], ["get_user_details"]]   # no empty 3rd round
    assert "Orders (0):" in model.seen[-1][-1]["text"]


# 8. unknown tool and wrong argument names go back to the model, no store call
def test_unknown_tool(env):
    model = ScriptedModel(Decision.call("modify_delivered_order_items", order_id="#W1"),
                          Decision.reply("Let me check that."))
    d = Driver(model)
    assert d.on_customer("exchange please") == Say("Let me check that.")
    assert "there is no tool called 'modify_delivered_order_items'" in model.seen[-1][-1]["text"]


def test_wrong_argument_name(env):
    model = ScriptedModel(Decision.call("verify_customer", zipcode="19122"), Decision.reply("?"))
    d = Driver(model)
    d.on_customer("19122")
    assert "wrong inputs for verify_customer" in model.seen[-1][-1]["text"]


# 9. an error AFTER the store (customer not found) resets the failure count
def test_store_error_resets_the_count(env):
    wrong = Decision.call("verify_customer", first_name="Yusuf", last_name="Rossi", zip="10001")
    model = ScriptedModel(*[Decision.call("verify_customer")] * (MAX_FAILED_TRIES - 1), wrong,
                          *[Decision.call("verify_customer")] * (MAX_FAILED_TRIES - 1),
                          Decision.reply("Please check your zip code."))
    d = Driver(model)
    out, rounds = until_say(d, env, d.on_customer("Yusuf Rossi 10001"))
    assert rounds == [["find_user_id_by_name_zip"]]
    assert out == Say("Please check your zip code.")                 # no safe message


# 10. an empty reply is never sent to the customer
def test_empty_reply_is_not_sent(env):
    model = ScriptedModel(Decision.reply("  "), Decision.reply("How can I help?"))
    d = Driver(model)
    assert d.on_customer("hi") == Say("How can I help?")
    assert "your reply was empty" in model.seen[-1][-1]["text"]


# 11. a whole change: prepare -> (store) -> the model replies
def test_prepare_change_round_trip(env):
    model = ScriptedModel(Decision.call("verify_customer", **YUSUF),
                          Decision.call("prepare_change", kind="cancel_order", order_id="#W6247578",
                                        reason="no longer needed"),
                          Decision.reply("Here is the summary."))
    d = Driver(model)
    out, rounds = until_say(d, env, d.on_customer("Yusuf Rossi 19122, cancel #W6247578"))
    assert rounds[-1] == ["get_user_details", "get_order_details"]    # prepare reads in one round
    assert d.notes.prepared.number == 1
    assert all(n not in sum(rounds, []) for n in ("cancel_pending_order",))  # nothing written


# 12. a model that keeps repeating a SUCCESSFUL call still hits the limit
def test_repeats_hit_the_limit(env):
    first = Decision.call("verify_customer", **YUSUF)
    again = Decision(tool="verify_customer", args=YUSUF, repeat=True)
    model = ScriptedModel(first, *[again] * (MAX_FAILED_TRIES + 3))
    d = Driver(model)
    out, rounds = until_say(d, env, d.on_customer("Yusuf Rossi 19122"))
    assert out == Say(SAFE_MESSAGE)
    assert len(rounds) == 3                          # only the first call reached the store
    repeated = [e for e in d.transcript if e["role"] == "tool_result"][1]
    assert "you already called verify_customer with exactly these inputs (tc-1)" in repeated["text"]
    assert "Its result is above: use it." in repeated["text"]      # points to it (F27)


# 13. a call rejected by the model client's checks: a note to the model, never run
def test_invalid_call_becomes_a_note(env):
    model = ScriptedModel(Decision(invalid="Error: there is no tool called 'x'."), Decision.reply("Sorry."))
    d = Driver(model)
    assert d.on_customer("hi") == Say("Sorry.")
    assert model.seen[-1][-1] == {"role": "note", "text": "Error: there is no tool called 'x'.", "failed": True}
    assert [e["kind"] for e in d.log] == ["customer", "invalid_call", "note", "agent"]


# 14. tool calls and their results share an ID
def test_calls_and_results_share_ids(env):
    model = ScriptedModel(Decision.call("verify_customer", first_name="Yusuf", last_name="Rossi"),
                          Decision.call("verify_customer", **YUSUF), Decision.reply("ok"))
    d = Driver(model)
    until_say(d, env, d.on_customer("Yusuf Rossi 19122"))
    pairs = [(e["role"], e["id"]) for e in d.transcript if e["role"] in ("tool_call", "tool_result")]
    assert pairs == [("tool_call", "tc-1"), ("tool_result", "tc-1"),
                     ("tool_call", "tc-2"), ("tool_result", "tc-2")]
