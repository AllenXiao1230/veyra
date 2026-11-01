"use strict";
(() => {
  const paths = {
    bell: '<path d="M5 17h14l-2-3V9a5 5 0 0 0-10 0v5l-2 3Z"/><path d="M10 21h4"/>',
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
    ,star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z"/>'
  };
  const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.image}</svg>`;
  const $ = id => document.getElementById(id);
  const page = document.body.dataset.page;
  const pageSizeControl = $('mediaPageSize');
  const pageSizes = ['12', '24', '48', '96'];
  const defaultPageSize = window.matchMedia('(max-width:600px)').matches ? '12' : '24';
  const pageSizeKey = 'camera.library.pageSize.' + page;
  const pageSize = () => pageSizes.includes(pageSizeControl?.value) ? pageSizeControl.value : defaultPageSize;
  if (pageSizeControl) {
    let saved = 'auto';
    try { saved = localStorage.getItem(pageSizeKey) || 'auto'; } catch (_) {}
    pageSizeControl.value = pageSizes.includes(saved) ? saved : 'auto';
    pageSizeControl.firstElementChild.textContent = `自動（${defaultPageSize} 項）`;
    pageSizeControl.addEventListener('change', () => {
      try { localStorage.setItem(pageSizeKey, pageSizeControl.value); } catch (_) {}
    });
  }
  $('mediaDate')?.setAttribute('aria-label','日期');
  document.querySelectorAll('.view-switch a').forEach(link => {
    const target = new URL(link.href);
    const current = new URL(location.href);
    if (target.pathname === current.pathname && target.searchParams.get('view') === current.searchParams.get('view')) link.setAttribute('aria-current','page');
  });
  $("appHeader").innerHTML = `<div class="header-inner"><a class="brand" href="/">${icon("camera")}<span>Camera Monitor</span></a><nav class="main-nav" aria-label="主要導覽"><a href="/" ${page === "live" ? 'aria-current="page"' : ""}>即時監看</a><a href="/snapshots.html" ${page === "snapshots" ? 'aria-current="page"' : ""}>快照</a><a href="/recordings.html" ${page === "recordings" ? 'aria-current="page"' : ""}>錄影</a></nav><button class="header-settings quiet" id="openSettings" type="button" aria-label="設定">${icon("settings")}<span>設定</span></button></div>`;
  document.querySelectorAll("[data-icon]").forEach(el => { el.innerHTML = icon(el.dataset.icon); });

  const readers = new Set();
  async function api(url, payload, timeout = 12000, signal) {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (signal?.aborted) controller.abort();
    signal?.addEventListener("abort", cancel, {once:true});
    if (payload === undefined) readers.add(controller);
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
    } finally { clearTimeout(timer); readers.delete(controller); signal?.removeEventListener("abort", cancel); }
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

  let preferences = {fit: "contain", reconnect: true, bandwidth: "auto", stream:"auto"};
  try { preferences = {...preferences, ...JSON.parse(localStorage.getItem("camera.preferences") || "{}")}; } catch (_) {}
  function refreshPreferences() {
    try { preferences={...preferences,...JSON.parse(localStorage.getItem('camera.preferences')||'{}')}; } catch (_) {}
    window.dispatchEvent(new CustomEvent('camera-preferences',{detail:preferences}));
  }
  let lastAudio = null;
  let lastRecorder = null;
  let settingsAudioBaseline = null;
  let settingsMotionBaseline = null;
  let settingsGeneration = 0;
  let settingsBackupBaseline = null;
  let settingsNotificationBaseline = null;
  let settingsDetectionBaseline = null;
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
  const detectionIds=['settingMotionArea','settingMotionDifference','settingMotionFrames','settingMotionLight'];
  $('settingMotion').closest('section').insertAdjacentHTML('afterend',`<section class="settings-section"><h3>動作判斷</h3><div class="setting-row"><label for="settingMotionArea">最小動作面積（%）</label><input id="settingMotionArea" type="number" min="0.05" max="50" step="0.001" disabled></div><div class="setting-row"><label for="settingMotionDifference">像素差閾值（8–80）</label><input id="settingMotionDifference" type="number" min="8" max="80" step="1" disabled></div><div class="setting-row"><label for="settingMotionFrames">連續確認（影格）</label><input id="settingMotionFrames" type="number" min="1" max="12" step="1" disabled></div><div class="setting-row"><label for="settingMotionLight">補償整體亮度變化</label><input id="settingMotionLight" type="checkbox" disabled></div><p class="settings-status">閾值越大越嚴格；目前約每秒 2 影格。即時套用，AI 只標註，錄影停止延遲另設。</p><p id="motionMeasurement" class="settings-status" role="status"></p></section>`);
  settings.querySelectorAll('section')[1].insertAdjacentHTML("beforeend", '<div class="setting-row"><label for="settingRecordQuota">錄影容量上限（GB）</label><input id="settingRecordQuota" type="number" min="0" max="102400" step="any" value="0" disabled></div>');
  $("settingMotion").closest("section").insertAdjacentHTML("beforeend", `<div class="setting-row"><label for="settingStopDelay">動作停止後延遲（秒）</label><input id="settingStopDelay" type="number" min="5" max="600" step="1" value="30" disabled></div><div class="setting-row"><label for="settingReserve">錄影保留空間（MB）</label><input id="settingReserve" type="number" min="256" max="102400" step="256" value="1024" disabled></div><p class="settings-status" id="settingsDetectionStatus">動作觸發，AI 輔助分類</p>`);
  $("settingsError").insertAdjacentHTML("beforebegin", `<section class="settings-section"><h3>備份</h3><div class="setting-row"><span>本機備份與復原</span><a class="button" href="/backups.html">備份管理</a></div></section>`);
  $("settingsError").previousElementSibling.insertAdjacentHTML("beforeend", '<div class="setting-row"><label for="settingBackupQuota">備份容量預算（GB）</label><input id="settingBackupQuota" type="number" min="0" max="102400" step="1" value="0" disabled></div><p class="settings-status">容量設為 0 表示不限。達上限不刪除資料；回收桶仍計入容量。備份預算以完整副本保守估算。</p>');
  $("settingsForm").querySelector(".dialog-footer").prepend($("settingsError"));
  $("settingsError").setAttribute("role", "alert");
  backdrop(settings);
  $("settingFit").closest("section").insertAdjacentHTML("beforeend", '<div class="setting-row"><label for="settingBandwidth">預覽頻寬</label><select id="settingBandwidth"><option value="auto">自動（手機省流量）</option><option value="standard">標準</option><option value="low">低頻寬</option></select></div><p class="settings-status">低頻寬使用 640px、約每秒 2 張影格；錄影畫質不變。</p>');
  $("settingReserve").step = "1";
  $('settingFit').closest('section').insertAdjacentHTML('beforeend','<div class="setting-row"><label for="settingStream">即時串流</label><select id="settingStream"><option value="auto">自動（手機省流量）</option><option value="integrated">音畫整合（低延遲）</option><option value="hls">音畫整合（穩定優先）</option><option value="images">影格備援</option></select></div>');
  settings.querySelector('.dialog-body').insertAdjacentHTML('beforeend','<section class="settings-section"><h3>站內通知</h3><div class="setting-row"><label for="settingNotices">故障通知</label><input id="settingNotices" type="checkbox" disabled></div><div class="setting-row"><label for="settingRecoveries">恢復通知</label><input id="settingRecoveries" type="checkbox" disabled></div><div class="setting-row"><label for="settingNoticeCooldown">相同故障冷卻</label><select id="settingNoticeCooldown" disabled><option value="60">1 分鐘</option><option value="300">5 分鐘</option><option value="900">15 分鐘</option><option value="3600">60 分鐘</option></select></div></section>');
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
    settingsNotificationBaseline=null;
    settingsDetectionBaseline=null;
    for(const id of detectionIds)$(id).disabled=true;
    for(const id of ['settingNotices','settingRecoveries','settingNoticeCooldown'])$(id).disabled=true;
    showSettingsError("");
    const results = await Promise.allSettled([api("/audio-api/status"), api("/recordings-api/status"), api("/backups-api/policy"),api('/backups-api/notifications/policy'),api('/ai-api/motion/settings')]);
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
    if(results[3].status==='fulfilled'){
      const policy=results[3].value;settingsNotificationBaseline={enabled:policy.enabled,recoveries:policy.recoveries,cooldown:policy.cooldown};
      $('settingNotices').checked=policy.enabled;$('settingRecoveries').checked=policy.recoveries;
      if(![...$('settingNoticeCooldown').options].some(o=>o.value===String(policy.cooldown))){const option=element('option','',policy.cooldown+' 秒');option.value=String(policy.cooldown);$('settingNoticeCooldown').append(option);}
      $('settingNoticeCooldown').value=String(policy.cooldown);
      for(const id of ['settingNotices','settingRecoveries','settingNoticeCooldown'])$(id).disabled=false;
    }else showSettingsError('通知設定讀取失敗，請重新開啟設定。','loadNotifications');
    if(results[4].status==='fulfilled'){
      const p=settingsDetectionBaseline=results[4].value;
      $('settingMotionArea').value=p.area_percent;$('settingMotionDifference').value=p.difference;
      $('settingMotionFrames').value=p.confirm_frames;$('settingMotionLight').checked=p.light_compensation;
      for(const id of detectionIds)$(id).disabled=false;
    }else showSettingsError('動作閾值讀取失敗，請重新開啟設定。','loadDetection');
    $("saveSettings").disabled = false;
  }
  $("openSettings").addEventListener("click", () => {
    $("settingFit").value = preferences.fit;
    $("settingReconnect").checked = preferences.reconnect;
    $("settingBandwidth").value = preferences.bandwidth;
    $('settingStream').value=preferences.stream||'auto';
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
      const detection={area_percent:Number($('settingMotionArea').value),difference:Number($('settingMotionDifference').value),confirm_frames:Number($('settingMotionFrames').value),light_compensation:$('settingMotionLight').checked};
      if(settingsDetectionBaseline&&JSON.stringify(detection)!==JSON.stringify(settingsDetectionBaseline)){
        try{settingsDetectionBaseline=await api('/ai-api/motion/settings',detection);showSettingsError('','saveDetection');}
        catch(error){showSettingsError('動作判斷：'+error.message,'saveDetection');}
      }else if(settingsDetectionBaseline)showSettingsError('','saveDetection');
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
      const notificationPolicy={enabled:$('settingNotices').checked,recoveries:$('settingRecoveries').checked,cooldown:Number($('settingNoticeCooldown').value)};
      if(settingsNotificationBaseline&&JSON.stringify(notificationPolicy)!==JSON.stringify(settingsNotificationBaseline)){
        try{await api('/backups-api/notifications/policy',notificationPolicy);settingsNotificationBaseline=notificationPolicy;showSettingsError('','saveNotifications');}
        catch(error){showSettingsError('通知：'+error.message,'saveNotifications');}
      }else if(settingsNotificationBaseline)showSettingsError('','saveNotifications');
      preferences = {fit: $("settingFit").value, reconnect: $("settingReconnect").checked, bandwidth: $("settingBandwidth").value,stream:$('settingStream').value};
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
    if (polling || document.hidden || (page !== "live" && !settings.open)) return;
    polling = true;
    try {
      const results = await Promise.allSettled([api("/audio-api/status", undefined, 7000), api("/recordings-api/status", undefined, 8000)]);
      if(settings.open){
        try{const state=await api('/ai-api/status',undefined,5000);if(settings.open){const m=state.motion_measurement;$('motionMeasurement').textContent=state.warmup_remaining>0?'背景學習中 · 剩餘 '+state.warmup_remaining+' 影格':m?`目前動作面積 ${m.area_percent}% · 變動畫素 ${m.activity_percent}%`:'等待動作量測';}}
        catch(_){if(settings.open)$('motionMeasurement').textContent='即時動作量測暫不可用';}
      }
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
  const timeFormat = new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
  const dateFormat = new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
  const dayFormat = new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',year:'numeric',month:'long',day:'numeric',weekday:'short'});
  window.CameraUI = {api, icon, pageSize, element, button, backdrop, toast, sourceLabel, preferences: () => preferences, poll,
    setRecorder: state => { lastRecorder = state; window.dispatchEvent(new CustomEvent("camera-recorder", {detail: state})); },
    audio: () => lastAudio, recorder: () => lastRecorder,
    time: value => timeFormat.format(new Date(value)),
    dateTime: value => dateFormat.format(new Date(typeof value === 'number' ? value * 1000 : value)),
    day: value => dayFormat.format(new Date(value + 'T12:00:00+08:00')),
    size: value => value >= 1073741824 ? (value / 1073741824).toFixed(1) + " GB" : value >= 1048576 ? (value / 1048576).toFixed(1) + " MB" : value >= 1024 ? (value / 1024).toFixed(1) + " KB" : value + " B"};
  poll();
  let pollingTimer = setInterval(poll, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); else readers.forEach(c => c.abort()); });
  window.addEventListener("pageshow", event => { if (event.persisted) { refreshPreferences();poll(); clearInterval(pollingTimer); pollingTimer = setInterval(poll, 5000); } });
  window.addEventListener('storage',event=>{if(event.key==='camera.preferences')refreshPreferences();});
  window.addEventListener("pagehide", event => { readers.forEach(c => c.abort()); if (!event.persisted) clearInterval(pollingTimer); });
})();
