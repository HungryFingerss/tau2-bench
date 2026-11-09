"""fde_agent's own log, turned into the steps of the conversation page.

The benchmark records only what crosses its boundary: messages, and the store calls
the agent returns. fde_agent_log.json (see fde_agent/LOG_FORMAT.md) also holds every
model call, every call to the agent's own tools with exactly what the tool returned,
the notes code gave the model, who wrote each message, and every check that fired.
This module lays all of it out in order.

Every link is an exact match, never a guess:
  model_call       -> llm_debug agent_response file   same generation_time_seconds
  customer message -> llm_debug customer file         same text, in order
  store_calls      -> results.json assistant message  same fde-N tool-call IDs
  store_results    -> results.json tool message       same fde-N ID
  agent/customer   -> results.json message            same text, in order

Steps the benchmark also recorded take their results.json message index as `index`,
so the checks panel and the jump links find them. Steps only the log knows about are
indexed "e<event number>".
"""

from datetime import datetime
from typing import Any

from viewer.artifacts import AGENT_CALL, CUSTOMER_CALL, JUDGE_CALLS, describe_call
from viewer.timeline import AGENT, CUSTOMER, GREETING
from viewer.verdicts import tool_results

INNER = "inner"  # A step inside the agent, which the benchmark never saw.
CHECK = "check"  # A check of the agent's that fired.

MODEL, CODE, BENCHMARK = "model", "code", "benchmark"

# Each CHECK event kind, as LOG_FORMAT.md lists them, with a short name for the page.
CHECK_KINDS = {
    "invalid_call": "Model reply rejected",
    "fixed": "Fixed by code",
    "ignored_inputs": "Inputs ignored",
    "blocked_reply": "Reply blocked",
    "empty_reply": "Empty reply",
    "safe_message": "Safe message sent",
    "tool_crash": "Tool crashed",
}

# What a model call led to, from the first of these events after it.
_OUTCOMES = {
    "tool_call": lambda e: f"called {e.get('tool')} ({e.get('id')})",
    "agent": lambda e: "wrote a message to the customer",
    "invalid_call": lambda e: "rejected before anything ran",
    "empty_reply": lambda e: "empty reply",
    "blocked_reply": lambda e: "reply blocked: it showed internal IDs",
}


def _timestamp(seconds: Any) -> str | None:
    if not isinstance(seconds, (int, float)):
        return None
    return datetime.fromtimestamp(seconds).isoformat(timespec="milliseconds")


def _text(content: Any) -> str | None:
    """llm_debug stores multi-line text as a list of lines."""
    if isinstance(content, list):
        return "\n".join(part for part in content if isinstance(part, str))
    return content


class _Messages:
    """Find a log event's message in results.json. Everything returns None without it."""

    def __init__(self, messages: list[dict] | None) -> None:
        messages = messages or []
        self.users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        self.says = [
            i
            for i, m in enumerate(messages)
            if m.get("role") == "assistant"
            and m.get("content")
            and not m.get("tool_calls")
        ]
        self.calls = {
            call.get("id"): i
            for i, m in enumerate(messages)
            for call in m.get("tool_calls") or []
        }
        self.results = {
            part.get("id"): i
            for i, m in enumerate(messages)
            for part in tool_results(m)
        }
        self.messages = messages
        self._user = self._say = 0

    def user(self, text: str) -> int | None:
        return self._next("users", "_user", text)

    def say(self, text: str) -> int | None:
        return self._next("says", "_say", text)

    def _next(self, kind: str, position: str, text: str) -> int | None:
        indexes = getattr(self, kind)
        at = getattr(self, position)
        if at >= len(indexes):
            return None
        setattr(self, position, at + 1)
        index = indexes[at]
        return index if self.messages[index].get("content") == text else None


def _role(call: dict) -> str:
    name = call.get("call_name")
    if name == AGENT_CALL:
        return "agent"
    if name == CUSTOMER_CALL:
        return "customer"
    return "judge" if name in JUDGE_CALLS else "other"


