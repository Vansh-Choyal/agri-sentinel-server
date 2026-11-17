/** Optional DOM integration QA against a real local backend. No visual browser claims.
 * Charts are instrumented, not rasterized. All dashboard measurements come from HTTP.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";
import { checkChat } from "./chat_checks.mjs";

try {
  process.loadEnvFile(fileURLToPath(new URL("../.env", import.meta.url)));
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}

const base = process.env.SIM_API_URL || "http://127.0.0.1:5000";
const cookies = new Map();
async function http(path, options = {}) {
  const response = await fetch(new URL(path, base), {
    redirect: "manual",
    ...options,
    headers: {
      Cookie: [...cookies].map(([k, v]) => `${k}=${v}`).join("; "),
      ...options.headers,
    },
  });
  for (const cookie of response.headers.getSetCookie()) {
    const [pair] = cookie.split(";");
    const index = pair.indexOf("=");
    cookies.set(pair.slice(0, index), pair.slice(index + 1));
  }
  return response;
}
const login = await (await http("/login")).text();
const csrf = login.match(/name="csrf_token" value="([^"]+)"/)[1];
let password = process.env.ADMIN_PASSWORD;
if (!password)
  password = (
    await readFile(
      new URL("../instance/admin-credentials.txt", import.meta.url),
      "utf8",
    )
  )
    .split("Password: ")[1]
    .trim();
const signed = await http("/login", {
  method: "POST",
  body: new URLSearchParams({
    username: process.env.ADMIN_USERNAME || "operator",
    password,
    csrf_token: csrf,
  }),
});
assert.equal(signed.status, 302, "Operator login must succeed");
const html = await (await http("/")).text();
const snapshot = await (await http("/api/v1/overview")).json();
const errors = [];
const console = new VirtualConsole();
console.on("jsdomError", (error) => errors.push(error.message));
const dom = new JSDOM(html, {
  url: base + "/",
  runScripts: "outside-only",
  pretendToBeVisual: true,
  virtualConsole: console,
});
const w = dom.window,
  d = w.document;
const charts = [];
class InstrumentedChart {
  static defaults = { font: {}, plugins: { legend: {} } };
  constructor(canvas, config) {
    this.id = canvas.id;
    this.data = config.data;
    this.options = config.options;
    charts.push(this);
  }
  update() {}
  destroy() {
    this.destroyed = true;
  }
}
w.Chart = InstrumentedChart;
w.EventSource = class {
  addEventListener() {}
  close() {}
};
w.fetch = (path, options) => http(path, options);
w.matchMedia = () => ({ matches: false });
w.scrollTo = () => {};
w.HTMLDialogElement.prototype.showModal = function () {
  this.open = true;
};
w.HTMLDialogElement.prototype.close = function () {
  this.open = false;
  this.dispatchEvent(new w.Event("close"));
};
w.addEventListener("error", (event) => errors.push(event.message));
w.eval(
  await readFile(
    new URL("../agrisentinel/static/js/dashboard.js", import.meta.url),
    "utf8",
  ),
);
const wait = async (fn, label) => {
  const end = Date.now() + 10000;
  while (Date.now() < end) {
    if (fn()) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error("Timed out: " + label);
};
const click = (selector) => {
  const element = d.querySelector(selector);
  assert.ok(element, selector);
  element.dispatchEvent(
    new w.MouseEvent("click", { bubbles: true, cancelable: true }),
  );
};
const navigate = async (page) => {
  w.location.hash = page;
  await wait(
    () => !d.querySelector("#page-" + page).hidden,
    "navigate " + page,
  );
};
try {
  await wait(
    () =>
      d.querySelectorAll("#overview-map [data-zone]").length ===
      snapshot.zones.length,
    "field map",
  );
  assert.equal(
    Number(d.querySelector("#overview-metrics strong").textContent),
    snapshot.counts.total,
  );
  assert.equal(
    d.querySelectorAll("#overview-fleet tbody tr").length,
    snapshot.robots.length,
  );
  assert.ok(!d.querySelector("#overview-map").textContent.includes("NaN"));
  assert.equal(d.querySelector("#demo-notice").hidden, !snapshot.demo);
  await wait(
    () => charts.some((c) => c.id === "overview-chart"),
    "overview chart dataset",
  );
  process.stdout.write(
    "PASS overview metrics, field partitions, fleet rows and chart data from HTTP\n",
  );

  await navigate("field");
  await wait(
    () => d.querySelectorAll("#detail-map [data-zone]").length > 0,
    "field details",
  );
  click('#detail-map [data-zone="zone-b"]');
  await wait(
    () =>
      d.querySelector("#detail-inspector").textContent.includes("Central Plot"),
    "zone selection",
  );
  assert.ok(
    d
      .querySelector("#detail-inspector")
      .textContent.includes("checkpoints reported today"),
  );
  process.stdout.write(
    "PASS interactive zone selection and reported inspection coverage\n",
  );

  await navigate("rovers");
  click('#rover-table [data-rover="rv-001"]');
  await wait(
    () =>
      d
        .querySelector("#detail-content")
        .textContent.includes("Device commands"),
    "rover details",
  );
  assert.ok(d.querySelector("#detail-content").textContent.includes(
    snapshot.robots[0].battery_pct === null ? "Not reported" : String(snapshot.robots[0].battery_pct)
  ));
  assert.equal(d.querySelectorAll("[data-detail-sensor]").length, 9);
  click("#detail-dialog [data-close-dialog]");
  click("#heading-action");
  assert.ok(d.querySelector("#register-dialog").open);
  assert.ok(d.querySelector("#reg-source"));
  click("#register-dialog [data-close-dialog]");
  process.stdout.write(
    "PASS rover detail, sensor values, command controls and registration form\n",
  );

  await navigate("analytics");
  await wait(
    () => d.querySelector("#analytics-stats strong"),
    "analytics response",
  );
  for (const button of d.querySelectorAll("[data-metric]")) {
    button.click();
    assert.equal(
      d.querySelector("#analytics-chart-title").textContent,
      button.textContent,
    );
    assert.equal(button.getAttribute("aria-pressed"), "true");
    if (snapshot.demo) {
      const chart = charts.findLast((c) => c.id === "analytics-chart" && !c.destroyed);
      for (const series of chart.data.datasets) {
        assert.ok(series.data.length > 1, "Demo curve needs multiple stored samples");
        assert.ok(series.data.every((point) => point.y !== null), "Empty aggregation buckets must not fragment demo curves");
        assert.ok(series.spanGaps > 0, "Demo curve uses a bounded gap between sampled points");
      }
    }
  }
  if (snapshot.demo) {
    const originalFetch = w.fetch;
    w.fetch = async (path, options) => {
      const response = await originalFetch(path, options);
      if (!path.startsWith("/api/v1/analytics?")) return response;
      const data = await response.json();
      data.sources = {hardware: data.record_count};
      for (const zone of data.zones) zone.sources = {hardware: zone.count};
      // This network double verifies that hardware gaps are preserved by the
      // renderer; it never writes telemetry or changes the running database.
      for (const metric of d.querySelectorAll("[data-metric]")) data.points[0][metric.dataset.metric] = null;
      return new Response(JSON.stringify(data), {headers: {"Content-Type": "application/json"}});
    };
    d.querySelector("#analytics-period").dispatchEvent(new w.Event("change", {bubbles: true}));
    await wait(() => charts.findLast((c) => c.id === "analytics-chart" && !c.destroyed)?.data.datasets[0]?.spanGaps === false,
      "hardware gaps preserved");
    const hardwareChart = charts.findLast((c) => c.id === "analytics-chart" && !c.destroyed);
    assert.ok(hardwareChart.data.datasets.some((series) => series.data.some((point) => point.y === null)));
    w.fetch = originalFetch;
    d.querySelector("#analytics-period").dispatchEvent(new w.Event("change", {bubbles: true}));
    await wait(() => charts.findLast((c) => c.id === "analytics-chart" && !c.destroyed)?.data.datasets[0]?.spanGaps > 0,
      "demo curves restored");
  }
  for (const checkbox of d.querySelectorAll("#analytics-zones input"))
    checkbox.checked = false;
  d.querySelector("#analytics-zones").dispatchEvent(
    new w.Event("change", { bubbles: true }),
  );
  await wait(
    () => !d.querySelector("#analytics-chart-empty").hidden,
    "empty zone selection",
  );
  assert.ok(
    d
      .querySelector("#analytics-comparison")
      .textContent.includes("No comparable measurements"),
  );
  process.stdout.write(
    "PASS all nine measurement views and missing-data/empty selection handling\n",
  );

  await navigate("alerts");
  await wait(
    () => d.querySelector("#alerts-list").children.length > 0,
    "alerts",
  );
  click("#settings-button");
  await wait(() => d.querySelectorAll(".rule-form").length === 7, "rules");
  click("#rules-dialog [data-close-dialog]");
  const downloads = [];
  const blobs = new Map();
  let printed = 0;
  w.URL.createObjectURL = (blob) => { const url = "blob:test-" + blobs.size; blobs.set(url, blob); return url; };
  w.URL.revokeObjectURL = () => {};
  w.HTMLAnchorElement.prototype.click = function () { downloads.push({name: this.download, blob: blobs.get(this.href)}); };
  w.print = () => { printed++; };
  await navigate("reports");
  await wait(() => d.querySelector(".report-banner"), "report generation");
  assert.ok(
    d.querySelector("#report-content").textContent.includes("No crop health"),
  );
  assert.ok(charts.some((c) => c.id === "report-chart"));
  const exported = await http(
    d.querySelector("#csv-export").getAttribute("href"),
  );
  assert.equal(exported.status, 200);
  assert.ok((await exported.text()).includes("Environmental trends"));
  for (const period of ["daily", "weekly", "monthly"]) {
    d.querySelector("#report-period").value = period;
    d.querySelector("#report-zone").value = "zone-c";
    d.querySelector("#report-end").value = new Date(Date.now() - 3600000).toISOString().slice(0,16);
    d.querySelector("#report-form").dispatchEvent(new w.Event("submit", {bubbles:true, cancelable:true}));
    await wait(() => d.querySelector("#csv-export").href.includes("period=" + period) &&
      d.querySelector("#csv-export").href.includes("zones=zone-c") &&
      d.querySelector("#report-content").getAttribute("aria-busy") === "false", "filtered " + period + " report");
    const url = new URL(d.querySelector("#csv-export").href);
    assert.ok(url.searchParams.get("from") && url.searchParams.get("to"), "CSV freezes displayed report bounds");
    const csv = await (await http(url.pathname + url.search)).text();
    assert.ok(csv.includes("Central Plot") === false);
    assert.ok(csv.includes("East Plot"));
  }
  click("#csv-export");
  await wait(() => downloads.length === 1, "CSV button download");
  assert.ok((await downloads[0].blob.text()).includes("East Plot"));
  click("#heading-action");
  await wait(() => downloads.length === 2, "heading CSV download");
  assert.ok(downloads[1].name.includes("monthly"));
  click("#print-report");
  await wait(() => printed === 1, "print / PDF action");
  assert.deepEqual(errors, []);
  assert.ok(d.querySelector("#error-banner").hidden);
  process.stdout.write(
    "PASS alert configuration, reports, environmental trends and CSV download\n",
  );
  // Navigation must remain usable with a keyboard on a narrow viewport.
  assert.ok(!d.body.textContent.includes("LOCAL INSTANCE"));
  assert.ok(!d.querySelector(".nav-key, .workspace-chevron, .title-dot"));
  for (const dialog of d.querySelectorAll("dialog")) {
    assert.ok(d.getElementById(dialog.getAttribute("aria-labelledby")), "Dialog needs a visible label");
  }
  w.matchMedia = (query) => ({ matches: query === "(max-width: 760px)" });
  w.dispatchEvent(new w.Event("resize"));
  assert.equal(d.querySelector("#sidebar").inert, true);
  click("#menu-button");
  assert.equal(d.querySelector("#menu-button").getAttribute("aria-expanded"), "true");
  assert.equal(d.activeElement.id, "close-navigation");
  const lastSidebarControl = d.querySelector("#sidebar .operator button");
  lastSidebarControl.focus();
  d.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
  assert.equal(d.activeElement.id, "close-navigation");
  d.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
  assert.equal(d.querySelector("#sidebar").inert, true);
  assert.equal(d.activeElement.id, "menu-button");
  assert.ok(d.querySelector("#navigation-backdrop").hidden);
  process.stdout.write("PASS removed decoration, dialog names and mobile keyboard navigation\n");
  await checkChat(w, d, wait, navigate);
  assert.deepEqual(errors, []);
  process.stdout.write("Frontend DOM checks passed. Chart rasterization and responsive visual QA require a browser.\n");
} finally {
  dom.window.close();
}
