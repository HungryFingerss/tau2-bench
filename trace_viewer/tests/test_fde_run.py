"""Recorded fde_agent runs on disk: the agent's own log lines up with the benchmark's files.

The viewer shows an fde_agent conversation from three sources: results.json, the raw
model calls in llm_debug/, and the agent's fde_agent_log.json. These tests check the
keys that join them, on every fde_agent run on this machine:

  - every store call in results.json (fde-N) is in the log's store_calls, same call
  - every store result in results.json is in the log's store_results, same content
  - one agent_response file per model_call event, matched by generation_time_seconds
  - every model-written message in results.json has a model_call; code-written ones
    have none
  - run_meta.json names the v1-fde-agent design and the fde_agent.run command

They skip when no fde_agent run is on disk (data/simulations is gitignored).
"""

import pytest

from viewer import runmeta
from viewer.artifacts import AGENT_CALL, find_sim_dir, read_calls
from viewer.store import DEFAULT_SIMULATIONS_DIR, read_json_file

LOG_FILE = "fde_agent_log.json"
FDE_START = ["uv", "run", "python", "-m", "fde_agent.run", "run"]


def fde_runs() -> list:
    found = []
    for results in sorted(DEFAULT_SIMULATIONS_DIR.glob("*/results.json")):
        try:
            data = read_json_file(results)  # In-place saves use cp1252.
        except (OSError, ValueError):
            continue  # A live run between saves.
        agent = (data.get("info") or {}).get("agent_info") or {}
        if agent.get("implementation") == "fde_agent":
            found.append(pytest.param(results.parent, data, id=results.parent.name))
    return found or [
        pytest.param(None, None, marks=pytest.mark.skip("no fde_agent run"))
    ]


def simulations(data: dict) -> list[dict]:
    sims = data.get("simulations") or []
    if not sims:
        pytest.skip("results.json has no finished conversation yet")
    return sims


def logged_attempts(run_dir) -> list:
    """Every attempt folder with an agent log, finished or not."""
    return sorted(p.parent for p in run_dir.glob(f"artifacts/task_*/sim_*/{LOG_FILE}"))


def log_of(run_dir, sim) -> tuple[dict, list[dict]]:
    sim_dir = find_sim_dir(run_dir, str(sim["task_id"]), sim["id"])
    assert sim_dir is not None, f"no artifacts folder for task {sim['task_id']}"
    log = read_json_file(sim_dir / LOG_FILE)
    assert log["agent"] == "fde_agent"
    return log, read_calls(sim_dir)


def events(log: dict, kind: str) -> list[dict]:
    return [e for e in log["events"] if e["kind"] == kind]


@pytest.mark.parametrize("run_dir, data", fde_runs())
def test_the_run_was_recorded_with_the_v1_design(run_dir, data):
    session = runmeta.latest_session(runmeta.read_meta(run_dir))
    assert session is not None, "not started with record_run"
    assert session["design_version"] == "v1-fde-agent"
    assert session["command"][:6] == FDE_START
    assert "--agent" in session["command"]


@pytest.mark.parametrize("run_dir, data", fde_runs())
def test_every_store_call_is_in_the_log(run_dir, data):
    for sim in simulations(data):
        log, _ = log_of(run_dir, sim)
        logged = {}
        for event in events(log, "store_calls"):
            logged.update(event["calls"])
        made = {
            call["id"]: call
            for message in sim["messages"]
            if message["role"] == "assistant"
            for call in message.get("tool_calls") or []
        }
        assert made, "no store calls at all"
        assert all(call_id.startswith("fde-") for call_id in made)
        assert set(made) == set(logged)
        for call_id, call in made.items():
            assert logged[call_id] == {"name": call["name"], "args": call["arguments"]}


@pytest.mark.parametrize("run_dir, data", fde_runs())
def test_every_store_result_is_in_the_log(run_dir, data):
    for sim in simulations(data):
        log, _ = log_of(run_dir, sim)
        logged = {}
        for event in events(log, "store_results"):
            logged.update(event["results"])
        for message in sim["messages"]:
            if message["role"] == "tool":
                assert logged[message["id"]]["content"] == message["content"]
                assert logged[message["id"]]["error"] == message["error"]


@pytest.mark.parametrize("run_dir, data", fde_runs())
def test_one_raw_call_file_per_model_call(run_dir, data):
    for sim in simulations(data):
        log, calls = log_of(run_dir, sim)
        files = [c for c in calls if c.get("call_name") == AGENT_CALL]
        times = [e["generation_time_seconds"] for e in events(log, "model_call")]
        assert len(files) == len(times)
        file_times = [c["response"]["generation_time_seconds"] for c in files]
        assert sorted(file_times) == sorted(times)
        assert len(set(times)) == len(times), (
            "two calls share a time: the join is ambiguous"
        )


@pytest.mark.parametrize("run_dir, data", fde_runs())
def test_every_logged_attempt_has_one_raw_file_per_model_call(run_dir, data):
    """From the artifacts alone, so it holds for attempts results.json lacks."""
    attempts = logged_attempts(run_dir)
    assert attempts, "no attempt has an agent log"
    for sim_dir in attempts:
        log = read_json_file(sim_dir / LOG_FILE)
        files = [c for c in read_calls(sim_dir) if c.get("call_name") == AGENT_CALL]
        times = [e["generation_time_seconds"] for e in events(log, "model_call")]
        file_times = [c["response"]["generation_time_seconds"] for c in files]
        assert sorted(file_times) == sorted(times), sim_dir.name


@pytest.mark.parametrize("run_dir, data", fde_runs())
def test_logged_store_call_ids_count_up_from_one(run_dir, data):
    for sim_dir in logged_attempts(run_dir):
        log = read_json_file(sim_dir / LOG_FILE)
        ids = [i for e in events(log, "store_calls") for i in e["calls"]]
        assert ids == [f"fde-{n}" for n in range(1, len(ids) + 1)], sim_dir.name
        answered = [i for e in events(log, "store_results") for i in e["results"]]
        assert answered == ids[: len(answered)]


@pytest.mark.parametrize("run_dir, data", fde_runs())
def test_model_written_messages_have_a_call_and_code_written_ones_none(run_dir, data):
    for sim in simulations(data):
        log, _ = log_of(run_dir, sim)
        times = {e["generation_time_seconds"] for e in events(log, "model_call")}
        said = [
            m for m in sim["messages"] if m["role"] == "assistant" and m.get("content")
        ]
        written = {"model": [], "code": [], "benchmark": []}
        for event in events(log, "agent"):
            written[event["by"]].append(event["text"])
        for message in said:
            seconds = message.get("generation_time_seconds")
            if seconds is None:
                assert message["content"] in written["code"] + written["benchmark"]
            else:
                assert seconds in times
                assert message["content"] in written["model"]
        assert len(said) == sum(len(texts) for texts in written.values())
