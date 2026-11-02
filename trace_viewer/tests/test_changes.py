"""Auto-update tokens and run state, including that polling never opens results.json."""

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from conftest import BASELINE
from fastapi.testclient import TestClient

from viewer import insight, runmeta
from viewer.annotations import Annotations
from viewer.api import create_app
from viewer.changes import Changes
from viewer.designs import Designs
from viewer.store import RESULTS_FILE, Store


def make_run(root: Path, name: str, data: dict | None = None) -> Path:
    run = root / name
    run.mkdir()
    (run / RESULTS_FILE).write_text(json.dumps(data or {"info": {}}), encoding="utf-8")
    return run


def write_meta(run: Path, **session) -> None:
    meta = {"run": run.name, "sessions": [session]}
    (run / runmeta.META_FILE).write_text(json.dumps(meta), encoding="utf-8")


def age(path: Path, seconds: float) -> None:
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


def token(changes: Changes, name: str, open_run: str | None = None) -> str:
    return changes.snapshot(open_run)["runs"][name]["token"]


def test_new_run_appears(tmp_path):
    changes = Changes(Store(tmp_path))
    assert changes.snapshot()["runs"] == {}
    make_run(tmp_path, "new")
    assert "new" in changes.snapshot()["runs"]


def test_token_is_stable_when_nothing_changes(tmp_path):
    make_run(tmp_path, "run")
    changes = Changes(Store(tmp_path))
    assert token(changes, "run") == token(changes, "run")


def test_token_changes_with_results(tmp_path):
    run = make_run(tmp_path, "run")
    changes = Changes(Store(tmp_path))
    before = token(changes, "run")
    (run / RESULTS_FILE).write_text('{"info": {"a": 1}}', encoding="utf-8")
    assert token(changes, "run") != before


def test_token_changes_with_the_run_record(tmp_path):
    run = make_run(tmp_path, "run")
    changes = Changes(Store(tmp_path))
    before = token(changes, "run")
    write_meta(run, finished_at="now", exit_code=0)
    assert token(changes, "run") != before


def test_open_run_notices_a_new_attempt_and_new_calls(tmp_path):
    run = make_run(tmp_path, "run")
    changes = Changes(Store(tmp_path))
    before = token(changes, "run", "run")
    debug = run / "artifacts" / "task_1" / "sim_a" / "llm_debug"
    debug.mkdir(parents=True)
    second = token(changes, "run", "run")
    assert second != before
    (debug / "call.json").write_text("{}", encoding="utf-8")
    assert token(changes, "run", "run") != second


def test_polling_never_opens_or_stats_results_json(tmp_path, monkeypatch):
    """Both can break a live save on Windows. See viewer/store.py."""
    make_run(tmp_path, "run")
    real_stat = Path.stat
    real_open = Path.open

    def guarded_stat(self, **kwargs):
        assert self.name != RESULTS_FILE, "stat() on results.json"
        return real_stat(self, **kwargs)

    def guarded_open(self, *args, **kwargs):
        assert self.name != RESULTS_FILE, "open() on results.json"
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(Path, "open", guarded_open)

    def fail(*_):
        raise AssertionError("results.json was parsed while polling")

    monkeypatch.setattr("viewer.store.read_json_file", fail)
    Changes(Store(tmp_path)).snapshot("run")


def state(run: Path, meta: dict | None, done=None, expected=None, newest=None) -> str:
    return runmeta.run_state(run, meta, done, expected, newest)


def recorded(**session) -> dict:
    return {"sessions": [session]}


def test_fresh_heartbeat_means_running(tmp_path):
    run = make_run(tmp_path, "run")
    (run / runmeta.HEARTBEAT_FILE).touch()
    assert state(run, recorded(finished_at=None)) == runmeta.RUNNING


def test_stale_heartbeat_means_stopped(tmp_path):
    run = make_run(tmp_path, "run")
    heartbeat = run / runmeta.HEARTBEAT_FILE
    heartbeat.touch()
    age(heartbeat, runmeta.HEARTBEAT_STALE_SECONDS + 5)
    assert state(run, recorded(finished_at=None)) == runmeta.STOPPED


def test_missing_heartbeat_means_stopped(tmp_path):
    run = make_run(tmp_path, "run")
    assert state(run, recorded(finished_at=None)) == runmeta.STOPPED


def test_silent_model_call_is_still_running_if_the_heartbeat_is_fresh(tmp_path):
    """One call can be silent for 40 minutes, so activity cannot decide this."""
    run = make_run(tmp_path, "run")
    age(run / RESULTS_FILE, 3600)
    (run / runmeta.HEARTBEAT_FILE).touch()
    assert state(run, recorded(finished_at=None)) == runmeta.RUNNING


def test_finished_and_failed(tmp_path):
    run = make_run(tmp_path, "run")
    finished = recorded(finished_at="t", exit_code=0)
    assert state(run, finished) == runmeta.FINISHED
    failed = recorded(finished_at="t", exit_code=1)
    assert state(run, failed) == runmeta.FAILED


