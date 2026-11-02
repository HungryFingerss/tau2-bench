"""Start a benchmark run and save what is needed to understand it later.

Usage, from anywhere:

    .\\trace_viewer\\record_run.cmd --design v0-baseline --note "baseline" --name my-run
        -- --domain retail --agent-llm ollama_chat/gpt-oss:20b

Everything after `--` goes to `tau2 run`. Next to the results this saves:

    run_meta.json        the exact command, date, note, design, commit, judge models
    agent_code.diff      uncommitted changes to the design's code_paths
    run_meta.heartbeat   kept fresh while running, so the viewer can tell it is alive

These are the only files written to data/simulations.
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from viewer import runmeta
from viewer.designs import DesignError, Designs, UnknownDesign, source_status
from viewer.store import DEFAULT_SIMULATIONS_DIR, REPO_ROOT, has_results

SAFE_NAME = re.compile(r"^[\w.-]+$")
DEFAULT_AGENT = "llm_agent"
CONFIG_FILE = REPO_ROOT / "src" / "tau2" / "config.py"
JUDGE_SETTINGS = {
    "nl": "DEFAULT_LLM_NL_ASSERTIONS",
    "communicate": "DEFAULT_SEMANTIC_COMMUNICATE_MODEL",
}


class RecordError(Exception):
    """A problem to report to the user before anything has started."""


def parse_arguments(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    if "--" in argv:
        split = argv.index("--")
        ours, tau2_args = argv[:split], argv[split + 1 :]
    else:
        ours, tau2_args = argv, []
    parser = argparse.ArgumentParser(description="Start a recorded benchmark run.")
    parser.add_argument("--design", required=True, help="A file in designs/.")
    parser.add_argument("--note", required=True, help="Why this run exists.")
    parser.add_argument("--name", help="Run folder name. Defaults to date and design.")
    parser.add_argument("--resume", action="store_true", help="Continue this run.")
    return parser.parse_args(ours), tau2_args


def option_value(args: list[str], *names: str) -> str | None:
    """The value of a command-line option, in either --x y or --x=y form."""
    for index, arg in enumerate(args):
        if arg in names and index + 1 < len(args):
            return args[index + 1]
        for name in names:
            if arg.startswith(f"{name}="):
                return arg.split("=", 1)[1]
    return None


def has_option(args: list[str], *names: str) -> bool:
    return any(
        arg in names or arg.startswith(tuple(f"{n}=" for n in names)) for arg in args
    )


def load_design(version: str, designs: Designs) -> dict:
    try:
        return designs.get(version)
    except UnknownDesign:
        known = ", ".join(sorted(designs.all())) or "none"
        raise RecordError(f"Unknown design {version}. Available: {known}.") from None
    except DesignError as error:
        raise RecordError(str(error)) from None


def choose_name(options: argparse.Namespace, tau2_args: list[str]) -> str:
    given = option_value(tau2_args, "--save-to", "--output")
    if options.name and given and options.name != given:
        raise RecordError(f"--name {options.name} conflicts with --save-to {given}.")
    name = options.name or given
    if name is None:
        name = f"{datetime.now():%Y%m%d_%H%M%S}_{options.design}"
    if not SAFE_NAME.match(name):
        raise RecordError(f"Run names can use letters, digits, ., - and _: {name}")
    return name


def build_command(tau2_args: list[str], name: str, resume: bool) -> list[str]:
    """`uv run tau2 run`, with the flags that make a run fully inspectable."""
    args = list(tau2_args)
    if not has_option(args, "--save-to", "--output"):
        args += ["--save-to", name]
    if not has_option(args, "--verbose-logs"):
        args += ["--verbose-logs"]
    if option_value(args, "--llm-log-mode") not in (None, "all"):
        raise RecordError("--llm-log-mode must be all, or raw calls are lost.")
    if not has_option(args, "--llm-log-mode"):
        args += ["--llm-log-mode", "all"]
    if resume and not has_option(args, "--auto-resume"):
        args += ["--auto-resume"]
    return ["uv", "run", "tau2", "run", *args]


def _git(*args: str) -> str:
    finished = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return finished.stdout if finished.returncode == 0 else ""


def capture_code(code_paths: list[str]) -> tuple[dict, str]:
    """The commit, plus changes it does not show, including new files."""
    diff = _git("diff", "HEAD", "--", *code_paths)
    changed = _git("diff", "HEAD", "--name-only", "--", *code_paths).splitlines()
    untracked = _git(
        "ls-files", "--others", "--exclude-standard", "--", *code_paths
    ).splitlines()

    sections = [diff]
    for file in untracked:
        try:
            text = (REPO_ROOT / file).read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            text = f"(could not read: {error})\n"
        sections.append(f"# Untracked file, not in git: {file}\n{text}")

    info = {
        "commit": _git("rev-parse", "HEAD").strip(),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD").strip(),
        "code_paths": code_paths,
        "changed_files": changed,
        "untracked_files": untracked,
        "diff_file": runmeta.DIFF_FILE,
    }
    return info, "\n".join(section for section in sections if section)


def read_judges(path: Path = CONFIG_FILE) -> dict[str, str | None]:
    """Judge models from config.py. results.json does not record the NL judge."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {key: None for key in JUDGE_SETTINGS}
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value.value
    return {key: values.get(name) for key, name in JUDGE_SETTINGS.items()}


