"""Check the viewer's reading of the real run data/simulations/baseline-practice-10-v2.

The expected values were computed independently from the raw JSON files.
"""

import pytest
from conftest import BASELINE

from viewer import pages
from viewer.artifacts import (
    attempt_dirs,
    find_sim_dir,
    join_steps,
    read_calls,
    rebuild_conversation,
)
from viewer.timeline import AGENT, CUSTOMER, GREETING, build_timeline
from viewer.verdicts import INFRA, NONE_DEFINED, NOT_EVALUATED, PASS, STOPPED

FAIL = "fail"

# task: result, db, nl, termination, messages, turns, tool calls, tool errors
EXPECTED = {
    "0": (PASS, PASS, NONE_DEFINED, "user_stop", 18, 5, 4, 0),
    "1": (FAIL, FAIL, NONE_DEFINED, "user_stop", 18, 5, 4, 0),
    "2": (FAIL, PASS, FAIL, "user_stop", 42, 7, 14, 3),
    "3": (FAIL, FAIL, PASS, "user_stop", 36, 8, 10, 0),
    "4": (FAIL, PASS, FAIL, "user_stop", 36, 7, 11, 0),
    "6": (STOPPED, NOT_EVALUATED, NOT_EVALUATED, "too_many_errors", 48, 8, 16, 10),
    "7": (FAIL, FAIL, NONE_DEFINED, "user_stop", 28, 8, 6, 0),
    "8": (INFRA, NOT_EVALUATED, NOT_EVALUATED, "infrastructure_error", 0, 0, 0, 0),
    "10": (PASS, PASS, NONE_DEFINED, "user_stop", 22, 6, 5, 0),
    "11": (PASS, PASS, NONE_DEFINED, "user_stop", 28, 7, 7, 1),
}


@pytest.fixture(scope="module")
def data(store):
    return store.load(BASELINE).data


@pytest.fixture(scope="module")
def run(store):
    return pages.run_page(store, BASELINE)


def test_counts_and_pass_rate(data, run):
    assert len(data["simulations"]) == 10
    assert len(data["tasks"]) == 10
    counts = run["summary"]["counts"]
    assert counts["passed"] == 3
    assert counts["scored"] == 9
    assert counts["infra"] == 1
    assert counts["rate"] == pytest.approx(3 / 9)
    assert run["summary"]["done_simulations"] == 10
    assert run["summary"]["expected_simulations"] == 10


def test_passed_tasks(run):
    passed = {row["task_id"] for row in run["rows"] if row["result"] == PASS}
    assert passed == {"0", "10", "11"}


def test_rows_match_expected_table_in_numeric_order(run):
    assert [row["task_id"] for row in run["rows"]] == list(EXPECTED)
    for row in run["rows"]:
        actual = (
            row["result"],
            row["db"],
            row["nl"],
            row["termination_reason"],
            row["messages"],
            row["turns"],
            row["tool_calls"],
            row["tool_errors"],
        )
        assert actual == EXPECTED[row["task_id"]], row["task_id"]


def test_run_summary_fields(run):
    summary = run["summary"]
    assert summary["agent"]["implementation"] == "llm_agent"
    assert summary["agent"]["llm"] == "ollama_chat/gpt-oss:20b"
    assert summary["customer"]["llm"] == "ollama_chat/gemma4:12b"
    assert summary["judges"]["nl"] == "ollama_chat/gemma4:12b"
    assert summary["judges"]["communicate_scored"] is False
    assert summary["split"] == "train"


def test_every_model_step_has_its_exact_raw_call(store, data):
    run_dir = store.run_dir(BASELINE)
    agent = customer = greetings = 0
    for sim in data["simulations"]:
        messages = sim["messages"] or []
        sim_dir = find_sim_dir(run_dir, sim["task_id"], sim["id"])
        matches = join_steps(messages, read_calls(sim_dir)) if sim_dir else {}
        for step in build_timeline(messages):
            if step["type"] == GREETING:
                greetings += 1
                assert step["index"] not in matches
            elif step["type"] == AGENT:
                agent += 1
                call = matches[step["index"]]
                assert call["call_name"] == "agent_response"
                assert call["response"]["usage"] == step["usage"]
            elif step["type"] == CUSTOMER:
                customer += 1
                call = matches[step["index"]]
                assert call["call_name"] == "user_simulator_response"
                assert call["response"]["content"] == step["content"]
    assert (agent, customer, greetings) == (129, 61, 9)


def test_every_tool_call_has_its_result(data):
    calls = [
        call
        for sim in data["simulations"]
        for step in build_timeline(sim["messages"] or [])
        for call in step["tool_calls"]
    ]
    assert len(calls) == 77
    assert all(call["result"] is not None for call in calls)
    assert sum(call["result"]["error"] for call in calls) == 14


