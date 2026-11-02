// Trace viewer frontend. All model output is untrusted text: it only ever reaches
// the page as text nodes, never as HTML.

import { checksPanel, checksTab } from "./checks.js";
import { renderCompare } from "./compare.js";
import { openRecord, renderExplorer, renderRecordPage } from "./database.js";
import { renderDesigns, renderRunDesign } from "./design.js";
import {
  closeDrawer,
  drawerLoading,
  isOpen,
  openDrawer,
  resetDrawer,
  setOnClose,
} from "./drawer.js";
import { linked, loadIds, setLinking } from "./ids.js";
import { labelsPanel, whatsBreaking } from "./labels.js";
import { watch } from "./live.js";
import { differencePanel, showDifference } from "./replay.js";
import {
  CHECK_LABELS,
  api,
  chip,
  el,
  enc,
  formatDate,
  formatTime,
  keyValues,
  pretty,
  rawJson,
  runLink,
  selectRun,
  taskLink,
} from "./util.js";

const view = document.getElementById("view");
const crumbs = document.getElementById("crumbs");

const EXPLORER_TABS = ["users", "orders", "products", "items"];

const RUN_STATES = {
  running: ["Running", "infra"],
  stopped: ["Stopped unexpectedly", "fail"],
  finished: ["Finished", ""], // Status says if a run is going, not how well it did.
  failed: ["Finished with an error", "fail"],
  complete: ["Complete", ""],
  may_be_running: ["May be running", "infra"],
  incomplete: ["Incomplete", "stopped"],
};
const RUN_STATE_HELP = {
  running: "The run script's heartbeat is fresh.",
  stopped: "The run script stopped updating its heartbeat without finishing.",
  may_be_running: "This run was not started by the run script, so this is a guess from recent activity.",
  incomplete: "Some tasks have no result, and nothing has happened recently.",
};
const ATTEMPT_LABELS = {
  used: "Final",
  failed: "Failed",
  discarded: "Discarded",
  no_status: "No status",
};
const ATTEMPT_HELP = {
  used: "This attempt is the one recorded in results.json.",
  failed: "This attempt crashed and the runner retried the task.",
  discarded: "This attempt was thrown away and re-run.",
  no_status:
    "No sim_status.json: the run was interrupted here, or it is still running.",
};
const STEP_LABELS = {
  customer: "Customer",
  agent: "Agent",
  greeting: "Agent greeting (scripted)",
  system: "System",
  tool: "Tool result",
};

let failuresOnly = false;
let runTab = "results";
let navigation = 0;
let selectedStep = null;

// ---- Small helpers ----

function formatDuration(seconds) {
  if (seconds === undefined || seconds === null) return "";
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
}

function formatSeconds(seconds) {
  return seconds === undefined || seconds === null ? "" : `${seconds.toFixed(1)}s`;
}

function formatTokens(usage) {
  if (!usage) return "";
  return `${usage.prompt_tokens ?? "?"} in, ${usage.completion_tokens ?? "?"} out`;
}

function formatRate(counts) {
  if (!counts || counts.rate === null) return "no scored simulations";
  return `${counts.passed}/${counts.scored} (${Math.round(counts.rate * 100)}%)`;
}

function asText(content) {
  if (Array.isArray(content)) {
    return content.map((p) => (typeof p === "string" ? p : pretty(p))).join("\n");
  }
  return content ?? "";
}

function prettyToolOutput(content) {
  if (typeof content !== "string") return pretty(content);
  try {
    const parsed = JSON.parse(content);
    return typeof parsed === "object" && parsed !== null ? pretty(parsed) : content;
  } catch {
    return content;
  }
}

function runStateChip(state) {
  const [label, css] = RUN_STATES[state] ?? [state, ""];
  return el("span", { class: `chip ${css}`, title: RUN_STATE_HELP[state], text: label });
}

function setCrumbs(...items) {
  crumbs.replaceChildren();
  items.forEach((item, index) => {
    if (index) crumbs.append(el("span", { class: "sep", text: "/" }));
    crumbs.append(
      item.href ? el("a", { href: item.href, text: item.label }) : el("span", { text: item.label }),
    );
  });
}

function show(...nodes) {
  view.replaceChildren(...nodes.filter(Boolean));
}

function showError(error) {
  show(el("div", { class: "empty", text: error.message }));
}

// ---- Runs list ----

function modelCell(info) {
  if (!info) return el("td", { class: "model", text: "unknown" });
  return el(
    "td",
    { class: "model" },
    el("div", { class: "mono", text: info.llm ?? "no model recorded" }),
    el("div", { class: "subtle small", text: info.implementation }),
  );
}

function judgesCell(judges) {
  const communicate = judges.communicate
    ? `${judges.communicate}${judges.communicate_scored ? "" : " (not scored)"}`
    : "not recorded";
  return el(
    "td",
    { class: "judges" },
    el("div", { class: "small" }, "NL: ", el("span", { class: "mono", text: judges.nl ?? "not recorded" })),
    el("div", { class: "small subtle" }, "Communicate: ", el("span", { class: "mono", text: communicate })),
  );
}

function designCell(run) {
  return el(
    "td",
    { class: "nowrap" },
    el("a", {
      class: "mono",
      href: `${runLink(run.name)}/design`,
      text: run.design_version ?? "unknown",
      onclick: (event) => event.stopPropagation(),
    }),
    run.design_origin === "assumed" && el("div", { class: "subtle small", text: "assumed" }),
  );
}

