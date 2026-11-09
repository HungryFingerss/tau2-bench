// Agent designs: each run's design, the source behind a decision, and design comparison.

import { openDrawer } from "./drawer.js";
import { api, el, enc, keyValues, plural, pretty, runLink, taskChips } from "./util.js";

const STATUS_LABELS = { implemented: "Implemented", not_handled: "Not handled" };

function statusChip(status) {
  const css = status === "not_handled" ? "stopped" : "pass";
  return el("span", { class: `chip ${css}`, text: STATUS_LABELS[status] ?? status });
}

function sourceLabel(source) {
  const [first, last] = source.lines;
  const range = first === last ? `${first}` : `${first}-${last}`;
  return `${source.file.split("/").pop()}:${range}`;
}

export async function openSource(source, opener) {
  const { file, lines } = await api(`/api/source?file=${enc(source.file)}`);
  const [first, last] = source.lines;
  const rows = lines.map((line, index) => {
    const number = index + 1;
    const marked = number >= first && number <= last;
    return el(
      "div",
      { class: `source-line${marked ? " marked" : ""}`, id: number === first ? "source-target" : undefined },
      el("span", { class: "source-number", text: String(number) }),
      el("span", { class: "source-text", text: line || " " }),
    );
  });
  openDrawer({
    title: source.symbol ?? file.split("/").pop(),
    subtitle: `${file}, lines ${first} to ${last}`,
    opener,
    tabs: [{ label: "Source", render: () => el("div", { class: "source" }, rows) }],
  });
  requestAnimationFrame(() => document.getElementById("source-target")?.scrollIntoView({ block: "center" }));
}

function sourceLinks(sources) {
  return el(
    "div",
    { class: "source-links" },
    sources.map((source) =>
      el("button", {
        class: "link-button mono",
        type: "button",
        title: source.symbol ? `${source.symbol} in ${source.file}` : source.file,
        text: sourceLabel(source),
        onclick: (event) => openSource(source, event.currentTarget),
      }),
    ),
  );
}

function decisionCard(decision, focused) {
  return el(
    "article",
    {
      class: `decision panel${focused ? " focused" : ""}`,
      id: `decision-${decision.id}`,
    },
    el(
      "div",
      { class: "decision-head" },
      el("span", { class: "decision-id", text: decision.id }),
      el("h3", { class: "decision-name", text: decision.name }),
      statusChip(decision.status),
    ),
    el("p", { text: decision.what }),
    el(
      "dl",
      { class: "kv decision-facts" },
      el("dt", { text: "Code enforces" }),
      el("dd", { text: decision.code_enforces }),
      el("dt", { text: "Left to the model" }),
      el("dd", { text: decision.left_to_model }),
      decision.addresses && el("dt", { text: "Addresses" }),
      decision.addresses &&
        el("dd", { text: decision.addresses.length ? decision.addresses.join(", ") : "nothing in the baseline: new" }),
    ),
    sourceLinks(decision.source),
  );
}

function originNote(page) {
  if (page.origin === "recorded") return "This run recorded its design when it started.";
  if (page.origin === "assumed") {
    return `Assumed from the agent name ${page.agent_implementation}, because this run did not record a design.`;
  }
  if (page.origin === "missing") return `This run used ${page.missing_version}, but that design file no longer exists.`;
  return `No design file covers the agent ${page.agent_implementation}.`;
}

function promptNote(page) {
  if (page.prompt_source === "saved_call") {
    return page.prompt_matches_design
      ? "Taken from a saved model call. It matches what this design predicts, byte for byte."
      : "Taken from a saved model call. It does not match what this design predicts, so the agent may have changed.";
  }
  if (page.prompt_source === "rebuilt") {
    return "This run saved no model calls, so this text is rebuilt from the design and the run's policy.";
  }
  return "No prompt is available for this run.";
}

