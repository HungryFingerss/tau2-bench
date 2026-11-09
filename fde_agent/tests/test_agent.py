"""Piece 6, part A: our agent inside the benchmark. No AI model: a scripted model
plays the AI. The last test runs a whole conversation through the benchmark's REAL
orchestrator, with a scripted customer, so we know the benchmark accepts our messages."""
import json

import pytest
from tau2.data_model.message import AssistantMessage, MultiToolMessage, ToolMessage, UserMessage

from fde_agent.agent import LOG_FILE, FdeAgent, create_fde_agent
from fde_agent.driver import Decision
from fde_agent.model_client import ModelClient
from fde_agent.tests.test_driver import YUSUF, ScriptedModel

GREETING = AssistantMessage(role="assistant", content="Hi! How can I help you today?")
CANCEL = "Hi, I'm Yusuf Rossi, zip 19122. Please cancel order #W6247578, I don't need it anymore."


@pytest.fixture
def env():
    from tau2.domains.retail.environment import get_environment
    return get_environment()


def agent_with(*decisions):
    model = ScriptedModel(*decisions)
    return FdeAgent(tools=[], domain_policy="", model_client_factory=lambda: model), model


def answer(env, msg: AssistantMessage):
    """What the benchmark does with our tool calls: run them, wrap the results."""
    results = [env.get_response(c) for c in msg.tool_calls]
    return results[0] if len(results) == 1 else MultiToolMessage(role="tool", tool_messages=results)


# 1. the greeting goes into the transcript, marked as the benchmark's
def test_greeting_is_remembered():
    agent, _ = agent_with()
    state = agent.get_init_state(message_history=[GREETING])
    assert state.driver.transcript == [{"role": "agent", "text": "Hi! How can I help you today?"}]
    assert state.driver.log[0]["by"] == "benchmark"


def test_resuming_a_conversation_is_refused():
    agent, _ = agent_with()
    with pytest.raises(NotImplementedError):
        agent.get_init_state(message_history=[GREETING, UserMessage(role="user", content="hi")])


# 2. our store calls become tool calls with the same IDs; results come back by ID
def test_round_trip_through_benchmark_messages(env):
    agent, model = agent_with(Decision.call("verify_customer", **YUSUF), Decision.reply("Found you."))
    state = agent.get_init_state([GREETING])
    msg, state = agent.generate_next_message(UserMessage(role="user", content="Yusuf Rossi 19122"), state)
    assert msg.content is None and [c.name for c in msg.tool_calls] == ["find_user_id_by_name_zip"]
    assert msg.tool_calls[0].id == "fde-1" and msg.tool_calls[0].requestor == "assistant"
    msg.validate()                                                   # what the orchestrator checks

    msg, state = agent.generate_next_message(answer(env, msg), state)         # single ToolMessage
    msg, state = agent.generate_next_message(answer(env, msg), state)
    assert len(msg.tool_calls) == 5                                  # the 5 orders, one turn
    results = answer(env, msg)
    reversed_results = MultiToolMessage(role="tool", tool_messages=list(reversed(results.tool_messages)))
    msg, state = agent.generate_next_message(reversed_results, state)  # matched by ID, not order
    assert msg.content == "Found you." and not msg.tool_calls
    msg.validate()
    assert state.driver.notes.orders["#W2378156"] == "delivered"


# 3. tokens are summed over the turn; the timing link is set only for model-written messages
def test_usage_and_timing_link(env):
    calls = iter([
        AssistantMessage(role="assistant", content=None, generation_time_seconds=1.5,
                         usage={"prompt_tokens": 100, "completion_tokens": 10}, tool_calls=[
                             __import__("tau2.data_model.message", fromlist=["ToolCall"]).ToolCall(
                                 id="r1", name="verify_customer", arguments={"first_name": "Yusuf",
                                                                             "last_name": "Rossi"})]),
        AssistantMessage(role="assistant", content="What's your zip code?", generation_time_seconds=2.5,
                         usage={"prompt_tokens": 120, "completion_tokens": 8}),
    ])
    client = ModelClient("fake", generate_fn=lambda **k: next(calls), retry_wait=0)
    agent = FdeAgent(tools=[], domain_policy="", model_client_factory=lambda: client)
    state = agent.get_init_state([GREETING])
    msg, state = agent.generate_next_message(UserMessage(role="user", content="I'm Yusuf Rossi"), state)
    assert msg.content == "What's your zip code?"
    assert msg.usage == {"prompt_tokens": 220, "completion_tokens": 18}   # both calls of the turn
    assert msg.generation_time_seconds == 2.5                         # the call that wrote it
    kinds = [e["kind"] for e in state.driver.log]
    assert kinds.count("model_call") == 2


def test_code_written_messages_have_no_model_link(env):
    agent, _ = agent_with(Decision.call("verify_customer", **YUSUF),
                          Decision.call("prepare_change", kind="cancel_order", order_id="#W6247578",
                                        reason="no longer needed"))
    state = agent.get_init_state([GREETING])
    msg, state = agent.generate_next_message(UserMessage(role="user", content=CANCEL), state)
    while msg.tool_calls:
        msg, state = agent.generate_next_message(answer(env, msg), state)
    assert msg.content.startswith("Here's the change I'll make. Nothing has changed yet.\nCancel order #W6247578")           # the summary, from code
    assert msg.generation_time_seconds is None
    last_said = [e for e in state.driver.log if e["kind"] == "agent"][-1]
    assert last_said["by"] == "code" and last_said["text"] == msg.content


