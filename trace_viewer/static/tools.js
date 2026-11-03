// The Tools page: run fde_agent's tools by hand, playing both the model and the customer.
//
// The session is a list of steps kept here. Every action sends the whole list, and the
// server replays it on a fresh in-memory copy of db.json, so the page never holds
// state the server could disagree with. Tool names, descriptions and parameters come
// from the tool modules through /api/tools, never from this file.

import { openDrawer } from "./drawer.js";
import { linked, loadIds, recordLink, setLinking } from "./ids.js";
import { api, el, plural, pretty } from "./util.js";

const STORAGE_KEY = "trace-viewer-tools-steps";
const LONG_RESULT = 600;
const YES_CHECK_DELAY_MS = 250;
const RECORD_KINDS = { users: "user", orders: "order", products: "product" };
const OUTCOMES = {
  failed: ["fail", "failed"],
  error: ["fail", "error"],
  passed: ["pass", "passed"],
  skipped: ["", "skipped"],
};

const state = {
  catalog: null, // the /api/tools reply
  steps: [], // what the server replays, in order
  reply: null, // the last successful /api/tools/session reply
  selected: null, // tool shown in the picker
  notesAt: null, // step whose notes are shown; null means the latest
  busy: false,
  error: null,
  testRun: null, // the last /api/tools/tests reply
  testsRunning: false,
};

let ui = null; // the page's live nodes while it is on screen

// ---- Session ----

function savedSteps() {
  try {
    const steps = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "[]");
    return Array.isArray(steps) ? steps : [];
  } catch {
    return [];
  }
}

function saveSteps() {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state.steps));
  } catch {
    // Storage can be off. The session still works until the page is reloaded.
  }
}

async function replay(steps) {
  const response = await fetch("/api/tools/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ steps }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(body.detail)
      ? body.detail.map((item) => `${(item.loc ?? []).slice(2).join(".")} ${item.msg}`).join("; ")
      : body.detail;
    throw new Error(detail || `Request failed: ${response.status}`);
  }
  if (!body.ok) throw new Error(body.error || "The tools worker failed.");
  return body;
}

// Replace the session with `steps`, or keep the old one if the server refuses.
async function act(steps) {
  if (state.busy) return false;
  state.busy = true;
  state.error = null;
  draw();
  let done = false;
  try {
    state.reply = steps.length ? await replay(steps) : null;
    state.steps = steps;
    state.notesAt = null;
    saveSteps();
    done = true;
  } catch (error) {
    state.error = error.message;
  }
  state.busy = false;
  draw();
  if (done && steps.length) ui?.timeline.lastElementChild?.scrollIntoView({ block: "nearest" });
  return done;
}

const addStep = (step) => act([...state.steps, step]);

// ---- Timeline ----

function chipNode(css, text, title) {
  return el("span", { class: `chip ${css}`, text, title });
}

function stepHead(index, label, extras = []) {
  return el(
    "div",
    { class: "step-head" },
    el("span", { class: "step-label", text: label }),
    el("span", { text: `#${index + 1}` }),
    extras,
    el("span", { class: "spacer" }),
    el("button", {
      class: "link-button",
      type: "button",
      text: "Notes after this",
      onclick: () => {
        state.notesAt = index;
        drawSide();
      },
    }),
  );
}

function prettyResult(text) {
  try {
    const parsed = JSON.parse(text);
    return typeof parsed === "object" && parsed !== null ? pretty(parsed) : text;
  } catch {
    return text;
  }
}

function callCard(call) {
  const output = prettyResult(call.result ?? "");
  const result =
    output.length > LONG_RESULT && !call.error
      ? el(
          "details",
          {},
          el("summary", { text: `Show result (${output.length.toLocaleString()} characters)` }),
          el("pre", {}, linked(output)),
        )
      : el("pre", {}, linked(output));
  return el(
    "div",
    { class: `tool ${call.error ? "error" : ""}` },
    el(
      "div",
      { class: "tool-section" },
      el(
        "div",
        { class: "call-head" },
        el("span", { class: "tool-name", text: call.name }),
        call.write &&
          chipNode("stopped", "WRITE", "The store marks this tool as one that changes the database."),
        call.error && chipNode("fail", "error"),
      ),
      el("pre", {}, linked(pretty(call.args))),
    ),
    el(
      "div",
      { class: "tool-section" },
      el("div", { class: "tool-kind result", text: call.error ? "Store error" : "Store result" }),
      result,
    ),
  );
}

