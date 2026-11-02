"""Comparing two runs, and the design page behind each run."""

import pytest
from conftest import BASELINE
from fastapi.testclient import TestClient

from viewer.api import create_app
from viewer.compare import (
    IMPROVED,
    NOT_COMPARABLE,
    REGRESSED,
    SAME,
    change,
    compare_rows,
)

OLDER = "baseline-practice-10"


@pytest.mark.parametrize(
    "first, second, expected",
    [
        ("pass", "pass", SAME),
        ("fail", "fail", SAME),
        ("fail", "stopped", SAME),
        ("fail", "pass", IMPROVED),
        ("stopped", "pass", IMPROVED),
        ("pass", "fail", REGRESSED),
        ("pass", "stopped", REGRESSED),
        ("infra", "pass", NOT_COMPARABLE),
        ("pass", "infra", NOT_COMPARABLE),
        ("pending", "pass", NOT_COMPARABLE),
        (None, "pass", NOT_COMPARABLE),
        ("pass", None, NOT_COMPARABLE),
    ],
)
def test_change(first, second, expected):
    assert change(first, second) == expected


def row(task_id, result, **extra):
    return {"task_id": task_id, "trial": 0, "result": result, **extra}


def test_rows_cover_both_runs_in_numeric_order():
    compared = compare_rows(
        [row("10", "pass"), row("2", "fail")], [row("2", "pass"), row("3", "fail")]
    )
    assert [r["task_id"] for r in compared["rows"]] == ["2", "3", "10"]
    assert compared["rows"][1]["first"] is None
    assert compared["rows"][2]["second"] is None
    assert compared["counts"][IMPROVED] == 1
    assert compared["counts"][NOT_COMPARABLE] == 2


def test_labels_and_checks_travel_with_each_side():
    compared = compare_rows(
        [row("1", "fail", labels=["a"], checks={"x": 1})], [row("1", "fail")]
    )
    side = compared["rows"][0]["first"]
    assert side["labels"] == ["a"]
    assert side["checks"] == {"x": 1}
    assert compared["rows"][0]["second"]["labels"] == []


@pytest.fixture(scope="module")
def client(store) -> TestClient:
    return TestClient(create_app(store.simulations_dir))


def test_compare_route_on_real_runs(client):
    body = client.get("/api/compare", params={"a": OLDER, "b": BASELINE}).json()
    rows = {r["task_id"]: r for r in body["rows"]}
    assert len(rows) == 10
    assert rows["0"]["change"] == SAME
    assert rows["0"]["first"]["result"] == rows["0"]["second"]["result"] == "pass"
    assert rows["2"]["change"] == NOT_COMPARABLE
    assert rows["2"]["first"]["result"] == "infra"
    assert rows["2"]["second"]["result"] == "fail"
    assert rows["10"]["first"]["result"] == "pending"
    assert body["first"]["counts"]["passed"] == 1
    assert body["second"]["counts"]["passed"] == 3
    assert body["first"]["design"] == body["second"]["design"] == "v0-baseline"
    assert body["design_difference"] is None
    assert rows["1"]["second"]["checks"]["identity_first"]["state"] == "issue"


def test_compare_route_rejects_unknown_runs(client):
    params = {"a": BASELINE, "b": "nope"}
    assert client.get("/api/compare", params=params).status_code == 404


def test_design_page_uses_what_the_run_actually_sent(client):
    body = client.get(f"/api/runs/{BASELINE}/design").json()
    assert body["origin"] == "assumed"
    assert body["agent_implementation"] == "llm_agent"
    assert body["design"]["version"] == "v0-baseline"
    assert body["prompt_source"] == "saved_call"
    assert body["prompt_matches_design"] is True
    assert "Respond in English using Latin script" in body["prompt"]
    assert len(body["tools"]) == 16
    assert body["parameters"]["model"] == "ollama_chat/gpt-oss:20b"
    assert body["parameters"]["call_arguments"]["temperature"] == 0.0
    assert body["parameters"]["max_errors"] == 10
    assert body["source_status"] == {"changed": [], "missing": []}


def test_design_page_rebuilds_the_prompt_when_no_call_was_saved(client):
    body = client.get("/api/runs/local-smoke-test/design").json()
    assert body["prompt_source"] == "rebuilt"
    assert body["prompt_matches_design"] is None
    assert "Respond in English" in body["prompt"]
    assert body["tools"] is None


def test_run_page_carries_checks_and_analysis(client):
    run = client.get(f"/api/runs/{BASELINE}").json()
    assert run["design"] == {"version": "v0-baseline", "origin": "assumed"}
    row = next(r for r in run["rows"] if r["task_id"] == "6")
    assert row["checks"]["duplicates"] == {"state": "issue", "count": 6}
    assert len(run["check_definitions"]) == 5
    assert run["analysis"]["failures"] == 7


def test_designs_list_route(client):
    body = client.get("/api/designs").json()
    assert body["problems"] == []
    assert body["designs"][0]["version"] == "v0-baseline"
    assert body["designs"][0]["decisions"] == 18


def test_design_compare_route(client):
    params = {"a": "v0-baseline", "b": "v0-baseline"}
    assert client.get("/api/designs/compare", params=params).json()["same"] is True
    params = {"a": "v0-baseline", "b": "nope"}
    assert client.get("/api/designs/compare", params=params).status_code == 404


def test_source_route_only_serves_cited_files(client):
    good = client.get("/api/source", params={"file": "src/tau2/agent/llm_agent.py"})
    assert good.status_code == 200
    assert good.json()["lines"][23].startswith("AGENT_INSTRUCTION")
    for file in (".env", "pyproject.toml", "../../etc/passwd"):
        assert client.get("/api/source", params={"file": file}).status_code == 404


def test_meta_route_for_an_unrecorded_run(client):
    assert client.get(f"/api/runs/{BASELINE}/meta").json() == {
        "meta": None,
        "diff": None,
    }
