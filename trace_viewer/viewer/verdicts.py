"""Turn a simulation's raw reward data into plain states and a one-line cause.

Rewards alone hide what happened: 0.0 can mean a scored failure or a run that was
stopped before any check ran, and 1.0 on NL_ASSERTION can mean that no assertions
exist. These functions keep those cases apart.
"""

from typing import Any

PASS_TOLERANCE = 1e-6  # Same tolerance as tau2.metrics.agent_metrics.is_successful.
INFRASTRUCTURE_ERROR = "infrastructure_error"

PASS, FAIL, STOPPED, INFRA, PENDING = "pass", "fail", "stopped", "infra", "pending"
NONE_DEFINED, NOT_EVALUATED = "none", "not_evaluated"

FAILURE_RESULTS = {FAIL, STOPPED, INFRA}


def is_successful(reward: float | None) -> bool:
    return reward is not None and abs(reward - 1) <= PASS_TOLERANCE


def result_state(sim: dict) -> str:
    if sim.get("termination_reason") == INFRASTRUCTURE_ERROR:
        return INFRA
    reward_info = sim.get("reward_info") or {}
    if is_successful(reward_info.get("reward")):
        return PASS
    # evaluate_simulation leaves reward_basis null when it skips every check.
    return FAIL if reward_info.get("reward_basis") else STOPPED


def db_state(sim: dict) -> str:
    db_check = (sim.get("reward_info") or {}).get("db_check")
    if db_check is None:
        return NOT_EVALUATED
    return PASS if db_check.get("db_match") else FAIL


def nl_state(sim: dict) -> str:
    reward_info = sim.get("reward_info") or {}
    assertions = reward_info.get("nl_assertions")
    if assertions is None:
        return NOT_EVALUATED
    if not assertions:
        return NONE_DEFINED
    return PASS if all(item.get("met") for item in assertions) else FAIL


def tool_results(message: dict) -> list[dict]:
    """A tool message, or the parts of a multi-tool message."""
    if message.get("role") != "tool":
        return []
    return message.get("tool_messages") or [message]


def message_counts(sim: dict) -> dict[str, int]:
    messages = sim.get("messages") or []
    return {
        "messages": len(messages),
        "turns": sum(1 for m in messages if m.get("role") == "user"),
        "tool_calls": sum(len(m.get("tool_calls") or []) for m in messages),
        "tool_errors": sum(
            1 for m in messages for t in tool_results(m) if t.get("error")
        ),
    }


def why(sim: dict, counts: dict[str, int]) -> str:
    """One line saying why this simulation did not pass."""
    result = result_state(sim)
    if result == PASS:
        return ""
    if result == INFRA:
        info = sim.get("info") or {}
        attempts = info.get("failed_after_attempts")
        suffix = f" ({attempts} attempts)" if attempts else ""
        return f"{info.get('error') or 'Infrastructure error.'}{suffix}"
    if result == STOPPED:
        reason = sim.get("termination_reason")
        return (
            f"Stopped early: {reason} ({counts['tool_errors']} tool errors). "
            "DB and NL checks did not run."
        )

    causes = []
    if db_state(sim) == FAIL:
        causes.append("DB does not match the expected state")
    reward_info = sim.get("reward_info") or {}
    for item in reward_info.get("nl_assertions") or []:
        if not item.get("met"):
            causes.append(f"NL not met: {item.get('nl_assertion')}")
    return "; ".join(causes) or "Reward below 1."


def task_sort_key(task_id: Any) -> tuple:
    """Sort task IDs numerically where possible, so 10 follows 8."""
    text = str(task_id)
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


def pass_rate(results: list[str]) -> dict[str, Any]:
    """Count outcomes. Infra errors are excluded, as in tau2's own metrics."""
    scored = sum(1 for result in results if result in {PASS, FAIL, STOPPED})
    passed = sum(1 for result in results if result == PASS)
    return {
        "passed": passed,
        "failed": scored - passed,
        "scored": scored,
        "infra": sum(1 for result in results if result == INFRA),
        "rate": passed / scored if scored else None,
    }