function codeCell(run) {
  const recorded = run.recorded;
  if (!recorded) return el("td", { class: "subtle small", text: "not recorded" });
  return el(
    "td",
    { class: "nowrap" },
    el("button", {
      class: "link-button mono",
      type: "button",
      title: "Show the exact command and code changes",
      text: (recorded.commit ?? "unknown").slice(0, 8),
      onclick: (event) => {
        event.stopPropagation();
        openRunRecord(run.name, event.currentTarget);
      },
    }),
    recorded.code_changed && el("div", { class: "small subtle", text: "with changes" }),
  );
}

function runRow(run) {
  const open = () => (location.hash = runLink(run.name));
  const name = el(
    "td",
    {},
    el("a", { href: runLink(run.name), text: run.name }),
    run.status === "ok" && el("div", {}, runStateChip(run.state)),
    run.recorded?.note && el("div", { class: "small", text: run.recorded.note }),
  );
  if (run.status !== "ok") {
    return el(
      "tr",
      {},
      name,
      el("td", { colspan: 10, class: "subtle", text: run.problem }),
    );
  }
  const taskCount = run.task_ids.length;
  const tasks = `${run.split ?? "?"}: ${taskCount} ${taskCount === 1 ? "task" : "tasks"}`;
  const row = el(
    "tr",
    { class: "clickable", onclick: open },
    name,
    el("td", { class: "num nowrap", text: formatRate(run.counts) }),
    el("td", { class: "num", text: run.counts.infra }),
    el("td", { class: "num", text: `${run.done_simulations}/${run.expected_simulations}` }),
    el("td", { class: "nowrap", text: formatDate(run.date) }),
    designCell(run),
    codeCell(run),
    modelCell(run.agent),
    modelCell(run.customer),
    judgesCell(run.judges),
    el("td", { class: "nowrap", title: `Tasks: ${run.task_ids.join(", ")}`, text: tasks }),
  );
  return row;
}

async function renderRuns() {
  const token = navigation;
  setCrumbs();
  document.title = "Runs · Trace Viewer";
  const { runs, simulations_dir: folder } = await api("/api/runs");
  if (token !== navigation) return;
  if (!runs.length) {
    show(el("div", { class: "empty", text: `No runs found in ${folder}` }));
    return;
  }
  const columns = [
    ["Run"],
    ["Pass rate", "Passed simulations out of scored ones. Infra errors are left out, as in tau2's own metrics. Stopped-early simulations count as failures.", "num"],
    ["Infra errors", "Simulations that ended with infrastructure_error, such as a model timeout.", "num"],
    ["Done", "Simulations recorded out of tasks times trials.", "num"],
    ["Date"],
    ["Design", "The agent design this run used. Assumed means the run did not record one, so it is inferred from the agent's name."],
    ["Code", "The commit and uncommitted changes saved by the run script. Older runs did not record this."],
    ["Agent"],
    ["Customer"],
    ["Judges", "The NL judge model is not in results.json, so it is read from a saved judge call. If no call was saved it shows as not recorded."],
    ["Tasks"],
  ];
  show(
    el("div", { class: "page-head" }, el("h1", { text: "Runs" }), el("span", { class: "subtle small mono", text: folder })),
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        {},
        el("thead", {}, el("tr", {}, columns.map(headerCell))),
        el("tbody", {}, runs.map(runRow)),
      ),
    ),
  );
}

function headerCell([label, help, css]) {
  return el("th", { class: [help ? "help" : "", css ?? ""].join(" ").trim(), title: help, text: label });
}

// ---- Run overview ----

const ROW_COLUMNS = [
  ["Task"],
  ["Result", "Pass: reward 1. Fail: scored below 1. Stopped early: the run ended before scoring, so reward is 0 and no check ran. Infra error: the task never finished and is left out of the pass rate."],
  ["DB", "Does the final database match the state from replaying the expected actions? Not evaluated means the run stopped before scoring."],
  ["NL", "Natural-language assertions graded by the judge. None defined means the task has none, which the benchmark scores as a vacuous 1.0."],
  ["Termination"],
  ["Duration", "Wall-clock time of the final attempt.", "num"],
  ["Turns", "Customer messages.", "num"],
  ["Tool calls", "Tool calls made during the conversation.", "num"],
  ["Tool errors", "Tool results with error: true.", "num"],
  ["Attempts", "Folders in artifacts/ for this task, across all trials. Failed attempts were retried by the runner and are not in results.json.", "num"],
  ["Labels", "Failure labels you have saved on this task."],
  ["Why"],
];

function attemptsText(attempts) {
  if (!attempts || !attempts.total) return "none saved";
  const notes = [];
  if (attempts.failed) notes.push(`${attempts.failed} failed`);
  if (attempts.no_status) notes.push(`${attempts.no_status} no status`);
  if (attempts.discarded) notes.push(`${attempts.discarded} discarded`);
  return notes.length ? `${attempts.total} (${notes.join(", ")})` : `${attempts.total}`;
}

