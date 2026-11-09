"""An agent's own log on the conversation page: viewer/agent_log.py and its routes.

Unit tests use small hand-written logs. The rest use the real smoke run
v1-smoke-task0, whose attempt sim_82738a0e stopped after code sent the change
summary (58 events), and skip when it is not on this machine. For a conversation that
results.json does hold, a copy of that attempt gets a results.json simulation built
from its log, so message links, checks and the database replay are tested on real data.
"""

import copy
import json
import os
import shutil
import time
from pathlib import Path

import pytest
from conftest import BASELINE
from fastapi.testclient import TestClient

from viewer import agent_log, runmeta
from viewer.api import create_app
from viewer.artifacts import AGENT_LOG_FILE, AGENT_LOG_TEMP_PREFIX, read_calls
from viewer.changes import Changes
from viewer.replay import benchmark_python
from viewer.store import DEFAULT_SIMULATIONS_DIR, Store, read_json_file

SMOKE = "v1-smoke-task0"
SMOKE_SIM = "82738a0e-44e0-4b88-bbca-f80e4d10b7e4"
SMOKE_DIR = (
    DEFAULT_SIMULATIONS_DIR / SMOKE / "artifacts" / "task_0" / f"sim_{SMOKE_SIM}"
)

needs_smoke = pytest.mark.skipif(
    not (SMOKE_DIR / AGENT_LOG_FILE).is_file(), reason=f"{SMOKE} is not on this machine"
)


# ---------- unit: build() on small logs ----------


def ev(n: int, kind: str, **fields) -> dict:
    return {"n": n, "time": 1_700_000_000 + n, "kind": kind, **fields}


def agent_call(seconds: float, file: str) -> dict:
    return {
        "file": file,
        "call_name": "agent_response",
        "timestamp": "2026-11-06T10:00:00",
        "request": {"model": "m"},
        "response": {"generation_time_seconds": seconds, "usage": {}},
    }


def customer_call(text: str, file: str) -> dict:
    return {
        "file": file,
        "call_name": "user_simulator_response",
        "response": {"content": text.split("\n")},  # llm_debug splits lines.
    }


SUMMARY = "Cancel order #W1.\nShall I go ahead?"
LOG = {
    "agent": "fde_agent",
    "model": "m",
    "events": [
        ev(1, "agent", text="Hi!", by="benchmark"),
        ev(2, "customer", text="Cancel #W1.\nPlease."),
        ev(3, "model_call", generation_time_seconds=1.5, usage={"prompt_tokens": 9}),
        ev(4, "fixed", what="tool name 'x<|channel|>' -> 'x'"),
        ev(5, "invalid_call", error="Error: no tool called 'nope'."),
        ev(6, "note", text="Error: no tool called 'nope'."),
        ev(7, "model_call", generation_time_seconds=2.25),
        ev(
            8,
            "tool_call",
            id="tc-1",
            tool="prepare_change",
            args={"kind": "cancel_order"},
        ),
        ev(
            9,
            "ignored_inputs",
            tool="prepare_change",
            change_kind="cancel_order",
            inputs=["address"],
        ),
        ev(
            10,
            "store_calls",
            tool="prepare_change",
            calls={
                "fde-1": {"name": "get_user_details", "args": {"user_id": "u"}},
                "fde-2": {"name": "get_order_details", "args": {"order_id": "#W1"}},
            },
        ),
        ev(
            11,
            "store_results",
            tool="prepare_change",
            results={
                "fde-1": {"content": "{}", "error": False},
                "fde-2": {"content": "Error: no order", "error": True},
            },
        ),
        ev(
            12,
            "tool_result",
            id="tc-1",
            tool="prepare_change",
            ok=True,
            text="Change #1",
        ),
        ev(13, "agent", text=SUMMARY, by="code"),
        ev(14, "customer", text="Yes."),
        ev(15, "model_call", generation_time_seconds=3.0),  # No raw file saved.
        ev(16, "blocked_reply", ids=["item_1"], text="Your item_1"),
        ev(17, "model_call", generation_time_seconds=4.0),
        ev(18, "agent", text="Done!", by="model"),
        ev(19, "empty_reply"),
        ev(20, "safe_message", reason="5 failing tool calls in a row"),
        ev(21, "agent", text="Sorry, could you tell me again?", by="code"),
        ev(22, "tool_crash", tool="get_order", traceback="Traceback ..."),
        ev(23, "something_new", detail=1),
    ],
}
CALLS = [
    customer_call("Cancel #W1.\nPlease.", "c1.json"),
    agent_call(1.5, "a1.json"),
    agent_call(2.25, "a2.json"),
    agent_call(2.2500001, "near.json"),  # Close is not equal: never matched.
    customer_call("Yes.", "c2.json"),
    agent_call(4.0, "a4.json"),
    {"file": "j.json", "call_name": "nl_assertions_eval", "response": {}},
]