function promptSection(page) {
  const mismatch = page.prompt_matches_design === false;
  return el(
    "section",
    { class: "panel" },
    el("h2", { text: "Exact system prompt" }),
    el("p", { class: mismatch ? "notice" : "caption", text: promptNote(page) }),
    el(
      "details",
      {},
      el("summary", { text: `Show the prompt (${(page.prompt ?? "").length.toLocaleString()} characters)` }),
      el("pre", { class: "prompt-text", text: page.prompt ?? "" }),
    ),
  );
}

function parametersSection(page) {
  const parameters = page.parameters;
  return el(
    "section",
    { class: "panel" },
    el("h2", { text: "Parameters in this run" }),
    keyValues([
      ["Model", parameters.model],
      ["Agent arguments", parameters.llm_args && JSON.stringify(parameters.llm_args)],
      ["Call arguments", parameters.call_arguments && JSON.stringify(parameters.call_arguments)],
      ["Tool choice", parameters.tool_choice],
      ["Max steps", parameters.max_steps],
      ["Max errors", parameters.max_errors],
      ["Seed", parameters.seed],
    ]),
  );
}

function toolRows(page) {
  const kinds = Object.fromEntries((page.design?.tools ?? []).map((tool) => [tool.name, tool.kind]));
  const tools = (page.tools ?? page.design?.tools ?? []).map((tool) => tool.function ?? tool);
  return tools.map((tool) =>
    el(
      "tr",
      {},
      el("td", { class: "mono nowrap", text: tool.name }),
      el("td", { class: "nowrap", text: kinds[tool.name] ?? tool.kind ?? "" }),
      el(
        "td",
        {},
        el("details", {}, el("summary", { text: tool.description.split("\n")[0] }), el("pre", { text: tool.description }), el("pre", { text: pretty(tool.parameters) })),
      ),
    ),
  );
}

function toolsSection(page) {
  const rows = toolRows(page);
  return el(
    "section",
    { class: "panel" },
    el("h2", { text: `Tools (${rows.length})` }),
    el("p", {
      class: "caption",
      text: page.tools ? "As sent to the model in this run." : "From the design file, because this run saved no model calls.",
    }),
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        {},
        el("thead", {}, el("tr", {}, ["Tool", "Kind", "Description"].map((text) => el("th", { text })))),
        el("tbody", {}, rows),
      ),
    ),
  );
}

function staleNotice(status) {
  if (!status) return null;
  const files = [...status.changed, ...status.missing];
  if (!files.length) return null;
  return el("div", {
    class: "notice",
    text: `These files changed since this design was documented, so its line numbers may be off: ${files.join(", ")}`,
  });
}

export async function renderRunDesign(view, run, focus, setCrumbs) {
  setCrumbs({ label: run, href: runLink(run) }, { label: "Agent design" });
  document.title = `Agent design · ${run}`;
  const page = await api(`/api/runs/${enc(run)}/design`);
  const heading = el("div", { class: "page-head" }, el("h1", { text: "Agent design" }));
  if (!page.design) {
    view.replaceChildren(heading, el("p", { class: "notice", text: originNote(page) }));
    return;
  }

  const { design } = page;
  const notHandled = design.decisions.filter((d) => d.status === "not_handled").length;
  view.replaceChildren(
    ...[
      heading,
      el(
        "section",
        { class: "panel" },
        el("h2", {}, design.version, ": ", design.title),
        el("p", { text: design.description }),
        el("p", { class: "caption", text: originNote(page) }),
        el("p", {
          class: "caption",
          text: `${plural(design.decisions.length, "decision")}, ${notHandled} not handled.`,
        }),
      ),
      staleNotice(page.source_status),
      promptSection(page),
      parametersSection(page),
      toolsSection(page),
      el("h2", { class: "section-heading", text: "Design decisions" }),
      el(
        "div",
        { class: "decisions" },
        design.decisions.map((decision) => decisionCard(decision, decision.id === focus)),
      ),
    ].filter(Boolean),
  );
  if (focus) document.getElementById(`decision-${focus}`)?.scrollIntoView({ block: "center" });
}

function changeRow(label, content) {
  return el("div", { class: "design-change" }, el("h3", { text: label }), content);
}