def write_json(path: Path, data: dict) -> None:
    """Replace the file in one step. The viewer may be reading it, so retry briefly."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for attempt in range(40):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(temporary, path)


class Heartbeat:
    """Touch a file while the run lives, so a crash is distinguishable from a wait."""

    def __init__(self, path: Path, interval: float = runmeta.HEARTBEAT_SECONDS) -> None:
        self.path = path
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True)

    def _beat(self) -> None:
        while not self._stop.wait(self.interval):
            self.path.touch()

    def __enter__(self) -> "Heartbeat":
        self.path.touch()
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def child_environment() -> dict[str, str]:
    """UV_FROZEN keeps the root uv.lock untouched. See the root AGENTS.md."""
    environment = {**os.environ, "UV_FROZEN": "1"}
    environment.pop("VIRTUAL_ENV", None)
    return environment


def record(
    argv: list[str],
    simulations_dir: Path = DEFAULT_SIMULATIONS_DIR,
    designs: Designs | None = None,
    run_command=subprocess.call,
) -> int:
    options, tau2_args = parse_arguments(argv)
    designs = designs or Designs()
    design = load_design(options.design, designs)

    agent = option_value(tau2_args, "--agent") or DEFAULT_AGENT
    expected = design["applies_to"]["agent_implementation"]
    if agent != expected:
        raise RecordError(f"Design {options.design} is for {expected}, not {agent}.")

    name = choose_name(options, tau2_args)
    run_dir = simulations_dir / name
    exists = run_dir.is_dir() and has_results(run_dir)
    if exists and not options.resume:
        raise RecordError(f"Run {name} already exists. Pass --resume to continue it.")

    meta = runmeta.read_meta(run_dir) or {"run": name, "sessions": []}
    earlier = runmeta.latest_session(meta)
    if options.resume and earlier and earlier["design_version"] != options.design:
        raise RecordError(
            f"Run {name} used design {earlier['design_version']}. "
            "A run cannot change design partway."
        )

    stale = source_status(design)
    for file in stale["changed"] + stale["missing"]:
        print(f"Warning: {file} changed since {options.design} was documented.")

    command = build_command(tau2_args, name, options.resume)
    git, diff = capture_code(design.get("code_paths") or [])
    session = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "exit_code": None,
        "note": options.note,
        "design_version": options.design,
        "command": command,
        "command_line": subprocess.list2cmdline(command),
        "git": git,
        "judges": read_judges(),
    }
    run_dir.mkdir(parents=True, exist_ok=True)  # Only once nothing can be refused.
    (run_dir / runmeta.DIFF_FILE).write_text(diff, encoding="utf-8")
    meta["sessions"].append(session)
    write_json(run_dir / runmeta.META_FILE, meta)

    print(f"Recording run {name}: {session['command_line']}")
    exit_code = 1
    try:
        with Heartbeat(run_dir / runmeta.HEARTBEAT_FILE):
            exit_code = run_command(command, cwd=REPO_ROOT, env=child_environment())
    except KeyboardInterrupt:
        exit_code = 130
    finally:
        session["finished_at"] = datetime.now().isoformat(timespec="seconds")
        session["exit_code"] = exit_code
        write_json(run_dir / runmeta.META_FILE, meta)
    return exit_code


def main() -> int:
    try:
        return record(sys.argv[1:])
    except RecordError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