@pytest.fixture(scope="module")
def built() -> dict:
    return agent_log.build(copy.deepcopy(LOG), copy.deepcopy(CALLS))


def step(built: dict, event: int) -> dict:
    return next(s for s in built["steps"] if s["event"] == event)


def test_every_event_is_a_step_except_store_results(built):
    assert [s["event"] for s in built["steps"]] == [n for n in range(1, 24) if n != 11]


def test_who_wrote_each_message(built):
    assert step(built, 1)["type"] == "greeting"
    assert step(built, 1)["written_by"] == "benchmark"
    summary = step(built, 13)
    assert (summary["type"], summary["written_by"]) == ("agent", "code")
    assert "call_file" not in summary
    assert "prepare_change (tc-1)" in summary["code_source"]
    safe = step(built, 21)
    assert safe["written_by"] == "code" and "safe message" in safe["code_source"]
    model = step(built, 18)
    assert model["written_by"] == "model"
    assert model["call_file"] == "a4.json"
    assert model["model_call_step"] == "e17"


def test_model_calls_match_raw_files_by_exact_time_only(built):
    assert step(built, 3)["call_file"] == "a1.json"
    assert step(built, 7)["call_file"] == "a2.json"
    assert step(built, 15)["call_file"] is None
    assert built["missing_calls"] == [{"step": "e15", "generation_time_seconds": 3.0}]
    near = next(c for c in built["calls"] if c["file"] == "near.json")
    assert near["step"] is None


def test_customer_messages_match_their_calls_by_text(built):
    assert step(built, 2)["call_file"] == "c1.json"
    assert step(built, 14)["call_file"] == "c2.json"


def test_each_model_call_says_what_it_led_to(built):
    assert step(built, 3)["outcome"] == "rejected before anything ran"
    assert step(built, 3)["outcome_step"] == "e5"  # Not the "fixed" before it.
    assert step(built, 7)["outcome"] == "called prepare_change (tc-1)"
    assert step(built, 7)["outcome_step"] == "e8"
    assert step(built, 15)["outcome"] == "reply blocked: it showed internal IDs"
    assert step(built, 17)["outcome"] == "wrote a message to the customer"


def test_every_check_kind_is_its_own_highlighted_step(built):
    checks = {s["check"]: s for s in built["steps"] if s["type"] == "check"}
    assert set(checks) == set(agent_log.CHECK_KINDS)
    assert checks["invalid_call"]["details"] == {
        "error": "Error: no tool called 'nope'."
    }
    assert checks["ignored_inputs"]["details"]["inputs"] == ["address"]
    assert checks["blocked_reply"]["details"]["ids"] == ["item_1"]
    assert checks["tool_crash"]["details"]["traceback"] == "Traceback ..."
    assert checks["invalid_call"]["model_call_step"] == "e3"


def test_our_tool_call_and_result_are_shown_as_the_model_saw_them(built):
    call, result = step(built, 8), step(built, 12)
    assert (call["kind"], call["id"], call["tool"]) == (
        "tool_call",
        "tc-1",
        "prepare_change",
    )
    assert call["args"] == {"kind": "cancel_order"}
    assert (result["kind"], result["ok"], result["text"]) == (
        "tool_result",
        True,
        "Change #1",
    )
    note = step(built, 6)
    assert (note["type"], note["kind"]) == ("inner", "note")
    assert note["text"] == "Error: no tool called 'nope'."


def test_store_calls_carry_their_fde_ids_results_and_tool(built):
    store = step(built, 10)
    assert store["type"] == "agent" and store["written_by"] == "code"
    assert store["via"] == {"id": "tc-1", "tool": "prepare_change"}
    assert [c["id"] for c in store["tool_calls"]] == ["fde-1", "fde-2"]
    assert store["tool_calls"][1]["result"]["error"] is True
    assert store["tool_calls"][0]["result"]["content"] == "{}"


