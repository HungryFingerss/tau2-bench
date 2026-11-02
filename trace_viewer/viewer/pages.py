"""Build the JSON behind each screen. Everything here only reads."""

from pathlib import Path
from typing import Any

from viewer import dbdiff
from viewer.artifacts import (
    ARTIFACTS_DIR,
    JUDGE_CALLS,
    SAVE_HINT,
    count_attempts,
    debug_files,
    describe_attempt,
    describe_call,
    find_nl_judge_model,
    find_sim_dir,
    join_steps,
    list_attempts,
    missing_call_reason,
    read_calls,
    read_log,
    read_status,
    rebuild_conversation,
)
from viewer.replay import Replayer, unavailable_reason, uses_canonical_retail_db
from viewer.store import LoadedRun, Store, has_results
from viewer.timeline import build_timeline, first_error_step
from viewer.verdicts import (
    FAILURE_RESULTS,
    NONE_DEFINED,
    NOT_EVALUATED,
    PENDING,
    db_state,
    message_counts,
    nl_state,
    pass_rate,
    result_state,
    task_sort_key,
    why,
)


class NotFound(Exception):
    """A task, simulation, step or file does not exist in this run."""


def _load(store: Store, name: str) -> LoadedRun:
    loaded = store.load(name)
    if loaded.data is None:
        raise NotFound(loaded.problem or f"Run {name} has no results.")
    return loaded


def _unsupported_reason(run_dir: Path, info: dict | None = None) -> str | None:
    if (run_dir / "simulations").is_dir() or (info or {}).get("audio_native_config"):
        return "Voice run. This viewer supports text runs only."
    if not has_results(run_dir) and any(
        has_results(entry) for entry in run_dir.iterdir() if entry.is_dir()
    ):
        return "Benchmark folder with one run per language and domain. Not supported."
    return None


def _ids(data: dict) -> set[str]:
    return {sim.get("id") for sim in data.get("simulations") or []}


def _reward_bases(data: dict) -> set[str]:
    return {
        basis
        for task in data.get("tasks") or []
        for basis in (task.get("evaluation_criteria") or {}).get("reward_basis") or []
    }


def _split(info: dict) -> str | None:
    for context in (info.get("language_tasks") or {}).values():
        return (context.get("task_selection") or {}).get("task_split_name")
    return None


def _build_rows(
    run_dir: Path, data: dict, *, with_attempts: bool = True
) -> list[dict[str, Any]]:
    """One row per task and trial, including tasks with no simulation yet."""
    trials = range((data.get("info") or {}).get("num_trials") or 1)
    keys = {(task.get("id"), n) for task in data.get("tasks") or [] for n in trials}
    sims = {}
    for sim in data.get("simulations") or []:
        key = (sim.get("task_id"), sim.get("trial") or 0)
        sims[key] = sim
        keys.add(key)

    current_ids = _ids(data)
    rows = []
    for task_id, trial in sorted(keys, key=lambda k: (task_sort_key(k[0]), k[1])):
        sim = sims.get((task_id, trial))
        attempts = (
            count_attempts(list_attempts(run_dir, task_id, current_ids))
            if with_attempts
            else None
        )
        row: dict[str, Any] = {"task_id": task_id, "trial": trial, "attempts": attempts}
        if sim is None:
            row.update(sim_id=None, result=PENDING)
        else:
            counts = message_counts(sim)
            row.update(
                counts,
                sim_id=sim.get("id"),
                result=result_state(sim),
                db=db_state(sim),
                nl=nl_state(sim),
                reward=(sim.get("reward_info") or {}).get("reward"),
                termination_reason=sim.get("termination_reason"),
                duration=sim.get("duration"),
                why=why(sim, counts),
            )
        rows.append(row)
    return rows


