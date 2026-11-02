"""Design files: loading, and proof that v0-baseline describes the real code.

A design document is only useful if it is true, so these tests check its claims
against the benchmark's source and saved runs.
"""

import ast
import copy
import glob
import json
from pathlib import Path

import pytest
import yaml

from viewer.annotations import load_labels
from viewer.designs import (
    DESIGNS_DIR,
    DesignError,
    Designs,
    UnknownDesign,
    compare_designs,
    current_hashes,
    load_design,
    normalized_hash,
    rebuild_prompt,
    source_status,
)
from viewer.store import DEFAULT_SIMULATIONS_DIR, REPO_ROOT

V0 = DESIGNS_DIR / "v0-baseline.yaml"


@pytest.fixture(scope="module")
def design() -> dict:
    return load_design(V0)


def spans(path: Path) -> dict[str, tuple[int, int]]:
    """Every function, method, class and top-level constant, with its line span."""
    found: dict[str, tuple[int, int]] = {}
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                found[prefix + child.name] = (first, child.end_lineno)
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.Assign) and not prefix:
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        found[target.id] = (child.lineno, child.end_lineno)

    walk(tree)
    return found


def constant_text(path: Path, name: str) -> str:
    """A top-level string constant, including a trailing .strip()."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == name:
            value = node.value
            if isinstance(value, ast.Call):
                return value.func.value.value.strip()
            return value.value
    raise AssertionError(f"{name} not found")


def test_decisions_are_complete(design):
    ids = [d["id"] for d in design["decisions"]]
    assert ids == [f"D{i}" for i in range(1, len(ids) + 1)]
    assert len(ids) == 18
    assert all(d["what"] and d["source"] for d in design["decisions"])


def test_required_topics_are_covered(design):
    names = " ".join(d["name"].lower() for d in design["decisions"])
    for topic in ("system prompt", "tool", "history", "parameters", "errors"):
        assert topic in names, topic


def test_not_handled_entries_exist_for_the_prompt_only_rules(design):
    statuses = {d["id"]: d["status"] for d in design["decisions"]}
    for decision_id in ("D3", "D6", "D11", "D12", "D13", "D14", "D15", "D17", "D18"):
        assert statuses[decision_id] == "not_handled", decision_id


def test_d3_is_identity_verification(design):
    d3 = next(d for d in design["decisions"] if d["id"] == "D3")
    assert d3["name"] == "Identity verification is prompt-only"
    assert d3["code_enforces"] == "Nothing."


def test_every_citation_points_at_real_code(design):
    """Each cited file, symbol and line range must exist exactly as stated."""
    checked = 0
    for decision in design["decisions"]:
        for source in decision["source"]:
            path = REPO_ROOT / source["file"]
            assert path.is_file(), source["file"]
            first, last = source["lines"]
            assert 1 <= first <= last
            assert last <= len(path.read_text(encoding="utf-8").splitlines())
            symbol = source.get("symbol")
            if symbol:
                table = spans(path)
                assert symbol in table, f"{decision['id']}: {symbol}"
                start, end = table[symbol]
                assert start <= first and last <= end, f"{decision['id']}: {symbol}"
            checked += 1
    assert checked > 30


@pytest.mark.parametrize(
    "file, line, text",
    [
        ("data/tau2/domains/retail/policy.md", 10, "authenticate the user identity"),
        ("data/tau2/domains/retail/policy.md", 16, "explicit user confirmation"),
        ("data/tau2/domains/retail/policy.md", 24, "transfer the user to a human"),
        ("src/tau2/agent/llm_agent.py", 29, "You cannot do both"),
        ("src/tau2/agent/llm_agent.py", 137, "state.system_messages + state.messages"),
        (
            "src/tau2/utils/llm_utils.py",
            708,
            "json.loads(tool_call.function.arguments)",
        ),
        ("src/tau2/environment/environment.py", 491, "self.to_json_str(resp)"),
        ("src/tau2/cli.py", 247, "--enforce-communication-protocol"),
        ("src/tau2/config.py", 34, "DEFAULT_LLM_ARGS_AGENT"),
        ("src/tau2/config.py", 72, "DEFAULT_MAX_RETRIES"),
    ],
)
def test_key_lines_say_what_the_design_claims(file, line, text):
    lines = (REPO_ROOT / file).read_text(encoding="utf-8").splitlines()
    assert text in lines[line - 1]


def test_prompt_text_equals_the_code(design):
    source = REPO_ROOT / "src/tau2/agent/llm_agent.py"
    prompt = design["system_prompt"]
    assert prompt["instruction"] == constant_text(source, "AGENT_INSTRUCTION")
    assert prompt["template"] == constant_text(source, "SYSTEM_PROMPT")


def test_prompt_rebuilds_byte_for_byte_for_every_saved_call(design):
    """The composition rule must reproduce exactly what reached the model."""
    checked = 0
    for results in DEFAULT_SIMULATIONS_DIR.glob("*/results.json"):
        run_dir = results.parent
        info = json.loads(results.read_text(encoding="utf-8"))["info"]
        rebuilt = rebuild_prompt(design, info)
        pattern = str(run_dir / "artifacts/*/*/llm_debug/*agent_response*.json")
        for path in glob.glob(pattern):
            call = json.loads(Path(path).read_text(encoding="utf-8"))
            content = call["request"]["messages"][0]["content"]
            saved = "\n".join(content) if isinstance(content, list) else content
            assert saved == rebuilt, path
            checked += 1
    if not checked:
        pytest.skip("no saved agent calls on this machine")
    assert checked >= 400


def test_policy_field_alone_is_not_the_prompt(design):
    """Why rebuild_prompt needs the language clause: results.json leaves it out."""
    info = {"environment_info": {"policy": "P"}}
    prompt = rebuild_prompt(design, info)
    assert "<policy>\nP\n</policy>" in prompt
    info["language_tasks"] = {"0": {"agent_language_clause": "L"}}
    assert "<policy>\nP\n\nL\n</policy>" in rebuild_prompt(design, info)


def test_tools_match_a_saved_call_exactly(design):
    paths = glob.glob(
        str(DEFAULT_SIMULATIONS_DIR / "*/artifacts/*/*/llm_debug/*agent_response*.json")
    )
    if not paths:
        pytest.skip("no saved agent calls on this machine")
    tools = json.loads(Path(paths[0]).read_text(encoding="utf-8"))["request"]["tools"]
    saved = {t["function"]["name"]: t["function"] for t in tools}
    documented = {t["name"]: t for t in design["tools"]}
    assert set(saved) == set(documented)
    assert len(saved) == 16
    for name, function in saved.items():
        assert documented[name]["description"] == function["description"]
        assert documented[name]["parameters"] == function["parameters"]


def test_tool_kinds_match_the_decorators(design):
    tree = ast.parse(
        (REPO_ROOT / "src/tau2/domains/retail/tools.py").read_text(encoding="utf-8")
    )
    kinds = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call):
                    if getattr(decorator.func, "id", "") == "is_tool":
                        kinds[node.name] = decorator.args[0].attr.lower()
    assert {t["name"]: t["kind"] for t in design["tools"]} == kinds


def test_recorded_hashes_match_the_files(design):
    assert design["source_hashes"] == current_hashes(design)
    assert source_status(design) == {"changed": [], "missing": []}


def test_label_links_use_real_labels_and_decisions(design):
    """New labels need no default links, so only the reverse direction is checked."""
    labels = {label["id"] for label in load_labels()}
    decisions = {d["id"] for d in design["decisions"]}
    assert set(design["label_links"]) <= labels
    for linked in design["label_links"].values():
        assert set(linked) <= decisions


def test_code_paths_exist(design):
    assert all((REPO_ROOT / path).exists() for path in design["code_paths"])


def test_hash_ignores_line_endings(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.write_bytes(b"x\r\ny\r\n")
    second.write_bytes(b"x\ny\n")
    assert normalized_hash(first) == normalized_hash(second)


def write(folder: Path, design: dict, name: str | None = None) -> Path:
    path = folder / f"{name or design['version']}.yaml"
    path.write_text(yaml.safe_dump(design), encoding="utf-8")
    return path


def test_shipped_designs_load():
    designs = Designs()
    assert "v0-baseline" in designs.all()
    assert designs.problems() == []
    assert designs.get("v0-baseline")["title"]


def test_unknown_design():
    with pytest.raises(UnknownDesign):
        Designs().get("nope")


def test_design_is_found_from_the_agent_name():
    designs = Designs()
    assert designs.for_agent("llm_agent")["version"] == "v0-baseline"
    assert designs.for_agent("something_else") is None


def test_filename_must_match_the_version(design, tmp_path):
    with pytest.raises(DesignError):
        load_design(write(tmp_path, design, "other-name"))


@pytest.mark.parametrize("field", ["version", "title", "decisions", "system_prompt"])
def test_missing_top_level_field_is_rejected(design, tmp_path, field):
    broken = copy.deepcopy(design)
    del broken[field]
    path = tmp_path / "v0-baseline.yaml"
    path.write_text(yaml.safe_dump(broken), encoding="utf-8")
    with pytest.raises(DesignError):
        load_design(path)


@pytest.mark.parametrize("field", ["name", "status", "what", "left_to_model", "source"])
def test_missing_decision_field_is_rejected(design, tmp_path, field):
    broken = copy.deepcopy(design)
    del broken["decisions"][0][field]
    with pytest.raises(DesignError):
        load_design(write(tmp_path, broken))


def test_duplicate_decision_is_rejected(design, tmp_path):
    broken = copy.deepcopy(design)
    broken["decisions"][1]["id"] = "D1"
    with pytest.raises(DesignError):
        load_design(write(tmp_path, broken))


def test_bad_status_is_rejected(design, tmp_path):
    broken = copy.deepcopy(design)
    broken["decisions"][0]["status"] = "maybe"
    with pytest.raises(DesignError):
        load_design(write(tmp_path, broken))


def test_a_broken_file_is_reported_without_hiding_good_ones(design, tmp_path):
    write(tmp_path, design)
    (tmp_path / "broken.yaml").write_text("version: [", encoding="utf-8")
    designs = Designs(tmp_path)
    assert list(designs.all()) == ["v0-baseline"]
    assert len(designs.problems()) == 1


def test_edits_are_picked_up_without_a_restart(design, tmp_path):
    write(tmp_path, design)
    designs = Designs(tmp_path)
    assert designs.get("v0-baseline")["title"] == design["title"]
    write(tmp_path, {**design, "title": "A longer new title than before"})
    assert designs.get("v0-baseline")["title"] == "A longer new title than before"


def test_only_cited_files_can_be_read():
    designs = Designs()
    assert "class LLMAgent" in designs.read_source("src/tau2/agent/llm_agent.py")
    assert designs.read_source("pyproject.toml") is None
    assert designs.read_source(".env") is None
    assert designs.read_source("../../secrets.txt") is None


def second_version(design: dict) -> dict:
    other = copy.deepcopy(design)
    other["version"] = "v1-test"
    d3 = next(d for d in other["decisions"] if d["id"] == "D3")
    d3["status"] = "implemented"
    d3["code_enforces"] = "A gate blocks account tools until identity is verified."
    other["decisions"] = [d for d in other["decisions"] if d["id"] != "D13"]
    other["decisions"].append({**other["decisions"][0], "id": "D19", "name": "New"})
    other["decisions"][0]["source"] = [
        {"file": "src/tau2/agent/llm_agent.py", "lines": [1, 2]}
    ]
    other["system_prompt"]["instruction"] += "\nAlways verify identity."
    other["parameters"]["temperature"] = "0.2"
    other["tools"] = other["tools"][1:]
    other["tools"][0] = {**other["tools"][0], "description": "changed"}
    return other


def test_compare_finds_every_kind_of_change(design):
    result = compare_designs(design, second_version(design))
    assert result["same"] is False
    assert [d["id"] for d in result["added"]] == ["D19"]
    assert [d["id"] for d in result["removed"]] == ["D13"]
    changed = {item["id"]: item["fields"] for item in result["changed"]}
    assert changed == {"D3": ["status", "code_enforces"]}
    assert result["moved"] == ["D1"]
    assert result["instruction_changed"] is True
    assert result["template_changed"] is False
    assert result["tools_removed"] == [design["tools"][0]["name"]]
    assert result["tools_changed"] == [design["tools"][1]["name"]]
    assert [p["name"] for p in result["parameters_changed"]] == ["temperature"]


def test_compare_with_itself_is_the_same(design):
    assert compare_designs(design, copy.deepcopy(design))["same"] is True
