"use strict";

(() => {
  const API = "/ai-api";
  const escapeText = value => String(value ?? "");
  let pending = null;
  let refreshTimer = null;

  function makeUi() {
    const panel = document.createElement("section");
    panel.className = "panel ai-event-card";
    panel.id = "aiEventsPanel";
    panel.innerHTML = [
      '<div class="ai-event-header">',
      '<h2>AI 偵測事件</h2>',
      '<div><span id="aiEventStatus" class="ai-event-status">讀取中</span> <button id="refreshAiEvents">重新整理</button></div>',
      '</div>',
      '<div id="aiEventList" class="ai-event-list"><div class="ai-event-empty">正在載入事件……</div></div>',
      '<p class="hint">服務只分析本機 uStreamer 影像；事件和快照保存在伺服器，並不會改變既有錄影。</p>'
    ].join("");
    const playback = document.getElementById("playbackPanel");
    (playback?.parentNode || document.querySelector("main") || document.body).insertBefore(panel, playback || null);
    document.getElementById("refreshAiEvents").addEventListener("click", refresh);
  }

  function setStatus(text, type = "") {
    const element = document.getElementById("aiEventStatus");
    if (!element) return;
    element.textContent = text;
    element.className = "ai-event-status " + type;
  }

  function timeText(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? escapeText(value) : date.toLocaleString();
  }

  async function refresh() {
    const list = document.getElementById("aiEventList");
    if (!list || pending) return;
    pending = new AbortController();
    const signal = pending.signal;
    const timeout = setTimeout(() => pending?.abort(), 20000);
    try {
      const [statusResponse, eventsResponse] = await Promise.all([
        fetch(API + "/status", {cache: "no-store", signal}),
        fetch(API + "/events?limit=30", {cache: "no-store", signal})
      ]);
      const status = await statusResponse.json();
      const body = await eventsResponse.json();
      if (!statusResponse.ok) throw new Error(status.error || "AI 服務無法回應");
      if (!eventsResponse.ok) throw new Error(body.error || "事件清單無法回應");
      const frameAge = Date.now() - Date.parse(status.last_frame_at);
      const health = typeof status.healthy === "boolean" ? status.healthy
        : status.running && status.stream_connected && frameAge < 30000;
      setStatus(health ? "分析服務正常" : (status.last_error || "等待影像來源"), health ? "ready" : "error");
      const events = Array.isArray(body.events) ? body.events : [];
      const previousIds = list.dataset.eventIds;
      const nextIds = JSON.stringify(events.map(event => event.id));
      if (previousIds === nextIds) return;
      list.dataset.eventIds = nextIds;
      list.replaceChildren();
      if (!events.length) {
        const empty = document.createElement("div");
        empty.className = "ai-event-empty";
        empty.textContent = "目前尚無偵測事件。";
        list.append(empty);
        return;
      }
      for (const event of events) {
        const card = document.createElement("article");
        card.className = "ai-event";

        const source = event.source || event.metadata?.source || "server";
        const isBrowser = source === "browser" || event.event_type === "browser_motion";

        if (typeof event.snapshot_url === "string" && event.snapshot_url.length > 0) {
          const image = document.createElement("img");
          image.loading = "lazy";
          image.alt = "事件快照";
          image.src = API + event.snapshot_url;
          card.append(image);
        } else {
          const placeholder = document.createElement("div");
          placeholder.className = "ai-event-no-snapshot";
          placeholder.textContent = isBrowser
            ? "瀏覽器移動偵測 · 無快照"
            : "此事件沒有快照";
          card.append(placeholder);
        }

        const body = document.createElement("div");
        body.className = "ai-event-body";

        const label = document.createElement("div");
        label.className = "ai-event-label";
        if (isBrowser) {
          label.textContent = "瀏覽器偵測到移動";
        } else if (event.person_detected || event.event_type === "person") {
          label.textContent = "伺服器偵測到人員";
        } else {
          label.textContent = "伺服器偵測到移動";
        }

        const meta = document.createElement("div");
        meta.className = "ai-event-meta";
        meta.textContent = timeText(event.created_at);
        body.append(label, meta);

        if (isBrowser && event.metadata) {
          const details = document.createElement("div");
          details.className = "ai-event-meta";

          const ratio = Number(event.metadata.changed_ratio);
          const cells = Number(event.metadata.changed_cells);
          const sensitivity = event.metadata.sensitivity;

          const parts = [];
          if (Number.isFinite(ratio)) {
            parts.push("變動比例 " + (ratio * 100).toFixed(1) + "%");
          }
          if (Number.isInteger(cells)) {
            parts.push("變動區塊 " + cells);
          }
          if (["low", "normal", "high"].includes(sensitivity)) {
            const names = { low: "低", normal: "一般", high: "高" };
            parts.push("靈敏度 " + names[sensitivity]);
          }

          details.textContent = parts.join(" · ");
          if (parts.length) body.append(details);
        }

        card.append(body);
        list.append(card);
      }
    } catch (error) {
      if (document.hidden && error.name === "AbortError") return;
      setStatus(error.name === "AbortError" ? "連線逾時，稍後重試" : "事件連線中斷，稍後重試", "error");
      delete list.dataset.eventIds;
      if (list.querySelector(".ai-event")) return;
      list.replaceChildren();
      const message = document.createElement("div");
      message.className = "ai-event-empty";
      message.textContent = "無法載入 AI 事件：" + (error?.message || error);
      list.append(message);
    } finally {
      clearTimeout(timeout);
      pending = null;
      clearTimeout(refreshTimer);
      if (!document.hidden) refreshTimer = setTimeout(refresh, 15000);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    makeUi();
    refresh();
    document.addEventListener("visibilitychange", () => {
      clearTimeout(refreshTimer);
      if (document.hidden) pending?.abort();
      else refresh();
    });
    window.addEventListener("pagehide", () => {
      clearTimeout(refreshTimer);
      pending?.abort();
    });
  });
})();
