"""The Tools page API: running fde_agent's tools by hand.

Real-worker tests start tools_worker.py under the benchmark's .venv, against the
real retail database, and skip when that .venv or fde_agent/ is missing. Each
session costs about 4 seconds, so a few sessions cover the checks between them.
"""

import hashlib
import importlib
import json
import sys

import pytest
from conftest import file_states
from fastapi.testclient import TestClient

from viewer import replay, tools
from viewer.api import MAX_SESSION_STEPS, create_app
from viewer.replay import benchmark_python
from viewer.store import REPO_ROOT
from viewer.tools import TOOLS_DIR, ToolRunner, record_changes

pytestmark = pytest.mark.usefixtures("fde_agent_untouched")

needs_tools = pytest.mark.skipif(
    benchmark_python() is None or not TOOLS_DIR.is_dir(),
    reason="the benchmark's .venv or fde_agent/ is missing",
)

DB_JSON = REPO_ROOT / "data" / "tau2" / "domains" / "retail" / "db.json"
YUSUF = {
    "type": "tool",
    "tool": "verify_customer",
    "inputs": {"first_name": "Yusuf", "last_name": "Rossi", "zip": "19122"},
}
YUSUF_ORDERS = {
    "#W6247578": "pending",
    "#W9711842": "cancelled",
    "#W4776164": "pending",
    "#W6679257": "delivered",
    "#W2378156": "delivered",
}
CANCEL = {
    "kind": "cancel_order",
    "order_id": "#W6247578",
    "reason": "no longer needed",
}


def tool(name: str, **inputs) -> dict:
    return {"type": "tool", "tool": name, "inputs": inputs}


def show(number: int) -> dict:
    return {"type": "show_change", "number": number}


def says(text: str) -> dict:
    return {"type": "customer_says", "text": text}


def db_json_hash() -> str:
    return hashlib.sha256(DB_JSON.read_bytes()).hexdigest()


