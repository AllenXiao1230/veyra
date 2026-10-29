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
  let context = null;
  let analyser = null;
  let source = null;
  let attachedStream = null;
  let samples = null;
  let meterRaf = 0;
  let fingerprint = "";
  let lastChange = Date.now();
  let healthBusy = false;
  const canvas = document.createElement("canvas");
  canvas.width = 160;
  canvas.height = 40;
  const painter = canvas.getContext("2d", {willReadFrequently: true});

  function liveStatus(healthy, text) {
    $("liveStatus").dataset.state = healthy ? "ok" : "error";
    $("liveStatus").lastElementChild.textContent = text;
  }
  function scheduleRetry() {
    if (retryTimer || !ui.preferences().reconnect || paused || document.hidden) return;
    const delay = Math.min(15000, 1000 * 2 ** Math.min(4, retries++));
    retryTimer = setTimeout(() => { retryTimer = null; connect(); }, delay);
  }
  function connect(replace = true) {
    clearTimeout(retryTimer);
    clearTimeout(connectionTimer);
    retryTimer = null;
    if (paused || document.hidden) return;
    connected = false;
    imageFailed = false;
    fingerprint = "";
    lastChange = Date.now();
    liveStatus(false, "連線中");
    $("viewerMessage").hidden = false;
    $("viewerMessage").textContent = "正在連接攝影機…";
    image.style.objectFit = ui.preferences().fit;
    if (replace || !image.getAttribute("src")) image.src = "/camera/stream?t=" + Date.now();
    connectionTimer = setTimeout(() => { if (!connected) { liveStatus(false, "重新連線中"); scheduleRetry(); } }, 12000);
  }
  image.addEventListener("load", () => {
    if (paused) return;
    connected = true;
    imageFailed = false;
    retries = 0;
    clearTimeout(connectionTimer);
    $("viewerMessage").hidden = true;
    liveStatus(true, "即時連線");
  });
  image.addEventListener("error", () => {
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
    if (!ui.preferences().reconnect) { clearTimeout(retryTimer); retryTimer = null; }
    else if (!connected) scheduleRetry();
  });
  function recorderState(state) {
    const active = state.recording;
    $("recordStatus").dataset.state = active ? "recording" : state.recording_state === "failed" ? "error" : "ok";
    $("recordStatus").lastElementChild.textContent = active ? "錄影中" : state.motion_enabled ? "動態觸發已開啟" : "尚未錄影";
    if (!recordBusy) {
      $("recordToggle").disabled = false;
      $("recordToggle").classList.toggle("danger", active);
      $("recordToggle").innerHTML = ui.icon(active ? "stop" : "record") + `<span>${active ? "停止錄影" : "開始錄影"}</span>`;
    }
    $("recordWarning").hidden = !state.last_error;
    $("recordWarning").textContent = state.last_error ?
      (/8554|audio|音訊|404/.test(state.last_error) ? "音訊尚未就緒，錄影無法啟動。請到設定檢查麥克風或重啟音訊。" : "錄影失敗，請稍後重試或檢查服務狀態。") : "";
  }
  window.addEventListener("camera-recorder", event => recorderState(event.detail));
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
  function drawMeter() {
    if (!analyser || !samples || document.hidden) return;
    analyser.getByteTimeDomainData(samples);
    const rms = Math.sqrt(samples.reduce((sum, x) => sum + ((x - 128) / 128) ** 2, 0) / samples.length);
    const db = rms > 0 ? 20 * Math.log10(rms) : -100;
    const value = Math.round(Math.max(0, Math.min(100, (db + 60) / 60 * 100)));
    $("audioLevel").style.width = value + "%";
    $("audioLevel").parentElement.setAttribute("aria-valuenow", String(value));
    meterRaf = requestAnimationFrame(drawMeter);
  }
  function attachMeter() {
    const stream = audioVideo()?.srcObject;
    if (!context || !stream?.getAudioTracks().length || stream === attachedStream) return;
    try {
      source?.disconnect();
      analyser?.disconnect();
      source = context.createMediaStreamSource(stream);
      analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      samples = new Uint8Array(analyser.fftSize);
      source.connect(analyser);
      attachedStream = stream;
      cancelAnimationFrame(meterRaf);
      drawMeter();
    } catch (_) {}
  }
  async function startAudio() {
    const video = audioVideo();
    if (!video) throw new Error("音訊播放器載入中，請再按一次開啟監聽。");
    context ||= new (window.AudioContext || window.webkitAudioContext)();
    context.resume().catch(() => {});
    video.muted = false;
    await video.play();
    attachMeter();
  }
  $("listenToggle").addEventListener("click", async () => {
    if (wantsAudio) {
      const video = audioVideo();
      if (video) { video.muted = true; video.pause(); }
      wantsAudio = false;
      cancelAnimationFrame(meterRaf);
      $("audioLevel").style.width = "0%";
    } else {
      try { await startAudio(); wantsAudio = true; if (analyser) { cancelAnimationFrame(meterRaf); drawMeter(); } }
      catch (error) { ui.toast(error.message || "請重新開啟監聽。"); }
    }
    listenButton();
  });
  frame.addEventListener("load", () => {
    attachedStream = null;
    source?.disconnect();
    analyser?.disconnect();
    cancelAnimationFrame(meterRaf);
    if (wantsAudio) startAudio().catch(() => { wantsAudio = false; listenButton(); ui.toast("音訊已重連，請再按一次開啟監聽。"); });
  });
  window.addEventListener("camera-audio", event => {
    const state = event.detail;
    const selected = state.sources.find(s => s.name === state.selected_source);
    $("audioSummary").textContent = state.healthy ? ui.sourceLabel(selected) + (state.actual_muted ? " · 麥克風已靜音" : " · 已連線") : "音訊中斷，自動重連中";
    if (state.healthy && audioSeen && (state.pid !== publisherPid || !wasHealthy)) frame.src = "/mic/?reconnect=" + Date.now();
    publisherPid = state.pid;
    wasHealthy = state.healthy;
    audioSeen = true;
  });
  async function checkPreview() {
    if (healthBusy || paused || document.hidden) return;
    healthBusy = true;
    try {
      const health = await ui.api("/camera/health", undefined, 5000);
      if (health.healthy && !imageFailed && image.getAttribute("src")) {
        connected = true;
        retries = 0;
        clearTimeout(connectionTimer);
        $("viewerMessage").hidden = true;
        liveStatus(true, "即時連線");
      } else { liveStatus(false, "影像暫停更新"); scheduleRetry(); }
    } catch (_) { liveStatus(false, "影像連線中斷"); scheduleRetry(); }
    finally { healthBusy = false; }
  }
  const monitorTimer = setInterval(() => {
    if (document.hidden) return;
    checkPreview();
    if (wantsAudio) attachMeter();
    if (!paused && image.naturalWidth && connected) {
      try {
        painter.drawImage(image, 12, 12, Math.min(450, image.naturalWidth - 12), 48, 0, 0, 160, 40);
        const bytes = painter.getImageData(0, 0, 160, 40).data;
        let hash = 0;
        for (let i = 0; i < bytes.length; i += 16) hash = ((hash << 5) - hash + bytes[i]) | 0;
        if (String(hash) !== fingerprint) { fingerprint = String(hash); lastChange = Date.now(); }
        else if (Date.now() - lastChange > 20000) { liveStatus(false, "影像暫停更新"); scheduleRetry(); }
      } catch (_) {}
    }
  }, 3000);
  document.addEventListener("visibilitychange", () => {
    cancelAnimationFrame(meterRaf);
    if (document.hidden) { clearTimeout(retryTimer); retryTimer = null; image.removeAttribute("src"); connected = false; }
    else { if (!paused) connect(); if (wantsAudio && analyser) drawMeter(); }
  });
  window.addEventListener("pagehide", event => {
    image.removeAttribute("src");
    cancelAnimationFrame(meterRaf);
    if (!event.persisted) { clearInterval(monitorTimer); clearTimeout(retryTimer); clearTimeout(connectionTimer); }
  });
  if (ui.recorder()) recorderState(ui.recorder());
  connect(false);
  checkPreview();
})();