def test_latest_session_decides(tmp_path):
    run = make_run(tmp_path, "run")
    (run / runmeta.HEARTBEAT_FILE).touch()
    meta = {
        "sessions": [{"finished_at": "t", "exit_code": 1}, {"finished_at": None}],
    }
    assert state(run, meta) == runmeta.RUNNING


def test_unrecorded_complete_run(tmp_path):
    run = make_run(tmp_path, "run")
    assert state(run, None, 10, 10) == runmeta.COMPLETE


def test_unrecorded_recent_incomplete_run_may_be_running(tmp_path):
    run = make_run(tmp_path, "run")
    assert state(run, None, 4, 10) == runmeta.MAYBE_RUNNING


def test_unrecorded_old_incomplete_run(tmp_path):
    run = make_run(tmp_path, "run")
    age(run / RESULTS_FILE, runmeta.MAYBE_RUNNING_SECONDS + 60)
    assert state(run, None, 4, 10) == runmeta.INCOMPLETE


def test_recent_artifact_activity_counts_for_unrecorded_runs(tmp_path):
    run = make_run(tmp_path, "run")
    age(run / RESULTS_FILE, runmeta.MAYBE_RUNNING_SECONDS + 60)
    assert state(run, None, 4, 10, newest=30) == runmeta.MAYBE_RUNNING


def test_artifact_scan_counts_unfinished_work(tmp_path):
    run = make_run(tmp_path, "run")
    finished = run / "artifacts" / "task_1" / "sim_a"
    finished.mkdir(parents=True)
    (finished / runmeta.STATUS_FILE).write_text("{}", encoding="utf-8")
    active = run / "artifacts" / "task_2" / "sim_b" / "llm_debug"
    active.mkdir(parents=True)
    (active / "1.json").write_text("{}", encoding="utf-8")
    (active / "2.json").write_text("{}", encoding="utf-8")

    scan = runmeta.scan_artifacts(run)
    assert (scan.attempts, scan.finished, scan.calls_in_progress) == (2, 1, 2)
    assert scan.newest_age_seconds < 60


def test_artifact_scan_of_a_run_with_no_artifacts(tmp_path):
    scan = runmeta.scan_artifacts(make_run(tmp_path, "run"))
    assert scan == (0, 0, 0, None)


def test_summary_of_a_recorded_run():
    meta = recorded(
        note="baseline",
        design_version="v0-baseline",
        git={"commit": "abc", "changed_files": ["a"], "untracked_files": []},
        started_at="t",
        command_line="uv run",
    )
    summary = runmeta.meta_summary(meta)
    assert summary["note"] == "baseline"
    assert summary["design_version"] == "v0-baseline"
    assert summary["code_changed"] is True


def test_summary_with_clean_code():
    meta = recorded(git={"changed_files": [], "untracked_files": []})
    assert runmeta.meta_summary(meta)["code_changed"] is False


def test_unrecorded_run_has_no_summary():
    assert runmeta.meta_summary(None) is None


def test_runs_list_falls_back_to_the_recorded_judge(store, tmp_path):
    """With no saved judge call, the run script's record still names the model."""
    run = tmp_path / "run"
    run.mkdir()
    shutil.copy(store.run_dir(BASELINE) / RESULTS_FILE, run / RESULTS_FILE)
    age(run / RESULTS_FILE, 10)
    write_meta(run, design_version="v0-baseline", judges={"nl": "recorded-judge"})

    context = insight.Context(
        Store(tmp_path), Designs(), Annotations(tmp_path / "annotations")
    )
    listed = insight.runs_view(context)["runs"][0]
    assert listed["judges"]["nl"] == "recorded-judge"


def test_corrupt_record_reads_as_none(tmp_path):
    run = make_run(tmp_path, "run")
    (run / runmeta.META_FILE).write_text("{", encoding="utf-8")
    assert runmeta.read_meta(run) is None


@pytest.fixture
def client(store) -> TestClient:
    return TestClient(create_app(store.simulations_dir))


def test_changes_route(client):
    body = client.get("/api/changes", params={"run": BASELINE}).json()
    assert BASELINE in body["runs"]
    assert body["runs"][BASELINE]["token"]


def test_existing_runs_show_as_not_recorded(client):
    runs = {r["name"]: r for r in client.get("/api/runs").json()["runs"]}
    baseline = runs[BASELINE]
    assert baseline["recorded"] is None
    assert baseline["state"] == runmeta.COMPLETE
    assert baseline["design_version"] == "v0-baseline"
    assert baseline["design_origin"] == "assumed"


def test_older_incomplete_run_state(client):
    runs = {r["name"]: r for r in client.get("/api/runs").json()["runs"]}
    assert runs["baseline-practice-10"]["state"] == runmeta.INCOMPLETE
