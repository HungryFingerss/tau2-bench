"""Agent design files: load them, check them against the code, compare them.

A design documents how an agent works as named decisions, each citing the source
that implements it. Line numbers go stale when code changes, so each design records
a hash of every file it cites, and the UI warns when one no longer matches.

Print the hashes for a new or updated design with:
    uv run --project trace_viewer python -m viewer.designs <design_version>
"""

import hashlib
import os
import sys
import threading
from pathlib import Path
from typing import Any

import yaml

from viewer.store import REPO_ROOT

VIEWER_DIR = Path(__file__).resolve().parents[1]
DESIGNS_DIR = VIEWER_DIR / "designs"

REQUIRED_FIELDS = ("version", "title", "applies_to", "system_prompt", "decisions")
DECISION_FIELDS = (
    "id",
    "name",
    "status",
    "what",
    "code_enforces",
    "left_to_model",
    "source",
)
COMPARED_FIELDS = ("name", "status", "what", "code_enforces", "left_to_model")
STATUSES = ("implemented", "not_handled")


class UnknownDesign(KeyError):
    """No design file has this version."""


class DesignError(ValueError):
    """A design file is missing something it needs."""


def validate(design: Any, path: Path) -> dict:
    if not isinstance(design, dict):
        raise DesignError(f"{path.name} is not a mapping.")
    for field in REQUIRED_FIELDS:
        if field not in design:
            raise DesignError(f"{path.name} is missing `{field}`.")
    if design["version"] != path.stem:
        raise DesignError(f"{path.name}: version must be `{path.stem}`.")
    if not (design["applies_to"] or {}).get("agent_implementation"):
        raise DesignError(f"{path.name} needs applies_to.agent_implementation.")

    seen = set()
    for decision in design["decisions"]:
        for field in DECISION_FIELDS:
            if field not in decision:
                raise DesignError(f"{path.name}: a decision is missing `{field}`.")
        if decision["id"] in seen:
            raise DesignError(f"{path.name}: duplicate decision {decision['id']}.")
        seen.add(decision["id"])
        if decision["status"] not in STATUSES:
            raise DesignError(f"{path.name}: bad status on {decision['id']}.")
        for source in decision["source"]:
            first, last = source["lines"]
            if not source.get("file") or first > last:
                raise DesignError(f"{path.name}: bad source on {decision['id']}.")
    return design


def load_design(path: Path) -> dict:
    try:
        design = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise DesignError(f"{path.name} is not valid YAML: {error}") from error
    return validate(design, path)


def normalized_hash(path: Path) -> str:
    """Hash a file ignoring line endings, which differ between checkouts."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def cited_files(design: dict) -> list[str]:
    files = {s["file"] for d in design["decisions"] for s in d["source"]}
    return sorted(files)


def current_hashes(design: dict) -> dict[str, str]:
    return {file: normalized_hash(REPO_ROOT / file) for file in cited_files(design)}


def source_status(design: dict) -> dict[str, list[str]]:
    """Which cited files changed since the design was documented."""
    recorded = design.get("source_hashes") or {}
    changed, missing = [], []
    for file in cited_files(design):
        path = REPO_ROOT / file
        if not path.is_file():
            missing.append(file)
        elif recorded.get(file) != normalized_hash(path):
            changed.append(file)
    return {"changed": changed, "missing": missing}


class Designs:
    """Every design in the folder. Edits are picked up without a restart."""

    def __init__(self, directory: Path = DESIGNS_DIR) -> None:
        self.directory = directory
        self._lock = threading.Lock()
        self._signature: tuple | None = None
        self._designs: dict[str, dict] = {}
        self._problems: list[str] = []

    def _load(self) -> dict[str, dict]:
        files = []
        if self.directory.is_dir():
            with os.scandir(self.directory) as entries:
                for entry in entries:
                    if entry.name.endswith((".yaml", ".yml")):
                        stat = entry.stat()
                        files.append((entry.name, stat.st_mtime_ns, stat.st_size))
        signature = tuple(sorted(files))
        with self._lock:
            if signature != self._signature:
                designs, problems = {}, []
                for name, _, _ in signature:
                    try:
                        design = load_design(self.directory / name)
                        designs[design["version"]] = design
                    except (OSError, DesignError) as error:
                        problems.append(str(error))
                self._signature = signature
                self._designs = designs
                self._problems = problems
            return self._designs

    def all(self) -> dict[str, dict]:
        return self._load()

    def problems(self) -> list[str]:
        self._load()
        return list(self._problems)

    def get(self, version: str) -> dict:
        designs = self._load()
        if version not in designs:
            raise UnknownDesign(version)
        return designs[version]

    def for_agent(self, implementation: str | None) -> dict | None:
        """The design for an agent name. Used for runs that recorded no design."""
        for design in self._load().values():
            if design["applies_to"]["agent_implementation"] == implementation:
                return design
        return None

    def read_source(self, file: str) -> str | None:
        """A cited file's text. Files no design cites cannot be read this way."""
        allowed = {f for design in self._load().values() for f in cited_files(design)}
        if file not in allowed:
            return None
        return (REPO_ROOT / file).read_text(encoding="utf-8", errors="replace")


