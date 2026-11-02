"""The automatic checks, on the real run and on small hand-written conversations."""

import ast
import json

import pytest
from conftest import BASELINE

from viewer.artifacts import find_saved_agent_call
from viewer.checks import (
    DEFINITIONS,
    DUPLICATES,
    IDENTITY_FIRST,
    INVALID,
    ISSUE,
    MALFORMED_CALL,
    NOT_APPLICABLE,
    OK,
    REVIEW,
    SETUP_PROBLEM,
    SKIPPED_IDENTITY,
    TOOL_ERRORS,
    UNCONFIRMED_WRITE,
    WRITE_TOOLS,
    WRITE_WITHOUT_YES,
    check_duplicates,
    check_identity_first,
    check_invalid,
    check_tool_errors,
    check_write_without_yes,
    run_checks,
    suggested_labels,
    tool_calls,
    tool_schemas_from_call,
    totals,
)
from viewer.store import REPO_ROOT

# task: identity state, duplicates, invalid, tool errors, writes needing review
EXPECTED = {
    "0": (ISSUE, 0, 0, 0, 0),
    "1": (ISSUE, 0, 0, 0, 0),
    "2": (OK, 0, 2, 3, 0),
    "3": (OK, 0, 0, 0, 0),
    "4": (OK, 0, 0, 0, 0),
    "6": (OK, 6, 9, 10, 1),
    "7": (OK, 0, 0, 0, 0),
    "8": (NOT_APPLICABLE, 0, 0, 0, 0),
    "10": (OK, 0, 0, 0, 0),
    "11": (OK, 0, 0, 1, 2),
}


@pytest.fixture(scope="module")
def results(store) -> dict:
    data = store.load(BASELINE).data
    call = find_saved_agent_call(store.run_dir(BASELINE))
    schemas = tool_schemas_from_call(call["request"]["tools"])
    return {
        sim["task_id"]: {c["id"]: c for c in run_checks(sim["messages"], schemas)}
        for sim in data["simulations"]
    }


def test_every_task_matches_the_expected_table(results):
    assert set(results) == set(EXPECTED)
    for task_id, expected in EXPECTED.items():
        checks = results[task_id]
        actual = (
            checks[IDENTITY_FIRST]["state"],
            checks[DUPLICATES]["count"],
            checks[INVALID]["count"],
            checks[TOOL_ERRORS]["count"],
            checks[WRITE_WITHOUT_YES]["count"],
        )
        assert actual == expected, task_id


@pytest.mark.parametrize("task_id", ["0", "1"])
def test_identity_was_never_verified_on_tasks_0_and_1(results, task_id):
    check = results[task_id][IDENTITY_FIRST]
    assert check["state"] == ISSUE
    assert check["summary"] == "Identity was never verified."
    assert "get_order_details" in check["evidence"][0]["text"]


def test_identity_passes_after_a_failed_first_try(results):
    """Task 2 calls the lookup with empty fields, then succeeds before account tools."""
    assert results["2"][IDENTITY_FIRST]["state"] == OK


def test_task_6_write_right_after_a_question_needs_review(results):
    check = results["6"][WRITE_WITHOUT_YES]
    assert check["state"] == REVIEW
    assert "exchange_delivered_order_items" in check["evidence"][0]["text"]
    assert "I don't have the item IDs" in check["evidence"][0]["text"]


def test_task_11_fine_is_not_a_confirmation_word(results):
    check = results["11"][WRITE_WITHOUT_YES]
    assert check["state"] == REVIEW
    assert "What do you mean" in check["evidence"][0]["text"]


def test_task_4_two_writes_after_one_yes_are_fine(results):
    assert results["4"][WRITE_WITHOUT_YES]["state"] == OK


def test_tasks_without_writes_are_not_applicable(results):
    assert results["3"][WRITE_WITHOUT_YES]["state"] == NOT_APPLICABLE
    assert results["10"][WRITE_WITHOUT_YES]["state"] == NOT_APPLICABLE


def test_tool_errors_show_their_messages(results):
    evidence = results["11"][TOOL_ERRORS]["evidence"]
    assert len(evidence) == 1
    assert "Payment method should be the original" in evidence[0]["text"]


def test_invalid_names_on_task_6_include_the_leaked_token(results):
    texts = [item["text"] for item in results["6"][INVALID]["evidence"]]
    assert any("<|channel|>commentary" in text for text in texts)
    assert sum("modify_delivered_order_items" in text for text in texts) == 8


