// The side drawer. Each view is a plain config, so going back just reopens one.

import { el } from "./util.js";

const drawer = document.getElementById("drawer");

let returnFocus = null;
let current = null;
let onClose = null;

export function isOpen() {
  return !drawer.hidden;
}

export function currentView() {
  return isOpen() ? current : null;
}

export function setOnClose(callback) {
  onClose = callback;
}

export function closeDrawer() {
  drawer.hidden = true;
  drawer.replaceChildren();
  current = null;
  onClose?.();
  returnFocus?.focus();
  returnFocus = null;
}

export function resetDrawer() {
  drawer.hidden = true;
  drawer.replaceChildren();
  current = null;
  returnFocus = null;
}

// tabs: [{label, render}]. back: another view to return to.
export function openDrawer(view) {
  const { title, subtitle, tabs, opener, back, onBack } = view;
  current = view;
  // A link inside the drawer disappears when the view changes, so closing must
  // return to whatever opened the drawer in the first place.
  if (opener && !drawer.contains(opener)) returnFocus = opener;

  const closeButton = el("button", { class: "link-button", type: "button", text: "Close (Esc)", onclick: closeDrawer });
  const goBack = onBack ?? (back ? () => openDrawer(back) : null);
  const backButton = goBack && el("button", { class: "link-button", type: "button", text: "Back", onclick: goBack });
  const body = el("div", { class: "drawer-body" });
  const tabBar = el("div", { class: "tabs", role: "tablist" });

  const select = async (index) => {
    [...tabBar.children].forEach((tab, i) => tab.setAttribute("aria-selected", String(i === index)));
    body.replaceChildren(el("div", { class: "empty", text: "Loading" }));
    try {
      body.replaceChildren(await tabs[index].render());
    } catch (error) {
      body.replaceChildren(el("div", { class: "empty", text: error.message }));
    }
    body.scrollTop = 0;
  };

  tabs.forEach((tab, index) =>
    tabBar.append(
      el("button", { class: "tab", type: "button", role: "tab", text: tab.label, onclick: () => select(index) }),
    ),
  );

  drawer.replaceChildren(
    el(
      "div",
      { class: "drawer-head" },
      el(
        "div",
        {},
        el("h2", { text: title }),
        subtitle && el("p", { class: "subtle small" }, subtitle),
      ),
      backButton,
      closeButton,
    ),
    tabBar,
    body,
  );
  drawer.hidden = false;
  if (tabs.length) select(0);
  closeButton.focus();
}

export function drawerLoading(title, opener) {
  openDrawer({ title, tabs: [{ label: "Loading", render: () => el("div", { class: "empty", text: "Loading" }) }], opener });
}
