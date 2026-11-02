"""Automatic checks on what the agent did with its tools.

Each check is a plain rule over the saved messages, and its definition is shown in
the UI next to its result. Checks only suggest a failure label. Confirming one is
always a person's decision.
"""

import json
import re
from typing import Any

from jsonschema import Draft202012Validator

from viewer.verdicts import INFRASTRUCTURE_ERROR, tool_results

IDENTITY_TOOLS = frozenset({"find_user_id_by_email", "find_user_id_by_name_zip"})

# Tools decorated with @is_tool(ToolType.WRITE) in src/tau2/domains/retail/tools.py.
# tests/test_checks.py fails if this list drifts from that file.
WRITE_TOOLS = frozenset(
    {
        "cancel_pending_order",
        "exchange_delivered_order_items",
        "modify_pending_order_address",
        "modify_pending_order_items",
        "modify_pending_order_payment",
        "modify_user_address",
        "return_delivered_order_items",
    }
)
ACCOUNT_TOOLS = WRITE_TOOLS | {"get_user_details", "get_order_details"}

CONFIRMATION = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|correct|confirm(?:ed)?|go ahead|proceed"
    r"|please do|that'?s right|sounds good|absolutely|definitely)\b",
    re.IGNORECASE,
)

OK, ISSUE, REVIEW, NOT_APPLICABLE = "ok", "issue", "review", "na"

IDENTITY_FIRST = "identity_first"
DUPLICATES = "duplicates"
INVALID = "invalid"
TOOL_ERRORS = "tool_errors"
WRITE_WITHOUT_YES = "write_without_yes"

SKIPPED_IDENTITY = "skipped-identity-check"
MALFORMED_CALL = "malformed-tool-call"
UNCONFIRMED_WRITE = "unconfirmed-write"
SETUP_PROBLEM = "setup-problem"

DEFINITIONS = [
    {
        "id": IDENTITY_FIRST,
        "name": "Identity verified first",
        "definition": (
            "find_user_id_by_email or find_user_id_by_name_zip succeeded before any "
            "account tool: get_user_details, get_order_details, or a database-changing "
            "tool. Product lookups and calculate do not count as account tools."
        ),
        "suggests": SKIPPED_IDENTITY,
    },
    {
        "id": DUPLICATES,
        "name": "Duplicate tool calls",
        "definition": (
            "The same tool with the same arguments as the previous tool call, with no "
            "customer message between them."
        ),
        "suggests": MALFORMED_CALL,
    },
    {
        "id": INVALID,
        "name": "Invalid tool calls",
        "definition": (
            "The tool name is not in the run's tool list, or the arguments fail the "
            "tool's schema or include one it does not accept."
        ),
        "suggests": MALFORMED_CALL,
    },
    {
        "id": TOOL_ERRORS,
        "name": "Tool errors",
        "definition": (
            "Tool results marked as errors, with their messages. An error can be a "
            "correct refusal, so this suggests no label."
        ),
        "suggests": None,
    },
    {
        "id": WRITE_WITHOUT_YES,
        "name": "Write without a customer yes",
        "definition": (
            "A database-changing call where the customer's previous message contains "
            "no confirmation word such as yes, yeah, correct or go ahead. This is a "
            "word match, so each one needs review and is not certain."
        ),
        "suggests": UNCONFIRMED_WRITE,
    },
]


def tool_calls(messages: list[dict]) -> list[dict[str, Any]]:
    """Every tool call in order, with its result and the customer's last message."""
    results = {t.get("id"): t for m in messages for t in tool_results(m)}
    calls = []
    customer_text = None
    customer_turn = 0
    for index, message in enumerate(messages):
        if message.get("role") == "user":
            customer_text = message.get("content")
            customer_turn += 1
        for call in message.get("tool_calls") or []:
            result = results.get(call.get("id")) or {}
            calls.append(
                {
                    "step": index,
                    "name": call.get("name"),
                    "arguments": call.get("arguments") or {},
                    "failed": bool(result.get("error")),
                    "result": result.get("content"),
                    "customer_text": customer_text,
                    "customer_turn": customer_turn,
                }
            )
    return calls


def _display_name(name: str | None) -> str:
    return name if name else "(empty tool name)"


def _result(check: str, state: str, count: int, summary: str, evidence: list) -> dict:
    return {
        "id": check,
        "state": state,
        "count": count,
        "summary": summary,
        "evidence": evidence,
    }


def check_identity_first(calls: list[dict]) -> dict:
    verified = next(
        (c for c in calls if c["name"] in IDENTITY_TOOLS and not c["failed"]), None
    )
    first_account = next((c for c in calls if c["name"] in ACCOUNT_TOOLS), None)

    if first_account is None:
        if verified is None:
            return _result(
                IDENTITY_FIRST, NOT_APPLICABLE, 0, "No account tool was used.", []
            )
        evidence = [{"step": verified["step"], "text": f"{verified['name']} succeeded"}]
        return _result(IDENTITY_FIRST, OK, 0, "Identity verified.", evidence)

    if verified is not None and verified["step"] < first_account["step"]:
        evidence = [{"step": verified["step"], "text": f"{verified['name']} succeeded"}]
        summary = "Verified before any account tool."
        return _result(IDENTITY_FIRST, OK, 0, summary, evidence)

    evidence = [
        {
            "step": first_account["step"],
            "text": f"{first_account['name']} ran before identity was verified",
        }
    ]
    if verified is None:
        summary = "Identity was never verified."
    else:
        summary = "Identity was verified too late."
        evidence.append(
            {"step": verified["step"], "text": f"{verified['name']} succeeded later"}
        )
    return _result(IDENTITY_FIRST, ISSUE, 1, summary, evidence)


