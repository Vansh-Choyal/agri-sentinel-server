import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

/** Deterministic network/audio doubles exercise the real streaming and voice UI. */
export async function checkChat(w, d, wait, navigate) {
  const originalFetch = w.fetch, calls = [], encoder = new TextEncoder();
  let mode = "normal", streamControl, getMic, stopped = 0, startedAudio = 0, micFrames = 0, outputFrames = 0;
  let worklet, fakeTime = 1000;
  w.Blob = Blob; w.FormData = FormData; w.TextDecoder = TextDecoder;
  const json = (value, status = 200) => new Response(JSON.stringify(value), {status, headers: {"Content-Type": "application/json"}});
  const event = (type, value) => encoder.encode(`event: ${type}\ndata: ${JSON.stringify(value)}\n\n`);
  const chats = () => calls.filter((c) => c.path === "/api/v1/chat");
  w.fetch = async (path, options = {}) => {
    if (!path.startsWith("/api/v1/chat")) return originalFetch(path, options);
    assert.ok(options.headers["X-CSRF-Token"]); calls.push({path, options});
    if (path.endsWith("/config")) return json({configured: true});
    if (path.endsWith("/transcribe")) {
      assert.equal(options.body.get("language"), "hi");
      const bytes = Buffer.from(await options.body.get("audio").arrayBuffer());
      assert.equal(bytes.subarray(0, 4).toString(), "RIFF"); assert.equal(bytes.readUInt32LE(24), 16000);
      return json({text: "What needs attention?"});
    }
    if (path.endsWith("/speech")) {
      assert.equal(JSON.parse(options.body).stream, true);
      const bytes = new Uint8Array(4800); // 0.1 seconds of 24 kHz PCM, deliberately split on an odd byte.
      return new Response(new ReadableStream({start(c) { c.enqueue(bytes.subarray(0, 1361)); c.enqueue(bytes.subarray(1361)); c.close(); }}));
    }
    if (mode === "fail") { mode = "normal"; return json({message: "Provider test failure"}, 502); }
    const stream = new ReadableStream({start(c) {
      options.signal?.addEventListener("abort", () => { try { c.error(new DOMException("Aborted", "AbortError")); } catch (_) {} }, {once: true});
      if (mode === "manual") { streamControl = c; return; }
      c.enqueue(event("meta", {context_at: new Date().toISOString()}));
      c.enqueue(event("delta", {text: '<img src=x onerror="alert(1)"> Readings are unavailable.'}));
      c.enqueue(event("done", {})); c.close();
    }});
    return new Response(stream, {headers: {"Content-Type": "text/event-stream"}});
  };
  Object.defineProperty(w, "isSecureContext", {value: true, configurable: true});
  Object.defineProperty(w.navigator, "mediaDevices", {value: {getUserMedia: (...args) => getMic(...args)}, configurable: true});
  Object.defineProperty(w.performance, "now", {value: () => fakeTime, configurable: true});
  const node = () => ({connect() {}, disconnect() {}});
  w.AudioContext = class {
    constructor() { this.sampleRate = 48000; this.state = "running"; this.began = Date.now(); this.analyserCount = 0; this.audioWorklet = {addModule: async () => {}}; }
    get currentTime() { return (Date.now() - this.began) / 1000; }
    async resume() {}
    async close() { this.state = "closed"; }
    createMediaStreamSource() { return node(); }
    createGain() { return {...node(), gain: {value: 0}}; }
    createAnalyser() { const output = ++this.analyserCount === 2; return {...node(), getByteFrequencyData(data) { data.fill(100); if (output) outputFrames++; else micFrames++; }}; }
    createBuffer(channels, length, rate) { return {duration: length / rate, getChannelData: () => new Float32Array(length)}; }
    createBufferSource() {
      const context = this;
      return {...node(), start(at) { startedAudio++; this.timer = setTimeout(() => this.onended?.(), Math.max(0, at - context.currentTime + this.buffer.duration) * 1000); }, stop() { clearTimeout(this.timer); }};
    }
  };
  w.AudioWorkletNode = class { constructor() { this.port = {}; worklet = this; } connect() {} disconnect() {} };
  const painter = new Proxy({}, {get: (target, key) => target[key] || (() => {}), set: (target, key, value) => { target[key] = value; return true; }});
  w.HTMLCanvasElement.prototype.getContext = () => painter;
  const fakeStream = () => ({getTracks: () => [{enabled: true, stop() { stopped++; }}]});
  const $ = (s) => d.querySelector(s), click = (s) => $(s).click();
  const submit = (text) => { $("#chat-input").value = text; $("#chat-form").dispatchEvent(new w.Event("submit", {bubbles: true, cancelable: true})); };
  const complete = () => wait(() => !$("#chat-send").disabled, "assistant turn");
  await navigate("chatbot");
  for (const file of ["voice.js", "chat.js"]) w.eval(await readFile(new URL("../agrisentinel/static/js/" + file, import.meta.url), "utf8"));
  await wait(() => $("#chat-language-dialog").open, "first-entry language modal");
  assert.equal($("#chat-input").disabled, true);
  assert.equal(d.querySelectorAll("[data-language]").length, 7);
  assert.equal($(".chat-voice"), null, "Voice aside must be removed");
  click('[data-language="hi"]'); await complete();
  assert.equal($("#chat-language-dialog").open, false);
  assert.equal($("#chat-language-label").textContent, "Hindi");
  submit("Farm status?"); assert.ok($(".chat-thinking"), "Thinking indicator must appear immediately"); await complete();
  assert.equal($(".chat-message-assistant img"), null);
  assert.ok($(".chat-message-assistant").textContent.includes("<img"));
  assert.equal(JSON.parse(chats()[0].options.body).language, "hi");
  assert.equal(JSON.parse(chats()[0].options.body).stream, true);
  mode = "manual"; streamControl = null; submit("Stream a response");
  await wait(() => streamControl, "stream reader");
  streamControl.enqueue(event("meta", {}));
  // Split a UTF-8 Hindi delta across byte boundaries, not just SSE frames.
  const first = event("delta", {text: "???? ?????. "});
  streamControl.enqueue(first.subarray(0, 45)); streamControl.enqueue(first.subarray(45));
  await wait(() => $(".chat-message-assistant:last-child .chat-message-text")?.textContent === "???? ?????. ", "first chunk visible");
  assert.equal($("#chat-send").disabled, true, "First chunk must render before completion");
  assert.equal(d.querySelectorAll(".chat-thinking").length, 0);
  streamControl.enqueue(event("delta", {text: "Second chunk."})); streamControl.enqueue(event("done", {})); streamControl.close(); await complete();
  assert.ok(logText().includes("???? ?????. Second chunk.")); mode = "normal";
  function logText() { return $("#chat-messages").textContent; }
  mode = "fail"; submit("Failed question"); await complete();
  assert.equal($("#chat-input").value, "Failed question"); assert.ok($("#chat-error").textContent.includes("Provider test failure"));
  submit("Retry question"); await complete();
  assert.ok(!JSON.parse(chats().at(-1).options.body).messages.some((m) => m.content === "Failed question"));
  mode = "manual"; streamControl = null; submit("Slow question"); await wait(() => streamControl, "pending turn");
  const signal = chats().at(-1).options.signal; click("#chat-new");
  assert.equal(signal.aborted, true); assert.equal(d.querySelectorAll(".chat-message").length, 0); mode = "normal";
  click("#chat-change-language"); assert.equal($("#chat-language-dialog").open, true); click('[data-language="en"]');
  assert.equal($("#chat-language-label").textContent, "English");
  click("#chat-change-language"); click('[data-language="hi"]');
  getMic = async () => { const err = new Error(); err.name = "NotAllowedError"; throw err; };
  click("#chat-open-voice"); await wait(() => !$("#voice-error").hidden, "microphone denial");
  assert.equal($("#chat-text-view").hidden, true); assert.equal($("#chat-voice-view").hidden, false);
  assert.ok($("#voice-error").textContent.includes("denied")); click("#chat-close-voice");
  let allowMic; getMic = () => new Promise((resolve) => { allowMic = resolve; });
  click("#chat-open-voice"); await wait(() => allowMic, "permission prompt"); click("#chat-close-voice");
  const stoppedBefore = stopped; allowMic(fakeStream()); await new Promise((resolve) => setTimeout(resolve, 30));
  assert.ok(stopped > stoppedBefore, "Late permission must release microphone");
  getMic = async () => fakeStream(); click("#chat-open-voice");
  await wait(() => $("#voice-status").textContent === "Listening", "live microphone");
  assert.ok(d.body.classList.contains("chat-voice-active"));
  await wait(() => micFrames > 0, "microphone drives visualizer");
  mode = "manual"; streamControl = null;
  // Feed real capture callbacks, then pause: VAD must send automatically.
  for (let i = 0; i < 10; i++) { fakeTime += 43; worklet.port.onmessage({data: new Float32Array(2048).fill(.1)}); }
  for (let i = 0; i < 18; i++) { fakeTime += 43; worklet.port.onmessage({data: new Float32Array(2048)}); }
  await wait(() => streamControl, "automatic end-of-turn transcription");
  assert.equal(calls.filter((c) => c.path.endsWith("/transcribe")).length, 1);
  streamControl.enqueue(event("delta", {text: "The rover is offline. "}));
  await wait(() => startedAudio > 0, "audio starts before full text response");
  assert.equal($("#chat-send").disabled, true);
  await wait(() => outputFrames > 0, "assistant audio drives visualizer");
  streamControl.enqueue(event("delta", {text: "Check its connection."})); streamControl.enqueue(event("done", {})); streamControl.close();
  await complete(); await wait(() => $("#voice-status").textContent === "Listening", "hands-free next turn"); mode = "normal";
  click("#voice-mic"); assert.equal($("#voice-mic").getAttribute("aria-pressed"), "false");
  click("#voice-mic"); assert.equal($("#voice-mic").getAttribute("aria-pressed"), "true");
  const beforeLeave = stopped; await navigate("overview");
  assert.ok(stopped > beforeLeave); assert.ok(!d.body.classList.contains("chat-voice-active"));
  w.fetch = originalFetch;
  process.stdout.write("PASS language modal, incremental UTF-8 streaming, thinking state, retries, voice VAD, early PCM playback, live visualizer input/output and microphone cleanup\n");
}
