"""The model client: asks gpt-oss:20b what to do, and checks the answer
BEFORE anything runs ("a deterministic check before execution").

    driver's transcript -> chat messages -> generate() -> raw reply
        -> checks -> a clean Decision for the driver

The model is called through the benchmark's own generate() (decision 1), so every
call lands in the benchmark's model-call logs and the trace viewer's raw-calls page.

Checks (each is plain code; each fix or rejection is reported for the log):
   1. generate() fails (Ollama down, timeout): retry twice, then stop loudly
   2. empty reply: the driver tells the model
   3. text AND a tool call: run the tool, drop the text (the rulebook says never both)
   4. several tool calls: run the first, tell the model the others didn't run
   5. junk on a tool name ("<|channel|>...", "functions."): cut it off, but only if
      what's left is exactly one of our tools (decision 2)
   6. unknown or empty tool name: rejected, our tool names listed
   7. inputs that weren't valid JSON / not an object: rejected (this can happen
      inside generate(), which then raises; it's caught and never retried)
   8. unknown or missing input names: rejected, the real inputs listed
   9. a number where text is expected (zip 19122) and similar: converted when safe
  10. the same call as the previous one, nothing new since: answered from the
      earlier result, not run again (decision 3)
"""

import json
import time
from dataclasses import replace

from pydantic import ValidationError

from tau2.data_model.message import AssistantMessage, SystemMessage, ToolCall, ToolMessage, UserMessage

from fde_agent.driver import Decision
from fde_agent.toolset import MODULES, TOOLS

CALL_NAME = "agent_response"   # what the baseline used; the trace viewer files agent calls by it
ATTEMPTS = 3                   # check 1: the first try + 2 retries


class ModelUnavailable(RuntimeError):
    """The model couldn't be reached after all retries. Never hidden: the run stops."""


class _Rejected(Exception):
    """A tool call failed a check. The message is for the model."""


class ToolSpec:
    """Our tool in the shape generate() expects (it reads .openai_schema)."""
    def __init__(self, module) -> None:
        self.name = module.NAME
        self.openai_schema = {"type": "function", "function": {
            "name": module.NAME, "description": module.DESCRIPTION, "parameters": module.PARAMETERS}}


SPECS = [ToolSpec(m) for m in MODULES]
PARAMS = {m.NAME: m.PARAMETERS for m in MODULES}


# ---------- the transcript as chat messages ----------

def to_messages(system_prompt: str, transcript: list[dict]) -> list:
    """Driver transcript -> the benchmark's message objects, tool results linked by ID."""
    out = [SystemMessage(role="system", content=system_prompt)]
    for e in transcript:
        role = e["role"]
        if role == "customer":
            out.append(UserMessage(role="user", content=e["text"]))
        elif role == "agent":
            out.append(AssistantMessage(role="assistant", content=e["text"]))
        elif role == "tool_call":
            out.append(AssistantMessage(role="assistant", content=None, tool_calls=[
                ToolCall(id=e["id"], name=e["tool"], arguments=e["args"])]))
        elif role == "tool_result":
            out.append(ToolMessage(id=e["id"], role="tool", content=e["text"]))
        elif role == "note":
            out.append(SystemMessage(role="system", content=e["text"]))
        else:
            raise ValueError(f"unknown transcript entry {role!r}")
    return out


# ---------- the checks (pure: raw reply -> Decision) ----------

def clean_name(name: str) -> str:
    """Check 5: 'prepare_change<|channel|>commentary' -> 'prepare_change'."""
    name = (name or "").split("<|")[0].strip()
    if name.startswith("functions."):
        name = name[len("functions."):]
    return name


def check_args(tool: str, args) -> tuple[dict, list[str]]:
    """Checks 7-9. Returns (clean inputs, fixes), or raises _Rejected."""
    schema = PARAMS[tool]
    props, required = schema.get("properties", {}), schema.get("required", [])
    if not isinstance(args, dict):
        raise _Rejected(f"Error: the inputs for {tool} must be an object like "
                        f'{{"name": value}}, not {type(args).__name__}.')
    fixes, clean = [], {}
    for key, value in args.items():
        if value is None:
            fixes.append(f"{tool}: dropped empty input '{key}'")
            continue
        clean[key] = value
    unknown = [k for k in clean if k not in props]
    if unknown:
        raise _Rejected(f"Error: {tool} has no input {', '.join(repr(k) for k in unknown)}. "
                        f"Its inputs are: {', '.join(props)}.")
    missing = [k for k in required if k not in clean]
    if missing:
        raise _Rejected(f"Error: {tool} needs: {', '.join(missing)}.")
    for key, value in list(clean.items()):
        kind = props[key].get("type")
        if kind == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
            clean[key] = str(value)
            fixes.append(f"{tool}: {key} {value!r} -> {str(value)!r}")
        elif kind == "string" and not isinstance(value, str):
            raise _Rejected(f"Error: {tool} input '{key}' must be text.")
        elif kind == "integer" and isinstance(value, str) and value.strip().lstrip("#").isdigit():
            clean[key] = int(value.strip().lstrip("#"))
            fixes.append(f"{tool}: {key} {value!r} -> {clean[key]}")
        elif kind in ("object", "array") and isinstance(value, str):
            want = dict if kind == "object" else list
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = [value] if kind == "array" else None    # "item_1" -> ["item_1"]
            if not isinstance(parsed, want):
                raise _Rejected(f"Error: {tool} input '{key}' must be "
                                f"{'an object' if kind == 'object' else 'a list'}.")
            clean[key] = parsed
            fixes.append(f"{tool}: {key} text -> {kind}")
    return clean, fixes