def test_unknown_event_kinds_are_kept(built):
    assert step(built, 23)["kind"] == "other"
    assert step(built, 23)["details"] == {"kind": "something_new", "detail": 1}


def test_counts(built):
    counts = built["counts"]
    assert counts["model_calls"] == 4
    assert counts["tool_calls"] == 1
    assert counts["store_calls"] == 2
    assert counts["messages_by"] == {"model": 1, "code": 2, "benchmark": 1}
    assert all(counts["checks"][kind] == 1 for kind in agent_log.CHECK_KINDS)


def test_the_call_list_has_every_file_with_its_role(built):
    roles = {c["file"]: c["role"] for c in built["calls"]}
    assert roles == {
        "c1.json": "customer",
        "a1.json": "agent",
        "a2.json": "agent",
        "near.json": "agent",
        "c2.json": "customer",
        "a4.json": "agent",
        "j.json": "judge",
    }


def test_without_results_steps_are_indexed_by_event():
    built = agent_log.build(LOG, CALLS)
    assert all(s["index"] == f"e{s['event']}" for s in built["steps"])


def test_with_results_benchmark_steps_take_their_message_index():
    messages = [
        {"role": "assistant", "content": "Hi!"},
        {"role": "user", "content": "Cancel #W1.\nPlease."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "fde-1"}, {"id": "fde-2"}],
        },
        {"role": "tool", "id": "fde-1", "content": "{}"},
        {"role": "tool", "id": "fde-2", "content": "Error: no order"},
        {"role": "assistant", "content": SUMMARY},
        {"role": "user", "content": "Yes."},
        {"role": "assistant", "content": "Done!"},
        {"role": "assistant", "content": "Sorry, could you tell me again?"},
    ]
    built = agent_log.build(LOG, CALLS, messages)
    indexes = {s["event"]: s["index"] for s in built["steps"]}
    assert [indexes[n] for n in (1, 2, 10, 13, 14, 18, 21)] == [0, 1, 2, 5, 6, 7, 8]
    assert indexes[3] == "e3"  # Inner steps keep their event index.
    store = next(s for s in built["steps"] if s["event"] == 10)
    assert [c["result"]["message_index"] for c in store["tool_calls"]] == [3, 4]


def test_a_text_mismatch_is_not_linked():
    messages = [{"role": "assistant", "content": "Hello!"}]
    built = agent_log.build(LOG, CALLS, messages)
    assert built["steps"][0]["index"] == "e1"


def test_writer_of():
    assert agent_log.writer_of(LOG, SUMMARY) == "code"
    assert agent_log.writer_of(LOG, "Done!") == "model"
    assert agent_log.writer_of(LOG, "never said") is None


# ---------- reading a log that a live agent rewrites ----------


def write_log(folder: Path, data: dict, age_seconds: float = 10) -> Path:
    path = folder / AGENT_LOG_FILE
    path.write_text(json.dumps(data), encoding="utf-8")
    moment = time.time() - age_seconds
    os.utime(path, (moment, moment))
    return path


def counting_reads(monkeypatch) -> list:
    from viewer import store as store_module

    opened = []
    real = store_module.read_json_file

    def spy(path):
        opened.append(path)
        return real(path)

    monkeypatch.setattr(store_module, "read_json_file", spy)
    return opened


def test_settled_log_is_read_once_then_served_from_cache(tmp_path, monkeypatch):
    opened = counting_reads(monkeypatch)
    path = write_log(tmp_path, {"events": [1]})
    store = Store(tmp_path)
    first = store.read_settled(path, AGENT_LOG_TEMP_PREFIX)
    second = store.read_settled(path, AGENT_LOG_TEMP_PREFIX)
    assert first.data == second.data == {"events": [1]}
    assert not first.stale and first.problem is None
    assert len(opened) == 1


def test_a_log_just_rewritten_is_not_opened_and_the_old_copy_is_served(
    tmp_path, monkeypatch
):
    path = write_log(tmp_path, {"events": [1]})
    store = Store(tmp_path, settle_seconds=60)
    os.utime(path, (time.time() - 120, time.time() - 120))
    assert store.read_settled(path, AGENT_LOG_TEMP_PREFIX).data == {"events": [1]}
    opened = counting_reads(monkeypatch)
    write_log(tmp_path, {"events": [1, 2]}, age_seconds=0)
    read = store.read_settled(path, AGENT_LOG_TEMP_PREFIX)
    assert read.data == {"events": [1]} and read.stale
    assert opened == []