def _build_summary(store: Store, name: str) -> dict[str, Any]:
    run_dir = store.run_dir(name)
    summary: dict[str, Any] = {"name": name, "status": "ok", "problem": None}

    unsupported = _unsupported_reason(run_dir)
    if unsupported:
        return {**summary, "status": "unsupported", "problem": unsupported}

    loaded = store.load(name)
    if loaded.data is None:
        status = "unreadable" if loaded.signature else "no_results"
        return {**summary, "status": status, "problem": loaded.problem}

    data, info = loaded.data, loaded.data.get("info") or {}
    unsupported = _unsupported_reason(run_dir, info)
    if unsupported:
        return {**summary, "status": "unsupported", "problem": unsupported}

    tasks = sorted((t.get("id") for t in data.get("tasks") or []), key=task_sort_key)
    sims = data.get("simulations") or []
    num_trials = info.get("num_trials") or 1
    communicate = info.get("communicate_scoring") or {}
    return {
        **summary,
        "problem": loaded.problem,
        "date": data.get("timestamp"),
        "domain": (info.get("environment_info") or {}).get("domain_name"),
        "agent": info.get("agent_info"),
        "customer": info.get("user_info"),
        "judges": {
            "nl": find_nl_judge_model(run_dir),
            "communicate": (communicate.get("judge") or {}).get("model"),
            "communicate_scored": "COMMUNICATE" in _reward_bases(data),
        },
        "split": _split(info),
        "task_ids": tasks,
        "num_trials": num_trials,
        "expected_simulations": len(tasks) * num_trials,
        "done_simulations": len(sims),
        "counts": pass_rate([result_state(sim) for sim in sims]),
        "has_artifacts": (run_dir / ARTIFACTS_DIR).is_dir(),
    }


def run_summary(store: Store, name: str) -> dict[str, Any]:
    return store.memo(name, "summary", lambda: _build_summary(store, name))


def runs_page(store: Store) -> dict[str, Any]:
    runs = [run_summary(store, name) for name in store.run_names()]
    runs.sort(key=lambda run: run.get("date") or "", reverse=True)
    return {"runs": runs, "simulations_dir": str(store.simulations_dir)}


def run_page(store: Store, name: str) -> dict[str, Any]:
    summary = run_summary(store, name)
    if summary["status"] != "ok":
        return {"summary": summary, "rows": [], "config": {}}
    loaded = _load(store, name)
    info = loaded.data.get("info") or {}
    return {
        "summary": summary,
        "rows": _build_rows(store.run_dir(name), loaded.data),
        "config": {
            "seed": info.get("seed"),
            "git_commit": info.get("git_commit"),
            "max_steps": info.get("max_steps"),
            "max_errors": info.get("max_errors"),
            "policy": (info.get("environment_info") or {}).get("policy"),
        },
    }


def _find_task(data: dict, task_id: str) -> dict:
    for task in data.get("tasks") or []:
        if task.get("id") == task_id:
            return task
    raise NotFound(f"Task {task_id} is not in this run.")


def _find_sim(data: dict, task_id: str, trial: int) -> dict | None:
    for sim in data.get("simulations") or []:
        if sim.get("task_id") == task_id and (sim.get("trial") or 0) == trial:
            return sim
    return None


def _find_sim_by_id(data: dict, sim_id: str) -> dict:
    for sim in data.get("simulations") or []:
        if sim.get("id") == sim_id:
            return sim
    raise NotFound(f"Simulation {sim_id} is not in this run.")


def _task_view(task: dict, sim: dict | None) -> dict[str, Any]:
    """The task as it was run. results.json holds the runtime version."""
    scenario = task.get("user_scenario") or {}
    instructions = scenario.get("instructions") or {}
    if isinstance(instructions, str):
        instructions = {"reason_for_call": instructions}
    criteria = task.get("evaluation_criteria") or {}
    checks = {
        check.get("action", {}).get("action_id"): check
        for check in ((sim or {}).get("reward_info") or {}).get("action_checks") or []
    }
    actions = []
    for action in criteria.get("actions") or []:
        check = checks.get(action.get("action_id")) or {}
        actions.append(
            {
                "action_id": action.get("action_id"),
                "name": action.get("name"),
                "arguments": action.get("arguments"),
                "requestor": action.get("requestor"),
                "matched": check.get("action_match"),
                "tool_type": check.get("tool_type"),
            }
        )
    basis = criteria.get("reward_basis") or []
    return {
        "id": task.get("id"),
        "reason_for_call": instructions.get("reason_for_call"),
        "known_info": instructions.get("known_info"),
        "unknown_info": instructions.get("unknown_info"),
        "task_instructions": instructions.get("task_instructions"),
        "persona": scenario.get("persona"),
        "description": task.get("description"),
        "actions": actions,
        "nl_assertions": criteria.get("nl_assertions") or [],
        "communicate_info": criteria.get("communicate_info") or [],
        "communicate_scored": "COMMUNICATE" in basis,
        "reward_basis": basis,
    }