def build(log: dict, calls: list[dict], messages: list[dict] | None = None) -> dict:
    """The conversation as steps, every model call, and what could not be matched.

    `calls` are the attempt's llm_debug files (artifacts.read_calls). `messages` are
    its results.json messages, when results.json has this attempt.
    """
    events = log.get("events") or []
    by_time = {}
    for call in calls:
        if call.get("call_name") == AGENT_CALL:
            seconds = (call.get("response") or {}).get("generation_time_seconds")
            if seconds is not None:
                by_time.setdefault(seconds, call)
    customer_calls = [c for c in calls if c.get("call_name") == CUSTOMER_CALL]
    found = _Messages(messages)

    steps: list[dict] = []
    step_of_file: dict[str, Any] = {}
    store_calls: dict[str, dict] = {}
    model_step: dict | None = None  # The model call whose decision comes next.
    running_tool: dict | None = None  # Between a tool_call and its tool_result.
    finished_tool: dict | None = None  # The last tool_result, for code's messages.
    next_customer = 0

    def add(step: dict, event: dict, index: Any = None) -> dict:
        step["index"] = index if index is not None else f"e{event.get('n')}"
        step["event"] = event.get("n")
        step["timestamp"] = _timestamp(event.get("time"))
        steps.append(step)
        return step

    def consumed(step: dict, event: dict) -> None:
        """A decision: it says what the model call before it led to."""
        nonlocal model_step
        if model_step is not None:
            model_step["outcome"] = _OUTCOMES[event["kind"]](event)
            model_step["outcome_step"] = step["index"]
            model_step = None

    for event in events:
        kind = event.get("kind")
        if kind == "agent":
            by = event.get("by")
            text = event.get("text") or ""
            step: dict[str, Any] = {
                "type": GREETING if by == BENCHMARK else AGENT,
                "content": text,
                "tool_calls": [],
                "written_by": by,
            }
            if by == MODEL and model_step is not None:
                step["call_file"] = model_step.get("call_file")
                step["model_call_step"] = model_step["index"]
            elif by == CODE:
                step["code_source"] = _code_source(finished_tool, steps)
            add(step, event, found.say(text))
            if by == MODEL:
                consumed(step, event)

        elif kind == "customer":
            text = event.get("text") or ""
            file = None
            for position in range(next_customer, len(customer_calls)):
                response = customer_calls[position].get("response") or {}
                if _text(response.get("content")) == text:
                    file = customer_calls[position].get("file")
                    next_customer = position + 1
                    break
            step = add(
                {
                    "type": CUSTOMER,
                    "content": text,
                    "tool_calls": [],
                    "call_file": file,
                },
                event,
                found.user(text),
            )
            if file:
                step_of_file[file] = step["index"]

        elif kind == "model_call":
            seconds = event.get("generation_time_seconds")
            call = by_time.get(seconds)
            model_step = add(
                {
                    "type": INNER,
                    "kind": "model_call",
                    "generation_time_seconds": seconds,
                    "usage": event.get("usage"),
                    "cost": event.get("cost"),
                    "call_file": (call or {}).get("file"),
                    "outcome": None,
                },
                event,
            )
            if call is not None:
                step_of_file[call["file"]] = model_step["index"]

        elif kind == "tool_call":
            step = add(
                {
                    "type": INNER,
                    "kind": "tool_call",
                    "id": event.get("id"),
                    "tool": event.get("tool"),
                    "args": event.get("args"),
                    "model_call_step": model_step["index"] if model_step else None,
                },
                event,
            )
            consumed(step, event)
            running_tool = step

        elif kind == "tool_result":
            finished_tool = add(
                {
                    "type": INNER,
                    "kind": "tool_result",
                    "id": event.get("id"),
                    "tool": event.get("tool"),
                    "ok": event.get("ok"),
                    "text": event.get("text"),
                },
                event,
            )
            running_tool = None

        elif kind == "store_calls":
            calls_here = []
            for call_id, call in (event.get("calls") or {}).items():
                entry = {
                    "id": call_id,
                    "name": call.get("name"),
                    "arguments": call.get("args"),
                    "result": None,
                }
                store_calls[call_id] = entry
                calls_here.append(entry)
            first = calls_here[0]["id"] if calls_here else None
            add(
                {
                    "type": AGENT,
                    "content": None,
                    "tool_calls": calls_here,
                    "written_by": CODE,
                    "via": {
                        "id": (running_tool or {}).get("id"),
                        "tool": event.get("tool"),
                    },
                },
                event,
                found.calls.get(first),
            )

        elif kind == "store_results":
            for call_id, result in (event.get("results") or {}).items():
                entry = store_calls.get(call_id)
                if entry is None:
                    continue
                entry["result"] = {
                    "content": result.get("content"),
                    "error": bool(result.get("error")),
                    "message_index": found.results.get(call_id),
                }

        elif kind == "note":
            add({"type": INNER, "kind": "note", "text": event.get("text")}, event)

        elif kind in CHECK_KINDS:
            details = {
                key: value
                for key, value in event.items()
                if key not in ("n", "time", "kind")
            }
            step = add(
                {
                    "type": CHECK,
                    "check": kind,
                    "name": CHECK_KINDS[kind],
                    "details": details,
                    "model_call_step": model_step["index"] if model_step else None,
                },
                event,
            )
            if kind in _OUTCOMES:  # A rejected reply. "fixed" is not a decision.
                consumed(step, event)

        else:  # A kind this viewer does not know yet: shown, never dropped.
            details = {k: v for k, v in event.items() if k not in ("n", "time")}
            add({"type": INNER, "kind": "other", "details": details}, event)

    return {
        "steps": steps,
        "calls": _call_list(calls, step_of_file, steps),
        "missing_calls": [
            {
                "step": s["index"],
                "generation_time_seconds": s["generation_time_seconds"],
            }
            for s in steps
            if s.get("kind") == "model_call" and not s.get("call_file")
        ],
        "counts": counts(events),
        "agent": log.get("agent"),
        "model": log.get("model"),
        "llm_args": log.get("llm_args"),
        "instructions_sha256": log.get("instructions_sha256"),
    }


