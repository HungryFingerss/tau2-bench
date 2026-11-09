"""The run script, with the benchmark replaced by a fake command."""

import json
import time
from pathlib import Path

import pytest

import record_run
from record_run import (
    Heartbeat,
    RecordError,
    build_command,
    capture_code,
    child_environment,
    option_value,
    parse_arguments,
    read_judges,
    record,
)
from viewer import runmeta
from viewer.designs import Designs
from viewer.store import RESULTS_FILE

BASE = ["--design", "v0-baseline", "--note", "first try"]
TAU2 = ["--", "--domain", "retail", "--agent-llm", "model"]


class FakeBenchmark:
    def __init__(self, exit_code: int = 0, raises=None) -> None:
        self.exit_code = exit_code
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, command, cwd, env):
        self.calls.append({"command": command, "cwd": cwd, "env": env})
        if self.raises:
            raise self.raises
        return self.exit_code


@pytest.fixture
def fake_git(monkeypatch):
    def git(*args):
        if args[0] == "rev-parse" and args[1] == "HEAD":
            return "abc123\n"
        if args[0] == "rev-parse":
            return "main\n"
        if "--name-only" in args:
            return "src/tau2/config.py\n"
        if args[0] == "diff":
            return "diff --git a/x b/x\n"
        return ""

    monkeypatch.setattr(record_run, "_git", git)


def run(tmp_path, *argv, benchmark=None):
    benchmark = benchmark or FakeBenchmark()
    code = record(list(argv), tmp_path, Designs(), benchmark)
    return code, benchmark


def meta_of(tmp_path, name):
    return json.loads((tmp_path / name / runmeta.META_FILE).read_text(encoding="utf-8"))


def test_records_everything_requested(tmp_path, fake_git):
    code, benchmark = run(tmp_path, *BASE, "--name", "my-run", *TAU2)
    assert code == 0
    session = meta_of(tmp_path, "my-run")["sessions"][0]
    assert session["note"] == "first try"
    assert session["design_version"] == "v0-baseline"
    assert session["exit_code"] == 0
    assert session["started_at"] and session["finished_at"]
    assert session["command"] == benchmark.calls[0]["command"]
    assert session["git"]["commit"] == "abc123"
    assert session["git"]["branch"] == "main"
    assert session["git"]["changed_files"] == ["src/tau2/config.py"]
    assert "nl" in session["judges"]
    diff = (tmp_path / "my-run" / runmeta.DIFF_FILE).read_text(encoding="utf-8")
    assert diff.startswith("diff --git")


def test_only_the_expected_files_are_written(tmp_path, fake_git):
    run(tmp_path, *BASE, "--name", "my-run", *TAU2)
    assert {p.name for p in (tmp_path / "my-run").iterdir()} == {
        runmeta.DIFF_FILE,
        runmeta.META_FILE,
        runmeta.HEARTBEAT_FILE,
    }


def test_the_command_is_uv_run_tau2_with_the_flags_that_keep_raw_calls(
    tmp_path, fake_git
):
    _, benchmark = run(tmp_path, *BASE, "--name", "my-run", *TAU2)
    command = benchmark.calls[0]["command"]
    assert command[:4] == ["uv", "run", "tau2", "run"]
    assert command[4:8] == ["--domain", "retail", "--agent-llm", "model"]
    assert option_value(command, "--save-to") == "my-run"
    assert "--verbose-logs" in command
    assert option_value(command, "--llm-log-mode") == "all"


FDE = ["--design", "v1-fde-agent", "--note", "smoke"]
FDE_START = ["uv", "run", "python", "-m", "fde_agent.run", "run"]
FDE_TAU2 = ["--", "--domain", "retail", "--agent", "fde_agent", "--agent-llm", "model"]


def test_fde_agent_design_starts_its_own_runner_with_the_same_flags(tmp_path, fake_git):
    _, benchmark = run(tmp_path, *FDE, "--name", "fde-run", *FDE_TAU2)
    command = benchmark.calls[0]["command"]
    assert command[:6] == FDE_START
    assert command[6:12] == FDE_TAU2[1:]
    assert option_value(command, "--save-to") == "fde-run"
    assert "--verbose-logs" in command
    assert option_value(command, "--llm-log-mode") == "all"
    assert benchmark.calls[0]["cwd"] == record_run.REPO_ROOT
    assert benchmark.calls[0]["env"]["UV_FROZEN"] == "1"


def test_fde_agent_run_records_its_design_command_and_heartbeat(tmp_path, fake_git):
    run(tmp_path, *FDE, "--name", "fde-run", *FDE_TAU2)
    session = meta_of(tmp_path, "fde-run")["sessions"][0]
    assert session["design_version"] == "v1-fde-agent"
    assert session["command"][:6] == FDE_START
    assert "fde_agent.run run" in session["command_line"]
    assert {p.name for p in (tmp_path / "fde-run").iterdir()} == {
        runmeta.DIFF_FILE,
        runmeta.META_FILE,
        runmeta.HEARTBEAT_FILE,
    }