def test_empty_tool_name_is_shown_readably(results):
    assert "(empty tool name)" in results["2"][INVALID]["evidence"][0]["text"]


def test_evidence_points_at_real_steps(store, results):
    sims = {s["task_id"]: s for s in store.load(BASELINE).data["simulations"]}
    for item in results["6"][DUPLICATES]["evidence"]:
        message = sims["6"]["messages"][item["step"]]
        assert message["role"] == "assistant"
        assert message["tool_calls"]


def test_write_tool_list_matches_the_decorators_in_tools_py():
    tree = ast.parse(
        (REPO_ROOT / "src/tau2/domains/retail/tools.py").read_text(encoding="utf-8")
    )
    write_tools = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and getattr(decorator.func, "id", "") == "is_tool"
                    and decorator.args[0].attr == "WRITE"
                ):
                    write_tools.add(node.name)
    assert write_tools == set(WRITE_TOOLS)


def call(name, arguments=None, *, error=False, id="1"):
    return [
        {
            "role": "assistant",
            "tool_calls": [{"id": id, "name": name, "arguments": arguments or {}}],
        },
        {"role": "tool", "id": id, "content": "x", "error": error},
    ]


def say(text):
    return [{"role": "user", "content": text}]


def conversation(*parts):
    return tool_calls([message for part in parts for message in part])


def test_identity_after_an_account_tool_is_too_late():
    calls = conversation(
        call("get_user_details", id="1"), call("find_user_id_by_email", id="2")
    )
    check = check_identity_first(calls)
    assert check["state"] == ISSUE
    assert check["summary"] == "Identity was verified too late."
    assert len(check["evidence"]) == 2


def test_a_failed_identity_lookup_does_not_verify():
    calls = conversation(
        call("find_user_id_by_email", error=True, id="1"),
        call("get_order_details", id="2"),
    )
    assert check_identity_first(calls)["state"] == ISSUE


def test_product_lookups_are_not_account_tools():
    calls = conversation(call("get_product_details"))
    assert check_identity_first(calls)["state"] == NOT_APPLICABLE


def test_verified_with_no_account_tool_passes():
    calls = conversation(call("find_user_id_by_email"))
    assert check_identity_first(calls)["state"] == OK


def test_a_write_counts_as_an_account_tool():
    calls = conversation(call("cancel_pending_order"))
    assert check_identity_first(calls)["state"] == ISSUE


def test_no_conversation_is_not_applicable():
    assert check_identity_first([])["state"] == NOT_APPLICABLE


def test_duplicates_ignore_argument_order():
    calls = conversation(
        call("x", {"a": 1, "b": 2}, id="1"), call("x", {"b": 2, "a": 1}, id="2")
    )
    assert check_duplicates(calls)["count"] == 1


def test_different_arguments_are_not_duplicates():
    calls = conversation(call("x", {"a": 1}, id="1"), call("x", {"a": 2}, id="2"))
    assert check_duplicates(calls)["count"] == 0


def test_a_customer_message_between_calls_is_not_a_duplicate():
    calls = conversation(call("x", id="1"), say("hi"), call("x", id="2"))
    assert check_duplicates(calls)["count"] == 0


def test_a_different_call_between_is_not_a_duplicate():
    calls = conversation(call("x", id="1"), call("y", id="2"), call("x", id="3"))
    assert check_duplicates(calls)["count"] == 0


SCHEMAS = {
    "tool": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name"],
    }
}


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({}, "'name' is a required property"),
        ({"name": 5}, "5 is not of type 'string'"),
        ({"name": "a", "ids": "b"}, "'b' is not of type 'array'"),
        ({"name": "a", "ids": [1]}, "1 is not of type 'string'"),
        ({"name": "a", "extra": 1}, "unknown argument extra"),
    ],
)
def test_arguments_that_fail_the_schema(arguments, message):
    check = check_invalid(conversation(call("tool", arguments)), SCHEMAS)
    assert check["state"] == ISSUE
    assert message in check["evidence"][0]["text"]


def test_valid_arguments_pass():
    calls = conversation(call("tool", {"name": "a", "ids": ["b"]}))
    assert check_invalid(calls, SCHEMAS)["state"] == OK


