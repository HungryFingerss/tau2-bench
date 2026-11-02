"""Saving and loading labels, the run summary, and the write route's protection."""

import json
from pathlib import Path

import pytest
import yaml
from conftest import BASELINE
from fastapi.testclient import TestClient

from viewer.annotations import (
    Annotations,
    InvalidAnnotation,
    LabelsFileError,
    analysis,
    load_labels,
    task_key,
)
from viewer.api import create_app

LABELS = {
    "labels": [
        {"id": "first", "name": "First", "description": "One."},
        {"id": "second", "name": "Second"},
    ]
}


@pytest.fixture
def notes(tmp_path) -> Annotations:
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump(LABELS), encoding="utf-8")
    return Annotations(tmp_path / "annotations", path)


def label(label_id: str, *decisions: str) -> dict:
    return {"id": label_id, "decisions": list(decisions)}


def test_shipped_labels_include_the_starting_set():
    """labels.yaml is yours to extend, so extra labels are fine."""
    ids = {item["id"] for item in load_labels()}
    assert ids >= {
        "wrong-variant-choice",
        "misread-tool-output",
        "incomplete-lookup",
        "malformed-tool-call",
        "ignored-customer-change",
        "skipped-identity-check",
        "unconfirmed-write",
        "setup-problem",
    }
    assert all(item["description"] for item in load_labels())


def test_save_and_load_round_trip(notes):
    notes.save("run", "2", 0, "sim-1", [label("first", "D3")], "Looks wrong.")
    entry = Annotations(notes.directory, notes.labels_path).read("run")["2:0"]
    assert entry["labels"] == [{"id": "first", "decisions": ["D3"]}]
    assert entry["note"] == "Looks wrong."
    assert entry["sim_id"] == "sim-1"
    assert entry["task_id"] == "2"
    assert entry["updated_at"]


def test_file_goes_in_the_annotations_folder_under_the_run_name(notes):
    notes.save("my-run", "1", 0, None, [label("first")], "")
    path = notes.directory / "my-run.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["run"] == "my-run"
    assert "1:0" in data["tasks"]


def test_trials_are_kept_apart(notes):
    notes.save("run", "1", 0, None, [label("first")], "")
    notes.save("run", "1", 1, None, [label("second")], "")
    tasks = notes.read("run")
    assert tasks["1:0"]["labels"][0]["id"] == "first"
    assert tasks["1:1"]["labels"][0]["id"] == "second"


def test_a_note_can_be_saved_with_no_labels(notes):
    assert notes.save("run", "1", 0, None, [], "Worth a look.")["note"]


def test_clearing_everything_removes_the_entry(notes):
    notes.save("run", "1", 0, None, [label("first")], "note")
    assert notes.save("run", "1", 0, None, [], "  ") is None
    assert notes.read("run") == {}


def test_saving_again_replaces(notes):
    notes.save("run", "1", 0, None, [label("first")], "a")
    notes.save("run", "1", 0, None, [label("second")], "b")
    entry = notes.read("run")["1:0"]
    assert [item["id"] for item in entry["labels"]] == ["second"]
    assert entry["note"] == "b"


def test_non_ascii_notes_survive(notes):
    notes.save("run", "1", 0, None, [], "τ-bench’s “quote”")
    assert notes.read("run")["1:0"]["note"] == "τ-bench’s “quote”"


def test_no_temporary_files_are_left_behind(notes):
    for index in range(5):
        notes.save("run", str(index), 0, None, [label("first")], "")
    assert [p.name for p in notes.directory.iterdir()] == ["run.json"]


def test_failed_save_keeps_the_previous_file(notes, monkeypatch):
    notes.save("run", "1", 0, None, [label("first")], "keep me")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("viewer.annotations.os.replace", broken)
    with pytest.raises(OSError):
        notes.save("run", "2", 0, None, [label("first")], "new")
    monkeypatch.undo()
    assert notes.read("run")["1:0"]["note"] == "keep me"
    assert "2:0" not in notes.read("run")
    assert [p.name for p in notes.directory.iterdir()] == ["run.json"]


def test_missing_or_corrupt_file_reads_as_empty(notes):
    assert notes.read("nothing") == {}
    notes.directory.mkdir()
    (notes.directory / "bad.json").write_text("{", encoding="utf-8")
    assert notes.read("bad") == {}


def test_unknown_label_is_refused(notes):
    with pytest.raises(InvalidAnnotation):
        notes.save("run", "1", 0, None, [label("nope")], "")


