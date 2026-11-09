"""Find runs and read their results.json without disturbing a live benchmark.

The benchmark saves results.json by writing a .results_* temp file and swapping it
in with os.replace. On Windows the swap fails with PermissionError if any handle on
results.json is open or being opened, and a failed swap loses a finished simulation.
FILE_SHARE_DELETE does not help, and neither does reading in chunks.

tests/stress_live_save.py measures this. What it showed, and the rules that follow:

- stat() opens a handle, so changes are detected from the folder listing only.
- open() can stall for 20-40 ms just after a swap, which is long enough to overlap
  the next one. So a changed file is not opened until it has been quiet for
  SETTLE_SECONDS, and the previous copy is served meanwhile.
- A save's temp file exists for far longer than a read of a settled file takes, so
  the file is never opened while a temp file exists.

Run start and resume write the file in place, so a read can also see partial JSON.
In that case the last good copy keeps being served until the next change.

fde_agent rewrites its own log, fde_agent_log.json, the same way after every turn,
and a failed swap there crashes the agent's turn. Store.read_settled reads such files
by the same rules.
"""

import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SIMULATIONS_DIR = REPO_ROOT / "data" / "simulations"
RESULTS_FILE = "results.json"
SAVE_TEMP_PREFIX = ".results_"  # See tau2.runner.checkpoint.create_checkpoint_fns.
ABANDONED_SAVE_SECONDS = 60  # A crashed run can leave its temp file behind.
SETTLE_SECONDS = 1.0
SAVE_WAIT_SECONDS = 2.0
SAVE_POLL_SECONDS = 0.02
MAX_PARSED_RUNS = 4
MAX_SETTLED_FILES = 32

Signature = tuple[int, int]


class UnknownRun(KeyError):
    """The requested run is not a folder in the simulations directory."""


@dataclass
class LoadedRun:
    name: str
    signature: Signature | None
    data: dict | None  # None when no readable version has been seen.
    problem: str | None = None  # Why the newest file could not be used.


class Scan(NamedTuple):
    signature: Signature | None
    age_seconds: float  # Since the file last changed.
    saving: bool  # A save's temp file exists.


class SettledRead(NamedTuple):
    data: Any  # The newest copy safe to read, or None if there is none yet.
    stale: bool  # A newer version exists that is not safe to open yet.
    problem: str | None


