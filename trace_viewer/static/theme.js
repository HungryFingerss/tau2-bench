// Light and dark mode.
//
// A script in index.html sets data-theme before the page paints: the choice saved
// here, or the system's setting. Until you choose, the page follows the system,
// including when it changes. Your choice is kept in this browser only.

const KEY = "trace-viewer-theme";
const SVG = "http://www.w3.org/2000/svg";

// Feather icons (MIT): a moon offers dark mode, a sun offers light mode.
const MOON = [["path", { d: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" }]];
const SUN = [
  ["circle", { cx: "12", cy: "12", r: "4.5" }],
  ...[
    [12, 1, 12, 3],
    [12, 21, 12, 23],
    [4.2, 4.2, 5.6, 5.6],
    [18.4, 18.4, 19.8, 19.8],
    [1, 12, 3, 12],
    [21, 12, 23, 12],
    [4.2, 19.8, 5.6, 18.4],
    [18.4, 5.6, 19.8, 4.2],
  ].map(([x1, y1, x2, y2]) => ["line", { x1, y1, x2, y2 }]),
];

const root = document.documentElement;
const button = document.getElementById("theme-toggle");
const systemDark = matchMedia("(prefers-color-scheme: dark)");

function savedTheme() {
  try {
    const theme = localStorage.getItem(KEY);
    return theme === "dark" || theme === "light" ? theme : null;
  } catch {
    return null;
  }
}

function icon(shapes) {
  const svg = document.createElementNS(SVG, "svg");
  for (const [name, value] of Object.entries({
    viewBox: "0 0 24 24",
    width: "16",
    height: "16",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "2",
    "stroke-linecap": "round",
    "aria-hidden": "true",
  })) {
    svg.setAttribute(name, value);
  }
  for (const [tag, attributes] of shapes) {
    const shape = document.createElementNS(SVG, tag);
    for (const [name, value] of Object.entries(attributes)) shape.setAttribute(name, String(value));
    svg.append(shape);
  }
  return svg;
}

function show(theme) {
  root.dataset.theme = theme;
  const other = theme === "dark" ? "light" : "dark";
  button.replaceChildren(icon(theme === "dark" ? SUN : MOON));
  button.title = `Switch to ${other} mode`;
  button.setAttribute("aria-label", `Switch to ${other} mode`);
}

button.addEventListener("click", () => {
  const next = root.dataset.theme === "dark" ? "light" : "dark";
  try {
    localStorage.setItem(KEY, next);
  } catch {
    // Storage is off: the switch still works until the page is reloaded.
  }
  show(next);
});

systemDark.addEventListener("change", (event) => {
  if (!savedTheme()) show(event.matches ? "dark" : "light");
});

show(root.dataset.theme === "dark" ? "dark" : "light");
