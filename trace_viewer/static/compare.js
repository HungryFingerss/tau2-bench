// Compare two runs, task by task.

import { compareTotals } from "./agentcounts.js";
import { SHORT_NAMES } from "./checks.js";
import { designDifference } from "./design.js";
import { api, chip, el, enc, formatDate, runLink, selectRun, taskLink } from "./util.js";

const CHANGE_LABELS = {
  improved: "Improved",
  regressed: "Regressed",
  same: "Same",
  not_comparable: "Not comparable",
};
const CHANGE_CSS = { improved: "pass", regressed: "fail", same: "", not_comparable: "" };

let changesOnly = false;

function flagged(checks) {
  if (!checks) return [];
  return Object.entries(checks)
    .filter(([, check]) => check.state === "issue" || check.state === "review")
    .map(([id, check]) => (check.count > 1 ? `${SHORT_NAMES[id]} ${check.count}` : SHORT_NAMES[id]));
}

// CHECK events from the agent's own log, as "invalid_call 2".
function fired(agentChecks) {
  return Object.entries(agentChecks ?? {})
    .filter(([, count]) => count)
    .map(([kind, count]) => `${kind} ${count}`);
}

function callsAndSteps(side) {
  if (side.model_calls === undefined || side.model_calls === null) return "";
  const mark = side.in_results === false ? " *" : "";
  return `${side.model_calls} / ${side.benchmark_steps}${mark}`;
}

function sideCells(run, row, side) {
  if (!side) {
    return [el("td", { class: "subtle", text: "not in this run" }), el("td"), el("td"), el("td")];
  }
  return [
    el(
      "td",
      {},
      el("a", { href: taskLink(run, row.task_id, row.trial), "aria-label": `Task ${row.task_id} in ${run}` }, chip(side.result)),
    ),
    el("td", {
      class: "num nowrap",
      title: side.in_results === false ? "Counted from an attempt's log: results.json does not hold it yet." : "Agent model calls / benchmark steps",
      text: callsAndSteps(side),
    }),
    el("td", { class: "small", text: [...flagged(side.checks), ...fired(side.agent_checks)].join(", ") }),
    el("td", { class: "small", text: side.labels.join(", ") }),
  ];
}

function runHeader(side, title) {
  const rate = side.counts?.rate === null || !side.counts
    ? "no scored simulations"
    : `${side.counts.passed}/${side.counts.scored} (${Math.round(side.counts.rate * 100)}%)`;
  return el(
    "section",
    { class: "panel" },
    el("h3", { text: title }),
    el("h2", {}, el("a", { href: runLink(side.name), text: side.name })),
    el(
      "dl",
      { class: "kv" },
      el("dt", { text: "Pass rate" }),
      el("dd", { text: rate }),
      el("dt", { text: "Agent" }),
      el("dd", { class: "mono", text: side.agent ?? "unknown" }),
      el("dt", { text: "Design" }),
      el("dd", { class: "mono", text: `${side.design ?? "unknown"}${side.design_origin === "assumed" ? " (assumed)" : ""}` }),
      compareTotals(side),
      el("dt", { text: "Date" }),
      el("dd", { text: formatDate(side.date) }),
      side.note && el("dt", { text: "Note" }),
      side.note && el("dd", { text: side.note }),
    ),
  );
}

export async function renderCompare(view, first, second, setCrumbs) {
  setCrumbs({ label: "Compare runs" });
  document.title = "Compare runs · Trace Viewer";
  const { runs } = await api("/api/runs");
  const names = runs.map((run) => run.name);
  const go = (a, b) => {
    location.hash = `#/compare?a=${enc(a ?? "")}&b=${enc(b ?? "")}`;
  };
  const pickers = el(
    "div",
    { class: "toolbar" },
    selectRun(names, first, (value) => go(value, second), "First run"),
    selectRun(names, second, (value) => go(first, value), "Second run"),
  );
  const heading = el("div", { class: "page-head" }, el("h1", { text: "Compare runs" }));
  if (!first || !second) {
    view.replaceChildren(heading, pickers, el("p", { class: "caption", text: "Choose two runs." }));
    return;
  }

  const page = await api(`/api/compare?a=${enc(first)}&b=${enc(second)}`);
  const body = el("tbody");
  const shown = el("span", { class: "subtle small" });
  const paint = () => {
    const rows = changesOnly ? page.rows.filter((row) => row.change === "improved" || row.change === "regressed") : page.rows;
    body.replaceChildren(
      ...rows.map((row) =>
        el(
          "tr",
          {},
          el("td", { text: row.trial ? `${row.task_id}.${row.trial}` : row.task_id }),
          el("td", {}, el("span", { class: `chip ${CHANGE_CSS[row.change]}`, text: CHANGE_LABELS[row.change] })),
          sideCells(first, row, row.first),
          sideCells(second, row, row.second),
        ),
      ),
    );
    shown.textContent = `Showing ${rows.length} of ${page.rows.length}`;
  };
  paint();

  const designs = page.first.design === page.second.design
    ? el("p", {
        class: "caption",
        text: page.first.design ? `Both runs used ${page.first.design}.` : "The design of these runs is not known.",
      })
    : page.design_difference
      ? el("details", { class: "panel", open: true }, el("summary", { text: `Design difference: ${page.first.design} to ${page.second.design}` }), designDifference(page.design_difference))
      : el("p", { class: "caption", text: "These runs used different designs, and one of them has no file." });

  const counts = page.counts;
  view.replaceChildren(
    heading,
    pickers,
    el("div", { class: "compare-heads" }, runHeader(page.first, "First"), runHeader(page.second, "Second")),
    designs,
    el(
      "p",
      { class: "db-headline small-headline", text: `${counts.improved} improved, ${counts.regressed} regressed, ${counts.same} the same, ${counts.not_comparable} not comparable` },
    ),
    el(
      "div",
      { class: "toolbar" },
      el(
        "label",
        {},
        el("input", {
          type: "checkbox",
          checked: changesOnly,
          onchange: (event) => {
            changesOnly = event.target.checked;
            paint();
          },
        }),
        "Changes only",
      ),
      shown,
    ),
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        {},
        el(
          "thead",
          {},
          el(
            "tr",
            {},
            ["Task", "Change", "First", "Calls / steps", "Checks", "Labels", "Second", "Calls / steps", "Checks", "Labels"].map((text) =>
              el("th", {
                class: text === "Calls / steps" ? "help num" : undefined,
                title: text === "Calls / steps" ? "Agent model calls / benchmark steps. * means counted from an attempt's log that results.json does not hold yet." : undefined,
                text,
              }),
            ),
          ),
        ),
        body,
      ),
    ),
  );
}