function problem(result) {
  return [
    el("div", { class: "notice" }, result.problem),
    result.traceback &&
      el("details", {}, el("summary", { text: "Traceback" }), el("pre", { class: "small" }, result.traceback)),
  ];
}

function agentMessage(index, label, text) {
  return el(
    "article",
    { class: "step agent" },
    el(
      "div",
      { class: "step-head" },
      el("span", { class: "step-label", text: label }),
      el("span", { text: `after #${index + 1}` }),
    ),
    el("div", { class: "step-text" }, linked(text)),
  );
}

function toolCards(result) {
  const { index, step } = result;
  const calls = result.calls ?? [];
  const writes = calls.filter((call) => call.write).length;
  const hasText = typeof result.text === "string";
  const card = el(
    "article",
    { class: "step agent tool-step" },
    stepHead(index, `Model calls ${step.tool}`, [
      hasText && chipNode(result.ok ? "pass" : "fail", result.ok ? "ok" : "error"),
      el("span", { text: plural(calls.length, "store call") }),
      writes > 0 && chipNode("stopped", `${writes} WRITE`),
    ]),
    el("div", { class: "caption", text: "Inputs" }),
    el("pre", { class: "inputs" }, linked(pretty(step.inputs ?? {}))),
    hasText &&
      el(
        "div",
        { class: `model-sees ${result.ok ? "ok" : "error"}` },
        el("div", { class: "model-sees-label", text: "The model sees" }),
        el("div", { class: "step-text" }, linked(result.text)),
      ),
    result.problem && problem(result),
    calls.map(callCard),
  );
  const nodes = [card];
  if (result.agent_message) {
    nodes.push(agentMessage(index, "Code sends the customer", result.agent_message));
  }
  return nodes;
}

function showCards(result) {
  const { index, step } = result;
  if (result.ok) {
    return [
      el(
        "article",
        { class: "step agent" },
        stepHead(index, `Code shows change #${step.number} to the customer`),
        el("div", { class: "step-text" }, linked(result.agent_message)),
      ),
    ];
  }
  return [
    el(
      "article",
      { class: "step" },
      stepHead(index, `Show change #${step.number}`, [chipNode("fail", "not shown")]),
      problem(result),
    ),
  ];
}

function sayCards(result) {
  const { index, step } = result;
  return [
    el(
      "article",
      { class: "step customer" },
      stepHead(index, "Customer", [
        chipNode(result.clear_yes ? "pass" : "fail", result.clear_yes ? "clear yes" : "not a clear yes"),
        result.counts_as_reply
          ? chipNode("plain", "the reply to the shown change")
          : chipNode(
              "plain",
              "not a reply",
              "Notes keep a message as the reply only after code has shown the prepared change.",
            ),
      ]),
      el("div", { class: "step-text" }, linked(step.text)),
      result.problem && problem(result),
    ),
  ];
}

function drawTimeline() {
  const results = state.reply?.steps ?? [];
  const cards = results.flatMap((result) => {
    if (result.step.type === "tool") return toolCards(result);
    if (result.step.type === "show_change") return showCards(result);
    return sayCards(result);
  });
  ui.timeline.replaceChildren(
    ...(cards.length ? cards : [el("div", { class: "empty", text: "No steps yet. Run a tool below." })]),
  );
}

// ---- Notes and database changes ----

function nothing(text = "none") {
  return el("span", { class: "subtle", text });
}

function mapTable(map, link) {
  const entries = Object.entries(map ?? {});
  if (!entries.length) return nothing();
  return el(
    "table",
    { class: "notes-table" },
    el(
      "tbody",
      {},
      entries.map(([id, value]) => el("tr", {}, el("td", { class: "nowrap" }, link(id)), el("td", { text: value }))),
    ),
  );
}

