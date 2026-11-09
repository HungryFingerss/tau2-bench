"""Add designs, checks and labels to the run and task pages from viewer/pages.py."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from viewer import agent_log, annotations, checks, compare, designs, pages, runmeta
from viewer.annotations import Annotations, task_key
from viewer.artifacts import find_saved_agent_call
from viewer.designs import Designs
from viewer.store import Store


@dataclass
class Context:
    store: Store
    designs: Designs
    notes: Annotations


def _message_text(content: Any) -> str | None:
    if isinstance(content, list):
        return "\n".join(part for part in content if isinstance(part, str))
    return content


def _saved_call(ctx: Context, name: str) -> dict | None:
    return ctx.store.memo(
        name, "saved_call", lambda: find_saved_agent_call(ctx.store.run_dir(name))
    )


def _agent_implementation(info: dict) -> str | None:
    return (info.get("agent_info") or {}).get("implementation")


def _run_design(ctx: Context, name: str, info: dict, meta: dict | None) -> dict:
    """Which design a run used, and how that is known."""
    session = runmeta.latest_session(meta)
    recorded = (session or {}).get("design_version")
    if recorded:
        try:
            return {"design": ctx.designs.get(recorded), "origin": "recorded"}
        except designs.UnknownDesign:
            return {"design": None, "origin": "missing", "version": recorded}
    design = ctx.designs.for_agent(_agent_implementation(info))
    return {"design": design, "origin": "assumed" if design else "unknown"}


def _tool_schemas(ctx: Context, name: str, design: dict | None) -> dict | None:
    """The schemas of the tool calls in results.json, for the invalid-call check.

    For most agents those are the tools the model was sent. An agent with its own
    tools sends the store's calls itself, so its design names the design whose tool
    list is the store's (`store_tools_from`).
    """
    source = (design or {}).get("store_tools_from")
    if source:
        try:
            return checks.tool_schemas_from_call(ctx.designs.get(source).get("tools"))
        except designs.UnknownDesign:
            return None
    call = _saved_call(ctx, name)
    tools = ((call or {}).get("request") or {}).get("tools")
    return checks.tool_schemas_from_call(tools or (design or {}).get("tools"))


def _load_info(ctx: Context, name: str) -> dict:
    loaded = ctx.store.load(name)
    return ((loaded.data or {}).get("info")) or {}


def _simulations(ctx: Context, name: str) -> dict:
    data = ctx.store.load(name).data or {}
    return {
        (sim.get("task_id"), sim.get("trial") or 0): sim
        for sim in data.get("simulations") or []
    }


def runs_view(ctx: Context) -> dict[str, Any]:
    page = pages.runs_page(ctx.store)
    for run in page["runs"]:
        run_dir = ctx.store.run_dir(run["name"])
        meta = runmeta.read_meta(run_dir)
        run["recorded"] = runmeta.meta_summary(meta)
        run["state"] = runmeta.run_state(
            run_dir, meta, run.get("done_simulations"), run.get("expected_simulations")
        )
        if run.get("judges") and not run["judges"].get("nl"):
            recorded_judges = (run["recorded"] or {}).get("judges") or {}
            run["judges"]["nl"] = recorded_judges.get("nl")
        implementation = (run.get("agent") or {}).get("implementation")
        design = ctx.designs.for_agent(implementation)
        recorded = (run["recorded"] or {}).get("design_version")
        run["design_version"] = recorded or (design or {}).get("version")
        run["design_origin"] = "recorded" if recorded else "assumed" if design else None
    return page


def run_view(ctx: Context, name: str) -> dict[str, Any]:
    page = pages.run_page(ctx.store, name)
    run_dir = ctx.store.run_dir(name)
    meta = runmeta.read_meta(run_dir)
    summary = page["summary"]
    page["recorded"] = runmeta.meta_summary(meta)
    scan = runmeta.scan_artifacts(run_dir)
    page["state"] = runmeta.run_state(
        run_dir,
        meta,
        summary.get("done_simulations"),
        summary.get("expected_simulations"),
        scan.newest_age_seconds,
    )
    if summary["status"] != "ok":
        return page

    info = _load_info(ctx, name)
    found = _run_design(ctx, name, info, meta)
    design = found["design"]
    schemas = _tool_schemas(ctx, name, design)
    sims = _simulations(ctx, name)
    tasks = ctx.notes.read(name)
    one_trial = (info.get("num_trials") or 1) == 1

    for row in page["rows"]:
        sim = sims.get((row["task_id"], row["trial"]))
        if sim is not None:
            results = checks.run_checks(sim.get("messages") or [], schemas)
            row["checks"] = {
                r["id"]: {"state": r["state"], "count": r["count"]} for r in results
            }
        entry = tasks.get(task_key(row["task_id"], row["trial"])) or {}
        row["labels"] = [item["id"] for item in entry.get("labels") or []]
        row["agent"] = pages.task_counts(
            ctx.store, run_dir, row["task_id"], sim, fallback=one_trial
        )
    page["agent_totals"] = _agent_totals(page["rows"])

    labels, labels_problem = _labels(ctx)
    page["check_definitions"] = checks.DEFINITIONS
    page["check_totals"] = checks.totals(page["rows"])
    page["analysis"] = annotations.analysis(page["rows"], tasks, labels, design)
    page["labels_problem"] = labels_problem
    page["design"] = {
        "version": (design or {}).get("version") or found.get("version"),
        "origin": found["origin"],
    }
    return page


def _agent_totals(rows: list[dict]) -> dict[str, Any]:
    """A run's totals of the per-task counts, kept apart by where they came from."""
    counted = [row["agent"] for row in rows if row.get("agent")]
    logged = [c for c in counted if c["source"] == "agent_log"]
    totals: dict[str, Any] = {}
    for counts in logged:
        agent_log.add_counts(totals, {k: v for k, v in counts.items() if k != "sim_id"})
    return {
        "tasks": len(counted),
        "tasks_with_log": len(logged),
        "tasks_not_in_results": sum(1 for c in counted if not c["in_results"]),
        "model_calls": sum(c.get("model_calls") or 0 for c in counted),
        "benchmark_steps": sum(c.get("benchmark_steps") or 0 for c in counted),
        "log": totals or None,
    }


