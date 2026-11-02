"""Compare two runs task by task."""

from typing import Any

from viewer.verdicts import FAIL, PASS, STOPPED, task_sort_key

IMPROVED, REGRESSED, SAME = "improved", "regressed", "same"
NOT_COMPARABLE = "not_comparable"  # One side has no scored result.

SCORED = {PASS, FAIL, STOPPED}


def change(first: str | None, second: str | None) -> str:
    if first not in SCORED or second not in SCORED:
        return NOT_COMPARABLE
    if (first == PASS) == (second == PASS):
        return SAME
    return IMPROVED if second == PASS else REGRESSED


def _side(row: dict | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "result": row["result"],
        "db": row.get("db"),
        "nl": row.get("nl"),
        "checks": row.get("checks"),
        "labels": row.get("labels") or [],
    }


def compare_rows(first: list[dict], second: list[dict]) -> dict[str, Any]:
    """One row per task and trial found in either run."""
    old = {(r["task_id"], r["trial"]): r for r in first}
    new = {(r["task_id"], r["trial"]): r for r in second}
    rows = []
    for key in sorted(
        old.keys() | new.keys(), key=lambda k: (task_sort_key(k[0]), k[1])
    ):
        a, b = old.get(key), new.get(key)
        rows.append(
            {
                "task_id": key[0],
                "trial": key[1],
                "first": _side(a),
                "second": _side(b),
                "change": change(a and a["result"], b and b["result"]),
            }
        )
    counts = {
        kind: sum(1 for row in rows if row["change"] == kind)
        for kind in (IMPROVED, REGRESSED, SAME, NOT_COMPARABLE)
    }
    return {"rows": rows, "counts": counts}