def test_a_log_is_not_opened_while_the_agent_has_a_temp_file(tmp_path, monkeypatch):
    path = write_log(tmp_path, {"events": [1]})
    store = Store(tmp_path, settle_seconds=0)
    assert store.read_settled(path, AGENT_LOG_TEMP_PREFIX).data == {"events": [1]}
    (tmp_path / f"{AGENT_LOG_TEMP_PREFIX}abc.tmp").write_text("{", encoding="utf-8")
    write_log(tmp_path, {"events": [1, 2]})
    opened = counting_reads(monkeypatch)
    read = store.read_settled(path, AGENT_LOG_TEMP_PREFIX)
    assert read.stale and read.data == {"events": [1]}
    assert opened == []


def test_a_broken_log_keeps_the_last_good_copy(tmp_path):
    path = write_log(tmp_path, {"events": [1]})
    store = Store(tmp_path, settle_seconds=0)
    store.read_settled(path, AGENT_LOG_TEMP_PREFIX)
    path.write_text("{not json", encoding="utf-8")
    read = store.read_settled(path, AGENT_LOG_TEMP_PREFIX)
    assert read.data == {"events": [1]}
    assert "could not be read" in read.problem


def test_a_missing_log(tmp_path):
    read = Store(tmp_path).read_settled(
        tmp_path / AGENT_LOG_FILE, AGENT_LOG_TEMP_PREFIX
    )
    assert read.data is None and "does not exist" in read.problem


# ---------- the real smoke run: the page for a stopped attempt ----------


@pytest.fixture(scope="module")
def smoke_client():
    if not (SMOKE_DIR / AGENT_LOG_FILE).is_file():
        pytest.skip(f"{SMOKE} is not on this machine")
    return TestClient(create_app(DEFAULT_SIMULATIONS_DIR))


@pytest.fixture(scope="module")
def smoke_page(smoke_client) -> dict:
    return smoke_client.get(f"/api/runs/{SMOKE}/tasks/0").json()


@needs_smoke
def test_the_page_shows_the_newest_attempt_with_a_log(smoke_page):
    conversation = smoke_page["conversation"]
    assert conversation["available"] is True
    assert conversation["sim_id"] == SMOKE_SIM
    assert conversation["in_results"] is False
    assert conversation["status"] == "failed"
    assert conversation["reason"] == "infrastructure_error"
    assert conversation["counts"]["events"] == 58
    assert smoke_page["sim"] is None  # results.json has no simulation yet.


@needs_smoke
def test_the_summary_is_shown_as_written_by_code_with_no_model_call(smoke_page):
    steps = smoke_page["conversation"]["steps"]
    summary = next(s for s in steps if s.get("written_by") == "code" and s["content"])
    assert summary["content"].startswith("Exchange in order #W2378156:")
    assert "call_file" not in summary
    assert "prepare_change (tc-7)" in summary["code_source"]
    assert "not saved" not in summary["code_source"]
    last = steps[-1]
    assert last["kind"] == "note" and "word for word" in last["text"]


@needs_smoke
def test_every_model_call_opens_its_own_raw_file(smoke_client, smoke_page):
    conversation = smoke_page["conversation"]
    model_calls = [s for s in conversation["steps"] if s.get("kind") == "model_call"]
    assert len(model_calls) == 10
    for model_call in model_calls:
        url = (
            f"/api/runs/{SMOKE}/attempts/0/{SMOKE_SIM}/calls/{model_call['call_file']}"
        )
        call = smoke_client.get(url).json()
        assert call["call_name"] == "agent_response"
        seconds = call["response"]["generation_time_seconds"]
        assert seconds == model_call["generation_time_seconds"]


@needs_smoke
def test_the_model_calls_list_has_every_saved_call(smoke_page):
    calls = smoke_page["conversation"]["calls"]
    assert len(calls) == len(read_calls(SMOKE_DIR)) == 13
    agent = [c for c in calls if c["role"] == "agent"]
    assert len(agent) == 10 and all(c["outcome"] and c["step"] for c in agent)
    assert [c["role"] for c in calls].count("customer") == 3
    assert smoke_page["conversation"]["missing_calls"] == []


