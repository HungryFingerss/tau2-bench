"""Counts and comparison: CHECKs per task and run, ignored inputs, model calls,
benchmark steps, the v0 to v1 decision map, and failure labels linked to F decisions.

Real-data tests use baseline-practice-10-v2 and the smoke run v1-smoke-task0, and
skip when either is missing. Label tests write to a temporary annotations folder.
"""

import json
import shutil

import pytest
from conftest import BASELINE
from fastapi.testclient import TestClient
from test_agent_log import SMOKE, SMOKE_DIR, SMOKE_SIM, needs_smoke, sim_from_log

from viewer import agent_log
from viewer.annotations import Annotations
from viewer.api import create_app
from viewer.artifacts import AGENT_CALL, AGENT_LOG_FILE, find_sim_dir, read_calls
from viewer.designs import DESIGNS_DIR, compare_designs, decision_map, load_design
from viewer.store import DEFAULT_SIMULATIONS_DIR, read_json_file
from viewer.verdicts import benchmark_steps, model_calls_in

V0 = load_design(DESIGNS_DIR / "v0-baseline.yaml")
V1 = load_design(DESIGNS_DIR / "v1-fde-agent.yaml")


def ev(kind: str, **fields) -> dict:
    return {"n": 0, "time": 0, "kind": kind, **fields}


# ---------- counting rules ----------


def test_ignored_inputs_are_broken_down_by_kind_and_input():
    events = [
        ev(
            "ignored_inputs",
            tool="prepare_change",
            change_kind="return_items",
            inputs=["address", "reason"],
        ),
        ev(
            "ignored_inputs",
            tool="prepare_change",
            change_kind="return_items",
            inputs=["address"],
        ),
        ev(
            "ignored_inputs",
            tool="prepare_change",
            change_kind="cancel_order",
            inputs=["items"],
        ),
    ]
    counts = agent_log.counts(events)
    assert counts["checks"]["ignored_inputs"] == 3
    assert counts["ignored_inputs"] == {
        "by_kind": {"return_items": 2, "cancel_order": 1},
        "by_input": {"address": 2, "reason": 1, "items": 1},
        "by_kind_and_input": {
            "return_items": {"address": 2, "reason": 1},
            "cancel_order": {"items": 1},
        },
    }


def test_every_check_kind_is_counted():
    events = [ev(kind) for kind in agent_log.CHECK_KINDS] + [ev("invalid_call")]
    checks = agent_log.counts(events)["checks"]
    assert checks == {**{kind: 1 for kind in agent_log.CHECK_KINDS}, "invalid_call": 2}


def test_steps_count_what_max_steps_limits():
    """Greeting: none. Customer, agent text, tool-call message, its tools' turn: one each."""
    messages = [
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "Help"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "a"}, {"id": "b"}],
        },
        {"role": "tool", "id": "a"},
        {"role": "tool", "id": "b"},
        {"role": "assistant", "content": "Done", "raw_data": {"x": 1}},
    ]
    assert benchmark_steps(messages) == 4
    assert model_calls_in(messages) == 1
    events = [
        ev("agent", by="benchmark", text="Hi"),
        ev("customer", text="Help"),
        ev("model_call"),
        ev("model_call"),  # A rejected call and its retry: one turn, no extra step.
        ev("store_calls", calls={"a": {}, "b": {}}),
        ev("store_results", results={}),
        ev("agent", by="model", text="Done"),
    ]
    counts = agent_log.counts(events)
    assert counts["benchmark_steps"] == 4
    assert counts["model_calls"] == 2


@needs_smoke
def test_log_and_results_count_the_same_steps_on_the_real_attempt():
    log = read_json_file(SMOKE_DIR / AGENT_LOG_FILE)
    sim = sim_from_log(
        log,
        {
            k: None
            for k in ("task_id", "trial", "seed", "start_time", "end_time", "duration")
        },
        "x",
    )
    assert (
        agent_log.counts(log["events"])["benchmark_steps"]
        == benchmark_steps(sim["messages"])
        == 30
    )


def test_add_counts_sums_nested_breakdowns():
    total = {}
    agent_log.add_counts(
        total,
        {
            "model_calls": 2,
            "checks": {"fixed": 1},
            "ignored_inputs": {"by_input": {"a": 1}},
            "in_results": True,
            "source": "x",
        },
    )
    agent_log.add_counts(
        total,
        {
            "model_calls": 3,
            "checks": {"fixed": 2},
            "ignored_inputs": {"by_input": {"a": 1, "b": 4}},
        },
    )
    assert total == {
        "model_calls": 5,
        "checks": {"fixed": 3},
        "ignored_inputs": {"by_input": {"a": 2, "b": 4}},
    }


