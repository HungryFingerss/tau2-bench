"""F19: the second check on a customer's yes. No Ollama: the checker's model is faked.

The smoke run of 6 Nov failed on this reply. The code's rule refused it (the "don't" in
"I don't have any other items"), the agent asked again, and the customer left.
"""
from types import SimpleNamespace

import pytest

from fde_agent.agent import DEFAULT_MODEL, FdeAgent
from fde_agent.driver import Decision, Driver, Say, StoreCalls
from fde_agent.store import run_now
from fde_agent.tests.test_confirm_change import cancel_tshirt, env, read, writes, yusuf  # noqa: F401
from fde_agent.tests.test_driver import YUSUF, ScriptedModel, until_say
from fde_agent.tools.confirm_change import confirm_change, is_clear_yes
from fde_agent.yes_check import CALL_NAME, INSTRUCTION, YesCheck, YesChecker, read_answer

SMOKE_REPLY = ("Yeah, that sounds right. I don't have any other items in that order I need to "
               "swap out. So yeah, if you can go ahead and get that started, I'd appreciate it.")


class FakeChecker:
    """Plays the yes-check model: answers YES or NO, remembers what it was shown."""
    def __init__(self, answer="YES"):
        self.answer = answer
        self.asked = []

    def check(self, summary, reply):
        self.asked.append((summary, reply))
        return YesCheck(read_answer(self.answer), self.answer,
                        model_call={"generation_time_seconds": 1.25,
                                    "usage": {"prompt_tokens": 300, "completion_tokens": 2},
                                    "cost": 0.0})


# ---------- reading the model's answer ----------

@pytest.mark.parametrize("answer", ["YES", "yes", "Yes.", " YES \n", "YES!"])
def test_yes_answers(answer):
    assert read_answer(answer) is True


@pytest.mark.parametrize("answer", ["NO", "no.", "Probably yes", "YES, they agreed",
                                    "Yes and no", "", None, "Y"])
def test_everything_else_is_no(answer):
    assert read_answer(answer) is False


# ---------- the checker's one model call ----------

def test_checker_sees_only_summary_and_reply():
    seen = {}

    def generate(**kw):
        seen.update(kw)
        return SimpleNamespace(content="YES", generation_time_seconds=0.8,
                               usage={"prompt_tokens": 250, "completion_tokens": 1}, cost=0.0)

    out = YesChecker("ollama_chat/gpt-oss:20b", llm_args={"seed": 300}, generate_fn=generate).check(
        "Cancel order #W1.\nShall I go ahead?", SMOKE_REPLY)
    assert out.agreed is True and out.answer == "YES" and out.error is None
    assert out.model_call == {"generation_time_seconds": 0.8,
                              "usage": {"prompt_tokens": 250, "completion_tokens": 1}, "cost": 0.0}
    system, user = seen["messages"]                       # NO conversation: just these two
    assert system.content == INSTRUCTION
    assert "Cancel order #W1." in user.content and SMOKE_REPLY in user.content
    assert seen["call_name"] == CALL_NAME == "agent_yes_check"
    assert seen["temperature"] == 0.0 and seen["seed"] == 300
    assert "tools" not in seen


def test_model_failure_is_a_no():
    def generate(**kw):
        raise TimeoutError("Ollama took too long")

    out = YesChecker("m", generate_fn=generate).check("summary", "yes I guess")
    assert out.agreed is False and out.answer is None and out.model_call is None
    assert out.error == "TimeoutError: Ollama took too long"


# ---------- inside confirm_change ----------

def test_smoke_reply_is_refused_by_the_code_rule():
    assert is_clear_yes(SMOKE_REPLY) is False         # why F19 exists


