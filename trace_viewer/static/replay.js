// The database difference: expected and agent final states, from the benchmark's
// own evaluator run by replay_worker.py.

import { openDrawer } from "./drawer.js";
import { linked, recordLink } from "./ids.js";
import { api, el, enc, keyValues, plural, pretty } from "./util.js";

const KIND_LABELS = {
  missed: "Agent missed this",
  added: "Agent added this",
  different: "Agent did it differently",
  both: "Same on both sides",
};
const KIND_HELP = {
  missed: "A change was expected here, and the agent left the initial value.",
  added: "This was expected to stay as it was, and the agent changed it.",
  different: "Both changed it, to different values.",
  both: "The agent and the expected path ended on the same value.",
};
const KIND_CSS = { missed: "stopped", added: "infra", different: "fail", both: "" };

const COLLECTIONS = { orders: "order", users: "user", products: "product" };

function kindChip(kind) {
  return el("span", { class: `chip ${KIND_CSS[kind]}`, title: KIND_HELP[kind], text: KIND_LABELS[kind] });
}

function recordTitle(record) {
  const kind = COLLECTIONS[record.collection];
  return kind ? recordLink(kind, record.id) : record.id;
}

function outcomeText(summary) {
  const total = summary.fields_differ;
  if (summary.missed === total) return "agent missed this";
  if (summary.added === total) return "agent added this";
  if (summary.different === total) return "agent did it differently";
  const parts = [];
  if (summary.missed) parts.push(`${summary.missed} missed`);
  if (summary.added) parts.push(`${summary.added} added`);
  if (summary.different) parts.push(`${summary.different} done differently`);
  return parts.join(", ");
}

function headline(diff) {
  const { summary } = diff;
  if (diff.db_match) {
    return summary.records_changed
      ? `Both ended the same: ${plural(summary.records_changed, "record")} changed`
      : "Both ended the same: nothing changed";
  }
  const fields = plural(summary.fields_differ, "field");
  const records = plural(summary.records_differ, "record");
  return `${fields} ${summary.fields_differ === 1 ? "differs" : "differ"} in ${records}: ${outcomeText(summary)}`;
}

function formatValue(row, name) {
  if (row.absent.includes(name)) return el("span", { class: "subtle", text: "(absent)" });
  return el("pre", { class: "db-value" }, linked(valueText(row[name])));
}

function valueText(value) {
  if (Array.isArray(value) && value.every((item) => typeof item !== "object")) {
    return `[${value.map((item) => JSON.stringify(item)).join(", ")}]`;
  }
  return value !== null && typeof value === "object" ? pretty(value) : JSON.stringify(value);
}

function fieldRow(row) {
  const name = row.kind === "both" ? "agent" : null;
  return el(
    "tr",
    {},
    el("td", { class: "mono" }, row.field),
    el("td", {}, formatValue(row, "initial")),
    el("td", {}, formatValue(row, name ?? "expected")),
    el("td", {}, formatValue(row, name ?? "agent")),
    el("td", {}, kindChip(row.kind)),
  );
}

function fieldTable(rows) {
  return el(
    "div",
    { class: "table-wrap" },
    el(
      "table",
      {},
      el(
        "thead",
        {},
        el("tr", {}, ["Field", "Initial", "Expected", "Agent", "Result"].map((label) => el("th", { text: label }))),
      ),
      el("tbody", {}, rows.map(fieldRow)),
    ),
  );
}

function differencesTab(diff) {
  if (!diff.records.length) {
    return el("div", { class: "empty", text: "Neither the agent nor the expected path changed the database." });
  }
  return el(
    "div",
    { class: "db-record" },
    el("p", { class: "db-headline", text: headline(diff) }),
    diff.records.map((record) => {
      const different = record.fields.filter((row) => row.kind !== "both");
      const same = record.fields.filter((row) => row.kind === "both");
      return el(
        "section",
        { class: "db-section" },
        el("h3", {}, record.collection.replace(/s$/, ""), " ", recordTitle(record)),
        different.length > 0 && fieldTable(different),
        same.length > 0 &&
          (different.length
            ? el(
                "details",
                { class: "field" },
                el("summary", { text: `Also changed, the same on both sides (${same.length})` }),
                fieldTable(same),
              )
            : fieldTable(same)),
      );
    }),
  );
}

function finalStateTab(diff, side) {
  if (!diff.records.length) {
    return el("div", { class: "empty", text: "No record changed, so this state equals db.json." });
  }
  return el(
    "div",
    {},
    el("p", {
      class: "caption",
      text: "Every record that changed on either side, in full. All other records equal db.json.",
    }),
    diff.records.map((record) =>
      el(
        "section",
        { class: "db-section" },
        el("h3", {}, record.collection.replace(/s$/, ""), " ", recordTitle(record)),
        record[side]
          ? el("pre", {}, linked(pretty(record[side])))
          : el("p", { class: "subtle", text: "This record does not exist in this state." }),
      ),
    ),
  );
}

