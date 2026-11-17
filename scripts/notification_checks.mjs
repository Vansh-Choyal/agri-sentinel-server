import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {JSDOM} from "jsdom";

export async function checkNotifications(html, mainWindow, navigate, wait) {
  const dom = new JSDOM(html, {url: "https://farm.test/#overview", runScripts: "outside-only"});
  const w = dom.window, d = w.document, jobs = new Map();
  let now = 1000000, sequence = 0, received;
  w.Date.now = () => now;
  w.Math.random = () => .5;
  w.setTimeout = (callback, delay) => { jobs.set(++sequence, {callback, at: now + delay}); return sequence; };
  w.clearTimeout = (id) => jobs.delete(id);
  Object.defineProperty(d, "hidden", {value: false, configurable: true});
  const alert = {id: 701, robot_id: "rv-001", zone_id: "zone-a", status: "active",
    title: "Low soil moisture <img src=x>", detail: "Soil moisture: 23 %. Trigger <25; recovery >=29 %.", opened_at: "2026-11-17T12:00:00Z"};
  let data = {alerts: [], robots: [{id: "rv-001", name: "Sentinel 01", source: "demo"}]};
  const refresh = () => w.dispatchEvent(new w.CustomEvent("farm:updated", {detail: data}));
  const next = (fresh = true) => {
    const [id, job] = [...jobs].sort((a,b) => a[1].at-b[1].at)[0];
    jobs.delete(id); now = job.at;
    if (fresh) refresh();
    job.callback();
  };
  try {
    w.addEventListener("chat:alert", (event) => { received = event.detail; });
    w.eval(await readFile(new URL("../agrisentinel/static/js/notifications.js", import.meta.url), "utf8"));
    refresh(); next();
    assert.ok(d.querySelector("#alert-notification").hidden, "Do not manufacture alerts when there are none");
    data = {...data, alerts: [alert]}; next();
    assert.equal(d.querySelector("#alert-notification").hidden, false);
    assert.ok(d.querySelector("#alert-notification-label").textContent.includes("Demo alert"));
    assert.equal(d.querySelector("#alert-notification-title img"), null, "Alert content stays text");
    d.querySelector("#alert-notification-open").click();
    assert.equal(w.location.hash, "#chatbot");
    assert.equal(received.id, 701);
    assert.equal(received.source, "demo");
    assert.ok(d.querySelector("#alert-notification").hidden);
    w.location.hash = "overview";
    next();
    assert.ok(d.querySelector("#alert-notification").hidden, "Repeated alerts have a cooldown");
    next(); next();
    assert.equal(d.querySelector("#alert-notification").hidden, false);
    data = {...data, alerts: [{...alert, status: "acknowledged"}]}; refresh();
    assert.ok(d.querySelector("#alert-notification").hidden, "Acknowledged/resolved alerts stop notifying");
    data = {...data, alerts: [{...alert, id: 702}]};
    next(false);
    assert.ok(d.querySelector("#alert-notification").hidden, "Stale snapshots do not notify");
    Object.defineProperty(d, "hidden", {value: true, configurable: true});
    d.dispatchEvent(new w.Event("visibilitychange"));
    assert.equal(jobs.size, 0, "Hidden tabs cancel timers");
    Object.defineProperty(d, "hidden", {value: false, configurable: true});
    d.dispatchEvent(new w.Event("visibilitychange"));
    next();
    assert.equal(d.querySelector("#alert-notification").hidden, false);
    d.querySelector("#alert-notification-dismiss").click();
    assert.ok(d.querySelector("#alert-notification").hidden);
    w.dispatchEvent(new w.Event("pagehide"));
    assert.equal(jobs.size, 0);
  } finally { dom.window.close(); }

  // Exercise the real chatbot handler, including language selection and draft
  // preservation. Opening a notification must never submit a model request.
  const input = mainWindow.document.querySelector("#chat-input");
  input.value = "My existing draft";
  const originalFetch = mainWindow.fetch;
  let submitted = false;
  mainWindow.fetch = (path, options) => {
    if (path === "/api/v1/chat") submitted = true;
    return originalFetch(path, options);
  };
  try {
    await navigate("chatbot");
    mainWindow.dispatchEvent(new mainWindow.CustomEvent("chat:alert", {detail: {...alert, source: "demo"}}));
    await wait(() => input.value.includes("Explain alert #701"), "alert question ready in chatbot");
    assert.ok(input.value.startsWith("My existing draft"));
    assert.ok(input.value.includes("source: demo"));
    assert.equal(submitted, false);
  } finally { mainWindow.fetch = originalFetch; }
  process.stdout.write("PASS randomized alert notices, source labels, dismissal/cooldown, stale/hidden-tab suppression and chatbot draft handoff\n");
}
