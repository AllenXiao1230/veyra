"use strict";
(() => {
  const paths = {
    camera: '<path d="M15 7h4a2 2 0 0 1 2 2v9H3V9a2 2 0 0 1 2-2h2l2-3h4l2 3Z"/><circle cx="12" cy="12" r="3"/>',
    settings: '<path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z"/><path d="m9 3-1 3-3 1 1 3-2 2 2 2-1 3 3 1 1 3h6l1-3 3-1-1-3 2-2-2-2 1-3-3-1-1-3Z"/>',
    trash: '<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5 8a8 8 0 0 1 13-3l2 2M19 16a8 8 0 0 1-13 3l-2-2"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
    volume: '<path d="M4 9h4l5-5v16l-5-5H4ZM17 8a6 6 0 0 1 0 8M20 5a10 10 0 0 1 0 14"/>',
    muted: '<path d="M4 9h4l5-5v16l-5-5H4ZM17 9l5 6M22 9l-5 6"/>',
    pause: '<path d="M8 5v14M16 5v14"/>',
    play: '<path d="m8 4 12 8-12 8V4Z"/>',
    record: '<circle cx="12" cy="12" r="7"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    expand: '<path d="M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5"/>',
    image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m3 16 5-5 5 5 3-3 5 5"/>',
    video: '<rect x="3" y="5" width="13" height="14" rx="2"/><path d="m16 10 5-4v12l-5-4"/>',
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    restore: '<path d="M4 4v6h6"/><path d="M4 10a8 8 0 1 1 0 6M12 8v5l3 2"/>',
    check: '<path d="m4 12 5 5L20 6"/>'
  };
  const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.image}</svg>`;
  const $ = id => document.getElementById(id);
  const page = document.body.dataset.page;
  $("appHeader").innerHTML = `<div class="header-inner"><a class="brand" href="/">${icon("camera")}<span>Camera Monitor</span></a><nav class="main-nav" aria-label="主要導覽"><a href="/" ${page === "live" ? 'aria-current="page"' : ""}>即時監看</a><a href="/snapshots.html" ${page === "snapshots" ? 'aria-current="page"' : ""}>快照</a><a href="/recordings.html" ${page === "recordings" ? 'aria-current="page"' : ""}>錄影</a></nav><button class="header-settings quiet" id="openSettings" type="button">${icon("settings")}<span>設定</span></button></div>`;
  document.querySelectorAll("[data-icon]").forEach(el => { el.innerHTML = icon(el.dataset.icon); });

  async function api(url, payload, timeout = 12000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url, {cache: "no-store", signal: controller.signal,
        ...(payload === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)})});
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(data?.error || "操作失敗，請重新整理後再試");
      if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("服務回應格式不正確，請稍後重新整理");
      if (url === "/recordings-api/status" && (typeof data.recording !== "boolean" || typeof data.motion_enabled !== "boolean")) throw new Error("錄影狀態暫時無法讀取，請稍後重試");
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("連線逾時，請稍後重試");
      throw error;
    } finally { clearTimeout(timer); }
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function button(label, name, className = "icon-button quiet") {
    const node = element("button", className);
    node.type = "button";
    node.setAttribute("aria-label", label);
    node.title = label;
    node.innerHTML = icon(name);
    return node;
  }
  function backdrop(dialog) {
    dialog.addEventListener("click", event => {
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
    });
  }
  const toastNode = element("div", "toast");
  toastNode.setAttribute("role", "status");
  toastNode.hidden = true;
  document.body.append(toastNode);
  let toastTimer;
  function toast(text, action, actionLabel = "復原") {
    clearTimeout(toastTimer);
    toastNode.replaceChildren(element("span", "", text));
    if (action) {
      const undo = element("button", "quiet", actionLabel);
      undo.type = "button";
      undo.addEventListener("click", async () => {
        undo.disabled = true;
        try { await action(); }
        catch (error) { toast(error.message); }
      });
      toastNode.append(undo);
    }
    const close = button("關閉通知", "close");
    close.addEventListener("click", () => { toastNode.hidden = true; });
    toastNode.append(close);
    toastNode.hidden = false;
    if (!action) toastTimer = setTimeout(() => { toastNode.hidden = true; }, 7000);
  }

  let preferences = {fit: "contain", reconnect: true};
  try { preferences = {...preferences, ...JSON.parse(localStorage.getItem("camera.preferences") || "{}")}; } catch (_) {}
  let lastAudio = null;
  let lastRecorder = null;
  let settingsAudioBaseline = null;
  let settingsMotionBaseline = null;
  let settingsGeneration = 0;
  let settingsBackupBaseline = null;
  const settingsErrors = new Map();
  let polling = false;
  const settings = element("dialog");
  settings.id = "settingsDialog";
  settings.setAttribute("aria-labelledby", "settingsTitle");
  settings.innerHTML = `<form id="settingsForm"><div class="dialog-heading"><h2 id="settingsTitle">設定</h2><button class="icon-button quiet" type="button" id="closeSettings" aria-label="關閉設定">${icon("close")}</button></div><div class="dialog-body">
    <section class="settings-section"><h3>影像</h3><div class="setting-row"><label for="settingFit">畫面顯示</label><select id="settingFit"><option value="contain">完整顯示</option><option value="cover">填滿畫面</option></select></div><div class="setting-row"><label for="settingReconnect">自動重新連線</label><input id="settingReconnect" type="checkbox"></div></section>
    <section class="settings-section"><h3>錄影</h3><div class="setting-row"><label for="settingMotion">動態觸發錄影</label><input id="settingMotion" type="checkbox" disabled></div></section>
    <section class="settings-section"><h3>音訊</h3><div class="setting-source"><label for="settingSource">輸入來源</label><select id="settingSource" disabled><option value="">讀取中…</option></select></div><div class="setting-row"><label for="settingVolume">輸入音量</label><input id="settingVolume" type="range" min="0" max="100" value="80"><output id="settingVolumeValue" for="settingVolume">80%</output></div><div class="setting-row"><label for="settingMuted">麥克風靜音</label><input id="settingMuted" type="checkbox"></div><p class="settings-status" id="settingsAudioStatus"></p><p class="notice" id="settingsRecordingWarning" hidden>錄影中無法切換或重啟音訊，請先停止錄影。</p><div class="setting-row"><button id="settingAudioRestart" type="button">${icon("refresh")}重新啟動音訊</button><button id="settingSourcesRefresh" class="quiet" type="button">更新來源</button></div></section>
    <p class="dialog-error" id="settingsError" role="status" hidden></p></div><div class="dialog-footer"><button id="cancelSettings" type="button">取消</button><button id="saveSettings" class="primary" type="submit">儲存設定</button></div></form>`;
  document.body.append(settings);
  settings.querySelectorAll('section')[1].insertAdjacentHTML("beforeend", '<div class="setting-row"><label for="settingRecordQuota">錄影容量上限（GB）</label><input id="settingRecordQuota" type="number" min="0" max="102400" step="any" value="0" disabled></div>');
  $("settingMotion").closest("section").insertAdjacentHTML("beforeend", `<div class="setting-row"><label for="settingStopDelay">動作停止後延遲（秒）</label><input id="settingStopDelay" type="number" min="5" max="600" step="1" value="30" disabled></div><div class="setting-row"><label for="settingReserve">錄影保留空間（MB）</label><input id="settingReserve" type="number" min="256" max="102400" step="256" value="1024" disabled></div><p class="settings-status" id="settingsDetectionStatus">動作觸發，AI 輔助分類</p>`);
  $("settingsError").insertAdjacentHTML("beforebegin", `<section class="settings-section"><h3>備份</h3><div class="setting-row"><span>本機備份與復原</span><a class="button" href="/backups.html">備份管理</a></div></section>`);
  $("settingsError").previousElementSibling.insertAdjacentHTML("beforeend", '<div class="setting-row"><label for="settingBackupQuota">備份容量預算（GB）</label><input id="settingBackupQuota" type="number" min="0" max="102400" step="1" value="0" disabled></div><p class="settings-status">容量設為 0 表示不限。達上限不刪除資料；回收桶仍計入容量。備份預算以完整副本保守估算。</p>');
  $("settingsForm").querySelector(".dialog-footer").prepend($("settingsError"));
  $("settingsError").setAttribute("role", "alert");
  backdrop(settings);
  $("settingReserve").step = "1";
  settings.addEventListener("close", () => { settingsGeneration++; });
  function sourceLabel(source) {
    const description = source?.description || source?.name || "麥克風";
    if (description.includes("Digital Microphone")) return "內建數位麥克風";
    if (description.includes("Headphones Stereo Microphone")) return "耳麥輸入";
    return description;
  }
  function showSettingsError(text, scope = "general") {
    if (text) settingsErrors.set(scope, text);
    else settingsErrors.delete(scope);
    const partial = [...settingsErrors.keys()].some(key => key.startsWith("save"));
    $("settingsError").hidden = !settingsErrors.size;
    $("settingsError").textContent = (partial ? "部分設定未完成，成功項目已保留。" : "") + [...settingsErrors.values()].join("；");
  }
  function renderSettingsAudio(state) {
    settingsAudioBaseline = {source: state.selected_source, volume: state.volume, muted: state.muted};
    const source = $("settingSource");
    const options = (state.sources || []).map(device => {
      const option = element("option", "", sourceLabel(device) + (device.available ? "" : "（未連接）"));
      option.value = device.name;
      option.title = device.name;
      return option;
    });
    if (state.selected_source && !state.sources?.some(s => s.name === state.selected_source)) {
      const option = element("option", "", "所選麥克風已離線");
      option.value = state.selected_source;
      options.push(option);
    }
    source.replaceChildren(...options);
    source.value = state.selected_source || "";
    source.disabled = !options.length;
    $("settingVolume").value = state.volume;
    $("settingVolume").disabled = false;
    $("settingVolumeValue").textContent = state.volume + "%";
    $("settingMuted").checked = state.muted;
    $("settingMuted").disabled = false;
    $("settingsAudioStatus").textContent = state.healthy ? "音訊已連線" : "音訊尚未就緒，正在自動重連";
  }
  async function loadSettings() {
    const generation = ++settingsGeneration;
    settingsAudioBaseline = settingsMotionBaseline = null;
    settingsBackupBaseline = null;
    $("settingRecordQuota").disabled = true;
    $("settingBackupQuota").disabled = true;
    $("saveSettings").disabled = true;
    $("settingMotion").disabled = true;
    $("settingStopDelay").disabled = true;
    $("settingReserve").disabled = true;
    $("settingSource").disabled = true;
    $("settingVolume").disabled = true;
    $("settingMuted").disabled = true;
    settingsErrors.clear();
    showSettingsError("");
    const results = await Promise.allSettled([api("/audio-api/status"), api("/recordings-api/status"), api("/backups-api/policy")]);
    if (generation !== settingsGeneration || !settings.open) return;
    if (results[0].status === "fulfilled") {
      lastAudio = results[0].value;
      renderSettingsAudio(lastAudio);
    } else showSettingsError("無法讀取音訊設定，請按「更新來源」重試。", "loadAudio");
    if (results[1].status === "fulfilled") {
      lastRecorder = results[1].value;
      $("settingMotion").checked = lastRecorder.motion_enabled;
      settingsMotionBaseline = {enabled: lastRecorder.motion_enabled, stop_delay: lastRecorder.stop_delay || 30, min_free_mb: lastRecorder.min_free_mb || 1024, max_storage_mb: lastRecorder.max_storage_mb || 0};
      $("settingRecordQuota").value = settingsMotionBaseline.max_storage_mb / 1024;
      $("settingRecordQuota").disabled = false;
      $("settingStopDelay").value = settingsMotionBaseline.stop_delay;
      $("settingReserve").value = settingsMotionBaseline.min_free_mb;
      $("settingMotion").disabled = false;
      $("settingStopDelay").disabled = false;
      $("settingReserve").disabled = false;
      $("settingsDetectionStatus").textContent = lastRecorder.detection?.person_detector_enabled ? "動作觸發 · AI 分類正常" : "動作皆會記錄；AI 分類暫未就緒";
      $("settingsRecordingWarning").hidden = !lastRecorder.recording;
      $("settingAudioRestart").disabled = lastRecorder.recording || ["starting", "stopping"].includes(lastRecorder.recording_state);
    } else showSettingsError("無法讀取錄影設定，請稍後重新開啟設定。", "loadRecording");
    if (results[2].status === "fulfilled") {
      settingsBackupBaseline = results[2].value.max_backup_gb;
      $("settingBackupQuota").value = settingsBackupBaseline;
      $("settingBackupQuota").disabled = false;
    } else showSettingsError("無法讀取備份容量設定，請稍後重新開啟設定。", "loadBackup");
    $("saveSettings").disabled = false;
  }
  $("openSettings").addEventListener("click", () => {
    $("settingFit").value = preferences.fit;
    $("settingReconnect").checked = preferences.reconnect;
    settings.showModal();
    loadSettings();
  });
  $("closeSettings").addEventListener("click", () => settings.close());
  $("cancelSettings").addEventListener("click", () => settings.close());
  $("settingVolume").addEventListener("input", () => { $("settingVolumeValue").textContent = $("settingVolume").value + "%"; });
  $("settingSourcesRefresh").addEventListener("click", async () => {
    const pending = {source: $("settingSource").value, volume: Number($("settingVolume").value), muted: $("settingMuted").checked};
    const dirty = settingsAudioBaseline && Object.keys(pending).some(key => pending[key] !== settingsAudioBaseline[key]);
    try {
      renderSettingsAudio(lastAudio = await api("/audio-api/status"));
      if (dirty) {
        if (![...$("settingSource").options].some(option => option.value === pending.source)) {
          const option = element("option", "", "先前選擇的來源（未連接）"); option.value = pending.source; $("settingSource").append(option);
        }
        $("settingSource").value = pending.source;
        $("settingVolume").value = pending.volume;
        $("settingVolumeValue").textContent = pending.volume + "%";
        $("settingMuted").checked = pending.muted;
      }
      showSettingsError("", "loadAudio"); showSettingsError("", "audioRefresh");
    } catch (error) { showSettingsError(error.message, "audioRefresh"); }
  });
  $("settingAudioRestart").addEventListener("click", async event => {
    event.currentTarget.disabled = true;
    try { await api("/audio-api/restart", {}); showSettingsError("", "audioRestart"); $("settingsAudioStatus").textContent = "正在重新連接音訊…"; }
    catch (error) { showSettingsError(error.message, "audioRestart"); }
    finally { $("settingAudioRestart").disabled = false; }
  });
  $("settingsForm").addEventListener("submit", async event => {
    event.preventDefault();
    const save = $("saveSettings");
    save.disabled = true;
    save.textContent = "儲存中…";
    try {
      showSettingsError("", "general");
      const audio = {source: $("settingSource").value, volume: Number($("settingVolume").value), muted: $("settingMuted").checked};
      if (settingsAudioBaseline && !$("settingSource").disabled && audio.source &&
          (audio.source !== settingsAudioBaseline.source || audio.volume !== settingsAudioBaseline.volume || audio.muted !== settingsAudioBaseline.muted)) {
        try { await api("/audio-api/source", audio); settingsAudioBaseline = {...audio}; showSettingsError("", "saveAudio"); }
        catch (error) { showSettingsError("音訊：" + error.message, "saveAudio"); }
      } else if (settingsAudioBaseline) showSettingsError("", "saveAudio");
      const motion = {enabled: $("settingMotion").checked, stop_delay: Number($("settingStopDelay").value), min_free_mb: Number($("settingReserve").value), max_storage_mb: Math.round(Number($("settingRecordQuota").value) * 1024)};
      if (settingsMotionBaseline && !$("settingMotion").disabled && Object.keys(motion).some(key => motion[key] !== settingsMotionBaseline[key])) {
        try { await api("/recordings-api/motion", motion); settingsMotionBaseline = {...motion}; showSettingsError("", "saveRecording"); }
        catch (error) { showSettingsError("錄影：" + error.message, "saveRecording"); }
      } else if (settingsMotionBaseline) showSettingsError("", "saveRecording");
      const backupQuota = Number($("settingBackupQuota").value);
      if (settingsBackupBaseline !== null && backupQuota !== settingsBackupBaseline) {
        try { await api("/backups-api/policy", {max_backup_gb: backupQuota}); settingsBackupBaseline = backupQuota; showSettingsError("", "saveBackup"); }
        catch (error) { showSettingsError("備份：" + error.message, "saveBackup"); }
      } else if (settingsBackupBaseline !== null) showSettingsError("", "saveBackup");
      preferences = {fit: $("settingFit").value, reconnect: $("settingReconnect").checked};
      try { localStorage.setItem("camera.preferences", JSON.stringify(preferences)); } catch (_) {}
      window.dispatchEvent(new CustomEvent("camera-preferences", {detail: preferences}));
      if (settingsErrors.size) { poll(); return; }
      settings.close();
      toast("設定已儲存");
      poll();
    } catch (error) { showSettingsError(error.message); }
    finally { save.disabled = false; save.textContent = "儲存設定"; }
  });

  async function poll() {
    if (polling || document.hidden) return;
    polling = true;
    try {
      const results = await Promise.allSettled([api("/audio-api/status", undefined, 7000), api("/recordings-api/status", undefined, 8000)]);
      if (results[0].status === "fulfilled") {
        lastAudio = results[0].value;
        window.dispatchEvent(new CustomEvent("camera-audio", {detail: lastAudio}));
        if (settings.open) $("settingsAudioStatus").textContent = lastAudio.healthy ? "音訊已連線" : "音訊尚未就緒，正在自動重連";
      }
      if (results[1].status === "fulfilled") {
        lastRecorder = results[1].value;
        window.dispatchEvent(new CustomEvent("camera-recorder", {detail: lastRecorder}));
        if (settings.open) $("settingsRecordingWarning").hidden = !lastRecorder.recording;
        if (settings.open) $("settingAudioRestart").disabled = lastRecorder.recording || ["starting", "stopping"].includes(lastRecorder.recording_state);
      } else window.dispatchEvent(new Event("camera-recorder-unavailable"));
    } finally { polling = false; }
  }
  window.CameraUI = {api, icon, element, button, backdrop, toast, sourceLabel, preferences: () => preferences, poll,
    setRecorder: state => { lastRecorder = state; window.dispatchEvent(new CustomEvent("camera-recorder", {detail: state})); },
    audio: () => lastAudio, recorder: () => lastRecorder,
    time: value => new Intl.DateTimeFormat("zh-TW", {timeZone: "Asia/Taipei", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false}).format(new Date(value)),
    day: value => new Intl.DateTimeFormat("zh-TW", {timeZone: "Asia/Taipei", year: "numeric", month: "long", day: "numeric", weekday: "short"}).format(new Date(value + "T12:00:00+08:00")),
    size: value => value >= 1073741824 ? (value / 1073741824).toFixed(1) + " GB" : value >= 1048576 ? (value / 1048576).toFixed(1) + " MB" : value >= 1024 ? (value / 1024).toFixed(1) + " KB" : value + " B"};
  poll();
  let pollingTimer = setInterval(poll, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  window.addEventListener("pageshow", event => { if (event.persisted) { poll(); clearInterval(pollingTimer); pollingTimer = setInterval(poll, 5000); } });
  window.addEventListener("pagehide", event => { if (!event.persisted) clearInterval(pollingTimer); });
})();