# 4. the log is written next to the benchmark's model-call files, and only there
def test_log_written_to_the_conversation_folder(env, tmp_path):
    from tau2.utils.llm_utils import set_llm_log_dir
    agent, _ = agent_with(Decision.reply("Hello! How can I help?"))
    state = agent.get_init_state([GREETING])
    set_llm_log_dir(tmp_path / "llm_debug")
    try:
        agent.generate_next_message(UserMessage(role="user", content="hi"), state)
    finally:
        set_llm_log_dir(None)
    data = json.loads((tmp_path / LOG_FILE).read_text())
    assert data["agent"] == "fde_agent" and len(data["instructions_sha256"]) == 64
    assert [e["kind"] for e in data["events"]] == ["agent", "customer", "agent"]
    assert not list(tmp_path.glob(".fde_log_*"))                      # no temp file left behind


def test_no_log_folder_no_file(env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent, _ = agent_with(Decision.reply("Hello!"))
    state = agent.get_init_state([GREETING])
    agent.generate_next_message(UserMessage(role="user", content="hi"), state)
    assert not list(tmp_path.rglob(LOG_FILE))


# 5. the runner's seed and the run's model settings reach every model call
def test_seed_and_settings_reach_the_model():
    seen = []
    agent = create_fde_agent([], "", llm="ollama_chat/gpt-oss:20b", llm_args={"temperature": 0.0})
    agent.set_seed(300)
    state = agent.get_init_state([GREETING])
    state.driver.model.generate = lambda **k: seen.append(k) or AssistantMessage(role="assistant", content="Hi")
    agent.generate_next_message(UserMessage(role="user", content="hi"), state)
    assert seen[0]["seed"] == 300 and seen[0]["temperature"] == 0.0
    assert seen[0]["model"] == "ollama_chat/gpt-oss:20b" and seen[0]["call_name"] == "agent_response"


# 6. a whole conversation through the benchmark's REAL orchestrator
def test_whole_conversation_in_the_real_orchestrator(env):
    from tau2.orchestrator.orchestrator import Orchestrator
    from tau2.runner import get_tasks
    from tau2.user.user_simulator_base import STOP, HalfDuplexUser

    class ScriptedCustomer(HalfDuplexUser):
        def __init__(self, lines):
            super().__init__()
            self.lines = list(lines)

        def get_init_state(self, message_history=None):
            return {}

        def generate_next_message(self, message, state):
            return UserMessage(role="user", content=self.lines.pop(0)), state

        @classmethod
        def is_stop(cls, message):
            return STOP in (message.content or "")

    agent, model = agent_with(Decision.call("verify_customer", **YUSUF),
                              Decision.call("prepare_change", kind="cancel_order",
                                            order_id="#W6247578", reason="no longer needed"),
                              Decision.call("confirm_change", change_number=1))   # code says "done"
    task = get_tasks("retail", task_ids=["0"])[0]
    run = Orchestrator(domain="retail", agent=agent, user=ScriptedCustomer([CANCEL, "Yes.", STOP]),
                       environment=env, task=task, max_steps=200, max_errors=10, seed=300).run()

    texts = [m.content for m in run.messages if getattr(m, "role", "") == "assistant" and m.content]
    assert texts[0] == "Hi! How can I help you today?"
    assert texts[1].startswith("Here's the change I'll make. Nothing has changed yet.\nCancel order #W6247578")              # summary, by code
    assert texts[2].startswith("Done. This change is made:\nCancel order #W6247578")   # by code
    assert texts[2].endswith("Order #W6247578 is now: cancelled.")
    writes = [c.name for m in run.messages if getattr(m, "tool_calls", None) for c in m.tool_calls
              if c.name == "cancel_pending_order"]
    assert writes == ["cancel_pending_order"]
    tool_errors = [m for m in run.messages if getattr(m, "role", "") == "tool" and m.error]
    assert tool_errors == []
    assert "user_stop" in str(run.termination_reason).lower()
    status = json.loads(env.get_response(__import__("tau2.data_model.message", fromlist=["ToolCall"]).ToolCall(
        id="x", name="get_order_details", arguments={"order_id": "#W6247578"})).content)["status"]
    assert status == "cancelled"


# 7. Windows: the viewer may hold the log open while we rewrite it -> retry, never crash
def _turn_with_log(tmp_path, monkeypatch, fail_times):
    import os as _os
    from tau2.utils.llm_utils import set_llm_log_dir
    import fde_agent.agent as agent_module
    real_replace, calls = _os.replace, {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(agent_module.os, "replace", flaky_replace)
    monkeypatch.setattr(agent_module, "REPLACE_WAIT", 0)
    agent, _ = agent_with(Decision.reply("Hello!"))
    state = agent.get_init_state([GREETING])
    set_llm_log_dir(tmp_path / "llm_debug")
    try:
        msg, _ = agent.generate_next_message(UserMessage(role="user", content="hi"), state)
    finally:
        set_llm_log_dir(None)
    return msg, calls["n"]


def test_log_busy_for_a_moment_is_retried(tmp_path, monkeypatch):
    msg, tries = _turn_with_log(tmp_path, monkeypatch, fail_times=3)
    assert msg.content == "Hello!" and tries == 4
    assert (tmp_path / LOG_FILE).exists() and not list(tmp_path.glob(".fde_log_*"))


def test_log_busy_for_good_never_ends_the_conversation(tmp_path, monkeypatch):
    msg, tries = _turn_with_log(tmp_path, monkeypatch, fail_times=999)
    assert msg.content == "Hello!"                        # the conversation goes on
    assert not (tmp_path / LOG_FILE).exists() and not list(tmp_path.glob(".fde_log_*"))