def test_reasoning_comes_through_from_results_json(data):
    steps = [
        step
        for sim in data["simulations"]
        for step in build_timeline(sim["messages"] or [])
        if step["has_model_call"]
    ]
    assert len(steps) == 190
    assert sum(1 for step in steps if step["reasoning"]) == 178


def test_attempts(store, data, run):
    run_dir = store.run_dir(BASELINE)
    folders = sum(len(attempt_dirs(run_dir, t["id"])) for t in data["tasks"])
    assert folders == 17

    attempts = {row["task_id"]: row["attempts"] for row in run["rows"]}
    assert attempts["2"] == {"total": 3, "failed": 1, "discarded": 0, "no_status": 1}
    assert attempts["6"] == {"total": 3, "failed": 2, "discarded": 0, "no_status": 0}
    assert attempts["8"] == {"total": 4, "failed": 4, "discarded": 0, "no_status": 0}
    assert attempts["0"]["total"] == 1


def test_infra_task_record_has_no_folder_but_attempts_tell_the_story(store):
    page = pages.task_page(store, BASELINE, "8", 0)
    assert page["sim"]["result"] == INFRA
    assert page["sim"]["info"]["failed_after_attempts"] == 4
    assert "600.0 seconds" in page["sim"]["why"]
    assert find_sim_dir(store.run_dir(BASELINE), "8", page["sim"]["id"]) is None
    assert len(page["attempts"]) == 4
    assert all(not attempt["is_current"] for attempt in page["attempts"])
    assert all("timed out" in attempt["error"] for attempt in page["attempts"])


def test_failed_attempt_conversation_is_rebuilt(store):
    attempt = pages.task_page(store, BASELINE, "8", 0)["attempts"][0]
    page = pages.attempt_page(store, BASELINE, "8", attempt["sim_id"])
    steps = page["conversation"]
    assert steps[0]["type"] == GREETING
    assert steps[-1]["type"] == AGENT
    assert steps[-1]["content"].startswith("Got it")
    tool_calls = [call for step in steps for call in step["tool_calls"]]
    assert tool_calls
    assert all(call["result"] is not None for call in tool_calls)
    assert [c["file"] for c in page["calls"]] == sorted(
        c["file"] for c in page["calls"]
    )


def test_rebuild_ends_on_customer_when_they_spoke_last(store):
    sim_dir = next(
        path
        for path in attempt_dirs(store.run_dir(BASELINE), "2")
        if path.name.startswith("sim_46c0bb33")
    )
    calls = read_calls(sim_dir)
    assert calls[-1]["call_name"] == "nl_assertions_eval"
    assert rebuild_conversation(calls)[-1]["type"] == CUSTOMER


def test_judge_calls(store, data):
    by_task = {sim["task_id"]: sim["id"] for sim in data["simulations"]}
    for task_id in ("2", "3", "4"):
        page = pages.judge_calls(store, BASELINE, by_task[task_id])
        assert len(page["calls"]) == 1
        assert page["calls"][0]["call_name"] == "nl_assertions_eval"

    no_assertions = pages.judge_calls(store, BASELINE, by_task["0"])
    assert no_assertions["calls"] == []
    assert "no NL assertions" in no_assertions["missing_reason"]

    stopped = pages.judge_calls(store, BASELINE, by_task["6"])
    assert "stopped before evaluation" in stopped["missing_reason"]


def test_task_panel_uses_the_version_that_ran(store):
    page = pages.task_page(store, BASELINE, "6", 0)
    assert "Name details" in page["task"]["known_info"]
    assert page["task"]["reward_basis"] == ["DB", "NL_ASSERTION"]


def test_action_match_flags(store):
    actions = pages.task_page(store, BASELINE, "0", 0)["task"]["actions"]
    assert [action["matched"] for action in actions] == [False, True, True, True, True]
    stopped = pages.task_page(store, BASELINE, "6", 0)["task"]["actions"]
    assert all(action["matched"] is None for action in stopped)


def test_unscored_communicate_info_is_flagged(store):
    task = pages.task_page(store, BASELINE, "2", 0)["task"]
    assert task["communicate_info"] == ["10"]
    assert task["communicate_scored"] is False


def test_failure_navigation_skips_passes(store):
    page = pages.task_page(store, BASELINE, "8", 0)
    assert page["neighbours"]["previous"]["task_id"] == "7"
    assert page["neighbours"]["next"] is None
    page = pages.task_page(store, BASELINE, "0", 0)
    assert page["neighbours"]["previous"] is None
    assert page["neighbours"]["next"]["task_id"] == "1"


def test_verdict_jump_targets(store):
    task_6 = pages.task_page(store, BASELINE, "6", 0)["sim"]
    assert task_6["first_error_step"] is not None
    assert "too_many_errors" in task_6["why"]
    task_2 = pages.task_page(store, BASELINE, "2", 0)["sim"]
    assert task_2["first_failed_assertion"] == 0
    assert task_2["why"].startswith("NL not met")