function taskRow(run, row, showTrial) {
  const href = taskLink(run, row.task_id, row.trial);
  const label = showTrial ? `${row.task_id} · trial ${row.trial}` : row.task_id;
  const task = el("td", { class: "nowrap" }, el("a", { href, text: label }));
  const pending = row.result === "pending";
  const cell = (value, css) => el("td", { class: css, text: pending ? "" : value });
  return el(
    "tr",
    { class: "clickable", onclick: () => (location.hash = href) },
    task,
    el("td", {}, chip(row.result)),
    el("td", {}, !pending && chip(row.db, CHECK_LABELS, "plain")),
    el("td", {}, !pending && chip(row.nl, CHECK_LABELS, "plain")),
    cell(row.termination_reason, "mono nowrap"),
    el("td", { class: "num nowrap", title: pending ? undefined : `${row.duration} seconds`, text: formatDuration(row.duration) }),
    cell(row.turns, "num"),
    cell(row.tool_calls, "num"),
    cell(row.tool_errors, "num"),
    el("td", { class: "num nowrap", text: attemptsText(row.attempts) }),
    el("td", { class: "small", text: (row.labels ?? []).join(", ") }),
    cell(row.why, "why"),
  );
}

function fact(label, value) {
  return [el("dt", { text: label }), el("dd", {}, value ?? "not recorded")];
}

function factBlock(label, value) {
  return el("div", {}, ...fact(label, value));
}

function monoLine(text) {
  return el("span", { class: "mono", text });
}

function recordFacts(name, recorded) {
  if (!recorded) return null;
  return el(
    "section",
    { class: "panel run-record" },
    el("h3", { text: "Run record" }),
    el("p", { text: recorded.note }),
    el(
      "p",
      { class: "caption" },
      `Started ${formatDate(recorded.started_at)}. Commit ${(recorded.commit ?? "unknown").slice(0, 10)}${recorded.code_changed ? " with uncommitted changes" : ""}.`,
    ),
    el("button", {
      class: "link-button",
      type: "button",
      text: "Command and code changes",
      onclick: (event) => openRunRecord(name, event.currentTarget),
    }),
  );
}

async function openRunRecord(name, opener) {
  const title = `Run record, ${name}`;
  drawerLoading(title, opener);
  const { meta, diff } = await api(`/api/runs/${enc(name)}/meta`);
  const sessions = meta?.sessions ?? [];
  const sessionView = (session, index) =>
    el(
      "section",
      { class: "db-section" },
      sessions.length > 1 && el("h3", { text: `Session ${index + 1}` }),
      keyValues([
        ["Note", session.note],
        ["Design", session.design_version],
        ["Started", session.started_at],
        ["Finished", session.finished_at ?? "not finished"],
        ["Exit code", session.exit_code],
        ["Commit", session.git?.commit],
        ["Branch", session.git?.branch],
        ["NL judge", session.judges?.nl],
        ["Communicate judge", session.judges?.communicate],
        ["Changed files", (session.git?.changed_files ?? []).join(", ") || "none"],
        ["New files", (session.git?.untracked_files ?? []).join(", ") || "none"],
      ]),
      el("h3", { style: "margin-top:10px", text: "Exact command" }),
      el("pre", { text: session.command_line }),
    );
  openDrawer({
    title,
    subtitle: `${sessions.length} session${sessions.length === 1 ? "" : "s"}`,
    tabs: [
      { label: "Record", render: () => el("div", {}, sessions.map(sessionView)) },
      {
        label: "Code changes",
        render: () =>
          diff ? el("pre", { class: "log", text: diff }) : el("div", { class: "empty", text: "No uncommitted changes." }),
      },
    ],
  });
}

async function renderRun(name) {
  const token = navigation;
  setCrumbs({ label: name });
  document.title = `${name} · Trace Viewer`;
  const [page, { runs }] = await Promise.all([api(`/api/runs/${enc(name)}`), api("/api/runs")]);
  if (token !== navigation) return;
  const { summary, rows, config } = page;
  const others = runs.map((run) => run.name).filter((other) => other !== name);
  const heading = el(
    "div",
    { class: "page-head" },
    el("h1", { text: name }),
    summary.status === "ok" && runStateChip(page.state),
    summary.status === "ok" && el("a", { class: "link-button", href: `${runLink(name)}/design`, text: "Agent design" }),
    selectRun(others, null, (other) => (location.hash = `#/compare?a=${enc(name)}&b=${enc(other)}`), "Compare with"),
  );
  if (summary.status !== "ok") {
    show(heading, el("div", { class: "notice", text: summary.problem }));
    return;
  }

  const showTrial = summary.num_trials > 1;
  const body = el("tbody");
  const count = el("span", { class: "subtle small" });
  const failing = rows.filter((row) => ["fail", "stopped", "infra"].includes(row.result));

  const paint = () => {
    const shown = failuresOnly ? failing : rows;
    body.replaceChildren(...shown.map((row) => taskRow(name, row, showTrial)));
    count.textContent = `Showing ${shown.length} of ${rows.length}`;
  };
  paint();

  const filter = el("input", {
    type: "checkbox",
    checked: failuresOnly,
    onchange: (event) => {
      failuresOnly = event.target.checked;
      paint();
    },
  });

  const nlJudge = summary.judges.nl ?? page.recorded?.judges?.nl ?? "not recorded";
  const facts = el(
    "dl",
    { class: "facts", style: "margin:0" },
    factBlock("Pass rate", el("strong", { text: formatRate(summary.counts) })),
    factBlock("Infra errors (not in pass rate)", String(summary.counts.infra)),
    factBlock("Agent", monoLine(`${summary.agent?.implementation} · ${summary.agent?.llm}`)),
    factBlock("Customer", monoLine(summary.customer?.llm ?? "not recorded")),
    factBlock("NL judge", monoLine(nlJudge)),
    factBlock("Started", formatDate(summary.date)),
    factBlock("Task split", summary.split),
    factBlock("Seed", String(config.seed)),
    factBlock("Git commit", monoLine((config.git_commit ?? "").slice(0, 10))),
    factBlock("Limits", `${config.max_steps} steps, ${config.max_errors} errors`),
  );

  const results = el(
    "div",
    {},
    el(
      "div",
      { class: "toolbar" },
      el("label", {}, filter, "Failures only"),
      count,
    ),
    el(
      "div",
      { class: "table-wrap" },
      el("table", {}, el("thead", {}, el("tr", {}, ROW_COLUMNS.map(headerCell))), body),
    ),
  );
  const checks = checksTab(page, name);
  const panes = { results, checks };
  const tabBar = el("div", { class: "tabs", role: "tablist" });
  const pane = el("div", { class: "run-pane" });
  const selectTab = (tab) => {
    runTab = tab;
    [...tabBar.children].forEach((button) =>
      button.setAttribute("aria-selected", String(button.dataset.tab === tab)),
    );
    pane.replaceChildren(panes[tab]);
  };
  [
    ["results", "Results"],
    ["checks", "Automatic checks"],
  ].forEach(([tab, label]) =>
    tabBar.append(
      el("button", { class: "tab", type: "button", role: "tab", "data-tab": tab, text: label, onclick: () => selectTab(tab) }),
    ),
  );
  selectTab(runTab);

  show(
    heading,
    summary.problem && el("div", { class: "notice", text: summary.problem }),
    !summary.has_artifacts &&
      el("div", {
        class: "notice",
        text: "This run has no artifacts folder, so no raw model calls or logs exist. Run with --verbose-logs --llm-log-mode all to save them.",
      }),
    recordFacts(name, page.recorded),
    facts,
    whatsBreaking(page, name),
    tabBar,
    pane,
    config.policy &&
      el(
        "details",
        { class: "panel", style: "margin-top:12px" },
        el("summary", { text: "Agent policy used by this run" }),
        el("pre", { style: "margin-top:8px", text: config.policy }),
      ),
  );
}

