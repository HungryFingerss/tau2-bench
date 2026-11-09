"""Failure labels: the editable list, saved annotations, and their run summary.

Annotations live in trace_viewer/annotations/<run_name>.json. Nothing here ever
writes into data/simulations.
"""

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from viewer.verdicts import FAILURE_RESULTS

VIEWER_DIR = Path(__file__).resolve().parents[1]
ANNOTATIONS_DIR = VIEWER_DIR / "annotations"
LABELS_PATH = VIEWER_DIR / "labels.yaml"
MAX_NOTE_LENGTH = 5000
FORMAT_VERSION = 1


class InvalidAnnotation(ValueError):
    """A label or decision in a save request does not exist."""


class LabelsFileError(ValueError):
    """labels.yaml cannot be used as it stands."""


def load_labels(path: Path = LABELS_PATH) -> list[dict[str, str]]:
    """Read labels.yaml. It is read each time, so edits need no restart."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise LabelsFileError(f"{path.name} cannot be read: {error}") from error
    except yaml.YAMLError as error:
        raise LabelsFileError(f"{path.name} is not valid YAML: {error}") from error

    labels = (data or {}).get("labels") if isinstance(data, dict) else None
    if not isinstance(labels, list) or not labels:
        raise LabelsFileError(f"{path.name} needs a non-empty `labels` list.")
    seen = set()
    for label in labels:
        if not isinstance(label, dict) or not label.get("id"):
            raise LabelsFileError(f"{path.name}: every label needs an `id`.")
        if label["id"] in seen:
            raise LabelsFileError(f"{path.name}: duplicate label {label['id']}.")
        seen.add(label["id"])
    return [
        {
            "id": label["id"],
            "name": label.get("name") or label["id"],
            "description": label.get("description") or "",
        }
        for label in labels
    ]


def task_key(task_id: str, trial: int) -> str:
    return f"{task_id}:{trial}"


class Annotations:
    def __init__(
        self, directory: Path = ANNOTATIONS_DIR, labels_path: Path = LABELS_PATH
    ) -> None:
        self.directory = directory
        self.labels_path = labels_path
        self._lock = threading.Lock()

    def labels(self) -> list[dict[str, str]]:
        return load_labels(self.labels_path)

    def _path(self, run: str) -> Path:
        return self.directory / f"{run}.json"

    def read(self, run: str) -> dict[str, dict]:
        """A run's annotations by task key. Missing or unreadable means none."""
        try:
            data = json.loads(self._path(run).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data.get("tasks") or {}

    def save(
        self,
        run: str,
        task_id: str,
        trial: int,
        sim_id: str | None,
        labels: list[dict],
        note: str,
        allowed_decisions: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """Save one task's annotation. Empty labels and note remove the entry."""
        known = {label["id"] for label in self.labels()}
        seen = set()
        for label in labels:
            if label["id"] not in known:
                raise InvalidAnnotation(f"Unknown label: {label['id']}")
            if label["id"] in seen:
                raise InvalidAnnotation(f"Duplicate label: {label['id']}")
            seen.add(label["id"])
            if allowed_decisions is not None:
                for decision in label.get("decisions") or []:
                    if decision not in allowed_decisions:
                        raise InvalidAnnotation(f"Unknown decision: {decision}")
        if len(note) > MAX_NOTE_LENGTH:
            raise InvalidAnnotation(f"Note is over {MAX_NOTE_LENGTH} characters.")

        key = task_key(task_id, trial)
        with self._lock:
            tasks = self.read(run)
            if labels or note.strip():
                entry = {
                    "task_id": task_id,
                    "trial": trial,
                    "sim_id": sim_id,
                    "labels": [
                        {"id": label["id"], "decisions": label.get("decisions") or []}
                        for label in labels
                    ],
                    "note": note,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
                tasks[key] = entry
            else:
                entry = None
                tasks.pop(key, None)
            self._write(run, tasks)
        return entry

    def _write(self, run: str, tasks: dict) -> None:
        """Replace the file in one step, so a crash cannot leave half of it."""
        self.directory.mkdir(parents=True, exist_ok=True)
        data = {"version": FORMAT_VERSION, "run": run, "tasks": tasks}
        descriptor, temporary = tempfile.mkstemp(
            dir=self.directory, prefix=".annotation_", suffix=".json"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=2, ensure_ascii=False)
            os.replace(temporary, self._path(run))
        except Exception:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise


def _task_ref(row: dict) -> dict[str, Any]:
    return {"task_id": row["task_id"], "trial": row["trial"]}


def analysis(
    rows: list[dict], tasks: dict[str, dict], labels: list[dict], design: dict | None
) -> dict[str, Any]:
    """Failures per design decision and per label, most-hit first.

    A decision counts once per task, however many labels point at it.
    """
    decisions = {d["id"]: d for d in (design or {}).get("decisions") or []}
    names = {label["id"]: label["name"] for label in labels}
    by_decision: dict[str, dict] = {}
    by_label: dict[str, dict] = {}
    unlabelled = []
    labelled = 0

    def bucket(store: dict, key: str, build: dict) -> dict:
        return store.setdefault(key, {**build, "failures": [], "passed": []})

    failures = [r for r in rows if r["result"] in FAILURE_RESULTS]
    for row in rows:
        entry = tasks.get(task_key(row["task_id"], row["trial"]))
        failed = row["result"] in FAILURE_RESULTS
        if not entry or not entry.get("labels"):
            if failed:
                unlabelled.append(_task_ref(row))
            continue
        if row["result"] == "pending":
            continue
        labelled += failed
        side = "failures" if failed else "passed"
        linked: set[str] = set()
        for item in entry["labels"]:
            label_id = item["id"]
            label = bucket(
                by_label,
                label_id,
                {"id": label_id, "name": names.get(label_id, label_id)},
            )
            label[side].append(_task_ref(row))
            linked.update(item.get("decisions") or [])
        for decision_id in sorted(linked):
            known = decisions.get(decision_id) or {}
            decision = bucket(
                by_decision,
                decision_id,
                {
                    "id": decision_id,
                    "name": known.get("name") or "Not in this run's design",
                    "status": known.get("status"),
                    "addresses": known.get("addresses") or [],
                },
            )
            decision[side].append(_task_ref(row))

    def ranked(store: dict) -> list[dict]:
        return sorted(
            store.values(),
            key=lambda b: (-len(b["failures"]), -len(b["passed"]), b["id"]),
        )

    return {
        "failures": len(failures),
        "labelled": labelled,
        "unlabelled": unlabelled,
        "by_decision": ranked(by_decision),
        "by_label": ranked(by_label),
    }