@needs_smoke
def test_the_rejected_call_is_a_highlighted_check_where_it_happened(smoke_page):
    steps = smoke_page["conversation"]["steps"]
    position = next(i for i, s in enumerate(steps) if s["type"] == "check")
    check = steps[position]
    assert check["check"] == "invalid_call"
    assert "no name" in check["details"]["error"]
    assert steps[position - 1]["kind"] == "model_call"
    assert steps[position - 1]["outcome"] == "rejected before anything ran"
    assert steps[position + 1]["kind"] == "note"


@needs_smoke
def test_our_tool_results_are_exactly_what_the_model_saw(smoke_page):
    log = read_json_file(SMOKE_DIR / AGENT_LOG_FILE)
    logged = {e["id"]: e["text"] for e in log["events"] if e["kind"] == "tool_result"}
    steps = smoke_page["conversation"]["steps"]
    shown = {s["id"]: s["text"] for s in steps if s.get("kind") == "tool_result"}
    assert shown == logged and len(shown) == 7


@needs_smoke
def test_an_attempt_without_a_log_can_be_asked_for(smoke_client):
    attempts = smoke_client.get(f"/api/runs/{SMOKE}/tasks/0").json()["attempts"]
    other = next(a for a in attempts if not a["has_agent_log"])
    page = smoke_client.get(
        f"/api/runs/{SMOKE}/tasks/0", params={"attempt": other["sim_id"]}
    ).json()
    assert page["conversation"]["available"] is False
    missing = smoke_client.get(f"/api/runs/{SMOKE}/tasks/0", params={"attempt": "nope"})
    assert missing.status_code == 404


@needs_smoke
def test_the_attempt_drawer_uses_the_log(smoke_client):
    data = smoke_client.get(f"/api/runs/{SMOKE}/attempts/0/{SMOKE_SIM}").json()
    assert data["agent_log"]["available"] is True
    # 58 events, less the 12 store_results that attach to their store calls.
    assert len(data["agent_log"]["steps"]) == 46


def test_baseline_pages_have_no_agent_log(client):
    page = client.get(f"/api/runs/{BASELINE}/tasks/0").json()
    assert page["conversation"] is None
    assert page["timeline"]


# ---------- a conversation that results.json holds ----------


def sim_from_log(log: dict, template: dict, sim_id: str) -> dict:
    """A results.json simulation with exactly the messages the agent's log records.

    generation_time_seconds is set as fde_agent sets it: on a message the model
    wrote, and on the first store calls of a tool, from the model call before them.
    """
    messages = []
    deciding = None
    for event in log["events"]:
        if event["kind"] == "model_call":
            deciding = event["generation_time_seconds"]
        elif event["kind"] == "agent":
            seconds = deciding if event["by"] == "model" else None
            messages.append(
                {
                    "role": "assistant",
                    "content": event["text"],
                    "generation_time_seconds": seconds,
                }
            )
            deciding = None
        elif event["kind"] == "customer":
            messages.append({"role": "user", "content": event["text"]})
        elif event["kind"] == "store_calls":
            seconds, deciding = deciding, None
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "generation_time_seconds": seconds,
                    "tool_calls": [
                        {
                            "id": i,
                            "name": c["name"],
                            "arguments": c["args"],
                            "requestor": "assistant",
                        }
                        for i, c in event["calls"].items()
                    ],
                }
            )
        elif event["kind"] == "store_results":
            for i, result in event["results"].items():
                messages.append(
                    {
                        "id": i,
                        "role": "tool",
                        "content": result["content"],
                        "requestor": "assistant",
                        "error": result["error"],
                    }
                )
    sim = {
        key: template[key]
        for key in ("task_id", "trial", "seed", "start_time", "end_time", "duration")
    }
    return {
        **sim,
        "id": sim_id,
        "timestamp": template.get("timestamp"),
        "termination_reason": "user_stop",
        "messages": messages,
        "reward_info": None,
    }