function decisionList(decisions) {
  return el(
    "ul",
    { class: "db-records" },
    decisions.map((d) => el("li", {}, `${d.id}: ${d.name} `, statusChip(d.status))),
  );
}

function textChange(label, change) {
  return el(
    "details",
    { class: "design-change", open: true },
    el("summary", { text: `${label} changed` }),
    el("div", { class: "side-by-side" }, el("pre", { text: change.before ?? "" }), el("pre", { text: change.after ?? "" })),
  );
}

function decisionLink(run, decision) {
  const text = `${decision.id}: ${decision.name}`;
  return run
    ? el("a", { href: `${runLink(run)}/design?decision=${enc(decision.id)}`, title: decision.what, text })
    : el("span", { title: decision.what, text });
}

// Two designs whose decisions have different ids, read through `addresses`: each
// base decision beside what replaced it, and, when comparing runs, the failures
// each run linked to them.
function mappingTable(mapping) {
  const withRuns = mapping.base_run !== undefined;
  const failures = (tasks, run) =>
    tasks?.length ? taskChips(run, tasks) : el("span", { class: "subtle", text: "none" });
  const rows = mapping.rows.map((row) =>
    el(
      "tr",
      {},
      el("td", {}, decisionLink(mapping.base_run, row), " ", statusChip(row.status)),
      el(
        "td",
        {},
        row.replaced_by.length
          ? row.replaced_by.map((d, i) => [i ? el("br") : null, decisionLink(mapping.other_run, d)])
          : el("span", { class: "subtle", text: "nothing: the same as the benchmark does it" }),
      ),
      withRuns && el("td", {}, failures(row.base_failures, mapping.base_run)),
      withRuns && el("td", {}, row.replaced_by.length ? failures(row.other_failures, mapping.other_run) : ""),
    ),
  );
  const newRows = mapping.new.map((d) =>
    el(
      "tr",
      {},
      el("td", { class: "subtle", text: "(new)" }),
      el("td", {}, decisionLink(mapping.other_run, d)),
      withRuns && el("td"),
      withRuns && el("td", {}, failures(d.other_failures, mapping.other_run)),
    ),
  );
  const heads = [
    mapping.base,
    `${mapping.other} decision that addresses it`,
    ...(withRuns ? [`Failures linked in ${mapping.base_run}`, `Failures linked in ${mapping.other_run}`] : []),
  ];
  return changeRow(
    `How ${mapping.other} relates to ${mapping.base}`,
    el(
      "div",
      {},
      el("p", {
        class: "caption",
        text: `The decision ids differ, so they are matched through each ${mapping.other} decision's "addresses" list. ${mapping.replaced} of ${mapping.rows.length} ${mapping.base} decisions are addressed; ${mapping.new.length} ${mapping.other} decisions are new.${withRuns ? " Failures come from each run's failure labels and the decisions they link." : ""}`,
      }),
      el(
        "div",
        { class: "table-wrap" },
        el("table", {}, el("thead", {}, el("tr", {}, heads.map((text) => el("th", { text })))), el("tbody", {}, rows, newRows)),
      ),
    ),
  );
}

