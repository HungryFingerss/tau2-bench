"""Build the JSON behind each screen. Everything here only reads."""

from pathlib import Path
from typing import Any

from viewer import agent_log, dbdiff, runmeta
from viewer.artifacts import (
    AGENT_LOG_FILE,
    AGENT_LOG_TEMP_PREFIX,
    ARTIFACTS_DIR,
    JUDGE_CALLS,
    NO_STATUS,
    SAVE_HINT,
    SIM_PREFIX,
    attempt_dirs,
    count_attempts,
    debug_files,
    describe_attempt,
    describe_call,
    find_nl_judge_model,
    find_sim_dir,
    has_agent_log,
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
    benchmark_steps,
    db_state,
    message_counts,
    model_calls_in,
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


def _log_attempt(
    run_dir: Path, task_id: str, sim: dict | None, requested: str | None
) -> Path | None:
    """The attempt whose agent log the page shows.

    A requested attempt, else the one in results.json, else the newest attempt
    with a log: one still running before one that ended.
    """
    if requested:
        sim_dir = find_sim_dir(run_dir, task_id, requested)
        if sim_dir is None:
            raise NotFound(f"Attempt {requested} of task {task_id} does not exist.")
        return sim_dir
    if sim is not None:
        sim_dir = find_sim_dir(run_dir, task_id, sim.get("id"))
        return sim_dir if sim_dir and has_agent_log(sim_dir) else None
    candidates = [d for d in attempt_dirs(run_dir, task_id) if has_agent_log(d)]
    if not candidates:
        return None
    described = {d: describe_attempt(d, set()) for d in candidates}
    return max(
        candidates,
        key=lambda d: (
            described[d]["status"] == NO_STATUS,
            described[d]["started_at"] or "",
        ),
    )


def _run_is_alive(run_dir: Path) -> bool:
    scan = runmeta.scan_artifacts(run_dir)
    state = runmeta.run_state(
        run_dir, runmeta.read_meta(run_dir), None, None, scan.newest_age_seconds
    )
    return state in (runmeta.RUNNING, runmeta.MAYBE_RUNNING)


def agent_conversation(
    store: Store, run_dir: Path, sim_dir: Path, sim: dict | None
) -> dict[str, Any]:
    """One attempt's conversation from fde_agent_log.json, with every internal step."""
    sim_id = sim_dir.name.removeprefix(SIM_PREFIX)
    in_results = sim is not None and sim.get("id") == sim_id
    status = read_status(sim_dir)
    read = store.read_settled(sim_dir / AGENT_LOG_FILE, AGENT_LOG_TEMP_PREFIX)
    base = {
        "sim_id": sim_id,
        "in_results": in_results,
        "status": (status or {}).get("status", NO_STATUS),
        "reason": (status or {}).get("reason"),
        "error": (status or {}).get("error"),
        "live": status is None and _run_is_alive(run_dir),
        "stale": read.stale,
        "problem": read.problem,
    }
    if read.data is None:
        return {**base, "available": False, "steps": [], "calls": []}
    messages = (sim or {}).get("messages") if in_results else None
    return {
        **base,
        "available": True,
        **agent_log.build(read.data, read_calls(sim_dir), messages),
    }


def task_counts(
    store: Store, run_dir: Path, task_id: str, sim: dict | None, fallback: bool
) -> dict[str, Any] | None:
    """What one task's conversation took: model calls, benchmark steps, checks.

    From the agent's log when there is one, else from results.json alone, which
    has no checks or internal steps. `fallback` allows the newest attempt with a
    log when results.json has no simulation (only safe with one trial per task).
    """
    log_dir = _log_attempt(run_dir, task_id, sim, None) if sim or fallback else None
    if log_dir is not None:
        log = store.read_settled(log_dir / AGENT_LOG_FILE, AGENT_LOG_TEMP_PREFIX).data
        if log:
            sim_id = log_dir.name.removeprefix(SIM_PREFIX)
            return {
                "source": "agent_log",
                "sim_id": sim_id,
                "in_results": bool(sim) and sim.get("id") == sim_id,
                **agent_log.counts(log.get("events") or []),
            }
    if sim is None:
        return None
    messages = sim.get("messages") or []
    return {
        "source": "results",
        "sim_id": sim.get("id"),
        "in_results": True,
        "model_calls": model_calls_in(messages),
        "benchmark_steps": benchmark_steps(messages),
        "store_calls": sum(len(m.get("tool_calls") or []) for m in messages),
    }


def task_page(
    store: Store, name: str, task_id: str, trial: int, attempt: str | None = None
) -> dict[str, Any]:
    loaded = _load(store, name)
    run_dir = store.run_dir(name)
    data = loaded.data
    task = _find_task(data, task_id)
    sim = _find_sim(data, task_id, trial)
    rows = _build_rows(run_dir, data, with_attempts=False)
    if (task_id, trial) not in [(r["task_id"], r["trial"]) for r in rows]:
        raise NotFound(f"Task {task_id} has no trial {trial}.")
    log_dir = _log_attempt(run_dir, task_id, sim, attempt)
    conversation = agent_conversation(store, run_dir, log_dir, sim) if log_dir else None
    attempts = list_attempts(run_dir, task_id, _ids(data))
    # Redraw as data arrives when the conversation is live, or when nothing is shown
    # yet but an attempt is running: then there is no reader to disturb.
    live = bool(conversation and conversation["live"]) or (
        sim is None
        and not (conversation or {}).get("available")
        and any(a["status"] == NO_STATUS for a in attempts)
        and _run_is_alive(run_dir)
    )

    page: dict[str, Any] = {
        "run": name,
        "problem": loaded.problem,
        "task_id": task_id,
        "trial": trial,
        "num_trials": (data.get("info") or {}).get("num_trials") or 1,
        "task": _task_view(task, sim),
        "task_raw": task,
        "neighbours": _neighbours(rows, task_id, trial),
        "attempts": attempts,
        "links_ids": uses_canonical_retail_db(data.get("info") or {}),
        "conversation": conversation,
        "live": live,
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
        "raw_call_note": (
            _log_call_note(conversation)
            if conversation and conversation["in_results"]
            else _raw_call_note(
                find_sim_dir(run_dir, task_id, sim.get("id")), model_steps
            )
        ),
        "timeline": steps,
    }


def _log_call_note(conversation: dict) -> str | None:
    """For an agent log, the model calls it lists that have no raw file."""
    missing = len(conversation.get("missing_calls") or [])
    if not missing:
        return None
    total = (conversation.get("counts") or {}).get("model_calls")
    return f"{missing} of {total} model calls in the agent's log have no raw call file. {SAVE_HINT}"


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
    reason = None
    if call is None:
        reason = _written_without_a_model(store, sim_dir, message)
        reason = reason or missing_call_reason(message, sim_dir, len(calls))
    return {
        "index": index,
        "role": message.get("role"),
        "call": call,
        "missing_reason": reason,
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


WRITTEN_BY_CODE = (
    "Written by the agent's code, not by a model, so no model call was made. "
    "The agent's log (fde_agent_log.json) marks it by: code."
)
STORE_CALLS_BY_CODE = (
    "These store calls were sent by one of the agent's own tools, not chosen by a "
    "model directly. The model call that picked the tool is on the agent's step above."
)


def _written_without_a_model(
    store: Store, sim_dir: Path | None, message: dict
) -> str | None:
    """For an agent with its own log, say when code wrote a message."""
    if sim_dir is None or message.get("role") != "assistant":
        return None
    if not has_agent_log(sim_dir):
        return None
    log = store.read_settled(sim_dir / AGENT_LOG_FILE, AGENT_LOG_TEMP_PREFIX).data
    if not log:
        return None
    if message.get("tool_calls"):
        return STORE_CALLS_BY_CODE
    writer = agent_log.writer_of(log, message.get("content"))
    if writer == "code":
        return WRITTEN_BY_CODE
    if writer == "benchmark":
        return "This line is scripted by the benchmark, so no model call was made."
    if writer == "model":
        return (
            "Written by the model, but no saved call has this message's exact "
            f"generation_time_seconds. {SAVE_HINT}"
        )
    return None


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
    sim = next(
        (s for s in data.get("simulations") or [] if s.get("id") == sim_id), None
    )
    return {
        "attempt": describe_attempt(sim_dir, _ids(data)),
        "status_file": read_status(sim_dir),
        "calls": [describe_call(call) for call in calls],
        "conversation": rebuild_conversation(calls),
        "agent_log": (
            agent_conversation(store, store.run_dir(name), sim_dir, sim)
            if has_agent_log(sim_dir)
            else None
        ),
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