def _labels(ctx: Context) -> tuple[list[dict], str | None]:
    try:
        return ctx.notes.labels(), None
    except annotations.LabelsFileError as error:
        return [], str(error)


def _label_links(design: dict | None) -> dict:
    return (design or {}).get("label_links") or {}


def _brief_decisions(design: dict | None) -> list[dict]:
    return [
        {
            "id": d["id"],
            "name": d["name"],
            "status": d["status"],
            "addresses": d.get("addresses") or [],
        }
        for d in (design or {}).get("decisions") or []
    ]


def task_view(
    ctx: Context, name: str, task_id: str, trial: int, attempt: str | None = None
) -> dict[str, Any]:
    page = pages.task_page(ctx.store, name, task_id, trial, attempt)
    meta = runmeta.read_meta(ctx.store.run_dir(name))
    info = _load_info(ctx, name)
    design = _run_design(ctx, name, info, meta)["design"]
    labels, problem = _labels(ctx)
    entry = ctx.notes.read(name).get(task_key(task_id, trial))
    page["labels"] = labels
    page["labels_problem"] = problem
    page["annotation"] = entry
    page["design_decisions"] = _brief_decisions(design)
    page["design_version"] = (design or {}).get("version")
    page["label_links"] = _label_links(design)

    sim = _simulations(ctx, name).get((task_id, trial))
    if sim is None:
        page["checks"] = []
        page["suggested_labels"] = []
        return page
    page["annotation_is_stale"] = bool(
        entry and entry.get("sim_id") and entry["sim_id"] != sim.get("id")
    )
    schemas = _tool_schemas(ctx, name, design)
    results = checks.run_checks(sim.get("messages") or [], schemas)
    definitions = {item["id"]: item for item in checks.DEFINITIONS}
    page["checks"] = [{**definitions[r["id"]], **r} for r in results]
    page["suggested_labels"] = checks.suggested_labels(
        results, sim.get("termination_reason")
    )
    return page


def save_annotation(
    ctx: Context, name: str, task_id: str, trial: int, labels: list[dict], note: str
) -> dict[str, Any]:
    page = pages.task_page(ctx.store, name, task_id, trial)  # 404s on a bad task.
    meta = runmeta.read_meta(ctx.store.run_dir(name))
    design = _run_design(ctx, name, _load_info(ctx, name), meta)["design"]
    allowed = {d["id"] for d in design["decisions"]} if design else None
    entry = ctx.notes.save(
        name,
        task_id,
        trial,
        (page.get("sim") or {}).get("id"),
        labels,
        note,
        allowed,
    )
    return {"annotation": entry}