// ---- Task page ----

function field(label, value) {
  if (value === undefined || value === null || value === "") return null;
  return el("div", { class: "field" }, el("h3", { text: label }), el("p", {}, linked(value)));
}

function taskPanel(task) {
  const panel = el(
    "section",
    { class: "panel" },
    el("h2", { text: `Task ${task.id}` }),
    field("Customer's goal", task.reason_for_call),
    field("Known to the customer", task.known_info),
    field("Unknown to the customer", task.unknown_info),
    field("Persona instructions", task.task_instructions),
    task.persona && field("Persona", typeof task.persona === "string" ? task.persona : pretty(task.persona)),
    field("Reward basis", task.reward_basis.join(" × ")),
  );

  if (task.nl_assertions.length) {
    panel.append(
      el(
        "div",
        { class: "field" },
        el("h3", { text: "NL assertions" }),
        task.nl_assertions.map((text) => el("p", {}, linked(text))),
      ),
    );
  }
  if (task.communicate_info.length) {
    panel.append(
      el(
        "div",
        { class: "field" },
        el("h3", { text: "Communicate info" }),
        !task.communicate_scored &&
          el("p", { class: "caption", text: "Not scored: COMMUNICATE is not in this task's reward basis." }),
        el("p", { class: "mono", text: task.communicate_info.join(", ") }),
      ),
    );
  }

  if (task.actions.length) {
    const matched = task.actions.filter((a) => a.matched).length;
    const evaluated = task.actions.some((a) => a.matched !== null && a.matched !== undefined);
    panel.append(
      el(
        "details",
        { class: "field", open: true },
        el("summary", { text: `Expected actions${evaluated ? ` (${matched} of ${task.actions.length} matched)` : ""}` }),
        el("p", {
          class: "caption",
          text: "One reference path, not required. The DB check compares end states, so a different path can still pass.",
        }),
        task.actions.map(actionRow),
      ),
    );
  }
  return panel;
}

function actionRow(action) {
  const state =
    action.matched === null || action.matched === undefined
      ? null
      : action.matched
        ? el("span", { class: "chip pass plain", text: "matched" })
        : el("span", { class: "chip plain", text: "not matched" });
  return el(
    "div",
    { class: "action" },
    el(
      "div",
      { class: "action-head" },
      el("span", { class: "tool-name", text: action.name }),
      action.tool_type && el("span", { class: "subtle small", text: action.tool_type }),
      state,
    ),
    el("pre", {}, linked(pretty(action.arguments))),
  );
}

