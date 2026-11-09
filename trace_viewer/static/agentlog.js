// A conversation from an agent's own log (fde_agent_log.json): the benchmark's
// messages, and between them every step inside the agent the benchmark never saw.
// The steps come from viewer/agent_log.py. Messages and tool calls are drawn by the
// page's own stepNode, so they look the same as for any other agent.

import { ignoredBreakdown } from "./agentcounts.js";
import { linked } from "./ids.js";
import { el, formatTime, plural, pretty } from "./util.js";

const CHECK_HELP = {
  invalid_call: "The model's reply was rejected before anything ran, and the model was told why.",
  fixed: "Code fixed something in the model's reply on the way.",
  ignored_inputs: "prepare_change got inputs its change kind does not use. They were ignored.",
  blocked_reply: "The model's message showed internal IDs, so it was not sent. The model was asked to rewrite it.",
  empty_reply: "The model replied with nothing. It was told to call a tool or write a message.",
  safe_message: "Too many failing tries in a row, so code sent its safe message.",
  tool_crash: "A bug in one of the agent's tools. The model only got a generic error.",
};

let showInner = true;

function seconds(value) {
  return value === undefined || value === null ? "" : `${value.toFixed(1)}s`;
}

function tokens(usage) {
  return usage ? `${usage.prompt_tokens ?? "?"} in, ${usage.completion_tokens ?? "?"} out` : "";
}

function head(label, step, ...extra) {
  return el(
    "div",
    { class: "step-head" },
    el("span", { class: "step-label", text: label }),
    el("span", { text: formatTime(step.timestamp) }),
    ...extra,
  );
}

function jump(index, text, scrollTo) {
  return el("button", { class: "link-button", type: "button", text, onclick: () => scrollTo(index) });
}

function modelCallNode(step, actions) {
  return el(
    "article",
    { class: "step inner model-call", id: `step-${step.index}` },
    head(
      "Model call",
      step,
      step.usage && el("span", { text: tokens(step.usage) }),
      step.generation_time_seconds && el("span", { text: seconds(step.generation_time_seconds) }),
      step.outcome &&
        el("span", {}, "→ ", step.outcome_step ? jump(step.outcome_step, step.outcome, actions.scrollTo) : step.outcome),
      el("span", { class: "spacer" }),
      step.call_file
        ? el("button", {
            class: "link-button",
            type: "button",
            text: "Raw call",
            onclick: (event) => actions.openCall(step.call_file, "Model call", event.currentTarget),
          })
        : el("span", { class: "chip fail", title: "No llm_debug file has this call's exact time.", text: "raw call file not found" }),
    ),
  );
}

function toolCallNode(step) {
  return el(
    "article",
    { class: "step inner", id: `step-${step.index}` },
    head("Agent's tool call", step, el("span", { class: "mono", text: step.id })),
    el("div", { class: "tool-name", text: step.tool }),
    el("pre", {}, linked(pretty(step.args ?? {}))),
  );
}

function toolResultNode(step) {
  return el(
    "article",
    { class: `step inner ${step.ok ? "" : "failed"}`, id: `step-${step.index}` },
    head(
      step.ok ? "Agent's tool result" : "Agent's tool error",
      step,
      el("span", { class: "mono", text: `${step.id} ${step.tool}` }),
      el("span", { class: "subtle", text: "exactly what the model saw" }),
    ),
    el("pre", {}, linked(step.text ?? "")),
  );
}

function noteNode(step) {
  return el(
    "article",
    { class: "step inner note", id: `step-${step.index}` },
    head("Note from code to the model", step),
    el("div", { class: "step-text" }, linked(step.text ?? "")),
  );
}