def design_view(ctx: Context, name: str) -> dict[str, Any]:
    """A run's design, next to what the run actually sent to the model."""
    run_dir = ctx.store.run_dir(name)
    info = _load_info(ctx, name)
    found = _run_design(ctx, name, info, runmeta.read_meta(run_dir))
    design = found["design"]

    call = _saved_call(ctx, name)
    request = (call or {}).get("request") or {}
    saved_prompt = None
    for message in request.get("messages") or []:
        if message.get("role") == "system":
            saved_prompt = _message_text(message.get("content"))
            break
    rebuilt = designs.rebuild_prompt(design, info) if design else None

    return {
        "run": name,
        "design": design,
        "origin": found["origin"],
        "missing_version": found.get("version"),
        "agent_implementation": _agent_implementation(info),
        "source_status": designs.source_status(design) if design else None,
        "prompt": saved_prompt or rebuilt,
        "prompt_source": "saved_call"
        if saved_prompt
        else "rebuilt"
        if rebuilt
        else None,
        "prompt_matches_design": (
            saved_prompt == rebuilt if saved_prompt and rebuilt else None
        ),
        "tools": request.get("tools") or None,
        "parameters": {
            "model": request.get("model") or (info.get("agent_info") or {}).get("llm"),
            "llm_args": (info.get("agent_info") or {}).get("llm_args"),
            "call_arguments": request.get("kwargs"),
            "tool_choice": request.get("tool_choice"),
            "max_steps": info.get("max_steps"),
            "max_errors": info.get("max_errors"),
            "seed": info.get("seed"),
        },
    }


def designs_view(ctx: Context) -> dict[str, Any]:
    return {
        "designs": [
            {
                "version": design["version"],
                "title": design["title"],
                "decisions": len(design["decisions"]),
                "source_status": designs.source_status(design),
            }
            for design in ctx.designs.all().values()
        ],
        "problems": ctx.designs.problems(),
    }


def design_compare_view(ctx: Context, first: str, second: str) -> dict[str, Any]:
    return designs.compare_designs(ctx.designs.get(first), ctx.designs.get(second))


def compare_view(ctx: Context, first: str, second: str) -> dict[str, Any]:
    old, new = run_view(ctx, first), run_view(ctx, second)
    old_design = (old.get("design") or {}).get("version")
    new_design = (new.get("design") or {}).get("version")
    difference = None
    if old_design and new_design and old_design != new_design:
        try:
            difference = designs.compare_designs(
                ctx.designs.get(old_design), ctx.designs.get(new_design)
            )
        except designs.UnknownDesign:
            pass
    if difference and difference.get("mapping"):
        _add_linked_failures(difference["mapping"], old, new, old_design)
    return {
        "first": _compare_side(first, old),
        "second": _compare_side(second, new),
        **compare.compare_rows(old["rows"], new["rows"]),
        "check_definitions": checks.DEFINITIONS,
        "design_difference": difference,
    }


def _linked(page: dict) -> dict[str, list[dict]]:
    """Failed tasks per design decision, from the run's failure labels."""
    return {
        bucket["id"]: bucket["failures"]
        for bucket in (page.get("analysis") or {}).get("by_decision") or []
    }


def _add_linked_failures(mapping: dict, old: dict, new: dict, old_design: str) -> None:
    """Beside each base decision: the failures each run linked to it, or to what
    replaced it. This compares runs whose designs use different decision ids."""
    base_page, other_page = (old, new) if mapping["base"] == old_design else (new, old)
    base_links, other_links = _linked(base_page), _linked(other_page)
    for row in mapping["rows"]:
        row["base_failures"] = base_links.get(row["id"], [])
        seen, failures = set(), []
        for replacement in row["replaced_by"]:
            for task in other_links.get(replacement["id"], []):
                key = (task["task_id"], task["trial"])
                if key not in seen:
                    seen.add(key)
                    failures.append(task)
        row["other_failures"] = failures
    for decision in mapping["new"]:
        decision["other_failures"] = other_links.get(decision["id"], [])
    mapping["base_run"] = base_page["summary"]["name"]
    mapping["other_run"] = other_page["summary"]["name"]


def _compare_side(name: str, page: dict) -> dict[str, Any]:
    return {
        "name": name,
        "date": page["summary"].get("date"),
        "counts": page["summary"].get("counts"),
        "design": (page.get("design") or {}).get("version"),
        "design_origin": (page.get("design") or {}).get("origin"),
        "note": (page.get("recorded") or {}).get("note"),
        "problem": page["summary"].get("problem"),
        "agent": (page["summary"].get("agent") or {}).get("implementation"),
        "agent_totals": page.get("agent_totals"),
    }


def meta_view(ctx: Context, name: str) -> dict[str, Any]:
    run_dir = ctx.store.run_dir(name)
    return {
        "meta": runmeta.read_meta(run_dir),
        "diff": runmeta.read_diff(run_dir),
    }


def source_view(ctx: Context, file: str) -> dict[str, Any]:
    text = ctx.designs.read_source(file)
    if text is None:
        raise pages.NotFound(f"{Path(file).name} is not cited by any design.")
    return {"file": file, "lines": text.splitlines()}
