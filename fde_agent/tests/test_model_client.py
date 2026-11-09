"""The model client's checks, with a FAKE generate() that returns replies shaped
exactly like the real one, including the baseline's real bad tool calls. No AI model."""
import json

import pytest
from tau2.data_model.message import AssistantMessage, ToolCall

from fde_agent.driver import Driver, Say, StoreCalls
from fde_agent.model_client import ModelClient, ModelUnavailable, parse, to_messages
from fde_agent.store import StoreResult

YUSUF = {"first_name": "Yusuf", "last_name": "Rossi", "zip": "19122"}


def tool_reply(*calls, text=None):
    """A raw reply with tool calls, as generate() returns it. calls: (name, args)."""
    return AssistantMessage(role="assistant", content=text, tool_calls=[
        ToolCall(id=f"raw-{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)])


def text_reply(text):
    return AssistantMessage(role="assistant", content=text)


class FakeGenerate:
    """Returns (or raises) the scripted replies in order; remembers every request."""
    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, **kwargs):
        self.requests.append(kwargs)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(*replies):
    return ModelClient("fake", "You are a store agent.", generate_fn=FakeGenerate(*replies), retry_wait=0)


@pytest.fixture
def env():
    from tau2.domains.retail.environment import get_environment
    return get_environment()


def run_store(env, out):
    from tau2.data_model.message import ToolCall as TC
    res = {}
    for i, c in out.calls.items():
        r = env.get_response(TC(id=i, name=c.name, arguments=c.args))
        res[i] = StoreResult(r.content, r.error)
    return res


def until_say(d, env, out):
    rounds = 0
    while isinstance(out, StoreCalls):
        rounds += 1
        out = d.on_store_results(run_store(env, out))
    return out, rounds


# 1. the baseline's invented tool -> rejected, our tools listed, nothing runs
def test_invented_tool_name():
    d = parse(tool_reply(("modify_delivered_order_items", {"order_id": "#W1"})), [])
    assert d.tool is None and "a tool called 'modify_delivered_order_items', which doesn't exist" in d.invalid
    assert "verify_customer, get_order, find_variants" in d.invalid


# 2. the baseline's empty tool name
def test_empty_tool_name():
    d = parse(tool_reply(("", {})), [])
    assert "a tool call with no name" in d.invalid


# 3. the baseline's leaked junk on a name -> cleaned, logged
def test_leaked_junk_is_cleaned():
    d = parse(tool_reply(("prepare_change<|channel|>commentary", {"kind": "cancel_order"})), [])
    assert d.tool == "prepare_change" and d.invalid is None
    assert "tool name 'prepare_change<|channel|>commentary' -> 'prepare_change'" in d.fixes


def test_junk_that_leaves_no_real_tool_is_rejected():
    d = parse(tool_reply(("modify_delivered_order_items<|channel|>commentary", {})), [])
    assert d.invalid and d.tool is None


# 4. text AND a tool call (5 times in the baseline) -> the tool runs, the text is dropped
def test_text_with_tool_call():
    d = parse(tool_reply(("verify_customer", YUSUF), text="Let me look that up for you."), [])
    assert d.tool == "verify_customer" and d.say is None
    assert any("dropped text sent with a tool call" in f for f in d.fixes)


# 5. two tool calls -> only the first, and the model is told
def test_two_calls_only_first_runs():
    d = parse(tool_reply(("verify_customer", YUSUF), ("get_order", {"order_id": "#W2378156"})), [])
    assert d.tool == "verify_customer"
    assert "These calls were not run: get_order" in d.note


# 6. broken JSON inside generate() -> rejected, not retried, not a crash
def test_broken_json_from_generate():
    c = client(json.JSONDecodeError("Expecting value", "{order_id: ", 0))
    d = c.decide([])
    assert "not a valid JSON object" in d.invalid
    assert len(c.generate.requests) == 1


def test_inputs_not_an_object():
    # generate() itself raises: the benchmark's ToolCall refuses a list as inputs
    try:
        ToolCall(id="x", name="get_order", arguments=["#W2378156"])
    except Exception as e:
        refused = e
    c = client(refused)
    d = c.decide([])
    assert "not a valid JSON object" in d.invalid
    assert len(c.generate.requests) == 1                     # not mistaken for Ollama being down


# 7. wrong / missing input names
def test_unknown_input_name():
    d = parse(tool_reply(("verify_customer", {"first_name": "Yusuf", "last_name": "Rossi", "zipcode": "19122"})), [])
    assert "verify_customer has no input 'zipcode'. Its inputs are: email, first_name, last_name, zip." in d.invalid


def test_missing_required_input():
    d = parse(tool_reply(("get_order", {})), [])
    assert d.invalid == "Error: get_order needs: order_id."


# 8. a number where text is expected -> converted, logged
def test_zip_as_number():
    d = parse(tool_reply(("verify_customer", {"first_name": "Yusuf", "last_name": "Rossi", "zip": 19122})), [])
    assert d.args["zip"] == "19122" and "verify_customer: zip 19122 -> '19122'" in d.fixes


def test_other_safe_conversions():
    d = parse(tool_reply(("confirm_change", {"change_number": "1"})), [])
    assert d.args == {"change_number": 1}
    d = parse(tool_reply(("find_variants", {"product_name": "t shirt", "options": '{"color": "blue"}'})), [])
    assert d.args["options"] == {"color": "blue"}
    d = parse(tool_reply(("prepare_change", {"kind": "return_items", "item_ids": "item_1"})), [])
    assert d.args["item_ids"] == ["item_1"]
    d = parse(tool_reply(("verify_customer", {"email": None, **YUSUF})), [])
    assert "email" not in d.args


# 9. the same get_order twice in a row -> ONE store call
def test_repeat_runs_once(env):
    call = ("get_order", {"order_id": "#W2378156"})
    c = client(tool_reply(("verify_customer", YUSUF)), tool_reply(call), tool_reply(call),
               text_reply("Here are the details."))
    d = Driver(c)
    out, rounds = until_say(d, env, d.on_customer("Yusuf Rossi 19122, order #W2378156 please"))
    assert out == Say("Here are the details.")
    assert rounds == 3 + 1                                   # verify (3) + ONE get_order
    assert "you already called get_order with exactly these inputs" in d.transcript[-2]["text"]   # F27


# 10. Ollama failing: retried; failing for good: a loud stop
def test_model_retried_then_works():
    c = client(ConnectionError("refused"), TimeoutError("slow"), text_reply("Hello!"))
    assert c.decide([]).say == "Hello!" and len(c.generate.requests) == 3


def test_model_fails_for_good():
    c = client(*[ConnectionError("refused")] * 3)
    with pytest.raises(ModelUnavailable, match="failed 3 times: ConnectionError"):
        c.decide([])


# 11. the transcript becomes chat messages with every result linked to its call
def test_transcript_to_messages():
    transcript = [
        {"role": "customer", "text": "Hi"},
        {"role": "tool_call", "id": "tc-1", "tool": "verify_customer", "args": YUSUF},
        {"role": "tool_result", "id": "tc-1", "tool": "verify_customer", "ok": True, "text": "Verified."},
        {"role": "note", "text": "Only one tool runs at a time."},
        {"role": "agent", "text": "Found you."},
    ]
    m = to_messages("SYSTEM", transcript)
    assert [type(x).__name__ for x in m] == ["SystemMessage", "UserMessage", "AssistantMessage",
                                             "ToolMessage", "SystemMessage", "AssistantMessage"]
    assert m[2].tool_calls[0].id == "tc-1" and m[3].id == "tc-1" and m[3].content == "Verified."


def test_generate_gets_our_tools_and_settings():
    c = client(text_reply("Hi"))
    c.decide([{"role": "customer", "text": "hello"}])
    req = c.generate.requests[0]
    assert [t.name for t in req["tools"]] == ["verify_customer", "get_order", "find_variants",
                                              "find_replacement", "prepare_change", "confirm_change",
                                              "transfer_to_human"]
    assert req["call_name"] == "agent_response" and req["temperature"] == 0.0
    assert req["tools"][0].openai_schema["function"]["name"] == "verify_customer"


# end to end: the baseline's real mistakes, then a good call, through the driver and the real store
def test_bad_calls_never_reach_the_store(env):
    c = client(tool_reply(("modify_delivered_order_items", {})),
               tool_reply(("", {})),
               tool_reply(("verify_customer<|channel|>commentary", {**YUSUF, "zip": 19122}),
                          text="One moment."),
               text_reply("Thanks, I found your account."))
    d = Driver(c)
    out, rounds = until_say(d, env, d.on_customer("Yusuf Rossi, 19122"))
    assert out == Say("Thanks, I found your account.") and rounds == 3
    kinds = [e["kind"] for e in d.log]
    assert kinds.count("invalid_call") == 2 and kinds.count("fixed") == 3
    store_names = [c["name"] for e in d.log if e["kind"] == "store_calls" for c in e["calls"].values()]
    assert "modify_delivered_order_items" not in store_names and "" not in store_names