# ---------- per task and per run, on the real runs ----------


@pytest.fixture(scope="module")
def real_client():
    if not (DEFAULT_SIMULATIONS_DIR / BASELINE).is_dir():
        pytest.skip(f"{BASELINE} is not on this machine")
    return TestClient(create_app(DEFAULT_SIMULATIONS_DIR))


@needs_smoke
def test_v1_task_counts_come_from_its_log(real_client):
    page = real_client.get(f"/api/runs/{SMOKE}").json()
    agent = page["rows"][0]["agent"]
    assert agent["source"] == "agent_log"
    assert agent["sim_id"] == SMOKE_SIM and agent["in_results"] is False
    assert (agent["model_calls"], agent["benchmark_steps"]) == (10, 30)
    assert (agent["tool_calls"], agent["store_calls"]) == (7, 18)
    assert agent["checks"]["invalid_call"] == 1
    assert sum(agent["checks"].values()) == 1
    totals = page["agent_totals"]
    assert totals["tasks_with_log"] == 1 and totals["tasks_not_in_results"] == 1
    assert totals["log"]["checks"]["invalid_call"] == 1


def test_baseline_counts_come_from_results_and_match_the_raw_files(real_client):
    """Model calls per task are checked against the llm_debug files on disk."""
    page = real_client.get(f"/api/runs/{BASELINE}").json()
    run_dir = DEFAULT_SIMULATIONS_DIR / BASELINE
    sims = {
        s["task_id"]: s for s in read_json_file(run_dir / "results.json")["simulations"]
    }
    for row in page["rows"]:
        agent = row["agent"]
        assert agent["source"] == "results"
        sim = sims[row["task_id"]]
        assert agent["benchmark_steps"] == benchmark_steps(sim["messages"])
        sim_dir = find_sim_dir(run_dir, row["task_id"], sim["id"])
        if sim_dir is None:  # Task 8's infra error: its attempts all failed.
            assert sim["termination_reason"] == "infrastructure_error"
            assert agent["model_calls"] == agent["benchmark_steps"] == 0
            continue
        files = [c for c in read_calls(sim_dir) if c.get("call_name") == AGENT_CALL]
        assert agent["model_calls"] == len(files), row["task_id"]
    totals = page["agent_totals"]
    assert totals["tasks_with_log"] == 0 and totals["log"] is None
    assert totals["model_calls"] == sum(r["agent"]["model_calls"] for r in page["rows"])


# ---------- the design map ----------


def test_the_map_reads_v1_against_the_baseline_decisions():
    mapping = decision_map(V0, V1)
    assert (mapping["base"], mapping["other"]) == ("v0-baseline", "v1-fde-agent")
    rows = {row["id"]: [d["id"] for d in row["replaced_by"]] for row in mapping["rows"]}
    assert list(rows) == [f"D{i}" for i in range(1, 19)]
    assert rows["D3"] == ["F6", "F23"]
    assert rows["D11"] == ["F10", "F11", "F12", "F19", "F20"]
    assert set(rows["D14"]) == {"F7", "F8", "F22"}
    assert [row for row, by in rows.items() if not by] == [
        "D5",
        "D9",
        "D10",
        "D16",
    ]
    assert [d["id"] for d in mapping["new"]] == ["F3", "F9", "F13", "F16", "F18"]
    assert mapping["replaced"] == 14     # D8 (full history resent) addressed by F26
    every_f = {d for by in rows.values() for d in by} | {
        d["id"] for d in mapping["new"]
    }
    assert every_f == {d["id"] for d in V1["decisions"]}


def test_the_map_works_in_either_order_and_only_with_addresses():
    assert decision_map(V1, V0)["base"] == "v0-baseline"
    assert decision_map(V0, V0) is None
    assert compare_designs(V0, V1)["mapping"]["replaced"] == 14
    assert compare_designs(V0, V0)["same"] is True


def test_the_design_compare_page_has_the_map(real_client):
    data = real_client.get(
        "/api/designs/compare", params={"a": "v1-fde-agent", "b": "v0-baseline"}
    ).json()
    assert data["mapping"]["base"] == "v0-baseline"
    assert [d["id"] for d in data["removed"]][:3] == [
        "F1",
        "F2",
        "F3",
    ]  # Natural order.


@needs_smoke
def test_compare_runs_shows_counts_and_the_map_side_by_side(real_client):
    data = real_client.get("/api/compare", params={"a": BASELINE, "b": SMOKE}).json()
    assert (data["first"]["agent"], data["second"]["agent"]) == (
        "llm_agent",
        "fde_agent",
    )
    task0 = next(r for r in data["rows"] if r["task_id"] == "0")
    assert (
        task0["second"]["model_calls"] == 10 and task0["second"]["in_results"] is False
    )
    assert task0["second"]["agent_checks"]["invalid_call"] == 1
    assert task0["first"]["model_calls"] == 8
    mapping = data["design_difference"]["mapping"]
    assert (mapping["base_run"], mapping["other_run"]) == (BASELINE, SMOKE)
    assert all(
        "base_failures" in row and "other_failures" in row for row in mapping["rows"]
    )


