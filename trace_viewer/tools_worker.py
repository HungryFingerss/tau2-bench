"""Run fde_agent's tools by hand, for the viewer's Tools page.

viewer/tools.py starts this under the benchmark's .venv, as it does replay_worker.py.
Tools run exactly as in fde_agent's own tests: store.run_now on a retail environment
from get_environment(), which loads db.json into memory and keeps every write there.

A session is a list of steps: tool runs, and what the agent loop does around them
(showing a prepared change, passing on a customer message). Every request replays
all of them on a fresh environment and fresh Notes, so the result always follows
from the steps and nothing is carried over between requests.

Input on stdin, one JSON object with a "mode":
  catalog                 NAME, DESCRIPTION and PARAMETERS of every tool module
  clear_yes  + "text"     whether confirm_change.is_clear_yes accepts the text
  session    + "steps"    each step's result and the notes after it, then the
                          database records that changed
Output on stdout: one JSON object.

Only "session" imports tau2. That import alone takes about 3.5 seconds.
"""

import dataclasses
import hashlib
import importlib
import json
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_PACKAGE = "fde_agent.tools"
TOOLS_DIR = REPO_ROOT / "fde_agent" / "tools"
TOOL_FIELDS = ("NAME", "DESCRIPTION", "PARAMETERS")