function evaluationPanel(page) {
  const panel = el("section", { class: "panel" }, el("h2", { text: "Evaluation" }));
  const info = page.reward_info;
  const sim = page.sim;

  if (!info) {
    panel.append(el("p", { class: "subtle", text: "No evaluation exists: this task never finished, so results.json has no reward_info." }));
    return panel;
  }

  const breakdown = Object.entries(info.reward_breakdown ?? {})
    .map(([key, value]) => `${key} ${value}`)
    .join(" × ");
  panel.append(
    el(
      "dl",
      { class: "kv" },
      el("dt", { text: "Reward" }),
      el("dd", { text: String(info.reward) }),
      breakdown && el("dt", { text: "Breakdown" }),
      breakdown && el("dd", { text: breakdown }),
      el("dt", { text: "DB check" }),
      el("dd", {}, chip(sim.db, CHECK_LABELS, "plain")),
      el("dt", { text: "NL check" }),
      el("dd", {}, chip(sim.nl, CHECK_LABELS, "plain")),
    ),
  );

  if (info.info?.note) panel.append(el("p", { class: "caption", style: "margin-top:6px", text: info.info.note }));

  (info.nl_assertions ?? []).forEach((item, index) => {
    panel.append(
      el(
        "div",
        { class: "assertion", id: `assertion-${index}` },
        el("span", { class: `chip ${item.met ? "pass" : "fail"}`, text: item.met ? "Met" : "Not met" }),
        el("p", {}, linked(item.nl_assertion)),
        el("p", { class: "subtle small" }, "Judge: ", linked(item.justification)),
      ),
    );
  });

  panel.append(
    el(
      "div",
      { class: "toolbar" },
      el("button", {
        class: "link-button",
        type: "button",
        text: "Judge's raw call",
        onclick: (event) => openJudgeCalls(page, event.currentTarget),
      }),
    ),
    el(
      "details",
      {},
      el("summary", { text: "Raw reward_info" }),
      el("pre", { style: "margin-top:6px", text: pretty(info) }),
    ),
  );
  return panel;
}

function attemptsPanel(page) {
  const panel = el("section", { class: "panel" }, el("h2", { text: `Attempts (${page.attempts.length})` }));
  if (!page.attempts.length) {
    panel.append(el("p", { class: "subtle small", text: "No artifacts were saved for this task." }));
    return panel;
  }
  if (page.attempts.length > 1) {
    panel.append(el("p", { class: "caption", text: "The runner retries failures. results.json keeps only the final one, so the rest exist only here." }));
  }
  for (const attempt of page.attempts) {
    const css = attempt.status === "used" ? "pass" : attempt.status === "failed" ? "fail" : "";
    panel.append(
      el(
        "div",
        { class: "attempt" },
        el(
          "div",
          { class: "attempt-head" },
          el("span", {
            class: `chip ${css}`,
            title: ATTEMPT_HELP[attempt.status],
            text: ATTEMPT_LABELS[attempt.status] ?? attempt.status,
          }),
          el("span", { class: "mono small", title: attempt.sim_id, text: attempt.sim_id.slice(0, 8) }),
          el("span", { class: "subtle small", text: `${attempt.llm_calls} calls` }),
          el("span", { class: "subtle small", text: formatDate(attempt.started_at) }),
          el("button", {
            class: "link-button",
            type: "button",
            text: "Open",
            onclick: (event) => openAttempt(page, attempt, event.currentTarget),
          }),
        ),
        attempt.error && el("p", { class: "error", text: `${attempt.error_type}: ${attempt.error}` }),
      ),
    );
  }
  return panel;
}

function toolCard(call) {
  const result = call.result;
  const output = result ? prettyToolOutput(result.content) : null;
  const isLong = output !== null && output.length > 600;
  const resultBody = !result
    ? el("div", { class: "subtle small", text: "No result recorded." })
    : isLong && !result.error
      ? el(
          "details",
          {},
          el("summary", { text: `Show result (${output.length.toLocaleString()} characters)` }),
          el("pre", {}, linked(output)),
        )
      : el("pre", {}, linked(output));

  const name = call.name === "" ? "(empty tool name)" : (call.name ?? "(unknown call)");
  return el(
    "div",
    { class: `tool ${result?.error ? "error" : ""}` },
    el(
      "div",
      { class: "tool-section" },
      el("div", { class: "tool-kind", text: "Tool call" }),
      el("div", { class: "tool-name", text: name }),
      el("pre", {}, linked(pretty(call.arguments))),
    ),
    el(
      "div",
      { class: "tool-section" },
      el("div", { class: "tool-kind result", text: result?.error ? "Tool error" : "Tool result" }),
      resultBody,
    ),
  );
}

function stepNode(step, onRaw) {
  const hasRawButton = onRaw && ["agent", "customer"].includes(step.type);
  const reasoning =
    step.reasoning &&
    el(
      "details",
      { class: "reasoning" },
      el("summary", { text: "Model reasoning" }),
      el("pre", {}, linked(step.reasoning)),
    );
  const node = el(
    "article",
    { class: `step ${step.type}`, id: `step-${step.index}` },
    el(
      "div",
      { class: "step-head" },
      el("span", { class: "step-label", text: STEP_LABELS[step.type] ?? step.type }),
      step.turn_idx !== undefined && step.turn_idx !== null && el("span", { text: `#${step.turn_idx}` }),
      step.timestamp && el("span", { text: formatTime(step.timestamp) }),
      step.usage && el("span", { text: formatTokens(step.usage) }),
      step.generation_time_seconds && el("span", { text: formatSeconds(step.generation_time_seconds) }),
      el("span", { class: "spacer" }),
      hasRawButton &&
        el("button", {
          class: "link-button",
          type: "button",
          text: "Raw call",
          onclick: (event) => onRaw(step, event.currentTarget),
        }),
    ),
    step.content && el("div", { class: "step-text" }, linked(step.content)),
    reasoning,
    (step.tool_calls ?? []).map(toolCard),
  );
  return node;
}

function scrollToStep(index) {
  const node = document.getElementById(`step-${index}`);
  if (!node) return;
  node.scrollIntoView({ behavior: "smooth", block: "center" });
  highlightStep(node);
}

function highlightStep(node) {
  selectedStep?.classList.remove("selected");
  selectedStep = node;
  node?.classList.add("selected");
}