function verdictText(value) {
  if (value === null || value === undefined) return "not scored by the benchmark";
  return value ? "match" : "no match";
}

function evidenceTab(diff) {
  return el(
    "div",
    { class: "db-record" },
    !diff.agrees &&
      el("div", {
        class: "notice",
        text: "This replay does not reproduce the verdict in results.json. The benchmark code or data may have changed since the run, so read this difference with care.",
      }),
    el("p", {
      class: "caption",
      text: "Produced by the benchmark's own EnvironmentEvaluator, running under the benchmark's .venv. Hashes are the benchmark's own database hashes.",
    }),
    keyValues([
      ["Replay verdict", verdictText(diff.db_match)],
      ["results.json verdict", verdictText(diff.recorded_db_match)],
      ["Agrees with results.json", diff.recorded_db_match === null ? "not applicable" : diff.agrees ? "yes" : "NO"],
      ["Started from db.json", diff.initial_matches_db_json ? "yes" : "NO"],
      ["Expected hash", diff.expected_hash],
      ["Agent hash", diff.agent_hash],
      ["Initial hash", diff.initial_hash],
    ]),
    el("h3", { style: "margin-top:12px", text: `Replay notes (${diff.notes.length})` }),
    diff.notes.length
      ? diff.notes.map((note) => el("pre", { class: "db-note" }, linked(note)))
      : el("p", { class: "subtle small", text: "No warnings from the benchmark during replay." }),
  );
}

function openDifference(page, diff, opener) {
  openDrawer({
    title: `Database difference, task ${page.task_id}`,
    subtitle: headline(diff),
    opener,
    tabs: [
      { label: "Differences", render: () => differencesTab(diff) },
      { label: "Expected final state", render: () => finalStateTab(diff, "expected") },
      { label: "Agent's final state", render: () => finalStateTab(diff, "agent") },
      { label: "Evidence", render: () => evidenceTab(diff) },
    ],
  });
}

function resultPanel(panel, page, diff, sim) {
  const open = (opener) => openDifference(page, diff, opener);
  const failedActions = diff.notes.filter((note) => note.startsWith("Error in golden actions")).length;
  const content = [
    !diff.agrees &&
      el("div", { class: "notice", text: "Replay does not reproduce the verdict in results.json. See Evidence." }),
    el("p", { class: "db-headline small-headline", text: headline(diff) }),
    sim.db === "not_evaluated" &&
      el("p", { class: "caption", text: "The benchmark never scored this database, because the run stopped early. This comes from replaying what the agent did." }),
    diff.records.length > 0 &&
      el(
        "ul",
        { class: "db-records" },
        diff.records.map((record) =>
          el(
            "li",
            {},
            recordTitle(record),
            ` ${record.differs ? `${plural(record.fields.filter((row) => row.kind !== "both").length, "field")} differ` : "changed the same way"}`,
          ),
        ),
      ),
    failedActions > 0 &&
      el("p", { class: "caption", text: `${plural(failedActions, "reference action")} failed during replay. See Evidence.` }),
    el(
      "div",
      { class: "toolbar" },
      el("button", {
        class: "link-button",
        type: "button",
        text: "Open database difference",
        onclick: (event) => open(event.currentTarget),
      }),
    ),
  ];
  panel.append(...content.filter(Boolean)); // append() would print false as text.
}

// Fills itself in, so a first-time replay never holds up the page.
export function differencePanel(page) {
  const panel = el("section", { class: "panel", id: "db-difference" }, el("h2", { text: "Database difference" }));
  const waiting = el("p", {
    class: "subtle small",
    text: "Replaying with the benchmark's own code. The first time takes a few seconds.",
  });
  panel.append(waiting);

  page.dbDifference = api(`/api/runs/${enc(page.run)}/sims/${enc(page.sim.id)}/db-diff`)
    .then((diff) => {
      waiting.remove();
      if (diff.available) {
        resultPanel(panel, page, diff, page.sim);
      } else {
        const css = diff.failed ? "notice" : "subtle small";
        panel.append(el("p", { class: css, text: diff.reason }));
      }
      return diff;
    })
    .catch((error) => {
      waiting.remove();
      panel.append(el("p", { class: "notice", text: `Replay failed: ${error.message}` }));
      return null;
    });
  return panel;
}

export async function showDifference(page, opener) {
  const diff = await page.dbDifference;
  if (diff?.available) openDifference(page, diff, opener);
  else document.getElementById("db-difference")?.scrollIntoView({ behavior: "smooth", block: "center" });
}