def _tools_by_name(design: dict) -> dict[str, dict]:
    return {tool["name"]: tool for tool in design.get("tools") or []}


def compare_designs(first: dict, second: dict) -> dict[str, Any]:
    """What changed from one design to another."""
    old = {d["id"]: d for d in first["decisions"]}
    new = {d["id"]: d for d in second["decisions"]}

    changed, moved = [], []
    for decision_id in old.keys() & new.keys():
        fields = [
            f for f in COMPARED_FIELDS if old[decision_id][f] != new[decision_id][f]
        ]
        if fields:
            changed.append(
                {
                    "id": decision_id,
                    "fields": fields,
                    "before": old[decision_id],
                    "after": new[decision_id],
                }
            )
        elif old[decision_id]["source"] != new[decision_id]["source"]:
            moved.append(decision_id)

    old_tools, new_tools = _tools_by_name(first), _tools_by_name(second)
    old_prompt, new_prompt = first["system_prompt"], second["system_prompt"]
    old_parameters = first.get("parameters") or {}
    new_parameters = second.get("parameters") or {}
    result = {
        "first": first["version"],
        "second": second["version"],
        "added": [new[i] for i in new.keys() - old.keys()],
        "removed": [old[i] for i in old.keys() - new.keys()],
        "changed": sorted(changed, key=lambda item: item["id"]),
        "moved": sorted(moved),
        "instruction_changed": old_prompt.get("instruction")
        != new_prompt.get("instruction"),
        "template_changed": old_prompt.get("template") != new_prompt.get("template"),
        "instruction": {
            "before": old_prompt.get("instruction"),
            "after": new_prompt.get("instruction"),
        },
        "template": {
            "before": old_prompt.get("template"),
            "after": new_prompt.get("template"),
        },
        "tools_added": sorted(new_tools.keys() - old_tools.keys()),
        "tools_removed": sorted(old_tools.keys() - new_tools.keys()),
        "tools_changed": sorted(
            name
            for name in old_tools.keys() & new_tools.keys()
            if old_tools[name] != new_tools[name]
        ),
        "parameters_changed": [
            {
                "name": name,
                "before": old_parameters.get(name),
                "after": new_parameters.get(name),
            }
            for name in sorted(old_parameters.keys() | new_parameters.keys())
            if old_parameters.get(name) != new_parameters.get(name)
        ],
    }
    result["added"].sort(key=lambda d: d["id"])
    result["removed"].sort(key=lambda d: d["id"])
    result["same"] = not any(
        result[key]
        for key in (
            "added",
            "removed",
            "changed",
            "moved",
            "instruction_changed",
            "template_changed",
            "tools_added",
            "tools_removed",
            "tools_changed",
            "parameters_changed",
        )
    )
    return result


def rebuild_prompt(design: dict, info: dict) -> str | None:
    """The prompt the design predicts for a run, from its saved run info.

    The policy in results.json leaves out the language line that the benchmark adds
    inside <policy>, so it is taken from the run's language context.
    """
    policy = (info.get("environment_info") or {}).get("policy")
    if policy is None:
        return None
    contexts = list((info.get("language_tasks") or {}).values())
    clause = contexts[0].get("agent_language_clause") if contexts else None
    domain_policy = f"{policy}\n\n{clause}" if clause else policy
    prompt = design["system_prompt"]
    return prompt["template"].format(
        agent_instruction=prompt["instruction"], domain_policy=domain_policy
    )


if __name__ == "__main__":
    design = Designs().get(sys.argv[1])
    print(yaml.safe_dump({"source_hashes": current_hashes(design)}, sort_keys=True))