def test_fde_agent_design_still_needs_its_agent(tmp_path, fake_git):
    message = refused(tmp_path, *FDE, *TAU2)
    assert "is for fde_agent, not llm_agent" in message


def test_the_entry_point_comes_from_the_design_not_the_agent_name(tmp_path, fake_git):
    """A design for some other agent can name any command, and that one is used."""
    designs = Designs(tmp_path / "designs")
    (tmp_path / "designs").mkdir()
    source = (Designs().directory / "v0-baseline.yaml").read_text(encoding="utf-8")
    text = source.replace("version: v0-baseline", "version: v9-other")
    text = text.replace(
        "agent_implementation: llm_agent",
        "agent_implementation: my_agent\nentry_point: [python, my_runner.py]",
    )
    (tmp_path / "designs" / "v9-other.yaml").write_text(text, encoding="utf-8")
    benchmark = FakeBenchmark()
    record(
        [
            "--design",
            "v9-other",
            "--note",
            "x",
            "--name",
            "r",
            "--",
            "--agent",
            "my_agent",
        ],
        tmp_path / "sims",
        designs,
        benchmark,
    )
    assert benchmark.calls[0]["command"][:4] == [
        "python",
        "my_runner.py",
        "--agent",
        "my_agent",
    ]


def test_build_command_defaults_to_tau2():
    assert build_command([], "r", False)[:4] == ["uv", "run", "tau2", "run"]
    assert build_command([], "r", False, ["x", "y"])[:2] == ["x", "y"]


def test_root_lock_is_protected(tmp_path, fake_git, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "somewhere")
    _, benchmark = run(tmp_path, *BASE, *TAU2)
    environment = benchmark.calls[0]["env"]
    assert environment["UV_FROZEN"] == "1"
    assert "VIRTUAL_ENV" not in environment


def test_runs_from_the_repo_root(tmp_path, fake_git):
    _, benchmark = run(tmp_path, *BASE, *TAU2)
    assert benchmark.calls[0]["cwd"] == record_run.REPO_ROOT


def test_default_name_uses_date_and_design(tmp_path, fake_git):
    run(tmp_path, *BASE, *TAU2)
    names = [p.name for p in tmp_path.iterdir()]
    assert len(names) == 1
    assert names[0].endswith("_v0-baseline")


def test_exit_code_is_passed_on_and_recorded(tmp_path, fake_git):
    code, _ = run(tmp_path, *BASE, "--name", "r", *TAU2, benchmark=FakeBenchmark(3))
    assert code == 3
    assert meta_of(tmp_path, "r")["sessions"][0]["exit_code"] == 3


def test_ctrl_c_is_recorded_as_finished(tmp_path, fake_git):
    benchmark = FakeBenchmark(raises=KeyboardInterrupt())
    code, _ = run(tmp_path, *BASE, "--name", "r", *TAU2, benchmark=benchmark)
    assert code == 130
    session = meta_of(tmp_path, "r")["sessions"][0]
    assert session["exit_code"] == 130
    assert session["finished_at"]


def test_crash_is_still_recorded(tmp_path, fake_git):
    benchmark = FakeBenchmark(raises=FileNotFoundError("uv is missing"))
    with pytest.raises(FileNotFoundError):
        run(tmp_path, *BASE, "--name", "r", *TAU2, benchmark=benchmark)
    assert meta_of(tmp_path, "r")["sessions"][0]["finished_at"]


def refused(tmp_path, *argv) -> str:
    with pytest.raises(RecordError) as error:
        run(tmp_path, *argv)
    return str(error.value)


def test_unknown_design_is_refused_and_lists_the_real_ones(tmp_path, fake_git):
    message = refused(tmp_path, "--design", "nope", "--note", "x", *TAU2)
    assert "nope" in message
    assert "v0-baseline" in message


def test_design_for_another_agent_is_refused(tmp_path, fake_git):
    message = refused(tmp_path, *BASE, "--", "--agent", "my_agent")
    assert "is for llm_agent" in message


def test_refused_runs_leave_no_folder(tmp_path, fake_git):
    refused(tmp_path, "--design", "nope", "--note", "x", "--name", "r", *TAU2)
    refused(tmp_path, *BASE, "--name", "bad name", *TAU2)
    refused(tmp_path, *BASE, "--name", "r", "--", "--agent", "my_agent")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["bad name", "a/b", "..\\x", "x:y"])
def test_unsafe_names_are_refused(tmp_path, fake_git, name):
    refused(tmp_path, *BASE, "--name", name, *TAU2)


def test_conflicting_names_are_refused(tmp_path, fake_git):
    refused(tmp_path, *BASE, "--name", "a", "--", "--save-to", "b")


