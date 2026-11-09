// Counts from agents' own logs, per task and per run: how often each CHECK fired,
// what prepare_change ignored, model calls and benchmark steps. The numbers come
// from viewer/agent_log.counts and pages.task_counts.

import { el, plural, taskLink } from "./util.js";

export const CHECK_KINDS = [
  ["invalid_call", "Rejected", "A model reply rejected before anything ran."],
  ["fixed", "Fixed", "Something code fixed in a model reply on the way."],
  ["ignored_inputs", "Ignored inputs", "prepare_change got inputs its change kind does not use."],
  ["blocked_reply", "Blocked", "A model message with internal IDs, not sent."],
  ["empty_reply", "Empty", "The model replied with nothing."],
  ["safe_message", "Safe message", "Too many failing tries in a row: code sent its safe message."],
  ["tool_crash", "Tool crash", "A bug in one of the agent's tools."],
];

const STEPS_HELP =
  "Benchmark steps, as max_steps counts them: each customer message, each agent message after the greeting, and each batch of tool results.";
const CALLS_HELP = "Agent model calls. For an agent with its own log, every call, including retries inside one turn.";

function num(value, css = "num") {
  return el("td", { class: css, text: value === undefined || value === null ? "" : String(value) });
}

function th(label, help, css = "num") {
  return el("th", { class: `${css} ${help ? "help" : ""}`.trim(), title: help, text: label });
}

function sourceNote(agent) {
  if (!agent || agent.in_results) return null;
  return `From attempt ${agent.sim_id.slice(0, 8)}, which results.json does not hold yet.`;
}

// The Results table's two cells: model calls and benchmark steps of one task.
export function countCells(agent) {
  const note = sourceNote(agent);
  const mark = note ? "*" : "";
  return [
    el("td", { class: "num", title: note ?? CALLS_HELP, text: agent?.model_calls === undefined ? "" : `${agent.model_calls}${mark}` }),
    el("td", { class: "num", title: note ?? STEPS_HELP, text: agent?.benchmark_steps === undefined ? "" : `${agent.benchmark_steps}${mark}` }),
  ];
}

export const COUNT_COLUMNS = [
  ["Model calls", CALLS_HELP, "num"],
  ["Steps", STEPS_HELP, "num"],
];

function breakdownTable(title, firstColumn, entries) {
  if (!entries.length) return null;
  return el(
    "div",
    {},
    el("h3", { text: title }),
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        {},
        el("thead", {}, el("tr", {}, el("th", { text: firstColumn }), th("Times"))),
        el("tbody", {}, entries.map(([name, count]) => el("tr", {}, el("td", { class: "mono", text: name }), num(count)))),
      ),
    ),
  );
}

function sorted(counts) {
  return Object.entries(counts ?? {}).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

// ignored_inputs: by change kind, by input name, and both together.
export function ignoredBreakdown(ignored, empty = "prepare_change never got an input its change kind does not use.") {
  const byKind = sorted(ignored?.by_kind);
  if (!byKind.length) return el("p", { class: "subtle small", text: empty });
  const pairs = Object.entries(ignored.by_kind_and_input ?? {}).flatMap(([kind, inputs]) =>
    Object.entries(inputs).map(([input, count]) => [`${kind} · ${input}`, count]),
  );
  return el(
    "div",
    { class: "breakdowns" },
    breakdownTable("By change kind (events)", "Change kind", byKind),
    breakdownTable("By input name", "Input", sorted(ignored.by_input)),
    breakdownTable("By change kind and input", "Kind · input", pairs.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))),
  );
}

// The run page's "Agent checks" tab: one row per task with a log, and the run's totals.
export function agentChecksTab(page, run) {
  const totals = page.agent_totals ?? {};
  const rows = page.rows.filter((row) => row.agent?.source === "agent_log");
  const log = totals.log ?? {};
  const head = el(
    "tr",
    {},
    el("th", { text: "Task" }),
    el("th", { text: "Attempt" }),
    th("Model calls", CALLS_HELP),
    th("Steps", STEPS_HELP),
    th("Tool calls", "Calls to the agent's own tools."),
    th("Store calls", "Calls the agent's tools sent to the store (fde-N)."),
    CHECK_KINDS.map(([, label, help]) => th(label, help)),
  );
  const line = (row) => {
    const a = row.agent;
    return el(
      "tr",
      {},
      el("td", {}, el("a", { href: taskLink(run, row.task_id, row.trial), text: row.trial ? `${row.task_id}.${row.trial}` : row.task_id })),
      el("td", { class: "mono small", title: sourceNote(a) ?? "The attempt results.json holds.", text: `${a.sim_id.slice(0, 8)}${a.in_results ? "" : " *"}` }),
      num(a.model_calls),
      num(a.benchmark_steps),
      num(a.tool_calls),
      num(a.store_calls),
      CHECK_KINDS.map(([kind]) => el("td", { class: `num ${a.checks?.[kind] ? "flag" : "subtle"}`, text: String(a.checks?.[kind] ?? 0) })),
    );
  };
  const totalRow = el(
    "tr",
    { class: "total-row" },
    el("td", { text: "Run" }),
    el("td", { class: "small", text: plural(rows.length, "task") }),
    num(log.model_calls),
    num(log.benchmark_steps),
    num(log.tool_calls),
    num(log.store_calls),
    CHECK_KINDS.map(([kind]) => num(log.checks?.[kind] ?? 0)),
  );
  return el(
    "div",
    {},
    el("p", {
      class: "caption",
      text: "From each task's fde_agent_log.json. CHECK counts are events: one model reply can fire several. * marks an attempt that results.json does not hold yet.",
    }),
    el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, rows.map(line), totalRow))),
    el("h2", { class: "section-heading", text: "Ignored inputs (prepare_change)" }),
    el("p", {
      class: "caption",
      text: "Inputs the model sent that the change kind does not use. They are ignored, and counted to decide whether to split prepare_change into one tool per kind.",
    }),
    ignoredBreakdown(log.ignored_inputs),
  );
}

// Totals for one side of the compare page.
export function compareTotals(side) {
  const t = side.agent_totals;
  if (!t || !t.tasks) return null;
  const fired = Object.entries(t.log?.checks ?? {}).filter(([, n]) => n);
  return [
    el("dt", { text: "Model calls" }),
    el("dd", { text: `${t.model_calls} in ${plural(t.tasks, "task")}` }),
    el("dt", { text: "Benchmark steps" }),
    el("dd", { text: String(t.benchmark_steps) }),
    t.log && el("dt", { text: "CHECKs fired" }),
    t.log && el("dd", { class: "mono", text: fired.length ? fired.map(([k, n]) => `${k} ×${n}`).join(", ") : "none" }),
    t.tasks_not_in_results > 0 && el("dt", { text: "Not in results.json" }),
    t.tasks_not_in_results > 0 && el("dd", { text: `${plural(t.tasks_not_in_results, "task")}: counted from an attempt's log` }),
  ];
}