function verdictBanner(page) {
  const { sim, neighbours, run } = page;
  const nav = (label, target) =>
    target
      ? el("a", { class: "link-button", href: taskLink(run, target.task_id, target.trial), text: label })
      : el("span", { class: "link-button", "aria-disabled": "true", text: label });
  const hasDifference = sim?.db === "fail" || (sim?.result === "stopped" && sim.messages > 0);
  const links = el(
    "span",
    { style: "display:flex;gap:6px;flex-wrap:wrap" },
    hasDifference &&
      el("button", {
        class: "link-button",
        type: "button",
        text: "Database difference",
        onclick: (event) => showDifference(page, event.currentTarget),
      }),
    sim?.first_error_step !== null && sim?.first_error_step !== undefined &&
      el("button", {
        class: "link-button",
        type: "button",
        text: "First tool error",
        onclick: () => scrollToStep(sim.first_error_step),
      }),
    sim?.first_failed_assertion !== null && sim?.first_failed_assertion !== undefined &&
      el("button", {
        class: "link-button",
        type: "button",
        text: "Failed assertion",
        onclick: () => {
          const node = document.getElementById(`assertion-${sim.first_failed_assertion}`);
          node?.scrollIntoView({ behavior: "smooth", block: "center" });
          node?.classList.add("target");
        },
      }),
    nav("Previous failure", neighbours.previous),
    nav("Next failure", neighbours.next),
  );

  if (!sim) {
    return el(
      "div",
      { class: "banner" },
      chip("pending"),
      el("span", { class: "why", text: "This task has no simulation in results.json yet. Any attempts so far are listed below." }),
      links,
    );
  }
  const stats = sim.messages
    ? ` · ${formatDuration(sim.duration)} · ${sim.turns} turns · ${sim.tool_calls} tool calls · ${sim.tool_errors} tool errors`
    : " · no conversation recorded";
  return el(
    "div",
    { class: `banner ${sim.result}` },
    chip(sim.result),
    el(
      "span",
      { class: "why" },
      el("div", { text: sim.why || "All scored checks passed." }),
      el("div", { class: "subtle small" }, monoLine(sim.termination_reason), stats),
    ),
    links,
  );
}

function infraDetails(sim) {
  const traceback = sim?.info?.error_traceback;
  if (!traceback) return null;
  return el(
    "details",
    { class: "panel" },
    el("summary", { text: "Error traceback" }),
    el("pre", { class: "log", style: "margin-top:8px", text: traceback }),
  );
}

async function renderTask(run, taskId, trial) {
  const title = `Task ${taskId}${trial ? `, trial ${trial}` : ""}`;
  setCrumbs({ label: run, href: runLink(run) }, { label: title });
  document.title = `${title} · ${run}`;
  const page = await api(`/api/runs/${enc(run)}/tasks/${enc(taskId)}?trial=${trial}`);
  if (page.links_ids) await loadIds();
  setLinking(page.links_ids);

  const onRaw = (step, opener) => openStepCall(page, step, opener);
  const timeline = el(
    "section",
    { class: "timeline", "aria-label": "Conversation" },
    page.timeline.map((step) => stepNode(step, page.sim ? onRaw : null)),
  );
  const emptyNote =
    !page.timeline.length &&
    el("div", {
      class: "panel subtle",
      text: page.sim
        ? "results.json holds no conversation for this simulation. Open a failed attempt on the right to see how far it got."
        : "No conversation yet.",
    });

  const side = el(
    "div",
    { class: "side" },
    page.sim && evaluationPanel(page),
    page.sim && differencePanel(page),
    labelsPanel(page),
    page.checks.length > 0 && checksPanel(page, scrollToStep),
    attemptsPanel(page),
    taskPanel(page.task),
    el(
      "details",
      { class: "panel" },
      el("summary", { text: "Raw task JSON" }),
      el("pre", { style: "margin-top:8px", text: pretty(page.task_raw) }),
    ),
  );

  show(
    el(
      "div",
      { class: "page-head" },
      el("h1", { text: title }),
      el("a", { class: "link-button", href: `${runLink(run)}/design`, text: "Agent design" }),
      el("span", { id: "new-data" }),
    ),
    page.problem && el("div", { class: "notice", text: page.problem }),
    verdictBanner(page),
    page.raw_call_note && el("div", { class: "notice", text: page.raw_call_note }),
    el(
      "div",
      { class: "task-layout" },
      el("div", { style: "display:grid;gap:12px;min-width:0" }, emptyNote, infraDetails(page.sim), timeline),
      side,
    ),
  );
}

// ---- Drawer ----

function promptRoleLabel(role, swapped) {
  if (!swapped) return role;
  if (role === "user") return "user (this is the agent)";
  if (role === "assistant") return "assistant (this is the simulated customer)";
  return role;
}

function promptMessage(message, swapped) {
  return el(
    "div",
    { class: "prompt-message" },
    el("div", { class: "prompt-role", text: promptRoleLabel(message.role, swapped) }),
    message.content !== null && message.content !== undefined && message.content !== "" &&
      el("pre", {}, linked(asText(message.content))),
    message.tool_calls && el("pre", {}, linked(pretty(message.tool_calls))),
    message.tool_call_id && el("div", { class: "subtle small mono", text: `tool_call_id: ${message.tool_call_id}` }),
  );
}

function toolDefinition(tool) {
  const definition = tool.function ?? tool;
  return el(
    "div",
    { class: "tool-def" },
    el("div", { class: "tool-name", text: definition.name }),
    el("p", { text: definition.description }),
    el("details", {}, el("summary", { text: "Parameters" }), el("pre", { text: pretty(definition.parameters) })),
  );
}

