"""Run replay_worker.py under the benchmark's .venv and cache what it returns.

The viewer's own environment does not contain tau2, and importing it takes seconds,
so replay happens in a short-lived process that exits when done.

The worker is handed data this process already holds and never touches
results.json, so viewer/store.py stays the only reader of that file.
"""

import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from viewer.store import REPO_ROOT

VIEWER_DIR = Path(__file__).resolve().parents[1]
WORKER = VIEWER_DIR / "replay_worker.py"
CACHE_DIR = VIEWER_DIR / ".cache" / "replay"
PYCACHE_DIR = VIEWER_DIR / ".cache" / "pycache"
CACHE_VERSION = 1
TIMEOUT_SECONDS = 120
SUPPORTED_DOMAIN = "retail"

# A change to any of these can change what a replay returns.
SOURCE_FILES = (
    "data/tau2/domains/retail/db.json",
    "src/tau2/domains/retail/data_model.py",
    "src/tau2/domains/retail/tools.py",
    "src/tau2/environment/environment.py",
    "src/tau2/evaluator/evaluator_env.py",
)


def benchmark_python() -> Path | None:
    for relative in (".venv/Scripts/python.exe", ".venv/bin/python"):
        path = REPO_ROOT / relative
        if path.is_file():
            return path
    return None


def worker_environment() -> dict[str, str]:
    """Environment variables for a worker under the benchmark's .venv.

    Compiled bytecode goes to the viewer's cache, never next to the benchmark's or
    fde_agent's code. Not writing bytecode at all would avoid that too, but then
    every worker compiles litellm again: 6.6 s instead of 4 s per start.
    """
    environment = {
        **os.environ,
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",  # Otherwise it fetches prices.
        "PYTHONPYCACHEPREFIX": str(PYCACHE_DIR),
    }
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    return environment


def uses_canonical_retail_db(info: dict) -> bool:
    """Only English retail runs start from data/tau2/domains/retail/db.json."""
    domain = (info.get("environment_info") or {}).get("domain_name")
    return domain == SUPPORTED_DOMAIN and info.get("language") in (None, "en")


def unavailable_reason(info: dict, sim: dict) -> str | None:
    """Why a simulation cannot be replayed, or None when it can."""
    if not uses_canonical_retail_db(info):
        return "Database replay supports English retail runs only."
    if not sim.get("messages"):
        return "This simulation has no conversation, so there is nothing to replay."
    return None


def _source_fingerprint() -> list:
    fingerprint: list = []
    for relative in SOURCE_FILES:
        try:
            stat = (REPO_ROOT / relative).stat()
            fingerprint.append([relative, stat.st_size, stat.st_mtime_ns])
        except OSError:
            fingerprint.append([relative, None, None])
    return fingerprint


def _cache_key(info: dict, task: dict, sim: dict) -> str:
    identity = [
        CACHE_VERSION,
        sim,
        task,
        info.get("language_artifacts"),
        _source_fingerprint(),
    ]
    text = json.dumps(identity, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


class Replayer:
    def __init__(self, cache_dir: Path = CACHE_DIR) -> None:
        self.cache_dir = cache_dir
        self._lock = threading.Lock()  # One worker at a time.

    def replay(self, info: dict, task: dict, sim: dict) -> dict[str, Any]:
        """Return the worker's result for one simulation, from cache if possible."""
        path = self.cache_dir / f"{_cache_key(info, task, sim)}.json"
        cached = self._read_cache(path)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._read_cache(path)  # Another request may have finished it.
            if cached is not None:
                return cached
            result = self._run_worker(info, task, sim)
            if result.get("ok"):
                self._write_cache(path, result)
            return result

    def replay_many(self, info: dict, tasks: list, sims: list) -> list[dict[str, Any]]:
        """Replay several simulations in one process, without using the cache."""
        with self._lock:
            return self._call_worker(
                {"info": info, "tasks": tasks, "simulations": sims}
            )

    def _run_worker(self, info: dict, task: dict, sim: dict) -> dict[str, Any]:
        payload = {"info": info, "tasks": [task], "simulations": [sim]}
        return self._call_worker(payload)[0]

    def _call_worker(self, payload: dict) -> list[dict[str, Any]]:
        python = benchmark_python()
        if python is None:
            return self._failure(
                payload, "The benchmark's .venv was not found. Run `uv sync` first."
            )
        try:
            finished = subprocess.run(
                [str(python), str(WORKER)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=REPO_ROOT,
                env=worker_environment(),
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return self._failure(
                payload, f"Replay took over {TIMEOUT_SECONDS} seconds."
            )
        except OSError as error:
            return self._failure(payload, f"Replay could not start: {error}")

        try:
            replies = json.loads(finished.stdout)
        except ValueError:
            tail = finished.stderr.strip().splitlines()[-1:] or ["no error output"]
            return self._failure(
                payload,
                f"Replay crashed (exit code {finished.returncode}): {tail[0]}",
            )
        return replies

    @staticmethod
    def _failure(payload: dict, message: str) -> list[dict[str, Any]]:
        return [
            {"sim_id": sim.get("id"), "ok": False, "error": message}
            for sim in payload["simulations"]
        ]

    @staticmethod
    def _read_cache(path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_cache(path: Path, result: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result), encoding="utf-8")
