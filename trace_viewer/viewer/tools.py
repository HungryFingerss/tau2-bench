"""The Tools page: run fde_agent's tools by hand, through tools_worker.py.

The viewer's own environment has no tau2, so the tools run in a worker under the
benchmark's .venv, started per request like the replay worker. The page keeps the
session as a list of steps and sends all of them every time. The worker replays
them on a fresh in-memory database, so resetting a session means sending none.

Presets are ready-made sessions from tool_presets.yaml, checked by the same step
rules as the API.
"""

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from viewer import dbdiff
from viewer.replay import benchmark_python, worker_environment
from viewer.store import REPO_ROOT

VIEWER_DIR = Path(__file__).resolve().parents[1]
WORKER = VIEWER_DIR / "tools_worker.py"
TOOLS_DIR = REPO_ROOT / "fde_agent" / "tools"
PRESETS_FILE = VIEWER_DIR / "tool_presets.yaml"
TIMEOUT_SECONDS = 120
MAX_SESSION_STEPS = 500


# ---------- session steps ----------


class ToolStep(BaseModel):
    """The model calls a tool. Inputs pass through unchecked, as a model's would."""

    type: Literal["tool"]
    tool: str
    inputs: dict[str, Any] = {}


class ShowChangeStep(BaseModel):
    """The agent loop shows the customer a prepared change: notes.mark_shown."""

    type: Literal["show_change"]
    number: int


class CustomerSaysStep(BaseModel):
    """A customer message: notes.customer_said."""

    type: Literal["customer_says"]
    text: str


SessionStep = Annotated[
    ToolStep | ShowChangeStep | CustomerSaysStep, Field(discriminator="type")
]
STEP_LIST = TypeAdapter(list[SessionStep])


# ---------- presets ----------

STEP_KEYS = ("tool", "show_change", "customer_says")


class PresetsFileError(ValueError):
    """tool_presets.yaml cannot be used. The message says where and why."""


def _preset_step(raw: Any, where: str) -> dict:
    """A step in the file's short form, as the API's step."""
    keys = [key for key in STEP_KEYS if isinstance(raw, dict) and key in raw]
    if len(keys) != 1:
        raise PresetsFileError(f"{where} needs exactly one of: {', '.join(STEP_KEYS)}.")
    if keys[0] == "tool":
        return {"type": "tool", "tool": raw["tool"], "inputs": raw.get("inputs") or {}}
    if keys[0] == "show_change":
        return {"type": "show_change", "number": raw["show_change"]}
    return {"type": "customer_says", "text": raw["customer_says"]}


def _first_problem(error: ValidationError) -> str:
    """'step 2 (number): Input should be a valid integer...' from a step list error."""
    detail = error.errors()[0]
    index, *place = detail["loc"]
    field = ".".join(str(part) for part in place[1:])  # place[0] is the step type.
    return f"step {index + 1}{f' ({field})' if field else ''}: {detail['msg']}"


def _preset(entry: Any, number: int) -> dict:
    where = f"preset {number}"
    if not isinstance(entry, dict) or not entry.get("name"):
        raise PresetsFileError(f"{where} needs a name.")
    where = f"preset '{entry['name']}'"
    raw_steps = entry.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PresetsFileError(f"{where} needs a list of steps.")
    if len(raw_steps) > MAX_SESSION_STEPS:
        raise PresetsFileError(f"{where} has over {MAX_SESSION_STEPS} steps.")
    steps = [
        _preset_step(raw, f"{where}, step {index}")
        for index, raw in enumerate(raw_steps, 1)
    ]
    try:
        checked = STEP_LIST.validate_python(steps)
    except ValidationError as error:
        raise PresetsFileError(f"{where}, {_first_problem(error)}") from error
    return {
        "name": str(entry["name"]),
        "description": entry.get("description"),
        "steps": [step.model_dump() for step in checked],
    }


def load_presets(path: Path = PRESETS_FILE) -> dict[str, Any]:
    """Every preset in the file, read again on each call so edits show at once."""
    if not path.is_file():
        return {"presets": [], "problem": None}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict) or not isinstance(data.get("presets"), list):
            raise PresetsFileError("needs a top-level `presets:` list.")
        presets = [
            _preset(entry, number) for number, entry in enumerate(data["presets"], 1)
        ]
    except (yaml.YAMLError, PresetsFileError) as error:
        return {"presets": [], "problem": f"{path.name}: {error}"}
    return {"presets": presets, "problem": None}


# Display order only. Names, descriptions and parameters come from the modules.
TOOL_ORDER = (
    "verify_customer",
    "get_order",
    "find_variants",
    "find_replacement",
    "prepare_change",
    "confirm_change",
    "transfer_to_human",
)


def _display_order(tool: dict) -> tuple[int, str]:
    name = tool["name"]
    position = TOOL_ORDER.index(name) if name in TOOL_ORDER else len(TOOL_ORDER)
    return position, name


def record_changes(record: dict) -> dict[str, Any]:
    """A changed record as field rows, compared the same way as on the run pages.

    dbdiff compares three versions. Passing the original as both "initial" and
    "expected" turns that into a before-and-after comparison.
    """
    compared = dbdiff.compare_record(
        {
            "initial": record["before"],
            "expected": record["before"],
            "agent": record["after"],
        }
    )
    return {
        "collection": record["collection"],
        "id": record["id"],
        "added": record["before"] is None,
        "removed": record["after"] is None,
        "fields": [
            {"field": row["field"], "before": row["initial"], "after": row["agent"]}
            for row in compared["fields"]
            if row["kind"] != dbdiff.BOTH
        ],
    }


class ToolRunner:
    def __init__(self, worker: Path = WORKER, tools_dir: Path = TOOLS_DIR) -> None:
        self.worker = worker
        self.tools_dir = tools_dir
        self._lock = threading.Lock()  # One session replay at a time.

    def catalog(self) -> dict[str, Any]:
        reply = self._call({"mode": "catalog"})
        if reply.get("ok"):
            reply["tools"] = sorted(reply["tools"], key=_display_order)
        return reply

    def clear_yes(self, text: str) -> dict[str, Any]:
        return self._call({"mode": "clear_yes", "text": text})

    def run_session(self, steps: list[dict]) -> dict[str, Any]:
        with self._lock:
            started = time.perf_counter()
            reply = self._call({"mode": "session", "steps": steps})
            reply["seconds"] = round(time.perf_counter() - started, 2)
        if reply.get("ok"):
            records = reply.pop("changed_records")
            reply["db_changes"] = [record_changes(record) for record in records]
        return reply

    def _call(self, request: dict) -> dict[str, Any]:
        if not self.tools_dir.is_dir():
            return {"ok": False, "error": "fde_agent/tools was not found."}
        python = benchmark_python()
        if python is None:
            return {
                "ok": False,
                "error": "The benchmark's .venv was not found. Run `uv sync` first.",
            }
        try:
            finished = subprocess.run(
                [str(python), str(self.worker)],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=REPO_ROOT,
                env=worker_environment(),
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"The tools took over {TIMEOUT_SECONDS} s."}
        except OSError as error:
            return {"ok": False, "error": f"The tools worker could not start: {error}"}
        try:
            return json.loads(finished.stdout)
        except ValueError:
            tail = finished.stderr.strip().splitlines()[-1:] or ["no error output"]
            return {
                "ok": False,
                "error": f"The tools worker crashed (exit code "
                f"{finished.returncode}): {tail[0]}",
            }