def _code_source(finished_tool: dict | None, steps: list[dict]) -> str:
    """Why code, not the model, sent a message, from what happened just before."""
    last = steps[-1] if steps else {}
    if last.get("type") == CHECK and last.get("check") == "safe_message":
        return "The safe message, after too many failing tries in a row."
    if finished_tool and finished_tool.get("ok"):
        tool = finished_tool.get("tool")
        if tool == "prepare_change":
            return (
                f"The change summary that prepare_change ({finished_tool.get('id')}) "
                "returned, sent word for word."
            )
        if tool == "transfer_to_human":
            return "The transfer sentence, sent word for word after transfer_to_human."
    return "Sent by code, not written by the model."


def _call_list(calls: list[dict], step_of_file: dict, steps: list[dict]) -> list[dict]:
    """Every saved model call of the attempt, with the step it belongs to."""
    outcomes = {
        s.get("call_file"): s.get("outcome")
        for s in steps
        if s.get("kind") == "model_call" and s.get("call_file")
    }
    return [
        {
            **describe_call(call),
            "role": _role(call),
            "step": step_of_file.get(call.get("file")),
            "outcome": outcomes.get(call.get("file")),
        }
        for call in calls
    ]


def counts(events: list[dict]) -> dict:
    """How many of each kind of thing happened in one conversation.

    benchmark_steps counts what the benchmark's max_steps limits (see
    verdicts.benchmark_steps): each customer message, each agent message after the
    greeting, and for each batch of store calls the agent's message and the store's
    turn. Model calls inside one turn are not steps.
    """
    checks = {kind: 0 for kind in CHECK_KINDS}
    writers = {MODEL: 0, CODE: 0, BENCHMARK: 0}
    ignored = {"by_kind": {}, "by_input": {}, "by_kind_and_input": {}}
    store = batches = customer = 0
    for event in events:
        kind = event.get("kind")
        if kind in checks:
            checks[kind] += 1
        if kind == "ignored_inputs":
            change = event.get("change_kind") or "(no kind)"
            ignored["by_kind"][change] = ignored["by_kind"].get(change, 0) + 1
            pairs = ignored["by_kind_and_input"].setdefault(change, {})
            for name in event.get("inputs") or []:
                ignored["by_input"][name] = ignored["by_input"].get(name, 0) + 1
                pairs[name] = pairs.get(name, 0) + 1
        elif kind == "agent" and event.get("by") in writers:
            writers[event["by"]] += 1
        elif kind == "customer":
            customer += 1
        elif kind == "store_calls":
            store += len(event.get("calls") or {})
            batches += 1
    return {
        "events": len(events),
        "model_calls": sum(1 for e in events if e.get("kind") == "model_call"),
        "tool_calls": sum(1 for e in events if e.get("kind") == "tool_call"),
        "store_calls": store,
        "benchmark_steps": customer + writers[MODEL] + writers[CODE] + 2 * batches,
        "notes": sum(1 for e in events if e.get("kind") == "note"),
        "messages_by": writers,
        "checks": checks,
        "ignored_inputs": ignored,
    }


def add_counts(total: dict, part: dict) -> dict:
    """Sum two counts() results, nested breakdowns included."""
    for key, value in part.items():
        if isinstance(value, dict):
            add_counts(total.setdefault(key, {}), value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
    return total


def writer_of(log: dict, text: str | None) -> str | None:
    """Who wrote an agent message with this text: model, code or benchmark."""
    for event in log.get("events") or []:
        if event.get("kind") == "agent" and event.get("text") == text:
            return event.get("by")
    return None
