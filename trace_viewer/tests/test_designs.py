"""Design files: loading, and proof that each shipped design describes the real code.

A design document is only useful if it is true, so these tests check its claims
against the source and saved runs: v0-baseline against the benchmark's llm_agent,
v1-fde-agent against fde_agent/.
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
    DEFAULT_ENTRY_POINT,
    DESIGNS_DIR,
    DesignError,
    Designs,
    UnknownDesign,
    compare_designs,
    current_hashes,
    entry_point,
    load_design,
    normalized_hash,
    rebuild_prompt,
    source_status,
)
from viewer.replay import benchmark_python
from viewer.store import DEFAULT_SIMULATIONS_DIR, REPO_ROOT, read_json_file
from viewer.tools import TOOLS_DIR, ToolRunner

V0 = DESIGNS_DIR / "v0-baseline.yaml"
V1 = DESIGNS_DIR / "v1-fde-agent.yaml"
SHIPPED = [V0, V1]


@pytest.fixture(scope="module")
def design() -> dict:
    return load_design(V0)


@pytest.fixture(scope="module")
def v1() -> dict:
    return load_design(V1)


@pytest.fixture(scope="module", params=SHIPPED, ids=lambda path: path.stem)
def shipped(request) -> dict:
    """Each design in the repo, for the checks every design must pass."""
    return load_design(request.param)


def saved_agent_calls(implementation: str):
    """(run info, raw agent call file) for every saved call of runs of one agent."""
    for results in sorted(DEFAULT_SIMULATIONS_DIR.glob("*/results.json")):
        info = read_json_file(results)["info"]  # In-place saves use cp1252.
        if (info.get("agent_info") or {}).get("implementation") != implementation:
            continue
        pattern = str(results.parent / "artifacts/*/*/llm_debug/*agent_response*.json")
        for path in sorted(glob.glob(pattern)):
            yield info, Path(path)


def system_text(call: dict) -> str:
    content = call["request"]["messages"][0]["content"]
    return "\n".join(content) if isinstance(content, list) else content


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


def test_every_citation_points_at_real_code(shipped):
    """Each cited file, symbol and line range must exist exactly as stated."""
    checked = 0
    for decision in shipped["decisions"]:
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
    for info, path in saved_agent_calls("llm_agent"):
        call = json.loads(path.read_text(encoding="utf-8"))
        assert system_text(call) == rebuild_prompt(design, info), path
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
    first = next(saved_agent_calls("llm_agent"), None)
    if first is None:
        pytest.skip("no saved agent calls on this machine")
    tools = json.loads(first[1].read_text(encoding="utf-8"))["request"]["tools"]
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


def test_recorded_hashes_match_the_files(shipped):
    assert shipped["source_hashes"] == current_hashes(shipped)
    assert source_status(shipped) == {"changed": [], "missing": []}


def test_label_links_use_real_labels_and_decisions(shipped):
    """New labels need no default links, so only the reverse direction is checked."""
    labels = {label["id"] for label in load_labels()}
    decisions = {d["id"] for d in shipped["decisions"]}
    assert set(shipped["label_links"]) <= labels
    for linked in shipped["label_links"].values():
        assert set(linked) <= decisions


def test_code_paths_exist(shipped):
    assert all((REPO_ROOT / path).exists() for path in shipped["code_paths"])


def test_baseline_starts_with_tau2_itself(design):
    assert "entry_point" not in design
    assert (
        entry_point(design) == list(DEFAULT_ENTRY_POINT) == ["uv", "run", "tau2", "run"]
    )


# ---------- v1-fde-agent ----------


def test_v1_decisions_are_f1_to_f27_and_never_reuse_baseline_ids(v1, design):
    ids = [d["id"] for d in v1["decisions"]]
    assert ids == [f"F{i}" for i in range(1, 28)]
    assert not set(ids) & {d["id"] for d in design["decisions"]}


def test_v1_addresses_point_at_real_baseline_decisions(v1, design):
    baseline = {d["id"] for d in design["decisions"]}
    linked = {d["id"]: d["addresses"] for d in v1["decisions"]}
    assert all(isinstance(ids, list) for ids in linked.values())
    for decision_id, ids in linked.items():
        assert set(ids) <= baseline, decision_id
    # The links the plan named, so they can't be dropped by accident.
    for decision_id, wanted in {
        "F1": "D1",
        "F2": "D2",
        "F5": "D15",
        "F6": "D3",
        "F7": "D14",
        "F8": "D14",
        "F10": "D11",
        "F11": "D11",
        "F12": "D11",
        "F14": "D18",
        "F17": "D17",
    }.items():
        assert wanted in linked[decision_id], decision_id
    assert {"D15", "D14"} <= set(linked["F7"])
    assert {"D6", "D13", "D18"} <= set(linked["F14"])


def test_v1_is_for_fde_agent_and_starts_through_its_runner(v1):
    designs = Designs()
    assert v1["applies_to"]["agent_implementation"] == "fde_agent"
    assert designs.for_agent("fde_agent")["version"] == "v1-fde-agent"
    assert entry_point(v1) == ["uv", "run", "python", "-m", "fde_agent.run", "run"]
    assert (REPO_ROOT / "fde_agent" / "run.py").is_file()
    assert v1["code_paths"] == ["fde_agent/"]


def test_v1_prompt_is_instructions_md_exactly(v1):
    """prompt.load_instructions() reads the file and strips it."""
    prompt = v1["system_prompt"]
    text = (REPO_ROOT / prompt["file"]).read_text(encoding="utf-8").strip()
    assert prompt["instruction"] == text
    assert "template" not in prompt
    assert rebuild_prompt(v1, {"environment_info": {"policy": "P"}}) == text


def test_v1_prompt_matches_every_saved_call(v1):
    checked = 0
    for info, path in saved_agent_calls("fde_agent"):
        call = json.loads(path.read_text(encoding="utf-8"))
        assert system_text(call) == rebuild_prompt(v1, info), path
        checked += 1
    if not checked:
        pytest.skip("no saved fde_agent calls on this machine")


def test_v1_tools_match_a_saved_call(v1):
    first = next(saved_agent_calls("fde_agent"), None)
    if first is None:
        pytest.skip("no saved fde_agent calls on this machine")
    tools = json.loads(first[1].read_text(encoding="utf-8"))["request"]["tools"]
    saved = [t["function"] for t in tools]
    documented = [
        {k: t[k] for k in ("name", "description", "parameters")} for t in v1["tools"]
    ]
    assert saved == documented


@pytest.mark.skipif(
    benchmark_python() is None or not TOOLS_DIR.is_dir(),
    reason="the benchmark's .venv or fde_agent/ is missing",
)
def test_v1_tools_equal_the_modules(v1):
    """NAME, DESCRIPTION and PARAMETERS of each module, read by the tools worker."""
    reply = ToolRunner().catalog()
    assert reply["ok"], reply
    real = {t["name"]: t for t in reply["tools"]}
    assert [t["name"] for t in v1["tools"]] == list(real)
    for tool in v1["tools"]:
        assert tool["description"] == real[tool["name"]]["description"]
        assert tool["parameters"] == real[tool["name"]]["parameters"]
    kinds = {t["name"]: t["kind"] for t in v1["tools"]}
    assert [name for name, kind in kinds.items() if kind == "write"] == [
        "confirm_change"
    ]


@pytest.mark.parametrize(
    "file, line, text",
    [
        ("fde_agent/instructions.md", 10, "Before anything else, verify the customer"),
        ("fde_agent/agent.py", 31, 'DEFAULT_MODEL = "ollama_chat/gpt-oss:20b"'),
        ("fde_agent/agent.py", 55, 'self.llm_args["seed"] = seed'),
        ("fde_agent/model_client.py", 38, "ATTEMPTS = 3"),
        ("fde_agent/model_client.py", 194, '"temperature": 0.0'),
        ("fde_agent/driver.py", 48, "MAX_FAILED_TRIES = 5"),
        ("fde_agent/driver.py", 445, 'by="code"'),
        ("fde_agent/driver.py", 458, "TRANSFER_MESSAGE"),
        ("fde_agent/tools/get_order.py", 154, "Possible actions"),
        ("fde_agent/tools/confirm_change.py", 171, "yield [change.call]"),
    ],
)
def test_v1_key_lines_say_what_the_design_claims(file, line, text):
    lines = (REPO_ROOT / file).read_text(encoding="utf-8").splitlines()
    assert text in lines[line - 1]


def test_v1_and_baseline_compare_without_errors(design, v1):
    result = compare_designs(design, v1)
    assert result["same"] is False
    assert len(result["added"]) == 27 and len(result["removed"]) == 18   # F1-F27 vs D1-D18
    assert result["instruction_changed"] and result["template_changed"]
    assert "confirm_change" in result["tools_added"]


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
    assert {"v0-baseline", "v1-fde-agent"} <= set(designs.all())
    assert designs.problems() == []
    assert designs.get("v0-baseline")["title"]
    assert designs.get("v1-fde-agent")["title"]


@pytest.mark.parametrize("bad", [[], "uv run x", ["uv", ""], ["uv", 3]])
def test_bad_entry_point_is_rejected(design, tmp_path, bad):
    broken = {**copy.deepcopy(design), "entry_point": bad}
    with pytest.raises(DesignError):
        load_design(write(tmp_path, broken))


def test_addresses_must_be_a_list(v1, tmp_path):
    broken = copy.deepcopy(v1)
    broken["decisions"][0]["addresses"] = "D1"
    with pytest.raises(DesignError):
        load_design(write(tmp_path, broken))


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
    assert "class Driver" in designs.read_source("fde_agent/driver.py")
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