function checkBody(step) {
  const d = step.details ?? {};
  switch (step.check) {
    case "invalid_call":
      return el("pre", {}, linked(d.error ?? ""));
    case "fixed":
      return el("div", { class: "step-text" }, linked(d.what ?? ""));
    case "ignored_inputs":
      return el("div", { class: "step-text" }, `${d.tool} (${d.change_kind}) ignored: `, el("span", { class: "mono", text: (d.inputs ?? []).join(", ") }));
    case "blocked_reply":
      return el(
        "div",
        {},
        el("div", { class: "step-text" }, "Internal IDs: ", el("span", { class: "mono", text: (d.ids ?? []).join(", ") })),
        el("details", {}, el("summary", { text: "The reply that was not sent" }), el("pre", {}, linked(d.text ?? ""))),
      );
    case "safe_message":
      return el("div", { class: "step-text", text: d.reason ?? "" });
    case "tool_crash":
      return el("div", {}, el("div", { class: "tool-name", text: d.tool }), el("pre", { class: "log", text: d.traceback ?? "" }));
    case "empty_reply":
      return null;
    default:
      return el("pre", { text: pretty(d) });
  }
}

function checkNode(step, actions) {
  return el(
    "article",
    { class: `step log-check ${step.check}`, id: `step-${step.index}`, title: CHECK_HELP[step.check] },
    head(
      `CHECK · ${step.name}`,
      step,
      el("span", { class: "mono", text: step.check }),
      el("span", { class: "spacer" }),
      step.model_call_step && jump(step.model_call_step, "its model call", actions.scrollTo),
    ),
    el("p", { class: "caption", text: CHECK_HELP[step.check] ?? "" }),
    checkBody(step),
  );
}

function otherNode(step) {
  return el(
    "article",
    { class: "step inner", id: `step-${step.index}` },
    head(`Log event: ${step.details?.kind ?? "unknown"}`, step),
    el("pre", { text: pretty(step.details) }),
  );
}

function innerNode(step, actions) {
  if (step.type === "check") return checkNode(step, actions);
  switch (step.kind) {
    case "model_call":
      return modelCallNode(step, actions);
    case "tool_call":
      return toolCallNode(step);
    case "tool_result":
      return toolResultNode(step);
    case "note":
      return noteNode(step);
    default:
      return otherNode(step);
  }
}

// actions: stepNode(step, onRaw) from the page, openCall(file, title, opener), scrollTo(index)
export function agentTimeline(conversation, actions) {
  const onRaw = (step, opener) => actions.openCall(step.call_file, step.type === "customer" ? "Customer call" : "Model call", opener);
  const section = el(
    "section",
    { class: `timeline ${showInner ? "" : "hide-inner"}`, "aria-label": "Conversation" },
    conversation.steps.map((step) =>
      step.type === "inner" || step.type === "check" ? innerNode(step, actions) : actions.stepNode(step, onRaw),
    ),
  );
  return section;
}

function checkSummary(counts) {
  const fired = Object.entries(counts.checks ?? {}).filter(([, n]) => n);
  if (!fired.length) return "no checks fired";
  return fired.map(([kind, n]) => `${kind} ×${n}`).join(", ");
}