// Fields with a better view than JSON. Every other field, including ones added to
// Notes later, is shown as it comes.
const NOTE_VIEWS = {
  customer: (value) => (value ? [recordLink("user", value.user_id), ` ${value.name}`] : nothing("not verified")),
  orders: (value) => mapTable(value, (id) => recordLink("order", id)),
  payment_methods: (value) => mapTable(value, (id) => recordLink("payment", id)),
  seen: (value) => {
    if (!Array.isArray(value?._seen)) return el("pre", { text: pretty(value) });
    if (!value._seen.length) return nothing();
    return el(
      "div",
      { class: "seen-ids" },
      value._seen.map(([kind, id]) => el("span", { class: "mono small" }, linked(`${kind}_${id}`))),
    );
  },
  prepared: (value) =>
    value
      ? el(
          "div",
          {},
          el("div", {}, `#${value.number} `, el("span", { class: "mono", text: value.kind })),
          el("pre", { class: "small" }, linked(`${value.call?.name} ${pretty(value.call?.args)}`)),
          el("div", { class: "prepared-text" }, linked(value.customer_text)),
        )
      : nothing(),
};

function genericNote(value) {
  if (value === null || value === undefined) return nothing();
  if (Array.isArray(value) && !value.length) return nothing();
  if (Array.isArray(value) && value.every((item) => item === null || typeof item !== "object")) {
    return el("span", { class: "mono", text: value.join(", ") });
  }
  if (typeof value === "object") return el("pre", { class: "small", text: pretty(value) });
  return el("span", { class: "mono", text: String(value) });
}

function notesList(notes, previous) {
  const rows = Object.entries(notes).flatMap(([key, value]) => {
    const changed = previous !== null && JSON.stringify(previous[key]) !== JSON.stringify(value);
    const css = changed ? "changed" : "";
    const render = NOTE_VIEWS[key] ?? genericNote;
    return [
      el("dt", { class: css, text: key, title: changed ? "Changed by this step" : undefined }),
      el("dd", { class: css }, render(value)),
    ];
  });
  return el("dl", { class: "kv notes" }, rows);
}

function notesPanel() {
  const results = state.reply?.steps ?? [];
  const at = state.notesAt ?? results.length - 1;
  const heading = el("div", { class: "field-head" }, el("h2", { text: "Notes" }));
  if (at < 0) return el("section", { class: "panel" }, heading, el("p", { class: "subtle small", text: "No steps yet." }));
  const result = results[at];
  const latest = state.notesAt === null || at === results.length - 1;
  heading.append(
    latest
      ? el("span", { class: "subtle small", text: `after step #${at + 1} (latest)` })
      : el("button", {
          class: "link-button",
          type: "button",
          text: "Show latest",
          onclick: () => {
            state.notesAt = null;
            drawSide();
          },
        }),
  );
  return el(
    "section",
    { class: "panel" },
    heading,
    !latest && el("p", { class: "caption", text: `After step #${at + 1}. Highlighted: changed by that step.` }),
    latest && el("p", { class: "caption", text: "Highlighted: changed by the last step." }),
    notesList(result.notes, at > 0 ? results[at - 1].notes : null),
    el("details", {}, el("summary", { text: "Raw JSON" }), el("pre", { class: "small", text: pretty(result.notes) })),
  );
}

