// Small helpers shared by every page.

export const RESULT_LABELS = {
  pass: "Pass",
  fail: "Fail",
  stopped: "Stopped early",
  infra: "Infra error",
  pending: "Pending",
};

export const CHECK_LABELS = {
  pass: "pass",
  fail: "fail",
  none: "none defined",
  not_evaluated: "not evaluated",
};

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child);
  }
  return node;
}

export async function api(path) {
  const response = await fetch(path);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed: ${response.status}`);
  return body;
}

export async function post(path, data) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : "The save failed.";
    throw new Error(detail);
  }
  return body;
}

export const enc = encodeURIComponent;

export const runLink = (name) => `#/run/${enc(name)}`;

export function taskLink(run, taskId, trial) {
  const base = `${runLink(run)}/task/${enc(taskId)}`;
  return trial ? `${base}?trial=${trial}` : base;
}

export function chip(state, labels = RESULT_LABELS, extra = "") {
  const css = state === "not_evaluated" || state === "none" ? "" : state;
  return el("span", { class: `chip ${css} ${extra}`, text: labels[state] ?? state ?? "" });
}

export function taskChips(run, tasks) {
  return tasks.map((task, index) => [
    index ? " " : "",
    el("a", {
      class: "task-chip",
      href: taskLink(run, task.task_id, task.trial),
      text: task.trial ? `${task.task_id}.${task.trial}` : task.task_id,
    }),
  ]);
}

export function selectRun(names, selected, onchange, label) {
  return el(
    "select",
    { class: "run-select", "aria-label": label, onchange: (event) => onchange(event.target.value) },
    el("option", { value: "", text: label }),
    names.map((name) => el("option", { value: name, selected: name === selected, text: name })),
  );
}

export function pretty(value) {
  return JSON.stringify(value, null, 2);
}

export function formatDate(value) {
  return value ? value.slice(0, 16).replace("T", " ") : "unknown";
}

export function formatTime(value) {
  return value ? value.slice(11, 19) : "";
}

export function formatPrice(value) {
  return value === undefined || value === null ? "" : Number(value).toFixed(2);
}

export function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

export function keyValues(rows) {
  return el(
    "dl",
    { class: "kv" },
    rows
      .filter(([, value]) => value !== undefined && value !== null && value !== "")
      .flatMap(([key, value]) => [el("dt", { text: key }), el("dd", { class: "mono", text: String(value) })]),
  );
}

export function copyButton(text) {
  return el("button", {
    class: "link-button",
    type: "button",
    text: "Copy JSON",
    onclick: async (event) => {
      const button = event.currentTarget;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
      } catch {
        button.textContent = "Copy failed";
      }
    },
  });
}

export function rawJson(value) {
  const text = pretty(value);
  return el(
    "div",
    {},
    el("div", { class: "toolbar", style: "margin-top:0" }, copyButton(text)),
    el("pre", { text }),
  );
}