def test_duplicate_label_is_refused(notes):
    with pytest.raises(InvalidAnnotation):
        notes.save("run", "1", 0, None, [label("first"), label("first")], "")


def test_unknown_decision_is_refused(notes):
    with pytest.raises(InvalidAnnotation):
        notes.save("run", "1", 0, None, [label("first", "D99")], "", {"D1"})


def test_very_long_note_is_refused(notes):
    with pytest.raises(InvalidAnnotation):
        notes.save("run", "1", 0, None, [], "x" * 6000)


def test_label_edits_apply_without_a_restart(notes):
    assert [item["id"] for item in notes.labels()] == ["first", "second"]
    edited = {"labels": [*LABELS["labels"], {"id": "third"}]}
    notes.labels_path.write_text(yaml.safe_dump(edited), encoding="utf-8")
    assert notes.labels()[-1] == {"id": "third", "name": "third", "description": ""}
    notes.save("run", "1", 0, None, [label("third")], "")


@pytest.mark.parametrize(
    "text",
    ["labels: [", "labels: []", "other: 1", "labels:\n  - name: no id", ""],
)
def test_bad_labels_file_is_reported(tmp_path, text):
    path = tmp_path / "labels.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LabelsFileError):
        load_labels(path)


def test_duplicate_label_id_in_the_file_is_reported(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text("labels:\n  - id: a\n  - id: a\n", encoding="utf-8")
    with pytest.raises(LabelsFileError):
        load_labels(path)


def row(task_id, result):
    return {"task_id": task_id, "trial": 0, "result": result}


DESIGN = {
    "decisions": [
        {"id": "D3", "name": "Identity", "status": "not_handled"},
        {"id": "D14", "name": "Reading", "status": "not_handled"},
    ]
}
NAMES = [{"id": "a", "name": "Label A"}, {"id": "b", "name": "Label B"}]


def entry(*labels):
    return {"labels": list(labels), "note": ""}


def test_analysis_ranks_decisions_by_failures():
    rows = [row("1", "fail"), row("2", "fail"), row("3", "stopped"), row("4", "pass")]
    tasks = {
        task_key("1", 0): entry(label("a", "D3")),
        task_key("2", 0): entry(label("a", "D3")),
        task_key("3", 0): entry(label("b", "D14")),
    }
    result = analysis(rows, tasks, NAMES, DESIGN)
    assert [d["id"] for d in result["by_decision"]] == ["D3", "D14"]
    assert len(result["by_decision"][0]["failures"]) == 2
    assert result["by_decision"][0]["name"] == "Identity"
    assert [item["id"] for item in result["by_label"]] == ["a", "b"]
    assert result["failures"] == 3
    assert result["labelled"] == 3
    assert result["unlabelled"] == []


def test_a_decision_counts_once_per_task():
    tasks = {task_key("1", 0): entry(label("a", "D3"), label("b", "D3", "D14"))}
    result = analysis([row("1", "fail")], tasks, NAMES, DESIGN)
    by_id = {d["id"]: d for d in result["by_decision"]}
    assert len(by_id["D3"]["failures"]) == 1
    assert len(by_id["D14"]["failures"]) == 1


def test_labels_on_passed_tasks_are_counted_apart():
    tasks = {task_key("0", 0): entry(label("a", "D3"))}
    result = analysis([row("0", "pass")], tasks, NAMES, DESIGN)
    decision = result["by_decision"][0]
    assert decision["failures"] == []
    assert decision["passed"] == [{"task_id": "0", "trial": 0}]
    assert result["failures"] == 0
    assert result["labelled"] == 0


def test_failures_ahead_of_passes_in_the_ranking():
    rows = [row("1", "fail"), row("2", "pass"), row("3", "pass")]
    tasks = {
        task_key("1", 0): entry(label("a", "D14")),
        task_key("2", 0): entry(label("a", "D3")),
        task_key("3", 0): entry(label("a", "D3")),
    }
    result = analysis(rows, tasks, NAMES, DESIGN)
    assert result["by_decision"][0]["id"] == "D14"


def test_unlabelled_failures_are_listed():
    rows = [row("1", "fail"), row("2", "infra"), row("3", "pass"), row("4", "pending")]
    result = analysis(rows, {}, NAMES, DESIGN)
    assert result["failures"] == 2
    assert [t["task_id"] for t in result["unlabelled"]] == ["1", "2"]


def test_a_note_alone_does_not_count_as_labelled():
    tasks = {task_key("1", 0): {"labels": [], "note": "hm"}}
    result = analysis([row("1", "fail")], tasks, NAMES, DESIGN)
    assert len(result["unlabelled"]) == 1


def test_decision_missing_from_the_design_is_still_shown():
    tasks = {task_key("1", 0): entry(label("a", "D99"))}
    result = analysis([row("1", "fail")], tasks, NAMES, DESIGN)
    assert result["by_decision"][0]["name"] == "Not in this run's design"


def test_unknown_label_is_still_counted():
    tasks = {task_key("1", 0): entry(label("gone"))}
    result = analysis([row("1", "fail")], tasks, NAMES, DESIGN)
    assert result["by_label"][0]["name"] == "gone"


@pytest.fixture
def app_client(store, tmp_path) -> TestClient:
    notes = Annotations(tmp_path / "annotations")
    return TestClient(create_app(store.simulations_dir, notes=notes))


def url(task_id="2", trial=0):
    return f"/api/runs/{BASELINE}/tasks/{task_id}/annotation?trial={trial}"


def body(*labels, note=""):
    return {"labels": list(labels), "note": note}


def test_saving_through_the_route_shows_up_on_every_page(app_client):
    saved = app_client.post(
        url(), json=body(label("misread-tool-output", "D14"), note="Counted 12.")
    )
    assert saved.status_code == 200
    assert saved.json()["annotation"]["note"] == "Counted 12."

    task = app_client.get(f"/api/runs/{BASELINE}/tasks/2").json()
    assert task["annotation"]["labels"][0]["id"] == "misread-tool-output"
    assert task["annotation_is_stale"] is False

    run = app_client.get(f"/api/runs/{BASELINE}").json()
    row = next(r for r in run["rows"] if r["task_id"] == "2")
    assert row["labels"] == ["misread-tool-output"]
    decisions = run["analysis"]["by_decision"]
    assert decisions[0]["id"] == "D14"
    assert decisions[0]["name"] == "Reading tool results is left to the model"


def test_simulation_id_is_recorded_from_the_run(app_client):
    entry = app_client.post(url(), json=body(label("setup-problem"))).json()
    task = app_client.get(f"/api/runs/{BASELINE}/tasks/2").json()
    assert entry["annotation"]["sim_id"] == task["sim"]["id"]


def test_label_on_a_replaced_simulation_is_flagged(app_client, store, tmp_path):
    notes = Annotations(tmp_path / "other")
    notes.save(BASELINE, "2", 0, "an-older-sim", [label("setup-problem")], "")
    client = TestClient(create_app(store.simulations_dir, notes=notes))
    assert client.get(f"/api/runs/{BASELINE}/tasks/2").json()["annotation_is_stale"]


def test_route_refuses_unknown_labels_and_decisions(app_client):
    assert app_client.post(url(), json=body(label("nope"))).status_code == 422
    bad = body(label("setup-problem", "D99"))
    assert app_client.post(url(), json=bad).status_code == 422


def test_route_refuses_unknown_tasks_and_runs(app_client):
    assert app_client.post(url("99999"), json=body()).status_code == 404
    path = "/api/runs/nope/tasks/1/annotation"
    assert app_client.post(path, json=body()).status_code == 404


def test_foreign_page_cannot_post(app_client):
    headers = {"origin": "https://evil.example"}
    response = app_client.post(url(), json=body(), headers=headers)
    assert response.status_code == 403


def test_local_page_can_post(app_client):
    headers = {"origin": "http://127.0.0.1:8765"}
    assert app_client.post(url(), json=body(), headers=headers).status_code == 200


def test_form_style_post_is_refused(app_client):
    """A plain HTML form on another site can only send form encodings."""
    response = app_client.post(url(), data={"note": "x"})
    assert response.status_code == 415


def test_nothing_is_written_into_the_simulations_folder(app_client, store):
    folder = Path(store.simulations_dir)
    before = sorted(str(p) for p in (folder / BASELINE).iterdir())
    app_client.post(url(), json=body(label("setup-problem"), note="x"))
    assert sorted(str(p) for p in (folder / BASELINE).iterdir()) == before


def test_task_page_offers_labels_decisions_and_defaults(app_client):
    task = app_client.get(f"/api/runs/{BASELINE}/tasks/0").json()
    assert len(task["labels"]) == len(load_labels())
    assert len(task["design_decisions"]) == 18
    assert task["label_links"]["skipped-identity-check"] == ["D3"]
    assert [s["label"] for s in task["suggested_labels"]] == ["skipped-identity-check"]


def test_task_8_suggests_setup_problem(app_client):
    task = app_client.get(f"/api/runs/{BASELINE}/tasks/8").json()
    assert [s["label"] for s in task["suggested_labels"]] == ["setup-problem"]