def plain(value):
    """Notes and store calls as JSON. Generic, so new Notes fields appear as they are."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [plain(item) for item in value]
        try:
            return sorted(items)
        except TypeError:
            return sorted(items, key=json.dumps)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "__dict__"):
        return {key: plain(item) for key, item in vars(value).items()}
    return repr(value)


def load_tools() -> tuple[dict, list[str]]:
    """Every module in fde_agent/tools that defines a tool, by its NAME."""
    tools, skipped = {}, []
    for path in sorted(TOOLS_DIR.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{TOOLS_PACKAGE}.{path.stem}")
        except Exception as error:
            skipped.append(f"{path.name}: {type(error).__name__}: {error}")
            continue
        name = getattr(module, "NAME", None)
        if not all(hasattr(module, field) for field in TOOL_FIELDS) or not callable(
            getattr(module, str(name), None)
        ):
            skipped.append(f"{path.name}: no NAME, DESCRIPTION, PARAMETERS and tool.")
            continue
        tools[name] = module
    return tools, skipped


def catalog() -> dict:
    tools, skipped = load_tools()
    return {
        "ok": True,
        "tools": [
            {
                "name": module.NAME,
                "description": module.DESCRIPTION,
                "parameters": module.PARAMETERS,
                "file": f"fde_agent/tools/{Path(module.__file__).name}",
            }
            for module in tools.values()
        ],
        "skipped": skipped,
    }


def clear_yes(text: str) -> dict:
    from fde_agent.tools.confirm_change import is_clear_yes

    return {"ok": True, "text": text, "clear_yes": is_clear_yes(text)}


class RecordingEnvironment:
    """The real environment for run_now, keeping each call and the store's reply.

    run_now returns the calls a tool made but not what the store answered, and the
    page shows both. Wrapping the one method run_now uses keeps run_now unchanged.
    """

    def __init__(self, environment) -> None:
        self.environment = environment
        self.exchanges: list = []

    def get_response(self, tool_call):
        reply = self.environment.get_response(tool_call)
        self.exchanges.append((tool_call, reply))
        return reply


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def changed_records(before: dict, after: dict) -> list[dict]:
    """Records that differ between two database dumps."""
    records = []
    for collection in sorted(set(before) | set(after)):
        old, new = before.get(collection), after.get(collection)
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                records.append(
                    {
                        "collection": collection,
                        "id": key,
                        "before": old.get(key),
                        "after": new.get(key),
                    }
                )
    return records


class Session:
    def __init__(self) -> None:
        from fde_agent.notes import Notes
        from fde_agent.store import run_now
        from fde_agent.tools.confirm_change import is_clear_yes
        from fde_agent.tools.transfer_to_human import TRANSFER_MESSAGE
        from tau2.domains.retail.environment import get_environment
        from tau2.environment.toolkit import ToolType

        self.run_now, self.is_clear_yes = run_now, is_clear_yes
        self.transfer_message = TRANSFER_MESSAGE
        self.write_type = ToolType.WRITE
        self.tools, _ = load_tools()
        self.environment = get_environment()
        self.notes = Notes()

    def is_write(self, name: str) -> bool:
        toolkit = self.environment.tools
        return toolkit.has_tool(name) and toolkit.tool_type(name) == self.write_type

    def run_tool(self, step: dict) -> dict:
        module = self.tools.get(step.get("tool"))
        if module is None:
            known = ", ".join(self.tools)
            return {
                "ok": False,
                "problem": f"No tool named {step.get('tool')!r}: {known}.",
            }
        recording = RecordingEnvironment(self.environment)
        transferred_before = self.notes.transferred
        reply = {}
        try:
            tool = getattr(module, module.NAME)
            outcome = self.run_now(
                tool(self.notes, **(step.get("inputs") or {})), recording
            )
            reply = {"ok": outcome.ok, "text": outcome.text}
        except Exception as error:  # Not a ToolError: a bad argument name, or a bug.
            reply = {
                "ok": False,
                "problem": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(),
            }
        reply["calls"] = [
            {
                "name": call.name,
                "args": call.arguments,
                "result": answer.content,
                "error": bool(answer.error),
                "write": self.is_write(call.name),
            }
            for call, answer in recording.exchanges
        ]
        if self.notes.transferred and not transferred_before:
            reply["agent_message"] = self.transfer_message
        return reply

    def show_change(self, step: dict) -> dict:
        number = step.get("number")
        self.notes.mark_shown(number)
        prepared = self.notes.prepared
        if prepared is not None and prepared.number == number == self.notes.shown:
            return {"ok": True, "agent_message": prepared.customer_text}
        if prepared is None:
            return {"ok": False, "problem": "There is no prepared change to show."}
        return {
            "ok": False,
            "problem": f"The prepared change is #{prepared.number}, not #{number}.",
        }

    def customer_says(self, step: dict) -> dict:
        text = step.get("text") or ""
        self.notes.customer_said(text)
        return {
            "ok": True,
            "clear_yes": self.is_clear_yes(text),
            # Notes keep a message as the reply only once the summary was shown.
            "counts_as_reply": self.notes.reply == text,
        }

    def run(self, steps: list[dict]) -> list[dict]:
        handlers = {
            "tool": self.run_tool,
            "show_change": self.show_change,
            "customer_says": self.customer_says,
        }
        replies = []
        for index, step in enumerate(steps):
            handler = handlers.get(step.get("type"))
            try:
                reply = (
                    handler(step)
                    if handler
                    else {
                        "ok": False,
                        "problem": f"Unknown step type {step.get('type')!r}.",
                    }
                )
            except Exception as error:
                reply = {
                    "ok": False,
                    "problem": f"{type(error).__name__}: {error}",
                    "traceback": traceback.format_exc(),
                }
            replies.append(
                {"index": index, "step": step, **reply, "notes": plain(self.notes)}
            )
        return replies


def session(steps: list[dict]) -> dict:
    from tau2.domains.retail.utils import RETAIL_DB_PATH

    db_file = Path(RETAIL_DB_PATH)
    hash_before = file_hash(db_file)
    run = Session()
    original = run.environment.tools.db.model_dump()
    replies = run.run(steps)
    return {
        "ok": True,
        "steps": replies,
        "notes": plain(run.notes),
        "changed_records": changed_records(
            original, run.environment.tools.db.model_dump()
        ),
        "db_file_unchanged": file_hash(db_file) == hash_before,
    }


def main() -> int:
    # Only the final JSON may reach stdout, whatever an import or a tool prints.
    output = sys.stdout
    sys.stdout = sys.stderr
    sys.path.insert(0, str(REPO_ROOT))

    request = json.load(sys.stdin)
    mode = request.get("mode")
    try:
        if mode == "catalog":
            reply = catalog()
        elif mode == "clear_yes":
            reply = clear_yes(request.get("text") or "")
        elif mode == "session":
            from loguru import logger

            logger.remove()  # The store logs every call at DEBUG.
            reply = session(request.get("steps") or [])
        else:
            reply = {"ok": False, "error": f"Unknown mode {mode!r}."}
    except Exception as error:
        reply = {
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc(),
        }
    json.dump(reply, output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
