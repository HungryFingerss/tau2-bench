// The database explorer, and the record views shared with the side drawer.

import { currentView, drawerLoading, openDrawer } from "./drawer.js";
import { loadIds, recordHref, recordLink, setLinking } from "./ids.js";
import { api, el, enc, formatPrice, keyValues, plural, rawJson } from "./util.js";

const PAGE_SIZE = 50;
const INITIAL_STATE =
  "Initial state from db.json, before any simulation. A simulation may have changed this record: see its database difference.";

const TABS = [
  { kind: "users", label: "Users" },
  { kind: "orders", label: "Orders" },
  { kind: "products", label: "Products" },
  { kind: "items", label: "Variants" },
];

const API_KIND = { user: "users", order: "orders", product: "products", item: "items", payment: "payments" };

function optionsText(options) {
  return Object.values(options ?? {}).join(", ");
}

function availability(available) {
  return el("span", { class: `chip plain ${available ? "pass" : "fail"}`, text: available ? "available" : "unavailable" });
}

function table(headers, rows) {
  return el(
    "div",
    { class: "table-wrap" },
    el(
      "table",
      {},
      el("thead", {}, el("tr", {}, headers.map(([label, css]) => el("th", { class: css, text: label })))),
      el("tbody", {}, rows),
    ),
  );
}

function cell(content, css) {
  return el("td", { class: css }, content);
}

function orderRows(orders) {
  return orders.map((order) =>
    el(
      "tr",
      {},
      cell(recordLink("order", order.id), "nowrap"),
      cell(order.status),
      cell(String(order.items), "num"),
      cell(formatPrice(order.total), "num"),
    ),
  );
}

const ORDER_HEADERS = [["Order"], ["Status"], ["Items", "num"], ["Total", "num"]];

function section(title, ...content) {
  return el("section", { class: "db-section" }, el("h3", { text: title }), ...content);
}

function addressText(address) {
  const lines = [address.address1, address.address2, `${address.city}, ${address.state} ${address.zip}`, address.country];
  return lines.filter(Boolean).join("\n");
}

function rawSection(raw) {
  return el("details", { class: "db-section" }, el("summary", { text: "Raw JSON" }), rawJson(raw));
}

function userBody(record) {
  const { row, raw } = record;
  return [
    section("Profile", keyValues([["User ID", row.id], ["Name", row.name], ["Email", row.email]])),
    section("Address", el("pre", { text: addressText(raw.address) })),
    section(
      `Payment methods (${record.payment_methods.length})`,
      table(
        [["ID"], ["Type"], ["Detail"]],
        record.payment_methods.map((method) =>
          el("tr", {}, cell(recordLink("payment", method.id), "nowrap"), cell(method.source), cell(method.detail)),
        ),
      ),
    ),
    section(`Orders (${record.orders.length})`, table(ORDER_HEADERS, orderRows(record.orders))),
    rawSection(raw),
  ];
}

function orderBody(record) {
  const { row, raw } = record;
  const payments = raw.payment_history.map((payment) =>
    el(
      "tr",
      {},
      cell(payment.transaction_type),
      cell(formatPrice(payment.amount), "num"),
      cell(recordLink("payment", payment.payment_method_id), "nowrap"),
    ),
  );
  const fulfillments = raw.fulfillments.map((entry) =>
    el(
      "tr",
      {},
      cell(entry.tracking_id.join(", "), "mono"),
      cell(entry.item_ids.flatMap((id, index) => [index ? ", " : "", recordLink("item", id)])),
    ),
  );
  return [
    section(
      "Order",
      el(
        "dl",
        { class: "kv" },
        el("dt", { text: "Status" }),
        el("dd", { text: row.status }),
        el("dt", { text: "Customer" }),
        el("dd", {}, recordLink("user", row.user_id), ` (${row.user_name})`),
        el("dt", { text: "Total" }),
        el("dd", { text: formatPrice(row.total) }),
      ),
    ),
    section(
      `Items (${record.items.length})`,
      table(
        [["Item"], ["Product"], ["Options"], ["Price", "num"], ["In store now"]],
        record.items.map((item) =>
          el(
            "tr",
            {},
            cell(recordLink("item", item.item_id), "nowrap"),
            cell(recordLink("product", item.product_id, item.name)),
            cell(optionsText(item.options)),
            cell(formatPrice(item.price), "num"),
            cell(availability(item.available_now)),
          ),
        ),
      ),
    ),
    section("Payments", table([["Type"], ["Amount", "num"], ["Method"]], payments)),
    section("Shipping address", el("pre", { text: addressText(raw.address) })),
    fulfillments.length > 0 && section("Fulfillment", table([["Tracking"], ["Items"]], fulfillments)),
    rawSection(raw),
  ];
}

