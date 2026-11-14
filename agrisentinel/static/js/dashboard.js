"use strict";
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [
    ...root.querySelectorAll(selector),
  ];
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (char) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[char],
    );
  const icon = (name) =>
    `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const fmt = (value, digits = 1) =>
    value === null || value === undefined
      ? "—"
      : Number(value).toLocaleString("en", {
          maximumFractionDigits: digits,
          minimumFractionDigits: digits,
        });
  const num = (value) => Number(value || 0).toLocaleString("en");
  const dateTime = (value) =>
    value
      ? new Date(value).toLocaleString("en-GB", {
          timeZone: "UTC",
          day: "2-digit",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        }) + " UTC"
      : "Not reported";
  const timeOnly = (value) =>
    value
      ? new Date(value).toLocaleTimeString("en-GB", {
          timeZone: "UTC",
          hour12: false,
        })
      : "—";
  const shortZone = (id) =>
    id ? id.replace("zone-", "Zone ").replace(/Zone (.)/, (_, letter) => "Zone " + letter.toUpperCase()) : "Unassigned";
  const metrics = {
    temperature_c: { label: "Temperature", unit: "°C", color: "#6b9fff" },
    humidity_pct: { label: "Humidity", unit: "%", color: "#b8b8b8" },
    soil_moisture_pct: { label: "Soil moisture", unit: "%", color: "#f4f4f4" },
    mq2_raw: { label: "MQ2 raw", unit: "ADC", color: "#6b9fff" },
    mq3_raw: { label: "MQ3 raw", unit: "ADC", color: "#b8b8b8" },
    heading_deg: { label: "Compass heading", unit: "°", color: "#6b9fff" },
    tilt_deg: { label: "Tilt", unit: "°", color: "#f4f4f4" },
    sound_level_raw: {
      label: "Sound level raw",
      unit: "ADC",
      color: "#b8b8b8",
    },
    obstacle_distance_cm: {
      label: "Obstacle distance",
      unit: "cm",
      color: "#6b9fff",
    },
  };
  const pageInfo = {
    overview: [
      "Farm overview",
      "Monitor field readings, rover connectivity, and active alerts.",
      "View reports",
    ],
    field: [
      "Field monitoring",
      "Select a partition to view its readings and inspection history.",
      "View analytics",
    ],
    rovers: [
      "Rover fleet",
      "Find a rover, review its status, or send a command.",
      "Register rover",
    ],
    analytics: [
      "Telemetry analytics",
      "Explore measurements over time. Compare conditions across zones.",
      "View reports",
    ],
    alerts: [
      "Alerts & conditions",
      "Review open conditions, acknowledge alerts, and adjust thresholds.",
      "Configure rules",
    ],
    chatbot: [
      "Farm chatbot",
      "Understand field conditions and rover activity. Type a question or use your voice.",
      "New conversation",
    ],
    reports: [
      "Field reports",
      "Generate and download summaries of recorded measurements.",
      "Download CSV",
    ],
  };
  const state = {
    page: "overview",
    data: null,
    zone: "zone-a",
    metric: "temperature_c",
    charts: {},
    analytics: null,
    report: null,
    reportQuery: "period=daily",
    telemetry: [],
    telemetryCursor: null,
    alertStatus: "open",
    alerts: [],
    alertCursor: null,
    lastPageRefresh: 0,
    detailRobot: null,
  };
  let toastTimer,
    refreshTimer,
    liveStream,
    fetching = false,
    queued = false,
    syncTime = 0;
  let cameraSession = null;
  const CAMERA_POLL_INTERVAL_MS = 20;

  function stopCamera() {
    if (!cameraSession) return;
    const view = cameraSession;
    cameraSession = null;
    clearTimeout(view.timer);
    clearTimeout(view.imageTimer);
    view.controller?.abort();
    view.image.onload = view.image.onerror = null;
    view.image.removeAttribute("src");
    for (const url of new Set([view.url, view.pendingUrl])) {
      if (url) URL.revokeObjectURL(url);
    }
  }

  function startCamera(robotId) {
    stopCamera();
    const panel = $("#rover-camera");
    if (!panel) return;
    const view = cameraSession = {
      image: $("#camera-image"), version: null, pending: false, loaded: false,
      frameTimes: [], url: null, pendingUrl: null, meta: null, connectionLost: false,
    };
    const status = $("#camera-status"), caption = $("#camera-caption");
    const placeholder = $("#camera-placeholder"), expand = $("#camera-expand");
    expand.addEventListener("click", () => {
      const expanded = panel.classList.toggle("camera-expanded");
      expand.textContent = expanded ? "Collapse" : "Expand";
      expand.setAttribute("aria-expanded", String(expanded));
      if (expanded) panel.scrollIntoView({ block: "start", behavior: "instant" });
    });
    function setStatus(live, message) {
      status.textContent = message;
      status.classList.toggle("offline", !live);
    }
    function paint() {
      if (!view.loaded || !view.meta) return;
      const now = performance.now();
      view.frameTimes = view.frameTimes.filter((time) => now - time < 2000);
      const times = view.frameTimes;
      const fps = times.length > 1 ? (times.length - 1) * 1000 / Math.max(1, times.at(-1) - times[0]) : 0;
      const age = view.ageAtLoad + (now - view.loadedAt) / 1000;
      const fresh = age < view.meta.offlineAfter && !view.connectionLost;
      setStatus(fresh, view.connectionLost ? "Camera connection lost" : fresh ? "Live" : "Camera offline");
      const ageText = age < 1 ? `${Math.round(age * 1000)}ms` : `${Math.floor(age)}s`;
      caption.textContent = `${fresh ? "Last frame" : "Last image (stale)"}: ${ageText} ago ? ${view.meta.width} ? ${view.meta.height} ? ${fps.toFixed(1)} fps`;
    }
    async function poll() {
      if (cameraSession !== view || !$("#detail-dialog").open) return;
      const started = performance.now();
      let retryDelay = CAMERA_POLL_INTERVAL_MS;
      try {
        if (document.hidden) return;
        paint();
        if (view.pending) return; // Decode one frame at a time; never queue old images.
        view.controller = new AbortController();
        const timeout = setTimeout(() => view.controller.abort(), 1500);
        let response, blob;
        try {
          const after = view.loaded ? view.version : "";
          response = await fetch(`/api/v1/robots/${encodeURIComponent(robotId)}/camera/latest.jpg?after=${encodeURIComponent(after || "")}`, {
            cache: "no-store", signal: view.controller.signal,
          });
          if (response.status === 200) blob = await response.blob();
        } finally {
          clearTimeout(timeout);
        }
        if (cameraSession !== view) return;
        if (response.status === 401) {
          stopCamera();
          liveStream?.close();
          window.location.assign("/login");
          return;
        }
        if (response.status === 404) {
          retryDelay = 200;
          setStatus(false, "Waiting for camera");
          caption.textContent = view.loaded ? "Last image (stale). Waiting for camera?" : "No camera images received yet.";
          return;
        }
        if (!response.ok) throw new Error("Camera request failed");
        view.connectionLost = false;
        const meta = {
          version: response.headers.get("X-Camera-Version"),
          width: Number(response.headers.get("X-Camera-Width")),
          height: Number(response.headers.get("X-Camera-Height")),
          age: Number(response.headers.get("X-Camera-Age")) + (performance.now() - started) / 1000,
          offlineAfter: Number(response.headers.get("X-Camera-Offline-After")) || 8,
          at: performance.now(),
        };
        if (response.status === 204) {
          if (view.loaded) {
            view.ageAtLoad = meta.age;
            view.loadedAt = meta.at;
            paint();
          }
          return;
        }
        if (!blob?.size || !meta.version || !meta.width || !meta.height) throw new Error("Empty camera image");
        const url = URL.createObjectURL(blob);
        view.pending = true;
        view.pendingUrl = url;
        if (!view.loaded) setStatus(false, "Loading image");
        view.image.onload = () => {
          if (cameraSession !== view || view.pendingUrl !== url) return;
          clearTimeout(view.imageTimer);
          if (view.url) URL.revokeObjectURL(view.url);
          view.url = url;
          view.pendingUrl = null;
          view.pending = false;
          view.loaded = true;
          view.version = meta.version;
          view.meta = meta;
          view.loadedAt = performance.now();
          view.ageAtLoad = meta.age + (view.loadedAt - meta.at) / 1000;
          view.frameTimes.push(view.loadedAt);
          view.image.hidden = false;
          placeholder.hidden = true;
          paint();
        };
        view.image.onerror = () => {
          if (cameraSession !== view || view.pendingUrl !== url) return;
          clearTimeout(view.imageTimer);
          URL.revokeObjectURL(url);
          view.pendingUrl = null;
          view.pending = false;
          view.loaded = false;
          view.image.onload = view.image.onerror = null;
          view.image.removeAttribute("src");
          view.image.hidden = true;
          placeholder.hidden = false;
          placeholder.textContent = "Image unavailable. Reconnecting?";
          setStatus(false, "Camera unavailable");
        };
        view.imageTimer = setTimeout(() => view.image.onerror?.(), 1500);
        view.image.src = url;
      } catch (error) {
        if (cameraSession !== view) return;
        retryDelay = 200;
        view.connectionLost = true;
        setStatus(false, "Camera connection lost");
        caption.textContent = view.loaded ? "Last image (stale). Reconnecting?" : "Reconnecting to the server?";
      } finally {
        if (cameraSession === view) view.timer = setTimeout(poll,
          document.hidden ? 250 : Math.max(0, retryDelay - (performance.now() - started)));
      }
    }
    poll();
  }

  function relative(value) {
    if (!value) return "Awaiting contact";
    const seconds = Math.max(
      0,
      Math.floor((Date.now() - new Date(value)) / 1000),
    );
    return seconds < 5
      ? "Just now"
      : seconds < 60
        ? `${seconds}s ago`
        : seconds < 3600
          ? `${Math.floor(seconds / 60)}m ago`
          : seconds < 86400
            ? `${Math.floor(seconds / 3600)}h ago`
            : `${Math.floor(seconds / 86400)}d ago`;
  }
  function toast(message, error = false) {
    clearTimeout(toastTimer);
    $("#toast").textContent = message;
    $("#toast").classList.toggle("error", error);
    $("#toast").hidden = false;
    toastTimer = setTimeout(() => {
      $("#toast").hidden = true;
    }, 5000);
  }
  async function api(path, options = {}) {
    const response = await fetch("/api/v1" + path, {
      cache: "no-store",
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": $('meta[name="csrf-token"]').content,
        ...options.headers,
      },
    });
    if (response.status === 401) {
      liveStream?.close();
      window.location.assign("/login");
      throw new Error("Your session has expired.");
    }
    const data = await response.json();
    if (!response.ok)
      throw new Error(data.message || `Request failed (${response.status})`);
    return data;
  }
  function sourceBadge(source) {
    return `<span class="source-tag ${source === "simulator" ? "simulator" : ""}">${source === "simulator" ? "Simulated" : "Hardware"}</span>`;
  }
  function statusBadge(robot) {
    return `<span class="status-badge ${robot.online ? "" : "offline"}">${robot.online ? "Online" : robot.last_seen ? "Offline" : "Not connected"}</span>`;
  }
  function stateLabel(robot) {
    return `<span class="state-label ${esc(robot.state)}">${esc(robot.state)}</span>`;
  }
  function empty(message, detail = "", symbol = "info") {
    return `<div class="empty-state">${icon(symbol)}${esc(message)}${detail ? `<small>${esc(detail)}</small>` : ""}</div>`;
  }
  function showError(error) {
    $("#error-banner").hidden = false;
    $("#error-message").textContent = error.message || String(error);
  }
  function roverName(robot) {
    return `<button class="table-link rover-name" data-rover="${esc(robot.id)}"><span class="rover-glyph">${icon("rover")}</span><span><strong>${esc(robot.name)}</strong><small>${esc(robot.id)}</small></span></button>`;
  }

  function renderMetrics(data) {
    const c = data.counts;
    const items = [
      ["Registered rovers", c.total, "devices enrolled", ""],
      ["Online now", c.online, `${c.offline} offline`, ""],
      ["Idle rovers", c.idle, "online & standing by", ""],
      ["Field partitions", c.zones, "configured zones", ""],
      [
        "Active alerts",
        c.active_alerts,
        "unresolved conditions",
        "alert-number",
      ],
    ];
    $("#overview-metrics").innerHTML = items
      .map(
        ([label, value, sub, cls]) =>
          `<div class="metric"><span>${label}</span><div class="metric-line"><strong class="${cls}">${num(value)}</strong></div><small>${sub}</small></div>`,
      )
      .join("");
    $("#nav-alert-count").textContent = c.active_alerts;
    $("#workspace-name").textContent =
      data.fields[0]?.name || "Field workspace";
    const fieldHeading = $("#page-overview .field-panel h2");
    if (fieldHeading)
      fieldHeading.textContent = data.fields[0]?.name || "Field overview";
    const partitions = $("#page-field .map-footer .mono");
    if (partitions) partitions.textContent = c.zones + " partitions";
    $("#field-area").textContent = data.fields[0]?.area_ha
      ? `${fmt(data.fields[0].area_ha)} ha`
      : "Area not configured";
  }

  function fieldMap(containerId) {
    const focusedZone = $("#" + containerId).contains(document.activeElement)
      ? document.activeElement.dataset.zone
      : null;
    const zones = state.data?.zones || [];
    if (!zones.length) {
      $("#" + containerId).innerHTML = empty("No field partitions configured.");
      return;
    }
    const plots = [...zones]
      .sort((a, b) => a.map_row - b.map_row || a.map_col - b.map_col)
      .map((zone) => {
        const values = zone.latest?.telemetry;
        const classes = ["partition", zone.id === state.zone ? "selected" : "", zone.active_alerts ? "has-alert" : "", zone.stale ? "unmeasured" : ""].join(" ");
        const condition = zone.active_alerts ? `${zone.active_alerts} active alert${zone.active_alerts === 1 ? "" : "s"}` : zone.stale ? "No fresh data" : "Fresh telemetry";
        return `<button type="button" class="${classes}" aria-pressed="${zone.id === state.zone}" data-zone="${esc(zone.id)}"><span class="partition-top"><span>${esc(shortZone(zone.id))}</span><span class="partition-condition">${esc(condition)}</span></span><strong>${esc(zone.name)}</strong><span class="partition-crop">${esc(zone.crop || "Crop unspecified")} &middot; ${fmt(zone.area_ha)} ha</span><span class="partition-reading">${fmt(values?.soil_moisture_pct)}<span>% soil</span></span><span class="partition-bottom"><span>${zone.stale ? (zone.latest ? "Stale reading" : "No readings yet") : `${fmt(values?.temperature_c)}&deg;C &middot; ${fmt(values?.humidity_pct)}% RH`}</span><span>${esc(zone.robot_ids[0] || "Unassigned")}</span></span></button>`;
      }).join("");
    $("#" + containerId).classList.remove("loading-map");
    $("#" + containerId).innerHTML = `<div class="partition-grid">${plots}</div>`;
    if (focusedZone)
      $(`[data-zone="${focusedZone}"]`, $("#" + containerId))?.focus({
        preventScroll: true,
      });
  }

  function inspector(containerId, expanded = false) {
    const z = state.data?.zones.find((z) => z.id === state.zone);
    if (!z) {
      $("#" + containerId).innerHTML = empty("Select a partition.");
      return;
    }
    const latest = z.latest,
      values = latest?.telemetry;
    const reading = (key) =>
      `<div class="sensor-row ${z.stale ? "stale" : ""}"><div><span>${metrics[key].label}</span>${!latest || z.stale ? `<small>${!latest ? "No measurement" : "Stale reading"}</small>` : ""}</div><div class="sensor-reading">${fmt(values?.[key], key.endsWith("_raw") ? 0 : 1)}<small>${metrics[key].unit}</small></div></div>`;
    $("#" + containerId).innerHTML =
      `<p class="eyebrow">Selected partition / ${esc(shortZone(z.id))}</p><div class="inspector-title"><h2>${esc(z.name)}</h2><span class="status-badge ${z.active_alerts ? "warning" : z.stale ? "offline" : ""}">${z.active_alerts ? `${z.active_alerts} alert${z.active_alerts === 1 ? "" : "s"}` : z.stale ? "No fresh data" : "Reporting"}</span></div><p class="inspector-meta">${esc(z.crop || "Crop unspecified")} · ${fmt(z.area_ha)} ha ${latest ? " / " + sourceBadge(latest.source) : ""}</p>${["temperature_c", "humidity_pct", "soil_moisture_pct"].map(reading).join("")}<div class="inspector-footer"><span>${latest ? relative(latest.recorded_at) : "Start a rover to receive data"}</span>${!expanded ? '<a href="#field" class="text-button">Zone details ↗</a>' : `<span>${esc(z.robot_ids.join(", ") || "Unassigned")}</span>`}</div>${expanded ? `<div class="inspection-progress"><b>${z.inspected_checkpoints_today}</b> / ${z.checkpoint_count} checkpoints reported today<div class="inspection-bar"><span></span></div><small>Last inspection: ${dateTime(z.last_inspection)}</small></div>` : ""}`;
    // Set style properties through DOM to retain a strict script/style CSP.
    const bar = $(".inspection-bar>span", $("#" + containerId));
    if (bar) {
      bar.removeAttribute("style");
      bar.style.width = `${Math.min(100, (z.inspected_checkpoints_today / z.checkpoint_count) * 100)}%`;
    }
  }

  function fleetTable(containerId, robots, full = false) {
    if (!robots.length) {
      $("#" + containerId).innerHTML = empty(
        "No rovers match this view.",
        "Register a device or clear your filters.",
        "rover",
      );
      return;
    }
    $("#" + containerId).innerHTML =
      `<table><thead><tr><th>Rover / device ID</th><th>Connectivity</th><th>Assigned zone</th><th>Operational state</th>${full ? "<th>Latest reading</th>" : ""}<th>Last contact</th><th>Source</th><th></th></tr></thead><tbody>${robots.map((r) => `<tr><td>${roverName(r)}</td><td>${statusBadge(r)}</td><td>${esc(shortZone(r.zone_id))}</td><td>${stateLabel(r)}</td>${full ? `<td class="mono">${fmt(r.latest?.telemetry.temperature_c)}°C ${r.stale ? '<span class="stale-indicator">Stale</span>' : ""}</td>` : ""}<td class="muted" title="${dateTime(r.last_seen)}">${relative(r.last_seen)}</td><td>${sourceBadge(r.source)}</td><td><button class="row-action" data-rover="${esc(r.id)}" aria-label="View ${esc(r.name)}">↗</button></td></tr>`).join("")}</tbody></table>`;
  }
  function renderFleet() {
    if (!state.data) return;
    const search = $("#rover-search").value.toLowerCase(),
      source = $("#rover-source").value;
    const robots = state.data.robots.filter(
      (r) =>
        (r.name + " " + r.id).toLowerCase().includes(search) &&
        (source === "all" || r.source === source),
    );
    fleetTable("rover-table", robots, true);
    $("#rover-summary").innerHTML =
      `<span><b>${state.data.counts.total}</b>registered</span><span><b>${state.data.counts.online}</b>online</span><span><b>${state.data.counts.offline}</b>offline</span><span><b>${state.data.counts.idle}</b>idle</span>`;
  }
  function renderZoneTable() {
    $("#zone-table").innerHTML =
      `<table><thead><tr><th>Partition</th><th>Crop / use</th><th>Assigned rovers</th><th>Soil moisture</th><th>Last inspection</th><th>Checkpoints today</th><th></th></tr></thead><tbody>${state.data.zones.map((z) => `<tr><td><button class="table-link" data-zone="${esc(z.id)}"><strong>${esc(z.name)}</strong><small class="muted mono"> / ${esc(shortZone(z.id))}</small></button></td><td class="muted">${esc(z.crop || "Unspecified")}</td><td class="mono">${esc(z.robot_ids.join(", ") || "Unassigned")}</td><td class="mono">${fmt(z.latest?.telemetry.soil_moisture_pct)}% ${z.stale ? '<span class="stale-indicator">Stale</span>' : ""}</td><td class="muted">${relative(z.last_inspection)}</td><td class="mono">${z.inspected_checkpoints_today} / ${z.checkpoint_count}</td><td><button class="row-action" data-zone="${esc(z.id)}" aria-label="Select ${esc(z.name)}">↗</button></td></tr>`).join("")}</tbody></table>`;
  }
  function renderOverviewAlerts() {
    const alerts = state.data.alerts.slice(0, 3);
    $("#overview-alerts").innerHTML = alerts.length
      ? alerts
          .map(
            (a) =>
              `<div class="alert-feed-item"><h3>${esc(a.title)}</h3><span class="alert-severity">${esc(a.severity === "critical" ? "Critical" : "Warning")}</span><p>${esc(a.detail)}</p><footer><span>${esc(a.robot_id)} / ${esc(a.zone_id || "FLEET")}</span><span>${relative(a.opened_at)}</span></footer></div>`,
          )
          .join("")
      : empty(
          "No open conditions.",
          "Rules are evaluated when fresh telemetry arrives.",
          "alert",
        );
  }
  function renderActivity() {
    $("#recent-activity").innerHTML = state.data.activity.length
      ? state.data.activity
          .map(
            (t) =>
              `<div class="activity-log-row"><span class="mono">${timeOnly(t.received_at)}</span><span><strong>${esc(t.robot_id)}</strong> reported to ${esc(shortZone(t.zone_id))}</span><span class="activity-measurement mono">${fmt(t.telemetry.temperature_c)}°C / ${fmt(t.telemetry.humidity_pct)}% RH / ${fmt(t.telemetry.soil_moisture_pct)}% soil</span>${sourceBadge(t.source)}</div>`,
          )
          .join("")
      : empty(
          "No telemetry received yet.",
          "Run python -m simulator in a separate terminal to begin.",
          "chart",
        );
  }
  function updateConfigurationOptions() {
    const zones = state.data.zones;
    if (!$("#analytics-zones").children.length) {
      $("#analytics-zones").innerHTML = zones
        .map(
          (z, i) =>
            `<label><input type="checkbox" value="${esc(z.id)}" ${i < 4 ? "checked" : ""}>${esc(shortZone(z.id))}</label>`,
        )
        .join("");
      const options = zones
        .map(
          (z) =>
            `<option value="${esc(z.id)}">${esc(shortZone(z.id))} · ${esc(z.name)}</option>`,
        )
        .join("");
      $("#report-zone").insertAdjacentHTML("beforeend", options);
      $("#reg-zone").innerHTML =
        '<option value="">Assign later</option>' + options;
    }
  }

  async function refreshOverview(initial = false) {
    if (fetching) {
      queued = true;
      return;
    }
    fetching = true;
    try {
      const data = await api("/overview");
      state.data = data;
      syncTime = Date.now();
      $("#error-banner").hidden = true;
      if (!data.zones.some((z) => z.id === state.zone))
        state.zone = data.zones[0]?.id;
      renderMetrics(data);
      updateConfigurationOptions();
      if (state.page === "overview") {
        fieldMap("overview-map");
        inspector("overview-inspector");
        fleetTable("overview-fleet", data.robots);
        renderOverviewAlerts();
        renderActivity();
      }
      if (state.page === "field") {
        fieldMap("detail-map");
        inspector("detail-inspector", true);
        renderZoneTable();
      }
      if (state.page === "rovers") renderFleet();
      $("#last-sync").textContent = "Synced " + timeOnly(data.server_time);
      if (initial || Date.now() - state.lastPageRefresh > 14000) {
        state.lastPageRefresh = Date.now();
        await refreshPageData(!initial);
      }
      if (state.detailRobot && $("#detail-dialog").open)
        await updateRoverDetails();
    } catch (error) {
      showError(error);
    } finally {
      fetching = false;
      if (queued) {
        queued = false;
        scheduleRefresh();
      }
    }
  }
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => refreshOverview(), 350);
  }

  function setupCharts() {
    if (!window.Chart) return;
    Chart.defaults.font.family = "Plex, sans-serif";
    Chart.defaults.font.size = 12;
    Chart.defaults.color = "#b8b8b8";
    Chart.defaults.borderColor = "#383838";
    Chart.defaults.plugins.legend.display = false;
  }
  function drawChart(id, datasets, unit = "", options = {}) {
    const canvas = $("#" + id);
    if (!canvas) return;
    const hasData = datasets.some((d) => d.data.some((p) => p.y !== null));
    const overlay = $("#" + id + "-empty");
    if (overlay) overlay.hidden = hasData;
    if (!window.Chart) {
      if (overlay) {
        overlay.hidden = false;
        overlay.textContent =
          "Chart assets unavailable. Measurement tables remain accessible.";
      }
      return;
    }
    const config = {
      type: "line",
      data: {
        datasets: datasets.map((d, index) => ({
          borderDash: [[], [6, 4], [2, 3], [8, 3, 2, 3], [12, 4], [4, 3]][index % 6],
          pointRadius: d.data.filter((p) => p.y !== null).length <= 2 ? 3 : 0,
          pointHoverRadius: 3,
          borderWidth: 1.6,
          tension: 0.25,
          spanGaps: false,
          ...d,
        })),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: window.matchMedia("(prefers-reduced-motion: reduce)").matches
          ? false
          : { duration: 250 },
        normalized: true,
        interaction: { mode: "nearest", intersect: false },
        parsing: false,
        plugins: {
          legend: {
            display: !!options.legend,
            position: "bottom",
            labels: {
              boxWidth: 12,
              boxHeight: 2,
              color: "#b8b8b8",
              padding: 20,
              font: { size: 12 },
            },
          },
          tooltip: {
            backgroundColor: "#121212",
            titleColor: "#f4f4f4",
            bodyColor: "#b8b8b8",
            borderColor: "#383838",
            borderWidth: 1,
            padding: 11,
            displayColors: true,
            callbacks: {
              title: (items) =>
                items.length ? dateTime(items[0].parsed.x) : "",
              label: (ctx) =>
                `${ctx.dataset.label}: ${fmt(ctx.parsed.y)} ${ctx.dataset.unit || unit}`,
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            grid: { display: false },
            border: { display: false },
            ticks: {
              maxTicksLimit: 6,
              maxRotation: 0,
              padding: 8,
              color: "#a3a3a3",
              callback: (value) =>
                new Date(value).toLocaleTimeString("en-GB", {
                  timeZone: "UTC",
                  hour: "2-digit",
                  minute: "2-digit",
                  hour12: false,
                }),
            },
          },
          y: {
            border: { display: false },
            grid: { color: "#2b2b2b", drawTicks: false },
            ticks: { maxTicksLimit: 5, padding: 9, color: "#a3a3a3" },
            ...options.y,
          },
        },
      },
    };
    if (state.charts[id]) {
      state.charts[id].data = config.data;
      state.charts[id].options = config.options;
      state.charts[id].update();
    } else state.charts[id] = new Chart(canvas, config);
  }
  function zoneDatasets(data, key) {
    const colors = [
      "#6b9fff",
      "#b8b8b8",
      "#f4f4f4",
      "#6b9fff",
      "#b8b8b8",
      "#f4f4f4",
    ];
    return data.zones.map((z, index) => ({
      label: shortZone(z.id),
      borderColor: colors[index % colors.length],
      data: data.points
        .filter((p) => p.zone_id === z.id)
        .map((p) => ({ x: new Date(p.at).getTime(), y: p[key] })),
    }));
  }
  async function loadOverviewChart() {
    const period = $("#overview-period").value;
    const data = await api("/analytics?period=" + period);
    if (period !== $("#overview-period").value) return;
    const byTime = new Map();
    for (const point of data.points) {
      if (!byTime.has(point.at)) byTime.set(point.at, []);
      byTime.get(point.at).push(point);
    }
    const datasets = ["temperature_c", "humidity_pct", "soil_moisture_pct"].map(
      (key) => ({
        label: metrics[key].label,
        unit: metrics[key].unit,
        borderColor: metrics[key].color,
        data: [...byTime]
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([at, points]) => {
            const valid = points.filter((p) => p[key] !== null);
            return {
              x: new Date(at).getTime(),
              y: valid.length
                ? valid.reduce((s, p) => s + p[key], 0) / valid.length
                : null,
            };
          }),
      }),
    );
    drawChart("overview-chart", datasets, "");
  }
  async function loadZoneChart() {
    const zone = state.zone,
      key = $("#zone-metric").value,
      period = $("#zone-period").value;
    if (!zone) return;
    const data = await api(
      `/analytics?period=${period}&zones=${encodeURIComponent(zone)}`,
    );
    if (state.zone !== zone || $("#zone-metric").value !== key) return;
    $("#zone-chart-title").textContent =
      `${shortZone(zone)} / ${metrics[key].label}`;
    drawChart("zone-chart", zoneDatasets(data, key), metrics[key].unit, {
      y: key === "heading_deg" ? { min: 0, max: 360 } : {},
    });
  }
  function analyticsQuery() {
    return new URLSearchParams({
      period: $("#analytics-period").value,
      zones: $$("#analytics-zones input:checked")
        .map((i) => i.value)
        .join(","),
    });
  }
  async function loadAnalytics(resetLog = true) {
    const query = analyticsQuery(),
      selected = $$("#analytics-zones input:checked");
    if (!selected.length) {
      state.analytics = null;
      renderAnalytics();
      state.telemetry = [];
      state.telemetryCursor = null;
      renderTelemetryLog();
      return;
    }
    const data = await api("/analytics?" + query);
    if (query.toString() !== analyticsQuery().toString()) return;
    state.analytics = data;
    renderAnalytics();
    if (resetLog) await loadTelemetry(true);
  }
  function renderAnalytics() {
    const key = state.metric,
      meta = metrics[key],
      data = state.analytics,
      stats = data?.statistics[key] || {
        count: 0,
        min: null,
        max: null,
        avg: null,
      };
    $("#analytics-stats").innerHTML = [
      ["Minimum", stats.min, meta.unit],
      ["Maximum", stats.max, meta.unit],
      [
        key === "heading_deg" ? "Circular average" : "Average",
        stats.avg,
        meta.unit,
      ],
      ["Valid samples", stats.count, "readings"],
    ]
      .map(
        ([label, value, unit]) =>
          `<div class="analytics-stat"><span>${label}</span><strong>${label === "Valid samples" ? num(value) : fmt(value, key.endsWith("_raw") ? 0 : 1)}</strong><small>${unit}</small></div>`,
      )
      .join("");
    $("#analytics-chart-title").textContent = meta.label;
    $("#analytics-unit").textContent = meta.unit;
    drawChart(
      "analytics-chart",
      data ? zoneDatasets(data, key) : [],
      meta.unit,
      { legend: true, y: key === "heading_deg" ? { min: 0, max: 360 } : {} },
    );
    $("#analytics-footnote").textContent = key.startsWith("mq")
      ? "Raw ADC readings only. No calibrated gas concentration or disease inference. Missing values remain gaps."
      : key === "heading_deg"
        ? "Compass averages use circular statistics. An undefined direction is shown as a gap."
        : "Averages per time bucket. Missing measurements remain gaps; activity is never inferred between readings.";
    $("#analytics-comparison").innerHTML = data?.zones.length
      ? `<table><thead><tr><th>ZONE</th><th>Valid readings</th><th>MINIMUM (${meta.unit})</th><th>MAXIMUM (${meta.unit})</th><th>${key === "heading_deg" ? "CIRCULAR AVG" : "AVERAGE"} (${meta.unit})</th></tr></thead><tbody>${data.zones.map((z) => `<tr><td>${esc(shortZone(z.id))} <span class="muted">/ ${esc(z.name)}</span></td><td class="mono">${num(z.statistics[key].count)}</td><td class="mono">${fmt(z.statistics[key].min)}</td><td class="mono">${fmt(z.statistics[key].max)}</td><td class="mono">${fmt(z.statistics[key].avg)}</td></tr>`).join("")}</tbody></table>`
      : empty(
          "No comparable measurements.",
          "Select one or more zones with received telemetry.",
        );
    $$("#analytics-metrics button").forEach((button) => {
      button.classList.toggle("active", button.dataset.metric === key);
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.metric === key),
      );
    });
  }
  async function loadTelemetry(reset = false) {
    if (reset) state.telemetry = [];
    const zones = $$("#analytics-zones input:checked").map((i) => i.value);
    if (!zones.length) {
      state.telemetryCursor = null;
      renderTelemetryLog();
      return;
    }
    const params = new URLSearchParams({
      period: $("#analytics-period").value,
      limit: "50",
    });
    params.set("zones", zones.join(","));
    if (!reset && state.telemetryCursor)
      params.set("before_id", state.telemetryCursor);
    const data = await api("/telemetry?" + params);
    state.telemetry.push(
      ...data.items.filter((row) => zones.includes(row.zone_id)),
    );
    state.telemetryCursor = data.next_cursor;
    renderTelemetryLog();
  }
  function renderTelemetryLog() {
    const key = state.metric,
      meta = metrics[key];
    $("#telemetry-log").innerHTML = state.telemetry.length
      ? `<table><thead><tr><th>Recorded utc</th><th>Rover</th><th>ZONE</th><th>${esc(meta.label.toUpperCase())} (${meta.unit})</th><th>State</th><th>Source</th><th>Event</th></tr></thead><tbody>${state.telemetry.map((t) => `<tr><td class="mono">${dateTime(t.recorded_at)}</td><td class="mono">${esc(t.robot_id)}</td><td>${esc(shortZone(t.zone_id))}</td><td class="mono">${fmt(t.telemetry[key], key.endsWith("_raw") ? 0 : 1)}</td><td class="muted">${esc(t.state)}</td><td>${sourceBadge(t.source)}</td><td class="mono muted" title="${esc(t.event_id)}">…${esc(t.event_id.slice(-8))}</td></tr>`).join("")}</tbody></table>`
      : empty("No records in this selection.");
    $("#load-telemetry").hidden = !state.telemetryCursor;
  }

  async function loadAlerts(reset = true) {
    const status = state.alertStatus,
      params = new URLSearchParams({ status, limit: "30" });
    if (!reset && state.alertCursor) params.set("before_id", state.alertCursor);
    const data = await api("/alerts?" + params);
    if (status !== state.alertStatus) return;
    state.alerts = reset ? data.items : [...state.alerts, ...data.items];
    state.alertCursor = data.next_cursor;
    renderAlerts();
  }
  function renderAlerts() {
    const openDetails = new Set(
      $$("#alerts-list details[open]").map((d) => d.dataset.alert),
    );
    $("#alerts-list").innerHTML = state.alerts.length
      ? state.alerts
          .map(
            (a) =>
              `<article class="alert-row"><div class="alert-row-head"><span class="alert-symbol ${a.severity}">${icon("alert")}</span><div class="alert-main"><h3>${esc(a.title)}</h3><p>${esc(a.detail)}</p><div class="alert-row-meta"><span>${esc(a.robot_id)}</span><span>${esc(a.zone_id || "FLEET CONNECTIVITY")}</span><time>${dateTime(a.opened_at)}</time><span>${esc(a.severity.toUpperCase())}</span></div></div><div class="alert-actions"><span class="alert-status ${a.status}">${a.status}</span>${a.status === "active" ? `<button class="button" data-ack="${a.id}">Acknowledge</button>` : ""}</div></div><details class="alert-history" data-alert="${a.id}" ${openDetails.has(String(a.id)) ? "open" : ""}><summary>Condition history / ${a.history.length} events</summary>${a.history.map((h) => `<div class="history-entry"><time>${dateTime(h.at)}</time><span><strong>${esc(h.action)}</strong> · ${esc(h.actor)}<br>${esc(h.note)}</span></div>`).join("")}</details></article>`,
          )
          .join("")
      : empty(
          state.alertStatus === "open"
            ? "No open conditions."
            : "No alerts in this view.",
          "Alerts are created by configured rules and resolved by measured recovery.",
          "alert",
        );
    $("#load-alerts").hidden = !state.alertCursor;
  }

  async function loadReport() {
    const params = new URLSearchParams({ period: $("#report-period").value });
    if ($("#report-zone").value) params.set("zones", $("#report-zone").value);
    if ($("#report-end").value)
      params.set("to", new Date($("#report-end").value + "Z").toISOString());
    const button = $("#report-form button");
    button.disabled = true;
    try {
      const data = await api("/reports?" + params);
      state.report = data;
      state.reportQuery = params.toString();
      renderReport();
      addReportTrend();
    } finally {
      button.disabled = false;
    }
  }
  function renderReport() {
    const r = state.report;
    if (!r) return;
    if (state.charts["report-chart"]) {
      state.charts["report-chart"].destroy();
      delete state.charts["report-chart"];
    }
    $("#report-range").textContent = `${dateTime(r.from)} → ${dateTime(r.to)}`;
    $("#csv-export").href = "/api/v1/reports/export.csv?" + state.reportQuery;
    const statsRows = Object.entries(metrics)
      .map(([key, meta]) => {
        const s = r.statistics[key];
        return `<tr><td>${meta.label}</td><td class="muted">${meta.unit}</td><td class="mono">${num(s.count)}</td><td class="mono">${fmt(s.min)}</td><td class="mono">${fmt(s.max)}</td><td class="mono">${fmt(s.avg)}</td></tr>`;
      })
      .join("");
    $("#report-content").innerHTML =
      `<div class="report-banner"><div><p class="eyebrow">${r.period.toUpperCase()} / MEASUREMENT SUMMARY</p><h2>Field observation report</h2><p class="muted">${r.zones.length} zones with data · ${r.robots.length} reporting rovers · ${num(r.alert_count)} overlapping alerts</p></div><div class="report-count">${num(r.record_count)}<small>telemetry records</small></div></div>${!r.record_count ? empty("No measurements in this period.", "The report remains empty until telemetry is received.") : ""}<section class="report-block"><h3>Environmental & device measurements</h3><div class="table-wrap"><table><thead><tr><th>Measurement</th><th>UNIT</th><th>Valid samples</th><th>Minimum</th><th>Maximum</th><th>Average</th></tr></thead><tbody>${statsRows}</tbody></table></div><p class="report-sources">SOURCES / ${
        Object.entries(r.sources)
          .map(
            ([source, count]) =>
              `${source.toUpperCase()}: ${num(count)} samples`,
          )
          .join(" · ") || "NO DATA"
      }</p></section><section class="report-block"><h3>Zone comparison</h3><div class="table-wrap">${r.zones.length ? `<table><thead><tr><th>Partition</th><th>Records</th><th>AVG TEMP °C</th><th>AVG HUMIDITY %</th><th>AVG SOIL %</th></tr></thead><tbody>${r.zones.map((z) => `<tr><td>${esc(shortZone(z.id))} / ${esc(z.name)}</td><td class="mono">${num(z.count)}</td><td class="mono">${fmt(z.statistics.temperature_c.avg)}</td><td class="mono">${fmt(z.statistics.humidity_pct.avg)}</td><td class="mono">${fmt(z.statistics.soil_moisture_pct.avg)}</td></tr>`).join("")}</tbody></table>` : empty("No zone measurements.")}</div></section><section class="report-block"><h3>Rover activity / reported state samples</h3><div class="table-wrap">${r.robots.length ? `<table><thead><tr><th>Rover</th><th>Records</th><th>Patrolling</th><th>Inspecting</th><th>IDLE</th><th>Stopped</th><th>Error</th></tr></thead><tbody>${r.robots.map((robot) => `<tr><td>${esc(robot.name)} <span class="mono muted">/ ${esc(robot.id)}</span></td><td class="mono">${num(robot.records)}</td>${["patrolling", "inspecting", "idle", "stopped", "error"].map((s) => `<td class="mono">${num(robot.state_samples[s])}</td>`).join("")}</tr>`).join("")}</tbody></table>` : empty("No rover activity.")}</div></section><section class="report-block"><h3>Conditions during period / ${num(r.alert_count)}</h3><div class="table-wrap">${r.alerts.length ? `<table><thead><tr><th>Condition</th><th>Rover / zone</th><th>Severity</th><th>Opened utc</th><th>Current status</th></tr></thead><tbody>${r.alerts.map((a) => `<tr><td>${esc(a.title)}</td><td>${esc(a.robot_id)} / ${esc(a.zone_id || "FLEET")}</td><td>${esc(a.severity)}</td><td class="mono">${dateTime(a.opened_at)}</td><td>${esc(a.status)}</td></tr>`).join("")}</tbody></table>` : empty("No alerts overlapped this period.")}</div></section><section class="report-block"><h3>Checkpoint inspections / ${num(r.inspection_count)}</h3><div class="table-wrap">${r.inspections.length ? `<table><thead><tr><th>Rover</th><th>ZONE</th><th>Checkpoint</th><th>Reported utc</th></tr></thead><tbody>${r.inspections.map((i) => `<tr><td class="mono">${esc(i.robot_id)}</td><td>${esc(shortZone(i.zone_id))}</td><td class="mono">${esc(i.checkpoint_id)}</td><td class="mono">${dateTime(i.at)}</td></tr>`).join("")}</tbody></table>` : empty("No checkpoint inspections reported.")}</div></section><p class="report-note">Measurements with missing values are excluded from statistics. Compass heading uses a circular mean; an undefined mean is shown as “—”. Rover activity counts represent telemetry samples, not operating duration. Alerts include conditions overlapping the period; detail lists are capped at 200 alerts and 100 inspections. No crop health, diagnosis, or yield is inferred.</p>`;
  }

  function addReportTrend() {
    const block = $("#report-content .report-block");
    if (!block || !state.report) return;
    block.insertAdjacentHTML(
      "afterend",
      '<section class="report-block panel chart-panel"><div class="panel-header"><h3>Temperature trend / zone comparison</h3><span class="subtle-tag">°C / UTC</span></div><div class="chart-wrap"><canvas id="report-chart" role="img" aria-label="Reported temperature trends by zone"></canvas><div id="report-chart-empty" class="chart-empty" hidden>No trend measurements in this period.</div></div><p class="panel-footnote">Time-bucket averages from stored samples. Gaps indicate unavailable measurements.</p></section>',
    );
    drawChart(
      "report-chart",
      zoneDatasets(state.report, "temperature_c"),
      "°C",
      { legend: true },
    );
  }

  async function showRover(robotId) {
    stopCamera();
    state.detailRobot = robotId;
    $("#detail-content").innerHTML = empty("Loading device details…");
    $("#detail-dialog").showModal();
    const data = await api("/robots/" + encodeURIComponent(robotId));
    if (state.detailRobot !== robotId) return;
    renderRoverDetail(data);
  }
  function renderRoverDetail(data) {
    const r = data.robot;
    const sensorHtml = Object.entries(metrics)
      .map(
        ([key, meta]) =>
          `<div class="sensor-row"><div><span>${meta.label}</span></div><div class="sensor-reading" data-detail-sensor="${key}">${fmt(r.latest?.telemetry[key], key.endsWith("_raw") ? 0 : 1)}<small>${meta.unit}</small></div></div>`,
      )
      .join("");
    $("#detail-content").innerHTML =
      `<div class="dialog-heading"><span class="rover-glyph">${icon("rover")}</span><div><h2>${esc(r.name)}</h2><p>${esc(r.id)} / ${esc(r.model)}</p></div></div><div id="detail-live-status">${statusBadge(r)} &nbsp; ${sourceBadge(r.source)} &nbsp; ${stateLabel(r)} ${r.stale ? ' &nbsp; <span class="stale-indicator">Stale telemetry</span>' : ""}</div><dl class="detail-meta"><div><dt>Last contact</dt><dd id="detail-contact">${relative(r.last_seen)}</dd></div><div><dt>Last measurement</dt><dd id="detail-recorded">${relative(r.latest_recorded_at)}</dd></div><div><dt>Battery</dt><dd>${r.battery_pct === null ? "Not reported" : fmt(r.battery_pct) + "%"}</dd></div><div><dt>Firmware</dt><dd>${esc(r.firmware)}</dd></div><div><dt>Device uptime</dt><dd id="detail-uptime">${r.uptime_ms === null ? "Not reported" : fmt(r.uptime_ms / 60000) + " min"}</dd></div><div><dt>Registered</dt><dd>${dateTime(r.registered_at)}</dd></div></dl><div class="detail-sensors">${sensorHtml}</div><section class="detail-section"><h3>Zone assignment</h3><form class="assignment-form" id="assignment-form"><select name="zone_id" aria-label="Assign rover to zone"><option value="">Unassigned</option>${state.data.zones.map((z) => `<option value="${esc(z.id)}" ${r.zone_id === z.id ? "selected" : ""}>${esc(shortZone(z.id))} / ${esc(z.name)}</option>`).join("")}</select><button class="button">Update</button></form><p class="detail-caption">The simulator reads assignment changes on its next polling cycle.</p></section><section class="detail-section"><h3>Device commands</h3><div class="command-controls">${["patrol", "inspect", "idle", "stop"].map((action) => `<button class="button" data-command="${action}">${action === "stop" ? "■ Stop" : action.charAt(0).toUpperCase() + action.slice(1)}</button>`).join("")}</div><p class="detail-caption">Queued for device acknowledgement. Commands expire after five minutes.</p><div class="detail-history" id="command-history">${commandsHtml(data.commands)}</div></section><section class="detail-section"><h3>Temperature history / past 24 hours</h3><div class="chart-wrap"><canvas id="rover-chart" aria-label="Rover temperature history" role="img"></canvas><div id="rover-chart-empty" class="chart-empty" hidden>No measurements in this period.</div></div></section><section class="detail-section"><h3>Recent checkpoint inspections</h3><div class="detail-history">${
        data.inspections.length
          ? data.inspections
              .slice(0, 10)
              .map(
                (i) =>
                  `<div><span>${esc(i.zone_id)} / ${esc(i.checkpoint_id)}</span><span class="mono">${dateTime(i.at)}</span></div>`,
              )
              .join("")
          : empty("No inspections reported yet.")
      }</div></section>`;
    if (r.source === "hardware") {
      $("#detail-live-status").insertAdjacentHTML("afterend", `
        <section class="rover-camera" id="rover-camera" aria-label="Rover camera">
          <div class="camera-toolbar"><h3>Live camera</h3><button type="button" class="text-button" id="camera-expand" aria-expanded="false">Expand</button></div>
          <div class="camera-viewport"><img id="camera-image" alt="Camera view from ${esc(r.name)}" hidden><p id="camera-placeholder">Waiting for the first camera image…</p></div>
          <div class="camera-footer"><span id="camera-status" class="status-badge offline" role="status">Waiting for camera</span><span id="camera-caption">No camera images received yet.</span></div>
        </section>`);
      startCamera(r.id);
    }
    const historySection = $("#rover-chart").closest("section");
    $("h3", historySection).textContent = "Sensor history / past 24 hours";
    $("h3", historySection).insertAdjacentHTML(
      "afterend",
      `<select id="rover-history-metric" aria-label="Rover history measurement">${Object.entries(
        metrics,
      )
        .map(([key, meta]) => `<option value="${key}">${meta.label}</option>`)
        .join("")}</select>`,
    );
    $("#rover-history-metric").addEventListener("change", () =>
      loadRoverChart(r.id).catch(showError),
    );
    loadRoverChart(r.id).catch(showError);
  }
  function commandsHtml(commands) {
    return commands.length
      ? commands
          .slice(0, 8)
          .map(
            (c) =>
              `<div><span>${esc(c.action)} <span class="mono muted">#${c.id}</span></span><span>${esc(c.status)} / ${relative(c.issued_at)}</span></div>`,
          )
          .join("")
      : '<p class="detail-caption">No commands issued.</p>';
  }
  async function updateRoverDetails() {
    const id = state.detailRobot,
      data = await api("/robots/" + encodeURIComponent(id));
    if (state.detailRobot !== id || !$("#detail-dialog").open) return;
    const r = data.robot;
    $("#detail-live-status").innerHTML =
      `${statusBadge(r)} &nbsp; ${sourceBadge(r.source)} &nbsp; ${stateLabel(r)} ${r.stale ? ' &nbsp; <span class="stale-indicator">Stale telemetry</span>' : ""}`;
    $("#detail-contact").textContent = relative(r.last_seen);
    $("#detail-recorded").textContent = relative(r.latest_recorded_at);
    $("#detail-uptime").textContent =
      r.uptime_ms === null ? "Not reported" : fmt(r.uptime_ms / 60000) + " min";
    $$("[data-detail-sensor]").forEach((el) => {
      const k = el.dataset.detailSensor;
      el.innerHTML = `${fmt(r.latest?.telemetry[k], k.endsWith("_raw") ? 0 : 1)}<small>${metrics[k].unit}</small>`;
    });
    $("#command-history").innerHTML = commandsHtml(data.commands);
  }
  async function loadRoverChart(id) {
    const key = $("#rover-history-metric")?.value || "temperature_c";
    const data = await api(
      "/analytics?period=24h&robot_id=" + encodeURIComponent(id),
    );
    if (
      state.detailRobot === id &&
      $("#detail-dialog").open &&
      key === ($("#rover-history-metric")?.value || "temperature_c")
    ) {
      drawChart("rover-chart", zoneDatasets(data, key), metrics[key].unit, {
        y: key === "heading_deg" ? { min: 0, max: 360 } : {},
      });
      $("#rover-chart").setAttribute("aria-label", metrics[key].label + " history for " + id);
    }
  }

  async function showRules() {
    $("#rules-dialog").showModal();
    $("#rules-content").innerHTML = empty("Loading configured rules…");
    const data = await api("/rules");
    $("#rules-content").innerHTML = data.items
      .map(
        (r) =>
          `<form class="rule-form" data-rule="${esc(r.id)}"><div class="rule-name">${esc(r.name)}<small>${r.direction.toUpperCase()} / ${esc(metrics[r.metric].unit)} · ${esc(r.severity.toUpperCase())}</small><div class="rule-enabled"><input id="enabled-${r.id}" type="checkbox" name="enabled" ${r.enabled ? "checked" : ""}><label for="enabled-${r.id}">Enabled</label></div></div><div><label for="trigger-${r.id}">Trigger ${r.direction === "below" ? "&lt;" : "&gt;"}</label><input id="trigger-${r.id}" name="trigger" type="number" step="any" value="${r.trigger}" required></div><div><label for="recovery-${r.id}">Recovery ${r.direction === "below" ? "≥" : "≤"}</label><input id="recovery-${r.id}" name="recovery" type="number" step="any" value="${r.recovery}" required></div><button class="button" type="submit">Save</button></form>`,
      )
      .join("");
  }

  async function refreshPageData(preserveHistory = false) {
    try {
      if (state.page === "overview") await loadOverviewChart();
      else if (state.page === "field") await loadZoneChart();
      else if (state.page === "analytics")
        await loadAnalytics(!preserveHistory || !state.telemetry.length);
      else if (state.page === "alerts" && (!preserveHistory || state.alerts.length <= 30))
        await loadAlerts();
    } catch (error) {
      showError(error);
    }
  }
  async function route() {
    const page = window.location.hash.slice(1) || (window.location.pathname === "/chatbot" ? "chatbot" : "overview");
    state.page = pageInfo[page] ? page : "overview";
    document.body.classList.toggle("chat-page", state.page === "chatbot");
    const [title, description, action] = pageInfo[state.page];
    $$(".page-section").forEach((el) => {
      el.hidden = el.id !== "page-" + state.page;
    });
    $$(".nav-link[data-page]").forEach((el) => {
      el.classList.toggle("active", el.dataset.page === state.page);
      if (el.dataset.page === state.page)
        el.setAttribute("aria-current", "page");
      else el.removeAttribute("aria-current");
    });
    $("#page-title").textContent = title;
    $("#page-description").textContent = description;
    $("#heading-action").textContent = action;
    $("#heading-action").classList.toggle("primary", state.page === "rovers");
    $("#breadcrumb-page").textContent =
      state.page === "rovers" ? "Rover fleet" : title;
    document.title = title + " · AgriSentinel";
    setNavigation(false);
    if (state.data) {
      if (state.page === "overview") {
        fieldMap("overview-map");
        inspector("overview-inspector");
        fleetTable("overview-fleet", state.data.robots);
        renderOverviewAlerts();
        renderActivity();
      } else if (state.page === "field") {
        fieldMap("detail-map");
        inspector("detail-inspector", true);
        renderZoneTable();
      } else if (state.page === "rovers") renderFleet();
      await refreshPageData();
      if (state.page === "reports" && !state.report)
        await loadReport().catch(showError);
    }
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  document.addEventListener("click", async (event) => {
    const target = event.target.closest("button,a,[data-zone]");
    if (!target) return;
    try {
      if (target.hasAttribute("data-page")) {
        setNavigation(false);
      }
      if (target.hasAttribute("data-zone")) {
        state.zone = target.dataset.zone;
        if (state.page === "field") {
          fieldMap("detail-map");
          inspector("detail-inspector", true);
          await loadZoneChart();
        } else {
          fieldMap("overview-map");
          inspector("overview-inspector");
        }
      }
      if (target.dataset.rover) await showRover(target.dataset.rover);
      if (target.hasAttribute("data-close-dialog"))
        target.closest("dialog").close();
      if (target.dataset.metric) {
        state.metric = target.dataset.metric;
        renderAnalytics();
        renderTelemetryLog();
      }
      if (target.dataset.status) {
        state.alertStatus = target.dataset.status;
        $$("#alert-filters button").forEach((b) =>
          { b.classList.toggle("active", b === target); b.setAttribute("aria-pressed", String(b === target)); },
        );
        await loadAlerts();
      }
      if (target.dataset.ack) {
        target.disabled = true;
        await api(`/alerts/${target.dataset.ack}/acknowledge`, {
          method: "POST",
          body: "{}",
        });
        toast("Condition acknowledged. Recovery monitoring continues.");
        await loadAlerts();
        scheduleRefresh();
      }
      if (target.dataset.command) {
        target.disabled = true;
        await api(`/robots/${encodeURIComponent(state.detailRobot)}/commands`, {
          method: "POST",
          body: JSON.stringify({ action: target.dataset.command }),
        });
        toast("Command queued. Waiting for the device.");
        await updateRoverDetails();
        target.disabled = false;
      }
    } catch (error) {
      toast(error.message, true);
      target.disabled = false;
    }
  });
  document.addEventListener("submit", async (event) => {
    if (event.target.id === "assignment-form") {
      event.preventDefault();
      const button = $("button", event.target);
      button.disabled = true;
      try {
        await api(`/robots/${encodeURIComponent(state.detailRobot)}`, {
          method: "PATCH",
          body: JSON.stringify({
            zone_id: event.target.elements.zone_id.value || null,
          }),
        });
        toast("Assignment updated.");
        scheduleRefresh();
      } catch (error) {
        toast(error.message, true);
      } finally {
        button.disabled = false;
      }
    }
    if (event.target.matches(".rule-form")) {
      event.preventDefault();
      const form = event.target,
        button = $("button", form);
      button.disabled = true;
      try {
        await api("/rules/" + encodeURIComponent(form.dataset.rule), {
          method: "PATCH",
          body: JSON.stringify({
            trigger: Number(form.elements.trigger.value),
            recovery: Number(form.elements.recovery.value),
            enabled: form.elements.enabled.checked,
          }),
        });
        toast("Rule saved. Fresh telemetry will evaluate the new threshold.");
        scheduleRefresh();
      } catch (error) {
        toast(error.message, true);
      } finally {
        button.disabled = false;
      }
    }
  });
  $("#register-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target,
      button = $("button", form);
    button.disabled = true;
    $("#register-error").textContent = "";
    const data = Object.fromEntries(new FormData(form));
    if (!data.zone_id) data.zone_id = null;
    if (!data.model) delete data.model;
    try {
      const result = await api("/robots", {
        method: "POST",
        body: JSON.stringify(data),
      });
      form.hidden = true;
      $("#registered-key").hidden = false;
      $("#registered-key").innerHTML =
        `<p class="muted">${esc(result.robot.name)} is registered. Save this device key securely; it is shown once.</p><code class="key-display">${esc(result.api_key)}</code><p class="detail-caption">${result.robot.source === "simulator" ? "Add robot_id, zone_id and this api_key to your local simulator config." : "Configure the ESP32 Authorization header as Bearer followed by this key."}</p><button class="button load-more" data-close-dialog>Done</button>`;
      scheduleRefresh();
    } catch (error) {
      $("#register-error").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
  $("#heading-action").addEventListener("click", async () => {
    try {
      if (state.page === "rovers") {
        $("#register-form").reset();
        $("#register-form").hidden = false;
        $("#registered-key").hidden = true;
        $("#register-error").textContent = "";
        $("#register-dialog").showModal();
      } else if (state.page === "chatbot") window.dispatchEvent(new Event("chat:reset"));
      else if (state.page === "field") window.location.hash = "analytics";
      else if (state.page === "alerts") await showRules();
      else if (state.page === "reports")
        window.location.assign($("#csv-export").href);
      else window.location.hash = "reports";
    } catch (error) {
      toast(error.message, true);
    }
  });
  $("#settings-button").addEventListener("click", () =>
    showRules().catch((error) => toast(error.message, true)),
  );
  function setNavigation(open, restoreFocus = false) {
    const mobile = window.matchMedia("(max-width: 760px)").matches;
    $("#sidebar").classList.toggle("open", open);
    $("#sidebar").inert = mobile && !open;
    $("#menu-button").setAttribute("aria-expanded", String(open));
    $("#navigation-backdrop").hidden = !open || !mobile;
    if (open && mobile) $("#close-navigation").focus();
    else if (restoreFocus && mobile) $("#menu-button").focus();
  }
  $("#menu-button").addEventListener("click", () =>
    setNavigation(!$("#sidebar").classList.contains("open")),
  );
  $("#close-navigation").addEventListener("click", () => setNavigation(false, true));
  $("#navigation-backdrop").addEventListener("click", () => setNavigation(false, true));
  window.addEventListener("resize", () => setNavigation(false));
  document.addEventListener("keydown", (event) => {
    if (!$("#sidebar").classList.contains("open") || !window.matchMedia("(max-width: 760px)").matches) return;
    if (event.key === "Escape") { event.preventDefault(); setNavigation(false, true); }
    if (event.key === "Tab") {
      const controls = $$("a[href],button,input:not([type=hidden])", $("#sidebar"));
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  $("#rover-search").addEventListener("input", renderFleet);
  $("#rover-source").addEventListener("change", renderFleet);
  $("#overview-period").addEventListener("change", () =>
    loadOverviewChart().catch(showError),
  );
  ["zone-metric", "zone-period"].forEach((id) =>
    $("#" + id).addEventListener("change", () =>
      loadZoneChart().catch(showError),
    ),
  );
  $("#analytics-period").addEventListener("change", () =>
    loadAnalytics().catch(showError),
  );
  $("#analytics-zones").addEventListener("change", () =>
    loadAnalytics().catch(showError),
  );
  $("#load-telemetry").addEventListener("click", () =>
    loadTelemetry().catch(showError),
  );
  $("#load-alerts").addEventListener("click", () =>
    loadAlerts(false).catch(showError),
  );
  $("#report-form").addEventListener("submit", (event) => {
    event.preventDefault();
    loadReport().catch(showError);
  });
  $("#print-report").addEventListener("click", () => window.print());
  $("#retry-button").addEventListener("click", () => refreshOverview(true));
  window.addEventListener("hashchange", () => route().then(() => $("#page-title").focus({preventScroll: true})).catch(showError));
  $("#detail-dialog").addEventListener("close", () => {
    stopCamera();
    state.detailRobot = null;
    if (state.charts["rover-chart"]) {
      state.charts["rover-chart"].destroy();
      delete state.charts["rover-chart"];
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) liveStream?.close();
    else {
      connectStream();
      refreshOverview(true);
    }
  });
  function connectStream() {
    liveStream?.close();
    liveStream = new EventSource("/api/v1/events");
    liveStream.addEventListener("ready", () => {
      $("#stream-state").innerHTML = "<i></i>Live stream";
      $("#stream-state").classList.remove("offline");
      scheduleRefresh();
    });
    liveStream.addEventListener("update", scheduleRefresh);
    liveStream.addEventListener("tick", () => {
      if (Date.now() - syncTime > 4500) scheduleRefresh();
    });
    liveStream.onerror = () => {
      $("#stream-state").innerHTML = "<i></i>Reconnecting";
      $("#stream-state").classList.add("offline");
    };
  }
  setupCharts();
  setInterval(() => {
    if (!document.hidden && Date.now() - syncTime > 12000) refreshOverview();
  }, 5000);
  route()
    .then(() => refreshOverview(true))
    .then(() => {
      if (state.page === "reports") return loadReport();
    })
    .catch(showError);
  connectStream();
})();
