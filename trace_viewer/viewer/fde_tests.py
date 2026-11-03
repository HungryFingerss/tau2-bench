"""Run fde_agent's own tests for the Tools page, and report each test's result.

The command is the one that works on Windows, from the repo root:
    uv run --with pytest python -m pytest fde_agent/tests -q
A plain `uv run pytest` can pick up a global Python without tau2. Three options are
added: a JUnit XML report, which gives each test's outcome and failure text, the
captured output of failing tests, and no pytest cache. Bytecode goes to the viewer's
cache, so a run adds nothing to fde_agent/.
"""

import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ElementTree
from collections import Counter
from pathlib import Path
from typing import Any

from viewer.replay import worker_environment
from viewer.store import REPO_ROOT

VIEWER_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "fde_agent" / "tests"
REPORT_FILE = VIEWER_DIR / ".cache" / "fde-tests" / "junit.xml"
TIMEOUT_SECONDS = 600
OUTPUT_LIMIT = 200_000
OUTCOMES = ("failed", "error", "passed", "skipped")
RESULT_TAGS = {"failure": "failed", "error": "error", "skipped": "skipped"}
# pytest heads each captured stream like this, even when the stream is empty.
CAPTURE_HEADER = re.compile(r"^-+ Captured \w+ -+$", re.MULTILINE)


def _shown_path(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def pytest_arguments(target: Path, report: Path) -> list[str]:
    return [
        "run",
        "--with",
        "pytest",
        "python",
        "-m",
        "pytest",
        _shown_path(target),
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
        "-o",
        "junit_logging=out-err",
        "-o",
        "junit_log_passing_tests=false",
    ]


def _case_result(case: ElementTree.Element) -> dict[str, Any]:
    outcome, message, details = "passed", None, None
    for tag, name in RESULT_TAGS.items():
        node = case.find(tag)
        if node is not None:
            outcome, message, details = name, node.get("message"), node.text
            break
    output = "\n".join(
        cleaned
        for text in (case.findtext("system-out"), case.findtext("system-err"))
        if (cleaned := CAPTURE_HEADER.sub("", text or "").strip())
    )
    group = case.get("classname") or ""
    return {
        "group": group,
        "name": case.get("name") or "",
        "outcome": outcome,
        "seconds": float(case.get("time") or 0),
        "message": message,
        "details": details,
        "output": output or None,
    }


def read_report(path: Path) -> list[dict[str, Any]]:
    """One entry per test case in a JUnit XML report, in the order they ran."""
    root = ElementTree.parse(path).getroot()
    return [_case_result(case) for case in root.iter("testcase")]


def _summary_line(stdout: str) -> str | None:
    lines = [line.strip("= ").strip() for line in stdout.splitlines() if line.strip()]
    return lines[-1] if lines else None


class SuiteRunner:
    def __init__(self, target: Path = TESTS_DIR, report: Path = REPORT_FILE) -> None:
        self.target = target
        self.report = report
        self._lock = threading.Lock()  # One run at a time: they share a report file.

    def run(self) -> dict[str, Any]:
        if not self.target.is_dir():
            return {"ok": False, "error": f"{_shown_path(self.target)} was not found."}
        uv = shutil.which("uv")
        if uv is None:
            return {"ok": False, "error": "uv was not found on PATH."}
        arguments = pytest_arguments(self.target, self.report)
        command = " ".join(["uv", *arguments])
        environment = {**worker_environment(), "UV_FROZEN": "1"}  # See AGENTS.md.
        with self._lock:
            self.report.parent.mkdir(parents=True, exist_ok=True)
            self.report.unlink(missing_ok=True)
            started = time.perf_counter()
            try:
                finished = subprocess.run(
                    [uv, *arguments],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=REPO_ROOT,
                    env=environment,
                    timeout=TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                return {
                    "ok": False,
                    "command": command,
                    "error": f"The tests took over {TIMEOUT_SECONDS} s.",
                }
            except OSError as error:
                return {"ok": False, "command": command, "error": str(error)}
            seconds = round(time.perf_counter() - started, 2)
            output = (finished.stdout + finished.stderr)[-OUTPUT_LIMIT:]
            try:
                tests = read_report(self.report) if self.report.is_file() else None
            except ElementTree.ParseError:
                tests = None
            self.report.unlink(missing_ok=True)

        if tests is None:
            return {
                "ok": False,
                "command": command,
                "error": f"pytest wrote no report (exit code {finished.returncode}).",
                "output": output,
            }
        counts = Counter(test["outcome"] for test in tests)
        return {
            "ok": True,
            "command": command,
            "exit_code": finished.returncode,
            "seconds": seconds,
            "summary": _summary_line(finished.stdout),
            "counts": {outcome: counts.get(outcome, 0) for outcome in OUTCOMES},
            "tests": tests,
            "output": output,
        }