def is_repeat(transcript: list[dict], tool: str, args: dict) -> bool:
    """Check 10: the last thing that happened was exactly this call and its result."""
    if len(transcript) < 2:
        return False
    call, result = transcript[-2], transcript[-1]
    return (call["role"] == "tool_call" and result["role"] == "tool_result"
            and call["tool"] == tool and call["args"] == args)


def parse(reply: AssistantMessage, transcript: list[dict]) -> Decision:
    """Checks 2-10 on one raw reply."""
    calls = reply.tool_calls or []
    text = (reply.content or "").strip()
    if not calls:
        return Decision.reply(text)          # an empty text -> the driver's empty-reply note
    fixes, note = [], None
    if text:                                                       # check 3
        fixes.append(f"dropped text sent with a tool call: {text[:200]!r}")
    if len(calls) > 1:                                             # check 4
        extra = ", ".join(c.name or "(no name)" for c in calls[1:])
        note = (f"Only one tool runs at a time. These calls were not run: {extra}. "
                f"Call them one by one if you still need them.")
    first = calls[0]
    name = clean_name(first.name)                                  # check 5
    if name != (first.name or "") and name in TOOLS:
        fixes.append(f"tool name {first.name!r} -> {name!r}")
    if name not in TOOLS:                                          # check 6
        what = f"a tool called {first.name!r}" if (first.name or "").strip() else "a tool call with no name"
        return Decision(invalid=f"Error: you made {what}, which doesn't exist. "
                        f"The tools are: {', '.join(TOOLS)}.", fixes=tuple(fixes))
    try:
        args, arg_fixes = check_args(name, first.arguments)        # checks 7-9
    except _Rejected as e:
        return Decision(invalid=str(e), fixes=tuple(fixes))
    fixes += arg_fixes
    return Decision(tool=name, args=args, fixes=tuple(fixes), note=note,
                    repeat=is_repeat(transcript, name, args))      # check 10


# ---------- the client ----------

def call_facts(reply) -> dict:
    """What our log keeps about one model call. generation_time_seconds is also how the
    trace viewer finds the call's raw file (it matches on this exact number)."""
    return {"generation_time_seconds": getattr(reply, "generation_time_seconds", None),
            "usage": getattr(reply, "usage", None), "cost": getattr(reply, "cost", None)}


class ModelClient:
    def __init__(self, model: str, system_prompt: str | None = None, llm_args: dict | None = None,
                 generate_fn=None, retry_wait: float = 2.0) -> None:
        self.model = model
        if system_prompt is None:                  # default: fde_agent/instructions.md
            from fde_agent.prompt import load_instructions
            system_prompt = load_instructions()
        self.system_prompt = system_prompt
        self.llm_args = {"temperature": 0.0, **(llm_args or {})}   # the baseline's setting
        if generate_fn is None:
            from tau2.utils.llm_utils import generate as generate_fn
        self.generate = generate_fn
        self.retry_wait = retry_wait

    def decide(self, transcript: list[dict]) -> Decision:
        messages = to_messages(self.system_prompt, transcript)
        last_error = None
        for attempt in range(1, ATTEMPTS + 1):                     # check 1
            try:
                reply = self.generate(model=self.model, messages=messages, tools=SPECS,
                                      call_name=CALL_NAME, **self.llm_args)
                return replace(parse(reply, transcript), model_call=call_facts(reply))
            except (json.JSONDecodeError, ValidationError):        # check 7, inside generate():
                # broken JSON, or JSON that isn't an object (the benchmark's ToolCall
                # refuses it). The model's mistake, not Ollama's: never retried.
                return Decision(invalid="Error: the inputs of your tool call were not a valid "
                                'JSON object. Send them like {"order_id": "#W2378156"}.')
            except Exception as e:
                last_error = e
                if attempt < ATTEMPTS:
                    time.sleep(self.retry_wait * attempt)
        raise ModelUnavailable(f"The model failed {ATTEMPTS} times: {type(last_error).__name__}: {last_error}")
