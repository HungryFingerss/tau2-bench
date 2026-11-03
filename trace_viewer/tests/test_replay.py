"""Replay through the benchmark's own evaluator, and how failures are reported.

Real-run tests start replay_worker.py under the benchmark's .venv, once for the
whole module.
"""

import json
import subprocess
from pathlib import Path

import pytest
from conftest import BASELINE
from fastapi.testclient import TestClient

from viewer import pages, replay
from viewer.api import create_app
from viewer.dbdiff import ADDED, DIFFERENT, MISSED, compare_records, summarize
from viewer.replay import Replayer, benchmark_python, unavailable_reason

needs_benchmark = pytest.mark.skipif(
    benchmark_python() is None, reason="the benchmark's .venv is missing"
)

INFO = {"environment_info": {"domain_name": "retail"}, "language": "en"}
TASK = {"id": "1"}
SIM = {"id": "a", "task_id": "1", "messages": [{"role": "user"}]}


@pytest.fixture(scope="module")
def replies(store) -> dict:
    """Every replayable simulation of the real run, by task ID."""
    data = store.load(BASELINE).data
    sims = [sim for sim in data["simulations"] if sim["messages"]]
    results = Replayer().replay_many(data["info"], data["tasks"], sims)
    assert all(reply["ok"] for reply in results), results
    return {sim["task_id"]: r for sim, r in zip(sims, results)}


def differing(reply: dict) -> dict:
    records = compare_records(reply["records"])
    return {
        row["field"]: row
        for record in records
        for row in record["fields"]
        if row["kind"] != "both"
    }


@needs_benchmark
def test_replay_reproduces_every_recorded_verdict(store, replies):
    sims = {s["task_id"]: s for s in store.load(BASELINE).data["simulations"]}
    assert set(replies) == {"0", "1", "2", "3", "4", "6", "7", "10", "11"}
    for task_id, reply in replies.items():
        db_check = sims[task_id]["reward_info"]["db_check"]
        if db_check is not None:
            assert reply["db_match"] == db_check["db_match"], task_id


@needs_benchmark
def test_field_diff_agrees_with_the_benchmark_hash(replies):
    """No differences exactly when the benchmark's two hashes are equal."""
    for reply in replies.values():
        hashes_equal = reply["expected_hash"] == reply["agent_hash"]
        assert hashes_equal == (not differing(reply))


@needs_benchmark
def test_replay_starts_from_db_json(replies):
    assert all(reply["initial_matches_db_json"] for reply in replies.values())


@needs_benchmark
@pytest.mark.parametrize("task_id", ["0", "2", "4", "10", "11"])
def test_matching_tasks_have_no_differences(replies, task_id):
    assert replies[task_id]["db_match"] is True
    assert differing(replies[task_id]) == {}


@needs_benchmark
def test_task_1_agent_exchanged_one_extra_item(replies):
    rows = differing(replies["1"])
    assert set(rows) == {
        "exchange_items",
        "exchange_new_items",
        "exchange_price_difference",
    }
    assert rows["exchange_items"]["expected"] == ["4983901480"]
    assert rows["exchange_items"]["agent"] == ["1151293680", "4983901480"]
    assert rows["exchange_price_difference"]["expected"] == -13.46
    assert rows["exchange_price_difference"]["agent"] == -40.88
    assert {row["kind"] for row in rows.values()} == {DIFFERENT}


@needs_benchmark
def test_task_3_agent_changed_nothing(replies):
    reply = replies["3"]
    rows = differing(reply)
    assert len(rows) == 6
    assert {row["kind"] for row in rows.values()} == {MISSED}
    assert reply["agent_hash"] == reply["initial_hash"]
    assert all(record["id"] == "#W4776164" for record in reply["records"])


@needs_benchmark
def test_task_6_gets_a_verdict_the_benchmark_never_scored(replies):
    assert replies["6"]["db_match"] is False
    assert set(differing(replies["6"])) == {
        "exchange_items",
        "exchange_new_items",
        "exchange_price_difference",
    }


@needs_benchmark
def test_task_7_wrong_replacement_item(replies):
    rows = differing(replies["7"])
    assert set(rows) == {"exchange_new_items", "exchange_price_difference"}
    assert rows["exchange_price_difference"]["expected"] == 5.08
    assert rows["exchange_price_difference"]["agent"] == -2.7


@needs_benchmark
def test_summary_of_a_real_failure(replies):
    summary = summarize(compare_records(replies["3"]["records"]))
    assert summary["fields_differ"] == summary[MISSED] == 6
    assert summary[ADDED] == summary[DIFFERENT] == 0
    assert summary["records_differ"] == 1


@needs_benchmark
@pytest.mark.parametrize("task_id", ["2", "3", "4"])
def test_failed_reference_action_is_reported(replies, task_id):
    notes = replies[task_id]["notes"]
    assert len(notes) == 1
    assert "6086499569" in notes[0]
    assert "Product not found" in notes[0]


@needs_benchmark
def test_no_notes_when_nothing_went_wrong(replies):
    assert replies["0"]["notes"] == []


@needs_benchmark
def test_drifted_data_is_refused_not_guessed(store):
    data = store.load(BASELINE).data
    info = json.loads(json.dumps(data["info"]))
    info["language_artifacts"]["scenario_sha256"] = "0" * 64
    sim = next(s for s in data["simulations"] if s["messages"])
    task = next(t for t in data["tasks"] if t["id"] == sim["task_id"])
    reply = Replayer().replay_many(info, [task], [sim])[0]
    assert reply["ok"] is False
    assert "scenario binding" in reply["error"]