def check_duplicates(calls: list[dict]) -> dict:
    evidence = []
    for previous, call in zip(calls, calls[1:]):
        same_call = previous["name"] == call["name"] and json.dumps(
            previous["arguments"], sort_keys=True
        ) == json.dumps(call["arguments"], sort_keys=True)
        if same_call and previous["customer_turn"] == call["customer_turn"]:
            name = _display_name(call["name"])
            evidence.append({"step": call["step"], "text": f"{name} repeated"})
    state = ISSUE if evidence else OK
    return _result(
        DUPLICATES, state, len(evidence), _count(evidence, "repeat"), evidence
    )


def _argument_problem(arguments: dict, schema: dict) -> str | None:
    unknown = sorted(set(arguments) - set(schema.get("properties") or {}))
    if unknown:
        return f"unknown argument {unknown[0]}"
    error = next(iter(Draft202012Validator(schema).iter_errors(arguments)), None)
    return error.message if error else None


def check_invalid(calls: list[dict], tool_schemas: dict | None) -> dict:
    if tool_schemas is None:
        return _result(
            INVALID, NOT_APPLICABLE, 0, "This run's tool list is not known.", []
        )
    evidence = []
    for call in calls:
        name = _display_name(call["name"])
        if call["name"] not in tool_schemas:
            evidence.append({"step": call["step"], "text": f"{name} is not a tool"})
            continue
        problem = _argument_problem(call["arguments"], tool_schemas[call["name"]])
        if problem:
            evidence.append({"step": call["step"], "text": f"{name}: {problem}"})
    state = ISSUE if evidence else OK
    return _result(
        INVALID, state, len(evidence), _count(evidence, "invalid call"), evidence
    )


def check_tool_errors(calls: list[dict]) -> dict:
    evidence = [
        {"step": c["step"], "text": f"{_display_name(c['name'])}: {c['result']}"}
        for c in calls
        if c["failed"]
    ]
    state = ISSUE if evidence else OK
    return _result(
        TOOL_ERRORS, state, len(evidence), _count(evidence, "error"), evidence
    )


def check_write_without_yes(calls: list[dict]) -> dict:
    writes = [c for c in calls if c["name"] in WRITE_TOOLS]
    if not writes:
        return _result(WRITE_WITHOUT_YES, NOT_APPLICABLE, 0, "No write attempted.", [])
    evidence = []
    for call in writes:
        customer = call["customer_text"] or ""
        if not CONFIRMATION.search(customer):
            quote = " ".join(customer.split())[:140]
            evidence.append(
                {"step": call["step"], "text": f'{call["name"]} after: "{quote}"'}
            )
    state = REVIEW if evidence else OK
    summary = (
        f"{_count(evidence, 'write')} to review"
        if evidence
        else f"All {len(writes)} followed a confirmation."
    )
    return _result(WRITE_WITHOUT_YES, state, len(evidence), summary, evidence)


def _count(items: list, word: str) -> str:
    return f"{len(items)} {word}{'' if len(items) == 1 else 's'}"


def run_checks(messages: list[dict], tool_schemas: dict | None) -> list[dict]:
    calls = tool_calls(messages)
    return [
        check_identity_first(calls),
        check_duplicates(calls),
        check_invalid(calls, tool_schemas),
        check_tool_errors(calls),
        check_write_without_yes(calls),
    ]


def suggested_labels(checks: list[dict], termination_reason: str | None) -> list[dict]:
    """Labels worth considering. Each carries the reason it was suggested."""
    suggests = {item["id"]: item["suggests"] for item in DEFINITIONS}
    names = {item["id"]: item["name"] for item in DEFINITIONS}
    found: dict[str, list[str]] = {}
    for check in checks:
        label = suggests[check["id"]]
        if label and check["state"] in (ISSUE, REVIEW):
            found.setdefault(label, []).append(names[check["id"]])
    if termination_reason == INFRASTRUCTURE_ERROR:
        found.setdefault(SETUP_PROBLEM, []).append("Infrastructure error")
    return [{"label": label, "because": why} for label, why in found.items()]


def tool_schemas_from_call(tools: list[dict] | None) -> dict | None:
    """Read {tool name: parameter schema} from a saved request's tool list."""
    if not tools:
        return None
    schemas = {}
    for tool in tools:
        function = tool.get("function") or tool
        schemas[function.get("name")] = function.get("parameters") or {}
    return schemas


def totals(rows: list[dict]) -> list[dict]:
    """Per-run totals for each check, from rows carrying a `checks` mapping."""
    summary = []
    for definition in DEFINITIONS:
        results = [row["checks"][definition["id"]] for row in rows if row.get("checks")]
        flagged = [r for r in results if r["state"] in (ISSUE, REVIEW)]
        summary.append(
            {
                **definition,
                "tasks_flagged": len(flagged),
                "tasks_checked": sum(
                    1 for r in results if r["state"] != NOT_APPLICABLE
                ),
                "total": sum(r["count"] for r in results),
            }
        )
    return summary
