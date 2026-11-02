"""Compare a record's initial, expected and agent versions, field by field.

Comparing three states rather than two shows the kind of mistake: whether the
agent left something undone, did something extra, or did it differently.

Values compare the way the benchmark's database hash does: JSON with sorted keys,
so a tiny float difference counts. Lists of plain values are one field, because
comparing by index turns one extra item into several wrong ones.
"""

import json
from typing import Any, Iterator

MISSED = "missed"  # Agent left the initial value where a change was expected.
ADDED = "added"  # Agent changed something that was expected to stay.
DIFFERENT = "different"  # Both changed it, to different values.
BOTH = "both"  # Both ended on the same value.

_ABSENT = object()


def _same(first: Any, second: Any) -> bool:
    if first is _ABSENT or second is _ABSENT:
        return first is second
    return json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def _is_record_list(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(item, dict) for item in value)


def _child(value: Any, key: Any) -> Any:
    if isinstance(value, dict):
        return value.get(key, _ABSENT)
    return value[key] if key < len(value) else _ABSENT


def _kind(initial: Any, expected: Any, agent: Any) -> str:
    if _same(expected, agent):
        return BOTH
    if _same(agent, initial):
        return MISSED
    return ADDED if _same(expected, initial) else DIFFERENT


def _row(path: str, initial: Any, expected: Any, agent: Any) -> dict[str, Any]:
    versions = {"initial": initial, "expected": expected, "agent": agent}
    return {
        "field": path,
        "kind": _kind(initial, expected, agent),
        "absent": [name for name, value in versions.items() if value is _ABSENT],
        **{name: None if v is _ABSENT else v for name, v in versions.items()},
    }


def _walk(path: str, initial: Any, expected: Any, agent: Any) -> Iterator[dict]:
    if _same(initial, expected) and _same(expected, agent):
        return
    versions = (initial, expected, agent)
    if all(isinstance(value, dict) for value in versions):
        keys: list[Any] = sorted({key for value in versions for key in value})
    elif all(_is_record_list(value) for value in versions):
        keys = list(range(max(len(value) for value in versions)))
    else:
        yield _row(path, initial, expected, agent)
        return
    for key in keys:
        children = (_child(value, key) for value in versions)
        yield from _walk(f"{path}.{key}" if path else str(key), *children)


def compare_record(record: dict) -> dict[str, Any]:
    """Add field rows to a record from the replay worker. Differences come first."""
    versions = [
        _ABSENT if record.get(name) is None else record[name]
        for name in ("initial", "expected", "agent")
    ]
    rows = list(_walk("", *versions))
    rows.sort(key=lambda row: row["kind"] == BOTH)
    return {
        **record,
        "fields": rows,
        "differs": any(row["kind"] != BOTH for row in rows),
    }


def compare_records(records: list[dict]) -> list[dict]:
    compared = [compare_record(record) for record in records]
    return sorted(compared, key=lambda record: not record["differs"])


def summarize(records: list[dict]) -> dict[str, int]:
    rows = [row for record in records for row in record["fields"]]
    return {
        "fields_differ": sum(1 for row in rows if row["kind"] != BOTH),
        "records_differ": sum(1 for record in records if record["differs"]),
        "records_changed": len(records),
        MISSED: sum(1 for row in rows if row["kind"] == MISSED),
        ADDED: sum(1 for row in rows if row["kind"] == ADDED),
        DIFFERENT: sum(1 for row in rows if row["kind"] == DIFFERENT),
    }
