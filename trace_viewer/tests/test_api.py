"""Smoke-test the HTTP routes against the real run."""

import pytest
from conftest import BASELINE
from fastapi.testclient import TestClient

from viewer.api import create_app


def test_page_and_assets_are_served(client):
    assert client.get("/").status_code == 200
    for file in (
        "app.js",
        "database.js",
        "drawer.js",
        "ids.js",
        "replay.js",
        "util.js",
        "style.css",
    ):
        assert client.get(f"/static/{file}").status_code == 200


def test_runs_list(client):
    runs = client.get("/api/runs").json()["runs"]
    names = [run["name"] for run in runs]
    assert BASELINE in names
    assert runs == sorted(runs, key=lambda r: r.get("date") or "", reverse=True)


def test_walk_from_run_to_raw_call(client):
    run = client.get(f"/api/runs/{BASELINE}").json()
    row = next(row for row in run["rows"] if row["task_id"] == "2")

    task = client.get(f"/api/runs/{BASELINE}/tasks/2").json()
    assert task["sim"]["id"] == row["sim_id"]
    step = next(s for s in task["timeline"] if s["type"] == "agent")

    url = f"/api/runs/{BASELINE}/sims/{row['sim_id']}/steps/{step['index']}/call"
    raw = client.get(url).json()
    assert raw["call"]["call_name"] == "agent_response"
    assert raw["call"]["request"]["tools"]
    assert raw["results_record"]["raw_data"]["choices"]


def test_greeting_explains_why_it_has_no_call(client):
    task = client.get(f"/api/runs/{BASELINE}/tasks/0").json()
    url = f"/api/runs/{BASELINE}/sims/{task['sim']['id']}/steps/0/call"
    raw = client.get(url).json()
    assert raw["call"] is None
    assert "scripted" in raw["missing_reason"]


def test_attempt_routes(client):
    task = client.get(f"/api/runs/{BASELINE}/tasks/8").json()
    sim_id = task["attempts"][0]["sim_id"]
    base = f"/api/runs/{BASELINE}/attempts/8/{sim_id}"

    attempt = client.get(base).json()
    assert attempt["status_file"]["status"] == "failed"
    file = attempt["calls"][0]["file"]
    assert client.get(f"{base}/calls/{file}").json()["request"]["messages"]
    assert "Connection timed out" in client.get(f"{base}/log").json()["text"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/does-not-exist",
        "/api/runs/..%2F..%2Fsrc",
        f"/api/runs/{BASELINE}/tasks/99999",
        f"/api/runs/{BASELINE}/sims/nope/steps/0/call",
        f"/api/runs/{BASELINE}/sims/nope/judge-calls",
        f"/api/runs/{BASELINE}/attempts/8/nope",
        f"/api/runs/{BASELINE}/attempts/..%2F..%2F/nope",
    ],
)
def test_bad_paths_are_404(client, path):
    assert client.get(path).status_code == 404


def test_step_out_of_range_is_404(client):
    task = client.get(f"/api/runs/{BASELINE}/tasks/0").json()
    url = f"/api/runs/{BASELINE}/sims/{task['sim']['id']}/steps/999/call"
    assert client.get(url).status_code == 404


def test_call_file_outside_the_listing_is_404(client):
    task = client.get(f"/api/runs/{BASELINE}/tasks/8").json()
    base = f"/api/runs/{BASELINE}/attempts/8/{task['attempts'][0]['sim_id']}"
    assert client.get(f"{base}/calls/results.json").status_code == 404


def test_read_routes_do_not_accept_writes(client):
    assert client.post("/api/runs", json={}).status_code == 405
    assert client.put(f"/api/runs/{BASELINE}", json={}).status_code == 405


def test_foreign_host_header_is_rejected(store):
    client = TestClient(create_app(store.simulations_dir), base_url="http://evil.test")
    assert client.get("/api/runs").status_code == 400