def _raw_call_note(sim_dir: Path | None, model_steps: int) -> str | None:
    """Warn up front when a simulation's raw calls were not fully saved."""
    if model_steps == 0:
        return None
    if sim_dir is None:
        return f"No raw model calls exist for this simulation. {SAVE_HINT}"
    saved = sum(1 for f in debug_files(sim_dir) if f.call_name not in JUDGE_CALLS)
    if saved >= model_steps:
        return None
    return f"Only {saved} of {model_steps} raw model calls were saved. {SAVE_HINT}"


def _neighbours(rows: list[dict], task_id: str, trial: int) -> dict[str, Any]:
    failures = [row for row in rows if row["result"] in FAILURE_RESULTS]
    order = [(row["task_id"], row["trial"]) for row in rows]
    position = order.index((task_id, trial))

    def pick(row: dict | None) -> dict | None:
        return row and {"task_id": row["task_id"], "trial": row["trial"]}

    before = [r for r in failures if order.index((r["task_id"], r["trial"])) < position]
    after = [r for r in failures if order.index((r["task_id"], r["trial"])) > position]
    return {
        "previous": pick(before[-1] if before else None),
        "next": pick(after[0] if after else None),
    }


def task_page(store: Store, name: str, task_id: str, trial: int) -> dict[str, Any]:
    loaded = _load(store, name)
    run_dir = store.run_dir(name)
    data = loaded.data
    task = _find_task(data, task_id)
    sim = _find_sim(data, task_id, trial)
    rows = _build_rows(run_dir, data, with_attempts=False)
    if (task_id, trial) not in [(r["task_id"], r["trial"]) for r in rows]:
        raise NotFound(f"Task {task_id} has no trial {trial}.")

    page: dict[str, Any] = {
        "run": name,
        "problem": loaded.problem,
        "task_id": task_id,
        "trial": trial,
        "num_trials": (data.get("info") or {}).get("num_trials") or 1,
        "task": _task_view(task, sim),
        "task_raw": task,
        "neighbours": _neighbours(rows, task_id, trial),
        "attempts": list_attempts(run_dir, task_id, _ids(data)),
        "links_ids": uses_canonical_retail_db(data.get("info") or {}),
    }
    if sim is None:
        return {**page, "sim": None, "timeline": []}

    messages = sim.get("messages") or []
    steps = build_timeline(messages)
    reward_info = sim.get("reward_info")
    counts = message_counts(sim)
    model_steps = sum(1 for step in steps if step.get("has_model_call"))
    assertions = (reward_info or {}).get("nl_assertions") or []
    return {
        **page,
        "sim": {
            **counts,
            "id": sim.get("id"),
            "seed": sim.get("seed"),
            "start_time": sim.get("start_time"),
            "end_time": sim.get("end_time"),
            "duration": sim.get("duration"),
            "termination_reason": sim.get("termination_reason"),
            "info": sim.get("info"),
            "result": result_state(sim),
            "db": db_state(sim),
            "nl": nl_state(sim),
            "why": why(sim, counts),
            "first_error_step": first_error_step(steps),
            "first_failed_assertion": next(
                (i for i, item in enumerate(assertions) if not item.get("met")), None
            ),
        },
        "reward_info": reward_info,
        "raw_call_note": _raw_call_note(
            find_sim_dir(run_dir, task_id, sim.get("id")), model_steps
        ),
        "timeline": steps,
    }