# ---------- failure labels linked to F decisions ----------


@pytest.fixture(scope="module")
def labelled(tmp_path_factory):
    """The baseline's results and a finished copy of the smoke attempt in one
    folder, with a fresh annotations folder."""
    if not (SMOKE_DIR / AGENT_LOG_FILE).is_file():
        pytest.skip(f"{SMOKE} is not on this machine")
    baseline = DEFAULT_SIMULATIONS_DIR / BASELINE
    if not baseline.is_dir():
        pytest.skip(f"{BASELINE} is not on this machine")
    root = tmp_path_factory.mktemp("sims")
    (root / BASELINE).mkdir()
    shutil.copy(baseline / "results.json", root / BASELINE / "results.json")
    run = root / "v1-finished"
    shutil.copytree(SMOKE_DIR, run / "artifacts" / "task_0" / f"sim_{SMOKE_SIM}")
    data = read_json_file(DEFAULT_SIMULATIONS_DIR / SMOKE / "results.json")
    template = next(
        s
        for s in read_json_file(baseline / "results.json")["simulations"]
        if s["task_id"] == "0"
    )
    log = read_json_file(SMOKE_DIR / AGENT_LOG_FILE)
    data["simulations"] = [sim_from_log(log, template, SMOKE_SIM)]
    (run / "results.json").write_text(json.dumps(data), encoding="utf-8")
    notes = Annotations(tmp_path_factory.mktemp("annotations"))
    return TestClient(create_app(root, notes=notes)), notes


def save(client, run, task, labels):
    return client.post(
        f"/api/runs/{run}/tasks/{task}/annotation", json={"labels": labels, "note": ""}
    )


def test_a_v1_task_offers_f_decisions_with_what_they_address(labelled):
    client, _ = labelled
    page = client.get("/api/runs/v1-finished/tasks/0").json()
    assert page["design_version"] == "v1-fde-agent"
    decisions = {d["id"]: d for d in page["design_decisions"]}
    assert set(decisions) == {f"F{i}" for i in range(1, 28)}
    assert decisions["F6"]["addresses"] == ["D3"]
    assert page["label_links"]["skipped-identity-check"] == ["F6", "F23"]


def test_labels_on_a_v1_task_link_f_decisions_and_refuse_d_ones(labelled):
    client, notes = labelled
    refused = save(
        client, "v1-finished", "0", [{"id": "malformed-tool-call", "decisions": ["D6"]}]
    )
    assert refused.status_code == 422
    saved = save(
        client,
        "v1-finished",
        "0",
        [{"id": "malformed-tool-call", "decisions": ["F14", "F15"]}],
    )
    assert saved.status_code == 200
    assert notes.read("v1-finished")["0:0"]["labels"][0]["decisions"] == ["F14", "F15"]


def test_whats_breaking_ranks_f_decisions_with_what_they_address(labelled):
    client, _ = labelled
    save(
        client,
        "v1-finished",
        "0",
        [{"id": "malformed-tool-call", "decisions": ["F14", "F15"]}],
    )
    analysis = client.get("/api/runs/v1-finished").json()["analysis"]
    buckets = {b["id"]: b for b in analysis["by_decision"]}
    assert set(buckets) == {"F14", "F15"}
    assert buckets["F14"]["addresses"] == ["D6", "D12", "D13", "D18"]
    assert buckets["F14"]["failures"] == [{"task_id": "0", "trial": 0}]


def test_compare_puts_each_runs_linked_failures_beside_the_baseline_decision(labelled):
    client, _ = labelled
    save(
        client,
        "v1-finished",
        "0",
        [{"id": "malformed-tool-call", "decisions": ["F14"]}],
    )
    save(client, BASELINE, "6", [{"id": "malformed-tool-call", "decisions": ["D13"]}])
    data = client.get("/api/compare", params={"a": BASELINE, "b": "v1-finished"}).json()
    rows = {row["id"]: row for row in data["design_difference"]["mapping"]["rows"]}
    assert rows["D13"]["base_failures"] == [{"task_id": "6", "trial": 0}]
    assert rows["D13"]["other_failures"] == [{"task_id": "0", "trial": 0}]  # Via F14.
    assert rows["D6"]["other_failures"] == [{"task_id": "0", "trial": 0}]
    assert rows["D3"]["other_failures"] == []