function valueText(value) {
  if (value === null || value === undefined) return "(none)";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function changeRecord(record) {
  const kind = RECORD_KINDS[record.collection];
  const id = kind ? recordLink(kind, record.id) : el("span", { class: "mono", text: record.id });
  const flag = record.added ? " (new record)" : record.removed ? " (removed)" : "";
  return el(
    "div",
    { class: "db-change" },
    el("div", {}, el("span", { class: "subtle small", text: `${record.collection} ` }), id, flag),
    record.fields.map((row) =>
      el(
        "div",
        { class: "db-field" },
        el("div", { class: "mono small db-field-name", text: row.field || "(whole record)" }),
        el("div", { class: "mono small before" }, el("span", { class: "db-side", text: "before" }), valueText(row.before)),
        el("div", { class: "mono small after" }, el("span", { class: "db-side", text: "after" }), valueText(row.after)),
      ),
    ),
  );
}

function databasePanel() {
  const reply = state.reply;
  const changes = reply?.db_changes ?? [];
  return el(
    "section",
    { class: "panel" },
    el("h2", { text: "Database changes" }),
    el("p", {
      class: "caption",
      text: "Compared with db.json. Writes stay in an in-memory copy: db.json is never written.",
    }),
    reply?.db_file_unchanged === false &&
      el("div", { class: "notice", text: "db.json changed on disk during this session. It should never change." }),
    changes.length ? changes.map(changeRecord) : nothing("No changes."),
  );
}

function drawSide() {
  ui.side.replaceChildren(notesPanel(), databasePanel());
}

// ---- The model's controls: pick a tool, fill its parameters ----

// A JSON placeholder built from the schema, e.g. [{"item_id": "", "new_item_id": ""}].
function skeleton(schema = {}) {
  if (schema.type === "object") {
    if (schema.properties) {
      return Object.fromEntries(Object.entries(schema.properties).map(([key, sub]) => [key, skeleton(sub)]));
    }
    return { name: "value" };
  }
  if (schema.type === "array") return [skeleton(schema.items)];
  if (schema.type === "integer" || schema.type === "number") return 0;
  if (schema.type === "boolean") return false;
  return "";
}

// Whole-call examples written in the description, as "Example: {...}".
function descriptionExamples(tool) {
  const names = new Set(Object.keys(tool.parameters?.properties ?? {}));
  const found = [];
  for (const match of tool.description.matchAll(/Example: (\{.*\})/g)) {
    try {
      const value = JSON.parse(match[1]);
      const fits = value && typeof value === "object" && !Array.isArray(value);
      if (fits && Object.keys(value).every((key) => names.has(key))) found.push(value);
    } catch {
      // Not JSON, so not an example the form can use.
    }
  }
  return found;
}

function inputField(name, schema, required) {
  const id = `tool-input-${name}`;
  let control;
  let read;
  let fill;
  if (Array.isArray(schema.enum)) {
    control = el(
      "select",
      { id, class: "text-box" },
      el("option", { value: "", text: "(not set)" }),
      schema.enum.map((value) => el("option", { value, text: value })),
    );
    read = () => control.value || undefined;
    fill = (value) => (control.value = value ?? "");
  } else if (schema.type === "object" || schema.type === "array") {
    control = el("textarea", {
      id,
      class: "text-box json-box",
      rows: 2,
      spellcheck: "false",
      placeholder: JSON.stringify(skeleton(schema)),
    });
    read = () => {
      const text = control.value.trim();
      if (!text) return undefined;
      try {
        return JSON.parse(text);
      } catch (error) {
        throw new Error(`${name} is not valid JSON: ${error.message}`);
      }
    };
    fill = (value) => (control.value = value === undefined ? "" : JSON.stringify(value));
  } else if (schema.type === "boolean") {
    control = el("input", { id, type: "checkbox" });
    read = () => (control.checked ? true : undefined);
    fill = (value) => (control.checked = Boolean(value));
  } else {
    control = el("input", { id, class: "text-box", type: "text", autocomplete: "off", spellcheck: "false" });
    read = () => {
      const text = control.value;
      if (text === "") return undefined;
      // Whole numbers go as numbers. Anything else goes as typed, so you can see
      // what the tool does with "#1".
      if (schema.type === "integer" && /^\s*-?\d+\s*$/.test(text)) return Number(text);
      return text;
    };
    fill = (value) => (control.value = value === undefined ? "" : String(value));
  }
  const kind = Array.isArray(schema.enum) ? "one of the values" : schema.type ?? "any";
  const caption = [`${kind}${required ? ", required" : ""}`, schema.description].filter(Boolean).join(". ");
  const node = el(
    "div",
    { class: "form-field" },
    el("label", { for: id, class: "mono", text: name }),
    el("div", { class: "caption", text: caption }),
    control,
  );
  return { name, node, read, fill };
}

function toolForm(tool) {
  const properties = tool.parameters?.properties ?? {};
  const required = new Set(tool.parameters?.required ?? []);
  const fields = Object.entries(properties).map(([name, schema]) => inputField(name, schema, required.has(name)));
  const examples = descriptionExamples(tool);
  const formError = el("div", { role: "alert" });

  const run = async (event) => {
    event.preventDefault();
    formError.replaceChildren();
    const inputs = {};
    try {
      for (const field of fields) {
        const value = field.read();
        if (value !== undefined) inputs[field.name] = value;
      }
    } catch (error) {
      formError.replaceChildren(el("div", { class: "notice", text: error.message }));
      return;
    }
    await addStep({ type: "tool", tool: tool.name, inputs });
  };

  return el(
    "form",
    { class: "tool-form", onsubmit: run },
    el("div", { class: "subtle small mono", text: tool.file }),
    el("div", { class: "tool-description", text: tool.description }),
    fields.length ? fields.map((field) => field.node) : el("p", { class: "subtle small", text: "No parameters." }),
    examples.length > 0 &&
      el(
        "div",
        { class: "examples" },
        el("span", { class: "caption", text: "Fill from an example in the description:" }),
        examples.map((example, index) =>
          el("button", {
            class: "link-button",
            type: "button",
            text: example.kind ?? `Example ${index + 1}`,
            onclick: () => fields.forEach((field) => field.fill(example[field.name])),
          }),
        ),
      ),
    formError,
    el("div", { class: "form-actions" }, el("button", { class: "primary-button", type: "submit", text: `Run ${tool.name}` })),
  );
}

function selectTool(name) {
  state.selected = name;
  const tool = state.catalog.tools.find((entry) => entry.name === name);
  [...ui.picks.children].forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.tool === name)));
  ui.toolForm.replaceChildren(toolForm(tool));
}