def read_json_file(path: Path) -> Any:
    """Parse a JSON file, holding it open only for one quick read."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # The benchmark's in-place writes use the Windows default encoding.
        text = raw.decode("cp1252", errors="replace")
    return json.loads(text)


def scan_file(folder: Path, name: str, temp_prefix: str) -> Scan:
    """Look at a file that is replaced by write-then-rename, from the folder listing.

    Never replace this with Path.stat() or Path.exists() on the file: those open a
    handle, which can break a save in a live run.
    """
    signature = None
    age = 0.0
    saving = False
    now = time.time()
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name == name:
                stat = entry.stat(follow_symlinks=False)
                signature = (stat.st_mtime_ns, stat.st_size)
                age = now - stat.st_mtime
            elif entry.name.startswith(temp_prefix):
                modified = entry.stat(follow_symlinks=False).st_mtime
                saving = saving or now - modified < ABANDONED_SAVE_SECONDS
    return Scan(signature, age, saving)


def scan_results(run_dir: Path) -> Scan:
    """Look at results.json through the folder listing alone."""
    return scan_file(run_dir, RESULTS_FILE, SAVE_TEMP_PREFIX)


def has_results(run_dir: Path) -> bool:
    return scan_results(run_dir).signature is not None


def is_safe_to_open(scan: Scan, settle_seconds: float) -> bool:
    # abs() because a file's clock can read slightly ahead of time.time().
    return not scan.saving and abs(scan.age_seconds) >= settle_seconds


class Store:
    """Run discovery plus a small cache of parsed results files."""

    def __init__(
        self,
        simulations_dir: Path | None = None,
        settle_seconds: float = SETTLE_SECONDS,
    ) -> None:
        self.simulations_dir = Path(simulations_dir or DEFAULT_SIMULATIONS_DIR)
        self.settle_seconds = settle_seconds
        self._lock = threading.Lock()
        self._runs: OrderedDict[str, LoadedRun] = OrderedDict()
        self._invalid: dict[str, tuple[Signature, str]] = {}
        self._memo: dict[tuple[str, str], tuple[Signature, Any]] = {}
        self._settled: OrderedDict[Path, tuple[Signature, Any]] = OrderedDict()

    def run_names(self) -> list[str]:
        if not self.simulations_dir.is_dir():
            return []
        return sorted(
            entry.name for entry in self.simulations_dir.iterdir() if entry.is_dir()
        )

    def run_dir(self, name: str) -> Path:
        """Return a run's folder. Only names that exist on disk are accepted."""
        if name not in self.run_names():
            raise UnknownRun(name)
        return self.simulations_dir / name

    def load(self, name: str) -> LoadedRun:
        """Return the newest results that are safe and possible to read."""
        run_dir = self.run_dir(name)
        with self._lock:
            scan = scan_results(run_dir)
            if scan.signature is None:
                return LoadedRun(
                    name, None, None, f"{RESULTS_FILE} does not exist yet."
                )

            cached = self._runs.get(name)
            if cached is not None:
                self._runs.move_to_end(name)
                if cached.signature == scan.signature:
                    return cached

            invalid = self._invalid.get(name)
            if invalid is not None and invalid[0] == scan.signature:
                return self._fallback(name, cached, scan.signature, invalid[1])

            if not is_safe_to_open(scan, self.settle_seconds):
                if cached is not None:
                    return cached  # A later request picks up the new version.
                scan = self._wait_until_safe(lambda: scan_results(run_dir))
                if not is_safe_to_open(scan, self.settle_seconds):
                    return self._fallback(
                        name, None, scan.signature, "A save is in progress."
                    )
            return self._read(name, run_dir / RESULTS_FILE, scan.signature, cached)

    def memo(self, name: str, key: str, compute: Callable[[], Any]) -> Any:
        """Cache a value derived from a run until the version being served changes."""
        loaded = self.load(name)
        if loaded.data is None:
            return compute()  # May be a passing state, so it must not be cached.
        signature = loaded.signature
        with self._lock:
            entry = self._memo.get((name, key))
        if entry is not None and entry[0] == signature:
            return entry[1]
        value = compute()
        with self._lock:
            self._memo[(name, key)] = (signature, value)
        return value

    def read_settled(self, path: Path, temp_prefix: str) -> SettledRead:
        """Read a JSON file that a live process replaces by write-then-rename.

        The same rules as results.json: changes are seen in the folder listing, and
        the file is opened only once it has been quiet and no temp file exists.
        Until then the last good copy is served, marked stale.
        """
        scan = scan_file(path.parent, path.name, temp_prefix)
        if scan.signature is None:
            return SettledRead(None, False, f"{path.name} does not exist yet.")
        with self._lock:
            cached = self._settled.get(path)
        if cached is not None and cached[0] == scan.signature:
            return SettledRead(cached[1], False, None)
        if not is_safe_to_open(scan, self.settle_seconds):
            if cached is not None:
                return SettledRead(cached[1], True, None)
            scan = self._wait_until_safe(
                lambda: scan_file(path.parent, path.name, temp_prefix)
            )
            if not is_safe_to_open(scan, self.settle_seconds):
                return SettledRead(None, True, f"{path.name} is being written.")
        try:
            data = read_json_file(path)
        except (OSError, ValueError) as error:
            old = cached[1] if cached is not None else None
            return SettledRead(old, True, f"{path.name} could not be read: {error}")
        with self._lock:
            self._settled[path] = (scan.signature, data)
            self._settled.move_to_end(path)
            while len(self._settled) > MAX_SETTLED_FILES:
                self._settled.popitem(last=False)
        return SettledRead(data, False, None)

    def _wait_until_safe(self, rescan: Callable[[], Scan]) -> Scan:
        deadline = time.monotonic() + self.settle_seconds + SAVE_WAIT_SECONDS
        scan = rescan()
        while (
            not is_safe_to_open(scan, self.settle_seconds)
            and time.monotonic() < deadline
        ):
            time.sleep(SAVE_POLL_SECONDS)
            scan = rescan()
        return scan

    def _read(
        self, name: str, path: Path, signature: Signature, cached: LoadedRun | None
    ) -> LoadedRun:
        try:
            data = read_json_file(path)
        except OSError as error:
            # Transient: not remembered, so the next request tries again.
            return self._fallback(name, cached, signature, f"Read failed: {error}")
        except ValueError as error:
            problem = f"{RESULTS_FILE} is unreadable: {error}"
            self._invalid[name] = (signature, problem)
            return self._fallback(name, cached, signature, problem)

        self._invalid.pop(name, None)
        loaded = LoadedRun(name, signature, data)
        self._runs[name] = loaded
        self._runs.move_to_end(name)
        while len(self._runs) > MAX_PARSED_RUNS:
            self._runs.popitem(last=False)
        return loaded

    @staticmethod
    def _fallback(
        name: str, cached: LoadedRun | None, signature: Signature | None, problem: str
    ) -> LoadedRun:
        if cached is None:
            return LoadedRun(name, signature, None, problem)
        return LoadedRun(
            name,
            cached.signature,
            cached.data,
            f"{problem} Showing the last good copy.",
        )