def tool_module(name: str):
    """The real module, imported here. Tool modules import no tau2 at load time.

    Imported without writing bytecode, so these tests never add files to fde_agent/.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.append(str(REPO_ROOT))  # Appended, so viewer modules come first.
    writes_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return importlib.import_module(f"fde_agent.tools.{name}")
    finally:
        sys.dont_write_bytecode = writes_bytecode


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> TestClient:
    return TestClient(create_app(tmp_path_factory.mktemp("simulations")))


def run(client: TestClient, steps: list[dict]) -> dict:
    reply = client.post("/api/tools/session", json={"steps": steps}).json()
    assert reply["ok"], reply
    return reply


# ---------- the checklist, as one session ----------


@pytest.fixture(scope="module")
def checklist(client) -> dict:
    before = db_json_hash()
    reply = run(
        client,
        [
            YUSUF,
            tool(
                "find_variants",
                product_name="t shirt",
                options={"color": "blue", "size": "large"},
            ),
            tool(
                "find_variants",
                product_name="t shirt",
                options={"color": "blue", "size": "L"},
            ),
            tool("prepare_change", **CANCEL),
            tool("confirm_change", change_number=1),
            show(1),
            says("Yes, but only the lamp"),
            tool("confirm_change", change_number=1),
            says("Yes."),
            tool("confirm_change", change_number=1),
            tool("confirm_change", change_number=1),
        ],
    )
    reply["db_json_unchanged_on_disk"] = db_json_hash() == before
    return reply


@needs_tools
def test_verify_lists_all_five_orders(checklist):
    step = checklist["steps"][0]
    assert step["ok"]
    for order_id, status in YUSUF_ORDERS.items():
        assert f"{order_id} | {status} |" in step["text"]
    assert step["notes"]["orders"] == YUSUF_ORDERS
    assert step["notes"]["customer"] == {
        "user_id": "yusuf_rossi_9620",
        "name": "Yusuf Rossi",
    }


@needs_tools
def test_every_store_call_has_its_result(checklist):
    calls = checklist["steps"][0]["calls"]
    assert [c["name"] for c in calls] == (
        ["find_user_id_by_name_zip", "get_user_details"] + ["get_order_details"] * 5
    )
    assert calls[0]["args"] == {
        "first_name": "Yusuf",
        "last_name": "Rossi",
        "zip": "19122",
    }
    assert calls[0]["result"] == "yusuf_rossi_9620"
    assert json.loads(calls[1]["result"])["user_id"] == "yusuf_rossi_9620"
    assert not any(c["error"] or c["write"] for c in calls)


@needs_tools
def test_unknown_size_is_an_error_naming_the_real_ones(checklist):
    step = checklist["steps"][1]
    assert not step["ok"]
    assert step["text"] == (
        "Error: T-Shirt has no size 'large'. Values for size: L, M, S, XL, XXL."
    )


@needs_tools
def test_no_blue_l_shows_three_closest(checklist):
    text = checklist["steps"][2]["text"]
    assert "No variant has color: blue, size: L." in text
    closest = text.split("Closest available (differ in one option):\n")[1]
    assert len(closest.splitlines()) == 3


@needs_tools
def test_prepare_writes_nothing(checklist):
    step = checklist["steps"][3]
    assert step["ok"] and step["text"].startswith("Change #1 prepared")
    assert step["calls"] and not any(c["write"] for c in step["calls"])
    assert step["notes"]["prepared"]["number"] == 1


@needs_tools
def test_confirm_before_show_is_refused_without_a_store_call(checklist):
    step = checklist["steps"][4]
    assert step["text"] == "Error: The customer hasn't seen change #1 yet."
    assert step["calls"] == []


@needs_tools
def test_show_sends_the_prepared_summary(checklist):
    step = checklist["steps"][5]
    assert step["ok"]
    assert (
        step["agent_message"]
        == checklist["steps"][4]["notes"]["prepared"]["customer_text"]
    )
    assert step["agent_message"].startswith(
        "Here's the change I'll make. Nothing has changed yet.\nCancel order #W6247578")
    assert step["notes"]["shown"] == 1


@needs_tools
def test_narrowed_yes_is_refused_and_quoted(checklist):
    said, confirm = checklist["steps"][6], checklist["steps"][7]
    assert said["clear_yes"] is False and said["counts_as_reply"] is True
    assert not confirm["ok"]
    assert '"Yes, but only the lamp"' in confirm["text"]
    assert confirm["calls"] == []


@needs_tools
def test_clear_yes_confirms_with_one_write(checklist):
    said, confirm = checklist["steps"][8], checklist["steps"][9]
    assert said["clear_yes"] is True
    assert confirm["ok"]
    assert "Order status is now 'cancelled'" in confirm["text"]
    assert [(c["name"], c["write"]) for c in confirm["calls"]] == [
        ("cancel_pending_order", True)
    ]


@needs_tools
def test_confirming_again_is_already_done(checklist):
    step = checklist["steps"][10]
    assert not step["ok"] and "already done" in step["text"]
    assert step["calls"] == []


@needs_tools
def test_database_changes_show_the_cancellation(checklist):
    [record] = checklist["db_changes"]
    assert (record["collection"], record["id"]) == ("orders", "#W6247578")
    fields = {row["field"]: row for row in record["fields"]}
    assert (fields["status"]["before"], fields["status"]["after"]) == (
        "pending",
        "cancelled",
    )
    assert checklist["notes"]["orders"]["#W6247578"] == "cancelled"


@needs_tools
def test_db_json_is_never_written(checklist):
    assert checklist["db_file_unchanged"] is True
    assert checklist["db_json_unchanged_on_disk"] is True


@needs_tools
def test_each_request_starts_fresh(client, checklist):
    """Reset: without the confirm steps, the order is pending again."""
    reply = run(client, [YUSUF, tool("get_order", order_id="#W6247578")])
    assert "Order #W6247578 | status: pending" in reply["steps"][1]["text"]
    assert reply["db_changes"] == []


@needs_tools
def test_empty_session_is_a_clean_start(client):
    reply = run(client, [])
    assert reply["steps"] == [] and reply["db_changes"] == []
    assert reply["notes"]["customer"] is None


# ---------- transfers and steps that cannot run ----------


@pytest.fixture(scope="module")
def odd_steps(client) -> dict:
    return run(
        client,
        [
            YUSUF,
            tool("verify_customer", nickname="Yusuf"),
            show(5),
            tool("no_such_tool"),
            says("Hello?"),
            tool("transfer_to_human", summary="Wants to undo a cancellation."),
            tool("get_order", order_id="#W6247578"),
        ],
    )


@needs_tools
def test_an_unknown_argument_is_reported_and_the_session_goes_on(odd_steps):
    step = odd_steps["steps"][1]
    assert not step["ok"]
    assert "unexpected keyword argument 'nickname'" in step["problem"]
    assert step["calls"] == []
    assert odd_steps["steps"][5]["ok"]


@needs_tools
def test_showing_a_change_that_does_not_exist(odd_steps):
    step = odd_steps["steps"][2]
    assert not step["ok"] and "no prepared change" in step["problem"]
    assert "agent_message" not in step


@needs_tools
def test_unknown_tool_name(odd_steps):
    assert "No tool named 'no_such_tool'" in odd_steps["steps"][3]["problem"]


@needs_tools
def test_message_before_any_change_is_not_a_reply(odd_steps):
    step = odd_steps["steps"][4]
    assert step["counts_as_reply"] is False and step["clear_yes"] is False


@needs_tools
def test_transfer_sends_the_transfer_message(odd_steps):
    step = odd_steps["steps"][5]
    assert step["ok"]
    assert step["agent_message"] == tool_module("transfer_to_human").TRANSFER_MESSAGE
    assert [(c["name"], c["write"]) for c in step["calls"]] == [
        ("transfer_to_human_agents", False)
    ]
    assert step["notes"]["transferred"] is True


@needs_tools
def test_nothing_works_after_a_transfer(odd_steps):
    step = odd_steps["steps"][6]
    assert not step["ok"] and "transferred to a human agent" in step["text"]
    assert "agent_message" not in step


# ---------- catalog and the yes check ----------


@needs_tools
def test_catalog_comes_from_the_modules(client):
    reply = client.get("/api/tools").json()
    assert reply["ok"] and reply["skipped"] == []
    names = [entry["name"] for entry in reply["tools"]]
    assert names == list(tools.TOOL_ORDER)
    for entry in reply["tools"]:
        module = tool_module(entry["name"])
        assert entry["description"] == module.DESCRIPTION
        assert entry["parameters"] == module.PARAMETERS
        assert entry["file"] == f"fde_agent/tools/{entry['name']}.py"


@needs_tools
@pytest.mark.parametrize(
    "text, expected",
    [("Yes.", True), ("Yes, but only the lamp", False), ("", False)],
)
def test_clear_yes_route_uses_the_real_rule(client, text, expected):
    reply = client.get("/api/tools/clear-yes", params={"text": text}).json()
    assert reply["ok"] and reply["clear_yes"] is expected
    assert expected == tool_module("confirm_change").is_clear_yes(text)


# ---------- the API's own checks, with no worker ----------


class FakeRunner:
    def __init__(self) -> None:
        self.received: list | None = None

    def run_session(self, steps):
        self.received = steps
        return {"ok": True, "steps": []}


@pytest.fixture
def fake(tmp_path) -> tuple[TestClient, FakeRunner]:
    runner = FakeRunner()
    return TestClient(create_app(tmp_path, tools=runner)), runner


def test_steps_reach_the_runner_with_defaults(fake):
    client, runner = fake
    steps = [{"type": "tool", "tool": "get_order"}, show(1), says("Yes.")]
    assert client.post("/api/tools/session", json={"steps": steps}).status_code == 200
    assert runner.received[0] == {"type": "tool", "tool": "get_order", "inputs": {}}
    assert runner.received[1:] == [show(1), says("Yes.")]


@pytest.mark.parametrize(
    "step",
    [
        {"type": "tool"},
        {"type": "tool", "tool": "get_order", "inputs": "order 1"},
        {"type": "show_change", "number": "first"},
        {"type": "customer_says"},
        {"type": "think", "text": "hmm"},
    ],
)
def test_malformed_steps_are_rejected(fake, step):
    client, runner = fake
    reply = client.post("/api/tools/session", json={"steps": [step]})
    assert reply.status_code == 422
    assert runner.received is None


def test_too_many_steps_are_rejected(fake):
    client, _ = fake
    steps = [says("hi")] * (MAX_SESSION_STEPS + 1)
    assert client.post("/api/tools/session", json={"steps": steps}).status_code == 422


def test_session_needs_json_from_a_local_page(fake):
    client, runner = fake
    assert client.post("/api/tools/session", content="steps").status_code == 415
    foreign = client.post(
        "/api/tools/session",
        json={"steps": []},
        headers={"Origin": "https://example.com"},
    )
    assert foreign.status_code == 403
    assert runner.received is None


def test_missing_tools_folder_is_reported(tmp_path):
    reply = ToolRunner(tools_dir=tmp_path / "missing").catalog()
    assert reply == {"ok": False, "error": "fde_agent/tools was not found."}


def test_missing_benchmark_venv_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "benchmark_python", lambda: None)
    reply = ToolRunner(tools_dir=tmp_path).run_session([])
    assert not reply["ok"] and "Run `uv sync` first" in reply["error"]


@needs_tools
def test_a_crashed_worker_is_reported(tmp_path):
    worker = tmp_path / "broken.py"
    worker.write_text("import sys\nprint('not json')\nsys.exit(3)\n", encoding="utf-8")
    reply = ToolRunner(worker=worker).run_session([])
    assert not reply["ok"] and "exit code 3" in reply["error"]


def test_record_changes_lists_only_changed_fields():
    before = {"status": "pending", "items": ["a"], "address": {"city": "X", "zip": "1"}}
    after = {
        "status": "cancelled",
        "items": ["a"],
        "address": {"city": "Y", "zip": "1"},
    }
    record = record_changes(
        {"collection": "orders", "id": "#W1", "before": before, "after": after}
    )
    assert record["fields"] == [
        {"field": "address.city", "before": "X", "after": "Y"},
        {"field": "status", "before": "pending", "after": "cancelled"},
    ]
    assert not record["added"] and not record["removed"]


def test_a_new_record_is_marked_added():
    record = record_changes(
        {"collection": "orders", "id": "#W2", "before": None, "after": {"a": 1}}
    )
    assert record["added"] and record["fields"][0]["after"] == {"a": 1}


def test_worker_turns_notes_into_plain_json():
    sys.path.insert(0, str(tools.VIEWER_DIR))
    try:
        worker = importlib.import_module("tools_worker")
    finally:
        sys.path.remove(str(tools.VIEWER_DIR))

    class Seen:
        def __init__(self):
            self._seen = {("item", "2"), ("item", "10")}

    assert worker.plain({"done": {10, 2}, "seen": Seen(), "t": (1, "a")}) == {
        "done": [2, 10],
        "seen": {"_seen": [["item", "10"], ["item", "2"]]},
        "t": [1, "a"],
    }
    assert worker.changed_records(
        {"orders": {"a": 1, "b": 2}, "meta": "x"}, {"orders": {"a": 1, "b": 3, "c": 4}}
    ) == [
        {"collection": "orders", "id": "b", "before": 2, "after": 3},
        {"collection": "orders", "id": "c", "before": None, "after": 4},
    ]


@needs_tools
def test_the_worker_leaves_fde_agent_untouched():
    """Bytecode goes to the viewer's cache, never into fde_agent/__pycache__."""
    before = file_states(TOOLS_DIR.parent)
    assert ToolRunner().clear_yes("Yes.")["clear_yes"] is True
    assert file_states(TOOLS_DIR.parent) == before


def test_workers_keep_bytecode_in_the_viewer_cache(monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    environment = replay.worker_environment()
    assert environment["PYTHONPYCACHEPREFIX"] == str(replay.PYCACHE_DIR)
    assert replay.PYCACHE_DIR.parent == tools.VIEWER_DIR / ".cache"
    assert "PYTHONDONTWRITEBYTECODE" not in environment


@pytest.mark.skipif(not TOOLS_DIR.is_dir(), reason="fde_agent/ is missing")
def test_no_tool_text_is_copied_into_the_viewer():
    """Descriptions and parameters must come from the modules, never from copies."""
    viewer_code = [
        path
        for pattern in ("*.py", "viewer/*.py", "static/*.js", "static/*.html")
        for path in tools.VIEWER_DIR.glob(pattern)
    ]
    texts = {path.name: path.read_text(encoding="utf-8") for path in viewer_code}
    for name in tools.TOOL_ORDER:
        opening = tool_module(name).DESCRIPTION[:40]
        copies = [file for file, text in texts.items() if opening in text]
        assert copies == [], f"{name}'s description is copied in {copies}"
