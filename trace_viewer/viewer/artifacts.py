"""Read what a run left in artifacts/: attempts, raw model calls and logs.

results.json keeps only the final simulation of each task. Every retry has its own
sim_<uuid> folder here, and for a task that never finished those folders are the
only evidence of what happened.

llm_debug files are matched to timeline steps by exact equality only. A step
without an exact match reports that its call was not saved. It is never paired
with a nearby file.
"""

import json
import re
from pathlib import Path
from typing import Any, NamedTuple

from viewer.store import read_json_file
from viewer.timeline import AGENT, CUSTOMER, GREETING

ARTIFACTS_DIR = "artifacts"
LLM_DEBUG_DIR = "llm_debug"
STATUS_FILE = "sim_status.json"
LOG_FILE = "task.log"
SIM_PREFIX = "sim_"

AGENT_CALL = "agent_response"
CUSTOMER_CALL = "user_simulator_response"
NL_JUDGE_CALL = "nl_assertions_eval"
JUDGE_CALLS = (NL_JUDGE_CALL, "communicate_info_eval")

NO_STATUS = "no_status"
MAX_LOG_BYTES = 2_000_000

SAVE_HINT = "Run with --verbose-logs --llm-log-mode all to save every call."

_DEBUG_FILE = re.compile(
    r"^(?P<date>\d{8})_(?P<time>\d{6})_(?P<ms>\d{3})_(?:(?P<name>.+)_)?"
    r"(?P<id>[0-9a-f]{8})\.json$"
)


class DebugFile(NamedTuple):
    file: str
    stamp: str
    call_name: str | None


def _stamp(match: re.Match) -> str:
    date, time = match["date"], match["time"]
    return (
        f"{date[:4]}-{date[4:6]}-{date[6:]}T"
        f"{time[:2]}:{time[2:4]}:{time[4:]}.{match['ms']}"
    )


def debug_files(sim_dir: Path) -> list[DebugFile]:
    """List a simulation's saved calls in time order, from file names alone."""
    folder = sim_dir / LLM_DEBUG_DIR
    if not folder.is_dir():
        return []
    files = []
    for entry in folder.iterdir():
        match = _DEBUG_FILE.match(entry.name)
        if match:
            files.append(DebugFile(entry.name, _stamp(match), match["name"]))
    return sorted(files)


def read_calls(sim_dir: Path) -> list[dict]:
    """Read a simulation's saved calls, in time order."""
    calls = []
    for entry in debug_files(sim_dir):
        try:
            call = read_json_file(sim_dir / LLM_DEBUG_DIR / entry.file)
        except (OSError, ValueError):
            continue  # Still being written by a live run.
        call["file"] = entry.file
        calls.append(call)
    return calls


def attempt_dirs(run_dir: Path, task_id: str) -> list[Path]:
    task_dir = run_dir / ARTIFACTS_DIR / f"task_{task_id}"
    if not task_dir.is_dir():
        return []
    return [
        entry
        for entry in task_dir.iterdir()
        if entry.is_dir() and entry.name.startswith(SIM_PREFIX)
    ]


def find_sim_dir(run_dir: Path, task_id: str, sim_id: str) -> Path | None:
    """Find a simulation's folder. Only folders that exist on disk are returned."""
    wanted = f"{SIM_PREFIX}{sim_id}"
    return next((d for d in attempt_dirs(run_dir, task_id) if d.name == wanted), None)


def read_status(sim_dir: Path) -> dict | None:
    try:
        return read_json_file(sim_dir / STATUS_FILE)
    except (OSError, ValueError):
        return None


def describe_attempt(sim_dir: Path, current_ids: set[str]) -> dict[str, Any]:
    status = read_status(sim_dir) or {}
    files = debug_files(sim_dir)
    sim_id = sim_dir.name.removeprefix(SIM_PREFIX)
    return {
        "sim_id": sim_id,
        "status": status.get("status", NO_STATUS),
        "reason": status.get("reason"),
        "error": status.get("error"),
        "error_type": status.get("error_type"),
        "is_current": sim_id in current_ids,
        "llm_calls": len(files),
        "started_at": files[0].stamp if files else None,
        "last_call_at": files[-1].stamp if files else None,
        "has_log": (sim_dir / LOG_FILE).is_file(),
    }


def list_attempts(run_dir: Path, task_id: str, current_ids: set[str]) -> list[dict]:
    attempts = [
        describe_attempt(d, current_ids) for d in attempt_dirs(run_dir, task_id)
    ]
    return sorted(attempts, key=lambda a: (a["started_at"] is None, a["started_at"]))


def count_attempts(attempts: list[dict]) -> dict[str, int]:
    statuses = [attempt["status"] for attempt in attempts]
    return {
        "total": len(statuses),
        "failed": statuses.count("failed"),
        "discarded": statuses.count("discarded"),
        "no_status": statuses.count(NO_STATUS),
    }