def step_call(store: Store, name: str, sim_id: str, index: int) -> dict[str, Any]:
    """The raw model call behind one timeline step."""
    data = _load(store, name).data
    sim = _find_sim_by_id(data, sim_id)
    messages = sim.get("messages") or []
    if not 0 <= index < len(messages):
        raise NotFound(f"Simulation {sim_id} has no step {index}.")

    message = messages[index]
    sim_dir = find_sim_dir(store.run_dir(name), sim.get("task_id"), sim_id)
    calls = read_calls(sim_dir) if sim_dir else []
    call = join_steps(messages, calls).get(index)
    return {
        "index": index,
        "role": message.get("role"),
        "call": call,
        "missing_reason": (
            None if call else missing_call_reason(message, sim_dir, len(calls))
        ),
        "results_record": {
            key: message.get(key)
            for key in (
                "timestamp",
                "turn_idx",
                "usage",
                "cost",
                "generation_time_seconds",
                "raw_data",
            )
        },
    }


def db_diff(store: Store, replayer: Replayer, name: str, sim_id: str) -> dict[str, Any]:
    """Expected and agent final databases, from the benchmark's own evaluator."""
    data = _load(store, name).data
    info = data.get("info") or {}
    sim = _find_sim_by_id(data, sim_id)
    reason = unavailable_reason(info, sim)
    if reason:
        return {"available": False, "reason": reason}

    result = replayer.replay(info, _find_task(data, sim.get("task_id")), sim)
    if not result.get("ok"):
        return {"available": False, "failed": True, "reason": result.get("error")}

    records = dbdiff.compare_records(result["records"])
    db_check = (sim.get("reward_info") or {}).get("db_check")
    recorded = db_check.get("db_match") if db_check else None
    return {
        "available": True,
        "db_match": result["db_match"],
        "recorded_db_match": recorded,
        "agrees": recorded is None or recorded == result["db_match"],
        "initial_hash": result["initial_hash"],
        "expected_hash": result["expected_hash"],
        "agent_hash": result["agent_hash"],
        "initial_matches_db_json": result.get("initial_matches_db_json"),
        "notes": result["notes"],
        "records": records,
        "summary": dbdiff.summarize(records),
    }


def judge_calls(store: Store, name: str, sim_id: str) -> dict[str, Any]:
    """The raw judge calls for a simulation."""
    data = _load(store, name).data
    sim = _find_sim_by_id(data, sim_id)
    sim_dir = find_sim_dir(store.run_dir(name), sim.get("task_id"), sim_id)
    calls = [
        call
        for call in (read_calls(sim_dir) if sim_dir else [])
        if call.get("call_name") in JUDGE_CALLS
    ]
    reason = None
    if not calls:
        state = nl_state(sim)
        if state == NONE_DEFINED:
            reason = "This task defines no NL assertions, so no judge was called."
        elif state == NOT_EVALUATED:
            reason = "The simulation stopped before evaluation, so no judge ran."
        else:
            reason = f"The judge's call was not saved. {SAVE_HINT}"
    return {"calls": calls, "missing_reason": reason}


def _attempt_dir(store: Store, name: str, task_id: str, sim_id: str) -> Path:
    sim_dir = find_sim_dir(store.run_dir(name), task_id, sim_id)
    if sim_dir is None:
        raise NotFound(f"Attempt {sim_id} of task {task_id} does not exist.")
    return sim_dir


def attempt_page(store: Store, name: str, task_id: str, sim_id: str) -> dict[str, Any]:
    """Everything saved for one attempt, including ones absent from results.json."""
    sim_dir = _attempt_dir(store, name, task_id, sim_id)
    data = _load(store, name).data
    calls = read_calls(sim_dir)
    return {
        "attempt": describe_attempt(sim_dir, _ids(data)),
        "status_file": read_status(sim_dir),
        "calls": [describe_call(call) for call in calls],
        "conversation": rebuild_conversation(calls),
    }


def attempt_call(
    store: Store, name: str, task_id: str, sim_id: str, file: str
) -> dict[str, Any]:
    sim_dir = _attempt_dir(store, name, task_id, sim_id)
    for call in read_calls(sim_dir):
        if call.get("file") == file:
            return call
    raise NotFound(f"Call {file} does not exist.")


def attempt_log(store: Store, name: str, task_id: str, sim_id: str) -> dict[str, Any]:
    log = read_log(_attempt_dir(store, name, task_id, sim_id))
    if log is None:
        raise NotFound("This attempt has no task.log.")
    return log