def test_save_to_is_honoured_as_the_name(tmp_path, fake_git):
    run(tmp_path, *BASE, "--", "--save-to", "given")
    assert meta_of(tmp_path, "given")["run"] == "given"


def test_existing_run_is_refused_without_resume(tmp_path, fake_git):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / RESULTS_FILE).write_text("{}", encoding="utf-8")
    assert "--resume" in refused(tmp_path, *BASE, "--name", "r", *TAU2)


def test_resume_adds_a_session_and_auto_resume(tmp_path, fake_git):
    run(tmp_path, *BASE, "--name", "r", *TAU2)
    (tmp_path / "r" / RESULTS_FILE).write_text("{}", encoding="utf-8")
    _, benchmark = run(tmp_path, *BASE, "--name", "r", "--resume", *TAU2)
    assert "--auto-resume" in benchmark.calls[0]["command"]
    assert len(meta_of(tmp_path, "r")["sessions"]) == 2


def test_resume_cannot_change_design(tmp_path, fake_git, monkeypatch):
    run(tmp_path, *BASE, "--name", "r", *TAU2)
    (tmp_path / "r" / RESULTS_FILE).write_text("{}", encoding="utf-8")
    meta = meta_of(tmp_path, "r")
    meta["sessions"][0]["design_version"] = "v-old"
    (tmp_path / "r" / runmeta.META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    message = refused(tmp_path, *BASE, "--name", "r", "--resume", *TAU2)
    assert "cannot change design" in message


def test_latest_log_mode_is_refused():
    with pytest.raises(RecordError):
        build_command(["--llm-log-mode", "latest"], "r", False)


def test_flags_already_given_are_not_doubled():
    command = build_command(
        ["--save-to", "r", "--verbose-logs", "--llm-log-mode=all"], "r", False
    )
    assert command.count("--verbose-logs") == 1
    assert sum(arg.startswith("--llm-log-mode") for arg in command) == 1
    assert command.count("--save-to") == 1


def test_arguments_split_at_the_double_dash():
    options, passed = parse_arguments([*BASE, "--", "--note", "for tau2"])
    assert options.note == "first try"
    assert passed == ["--note", "for tau2"]


def test_note_is_required():
    with pytest.raises(SystemExit):
        parse_arguments(["--design", "v0-baseline"])


def test_option_value_forms():
    assert option_value(["--a", "1"], "--a") == "1"
    assert option_value(["--a=1"], "--a") == "1"
    assert option_value(["--b", "1"], "--a") is None


def test_untracked_agent_files_are_captured(monkeypatch, tmp_path):
    """git diff skips new files, which is exactly where a new agent would be."""
    new_file = record_run.REPO_ROOT / "trace_viewer" / ".test_new_agent.py"

    def git(*args):
        return f"{new_file.name}\n" if args[0] == "ls-files" else ""

    monkeypatch.setattr(record_run, "_git", git)
    monkeypatch.setattr(record_run, "REPO_ROOT", new_file.parent)
    new_file.write_text("class MyAgent: ...\n", encoding="utf-8")
    try:
        info, diff = capture_code(["agents"])
    finally:
        new_file.unlink()
    assert info["untracked_files"] == [new_file.name]
    assert "Untracked file" in diff
    assert "class MyAgent" in diff


def test_git_is_really_callable_here():
    commit = record_run._git("rev-parse", "HEAD").strip()
    assert len(commit) == 40


def test_real_diff_covers_the_local_judge_edits():
    design = Designs().get("v0-baseline")
    info, _ = capture_code(design["code_paths"])
    assert info["commit"]
    assert isinstance(info["changed_files"], list)


def test_judges_come_from_the_config_file(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        'DEFAULT_LLM_NL_ASSERTIONS = "nl-model"\n'
        'DEFAULT_SEMANTIC_COMMUNICATE_MODEL = "other"\n',
        encoding="utf-8",
    )
    assert read_judges(config) == {"nl": "nl-model", "communicate": "other"}


def test_judges_read_from_the_real_config():
    judges = read_judges()
    assert judges["nl"]
    assert judges["communicate"]


def test_missing_config_gives_empty_judges(tmp_path):
    assert read_judges(tmp_path / "none.py")["nl"] is None


def test_heartbeat_keeps_the_file_fresh(tmp_path):
    path = tmp_path / "beat"
    with Heartbeat(path, interval=0.02):
        assert path.exists()
        first = path.stat().st_mtime_ns
        time.sleep(0.2)
    assert path.stat().st_mtime_ns != first


def test_environment_keeps_the_rest_of_the_shell(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "value")
    assert child_environment()["SOME_KEY"] == "value"


def test_cmd_file_passes_arguments_through():
    text = Path(record_run.__file__).with_suffix(".cmd").read_text(encoding="utf-8")
    assert "%*" in text
    assert "--frozen" in text
