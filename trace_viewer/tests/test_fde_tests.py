"""Running fde_agent's own tests from the Tools page.

A small throwaway suite checks the reporting, so these tests do not depend on
whether fde_agent's tests pass today. One test runs the real suite and checks only
that every test is reported and that nothing in fde_agent/ changed.
"""

import shutil
import subprocess
import textwrap

import pytest
from fastapi.testclient import TestClient

from viewer import fde_tests
from viewer.api import create_app
from viewer.fde_tests import SuiteRunner, pytest_arguments, read_report
from viewer.replay import benchmark_python

pytestmark = pytest.mark.usefixtures("fde_agent_untouched")

# The benchmark's .venv must exist: otherwise `uv run` would create it first.
needs_uv = pytest.mark.skipif(
    shutil.which("uv") is None or benchmark_python() is None,
    reason="uv or the benchmark's .venv is missing",
)

REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4">
<testcase classname="pkg.test_a" name="test_ok" time="0.010"/>
<testcase classname="pkg.test_a" name="test_bad" time="0.020">
<failure message="assert 1 == 2">long traceback</failure>
<system-out>----- Captured Out -----
printed by the test
----- Captured Err -----
</system-out></testcase>
<testcase classname="pkg.test_b" name="test_later" time="0">
<skipped message="not today"/>
<system-out>----- Captured Out -----
----- Captured Err -----</system-out></testcase>
<testcase classname="pkg.test_b" name="test_broken" time="0">
<error message="fixture failed">setup traceback</error></testcase>
</testsuite></testsuites>"""

SUITE = """
import pytest


def test_passes():
    assert True


def test_fails():
    print("what the tool said")
    assert 1 == 2


@pytest.mark.skip(reason="not today")
def test_skipped():
    pass
"""


def test_report_gives_each_outcome(tmp_path):
    path = tmp_path / "report.xml"
    path.write_text(REPORT, encoding="utf-8")
    tests = read_report(path)
    assert [(t["name"], t["outcome"]) for t in tests] == [
        ("test_ok", "passed"),
        ("test_bad", "failed"),
        ("test_later", "skipped"),
        ("test_broken", "error"),
    ]
    bad = tests[1]
    assert bad["group"] == "pkg.test_a" and bad["seconds"] == 0.02
    assert bad["message"] == "assert 1 == 2" and bad["details"] == "long traceback"
    assert bad["output"] == "printed by the test"
    assert tests[0]["output"] is None
    assert tests[2]["output"] is None  # Only empty capture headers.


def test_command_is_the_one_that_works_on_windows(tmp_path):
    arguments = pytest_arguments(fde_tests.TESTS_DIR, tmp_path / "r.xml")
    assert arguments[:7] == ["run", "--with", "pytest", "python", "-m", "pytest"] + [
        "fde_agent/tests"
    ]
    assert "-q" in arguments and "no:cacheprovider" in arguments


@needs_uv
def test_a_small_suite_reports_every_outcome(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_sample.py").write_text(textwrap.dedent(SUITE), encoding="utf-8")
    report = tmp_path / "report.xml"
    result = SuiteRunner(target=suite, report=report).run()

    assert result["ok"] and result["exit_code"] == 1
    assert result["counts"] == {"failed": 1, "error": 0, "passed": 1, "skipped": 1}
    failed = next(test for test in result["tests"] if test["outcome"] == "failed")
    assert failed["name"] == "test_fails"
    assert "assert 1 == 2" in failed["details"]
    assert "what the tool said" in failed["output"]
    assert "1 failed, 1 passed, 1 skipped" in result["summary"]
    assert not report.exists()


@needs_uv
@pytest.mark.skipif(not fde_tests.TESTS_DIR.is_dir(), reason="fde_agent/ is missing")
def test_the_real_suite_reports_every_test():
    result = SuiteRunner().run()
    assert result["ok"], result.get("error")
    assert sum(result["counts"].values()) == len(result["tests"]) > 0
    assert all(test["group"].startswith("fde_agent.tests.") for test in result["tests"])
    assert f"{result['counts']['passed']} passed" in result["summary"]


def test_a_missing_folder_is_reported(tmp_path):
    result = SuiteRunner(target=tmp_path / "none").run()
    assert not result["ok"] and "was not found" in result["error"]


def test_missing_uv_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(fde_tests.shutil, "which", lambda _: None)
    result = SuiteRunner(target=tmp_path).run()
    assert result == {"ok": False, "error": "uv was not found on PATH."}


def test_no_report_is_reported(tmp_path, monkeypatch):
    def finished(command, **kwargs):
        return subprocess.CompletedProcess(command, 4, stdout="usage error", stderr="")

    monkeypatch.setattr(fde_tests.shutil, "which", lambda _: "uv")
    monkeypatch.setattr(fde_tests.subprocess, "run", finished)
    result = SuiteRunner(target=tmp_path, report=tmp_path / "r.xml").run()
    assert not result["ok"]
    assert result["error"] == "pytest wrote no report (exit code 4)."
    assert result["output"] == "usage error"


class FakeSuite:
    runs = 0

    def run(self):
        FakeSuite.runs += 1
        return {"ok": True, "tests": []}


def test_tests_route_runs_the_suite_and_needs_json(tmp_path):
    client = TestClient(create_app(tmp_path, tests=FakeSuite()))
    assert client.post("/api/tools/tests", json={}).json() == {"ok": True, "tests": []}
    assert client.post("/api/tools/tests", content="run").status_code == 415
    assert FakeSuite.runs == 1