function modelPanel() {
  ui.picks = el(
    "div",
    { class: "tool-picks", role: "group", "aria-label": "Tools" },
    state.catalog.tools.map((tool) =>
      el("button", {
        class: "tool-pick",
        type: "button",
        "data-tool": tool.name,
        "aria-pressed": "false",
        text: tool.name,
        onclick: () => selectTool(tool.name),
      }),
    ),
  );
  ui.toolForm = el("div");
  return el("section", { class: "panel" }, el("h2", { text: "As the model: call a tool" }), ui.picks, ui.toolForm);
}

// ---- The agent loop's and the customer's controls ----

function customerPanel() {
  const number = el("input", { id: "show-number", class: "text-box number-box", type: "number", min: "1", value: "1" });
  const showForm = el(
    "form",
    {
      class: "inline-form",
      onsubmit: (event) => {
        event.preventDefault();
        addStep({ type: "show_change", number: Number(number.value) });
      },
    },
    el("label", { for: "show-number", text: "Show change #" }),
    number,
    el("button", { class: "primary-button", type: "submit", text: "Show it to the customer" }),
  );

  const text = el("textarea", { id: "customer-text", class: "text-box", rows: 2, placeholder: "Yes, go ahead." });
  const verdict = el("span", { class: "yes-check", "aria-live": "polite" });
  let timer = null;
  let asked = 0;
  const check = () => {
    clearTimeout(timer);
    const value = text.value;
    if (!value.trim()) {
      verdict.replaceChildren();
      return;
    }
    timer = setTimeout(async () => {
      const ticket = ++asked;
      try {
        const reply = await api(`/api/tools/clear-yes?text=${encodeURIComponent(value)}`);
        if (ticket !== asked) return;
        verdict.replaceChildren(
          reply.ok
            ? chipNode(reply.clear_yes ? "pass" : "fail", reply.clear_yes ? "clear yes" : "not a clear yes")
            : nothing(reply.error),
        );
      } catch (error) {
        if (ticket === asked) verdict.replaceChildren(nothing(error.message));
      }
    }, YES_CHECK_DELAY_MS);
  };
  text.addEventListener("input", check);
  ui.showNumber = number;

  const sayForm = el(
    "form",
    {
      onsubmit: async (event) => {
        event.preventDefault();
        if (await addStep({ type: "customer_says", text: text.value })) {
          text.value = "";
          verdict.replaceChildren();
        }
      },
    },
    el("label", { for: "customer-text", text: "Customer says" }),
    text,
    el(
      "div",
      { class: "form-actions" },
      el("span", { class: "caption", text: "is_clear_yes, as you type:" }),
      verdict,
      el("span", { class: "spacer" }),
      el("button", { class: "primary-button", type: "submit", text: "Send" }),
    ),
  );
  text.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sayForm.requestSubmit();
    }
  });

  return el(
    "section",
    { class: "panel" },
    el("h2", { text: "As the agent loop and the customer" }),
    el("p", {
      class: "caption",
      text: "Show calls notes.mark_shown(N): code sends the prepared summary word for word. Send calls notes.customer_said(text).",
    }),
    showForm,
    sayForm,
  );
}

// ---- Presets ----

