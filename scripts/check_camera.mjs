/** Camera integration against an ISOLATED server on 127.0.0.1:5001.
 * Real HTTP/JPEG bytes; jsdom image events are simulated, not browser rendering.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { JSDOM, VirtualConsole } from "jsdom";

const base = "http://127.0.0.1:5001";
const cookies = new Map();
async function http(path, options = {}) {
  const response = await fetch(new URL(path, base), {
    redirect: "manual", ...options,
    headers: { Cookie: [...cookies].map(([k, v]) => `${k}=${v}`).join("; "), ...options.headers },
  });
  for (const cookie of response.headers.getSetCookie()) {
    const pair = cookie.split(";")[0], index = pair.indexOf("=");
    cookies.set(pair.slice(0, index), pair.slice(index + 1));
  }
  return response;
}
const login = await (await http("/login")).text();
const token = login.match(/name="csrf_token" value="([^"]+)"/)[1];
assert.equal((await http("/login", { method: "POST", body: new URLSearchParams({
  username: "operator", password: "test-camera-dom-password", csrf_token: token,
}) })).status, 302);
const html = await (await http("/")).text();
const csrf = html.match(/name="csrf-token" content="([^"]+)"/)[1];
const robotId = "rv-camera-check-" + Date.now();
const registered = await http("/api/v1/robots", {
  method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
  body: JSON.stringify({ id: robotId, name: "Software camera test", source: "hardware" }),
});
assert.equal(registered.status, 201);
const { api_key: key } = await registered.json();
const jpeg = await readFile(new URL("../tests/fixtures/camera-test.jpg", import.meta.url));
const path = `/api/v1/robots/${robotId}/camera`;
async function upload() {
  assert.equal((await http(path + "/frame", { method: "POST", body: jpeg,
    headers: { "Content-Type": "image/jpeg", Authorization: `Bearer ${key}` },
  })).status, 200);
}
const errors = [];
const dom = new JSDOM(html, { url: base + "/#rovers", runScripts: "outside-only", pretendToBeVisual: true,
  virtualConsole: new VirtualConsole().on("jsdomError", (e) => errors.push(e.message)),
});
const w = dom.window, d = w.document;
w.AbortController = globalThis.AbortController;
let statusReads = 0;
let failNextCameraRequest = false;
const blobs = new Map();
let blobCounter = 0;
w.URL.createObjectURL = (blob) => {
  const url = "blob:camera-test-" + (++blobCounter);
  blobs.set(url, blob);
  return url;
};
w.URL.revokeObjectURL = (url) => blobs.delete(url);
w.fetch = (url, options) => {
  if (url.startsWith(path + "/latest.jpg")) {
    statusReads++;
    if (failNextCameraRequest) {
      failNextCameraRequest = false;
      return Promise.reject(new Error("Simulated LAN interruption"));
    }
  }
  assert.notEqual(url, path, "Viewer must not make an extra JSON status request per frame");
  return http(url, options);
};
w.Chart = class {
  static defaults = { font: {}, plugins: { legend: {} } };
  constructor() {} update() {} destroy() {}
};
w.EventSource = class { addEventListener() {} close() {} };
w.matchMedia = () => ({ matches: false });
w.scrollTo = () => {};
w.HTMLElement.prototype.scrollIntoView = () => {};
w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
w.HTMLDialogElement.prototype.close = function () {
  this.open = false;
  this.dispatchEvent(new w.Event("close"));
};
w.addEventListener("error", (e) => errors.push(e.message));
async function wait(predicate, label, timeout = 6000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error("Timed out: " + label + " / " + d.querySelector("#detail-content")?.textContent + " / " + errors.join("; ") + " / " + d.querySelector("#toast")?.textContent);
}
try {
  w.eval(await readFile(new URL("../agrisentinel/static/js/dashboard.js", import.meta.url), "utf8"));
  await wait(() => d.querySelector(`[data-rover="${robotId}"]`), "hardware rover in fleet");
  d.querySelector(`[data-rover="${robotId}"]`).click();
  await wait(() => d.querySelector("#camera-status")?.textContent === "Waiting for camera", "waiting state");
  assert.equal(d.querySelector("#camera-image").hidden, true);
  assert.equal((await http(path)).status, 200);
  await upload();
  await wait(() => d.querySelector("#camera-image").hasAttribute("src"), "JPEG source");
  const image = d.querySelector("#camera-image");
  const response = blobs.get(image.getAttribute("src"));
  assert.equal(response.type, "image/jpeg");
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), jpeg);
  // jsdom does not decode/rasterize images: signal load after checking real HTTP bytes.
  image.dispatchEvent(new w.Event("load"));
  assert.equal(d.querySelector("#camera-status").textContent, "Live");
  assert.equal(image.hidden, false);
  assert.equal(d.querySelector("#camera-placeholder").hidden, true);
  d.querySelector("#camera-expand").click();
  assert.ok(d.querySelector("#rover-camera").classList.contains("camera-expanded"));
  d.querySelector("#camera-expand").click();
  assert.equal(d.querySelector("#camera-expand").getAttribute("aria-expanded"), "false");
  const previous = image.getAttribute("src");
  await upload();
  await wait(() => image.getAttribute("src") !== previous, "next uploaded image");
  image.dispatchEvent(new w.Event("error"));
  assert.equal(d.querySelector("#camera-status").textContent, "Camera unavailable");
  await wait(() => d.querySelector("#camera-status").textContent === "Loading image", "retry failed image");
  image.dispatchEvent(new w.Event("load"));
  await wait(() => d.querySelector("#camera-status").textContent === "Camera offline", "offline timeout", 11000);
  assert.ok(d.querySelector("#camera-caption").textContent.includes("stale"));
  assert.equal(image.hidden, false);
  const offlineSource = image.getAttribute("src");
  await upload();
  await wait(() => image.hasAttribute("src") && image.getAttribute("src") !== offlineSource, "reconnect");
  image.dispatchEvent(new w.Event("load"));
  assert.equal(d.querySelector("#camera-status").textContent, "Live");
  // Exercise the faster cadence against real HTTP, not just a timer constant.
  let decodedSource = image.getAttribute("src"), decoding = false, displayed = 0;
  const decoder = setInterval(async () => {
    const source = image.getAttribute("src");
    if (!source || source === decodedSource || decoding) return;
    decoding = true;
    try {
      const frame = blobs.get(source);
      assert.equal(frame.type, "image/jpeg");
      assert.deepEqual(Buffer.from(await frame.arrayBuffer()), jpeg);
      if (image.getAttribute("src") === source) {
        decodedSource = source;
        image.dispatchEvent(new w.Event("load"));
        displayed++;
      }
    } catch (error) {
      errors.push(error.message);
    } finally {
      decoding = false;
    }
  }, 5);
  const cadenceStarted = performance.now();
  try {
    for (let index = 0; index < 60; index++) {
      const deadline = cadenceStarted + index * 50;
      await new Promise((resolve) => setTimeout(resolve, Math.max(0, deadline - performance.now())));
      await upload();
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  } finally {
    clearInterval(decoder);
  }
  const displayedFps = displayed * 1000 / (performance.now() - cadenceStarted);
  assert.ok(displayedFps >= 18.5, `Camera viewer cadence too slow: ${displayedFps.toFixed(1)} fps`);
  assert.match(d.querySelector("#camera-caption").textContent, /[\d.]+ fps/);
  console.log(`PASS 20 fps publisher: ${displayed} distinct images displayed, ${displayedFps.toFixed(1)} fps in HTTP/DOM simulation`);
  const beforeRaw = image.getAttribute("src");
  assert.equal((await http(path + "/frame", { method: "POST", body: Buffer.alloc(160 * 120, 0xe0),
    headers: { "Content-Type": "application/x-rgb332", "X-Camera-Width": "160", "X-Camera-Height": "120", Authorization: `Bearer ${key}` },
  })).status, 200);
  await wait(() => image.hasAttribute("src") && image.getAttribute("src") !== beforeRaw, "server-encoded raw frame");
  const serverJpeg = Buffer.from(await blobs.get(image.getAttribute("src")).arrayBuffer());
  assert.equal(serverJpeg.readUInt16BE(0), 0xffd8);
  image.dispatchEvent(new w.Event("load"));
  assert.match(d.querySelector("#camera-caption").textContent, /160.*120/);
  const beforeQuality = image.getAttribute("src");
  const fullColor = Buffer.alloc(320 * 240 * 2).fill(Buffer.from([0x07, 0xe0]));
  assert.equal((await http(path + "/frame", { method: "POST", body: fullColor,
    headers: { "Content-Type": "application/x-rgb565", "X-Camera-Width": "320", "X-Camera-Height": "240", Authorization: `Bearer ${key}` },
  })).status, 200);
  await wait(() => image.hasAttribute("src") && image.getAttribute("src") !== beforeQuality, "full-color quality frame");
  assert.equal(blobs.get(image.getAttribute("src")).type, "image/jpeg");
  image.dispatchEvent(new w.Event("load"));
  assert.match(d.querySelector("#camera-caption").textContent, /320.*240/);
  failNextCameraRequest = true;
  await wait(() => d.querySelector("#camera-status").textContent === "Camera connection lost", "LAN interruption");
  await wait(() => d.querySelector("#camera-status").textContent === "Live", "LAN recovery");
  // Hidden tabs stop camera requests; closing the panel also stops polling.
  Object.defineProperty(d, "hidden", { configurable: true, value: true });
  await new Promise((resolve) => setTimeout(resolve, 650));
  const hiddenCount = statusReads;
  await new Promise((resolve) => setTimeout(resolve, 650));
  assert.equal(statusReads, hiddenCount);
  Object.defineProperty(d, "hidden", { configurable: true, value: false });
  await wait(() => statusReads > hiddenCount, "resume visible tab");
  d.querySelector("#detail-dialog").close();
  await new Promise((resolve) => setTimeout(resolve, 100));
  const closedCount = statusReads;
  await new Promise((resolve) => setTimeout(resolve, 650));
  assert.equal(statusReads, closedCount);
  assert.equal(image.hasAttribute("src"), false);
  assert.equal(blobs.size, 0, "All camera object URLs must be released on close");
  assert.deepEqual(errors, []);
  console.log("PASS camera HTTP/JPEG, waiting/live/offline/reconnect, image retry, expand/collapse, hidden-tab pause and close cleanup");
} finally {
  dom.window.close();
}