function productBody(record, highlightItem) {
  const { row, raw } = record;
  const optionNames = [...new Set(record.variants.flatMap((variant) => Object.keys(variant.options)))];
  const rows = record.variants.map((variant) =>
    el(
      "tr",
      { class: variant.id === highlightItem ? "highlight" : null, id: `variant-${variant.id}` },
      cell(recordLink("item", variant.id), "nowrap"),
      optionNames.map((name) => cell(variant.options[name] ?? "")),
      cell(formatPrice(variant.price), "num"),
      cell(availability(variant.available)),
    ),
  );
  return [
    el("p", { class: "db-headline", text: `${row.name}: ${plural(row.variants, "variant")}, ${row.available} available` }),
    section(
      "Product",
      keyValues([
        ["Product ID", row.id],
        ["Price range", `${formatPrice(row.price_min)} to ${formatPrice(row.price_max)}`],
      ]),
    ),
    section(
      "Variants",
      table([["Item"], ...optionNames.map((name) => [name]), ["Price", "num"], ["In store"]], rows),
    ),
    rawSection(raw),
  ];
}

function paymentBody(record) {
  const { row, raw } = record;
  return [
    section(
      "Payment method",
      el(
        "dl",
        { class: "kv" },
        el("dt", { text: "ID" }),
        el("dd", { class: "mono", text: row.id }),
        el("dt", { text: "Type" }),
        el("dd", { text: row.source }),
        row.detail && el("dt", { text: "Detail" }),
        row.detail && el("dd", { text: row.detail }),
        el("dt", { text: "Owner" }),
        el("dd", {}, recordLink("user", row.user_id), ` (${row.user_name})`),
      ),
    ),
    section(
      `Orders that used it (${record.orders.length})`,
      record.orders.length ? table(ORDER_HEADERS, orderRows(record.orders)) : el("p", { class: "subtle small", text: "None." }),
    ),
    rawSection(raw),
  ];
}

function recordView(kind, record) {
  if (kind === "user") return { title: `User ${record.row.name}`, body: userBody(record) };
  if (kind === "order") return { title: `Order ${record.id}`, body: orderBody(record) };
  if (kind === "payment") return { title: `Payment method ${record.id}`, body: paymentBody(record) };
  if (kind === "item") {
    return { title: `Item ${record.id}`, body: productBody(record.product, record.id), highlight: record.id };
  }
  return { title: record.row.name, body: productBody(record) };
}

async function fetchRecord(kind, id) {
  return api(`/api/db/${API_KIND[kind]}/${enc(id)}`);
}

// Open a record beside the conversation, keeping the reader's place.
export async function openRecord(kind, id, opener) {
  const previous = currentView();
  drawerLoading(`Loading ${id}`, opener);
  const back = previous ?? undefined;
  try {
    const record = await fetchRecord(kind, id);
    const { title, body, highlight } = recordView(kind, record);
    openDrawer({
      title,
      subtitle: INITIAL_STATE,
      back,
      tabs: [{ label: "Record", render: () => el("div", {}, recordLinkHeader(kind, id), body) }],
    });
    if (highlight) {
      requestAnimationFrame(() => document.getElementById(`variant-${highlight}`)?.scrollIntoView({ block: "center" }));
    }
  } catch (error) {
    openDrawer({ title: id, subtitle: error.message, back, tabs: [] });
  }
}

function recordLinkHeader(kind, id) {
  return el(
    "p",
    { class: "small" },
    el("a", { href: recordHref(kind, id), target: "_blank", rel: "noopener", text: "Open in explorer" }),
  );
}

export async function renderRecordPage(view, kind, id, setCrumbs) {
  await loadIds();
  setLinking(true);
  setCrumbs({ label: id });
  document.title = `${id} · Database`;
  const record = await fetchRecord(kind, id);
  const { title, body, highlight } = recordView(kind, record);
  view.replaceChildren(
    el("div", { class: "page-head" }, el("h1", { text: title })),
    el("p", { class: "caption", text: INITIAL_STATE }),
    el("div", { class: "db-record" }, body),
  );
  if (highlight) document.getElementById(`variant-${highlight}`)?.scrollIntoView({ block: "center" });
}