function presetsBar(loaded) {
  if (loaded.problem) return el("div", { class: "notice", text: loaded.problem });
  if (!loaded.presets.length) return null;
  ui.presets = el(
    "fieldset",
    { class: "presets" },
    el("legend", { class: "caption", text: "Load a preset. It replaces the current session." }),
    loaded.presets.map((preset) =>
      el("button", {
        class: "link-button",
        type: "button",
        text: preset.name,
        title: preset.description ?? undefined,
        onclick: () => act(preset.steps),
      }),
    ),
  );
  return ui.presets;
}

// ---- fde_agent's own tests ----

function outcomeChip(outcome, count) {
  const [css, word] = OUTCOMES[outcome] ?? ["", outcome];
  return chipNode(css, count === undefined ? word : `${count} ${word}`);
}

function countChips(counts) {
  return Object.keys(OUTCOMES)
    .filter((outcome) => counts[outcome] > 0)
    .flatMap((outcome, index) => [index ? " " : "", outcomeChip(outcome, counts[outcome])]);
}

function shortGroup(group) {
  return group.replace(/^fde_agent\.tests\./, "");
}

function failureBlock(test) {
  return el(
    "div",
    { class: "test-failure" },
    el("div", { class: "call-head" }, outcomeChip(test.outcome), el("span", { class: "mono", text: `${shortGroup(test.group)}: ${test.name}` })),
    test.message && el("p", { class: "small", text: test.message }),
    test.details && el("pre", { class: "small test-output", text: test.details }),
    test.output &&
      el("details", {}, el("summary", { text: "Captured output" }), el("pre", { class: "small test-output", text: test.output })),
  );
}

function groupBlock(group, tests) {
  const counts = {};
  for (const test of tests) counts[test.outcome] = (counts[test.outcome] ?? 0) + 1;
  return el(
    "details",
    { class: "test-group", open: Boolean(counts.failed || counts.error) },
    el("summary", {}, el("span", { class: "mono", text: shortGroup(group) }), " ", countChips(counts)),
    el(
      "ul",
      { class: "test-list" },
      tests.map((test) =>
        el(
          "li",
          {},
          outcomeChip(test.outcome),
          " ",
          el("span", { class: "mono small", text: test.name }),
          el("span", { class: "subtle small", text: ` ${test.seconds.toFixed(2)}s` }),
        ),
      ),
    ),
  );
}

function testResults(run) {
  const broken = run.tests.filter((test) => test.outcome === "failed" || test.outcome === "error");
  const groups = new Map();
  for (const test of run.tests) groups.set(test.group, [...(groups.get(test.group) ?? []), test]);
  return el(
    "div",
    {},
    el("div", { class: "test-counts" }, countChips(run.counts), el("span", { class: "subtle small", text: ` in ${run.seconds}s` })),
    el("p", { class: "caption mono", text: run.command }),
    run.exit_code !== 0 &&
      !broken.length &&
      el("div", { class: "notice", text: `pytest exited with code ${run.exit_code}. See the Output tab.` }),
    broken.length > 0 && [el("h3", { class: "section-heading", text: "Failures" }), broken.map(failureBlock)],
    el("h3", { class: "section-heading", text: "All tests" }),
    [...groups].map(([group, tests]) => groupBlock(group, tests)),
  );
}

function showTestRun(opener) {
  const run = state.testRun;
  if (!run) return;
  const output = () => el("pre", { class: "small test-output", text: run.output || "(no output)" });
  if (!run.ok) {
    openDrawer({
      title: "fde_agent tests",
      opener,
      tabs: [
        {
          label: "Problem",
          render: () =>
            el(
              "div",
              {},
              el("div", { class: "notice", text: run.error }),
              run.command && el("p", { class: "caption mono", text: run.command }),
              run.output && output(),
            ),
        },
      ],
    });
    return;
  }
  openDrawer({
    title: "fde_agent tests",
    subtitle: run.summary ?? undefined,
    opener,
    tabs: [
      { label: "Results", render: () => testResults(run) },
      { label: "Output", render: output },
    ],
  });
}