// Used on the design comparison page and when comparing two runs.
export function designDifference(difference) {
  const parts = [];
  if (difference.same) {
    parts.push(el("p", { class: "subtle", text: "These two designs are identical." }));
  }
  if (difference.mapping) {
    parts.push(mappingTable(difference.mapping));
    parts.push(
      el(
        "details",
        { class: "design-change" },
        el("summary", { text: `Decision lists by id: ${difference.removed.length} only in one, ${difference.added.length} only in the other` }),
        difference.removed.length > 0 && changeRow(`Only in the first (${difference.removed.length})`, decisionList(difference.removed)),
        difference.added.length > 0 && changeRow(`Only in the second (${difference.added.length})`, decisionList(difference.added)),
      ),
    );
  } else {
    if (difference.added.length) parts.push(changeRow(`Added (${difference.added.length})`, decisionList(difference.added)));
    if (difference.removed.length) parts.push(changeRow(`Removed (${difference.removed.length})`, decisionList(difference.removed)));
  }
  if (difference.changed.length) {
    parts.push(
      changeRow(
        `Changed (${difference.changed.length})`,
        el(
          "div",
          {},
          difference.changed.map((item) =>
            el(
              "details",
              { class: "design-change" },
              el("summary", { text: `${item.id}: ${item.after.name} (${item.fields.join(", ")})` }),
              item.fields.map((field) =>
                el(
                  "div",
                  {},
                  el("h3", { text: field }),
                  el(
                    "div",
                    { class: "side-by-side" },
                    el("pre", { text: item.before[field] }),
                    el("pre", { text: item.after[field] }),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
  if (difference.moved.length) {
    parts.push(changeRow("Same decision, new source location", el("p", { class: "mono", text: difference.moved.join(", ") })));
  }
  if (difference.instruction_changed) parts.push(textChange("Instruction", difference.instruction));
  if (difference.template_changed) parts.push(textChange("Prompt template", difference.template));
  const tools = [
    ["Tools added", difference.tools_added],
    ["Tools removed", difference.tools_removed],
    ["Tools changed", difference.tools_changed],
  ].filter(([, names]) => names.length);
  tools.forEach(([label, names]) => parts.push(changeRow(label, el("p", { class: "mono", text: names.join(", ") }))));
  if (difference.parameters_changed.length) {
    parts.push(
      changeRow(
        "Parameters changed",
        el(
          "dl",
          { class: "kv" },
          difference.parameters_changed.flatMap((item) => [
            el("dt", { class: "mono", text: item.name }),
            el("dd", { text: `${item.before ?? "(not set)"} became ${item.after ?? "(not set)"}` }),
          ]),
        ),
      ),
    );
  }
  return el("div", {}, parts);
}

export async function renderDesigns(view, first, second, setCrumbs) {
  setCrumbs({ label: "Agent designs" });
  document.title = "Agent designs · Trace Viewer";
  const { designs, problems } = await api("/api/designs");
  const go = (a, b) => {
    location.hash = `#/designs?a=${enc(a ?? "")}&b=${enc(b ?? "")}`;
  };
  const picker = (selected, onchange, label) =>
    el(
      "select",
      { class: "run-select", "aria-label": label, onchange: (event) => onchange(event.target.value) },
      el("option", { value: "", text: label }),
      designs.map((design) => el("option", { value: design.version, selected: design.version === selected, text: design.version })),
    );

  const content = el("div");
  if (designs.length < 2) {
    content.append(el("p", {
      class: "caption",
      text: "Comparison needs two designs. Add another file to trace_viewer/designs/ and it appears here.",
    }));
  } else if (first && second) {
    const difference = await api(`/api/designs/compare?a=${enc(first)}&b=${enc(second)}`);
    content.append(el("section", { class: "panel" }, designDifference(difference)));
  }

  view.replaceChildren(
    el("div", { class: "page-head" }, el("h1", { text: "Agent designs" })),
    ...problems.map((text) => el("div", { class: "notice", text })),
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        {},
        el("thead", {}, el("tr", {}, ["Version", "Title", "Decisions", "Source"].map((text) => el("th", { text })))),
        el(
          "tbody",
          {},
          designs.map((design) => {
            const stale = design.source_status.changed.length + design.source_status.missing.length;
            return el(
              "tr",
              {},
              el("td", { class: "mono", text: design.version }),
              el("td", { text: design.title }),
              el("td", { class: "num", text: String(design.decisions) }),
              el("td", { class: stale ? "" : "subtle", text: stale ? `${plural(stale, "file")} changed since documented` : "Unchanged" }),
            );
          }),
        ),
      ),
    ),
    designs.length > 1 &&
      el(
        "div",
        { class: "toolbar" },
        picker(first, (value) => go(value, second), "First design"),
        picker(second, (value) => go(first, value), "Second design"),
      ),
    content,
  );
}
