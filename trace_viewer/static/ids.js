// Turn every ID in a text into a link to its database record.
//
// Shapes were checked against all of db.json and every message in the saved runs.
// A token that fits an ID shape but is not in db.json is kept visible and marked,
// because an agent quoting an ID that does not exist is a finding in itself.
//
// Nothing here builds HTML: output is text nodes and elements, so model text can
// never become markup.

import { api, el, enc } from "./util.js";

// The last alternative skips decimals such as 0.1234567890, but must still match
// an ID that ends a sentence, so a dot only counts when a digit follows it.
const TOKEN =
  /#?\bW\d{7}\b|\b(?:credit_card|gift_card|paypal)_\d{7}\b|\b[a-z]+_[a-z]+_\d{4}\b|(?<!\d)(?<!\d\.)\d{10}(?!\d|\.\d)/g;

const LABELS = { order: "Order", user: "User", product: "Product", item: "Item", payment: "Payment method" };

let known = null;
let enabled = false;

export async function loadIds() {
  if (known) return;
  const ids = await api("/api/db/ids");
  known = {
    orders: new Set(ids.orders),
    users: new Set(ids.users),
    products: new Set(ids.products),
    items: new Set(ids.items),
    payments: new Set(ids.payments),
  };
}

// Regional runs use another database, where links would point at wrong records.
export function setLinking(on) {
  enabled = on;
}

export function recordHref(kind, id) {
  return `#/db/${kind}/${enc(id)}`;
}

function resolve(token) {
  if (/W\d{7}$/.test(token)) {
    const id = token.startsWith("#") ? token : `#${token}`;
    return { kind: "order", id, found: known.orders.has(id), bare: !token.startsWith("#") };
  }
  if (/^\d{10}$/.test(token)) {
    if (known.products.has(token)) return { kind: "product", id: token, found: true };
    return { kind: "item", id: token, found: known.items.has(token) };
  }
  if (/^(credit_card|gift_card|paypal)_/.test(token)) {
    return { kind: "payment", id: token, found: known.payments.has(token) };
  }
  return { kind: "user", id: token, found: known.users.has(token) };
}

export function recordLink(kind, id, text = id, note = "") {
  return el("a", {
    class: "id-link",
    href: recordHref(kind, id),
    "data-kind": kind,
    "data-id": id,
    title: `${LABELS[kind]}${note}`,
    text,
  });
}

function tokenNode(token) {
  const { kind, id, found, bare } = resolve(token);
  if (found) return recordLink(kind, id, token, bare ? ", written without #. The tools reject this form." : "");
  const article = kind === "order" || kind === "item" ? "an" : "a";
  return el("span", {
    class: "id-missing",
    title: `Looks like ${article} ${kind} ID, but it is not in db.json`,
    text: token,
  });
}

// Returns a list of strings and nodes, ready to pass to el() as children.
export function linked(text) {
  if (text === undefined || text === null) return [];
  const value = String(text);
  if (!enabled || !known) return [value];
  const parts = [];
  let position = 0;
  for (const match of value.matchAll(TOKEN)) {
    if (match.index > position) parts.push(value.slice(position, match.index));
    parts.push(tokenNode(match[0]));
    position = match.index + match[0].length;
  }
  if (position < value.length) parts.push(value.slice(position));
  return parts;
}
