// Notice new runs and new results without a reload.
//
// /api/changes only lists folders, so polling it is harmless during a live benchmark
// save. New data is fetched through the normal routes, which read results.json safely.

import { api, enc } from "./util.js";

const POLL_MS = 3000;

let scope = null; // null watches nothing, "all" every run, or a list of run names.
let listener = null;
let lastSeen = null;
let timer = null;

const statusNode = document.getElementById("live-status");

function setStatus(connected) {
  statusNode.textContent = connected ? "Live" : "Not responding";
  statusNode.classList.toggle("down", !connected);
  statusNode.title = connected
    ? "New runs and results appear by themselves."
    : "The viewer's server is not answering. Is it still running?";
}

function fingerprint(runs) {
  const names = scope === "all" ? Object.keys(runs).sort() : scope;
  return JSON.stringify(names.map((name) => [name, runs[name]?.token ?? null]));
}

async function poll() {
  timer = null;
  if (scope !== null && document.visibilityState === "visible") {
    const open = Array.isArray(scope) ? scope[0] : null;
    const url = open ? `/api/changes?run=${enc(open)}` : "/api/changes";
    try {
      const { runs } = await api(url);
      setStatus(true);
      const current = fingerprint(runs);
      if (lastSeen !== null && current !== lastSeen) await listener?.();
      lastSeen = current;
    } catch {
      setStatus(false);
    }
  }
  timer = setTimeout(poll, POLL_MS);
}

// Watch for changes to some runs. Passing nothing stops watching.
export function watch(newScope = null, onChange = null) {
  scope = newScope;
  listener = onChange;
  lastSeen = null;
  if (timer === null) timer = setTimeout(poll, 0);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && timer !== null) {
    clearTimeout(timer);
    timer = setTimeout(poll, 0);
  }
});
