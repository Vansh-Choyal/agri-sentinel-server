"use strict";
(() => {
  const panel = document.querySelector("#alert-notification");
  if (!panel) return;
  let snapshot = null, refreshedAt = 0, timer, hideTimer, selected = null;
  const shown = new Map();
  const cooldown = 120000;
  function hide() {
    clearTimeout(hideTimer);
    panel.hidden = true;
    selected = null;
  }
  function schedule(first = false) {
    clearTimeout(timer);
    if (document.hidden) return;
    const delay = first ? 8000 + Math.random() * 7000 : 35000 + Math.random() * 35000;
    timer = setTimeout(notify, delay);
  }
  function notify() {
    if (document.hidden) return;
    const now = Date.now();
    for (const [id, at] of shown) if (now - at >= cooldown) shown.delete(id);
    const page = location.hash.slice(1) || (location.pathname === "/chatbot" ? "chatbot" : "overview");
    const canShow = panel.hidden && now - refreshedAt < 20000 && page !== "chatbot" && !document.querySelector("dialog[open]");
    const candidates = canShow ? (snapshot?.alerts || []).filter((alert) => alert.status === "active" && !shown.has(alert.id)) : [];
    if (candidates.length) {
      const alert = candidates[Math.floor(Math.random() * candidates.length)];
      const robot = snapshot.robots.find((item) => item.id === alert.robot_id);
      selected = {...alert, source: robot?.source || "unknown"};
      shown.set(alert.id, now);
      document.querySelector("#alert-notification-label").textContent =
        `${robot?.source === "demo" ? "Demo alert" : robot?.source === "simulator" ? "Simulated alert" : "Field alert"} · ${robot?.name || alert.robot_id}`;
      document.querySelector("#alert-notification-title").textContent = alert.title;
      document.querySelector("#alert-notification-detail").textContent = alert.detail;
      panel.hidden = false;
      hideTimer = setTimeout(() => {
        // Keep the action available while someone is navigating it by keyboard.
        if (!panel.contains(document.activeElement)) hide();
      }, 20000);
    }
    schedule();
  }
  window.addEventListener("farm:updated", (event) => {
    snapshot = event.detail;
    refreshedAt = Date.now();
    if (selected && !snapshot.alerts.some((alert) => alert.id === selected.id && alert.status === "active")) hide();
    if (!timer) schedule(true);
  });
  document.querySelector("#alert-notification-open").addEventListener("click", () => {
    if (!selected) return;
    const alert = selected;
    hide();
    location.hash = "chatbot";
    window.dispatchEvent(new CustomEvent("chat:alert", {detail: alert}));
  });
  document.querySelector("#alert-notification-dismiss").addEventListener("click", hide);
  panel.addEventListener("focusout", (event) => { if (!panel.contains(event.relatedTarget)) hide(); });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer); timer = null;
    hide();
    if (!document.hidden) schedule(true);
  });
  window.addEventListener("pagehide", () => { clearTimeout(timer); hide(); });
})();
