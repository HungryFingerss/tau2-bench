// Failure labels: assigning them on a task, and the run's "What's breaking" summary.

import { el, enc, plural, post, runLink, taskChips } from "./util.js";

// "addresses D3, D14": the baseline decisions a decision replaces, if its design says.
function addressesText(decision) {
  return decision?.addresses?.length ? ` (addresses ${decision.addresses.join(", ")})` : "";
}

function decisionName(page, id) {
  const decision = page.design_decisions.find((d) => d.id === id);
  return decision ? `${decision.name}${addressesText(decision)}` : "Not in this run's design";
}

// The label editor. Suggestions never apply themselves: a click confirms each one.
export function labelsPanel(page) {
  const panel = el("section", { class: "panel", id: "failure-labels" }, el("h2", { text: "Failure labels" }));
  if (page.labels_problem) {
    panel.append(el("p", { class: "notice", text: page.labels_problem }));
    return panel;
  }

  const chosen = new Map(
    (page.annotation?.labels ?? []).map((item) => [item.id, new Set(item.decisions)]),
  );
  const noteBox = el("textarea", {
    class: "note-box",
    rows: 3,
    placeholder: "What went wrong, in your own words",
    "aria-label": "Note",
  });
  noteBox.value = page.annotation?.note ?? "";
  const status = el("span", { class: "subtle small", role: "status" });
  const suggestions = el("div");
  const list = el("div");

  const markUnsaved = () => {
    status.textContent = "Not saved yet";
  };

  const add = (id) => {
    chosen.set(id, new Set(page.label_links[id] ?? []));
    markUnsaved();
    paint();
  };

  const decisionRow = (labelId) => {
    const decisions = chosen.get(labelId);
    const available = page.design_decisions.filter((d) => !decisions.has(d.id));
    return el(
      "div",
      { class: "label-decisions" },
      [...decisions].sort().map((id) =>
        el(
          "span",
          { class: "decision-chip", title: decisionName(page, id) },
          id,
          el("button", {
            type: "button",
            "aria-label": `Remove ${id}`,
            text: "×",
            onclick: () => {
              decisions.delete(id);
              markUnsaved();
              paint();
            },
          }),
        ),
      ),
      page.design_decisions.length > 0 &&
        el(
          "select",
          {
            class: "decision-add",
            "aria-label": "Link a design decision",
            onchange: (event) => {
              if (!event.target.value) return;
              decisions.add(event.target.value);
              markUnsaved();
              paint();
            },
          },
          el("option", { value: "", text: "Link a decision" }),
          available.map((d) => el("option", { value: d.id, text: `${d.id}: ${d.name}${addressesText(d)}` })),
        ),
    );
  };

  const paint = () => {
    const open = page.suggested_labels.filter((item) => !chosen.has(item.label));
    suggestions.replaceChildren(
      ...open.map((item) =>
        el(
          "div",
          { class: "suggestion" },
          el("button", {
            class: "link-button",
            type: "button",
            text: `Add ${item.label}`,
            onclick: () => add(item.label),
          }),
          el("span", { class: "subtle small", text: ` Suggested by: ${item.because.join(", ")}` }),
        ),
      ),
    );
    list.replaceChildren(
      ...page.labels.map((label) => {
        const checked = chosen.has(label.id);
        return el(
          "div",
          { class: "label-row" },
          el(
            "label",
            { title: label.description },
            el("input", {
              type: "checkbox",
              checked,
              onchange: (event) => {
                if (event.target.checked) add(label.id);
                else {
                  chosen.delete(label.id);
                  markUnsaved();
                  paint();
                }
              },
            }),
            label.name,
          ),
          checked && el("p", { class: "caption", text: label.description }),
          checked && decisionRow(label.id),
        );
      }),
    );
  };
  paint();
  noteBox.addEventListener("input", markUnsaved);

  const save = async () => {
    status.textContent = "Saving";
    const labels = [...chosen].map(([id, decisions]) => ({ id, decisions: [...decisions].sort() }));
    const url = `/api/runs/${enc(page.run)}/tasks/${enc(page.task_id)}/annotation?trial=${page.trial}`;
    try {
      await post(url, { labels, note: noteBox.value });
      status.textContent = "Saved";
    } catch (error) {
      status.textContent = `Not saved: ${error.message}`;
    }
  };

  if (page.annotation_is_stale) {
    panel.append(
      el("div", {
        class: "notice",
        text: "These labels were saved against an earlier simulation of this task, which a resumed run has since replaced.",
      }),
    );
  }
  panel.append(
    suggestions,
    list,
    el("div", { class: "field" }, el("h3", { text: "Note" }), noteBox),
    el(
      "div",
      { class: "toolbar" },
      el("button", { class: "link-button", type: "button", text: "Save", onclick: save }),
      status,
    ),
  );
  return panel;
}

function bucketRows(run, buckets, nameCell) {
  return buckets.map((bucket) =>
    el(
      "tr",
      {},
      nameCell(bucket),
      el("td", { class: "num" }, el("strong", { text: String(bucket.failures.length) })),
      el("td", {}, taskChips(run, bucket.failures)),
      el(
        "td",
        { class: "subtle" },
        bucket.passed.length > 0 && [`${bucket.passed.length} passed: `, taskChips(run, bucket.passed)],
      ),
    ),
  );
}

function bucketTable(heading, firstColumn, rows) {
  return el(
    "div",
    {},
    el("h3", { text: heading }),
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
            el("th", { text: firstColumn }),
            el("th", { class: "num", text: "Failures" }),
            el("th", { text: "Tasks" }),
            el("th", { class: "help", title: "Passed tasks that still carry this label.", text: "Also on" }),
          ),
        ),
        el("tbody", {}, rows),
      ),
    ),
  );
}

export function whatsBreaking(page, run) {
  const { analysis } = page;
  const section = el("section", { class: "breaking" }, el("h2", { text: "What's breaking" }));
  if (page.labels_problem) section.append(el("p", { class: "notice", text: page.labels_problem }));

  if (analysis.failures === 0 && !analysis.by_label.length) {
    section.append(el("p", { class: "subtle small", text: "No failures in this run." }));
    return section;
  }

  section.append(
    el(
      "p",
      { class: "small" },
      `${analysis.labelled} of ${plural(analysis.failures, "failure")} labelled.`,
      analysis.unlabelled.length > 0 && [" Still to label: ", taskChips(run, analysis.unlabelled)],
    ),
  );
  if (!analysis.by_label.length) return section;

  const design = page.design?.version;
  const decisionCell = (bucket) => {
    const href = `${runLink(run)}/design?decision=${enc(bucket.id)}`;
    return el(
      "td",
      {},
      el("a", { href: design ? href : undefined, text: bucket.id }),
      ` ${bucket.name}`,
      bucket.addresses?.length > 0 && el("span", { class: "subtle small", text: addressesText(bucket) }),
      bucket.status === "not_handled" && el("span", { class: "chip plain not-handled", text: "not handled" }),
    );
  };
  section.append(
    el(
      "div",
      { class: "breaking-tables" },
      analysis.by_decision.length > 0
        ? bucketTable("By design decision", "Decision", bucketRows(run, analysis.by_decision, decisionCell))
        : el("p", { class: "subtle small", text: "No labels are linked to a design decision yet." }),
      bucketTable(
        "By failure label",
        "Label",
        bucketRows(run, analysis.by_label, (bucket) => el("td", { text: bucket.name })),
      ),
    ),
  );
  return section;
}
