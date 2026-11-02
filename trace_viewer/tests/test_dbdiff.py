"""The field diff rules, on small hand-written records."""

from viewer.dbdiff import (
    ADDED,
    BOTH,
    DIFFERENT,
    MISSED,
    compare_record,
    compare_records,
    summarize,
)


def fields(initial, expected, agent):
    record = {
        "collection": "orders",
        "id": "#W1",
        "initial": initial,
        "expected": expected,
        "agent": agent,
    }
    return {row["field"]: row for row in compare_record(record)["fields"]}


def test_three_labels_and_matching_changes():
    rows = fields(
        {"a": 1, "b": 1, "c": 1, "d": 1, "e": 1},
        {"a": 2, "b": 1, "c": 2, "d": 2, "e": 1},
        {"a": 1, "b": 2, "c": 3, "d": 2, "e": 1},
    )
    assert rows["a"]["kind"] == MISSED
    assert rows["b"]["kind"] == ADDED
    assert rows["c"]["kind"] == DIFFERENT
    assert rows["d"]["kind"] == BOTH
    assert "e" not in rows


def test_list_of_plain_values_is_one_field():
    """One extra item must not read as several wrong ones."""
    rows = fields(
        {"exchange_items": None},
        {"exchange_items": ["4983901480"]},
        {"exchange_items": ["1151293680", "4983901480"]},
    )
    assert list(rows) == ["exchange_items"]
    row = rows["exchange_items"]
    assert row["kind"] == DIFFERENT
    assert row["expected"] == ["4983901480"]
    assert row["agent"] == ["1151293680", "4983901480"]


def test_list_order_counts_as_a_difference():
    """The benchmark's hash is order sensitive, so the diff must be too."""
    rows = fields({"x": None}, {"x": ["a", "b"]}, {"x": ["b", "a"]})
    assert rows["x"]["kind"] == DIFFERENT


def test_lists_of_records_compare_entry_by_entry():
    initial = {"items": [{"id": "1", "price": 10}, {"id": "2", "price": 20}]}
    expected = {"items": [{"id": "1", "price": 15}, {"id": "2", "price": 20}]}
    rows = fields(initial, expected, initial)
    assert list(rows) == ["items.0.price"]
    assert rows["items.0.price"]["kind"] == MISSED


def test_missing_list_entry_is_one_row_marked_absent():
    payment = {"transaction_type": "payment", "amount": 0.05}
    rows = fields(
        {"history": [{"a": 1}]},
        {"history": [{"a": 1}, payment]},
        {"history": [{"a": 1}]},
    )
    row = rows["history.1"]
    assert row["kind"] == MISSED
    assert row["expected"] == payment
    assert row["absent"] == ["initial", "agent"]


def test_tiny_float_difference_is_a_difference():
    rows = fields({"x": 0.05}, {"x": 0.05}, {"x": 0.04999999999999716})
    assert rows["x"]["kind"] == ADDED


def test_int_and_float_are_different_like_the_benchmark_hash():
    rows = fields({"x": 1}, {"x": 1}, {"x": 1.0})
    assert rows["x"]["kind"] == ADDED


def test_nested_dicts_report_full_paths():
    rows = fields(
        {"address": {"city": "A", "zip": "1"}},
        {"address": {"city": "B", "zip": "1"}},
        {"address": {"city": "A", "zip": "1"}},
    )
    assert list(rows) == ["address.city"]


def test_none_is_a_value_not_absent():
    rows = fields({"x": None}, {"x": 5}, {"x": None})
    assert rows["x"]["kind"] == MISSED
    assert rows["x"]["absent"] == []


def test_record_present_in_only_one_state():
    record = {
        "collection": "orders",
        "id": "#W9",
        "initial": None,
        "expected": None,
        "agent": {"status": "pending"},
    }
    compared = compare_record(record)
    assert compared["differs"] is True
    assert compared["fields"][0]["kind"] == ADDED
    assert compared["fields"][0]["absent"] == ["initial", "expected"]


def test_differences_sort_first_and_summary_counts_them():
    matched = {
        "collection": "orders",
        "id": "#W1",
        "initial": {"x": 1},
        "expected": {"x": 2},
        "agent": {"x": 2},
    }
    differs = {
        "collection": "orders",
        "id": "#W2",
        "initial": {"x": 1, "y": 1},
        "expected": {"x": 2, "y": 2},
        "agent": {"x": 1, "y": 3},
    }
    records = compare_records([matched, differs])
    assert [record["id"] for record in records] == ["#W2", "#W1"]
    assert summarize(records) == {
        "fields_differ": 2,
        "records_differ": 1,
        "records_changed": 2,
        MISSED: 1,
        ADDED: 0,
        DIFFERENT: 1,
    }
