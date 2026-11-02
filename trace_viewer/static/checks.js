// The automatic checks: on a task page, and as a table for the whole run.

import { el, plural, taskLink } from "./util.js";

const STATE_LABELS = { ok: "OK", issue: "Flagged", review: "Needs review", na: "Not applicable" };
const STATE_CSS = { ok: "pass", issue: "fail", review: "stopped", na: "" };

export const SHORT_NAMES = {
  identity_first: "Identity",
  duplicates: "Duplicates",
  invalid: "Invalid",
  tool_errors: "Errors",
  write_without_yes: "No yes",
};

function stateChip(state, text) {
  return el("span", { class: `chip ${STATE_CSS[state]}`, text: text ?? STATE_LABELS[state] });
}

function evidenceList(check, onJump) {
  if (!check.evidence.length) return null;
  return el(
    "ul",
    { class: "check-evidence" },
    check.evidence.slice(0, 12).map((item) =>
      el(
        "li",
        {},
        el("button", {
          class: "link-button",
          type: "button",
          text: `Step ${item.step}`,
          onclick: () => onJump(item.step),
        }),
        " ",
        item.text,
      ),
    ),
    check.evidence.length > 12 && el("li", { class: "subtle", text: `and ${check.evidence.length - 12} more` }),
  );
}

export function checksPanel(page, onJump) {
  const panel = el("section", { class: "panel" }, el("h2", { text: "Automatic checks" }));
  page.checks.forEach((check) => {
    panel.append(
      el(
        "div",
        { class: "check" },
        el(
          "div",
          { class: "check-head" },
          stateChip(check.state),
          el("span", { class: "check-name", text: check.name }),
        ),
        el("p", { class: "small", text: check.summary }),
        el("p", { class: "caption", text: check.definition }),
        evidenceList(check, onJump),
      ),
    );
  });
  return panel;
}

function cell(check) {
  if (!check) return el("td", { class: "subtle", text: "" });
  if (check.state === "na") return el("td", { class: "subtle", text: "n/a" });
  if (check.state === "ok") return el("td", {}, stateChip("ok"));
  const text = check.count > 1 ? `${STATE_LABELS[check.state]}: ${check.count}` : STATE_LABELS[check.state];
  return el("td", {}, stateChip(check.state, text));
}

export function checksTab(page, run) {
  const definitions = page.check_definitions;
  const totals = el(
    "div",
    { class: "check-totals" },
    page.check_totals.map((total) =>
      el(
        "div",
        { class: "panel" },
        el("h3", { text: total.name }),
        el("p", {
          class: "db-headline small-headline",
          text: `${total.tasks_flagged} of ${plural(total.tasks_checked, "task")}${total.total > total.tasks_flagged ? `, ${total.total} in all` : ""}`,
        }),
        el("p", { class: "caption", text: total.definition }),
      ),
    ),
  );
  const rows = page.rows
    .filter((row) => row.checks)
    .map((row) =>
      el(
        "tr",
        {},
        el("td", {}, el("a", { href: taskLink(run, row.task_id, row.trial), text: row.task_id })),
        definitions.map((definition) => cell(row.checks[definition.id])),
      ),
    );
  return el(
    "div",
    {},
    totals,
    el(
      "div",
      { class: "table-wrap", style: "margin-top:12px" },
      el(
        "table",
        {},
        el(
          "thead",
          {},
          el(
            "tr",
            {},
            el("th", { text: "Task" }),
            definitions.map((d) => el("th", { class: "help", title: d.definition, text: d.name })),
          ),
        ),
        el("tbody", {}, rows),
      ),
    ),
  );
}
