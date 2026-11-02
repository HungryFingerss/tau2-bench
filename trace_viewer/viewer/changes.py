"""Tell the page when a run has changed, without touching results.json.

The browser polls this every few seconds, so it must stay harmless during a live
benchmark save. It only lists folders. New data is then fetched through
viewer/store.py, which applies the safe-reading rules.
"""

import os
from pathlib import Path
from typing import Any

from viewer import runmeta
from viewer.store import RESULTS_FILE, Store


def _stamps(run_dir: Path) -> dict[str, tuple[int, int]]:
    """Modified time and size of the top-level files, from the folder listing."""
    stamps = {}
    try:
        with os.scandir(run_dir) as entries:
            for entry in entries:
                if entry.name in (RESULTS_FILE, runmeta.META_FILE):
                    stat = entry.stat(follow_symlinks=False)
                    stamps[entry.name] = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        pass
    return stamps


class Changes:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._meta: dict[str, tuple[Any, dict | None]] = {}

    def _read_meta(self, name: str, run_dir: Path, stamp: Any) -> dict | None:
        """run_meta.json is small and is not the benchmark's file, so this is safe."""
        cached = self._meta.get(name)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        meta = runmeta.read_meta(run_dir)
        self._meta[name] = (stamp, meta)
        return meta

    def snapshot(self, open_run: str | None = None) -> dict[str, Any]:
        """A token per run that changes whenever the run does.

        Only the open run's artifacts are scanned, so a long list of runs stays
        cheap to poll.
        """
        runs = {}
        for name in self.store.run_names():
            run_dir = self.store.simulations_dir / name
            stamps = _stamps(run_dir)
            meta = self._read_meta(name, run_dir, stamps.get(runmeta.META_FILE))
            scan = runmeta.scan_artifacts(run_dir) if name == open_run else None
            activity = scan.newest_age_seconds if scan else None
            state = runmeta.run_state(run_dir, meta, None, None, activity)
            token = [stamps.get(RESULTS_FILE), stamps.get(runmeta.META_FILE), state]
            if scan:
                token.extend([scan.attempts, scan.finished, scan.calls_in_progress])
            runs[name] = {"token": repr(token), "state": state}
        return {"runs": runs}