async function runTests(opener) {
  if (state.testsRunning) return;
  state.testsRunning = true;
  drawTests();
  openDrawer({
    title: "fde_agent tests",
    opener,
    tabs: [
      {
        label: "Running",
        render: () => el("div", { class: "empty", text: "Running fde_agent/tests. This takes about 10 seconds." }),
      },
    ],
  });
  try {
    const response = await fetch("/api/tools/tests", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const body = await response.json().catch(() => ({}));
    state.testRun = response.ok ? body : { ok: false, error: body.detail || `Request failed: ${response.status}` };
  } catch (error) {
    state.testRun = { ok: false, error: error.message };
  }
  state.testsRunning = false;
  if (!ui?.root.isConnected) return; // The reader has moved on to another page.
  drawTests();
  showTestRun(opener);
}

function drawTests() {
  ui.testsButton.disabled = state.testsRunning;
  ui.testsButton.textContent = state.testsRunning ? "Running tests..." : "Run fde_agent tests";
  const run = state.testRun;
  ui.lastTests.replaceChildren(
    run && !state.testsRunning
      ? el(
          "button",
          {
            class: "link-button",
            type: "button",
            title: "Show the last test run",
            onclick: (event) => showTestRun(event.currentTarget),
          },
          run.ok ? countChips(run.counts) : chipNode("fail", "tests could not run"),
        )
      : "",
  );
}

// ---- Page ----

function drawStatus() {
  const count = state.steps.length;
  let text = "No steps yet.";
  if (state.busy) text = "Replaying the session on a fresh copy of db.json...";
  else if (state.reply) text = `${plural(count, "step")}, replayed in ${state.reply.seconds}s.`;
  ui.status.textContent = text;
  ui.undo.disabled = state.busy || count === 0;
  ui.reset.disabled = state.busy || count === 0;
}

function draw() {
  if (!ui?.root.isConnected) return;
  drawStatus();
  drawTimeline();
  drawSide();
  ui.controls.disabled = state.busy;
  if (ui.presets) ui.presets.disabled = state.busy;
  ui.error.replaceChildren(state.error ? el("div", { class: "notice", text: state.error }) : "");
  const prepared = state.reply?.notes?.prepared;
  if (prepared && !state.busy) ui.showNumber.value = String(prepared.number);
}

export async function renderTools(view, setCrumbs) {
  setCrumbs(); // The page has its own nav link, like the database explorer.
  const [catalog, presets] = await Promise.all([api("/api/tools"), api("/api/tools/presets"), loadIds()]);
  if (!catalog.ok) throw new Error(catalog.error);
  setLinking(true);
  state.catalog = catalog;

  ui = {
    root: null,
    presets: null,
    status: el("span", { class: "subtle small", role: "status" }),
    testsButton: el("button", {
      class: "link-button",
      type: "button",
      onclick: (event) => runTests(event.currentTarget),
    }),
    lastTests: el("span"),
    undo: el("button", {
      class: "link-button",
      type: "button",
      text: "Undo last step",
      onclick: () => act(state.steps.slice(0, -1)),
    }),
    reset: el("button", { class: "link-button", type: "button", text: "Reset session", onclick: () => act([]) }),
    timeline: el("div", { class: "timeline" }),
    side: el("aside", { class: "side" }),
    error: el("div", { role: "alert" }),
  };
  ui.controls = el("fieldset", { class: "composer" }, modelPanel(), customerPanel());

  ui.root = el(
    "div",
    {},
    el(
      "div",
      { class: "page-head" },
      el("h1", { text: "Tools" }),
      ui.status,
      el("span", { class: "spacer" }),
      ui.lastTests,
      ui.testsButton,
      ui.undo,
      ui.reset,
    ),
    el("p", {
      class: "subtle small tools-intro",
      text:
        "Play the model and the customer in one conversation. Each action replays every step on a fresh in-memory copy of db.json, which takes about 4 seconds.",
    }),
    catalog.skipped?.length > 0 &&
      el("div", { class: "notice", text: `Not loaded from fde_agent/tools: ${catalog.skipped.join("; ")}` }),
    presetsBar(presets),
    el("div", { class: "task-layout tools-layout" }, el("div", {}, ui.timeline, ui.error, ui.controls), ui.side),
  );
  view.replaceChildren(ui.root);

  const names = catalog.tools.map((tool) => tool.name);
  selectTool(names.includes(state.selected) ? state.selected : names[0]);
  draw();
  drawTests();

  const saved = savedSteps();
  if (!state.steps.length && !state.busy && saved.length) await act(saved);
}
