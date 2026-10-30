"use strict";
(() => {
  const ui = window.CameraUI;
  const $ = id => document.getElementById(id);
  const image = $("liveImage");
  const frame = $("micFrame");
  let paused = false;
  let connected = false;
  let imageFailed = false;
  let retries = 0;
  let retryTimer;
  let connectionTimer;
  let recordBusy = false;
  let wantsAudio = false;
  let publisherPid = null;
  let wasHealthy = false;
  let audioSeen = false;
  let levelTimer;
  let levelBusy = false;
  let pageGone = false;
  let fingerprint = "";
  let lastChange = Date.now();
  let healthBusy = false;
  const pull = window.CameraFrames ? new window.CameraFrames() : null;
  const mobileViewport = window.matchMedia?.('(max-width: 600px), (pointer: coarse) and (max-width: 1024px)');
  function streamMode() {
    const mode = ui.preferences().bandwidth || "auto";
    const network = window.navigator?.connection;
    return mode === "low" || (mode === "auto" && (mobileViewport?.matches || network?.saveData || /(^|-)2g$|3g/.test(network?.effectiveType || ""))) ? "low" : "standard";
  }
  let mode = streamMode();
  window.navigator?.connection?.addEventListener?.('change', () => { if(!paused && !pageGone && !document.hidden && mode !== streamMode()) connect(); });
  mobileViewport?.addEventListener?.('change', () => { if(!paused && !pageGone && !document.hidden && mode !== streamMode()) connect(); });
  const canvas = document.createElement("canvas");
  canvas.width = 160;
  canvas.height = 40;
  const painter = canvas.getContext("2d", {willReadFrequently: true});

  function liveStatus(healthy, text) {
    $("liveStatus").dataset.state = healthy ? "ok" : "error";
    $("liveStatus").lastElementChild.textContent = text;
  }
  function scheduleRetry() {
    if (retryTimer || !ui.preferences().reconnect || paused || pageGone || document.hidden) return;
    const delay = Math.min(15000, 1000 * 2 ** Math.min(4, retries++));
    retryTimer = setTimeout(() => { retryTimer = null; connect(); }, delay);
  }
  function connect(replace = true) {
    clearTimeout(retryTimer);
    clearTimeout(connectionTimer);
    retryTimer = null;
    if (paused || pageGone || document.hidden) return;
    connected = false;
    imageFailed = false;
    fingerprint = "";
    lastChange = Date.now();
    liveStatus(false, "連線中");
    $("viewerMessage").hidden = false;
    $("viewerMessage").textContent = "正在連接攝影機…";
    image.style.objectFit = ui.preferences().fit;
    mode = streamMode();
    if (mode === "low" && pull) {
      pull.start(image, () => {
        lastChange = Date.now(); connected = true; imageFailed = false;
        $("viewerMessage").hidden = true;
        liveStatus(true, "低頻寬 · 即時連線");
      }, () => {
        connected = false;
        liveStatus(false, ui.preferences().reconnect ? '影格中斷，正在重試' : '影格中斷');
        $('viewerMessage').hidden=false;
        $('viewerMessage').textContent=ui.preferences().reconnect ? '影像更新中斷，正在重新連線…' : '影像更新中斷，請按重新連線。';
      }, () => ui.preferences().reconnect && !paused && !pageGone && !document.hidden);
      return;
    }
    pull?.stop();
    if (replace || !image.getAttribute("src")) image.src = "/camera/preview?t=" + Date.now();
    connectionTimer = setTimeout(() => { if (!connected) { liveStatus(false, "重新連線中"); scheduleRetry(); } }, 12000);
  }
  image.addEventListener("load", () => {
    if (paused) return;
    connected = true;
    imageFailed = false;
    retries = 0;
    clearTimeout(connectionTimer);
    $("viewerMessage").hidden = true;
    liveStatus(true, mode === "low" ? "低頻寬 · 即時連線" : "即時連線");
  });
  image.addEventListener("error", () => {
    if (mode === "low") return;
    connected = false;
    imageFailed = true;
    if (!paused && !document.hidden) { liveStatus(false, "影像中斷"); scheduleRetry(); }
  });
  $("previewReconnect").addEventListener("click", () => {
    paused = false;
    $("previewPause").innerHTML = ui.icon("pause");
    $("previewPause").setAttribute("aria-label", "暫停預覽");
    retries = 0;
    connect();
  });
  $("previewPause").addEventListener("click", () => {
    paused = !paused;
    $("previewPause").innerHTML = ui.icon(paused ? "play" : "pause");
    $("previewPause").setAttribute("aria-label", paused ? "恢復預覽" : "暫停預覽");
    $("previewPause").title = paused ? "恢復預覽" : "暫停預覽";
    if (paused) {
      pull?.stop();
      clearTimeout(connectionTimer);
      clearTimeout(retryTimer);
      retryTimer = null;
      image.removeAttribute("src");
      $("viewerMessage").hidden = false;
      $("viewerMessage").textContent = "預覽已暫停";
      liveStatus(false, "已暫停");
    } else connect();
  });
  $("previewFullscreen").addEventListener("click", async () => {
    try {
      const viewer = $("liveViewer");
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (viewer.requestFullscreen) await viewer.requestFullscreen();
      else if (viewer.webkitRequestFullscreen) viewer.webkitRequestFullscreen();
      else ui.toast("此瀏覽器不支援網頁全螢幕，可改用橫向觀看。");
    } catch (_) { ui.toast("無法開啟全螢幕，請再試一次。"); }
  });
  window.addEventListener("camera-preferences", () => {
    image.style.objectFit = ui.preferences().fit;
    if (mode !== streamMode() && !paused) { connect(); return; }
    if (!ui.preferences().reconnect) { clearTimeout(retryTimer); retryTimer = null; }
    else if (!connected) scheduleRetry();
  });
  function recorderState(state) {
    const active = state.recording;
    const transitioning = ["starting", "stopping"].includes(state.recording_state);
    $("recordStatus").dataset.state = active ? "recording" : state.recording_state === "failed" ? "error" : "ok";
    $("recordStatus").lastElementChild.textContent = active ? (state.recording_trigger === "motion" ? "動作錄影中" + (!state.motion_detected && state.stop_in_seconds !== null ? ` · ${state.stop_in_seconds} 秒後停止` : "") : "手動錄影中") : transitioning ? (state.recording_state === "starting" ? "啟動中" : "正在完成影片") : state.motion_enabled ? (state.motion_suppressed ? "待動作結束後恢復觸發" : "動態觸發已開啟") : "動態觸發已停用";
    if (!recordBusy) {
      $("recordToggle").disabled = transitioning;
      $("recordToggle").classList.toggle("danger", active);
      $("recordToggle").innerHTML = ui.icon(active ? "stop" : "record") + `<span>${active ? "停止錄影" : "開始錄影"}</span>`;
      if (transitioning) $("recordToggle").textContent = state.recording_state === "starting" ? "啟動中…" : "停止中…";
    }
    if(state.encoder?.stalled){$('recordStatus').dataset.state='error';$('recordStatus').lastElementChild.textContent='錄影輸出停止，正在恢復';}
    $("recordWarning").hidden = !(state.last_error || state.motion_error || state.warning);
    $("recordWarning").textContent = state.last_error ?
      "錄影暫時失敗，請查看儲存與服務狀態或稍後重試。" : state.motion_error ? (active ? "動作偵測暫時中斷，持續錄影並等待恢復。" : "動作偵測暫時無法使用，請檢查服務狀態。") : state.warning || "";
  }
  window.addEventListener("camera-recorder", event => recorderState(event.detail));
  window.addEventListener("camera-recorder-unavailable", () => {
    $("recordStatus").dataset.state = "error";
    $("recordStatus").lastElementChild.textContent = "錄影狀態暫時無法讀取";
    if (!recordBusy) $("recordToggle").disabled = true;
  });
  $("recordToggle").addEventListener("click", async () => {
    if (recordBusy) return;
    const stop = !!ui.recorder()?.recording;
    recordBusy = true;
    $("recordToggle").disabled = true;
    $("recordToggle").textContent = stop ? "停止中…" : "啟動中…";
    try { await ui.api("/recordings-api/" + (stop ? "stop" : "start"), {}, 35000); }
    catch (error) { ui.toast(error.message.includes("FFmpeg") ? "錄影無法啟動，請檢查音訊設定。" : error.message); }
    finally {
      recordBusy = false;
      const state = await ui.api("/recordings-api/status").catch(() => null);
      if (state) ui.setRecorder(state);
      else { $("recordToggle").textContent = "重新檢查錄影"; $("recordToggle").disabled = false; }
      ui.poll();
    }
  });

  function audioVideo() { return frame.contentDocument?.querySelector("video"); }
  function listenButton() {
    $("listenToggle").innerHTML = ui.icon(wantsAudio ? "muted" : "volume") + `<span>${wantsAudio ? "停止監聽" : "開啟監聽"}</span>`;
    $("listenToggle").setAttribute("aria-pressed", String(wantsAudio));
  }
  function renderLevel(state) {
    const value = state?.available ? Math.max(0, Math.min(100, state.level)) : 0;
    const text = state?.available ? (state.dbfs === null ? "−∞ dBFS" : state.dbfs.toFixed(1) + " dBFS") : "暫無訊號";
    const meter = $("audioLevel").parentElement;
    $("audioLevel").style.width = value + "%";
    meter.dataset.state = state?.available && state.dbfs !== null && state.dbfs >= -3 ? "high" : "normal";
    meter.setAttribute("aria-valuenow", String(value));
    meter.setAttribute("aria-valuetext", text);
    $("audioLevelValue").textContent = text;
    $("audioLevelValue").removeAttribute("title");
  }
  async function pollLevel() {
    if (levelBusy || pageGone || document.hidden) return;
    clearTimeout(levelTimer);
    levelBusy = true;
    let delay = mode === "low" ? 1000 : 250;
    try { renderLevel(await ui.api("/audio-api/level", undefined, 5000)); }
    catch (error) { renderLevel(null); $("audioLevelValue").title = error.message; delay = 1000; }
    finally {
      levelBusy = false;
      if (!pageGone && !document.hidden) levelTimer = setTimeout(pollLevel, delay);
    }
  }
  async function startAudio() {
    const video = audioVideo();
    if (!video) throw new Error("音訊播放器載入中，請再按一次開啟監聽。");
    video.muted = false;
    await video.play();
  }
  $("listenToggle").addEventListener("click", async () => {
    if (wantsAudio) {
      const video = audioVideo();
      if (video) { video.muted = true; video.pause(); }
      wantsAudio = false;
      frame.removeAttribute("src");
    } else {
      try {
        if (!frame.getAttribute("src")) { wantsAudio = true; frame.src = "/mic/"; }
        else { await startAudio(); wantsAudio = true; }
      }
      catch (error) { ui.toast(error.message || "請重新開啟監聽。"); }
    }
    listenButton();
  });
  frame.addEventListener("load", () => {
    if (wantsAudio) startAudio().catch(() => {
      wantsAudio = false; listenButton(); ui.toast("播放器已就緒，請再按一次開啟監聽。");
      setTimeout(() => { if (!wantsAudio) frame.removeAttribute("src"); }, 30000);
    });
  });
  window.addEventListener("camera-audio", event => {
    const state = event.detail;
    const selected = state.sources.find(s => s.name === state.selected_source);
    $("audioSummary").textContent = state.healthy ? ui.sourceLabel(selected) + (state.actual_muted ? " · 麥克風已靜音" : " · 已連線") : "音訊中斷，自動重連中";
    if (wantsAudio && !document.hidden && state.healthy && audioSeen && (state.pid !== publisherPid || !wasHealthy)) frame.src = "/mic/?reconnect=" + Date.now();
    publisherPid = state.pid;
    wasHealthy = state.healthy;
    audioSeen = true;
  });
  async function checkPreview() {
    if (mode === "low" || healthBusy || paused || pageGone || document.hidden) return;
    healthBusy = true;
    try {
      const health = await ui.api("/camera/health", undefined, 5000);
      if (paused || pageGone || document.hidden) return;
      if (connected && !imageFailed && Date.now() - lastChange < 20000) {
        liveStatus(true, "即時連線");
      } else if (health.healthy && !imageFailed && Date.now() - lastChange < 20000) {
        liveStatus(false, "等待影格");
      } else { liveStatus(false, "影像暫停更新"); scheduleRetry(); }
    } catch (_) {
      // A delayed status request says nothing about an MJPEG image that keeps
      // advancing. Only a failed image or a stalled timestamp needs reconnect.
      if (!paused && !pageGone && !document.hidden && (imageFailed || Date.now() - lastChange >= 20000)) {
        liveStatus(false, "影像連線中斷"); scheduleRetry();
      }
    }
    finally { healthBusy = false; }
  }
  const monitorTimer = setInterval(() => {
    if (document.hidden || pageGone) return;
    if (mode !== "low" && !paused && !imageFailed && image.getAttribute("src") && image.naturalWidth) {
      try {
        painter.drawImage(image, 12, 12, Math.min(450, image.naturalWidth - 12), 48, 0, 0, 160, 40);
        const bytes = painter.getImageData(0, 0, 160, 40).data;
        let hash = 0;
        for (let i = 0; i < bytes.length; i += 16) hash = ((hash << 5) - hash + bytes[i]) | 0;
        if (String(hash) !== fingerprint) {
          fingerprint = String(hash); lastChange = Date.now();
          connected = true; retries = 0;
          clearTimeout(connectionTimer); clearTimeout(retryTimer); retryTimer = null;
          $("viewerMessage").hidden = true;
          liveStatus(true, "即時連線");
        }
        else if (Date.now() - lastChange > 20000) { liveStatus(false, "影像暫停更新"); scheduleRetry(); }
      } catch (_) {}
    }
    checkPreview();
  }, 3000);
  document.addEventListener("visibilitychange", () => {
    clearTimeout(levelTimer);
    if (document.hidden) {
      pull?.stop(); clearTimeout(connectionTimer); clearTimeout(retryTimer); retryTimer = null;
      image.removeAttribute("src"); connected = false;
      wantsAudio = false; frame.removeAttribute("src"); listenButton();
    }
    else { if (!paused) connect(); pollLevel(); }
  });
  window.addEventListener("pagehide", event => {
    pull?.stop(); frame.removeAttribute("src"); wantsAudio = false;
    image.removeAttribute("src");
    pageGone = true;
    clearTimeout(levelTimer);
    clearTimeout(retryTimer); retryTimer = null; clearTimeout(connectionTimer);
    if (!event.persisted) clearInterval(monitorTimer);
  });
  window.addEventListener("pageshow", event => { pageGone = false; pollLevel(); if (event.persisted && !paused) { connect(); checkPreview(); } });
  if (ui.recorder()) recorderState(ui.recorder());
  connect(false);
  checkPreview();
  pollLevel();
})();