def test_unknown_tool_is_invalid():
    check = check_invalid(conversation(call("other")), SCHEMAS)
    assert "other is not a tool" in check["evidence"][0]["text"]


def test_unknown_tool_list_is_not_applicable():
    assert check_invalid(conversation(call("tool")), None)["state"] == NOT_APPLICABLE


@pytest.mark.parametrize(
    "text",
    [
        "Yes.",
        "yes please",
        "Yeah, go ahead",
        "That's right",
        "thats right",
        "OK",
        "okay do it",
        "Sure",
        "Correct",
        "I confirm",
        "Confirmed.",
        "Please proceed",
        "sounds good",
    ],
)
def test_confirmation_words(text):
    calls = conversation(say(text), call("cancel_pending_order"))
    assert check_write_without_yes(calls)["state"] == OK


@pytest.mark.parametrize(
    "text",
    [
        "Can you look that up?",
        "Fine. Just refund them",
        "I want the blue one",
        "yesterday I ordered this",
    ],
)
def test_no_confirmation_needs_review(text):
    calls = conversation(say(text), call("cancel_pending_order"))
    assert check_write_without_yes(calls)["state"] == REVIEW


def test_write_with_no_customer_message_needs_review():
    assert (
        check_write_without_yes(conversation(call("cancel_pending_order")))["state"]
        == REVIEW
    )


def test_tool_error_messages_are_kept():
    check = check_tool_errors(conversation(call("x", error=True)))
    assert check["count"] == 1
    assert check["state"] == ISSUE


def test_multi_tool_messages_are_paired():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "a", "name": "one", "arguments": {}},
                {"id": "b", "name": "two", "arguments": {}},
            ],
        },
        {
            "role": "tool",
            "tool_messages": [
                {"id": "a", "content": "ok"},
                {"id": "b", "content": "bad", "error": True},
            ],
        },
    ]
    assert [c["failed"] for c in tool_calls(messages)] == [False, True]


def results_with(check_id, state):
    return [{"id": check_id, "state": state, "count": 1}]


def test_suggestions_match_the_requested_labels():
    def suggest(check_id, state):
        return [
            s["label"] for s in suggested_labels(results_with(check_id, state), None)
        ]

    assert suggest(IDENTITY_FIRST, ISSUE) == [SKIPPED_IDENTITY]
    assert suggest(DUPLICATES, ISSUE) == [MALFORMED_CALL]
    assert suggest(INVALID, ISSUE) == [MALFORMED_CALL]
    assert suggest(WRITE_WITHOUT_YES, REVIEW) == [UNCONFIRMED_WRITE]
    assert suggest(TOOL_ERRORS, ISSUE) == []
    assert suggest(IDENTITY_FIRST, OK) == []


def test_two_checks_merge_into_one_suggestion_with_both_reasons():
    checks = results_with(DUPLICATES, ISSUE) + results_with(INVALID, ISSUE)
    suggestions = suggested_labels(checks, None)
    assert len(suggestions) == 1
    assert len(suggestions[0]["because"]) == 2


def test_infrastructure_errors_suggest_setup_problem():
    suggestions = suggested_labels([], "infrastructure_error")
    assert [s["label"] for s in suggestions] == [SETUP_PROBLEM]


def test_every_check_has_a_definition_to_show():
    assert {d["id"] for d in DEFINITIONS} == {
        IDENTITY_FIRST,
        DUPLICATES,
        INVALID,
        TOOL_ERRORS,
        WRITE_WITHOUT_YES,
    }
    assert all(len(d["definition"]) > 40 for d in DEFINITIONS)


def test_run_totals(results):
    rows = [
        {
            "checks": {
                k: {"state": v["state"], "count": v["count"]} for k, v in c.items()
            }
        }
        for c in results.values()
    ]
    by_id = {item["id"]: item for item in totals(rows)}
    assert by_id[IDENTITY_FIRST]["tasks_flagged"] == 2
    assert by_id[DUPLICATES]["total"] == 6
    assert by_id[INVALID]["total"] == 11
    assert by_id[TOOL_ERRORS]["total"] == 14
    assert by_id[WRITE_WITHOUT_YES]["total"] == 3
    assert by_id[WRITE_WITHOUT_YES]["tasks_flagged"] == 2


def test_results_can_be_sent_to_the_browser(results):
    json.dumps(results)