// ---- Explorer lists ----

const COLUMNS = {
  users: {
    headers: [["User"], ["Name"], ["Email"], ["City"], ["Zip"], ["Orders", "num"], ["Payment methods", "num"]],
    row: (r) => [
      cell(recordLink("user", r.id), "nowrap"),
      cell(r.name),
      cell(r.email),
      cell(`${r.city}, ${r.state}`),
      cell(r.zip),
      cell(String(r.orders), "num"),
      cell(String(r.payment_methods), "num"),
    ],
  },
  orders: {
    headers: [["Order"], ["Customer"], ["Status"], ["Items", "num"], ["Total", "num"], ["Zip"]],
    row: (r) => [
      cell(recordLink("order", r.id), "nowrap"),
      cell([recordLink("user", r.user_id, r.user_name)]),
      cell(r.status),
      cell(String(r.items), "num"),
      cell(formatPrice(r.total), "num"),
      cell(r.zip),
    ],
  },
  products: {
    headers: [["Product"], ["Product ID"], ["Variants", "num"], ["Available", "num"], ["Price range", "num"]],
    row: (r) => [
      cell(recordLink("product", r.id, r.name)),
      cell(r.id, "mono"),
      cell(String(r.variants), "num"),
      cell(String(r.available), "num"),
      cell(`${formatPrice(r.price_min)} to ${formatPrice(r.price_max)}`, "num nowrap"),
    ],
  },
  items: {
    headers: [["Item"], ["Product"], ["Options"], ["Price", "num"], ["In store"]],
    row: (r) => [
      cell(recordLink("item", r.id), "nowrap"),
      cell(recordLink("product", r.product_id, r.product_name)),
      cell(optionsText(r.options)),
      cell(formatPrice(r.price), "num"),
      cell(availability(r.available)),
    ],
  },
};

function explorerHash(kind, query) {
  return `#/db?tab=${kind}${query ? `&q=${enc(query)}` : ""}`;
}

export async function renderExplorer(view, kind, query, setCrumbs) {
  await loadIds();
  setLinking(true);
  setCrumbs();
  document.title = "Database · Trace Viewer";
  const summary = await api("/api/db/summary");

  const body = el("div");
  const status = el("span", { class: "subtle small" });
  let offset = 0;
  let requestId = 0;

  const load = async () => {
    const current = ++requestId;
    const text = input.value.trim();
    const page = await api(`/api/db/${kind}?q=${enc(text)}&offset=${offset}&limit=${PAGE_SIZE}`);
    if (current !== requestId) return;
    const columns = COLUMNS[kind];
    const first = page.total ? offset + 1 : 0;
    status.textContent = `${first} to ${Math.min(offset + PAGE_SIZE, page.total)} of ${page.total}`;
    previous.disabled = offset === 0;
    next.disabled = offset + PAGE_SIZE >= page.total;
    body.replaceChildren(
      page.rows.length
        ? table(columns.headers, page.rows.map((row) => el("tr", {}, columns.row(row))))
        : el("div", { class: "empty", text: "No records match this search." }),
    );
    history.replaceState(null, "", explorerHash(kind, text));
  };

  let timer = null;
  const input = el("input", {
    class: "db-search",
    type: "search",
    value: query,
    placeholder: "Search IDs, names, emails, zip codes, statuses, products, options",
    "aria-label": "Search the database",
    oninput: () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        offset = 0;
        load();
      }, 180);
    },
  });
  const pageButton = (text, change) =>
    el("button", {
      class: "link-button",
      type: "button",
      text,
      onclick: () => {
        offset = Math.max(0, offset + change);
        load();
      },
    });
  const previous = pageButton("Previous", -PAGE_SIZE);
  const next = pageButton("Next", PAGE_SIZE);

  view.replaceChildren(
    el(
      "div",
      { class: "page-head" },
      el("h1", { text: "Database" }),
      el("span", { class: "subtle small", text: "Initial state of the retail store, from db.json" }),
    ),
    el(
      "nav",
      { class: "db-tabs", "aria-label": "Record types" },
      TABS.map((tab) =>
        el("a", {
          class: `tab ${tab.kind === kind ? "active" : ""}`,
          href: explorerHash(tab.kind, input.value.trim()),
          text: `${tab.label} (${summary[tab.kind].toLocaleString()})`,
        }),
      ),
    ),
    el("div", { class: "toolbar" }, input, status, previous, next),
    body,
  );
  await load();
  input.focus();
}