def test_code_no_then_checker_yes_makes_the_change(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said(SMOKE_REPLY)
    asked = []
    out = run_now(confirm_change(yusuf, change_number=n,
                                 yes_checker=lambda s, r: asked.append((s, r)) or True), env)
    assert out.ok and [c.name for c in writes(out)] == ["cancel_pending_order"]
    [(summary, reply)] = asked                         # asked once
    assert reply == SMOKE_REPLY and summary.endswith("as soon as you say yes.")   # what the customer saw
    assert read(env, "get_order_details", order_id="#W6247578")["status"] == "cancelled"


def test_code_yes_never_asks_the_checker(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("Yes, go ahead.")
    asked = []
    out = run_now(confirm_change(yusuf, change_number=n,
                                 yes_checker=lambda s, r: asked.append(r) or False), env)
    assert out.ok and asked == []                      # the model is NOT called


def test_checker_no_means_ask_again(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said("No, wait, make it blue instead.")
    out = run_now(confirm_change(yusuf, change_number=n, yes_checker=lambda s, r: False), env)
    assert not out.ok and "was not a clear yes" in out.text and writes(out) == []
    assert read(env, "get_order_details", order_id="#W6247578")["status"] == "pending"


def test_only_true_counts(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said(SMOKE_REPLY)
    out = run_now(confirm_change(yusuf, change_number=n, yes_checker=lambda s, r: "YES"), env)
    assert not out.ok and writes(out) == []


def test_without_a_checker_only_the_rule_decides(yusuf, env):
    n = cancel_tshirt(yusuf, env)
    yusuf.mark_shown(n)
    yusuf.customer_said(SMOKE_REPLY)
    out = run_now(confirm_change(yusuf, change_number=n), env)
    assert not out.ok and writes(out) == []


def test_the_model_cannot_pass_a_checker():
    from fde_agent.model_client import _Rejected, check_args
    with pytest.raises(_Rejected, match="has no input 'yes_checker'"):   # rejected before it runs
        check_args("confirm_change", {"change_number": 1, "yes_checker": "x"})


# ---------- through the driver: the whole smoke-run moment ----------

def driver_at_summary(env, checker, *after):
    """Verify Yusuf, prepare cancelling his pending order; code shows the summary."""
    model = ScriptedModel(
        Decision.call("verify_customer", **YUSUF),
        Decision.call("get_order", order_id="#W6247578"),
        Decision.call("prepare_change", kind="cancel_order", order_id="#W6247578",
                      reason="no longer needed"),
        *after)
    d = Driver(model, yes_checker=checker)
    said, _ = until_say(d, env, d.on_customer("Hi, I'm Yusuf Rossi, zip 19122. Cancel my pending order."))
    assert isinstance(said, Say) and "Shall I go ahead?" in said.text
    return d, said


def test_driver_smoke_reply_goes_through(env):
    checker = FakeChecker("YES")
    d, summary = driver_at_summary(env, checker, Decision.call("confirm_change", change_number=1))
    d.turn_model_calls = []
    said, rounds = until_say(d, env, d.on_customer(SMOKE_REPLY))
    assert ["cancel_pending_order"] in rounds
    assert said.text.startswith("Done. This change is made:")      # by code (F22)
    assert checker.asked == [(summary.text, SMOKE_REPLY)]   # word for word what the customer saw
    [check] = [e for e in d.log if e["kind"] == "yes_check"]
    assert check["reply"] == SMOKE_REPLY and check["answer"] == "YES" and check["agreed"] is True
    assert check["error"] is None and check["generation_time_seconds"] == 1.25
    assert check["usage"] == {"prompt_tokens": 300, "completion_tokens": 2}
    assert {"generation_time_seconds": 1.25, "usage": {"prompt_tokens": 300, "completion_tokens": 2},
            "cost": 0.0} in d.turn_model_calls          # counted in this turn's cost
    order = [(e["kind"], e.get("tool")) for e in d.log]
    confirm_at = order.index(("tool_call", "confirm_change"))
    assert order[confirm_at + 1] == ("yes_check", None)              # checked first...
    assert order[confirm_at + 2] == ("store_calls", "confirm_change")  # ...then the write


def test_driver_clear_yes_logs_no_check(env):
    checker = FakeChecker("YES")
    d, _ = driver_at_summary(env, checker, Decision.call("confirm_change", change_number=1),
                             Decision.reply("Done."))
    _, rounds = until_say(d, env, d.on_customer("Yes, go ahead."))
    assert ["cancel_pending_order"] in rounds
    assert checker.asked == [] and not [e for e in d.log if e["kind"] == "yes_check"]


def test_driver_checker_no_goes_back_to_the_customer(env):
    checker = FakeChecker("NO")
    d, _ = driver_at_summary(env, checker, Decision.call("confirm_change", change_number=1),
                             Decision.reply("Just to check: shall I cancel it?"))
    said, rounds = until_say(d, env, d.on_customer("Hmm, let me think about it."))
    assert rounds == [] and said.text == "Just to check: shall I cancel it?"
    [check] = [e for e in d.log if e["kind"] == "yes_check"]
    assert check["agreed"] is False and check["answer"] == "NO"
    [result] = [e for e in d.log if e["kind"] == "tool_result" and e["tool"] == "confirm_change"]
    assert result["ok"] is False and "was not a clear yes" in result["text"]


def test_driver_checker_failure_is_logged_as_no(env):
    class Down:
        def check(self, summary, reply):
            return YesCheck(False, None, error="TimeoutError: too slow")
    d, _ = driver_at_summary(env, Down(), Decision.call("confirm_change", change_number=1),
                             Decision.reply("Shall I cancel it?"))
    _, rounds = until_say(d, env, d.on_customer(SMOKE_REPLY))
    assert rounds == []
    [check] = [e for e in d.log if e["kind"] == "yes_check"]
    assert check["agreed"] is False and check["error"] == "TimeoutError: too slow"
    assert "generation_time_seconds" not in check      # no model call to link to


# ---------- the agent builds the checker ----------

def test_agent_checker_uses_the_agents_model_and_seed():
    agent = FdeAgent(tools=[], domain_policy="")
    agent.set_seed(626729)
    checker = agent._make_checker()
    assert isinstance(checker, YesChecker) and checker.model == DEFAULT_MODEL
    assert checker.llm_args["seed"] == 626729


def test_faked_model_gets_no_real_checker():
    agent = FdeAgent(tools=[], domain_policy="", model_client_factory=lambda: None)
    assert agent._make_checker() is None
