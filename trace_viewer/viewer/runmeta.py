"""What record_run.py saved next to a run, and whether that run is still alive.

Run state never relies on opening results.json. Everything except run_meta.json
itself comes from folder listings, which are safe during a live benchmark save
(see viewer/store.py).

Recent activity alone cannot prove a run is alive: one model call can stay silent
for up to 40 minutes across its retries. So record_run.py keeps a heartbeat file
fresh, and only runs started some other way fall back to guessing from activity.
"""

import json
import os
import time
from pathlib import Path
from typing import Any, NamedTuple

from viewer.store import scan_results

META_FILE = "run_meta.json"
DIFF_FILE = "agent_code.diff"
HEARTBEAT_FILE = "run_meta.heartbeat"
ARTIFACTS_DIR = "artifacts"
STATUS_FILE = "sim_status.json"
LLM_DEBUG_DIR = "llm_debug"

HEARTBEAT_SECONDS = 20
HEARTBEAT_STALE_SECONDS = 90
MAYBE_RUNNING_SECONDS = 45 * 60

RUNNING = "running"
STOPPED = "stopped"  # Started by record_run.py, which vanished without finishing.
FINISHED = "finished"
FAILED = "failed"  # record_run.py finished, and tau2 exited with an error.
COMPLETE = "complete"
MAYBE_RUNNING = "may_be_running"
INCOMPLETE = "incomplete"


class ArtifactScan(NamedTuple):
    attempts: int
    finished: int
    calls_in_progress: int
    newest_age_seconds: float | None


def read_meta(run_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((run_dir / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_diff(run_dir: Path) -> str | None:
    try:
        return (run_dir / DIFF_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def latest_session(meta: dict | None) -> dict | None:
    sessions = (meta or {}).get("sessions") or []
    return sessions[-1] if sessions else None


def _listing_age(folder: Path, name: str) -> float | None:
    """Seconds since a file changed, read from the folder listing."""
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name == name:
                    return time.time() - entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        pass
    return None


def heartbeat_age(run_dir: Path) -> float | None:
    return _listing_age(run_dir, HEARTBEAT_FILE)


def _subfolders(folder: Path) -> list[Path]:
    try:
        with os.scandir(folder) as entries:
            return [
                Path(entry.path)
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return []


def scan_artifacts(run_dir: Path) -> ArtifactScan:
    """Count attempts, and the model calls of those still unfinished."""
    attempts = finished = calls = 0
    newest: float | None = None
    now = time.time()
    for task_dir in _subfolders(run_dir / ARTIFACTS_DIR):
        for sim_dir in _subfolders(task_dir):
            attempts += 1
            if _listing_age(sim_dir, STATUS_FILE) is not None:
                finished += 1
                continue
            try:
                with os.scandir(sim_dir / LLM_DEBUG_DIR) as entries:
                    for entry in entries:
                        calls += 1
                        age = now - entry.stat(follow_symlinks=False).st_mtime
                        newest = age if newest is None else min(newest, age)
            except OSError:
                pass
    return ArtifactScan(attempts, finished, calls, newest)


def run_state(
    run_dir: Path,
    meta: dict | None,
    done: int | None,
    expected: int | None,
    newest_activity: float | None = None,
) -> str:
    session = latest_session(meta)
    if session is not None:
        if session.get("finished_at"):
            return FINISHED if session.get("exit_code") == 0 else FAILED
        age = heartbeat_age(run_dir)
        return RUNNING if age is not None and age < HEARTBEAT_STALE_SECONDS else STOPPED

    if done is not None and expected and done >= expected:
        return COMPLETE
    ages = [scan_results(run_dir).age_seconds]
    if newest_activity is not None:
        ages.append(newest_activity)
    return MAYBE_RUNNING if min(ages) < MAYBE_RUNNING_SECONDS else INCOMPLETE


def meta_summary(meta: dict | None) -> dict[str, Any] | None:
    """The fields shown in the runs list."""
    session = latest_session(meta)
    if session is None:
        return None
    git = session.get("git") or {}
    return {
        "note": session.get("note"),
        "design_version": session.get("design_version"),
        "commit": git.get("commit"),
        "branch": git.get("branch"),
        "code_changed": bool(git.get("changed_files") or git.get("untracked_files")),
        "started_at": session.get("started_at"),
        "finished_at": session.get("finished_at"),
        "exit_code": session.get("exit_code"),
        "command": session.get("command_line"),
        "judges": session.get("judges"),
        "sessions": len(meta.get("sessions") or []),
    }