def find_nl_judge_model(run_dir: Path) -> str | None:
    """The NL judge model. results.json does not record it, a saved call does."""
    pattern = f"task_*/{SIM_PREFIX}*/{LLM_DEBUG_DIR}/*_{NL_JUDGE_CALL}_*.json"
    for path in (run_dir / ARTIFACTS_DIR).glob(pattern):
        try:
            return read_json_file(path)["request"]["model"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def find_saved_agent_call(run_dir: Path) -> dict | None:
    """Any saved agent call: the record of the prompt and tools the model received."""
    pattern = f"task_*/{SIM_PREFIX}*/{LLM_DEBUG_DIR}/*_{AGENT_CALL}_*.json"
    for path in (run_dir / ARTIFACTS_DIR).glob(pattern):
        try:
            return read_json_file(path)
        except (OSError, ValueError):
            continue
    return None


def join_steps(messages: list[dict], calls: list[dict]) -> dict[int, dict]:
    """Map message indexes to the saved call that produced them.

    Agent messages carry the unique timing of their call. Customer messages do
    not, so they are matched in order on exact content and token usage.
    """
    by_time = {}
    for call in calls:
        seconds = (call.get("response") or {}).get("generation_time_seconds")
        if seconds is not None:
            by_time.setdefault(seconds, call)
    customer_calls = [c for c in calls if c.get("call_name") == CUSTOMER_CALL]

    matches: dict[int, dict] = {}
    next_customer = 0
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            call = by_time.get(message.get("generation_time_seconds"))
            if call is not None:
                matches[index] = call
        elif role == "user":
            for position in range(next_customer, len(customer_calls)):
                response = customer_calls[position].get("response") or {}
                if response.get("content") == message.get("content") and response.get(
                    "usage"
                ) == message.get("usage"):
                    matches[index] = customer_calls[position]
                    next_customer = position + 1
                    break
    return matches


def missing_call_reason(message: dict, sim_dir: Path | None, saved: int) -> str:
    """Explain why a step has no raw call."""
    role = message.get("role")
    if role == "tool":
        return "Tool results come from the environment, not from a model."
    if role == "assistant" and not message.get("raw_data"):
        return "This line is scripted, so no model call was made."
    if sim_dir is None:
        return f"This simulation has no artifacts folder. {SAVE_HINT}"
    if saved == 0:
        return f"This simulation has no llm_debug files. {SAVE_HINT}"
    return (
        f"None of the {saved} saved calls matches this step exactly. "
        "--llm-log-mode latest keeps only the last call of each type. "
        f"{SAVE_HINT}"
    )


def _text(content: Any) -> str | None:
    """llm_debug stores multi-line text as a list of lines."""
    if isinstance(content, list):
        return "\n".join(
            part if isinstance(part, str) else json.dumps(part) for part in content
        )
    return content


def _parse_tool_call(raw: dict) -> dict:
    function = raw.get("function") or {}
    arguments = function.get("arguments", raw.get("arguments"))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    return {
        "id": raw.get("id"),
        "name": raw.get("name") or function.get("name"),
        "arguments": arguments,
        "result": None,
    }


def rebuild_conversation(calls: list[dict]) -> list[dict]:
    """Rebuild an attempt's conversation from its last saved agent request.

    Failed attempts are absent from results.json, but each agent request holds
    the full history so far. Reasoning is not saved in llm_debug, so rebuilt
    steps have none.
    """
    agent_calls = [c for c in calls if (c.get("request") or {}).get("tools")]
    if not agent_calls:
        return []
    last = agent_calls[-1]

    steps: list[dict] = []

    def add(step_type: str, content: Any, tool_calls: list[dict] | None = None) -> None:
        steps.append(
            {
                "index": len(steps),
                "type": step_type,
                "content": _text(content),
                "tool_calls": tool_calls or [],
                "rebuilt": True,
            }
        )

    pending: dict[str, dict] = {}
    for message in last["request"].get("messages") or []:
        role = message.get("role")
        if role == "tool":
            call = pending.pop(message.get("tool_call_id"), None)
            if call is not None:
                content = _text(message.get("content")) or ""
                call["result"] = {
                    "content": content,
                    "error": content.startswith("Error"),
                }
        elif role == "assistant":
            tool_calls = [_parse_tool_call(t) for t in message.get("tool_calls") or []]
            pending.update({call["id"]: call for call in tool_calls})
            step_type = GREETING if not steps else AGENT
            add(step_type, message.get("content"), tool_calls)
        elif role == "user":
            add(CUSTOMER, message.get("content"))

    response = last.get("response") or {}
    add(
        AGENT,
        response.get("content"),
        [_parse_tool_call(t) for t in response.get("tool_calls") or []],
    )
    for call in calls[calls.index(last) + 1 :]:
        if call.get("call_name") == CUSTOMER_CALL:
            add(CUSTOMER, (call.get("response") or {}).get("content"))
    return steps


def describe_call(call: dict) -> dict[str, Any]:
    """A one-line summary of a saved call for the attempt's call list."""
    request = call.get("request") or {}
    response = call.get("response") or {}
    usage = response.get("usage") or {}
    return {
        "file": call.get("file"),
        "call_name": call.get("call_name"),
        "timestamp": call.get("timestamp"),
        "model": request.get("model"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "generation_time_seconds": response.get("generation_time_seconds"),
    }


def read_log(sim_dir: Path) -> dict[str, Any] | None:
    """Read task.log, keeping only the end of a very large file."""
    path = sim_dir / LOG_FILE
    if not path.is_file():
        return None
    size = path.stat().st_size
    with path.open("rb") as log:
        log.seek(max(0, size - MAX_LOG_BYTES))
        raw = log.read()
    return {
        "text": raw.decode("utf-8", errors="replace"),
        "size": size,
        "truncated": size > MAX_LOG_BYTES,
    }
