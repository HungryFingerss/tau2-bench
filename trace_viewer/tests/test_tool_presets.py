"""Presets on the Tools page: the shipped file, its checks, and that each preset runs.

Running a preset starts tools_worker.py against the real retail database, about
4 seconds each, and skips when the benchmark's .venv or fde_agent/ is missing.
"""

import textwrap

import pytest
from fastapi.testclient import TestClient

from viewer.api import create_app
from viewer.replay import benchmark_python
from viewer.tools import TOOL_ORDER, TOOLS_DIR, ToolRunner, load_presets

pytestmark = pytest.mark.usefixtures("fde_agent_untouched")

needs_tools = pytest.mark.skipif(
    benchmark_python() is None or not TOOLS_DIR.is_dir(),
    reason="the benchmark's .venv or fde_agent/ is missing",
)

SHIPPED = [
    "Verify Yusuf Rossi (zip 19122)",
    "Blue T-shirt in L",
    "Keyboard -> clicky",
    "Cancel + confirm",
]


def write(tmp_path, text: str):
    path = tmp_path / "presets.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def run_preset(name: str) -> dict:
    preset = next(p for p in load_presets()["presets"] if p["name"] == name)
    reply = ToolRunner().run_session(preset["steps"])
    assert reply["ok"], reply
    return reply


def test_shipped_presets_load():
    loaded = load_presets()
    assert loaded["problem"] is None
    assert [preset["name"] for preset in loaded["presets"]] == SHIPPED
    used = {
        step["tool"]
        for preset in loaded["presets"]
        for step in preset["steps"]
        if step["type"] == "tool"
    }
    assert used <= set(TOOL_ORDER)


def test_short_form_becomes_api_steps(tmp_path):
    path = write(
        tmp_path,
        """
        presets:
          - name: One of each
            steps:
              - tool: get_order
                inputs: {order_id: "#W1"}
              - tool: verify_customer
              - show_change: 2
              - customer_says: "Yes."
        """,
    )
    [preset] = load_presets(path)["presets"]
    assert preset["steps"] == [
        {"type": "tool", "tool": "get_order", "inputs": {"order_id": "#W1"}},
        {"type": "tool", "tool": "verify_customer", "inputs": {}},
        {"type": "show_change", "number": 2},
        {"type": "customer_says", "text": "Yes."},
    ]


@pytest.mark.parametrize(
    "text, problem",
    [
        ("presets: [", "presets.yaml:"),
        ("other: 1", "needs a top-level `presets:` list"),
        ("presets:\n  - steps: [{show_change: 1}]", "preset 1 needs a name"),
        ("presets:\n  - name: Empty\n    steps: []", "'Empty' needs a list of steps"),
        (
            "presets:\n  - name: Two\n    steps: [{show_change: 1, customer_says: hi}]",
            "'Two', step 1 needs exactly one of",
        ),
        (
            "presets:\n  - name: Bad\n    steps: [{customer_says: hi}, {show_change: x}]",
            "'Bad', step 2 (number): Input should be a valid integer",
        ),
    ],
)
def test_a_broken_file_is_explained(tmp_path, text, problem):
    loaded = load_presets(write(tmp_path, text))
    assert loaded["presets"] == []
    assert problem in loaded["problem"]


def test_a_missing_file_means_no_presets(tmp_path):
    assert load_presets(tmp_path / "none.yaml") == {"presets": [], "problem": None}


def test_presets_route_reads_the_file_each_time(tmp_path):
    path = write(tmp_path, "presets:\n  - name: First\n    steps: [{show_change: 1}]")
    client = TestClient(create_app(tmp_path, presets_file=path))
    assert client.get("/api/tools/presets").json()["presets"][0]["name"] == "First"
    write(tmp_path, "presets:\n  - name: Second\n    steps: [{show_change: 1}]")
    assert client.get("/api/tools/presets").json()["presets"][0]["name"] == "Second"


@needs_tools
def test_verify_preset():
    reply = run_preset("Verify Yusuf Rossi (zip 19122)")
    assert len(reply["notes"]["orders"]) == 5


@needs_tools
def test_blue_tshirt_preset():
    large, size_l = run_preset("Blue T-shirt in L")["steps"][1:]
    assert not large["ok"] and "has no size 'large'" in large["text"]
    assert size_l["ok"] and "No variant has color: blue, size: L." in size_l["text"]


@needs_tools
def test_keyboard_preset():
    replacement = run_preset("Keyboard -> clicky")["steps"][-1]
    assert "NOT available (cannot be ordered): item_9025753381" in replacement["text"]


@needs_tools
def test_cancel_and_confirm_preset():
    reply = run_preset("Cancel + confirm")
    refused, done, again = (reply["steps"][i] for i in (4, 6, 7))
    assert not refused["ok"] and '"Yes, but only the lamp"' in refused["text"]
    assert done["ok"] and [c["write"] for c in done["calls"]] == [True]
    assert not again["ok"] and "already done" in again["text"]
    [record] = reply["db_changes"]
    status = next(row for row in record["fields"] if row["field"] == "status")
    assert (status["before"], status["after"]) == ("pending", "cancelled")