// Tabs for one model call. `record` is the step's entry in results.json, which
// holds the reasoning and finish reason that llm_debug leaves out.
function callTabs({ call, record, swapped, reason }) {
  const request = call?.request ?? {};
  const response = call?.response ?? {};
  const choice = record?.raw_data?.choices?.[0];
  const reasoning = choice?.message?.reasoning_content;
  const messages = request.messages ?? [];
  const system = messages.filter((m) => m.role === "system");
  const history = messages.filter((m) => m.role !== "system");
  const usage = response.usage ?? record?.usage;
  const seconds = response.generation_time_seconds ?? record?.generation_time_seconds;

  const overview = () =>
    el(
      "div",
      {},
      reason && el("div", { class: "notice", text: reason }),
      keyValues([
        ["Model", request.model ?? record?.raw_data?.model],
        ["Call", call?.call_name],
        ["Tokens in", usage?.prompt_tokens],
        ["Tokens out", usage?.completion_tokens],
        ["Time", seconds === undefined || seconds === null ? null : `${seconds.toFixed(2)} s`],
        ["Cost", response.cost ?? record?.cost],
        ["Finish reason", choice?.finish_reason],
        ["Tool choice", request.tool_choice],
        ["Arguments", request.kwargs && pretty(request.kwargs)],
        ["Sent", request.timestamp],
        ["Received", response.timestamp],
        ["File", call?.file],
      ]),
    );

  const responseTab = () => {
    const content = response.content ?? choice?.message?.content;
    const toolCalls = response.tool_calls ?? choice?.message?.tool_calls;
    return el(
      "div",
      { style: "display:grid;gap:12px" },
      el("div", {}, el("h3", { text: "Content" }), el("pre", {}, linked(content || "(empty)"))),
      toolCalls && el("div", {}, el("h3", { text: "Tool calls" }), el("pre", {}, linked(pretty(toolCalls)))),
      reasoning && el("div", {}, el("h3", { text: "Reasoning (from results.json)" }), el("pre", {}, linked(reasoning))),
      choice?.finish_reason && el("div", {}, el("h3", { text: "Finish reason" }), el("pre", { text: choice.finish_reason })),
    );
  };

  const tabs = [{ label: "Overview", render: overview }];
  if (call) {
    tabs.push({
      label: "System prompt",
      render: () =>
        system.length
          ? el("pre", { text: system.map((m) => asText(m.content)).join("\n\n") })
          : el("div", { class: "empty", text: "This call had no system prompt." }),
    });
    tabs.push({
      label: `Messages (${history.length})`,
      render: () =>
        el(
          "div",
          {},
          swapped &&
            el("p", {
              class: "caption",
              text: "The customer simulator sees the conversation with roles swapped: to its model, the agent is the user.",
            }),
          history.map((m) => promptMessage(m, swapped)),
        ),
    });
    if (request.tools?.length) {
      tabs.push({
        label: `Tools (${request.tools.length})`,
        render: () => el("div", {}, request.tools.map(toolDefinition)),
      });
    }
  }
  tabs.push({ label: "Response", render: responseTab });
  tabs.push({
    label: "Raw JSON",
    render: () => rawJson(record ? { llm_debug_file: call, results_json_message: record } : call),
  });
  return tabs;
}

async function openStepCall(page, step, opener) {
  highlightStep(document.getElementById(`step-${step.index}`));
  const title = `${STEP_LABELS[step.type]} step #${step.turn_idx ?? step.index}`;
  drawerLoading(title, opener);
  try {
    const url = `/api/runs/${enc(page.run)}/sims/${enc(page.sim.id)}/steps/${step.index}/call`;
    const data = await api(url);
    const swapped = data.call?.call_name === "user_simulator_response";
    openDrawer({
      title,
      subtitle: data.call?.file ?? data.missing_reason,
      tabs: callTabs({ call: data.call, record: data.results_record, swapped, reason: data.missing_reason }),
    });
  } catch (error) {
    openDrawer({ title, subtitle: error.message, tabs: [] });
  }
}

async function openJudgeCalls(page, opener) {
  const title = "Judge's raw call";
  drawerLoading(title, opener);
  try {
    const data = await api(`/api/runs/${enc(page.run)}/sims/${enc(page.sim.id)}/judge-calls`);
    if (!data.calls.length) {
      openDrawer({ title, subtitle: data.missing_reason, tabs: [{ label: "Overview", render: () => el("div", { class: "empty", text: data.missing_reason }) }] });
      return;
    }
    const single = data.calls.length === 1;
    const tabs = single
      ? callTabs({ call: data.calls[0] })
      : data.calls.map((call, index) => ({
          label: `Call ${index + 1}`,
          render: () => el("pre", { text: pretty(call) }),
        }));
    openDrawer({ title, subtitle: single ? data.calls[0].file : `${data.calls.length} calls`, tabs });
  } catch (error) {
    openDrawer({ title, subtitle: error.message, tabs: [] });
  }
}