// What the conversation is built from, which attempt, and whether it is live.
export function conversationHeader(conversation, { attempts, attemptLink, onToggle }) {
  const c = conversation.counts ?? {};
  const short = conversation.sim_id.slice(0, 8);
  const withLogs = attempts.filter((a) => a.has_agent_log);
  return el(
    "div",
    { class: "panel conversation-source" },
    el(
      "div",
      { class: "step-head" },
      el("span", { class: "step-label", text: "From the agent's own log" }),
      el("span", { class: "mono", title: conversation.sim_id, text: `attempt ${short}` }),
      conversation.live && el("span", { class: "chip infra", title: "The page redraws as the agent rewrites its log.", text: "Live" }),
      !conversation.in_results &&
        el("span", {
          class: "chip stopped",
          title: "results.json does not hold this attempt, so its evaluation and database replay are not available.",
          text: "not in results.json",
        }),
      el("span", { class: "spacer" }),
      el(
        "label",
        { class: "small" },
        el("input", {
          type: "checkbox",
          checked: showInner,
          onchange: (event) => {
            showInner = event.target.checked;
            onToggle(showInner);
          },
        }),
        " Show the agent's internal steps",
      ),
    ),
    el(
      "p",
      { class: "small subtle" },
      `fde_agent_log.json: ${plural(c.events ?? 0, "event")} · ${plural(c.model_calls ?? 0, "model call")} · `,
      `${plural(c.benchmark_steps ?? 0, "benchmark step")} · `,
      `${plural(c.tool_calls ?? 0, "tool call")} to the agent's tools · ${plural(c.store_calls ?? 0, "store call")} · `,
      `messages by model ${c.messages_by?.model ?? 0}, by code ${c.messages_by?.code ?? 0} · ${checkSummary(c)}`,
    ),
    conversation.status !== "used" && conversation.status !== "no_status" &&
      el("p", { class: "small", text: `This attempt ${conversation.status}${conversation.reason ? ` (${conversation.reason})` : ""}${conversation.error ? `: ${conversation.error}` : ""}` }),
    (c.checks?.ignored_inputs ?? 0) > 0 &&
      el("details", {}, el("summary", { text: `Ignored inputs (${c.checks.ignored_inputs})` }), ignoredBreakdown(c.ignored_inputs)),
    conversation.stale && el("p", { class: "small subtle", text: "The agent is writing a newer log. Showing the previous copy until it settles." }),
    conversation.problem && el("p", { class: "small error", text: conversation.problem }),
    withLogs.length > 1 &&
      el(
        "p",
        { class: "small" },
        "Other attempts with a log: ",
        withLogs
          .filter((a) => a.sim_id !== conversation.sim_id)
          .map((a, i) => [i ? ", " : "", el("a", { href: attemptLink(a.sim_id), text: a.sim_id.slice(0, 8) })]),
      ),
  );
}

export function setInnerVisible(section, visible) {
  section?.classList.toggle("hide-inner", !visible);
}

const ROLE_LABELS = { agent: "Agent model", customer: "Customer model", judge: "Judge", other: "Other" };

// Every model call of the attempt, including those no message points to.
export function modelCallsPanel(conversation, { openCall, scrollTo }) {
  const calls = conversation.calls ?? [];
  const missing = conversation.missing_calls ?? [];
  return el(
    "section",
    { class: "panel" },
    el("h2", { text: `Model calls (${calls.length})` }),
    el("p", {
      class: "caption",
      text: "Every raw call saved for this attempt, in order. One benchmark turn can hold several agent model calls; only the agent's log shows them all.",
    }),
    missing.length > 0 &&
      el("div", { class: "notice", text: `${plural(missing.length, "model call")} in the log ${missing.length === 1 ? "has" : "have"} no raw file.` }),
    calls.map((call) =>
      el(
        "div",
        { class: "call-row" },
        el("span", { class: "mono small", text: formatTime(call.timestamp) }),
        " ",
        el("span", { class: `chip ${call.role === "agent" ? "pass" : call.role === "customer" ? "plain" : ""}`, text: ROLE_LABELS[call.role] ?? call.role }),
        el("span", { class: "subtle small" }, ` ${call.prompt_tokens ?? "?"} in, ${call.completion_tokens ?? "?"} out · ${seconds(call.generation_time_seconds)}`),
        call.outcome && el("div", { class: "small", text: `→ ${call.outcome}` }),
        el(
          "div",
          { class: "small" },
          el("button", {
            class: "link-button",
            type: "button",
            text: "Open",
            onclick: (event) => openCall(call.file, ROLE_LABELS[call.role] ?? call.call_name, event.currentTarget),
          }),
          " ",
          call.step !== null && call.step !== undefined
            ? el("button", { class: "link-button", type: "button", text: "Go to step", onclick: () => scrollTo(call.step) })
            : el("span", { class: "subtle", text: "no step (judge or unmatched)" }),
        ),
      ),
    ),
  );
}
