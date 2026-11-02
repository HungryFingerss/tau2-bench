"""Cases the real run does not cover: live, partial, unsupported and sparse data."""

import json
import os
import time
from pathlib import Path

import pytest

from viewer import pages
from viewer.artifacts import join_steps, read_calls
from viewer.store import (
    ABANDONED_SAVE_SECONDS,
    RESULTS_FILE,
    SAVE_TEMP_PREFIX,
    Store,
    UnknownRun,
)
from viewer.timeline import build_timeline


def results(**overrides) -> dict:
    task = {
        "id": "1",
        "user_scenario": {"instructions": "Help me."},
        "evaluation_criteria": {"reward_basis": ["DB"]},
    }
    base = {
        "timestamp": "2026-01-01T00:00:00",
        "info": {"num_trials": 1, "agent_info": {}, "user_info": {}},
        "tasks": [task],
        "simulations": [],
    }
    return {**base, **overrides}


def write_run(root: Path, name: str, data: dict | str) -> Path:
    run = root / name
    run.mkdir(exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (run / RESULTS_FILE).write_text(text, encoding="utf-8")
    return run


def test_only_folders_on_disk_are_valid_runs(tmp_path):
    store = Store(tmp_path, settle_seconds=0)
    write_run(tmp_path, "real", results())
    assert store.run_names() == ["real"]
    for name in ("missing", "..", "../real", "real/../.."):
        with pytest.raises(UnknownRun):
            store.run_dir(name)


def test_partial_write_keeps_serving_the_last_good_copy(tmp_path):
    store = Store(tmp_path, settle_seconds=0)
    run = write_run(tmp_path, "live", results())
    assert store.load("live").problem is None

    (run / RESULTS_FILE).write_text('{"timestamp": "cut off', encoding="utf-8")
    partial = store.load("live")
    assert partial.data["timestamp"] == "2026-01-01T00:00:00"
    assert "last good copy" in partial.problem

    (run / RESULTS_FILE).write_text(json.dumps(results(timestamp="new")), "utf-8")
    assert store.load("live").data["timestamp"] == "new"


def test_partial_first_read_is_reported_not_raised(tmp_path):
    store = Store(tmp_path, settle_seconds=0)
    write_run(tmp_path, "live", '{"timestamp"')
    summary = pages.run_summary(store, "live")
    assert summary["status"] == "unreadable"
    assert "unreadable" in summary["problem"]


def test_unchanged_file_is_not_read_again(tmp_path, monkeypatch):
    store = Store(tmp_path, settle_seconds=0)
    write_run(tmp_path, "run", results())
    store.load("run")

    def fail(_):
        raise AssertionError("results.json was opened although it did not change")

    monkeypatch.setattr("viewer.store.read_json_file", fail)
    store.load("run")


def test_unchanged_bad_file_is_not_read_again(tmp_path, monkeypatch):
    store = Store(tmp_path, settle_seconds=0)
    write_run(tmp_path, "run", "{bad")
    store.load("run")

    def fail(_):
        raise AssertionError("results.json was opened although it did not change")

    monkeypatch.setattr("viewer.store.read_json_file", fail)
    assert store.load("run").data is None


def test_transient_read_error_is_retried(tmp_path, monkeypatch):
    """A lost race must not mark that version of the file as bad."""
    store = Store(tmp_path, settle_seconds=0)
    write_run(tmp_path, "run", results())

    def denied(_):
        raise PermissionError("[WinError 5] Access is denied")

    with monkeypatch.context() as patch:
        patch.setattr("viewer.store.read_json_file", denied)
        assert store.load("run").data is None

    assert store.load("run").data["timestamp"] == "2026-01-01T00:00:00"


def test_passing_read_problem_does_not_stick_to_the_summary(tmp_path, monkeypatch):
    """The same file version must show its data once a read finally succeeds."""
    write_run(tmp_path, "run", results())
    store = Store(tmp_path, settle_seconds=0)

    def denied(_):
        raise PermissionError("[WinError 5] Access is denied")

    with monkeypatch.context() as patch:
        patch.setattr("viewer.store.read_json_file", denied)
        assert pages.run_summary(store, "run")["status"] == "unreadable"

    summary = pages.run_summary(store, "run")
    assert summary["status"] == "ok"
    assert summary["date"] == "2026-01-01T00:00:00"


def test_results_file_is_never_opened_during_a_save(tmp_path, monkeypatch):
    run = write_run(tmp_path, "run", results())
    store = Store(tmp_path, settle_seconds=0)
    assert store.load("run").problem is None

    (run / RESULTS_FILE).write_text(json.dumps(results(timestamp="new")), "utf-8")
    (run / f"{SAVE_TEMP_PREFIX}abc.json").write_text("{", encoding="utf-8")

    def fail(_):
        raise AssertionError("results.json was opened during a save")

    with monkeypatch.context() as patch:
        patch.setattr("viewer.store.read_json_file", fail)
        during = store.load("run")
    assert during.data["timestamp"] == "2026-01-01T00:00:00"

    (run / f"{SAVE_TEMP_PREFIX}abc.json").unlink()
    assert store.load("run").data["timestamp"] == "new"


def test_first_read_during_a_save_reports_it_instead_of_opening(tmp_path, monkeypatch):
    run = write_run(tmp_path, "run", results())
    (run / f"{SAVE_TEMP_PREFIX}abc.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr("viewer.store.SAVE_WAIT_SECONDS", 0.02)

    def fail(_):
        raise AssertionError("results.json was opened during a save")

    monkeypatch.setattr("viewer.store.read_json_file", fail)
    loaded = Store(tmp_path, settle_seconds=0).load("run")
    assert loaded.data is None
    assert "save is in progress" in loaded.problem


def test_changed_file_is_not_opened_until_it_has_settled(tmp_path, monkeypatch):
    """open() stalls just after a swap, long enough to overlap the next one."""
    run = write_run(tmp_path, "run", results())
    settled = time.time() - 10
    os.utime(run / RESULTS_FILE, (settled, settled))
    store = Store(tmp_path, settle_seconds=5)
    assert store.load("run").data["timestamp"] == "2026-01-01T00:00:00"

    (run / RESULTS_FILE).write_text(json.dumps(results(timestamp="new")), "utf-8")

    def fail(_):
        raise AssertionError("results.json was opened right after it changed")

    with monkeypatch.context() as patch:
        patch.setattr("viewer.store.read_json_file", fail)
        just_changed = store.load("run")
    assert just_changed.data["timestamp"] == "2026-01-01T00:00:00"
    assert just_changed.problem is None

    os.utime(run / RESULTS_FILE, (settled, settled))
    assert store.load("run").data["timestamp"] == "new"


def test_summary_is_not_cached_under_a_version_it_did_not_read(tmp_path):
    run = write_run(tmp_path, "run", results())
    settled = time.time() - 10
    os.utime(run / RESULTS_FILE, (settled, settled))
    store = Store(tmp_path, settle_seconds=5)
    assert pages.run_summary(store, "run")["date"] == "2026-01-01T00:00:00"

    (run / RESULTS_FILE).write_text(json.dumps(results(timestamp="new")), "utf-8")
    assert pages.run_summary(store, "run")["date"] == "2026-01-01T00:00:00"

    os.utime(run / RESULTS_FILE, (settled + 1, settled + 1))
    assert pages.run_summary(store, "run")["date"] == "new"


def test_first_read_waits_for_the_file_to_settle(tmp_path):
    write_run(tmp_path, "run", results())
    started = time.monotonic()
    loaded = Store(tmp_path, settle_seconds=0.2).load("run")
    assert loaded.data is not None
    assert time.monotonic() - started >= 0.1


def test_leftover_temp_file_from_a_crash_does_not_block_reads(tmp_path):
    run = write_run(tmp_path, "run", results())
    leftover = run / f"{SAVE_TEMP_PREFIX}abc.json"
    leftover.write_text("{", encoding="utf-8")
    old = time.time() - ABANDONED_SAVE_SECONDS - 5
    os.utime(leftover, (old, old))
    assert Store(tmp_path, settle_seconds=0).load("run").problem is None


def test_results_file_is_never_statted(tmp_path, monkeypatch):
    """Path.stat() opens a handle on Windows, which can break a live save."""
    write_run(tmp_path, "run", results())
    real_stat = Path.stat

    def guarded(self, **kwargs):
        assert self.name != RESULTS_FILE, "stat() was called on results.json"
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded)
    store = Store(tmp_path, settle_seconds=0)
    pages.runs_page(store)
    pages.run_page(store, "run")
    pages.task_page(store, "run", "1", 0)


def test_windows_encoded_results_are_readable(tmp_path):
    store = Store(tmp_path, settle_seconds=0)
    run = tmp_path / "cp1252"
    run.mkdir()
    (run / RESULTS_FILE).write_bytes('{"timestamp": "I\x92ve"}'.encode("latin-1"))
    assert store.load("cp1252").data["timestamp"] == "I’ve"


def test_folders_without_usable_results(tmp_path):
    store = Store(tmp_path, settle_seconds=0)
    (tmp_path / "empty").mkdir()
    voice = write_run(tmp_path, "voice", results())
    (voice / "simulations").mkdir()
    write_run(
        tmp_path,
        "voice2",
        results(info={"num_trials": 1, "audio_native_config": {"provider": "x"}}),
    )
    bundle = tmp_path / "benchmark"
    bundle.mkdir()
    write_run(bundle, "en_retail", results())

    statuses = {run["name"]: run["status"] for run in pages.runs_page(store)["runs"]}
    assert statuses == {
        "benchmark": "unsupported",
        "empty": "no_results",
        "voice": "unsupported",
        "voice2": "unsupported",
    }
    assert pages.run_page(store, "voice")["rows"] == []


def test_incomplete_run_shows_pending_tasks_and_their_attempts(tmp_path):
    task = {"id": "2", "evaluation_criteria": {}}
    done = results()["tasks"][0]
    sim = {
        "id": "a",
        "task_id": "1",
        "trial": 0,
        "termination_reason": "user_stop",
        "reward_info": {
            "reward": 1.0,
            "reward_basis": ["DB"],
            "db_check": {"db_match": True},
        },
        "messages": [],
    }
    run = write_run(tmp_path, "run", results(tasks=[done, task], simulations=[sim]))
    (run / "artifacts" / "task_2" / "sim_running").mkdir(parents=True)

    store = Store(tmp_path, settle_seconds=0)
    page = pages.run_page(store, "run")
    assert page["summary"]["done_simulations"] == 1
    assert page["summary"]["expected_simulations"] == 2
    pending = page["rows"][1]
    assert pending["result"] == "pending"
    assert pending["attempts"]["no_status"] == 1

    detail = pages.task_page(store, "run", "2", 0)
    assert detail["sim"] is None
    assert detail["timeline"] == []
    assert detail["attempts"][0]["status"] == "no_status"


def test_string_instructions_and_missing_task(tmp_path):
    write_run(tmp_path, "run", results())
    store = Store(tmp_path, settle_seconds=0)
    assert (
        pages.task_page(store, "run", "1", 0)["task"]["reason_for_call"] == "Help me."
    )
    with pytest.raises(pages.NotFound):
        pages.task_page(store, "run", "99", 0)
    with pytest.raises(pages.NotFound):
        pages.task_page(store, "run", "1", 5)


def test_multi_tool_message_results_are_attached():
    messages = [
        {
            "role": "assistant",
            "raw_data": {"id": "x"},
            "tool_calls": [{"id": "a", "name": "one"}, {"id": "b", "name": "two"}],
        },
        {
            "role": "tool",
            "tool_messages": [
                {"id": "a", "content": "first"},
                {"id": "b", "content": "second", "error": True},
            ],
        },
    ]
    step = build_timeline(messages)[0]
    assert [c["result"]["content"] for c in step["tool_calls"]] == ["first", "second"]
    assert step["tool_calls"][1]["result"]["error"] is True


def test_latest_log_mode_never_guesses_a_match():
    """With only the last call saved, earlier steps must stay unmatched."""
    usage = {"prompt_tokens": 1, "completion_tokens": 1}
    later = {"prompt_tokens": 9, "completion_tokens": 1}
    messages = [
        {"role": "user", "content": "same", "usage": usage},
        {"role": "assistant", "content": "ok", "generation_time_seconds": 1.5},
        {"role": "user", "content": "same", "usage": later},
    ]
    saved = [
        {
            "call_name": "user_simulator_response",
            "response": {"content": "same", "usage": later},
        }
    ]
    assert list(join_steps(messages, saved)) == [2]


def test_call_file_being_written_is_skipped(tmp_path):
    folder = tmp_path / "llm_debug"
    folder.mkdir()
    good = "20260101_000000_001_agent_response_abcdef12.json"
    (folder / good).write_text('{"call_name": "agent_response"}', encoding="utf-8")
    bad = "20260101_000000_002_agent_response_abcdef13.json"
    (folder / bad).write_text('{"call_na', encoding="utf-8")
    (folder / "notes.txt").write_text("ignored", encoding="utf-8")
    assert [call["file"] for call in read_calls(tmp_path)] == [good]


def test_read_only_viewer_does_not_touch_run_files(tmp_path):
    run = write_run(tmp_path, "run", results())
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in run.rglob("*")}
    store = Store(tmp_path, settle_seconds=0)
    pages.runs_page(store)
    pages.run_page(store, "run")
    pages.task_page(store, "run", "1", 0)
    after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in run.rglob("*")}
    assert before == after
    assert os.listdir(tmp_path) == ["run"]