function attemptCallList(base, calls, attempt, page) {
  if (!calls.length) return el("div", { class: "empty", text: "This attempt saved no llm_debug files." });
  return el(
    "div",
    {},
    calls.map((call) =>
      el(
        "button",
        {
          class: "call-row",
          type: "button",
          onclick: async () => {
            const full = await api(`${base}/calls/${enc(call.file)}`);
            openDrawer({
              title: `${call.call_name} · ${formatTime(call.timestamp)}`,
              subtitle: `Attempt ${attempt.sim_id.slice(0, 8)}: ${call.file}`,
              tabs: callTabs({ call: full, swapped: call.call_name === "user_simulator_response" }),
              onBack: () => openAttempt(page, attempt),
            });
          },
        },
        el("span", { class: "mono small", text: formatTime(call.timestamp) }),
        " ",
        el("span", { class: "mono small", text: call.call_name }),
        el(
          "span",
          { class: "subtle small" },
          ` · ${call.prompt_tokens ?? "?"} in, ${call.completion_tokens ?? "?"} out · ${formatSeconds(call.generation_time_seconds)}`,
        ),
      ),
    ),
  );
}

async function openAttempt(page, attempt, opener) {
  const short = attempt.sim_id.slice(0, 8);
  const title = `Attempt ${short}`;
  drawerLoading(title, opener);
  const base = `/api/runs/${enc(page.run)}/attempts/${enc(page.task_id)}/${enc(attempt.sim_id)}`;
  try {
    const data = await api(base);
    const label = ATTEMPT_LABELS[data.attempt.status] ?? data.attempt.status;
    const stepsCount = data.conversation.length;
    const conversationTab = () =>
      el(
        "div",
        { class: "timeline" },
        stepsCount
          ? el("p", {
              class: "caption",
              text: "Rebuilt from the last saved agent request. Reasoning is not saved in llm_debug, and a call that failed is never written, so the conversation stops at the last successful call.",
            })
          : el("div", { class: "empty", text: "No agent request was saved, so the conversation cannot be rebuilt." }),
        data.conversation.map((step) => stepNode(step, null)),
      );
    openDrawer({
      title,
      subtitle: `${label}${data.attempt.error ? `: ${data.attempt.error}` : ""}`,
      tabs: [
        { label: `Conversation (${stepsCount})`, render: conversationTab },
        { label: `Model calls (${data.calls.length})`, render: () => attemptCallList(base, data.calls, attempt, page) },
        {
          label: "task.log",
          render: async () => {
            const log = await api(`${base}/log`);
            return el(
              "div",
              {},
              log.truncated && el("p", { class: "caption", text: `Showing the end of a ${log.size.toLocaleString()} byte log.` }),
              el("pre", { class: "log", text: log.text }),
            );
          },
        },
        { label: "Status", render: () => (data.status_file ? rawJson(data.status_file) : el("div", { class: "empty", text: ATTEMPT_HELP.no_status })) },
      ],
    });
  } catch (error) {
    openDrawer({ title, subtitle: error.message, tabs: [] });
  }
}

// ---- Routing ----

// Redraw the page in place when new data arrives, keeping the reader's position.
function refreshing(render) {
  return async () => {
    const position = window.scrollY;
    try {
      await render();
      window.scrollTo(0, position);
    } catch {
      // The next change tries again. Keep showing what is already on the page.
    }
  };
}

// A task page is being read, so offer the update instead of redrawing under the reader.
function offerReload() {
  const slot = document.getElementById("new-data");
  if (!slot || slot.childElementCount) return;
  slot.append(
    el("button", { class: "link-button", type: "button", text: "New data: reload", onclick: route }),
  );
}

async function route() {
  const token = ++navigation;
  resetDrawer();
  selectedStep = null;
  const [path, query] = location.hash.replace(/^#/, "").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const params = new URLSearchParams(query);
  watch();
  try {
    show(el("div", { class: "empty", text: "Loading" }));
    if (parts[0] === "db" && parts[2]) {
      await renderRecordPage(view, parts[1], parts[2], setCrumbs);
    } else if (parts[0] === "db") {
      const tab = EXPLORER_TABS.includes(params.get("tab")) ? params.get("tab") : "users";
      await renderExplorer(view, tab, params.get("q") ?? "", setCrumbs);
    } else if (parts[0] === "designs") {
      await renderDesigns(view, params.get("a") || null, params.get("b") || null, setCrumbs);
    } else if (parts[0] === "compare") {
      const [a, b] = [params.get("a") || null, params.get("b") || null];
      const render = () => renderCompare(view, a, b, setCrumbs);
      await render();
      if (a && b) watch([a, b], refreshing(render));
    } else if (parts[0] === "run" && parts[2] === "task") {
      await renderTask(parts[1], parts[3], Number(params.get("trial") ?? 0));
      watch([parts[1]], offerReload);
    } else if (parts[0] === "run" && parts[2] === "design") {
      await renderRunDesign(view, parts[1], params.get("decision"), setCrumbs);
      if (params.get("decision")) return;
    } else if (parts[0] === "run") {
      await renderRun(parts[1]);
      watch([parts[1]], refreshing(() => renderRun(parts[1])));
    } else {
      await renderRuns();
      watch("all", refreshing(renderRuns));
    }
    if (token === navigation) window.scrollTo(0, 0);
  } catch (error) {
    if (token === navigation) showError(error);
  }
}

setOnClose(() => highlightStep(null));

// An ID opens beside the conversation, so the reader keeps their place. Ctrl+click
// and middle-click stay normal links, and explorer pages navigate as usual.
document.addEventListener("click", (event) => {
  const link = event.target.closest?.("a.id-link");
  if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) return;
  if (location.hash.startsWith("#/db")) return;
  event.preventDefault();
  openRecord(link.dataset.kind, link.dataset.id, link);
});

window.addEventListener("hashchange", route);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && isOpen()) closeDrawer();
});

route();
