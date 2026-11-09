"""Our agent, as the benchmark sees it: a HalfDuplexAgent around the driver.

The benchmark hands us one message at a time and wants one message back:
    customer's UserMessage            -> driver.on_customer(text)
    store's ToolMessage(s)            -> driver.on_store_results({call id: result})
    driver's StoreCalls               -> an AssistantMessage with those tool calls (same IDs)
    driver's Say                      -> an AssistantMessage with that text
The benchmark's own greeting goes into the transcript first, so the model knows it greeted.

After every turn, our log (fde_agent_log.json) is rewritten next to the benchmark's
model-call files for this conversation, so the trace viewer can show it live.
"""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from tau2.agent.base_agent import HalfDuplexAgent
from tau2.data_model.message import AssistantMessage, MultiToolMessage, ToolCall, ToolMessage, UserMessage

from fde_agent.driver import Driver, Say, StoreCalls
from fde_agent.model_client import ModelClient
from fde_agent.prompt import load_instructions
from fde_agent.store import StoreResult
from fde_agent.yes_check import YesChecker

AGENT_NAME = "fde_agent"
DEFAULT_MODEL = "ollama_chat/gpt-oss:20b"
LOG_FILE = "fde_agent_log.json"


class FdeAgentState:
    def __init__(self, driver: Driver) -> None:
        self.driver = driver


class FdeAgent(HalfDuplexAgent[FdeAgentState]):
    def __init__(self, tools, domain_policy: str, llm: str | None = None, llm_args: dict | None = None,
                 model_client_factory=None, yes_checker_factory=None) -> None:
        super().__init__(tools=tools, domain_policy=domain_policy)   # store tools + policy: unused,
        self.llm = llm or DEFAULT_MODEL                                # our own tools and instructions
        self.llm_args = dict(llm_args or {})
        self._make_client = model_client_factory or (lambda: ModelClient(self.llm, llm_args=self.llm_args))
        # F19's second yes check: same model and settings (seed too) as the agent. A test that
        # fakes the model gets no real checker unless it passes one: it must never reach Ollama.
        if yes_checker_factory is None and model_client_factory is None:
            yes_checker_factory = lambda: YesChecker(self.llm, llm_args=self.llm_args)
        self._make_checker = yes_checker_factory or (lambda: None)

    def set_seed(self, seed: int) -> None:
        """The runner's per-trial seed, passed to every model call (as the baseline does)."""
        self.llm_args["seed"] = seed

    def get_init_state(self, message_history=None) -> FdeAgentState:
        driver = Driver(self._make_client(), yes_checker=self._make_checker())
        for m in message_history or []:
            if isinstance(m, AssistantMessage) and m.content and not m.tool_calls:
                driver.greeted(m.content)
            else:
                raise NotImplementedError(
                    "fde_agent can't resume a conversation that is already under way.")
        return FdeAgentState(driver)

    def generate_next_message(self, message, state: FdeAgentState):
        d = state.driver
        if isinstance(message, UserMessage):
            out = d.on_customer(message.content or "")
        elif isinstance(message, ToolMessage):
            out = d.on_store_results({message.id: StoreResult(message.content or "", message.error)})
        elif isinstance(message, MultiToolMessage):
            out = d.on_store_results({m.id: StoreResult(m.content or "", m.error)
                                      for m in message.tool_messages})
        else:
            raise TypeError(f"fde_agent can't handle a {type(message).__name__}.")
        reply = to_assistant_message(out, d.turn_model_calls)
        d.turn_model_calls = []
        write_log(d, self.llm, self.llm_args)
        return reply, state


def to_assistant_message(out: StoreCalls | Say, model_calls: list[dict]) -> AssistantMessage:
    """Driver output -> the benchmark's message. Tokens and cost are the SUM of every model
    call made during this turn (internal retries and tool calls included), so the run's
    totals are honest."""
    usage, cost = _sum_usage(model_calls), _sum_cost(model_calls)
    common = {"role": "assistant", "usage": usage, "cost": cost,
              "generation_time_seconds": out.generation_time_seconds}
    if isinstance(out, StoreCalls):
        return AssistantMessage(content=None, tool_calls=[
            ToolCall(id=i, name=c.name, arguments=c.args, requestor="assistant")
            for i, c in out.calls.items()], **common)
    return AssistantMessage(content=out.text, **common)


def _sum_usage(calls: list[dict]) -> dict | None:
    total: dict = {}
    for c in calls:
        for k, v in (c.get("usage") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total[k] = total.get(k, 0) + v
    return total or None


def _sum_cost(calls: list[dict]) -> float | None:
    costs = [c["cost"] for c in calls if isinstance(c.get("cost"), (int, float))]
    return sum(costs) if costs else None


def log_folder() -> Path | None:
    """This conversation's artifacts folder: the parent of the benchmark's llm_debug
    folder (set by the runner per conversation, when run with --verbose-logs)."""
    from tau2.utils.llm_utils import llm_log_dir
    folder = llm_log_dir.get()
    return Path(folder).parent if folder else None


REPLACE_TRIES = 10
REPLACE_WAIT = 0.1   # seconds between tries


def write_log(driver: Driver, model: str, llm_args: dict) -> Path | None:
    """Rewrite this conversation's log. NEVER raises: logging must not end a conversation.

    On Windows, replacing a file fails while another program has it open, e.g. the
    trace viewer reading it live at that instant. So: retry for about a second, and
    if it still fails, skip this turn (the next turn rewrites the whole log anyway)."""
    try:
        folder = log_folder()
        if folder is None:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        data = {
            "agent": AGENT_NAME,
            "model": model,
            "llm_args": llm_args,
            "instructions_sha256": hashlib.sha256(load_instructions().encode()).hexdigest(),
            "events": driver.log,
        }
        # write-then-rename, so a viewer reading it mid-run never sees half a file
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".fde_log_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, default=str)
    except Exception as e:                      # e.g. disk full: say so, carry on
        _warn(f"fde_agent log not written: {type(e).__name__}: {e}")
        return None
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, folder / LOG_FILE)
            return folder / LOG_FILE
        except PermissionError:                 # the file is open elsewhere right now
            time.sleep(REPLACE_WAIT)
        except Exception as e:
            _warn(f"fde_agent log not written: {type(e).__name__}: {e}")
            break
    else:
        _warn(f"fde_agent log busy for {REPLACE_TRIES * REPLACE_WAIT:.0f} s; skipped this turn")
    try:
        os.remove(tmp)                          # never leave temp files behind
    except OSError:
        pass
    return None


def _warn(text: str) -> None:
    try:
        from loguru import logger
        logger.warning(text)
    except Exception:
        pass


def create_fde_agent(tools, domain_policy, **kwargs) -> FdeAgent:
    """Factory for the benchmark's registry (same signature as create_llm_agent)."""
    return FdeAgent(tools=tools, domain_policy=domain_policy,
                    llm=kwargs.get("llm"), llm_args=kwargs.get("llm_args"))