@needs_benchmark
def test_route_returns_a_comparison(store, tmp_path):
    client = TestClient(create_app(store.simulations_dir, Replayer(tmp_path)))
    task = client.get(f"/api/runs/{BASELINE}/tasks/7").json()
    assert task["links_ids"] is True
    url = f"/api/runs/{BASELINE}/sims/{task['sim']['id']}/db-diff"
    body = client.get(url).json()
    assert body["available"] is True
    assert body["db_match"] is False
    assert body["recorded_db_match"] is False
    assert body["agrees"] is True
    assert body["summary"]["fields_differ"] == 2
    assert body["summary"]["records_differ"] == 1

    assert client.get(url).json() == body  # Second answer comes from the cache.


def test_infra_task_cannot_be_replayed_and_no_worker_starts(store, monkeypatch):
    class NoWorker(Replayer):
        def _call_worker(self, payload):
            raise AssertionError("no worker should start")

    data = store.load(BASELINE).data
    sim = next(s for s in data["simulations"] if s["task_id"] == "8")
    page = pages.db_diff(store, NoWorker(), BASELINE, sim["id"])
    assert page["available"] is False
    assert "no conversation" in page["reason"]


@pytest.mark.parametrize(
    "info, sim, expected",
    [
        (INFO, SIM, None),
        (INFO, {"messages": []}, "no conversation"),
        (INFO, {"messages": None}, "no conversation"),
        ({**INFO, "language": "es"}, SIM, "English retail"),
        ({"environment_info": {"domain_name": "airline"}}, SIM, "English retail"),
    ],
)
def test_unavailable_reasons(info, sim, expected):
    reason = unavailable_reason(info, sim)
    assert reason is None if expected is None else expected in reason


class CountingReplayer(Replayer):
    def __init__(self, cache_dir: Path, result: dict) -> None:
        super().__init__(cache_dir)
        self.result = result
        self.calls = 0

    def _run_worker(self, info, task, sim):
        self.calls += 1
        return self.result


def test_success_is_cached_on_disk(tmp_path):
    first = CountingReplayer(tmp_path, {"ok": True, "records": []})
    first.replay(INFO, TASK, SIM)
    first.replay(INFO, TASK, SIM)
    assert first.calls == 1

    restarted = CountingReplayer(tmp_path, {"ok": True, "records": []})
    restarted.replay(INFO, TASK, SIM)
    assert restarted.calls == 0


def test_failure_is_not_cached(tmp_path):
    replayer = CountingReplayer(tmp_path, {"ok": False, "error": "boom"})
    replayer.replay(INFO, TASK, SIM)
    replayer.replay(INFO, TASK, SIM)
    assert replayer.calls == 2


def test_a_different_simulation_is_not_served_from_the_cache(tmp_path):
    replayer = CountingReplayer(tmp_path, {"ok": True, "records": []})
    replayer.replay(INFO, TASK, SIM)
    replayer.replay(INFO, TASK, {**SIM, "messages": [{"role": "assistant"}]})
    assert replayer.calls == 2


def test_changed_benchmark_source_invalidates_the_cache(tmp_path, monkeypatch):
    replayer = CountingReplayer(tmp_path, {"ok": True, "records": []})
    replayer.replay(INFO, TASK, SIM)
    monkeypatch.setattr(replay, "_source_fingerprint", lambda: ["changed"])
    replayer.replay(INFO, TASK, SIM)
    assert replayer.calls == 2


def test_missing_benchmark_environment_is_explained(monkeypatch):
    monkeypatch.setattr(replay, "benchmark_python", lambda: None)
    reply = Replayer().replay_many(INFO, [TASK], [SIM])[0]
    assert reply["ok"] is False
    assert "uv sync" in reply["error"]


def test_worker_crash_is_explained(monkeypatch, tmp_path):
    monkeypatch.setattr(replay, "benchmark_python", lambda: Path("python"))
    crashed = subprocess.CompletedProcess([], 1, stdout="", stderr="Boom: details\n")
    monkeypatch.setattr(replay.subprocess, "run", lambda *a, **k: crashed)
    reply = Replayer(tmp_path).replay_many(INFO, [TASK], [SIM])[0]
    assert reply["ok"] is False
    assert "exit code 1" in reply["error"]
    assert "Boom: details" in reply["error"]


def test_worker_timeout_is_explained(monkeypatch, tmp_path):
    monkeypatch.setattr(replay, "benchmark_python", lambda: Path("python"))

    def too_slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="python", timeout=1)

    monkeypatch.setattr(replay.subprocess, "run", too_slow)
    reply = Replayer(tmp_path).replay_many(INFO, [TASK], [SIM])[0]
    assert "seconds" in reply["error"]


def test_worker_is_handed_data_and_never_a_results_path(monkeypatch, tmp_path):
    """Opening results.json can break a live save, so the worker must not need it."""
    seen = {}

    def capture(command, **kwargs):
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setattr(replay, "benchmark_python", lambda: Path("python"))
    monkeypatch.setattr(replay.subprocess, "run", capture)
    Replayer(tmp_path).replay_many(INFO, [TASK], [SIM])

    assert [Path(part).name for part in seen["command"]] == [
        "python",
        "replay_worker.py",
    ]
    assert json.loads(seen["input"])["simulations"] == [SIM]
    assert seen["env"]["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert seen["env"]["PYTHONPYCACHEPREFIX"] == str(replay.PYCACHE_DIR)
    assert "VIRTUAL_ENV" not in seen["env"]


def test_worker_source_never_mentions_opening_files():
    source = replay.WORKER.read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    assert "open(" not in code
    assert "Results.load" not in code
