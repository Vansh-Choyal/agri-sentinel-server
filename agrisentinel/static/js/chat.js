"use strict";
(() => {
  const $ = (selector) => document.querySelector(selector);
  if (!$("#page-chatbot")) return;
  const input = $("#chat-input"), log = $("#chat-messages"), dialog = $("#chat-language-dialog");
  let history = [], language = "", configured = false, loading = false, loaded = false;
  let busy = false, voiceMode = false, controller, generation = 0, pending;
  const onPage = () => (location.hash.slice(1) || (location.pathname === "/chatbot" ? "chatbot" : "overview")) === "chatbot";
  const status = (text) => { $("#chat-status").textContent = text; };
  function error(text = "") {
    $("#chat-error").textContent = text; $("#chat-error").hidden = !text;
    $("#voice-error").textContent = text; $("#voice-error").hidden = !text;
  }
  function controls() {
    input.disabled = busy || !configured || !language;
    $("#chat-send").disabled = input.disabled;
    $("#chat-send").hidden = busy; $("#chat-stop").hidden = !busy;
    $("#chat-open-voice").disabled = !configured;
    $("#voice-interrupt").hidden = !busy;
    $("#voice-send").hidden = busy || !voice.started;
    $("#voice-mic").disabled = voice.active && !voice.stream;
    document.querySelectorAll("[data-chat-prompt]").forEach((button) => { button.disabled = input.disabled; });
  }
  async function request(path, options = {}) {
    const response = await fetch("/api/v1/chat" + path, {
      ...options, cache: "no-store",
      headers: {"X-CSRF-Token": $('meta[name="csrf-token"]').content,
        ...(options.body instanceof FormData ? {} : {"Content-Type": "application/json"}), ...options.headers},
    });
    if (!response.ok) {
      let detail;
      try { detail = (await response.json()).message; } catch (_) {}
      throw new Error(detail || (response.status === 401 ? "Your session expired. Sign in again." : "The request failed. Please retry."));
    }
    return response;
  }
  const voice = new window.FarmVoice({
    ask: (audio) => transcribe(audio),
    state: (text) => { $("#voice-status").textContent = text; controls(); },
    error: (text) => { $("#voice-status").textContent = "Voice unavailable"; error(text); controls(); },
    requestSpeech: (text, signal) => request("/speech", {method: "POST", signal, body: JSON.stringify({text, language, stream: true})}),
  });
  async function load() {
    if (!onPage()) return;
    if (!language && !dialog.open) openLanguage();
    if (loaded || loading) return;
    loading = true;
    try {
      const data = await (await request("/config")).json();
      configured = data.configured; loaded = true;
      if (!configured) error("Set DEEPINFRA_API_KEY in .env and restart the server to use the assistant.");
      status(language ? "Ready" : "Choose a language to begin.");
    } catch (err) { error(err.message); status("Could not connect. Reopen Chatbot to retry."); }
    finally { loading = false; controls(); }
  }
  function openLanguage() {
    cancelTurn();
    if (voiceMode) voice.end();
    $("#chat-language-cancel").textContent = language ? "Cancel" : "Back to overview";
    document.querySelectorAll("[data-language]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.language === language)));
    if (!dialog.open) dialog.showModal();
  }
  function selectLanguage(code) {
    const choice = document.querySelector(`[data-language="${code}"]`);
    if (!choice) return;
    language = code;
    $("#chat-language-label").textContent = choice.querySelector("small").textContent;
    $("#voice-language").textContent = choice.querySelector("span").textContent;
    $("#voice-caption").lang = code;
    dialog.close(); error(); controls(); status("Ready");
    if (voiceMode) voice.begin(); else input.focus();
  }
  function scrollLog() {
    // Continue following the answer only while the reader is near the bottom.
    if (log.scrollHeight - log.scrollTop - log.clientHeight < 180) log.scrollTop = log.scrollHeight;
  }
  function message(role, text, thinking = false) {
    $("#chat-welcome").hidden = true;
    const article = document.createElement("article"); article.className = "chat-message chat-message-" + role;
    const label = document.createElement("p"); label.className = "chat-message-label";
    label.textContent = role === "user" ? "You" : "AgriSentinel";
    const content = document.createElement("div"); content.className = "chat-message-text";
    content.lang = language; content.dir = "auto"; content.textContent = text;
    article.append(label, content);
    if (thinking) {
      article.classList.add("is-streaming");
      const indicator = document.createElement("div"); indicator.className = "chat-thinking"; indicator.setAttribute("role", "status"); indicator.setAttribute("aria-label", "Thinking");
      indicator.append(document.createElement("i"), document.createElement("i"), document.createElement("i")); article.append(indicator);
    }
    log.append(article); log.scrollTop = log.scrollHeight;
    return {article, content, label};
  }
  function trimHistory() {
    while (history.length > 16 || history.reduce((sum, item) => sum + item.content.length, 0) > 18000) history.splice(0, 2);
  }
  async function consume(response, signal, onEvent) {
    if (!response.body || !response.headers.get("Content-Type")?.includes("text/event-stream")) throw new Error("Streaming is unavailable. Restart the backend and reload this page.");
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = "", finished = false;
    try {
      while (!signal.aborted) {
        const {value, done} = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
        let separator;
        while ((separator = /\r?\n\r?\n/.exec(buffer))) {
          const frame = buffer.slice(0, separator.index); buffer = buffer.slice(separator.index + separator[0].length);
          let kind = "message"; const payload = [];
          for (const line of frame.split(/\r?\n/)) {
            if (line.startsWith("event:")) kind = line.slice(6).trim();
            if (line.startsWith("data:")) payload.push(line.slice(5).trimStart());
          }
          if (!payload.length) continue;
          const data = JSON.parse(payload.join("\n"));
          if (kind === "error") throw new Error(data.message || "The response was interrupted.");
          if (kind === "done") finished = true;
          onEvent(kind, data);
        }
        if (done || finished) break;
      }
      if (!finished && !signal.aborted) throw new Error("The response was interrupted. Please retry.");
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  }
  function speechSentences(buffer, signal, flush = false) {
    while (buffer.length) {
      const boundary = /[.!??](?:\s|$)/u.exec(buffer);
      let end = boundary && boundary.index >= 18 ? boundary.index + 1 : 0;
      if (!end && buffer.length > 180) end = Math.max(90, buffer.lastIndexOf(" ", 180));
      if (!end && flush) end = buffer.length;
      if (!end) break;
      voice.enqueue(buffer.slice(0, end).trim(), signal); buffer = buffer.slice(end).trimStart();
    }
    return buffer;
  }
  async function runQuestion(text, spoken, turn, signal) {
    trimHistory();
    const user = message("user", text), assistant = message("assistant", "", true);
    pending = {user, assistant, text};
    input.value = ""; input.rows = 1; status("Thinking?");
    if (spoken) { voice.startReply(); $("#voice-caption").textContent = text; }
    let answer = "", speechBuffer = "", completed = false;
    try {
      const response = await request("", {method: "POST", signal, body: JSON.stringify({messages: [...history, {role: "user", content: text}], language, voice: spoken, stream: true})});
      await consume(response, signal, (kind, data) => {
        if (turn !== generation || signal.aborted) return;
        if (kind === "delta" && typeof data.text === "string") {
          answer += data.text; assistant.content.textContent = answer;
          if (answer.trim()) { assistant.article.querySelector(".chat-thinking")?.remove(); status("Responding?"); }
          if (spoken) { $("#voice-caption").textContent = answer; speechBuffer = speechSentences(speechBuffer + data.text, signal); }
          scrollLog();
        }
        if (kind === "done") completed = true;
      });
      if (turn !== generation || signal.aborted) return;
      if (!completed || !answer.trim()) throw new Error("The model returned no answer. Please retry.");
      history.push({role: "user", content: text}, {role: "assistant", content: answer.slice(0, 8000)});
      pending = null;
      assistant.article.classList.remove("is-streaming"); assistant.article.querySelector(".chat-thinking")?.remove();
      if (spoken && voiceMode) {
        speechSentences(speechBuffer, signal, true);
        try { await voice.finishReply(signal); }
        catch (err) { if (!signal.aborted) { error("Audio could not finish. " + err.message); voice.listen(); } }
      }
      status("Ready");
    } catch (err) {
      if (turn !== generation || signal.aborted) return;
      voice.cancelSpeech(); voice.listen();
      assistant.article.querySelector(".chat-thinking")?.remove(); assistant.article.classList.remove("is-streaming");
      if (answer.trim()) assistant.label.textContent = "AgriSentinel ? interrupted"; else assistant.article.remove();
      user.label.textContent = "You ? not completed";
      input.value = text; error(err.message); status("Your question is ready to retry.");
    } finally { if (turn === generation) pending = null; }
  }
  async function send(text) {
    text = text.trim();
    if (!text || busy || !configured || !onPage()) return;
    if (!language) { openLanguage(); return; }
    if (text.length > 4000) { error("Keep your question under 4000 characters."); return; }
    error(); busy = true; controller = new AbortController();
    const turn = generation, signal = controller.signal; controls();
    try { await runQuestion(text, false, turn, signal); }
    finally { if (turn === generation) { busy = false; controller = null; controls(); if (onPage() && !voiceMode) input.focus(); } }
  }
  async function transcribe(audio) {
    if (busy || !voiceMode || !language) return;
    error(); busy = true; controller = new AbortController();
    const turn = generation, signal = controller.signal;
    controls();
    try {
      const form = new FormData(); form.append("audio", audio, "question.wav"); form.append("language", language);
      const data = await (await request("/transcribe", {method: "POST", signal, body: form})).json();
      if (turn === generation && voiceMode) await runQuestion(data.text, true, turn, signal);
    } catch (err) { if (turn === generation && !signal.aborted) { error(err.message); voice.listen(); } }
    finally { if (turn === generation) { busy = false; controller = null; controls(); } }
  }
  function cancelTurn() {
    generation++; controller?.abort(); controller = null; voice.cancelSpeech();
    if (pending) {
      const {assistant, user, text} = pending;
      assistant.article.querySelector(".chat-thinking")?.remove(); assistant.article.classList.remove("is-streaming");
      if (assistant.content.textContent.trim()) assistant.label.textContent = "AgriSentinel ? stopped"; else assistant.article.remove();
      user.label.textContent = "You ? stopped"; input.value = text; pending = null;
    }
    busy = false; voice.listen(); controls(); status(language ? "Ready" : "Choose a language to begin.");
  }
  function enterVoice() {
    cancelTurn(); error(); voiceMode = true;
    $("#chat-text-view").hidden = true; $("#chat-voice-view").hidden = false;
    document.body.classList.add("chat-voice-active");
    $("#voice-caption").textContent = "Speak naturally. Your assistant will reply when you pause.";
    if (!language) openLanguage(); else voice.begin();
  }
  function leaveVoice() {
    cancelTurn(); voice.end(); voiceMode = false;
    document.body.classList.remove("chat-voice-active");
    $("#chat-voice-view").hidden = true; $("#chat-text-view").hidden = false;
    controls(); if (onPage()) input.focus();
  }
  $("#chat-form").addEventListener("submit", (event) => { event.preventDefault(); send(input.value); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#chat-form").requestSubmit(); }
  });
  input.addEventListener("input", () => { input.rows = Math.min(5, Math.max(1, input.value.split("\n").length)); });
  document.querySelectorAll("[data-chat-prompt]").forEach((el) => el.addEventListener("click", () => send(el.dataset.chatPrompt)));
  $("#chat-language-options").addEventListener("click", (event) => { const choice = event.target.closest("[data-language]"); if (choice) selectLanguage(choice.dataset.language); });
  $("#chat-language-cancel").addEventListener("click", () => { dialog.close(); if (!language) location.hash = "overview"; else if (voiceMode) voice.begin(); });
  dialog.addEventListener("cancel", (event) => { if (!language) event.preventDefault(); else if (voiceMode) voice.begin(); });
  $("#chat-change-language").addEventListener("click", openLanguage);
  $("#voice-change-language").addEventListener("click", openLanguage);
  $("#chat-open-voice").addEventListener("click", enterVoice);
  $("#chat-close-voice").addEventListener("click", leaveVoice);
  $("#voice-mic").addEventListener("click", () => { if (!voice.active) { error(); voice.begin(); } else voice.toggleMic(); });
  $("#voice-send").addEventListener("click", () => voice.finishInput());
  $("#chat-stop").addEventListener("click", cancelTurn);
  $("#voice-interrupt").addEventListener("click", cancelTurn);
  function reset() {
    cancelTurn(); history = []; error(); input.value = "";
    log.querySelectorAll(".chat-message").forEach((el) => el.remove()); $("#chat-welcome").hidden = false;
    if (!configured) { loaded = false; load(); } else if (!voiceMode) input.focus();
  }
  $("#chat-new").addEventListener("click", reset); window.addEventListener("chat:reset", reset);
  window.addEventListener("hashchange", () => {
    if (onPage()) load(); else { if (dialog.open) dialog.close(); leaveVoice(); }
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden && voiceMode) leaveVoice(); });
  window.addEventListener("pagehide", () => { cancelTurn(); voice.end(); });
  controls(); load();
})();
