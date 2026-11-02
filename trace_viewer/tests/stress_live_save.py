"""Manual check: can the viewer's reader break a live benchmark save on Windows?

Not collected by pytest, because it runs for minutes and writes files. Run it:

    uv run --project trace_viewer python trace_viewer/tests/stress_live_save.py

A writer process repeats the benchmark's save (tau2.runner.checkpoint._save_json)
on a copy of a real results.json: temp file, json.dump with indent=2, os.replace.
A reader process calls Store.load in a tight loop, far harder than the app ever
does. Any PermissionError in the writer is a save a live run would have lost.

The "ambush" scenarios are the worst case. Saves are timed to start just as the
file has been quiet long enough to be read, so reads and saves land together.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VIEWER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VIEWER_DIR))

from viewer.changes import Changes  # noqa: E402
from viewer.store import (  # noqa: E402
    DEFAULT_SIMULATIONS_DIR,
    RESULTS_FILE,
    SETTLE_SECONDS,
    Store,
)

SOURCE = DEFAULT_SIMULATIONS_DIR / "baseline-practice-10-v2" / RESULTS_FILE

# name, pause between saves in seconds (low, high), reader settle time
SCENARIOS = [
    ("back_to_back", (0, 0), SETTLE_SECONDS),
    ("paced_50ms", (0.05, 0.05), SETTLE_SECONDS),
    ("ambush_fast", (0.09, 0.11), 0.1),
    ("ambush_real_settle", (0.95, 1.05), SETTLE_SECONDS),
]


def writer(target: Path, seconds: float, gap: tuple[float, float]) -> None:
    data = json.loads(target.read_text(encoding="utf-8"))
    saved = failed = 0
    end = time.time() + seconds
    while time.time() < end:
        fd, tmp = tempfile.mkstemp(
            suffix=".json", prefix=".results_", dir=target.parent
        )
        with os.fdopen(fd, "w") as file:
            json.dump(data, file, indent=2)
        try:
            os.replace(tmp, target)
            saved += 1
        except PermissionError:
            failed += 1
            os.unlink(tmp)
        pause = random.uniform(*gap)
        if pause:
            time.sleep(pause)
    print(f"  saves: {saved + failed}, FAILED: {failed}", flush=True)
    sys.exit(1 if failed else 0)


def reader(target: Path, seconds: float, settle: float) -> None:
    store = Store(target.parent.parent, settle_seconds=settle)
    changes = Changes(store)
    loads = 0
    versions = set()
    end = time.time() + seconds
    while time.time() < end:
        changes.snapshot(target.parent.name)  # What the page polls for auto-update.
        loaded = store.load(target.parent.name)
        loads += 1
        if loaded.data is not None:
            versions.add(loaded.signature)
    print(f"  store.load calls: {loads}, versions read: {len(versions)}", flush=True)


def scenario(
    work: Path, name: str, seconds: float, gap: tuple[float, float], settle: float
) -> int:
    folder = work / name
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / RESULTS_FILE
    target.write_bytes(SOURCE.read_bytes())
    print(f"{name}: pause {gap[0]}-{gap[1]}s, settle {settle}s", flush=True)
    args = [sys.executable, __file__, "--target", str(target)]
    args += ["--seconds", str(seconds), "--settle", str(settle)]
    args += ["--gap-low", str(gap[0]), "--gap-high", str(gap[1])]
    write = subprocess.Popen([*args, "--role", "writer"])
    read = subprocess.Popen([*args, "--role", "reader"])
    read.wait()
    return write.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--only", help="Run one scenario, such as ambush_fast.")
    parser.add_argument("--role", choices=["writer", "reader"])
    parser.add_argument("--target", type=Path)
    parser.add_argument("--gap-low", type=float, default=0)
    parser.add_argument("--gap-high", type=float, default=0)
    parser.add_argument("--settle", type=float, default=SETTLE_SECONDS)
    args = parser.parse_args()

    if args.role == "writer":
        writer(args.target, args.seconds, (args.gap_low, args.gap_high))
    elif args.role == "reader":
        reader(args.target, args.seconds, args.settle)
    else:
        with tempfile.TemporaryDirectory() as work:
            codes = [
                scenario(Path(work), name, args.seconds, gap, settle)
                for name, gap, settle in SCENARIOS
                if args.only in (None, name)
            ]
        print("PASS: no save failed" if not any(codes) else "FAIL: saves were lost")
        return 1 if any(codes) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