@pytest.fixture(scope="module")
def finished_run(tmp_path_factory):
    """A copy of the smoke attempt that results.json holds."""
    if not (SMOKE_DIR / AGENT_LOG_FILE).is_file():
        pytest.skip(f"{SMOKE} is not on this machine")
    baseline = DEFAULT_SIMULATIONS_DIR / BASELINE / "results.json"
    if not baseline.is_file():
        pytest.skip(f"{BASELINE} is not on this machine")
    root = tmp_path_factory.mktemp("sims")
    run = root / "finished"
    target = run / "artifacts" / "task_0" / f"sim_{SMOKE_SIM}"
    shutil.copytree(SMOKE_DIR, target)
    (target / "sim_status.json").write_text(
        json.dumps({"status": "used"}), encoding="utf-8"
    )
    data = read_json_file(DEFAULT_SIMULATIONS_DIR / SMOKE / "results.json")
    template = next(
        s for s in read_json_file(baseline)["simulations"] if s["task_id"] == "0"
    )
    log = read_json_file(SMOKE_DIR / AGENT_LOG_FILE)
    data["simulations"] = [sim_from_log(log, template, SMOKE_SIM)]
    (run / "results.json").write_text(json.dumps(data), encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def finished_client(finished_run):
    return TestClient(create_app(finished_run))


def test_a_finished_conversation_links_every_message(finished_client):
    page = finished_client.get("/api/runs/finished/tasks/0").json()
    conversation = page["conversation"]
    assert conversation["in_results"] is True
    messages = page["timeline"]
    benchmark = [
        s
        for s in conversation["steps"]
        if s["type"] in ("agent", "customer", "greeting")
    ]
    assert all(isinstance(s["index"], int) for s in benchmark)
    assert len({s["index"] for s in benchmark}) == len(benchmark)
    assert {s["index"] for s in messages} == {s["index"] for s in benchmark}


def test_a_code_written_message_never_says_its_call_was_not_saved(finished_client):
    page = finished_client.get("/api/runs/finished/tasks/0").json()
    sim_id = page["sim"]["id"]
    said = store = 0
    for step in page["conversation"]["steps"]:
        if step.get("written_by") != "code":
            continue
        url = f"/api/runs/finished/sims/{sim_id}/steps/{step['index']}/call"
        data = finished_client.get(url).json()
        if step["content"]:  # The change summary: code wrote it.
            assert data["call"] is None
            assert data["missing_reason"].startswith("Written by the agent's code")
            said += 1
        elif data["call"] is not None:
            # A tool's first store calls carry the time of the model call that
            # picked the tool, so they open that call.
            assert data["call"]["call_name"] == "agent_response"
            store += 1
        else:
            assert "agent's own tools" in data["missing_reason"]
            store += 1
    assert said == 1 and store == 12


def test_model_written_messages_open_the_same_call_through_the_old_route(
    finished_client,
):
    page = finished_client.get("/api/runs/finished/tasks/0").json()
    sim_id = page["sim"]["id"]
    written = [
        s for s in page["conversation"]["steps"] if s.get("written_by") == "model"
    ]
    assert len(written) == 2
    for step in written:
        data = finished_client.get(
            f"/api/runs/finished/sims/{sim_id}/steps/{step['index']}/call"
        ).json()
        assert data["call"]["file"] == step["call_file"]


def test_a_model_message_with_no_matching_call_is_not_called_scripted(tmp_path):
    from viewer.pages import _written_without_a_model

    sim = tmp_path / "sim_x"
    sim.mkdir()
    write_log(sim, {"events": [ev(1, "agent", text="Hello", by="model")]})
    reason = _written_without_a_model(
        Store(tmp_path), sim, {"role": "assistant", "content": "Hello"}
    )
    assert "Written by the model" in reason and "scripted" not in reason


def test_checks_use_the_store_tools_not_the_agents_own(finished_client):
    page = finished_client.get("/api/runs/finished/tasks/0").json()
    checks = {c["id"]: c for c in page["checks"]}
    assert checks["invalid"]["state"] == "ok", checks["invalid"]["evidence"]
    assert checks["identity_first"]["state"] == "ok"


@pytest.mark.skipif(
    benchmark_python() is None, reason="the benchmark's .venv is missing"
)
def test_database_replay_works_for_fde_store_calls(finished_client):
    """Task 0 expects an exchange. This attempt stopped before confirm_change, so the
    agent's database equals the initial one and differs from the expected one."""
    page = finished_client.get("/api/runs/finished/tasks/0").json()
    diff = finished_client.get(
        f"/api/runs/finished/sims/{page['sim']['id']}/db-diff"
    ).json()
    assert diff["available"] is True, diff
    assert diff["db_match"] is False
    assert diff["agent_hash"] == diff["initial_hash"] != diff["expected_hash"]
    assert any(r["collection"] == "orders" for r in diff["records"])


# ---------- live: steps appear as the log is rewritten ----------


def live_run(root: Path) -> Path:
    run = root / "live"
    sim = run / "artifacts" / "task_0" / "sim_live"
    (sim / "llm_debug").mkdir(parents=True)
    data = read_json_file(DEFAULT_SIMULATIONS_DIR / SMOKE / "results.json")
    data["simulations"] = []
    (run / "results.json").write_text(json.dumps(data), encoding="utf-8")
    meta = {
        "run": "live",
        "sessions": [{"design_version": "v1-fde-agent", "finished_at": None}],
    }
    (run / runmeta.META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    (run / runmeta.HEARTBEAT_FILE).touch()
    return sim


def swap_in(sim: Path, events: list) -> None:
    """What fde_agent's write_log does: a temp file, then os.replace."""
    temp = sim / f"{AGENT_LOG_TEMP_PREFIX}x.tmp"
    temp.write_text(
        json.dumps({"agent": "fde_agent", "events": events}), encoding="utf-8"
    )
    os.replace(temp, sim / AGENT_LOG_FILE)
    moment = time.time() - 5  # As if it had settled.
    os.utime(sim / AGENT_LOG_FILE, (moment, moment))


@needs_smoke
def test_a_live_conversation_grows_as_the_log_is_rewritten(tmp_path):
    sim = live_run(tmp_path)
    log = read_json_file(SMOKE_DIR / AGENT_LOG_FILE)["events"]
    client = TestClient(create_app(tmp_path))
    changes = Changes(Store(tmp_path))

    swap_in(sim, log[:2])
    first = client.get("/api/runs/live/tasks/0").json()["conversation"]
    token = changes.snapshot("live")["runs"]["live"]["token"]
    assert first["live"] is True
    assert [s["type"] for s in first["steps"]] == ["greeting", "customer"]

    swap_in(sim, log[:7])
    assert changes.snapshot("live")["runs"]["live"]["token"] != token
    second = client.get("/api/runs/live/tasks/0").json()["conversation"]
    assert len(second["steps"]) == 7
    assert second["steps"][-1]["written_by"] == "model"


@needs_smoke
def test_a_running_attempt_with_no_log_yet_is_live_so_its_first_turn_appears(
    tmp_path,
):
    """Opened before the agent's first turn ends: nothing to read, so redraw."""
    sim = live_run(tmp_path)
    client = TestClient(create_app(tmp_path))
    page = client.get("/api/runs/live/tasks/0").json()
    assert page["conversation"] is None
    assert page["live"] is True
    swap_in(sim, read_json_file(SMOKE_DIR / AGENT_LOG_FILE)["events"][:2])
    assert client.get("/api/runs/live/tasks/0").json()["conversation"]["live"] is True


@needs_smoke
def test_a_stopped_run_is_not_live(tmp_path):
    sim = live_run(tmp_path)
    swap_in(sim, read_json_file(SMOKE_DIR / AGENT_LOG_FILE)["events"][:2])
    beat = tmp_path / "live" / runmeta.HEARTBEAT_FILE
    old = time.time() - runmeta.HEARTBEAT_STALE_SECONDS - 60
    os.utime(beat, (old, old))
    page = TestClient(create_app(tmp_path)).get("/api/runs/live/tasks/0").json()
    assert page["live"] is False and page["conversation"]["live"] is False


def test_baseline_task_pages_are_never_live(client):
    assert client.get(f"/api/runs/{BASELINE}/tasks/0").json()["live"] is False


def test_the_change_token_moves_when_a_log_settles(tmp_path, monkeypatch):
    """The page must hear about a log that was too fresh to read on its last visit."""
    run = tmp_path / "r"
    sim = run / "artifacts" / "task_0" / "sim_a"
    sim.mkdir(parents=True)
    (run / "results.json").write_text("{}", encoding="utf-8")
    (sim / AGENT_LOG_FILE).write_text("{}", encoding="utf-8")
    changes = Changes(Store(tmp_path))
    fresh = changes.snapshot("r")["runs"]["r"]["token"]
    later = time.time() + 30  # The file itself is untouched.
    monkeypatch.setattr(runmeta.time, "time", lambda: later)
    assert changes.snapshot("r")["runs"]["r"]["token"] != fresh
