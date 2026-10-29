"use strict";
(() => {
  const ui = window.CameraUI;
  const $ = id => document.getElementById(id);
  const api = (path, body) => ui.api("/backups-api" + path, body, 30000);
  const date = epoch => new Intl.DateTimeFormat("zh-TW", {timeZone: "Asia/Taipei", dateStyle: "medium", timeStyle: "short"}).format(new Date(epoch * 1000));
  const actions = {create: "建立備份", verify: "驗證備份", recover: "還原至隔離目錄", trash: "移到回收桶", restore: "復原備份"};
  const states = {queued: "等待中", running: "進行中", complete: "已完成", failed: "失敗", interrupted: "已中斷"};
  let rows = [], trash = [], trashMode = false, busy = false, updating = false, lastJob = null, timer;
  let pending = null, loadGeneration = 0, loaded = false, statusLoaded = false;
  const confirmation = ui.element("dialog", "confirm-dialog");
  confirmation.setAttribute("aria-labelledby", "backupConfirmTitle");
  confirmation.innerHTML = '<div class="dialog-heading"><h2 id="backupConfirmTitle"></h2></div><p class="dialog-body" id="backupConfirmCopy"></p><div class="dialog-footer"><button id="backupCancel" type="button">取消</button><button id="backupConfirm" type="button">確認</button></div>';
  document.body.append(confirmation);
  ui.backdrop(confirmation);
  $("backupCancel").addEventListener("click", () => confirmation.close());
  function confirm(action, item) {
    pending = {action, item};
    $("backupConfirmTitle").textContent = actions[action] + "？";
    $("backupConfirmCopy").textContent = action === "trash" ? `${date(item.created_at)} 的備份會移到回收桶，可稍後復原。` : `將建立約 ${ui.size(item.size)} 的隔離副本，並校驗檔案；不會覆寫正式資料或套用系統設定。`;
    $("backupConfirm").textContent = actions[action];
    $("backupConfirm").className = action === "trash" ? "danger" : "primary";
    confirmation.showModal();
    $("backupCancel").focus();
  }
  $("backupConfirm").addEventListener("click", async () => {
    if (!pending) return;
    const {action, item} = pending;
    pending = null;
    confirmation.close();
    await perform(action, item);
  });
  async function perform(action, item) {
    if (busy) return;
    busy = true;
    render();
    try {
      const result = await api("/" + action, action === "restore" ? {token: item.token} : action === "create" ? {} : {name: item.name});
      if (result.job) { lastJob = result.job; renderJob(lastJob); }
      else ui.toast(action === "trash" ? "備份已移到回收桶，可在回收桶復原。" : "備份已復原");
      await load();
    } catch (error) { ui.toast(error.message); }
    finally { busy = false; render(); status(); }
  }
  function makeButton(text, action, item, className = "") {
    const button = ui.element("button", className, text);
    button.type = "button";
    button.disabled = busy || Boolean(lastJob && ["queued", "running"].includes(lastJob.state));
    button.addEventListener("click", () => ["trash", "recover"].includes(action) ? confirm(action, item) : perform(action, item));
    return button;
  }
  function render() {
    const list = $("backupList");
    const current = trashMode ? trash : rows;
    const working = Boolean(lastJob && ["queued", "running"].includes(lastJob.state));
    $("backupCount").textContent = loaded ? `${current.length} 個${trashMode ? "已移除備份" : "備份"}` : "讀取中";
    $("backupCreate").disabled = busy || working || !loaded || !statusLoaded;
    $("backupTrash").setAttribute("aria-pressed", String(trashMode));
    $("backupTrash").lastElementChild.textContent = trashMode ? "返回備份" : `回收桶${trash.length ? " · " + trash.length : ""}`;
    if (!loaded) return;
    list.replaceChildren();
    if (!current.length) {
      const empty = ui.element("div", "empty-state");
      empty.innerHTML = ui.icon(trashMode ? "trash" : "download");
      empty.append(ui.element("h2", "", trashMode ? "回收桶是空的" : "尚無備份"));
      if (!trashMode) empty.append(ui.element("p", "", "按「立即備份」保存目前資料與設定。"));
      list.append(empty);
    }
    for (const item of current) {
      const row = ui.element("article", "backup-row");
      const copy = ui.element("div", "backup-copy");
      copy.append(ui.element("h2", "", date(item.created_at || item.deleted_at)));
      const labels = [item.size ? ui.size(item.size) : "容量尚未統計"];
      if (trashMode) labels.push("已移到回收桶");
      else {
        if (item.latest) labels.push("最近成功");
        labels.push(item.complete ? item.verified_at ? "已校驗 " + date(item.verified_at) : "尚未深度校驗" : "未完成");
        if (item.skipped_active_recordings?.length) labels.push(`略過 ${item.skipped_active_recordings.length} 個錄影中的片段`);
      }
      copy.append(ui.element("p", "muted", labels.join(" · ")));
      const controls = ui.element("div", "backup-actions");
      if (trashMode) controls.append(makeButton("復原", "restore", item));
      else {
        if (item.complete) controls.append(makeButton("驗證", "verify", item), makeButton("隔離還原", "recover", item));
        const remove = makeButton("移到回收桶", "trash", item, "quiet");
        if (item.latest) { remove.disabled = true; remove.title = "最近成功的備份受保護，請先建立新備份"; }
        controls.append(remove);
      }
      row.append(copy, controls);
      list.append(row);
    }
  }
  function renderJob(job) {
    $("backupJob").hidden = !job;
    if (!job) return;
    $("backupJobTitle").textContent = actions[job.action] || "備份工作";
    $("backupJobState").textContent = states[job.state] || job.state;
    $("backupJobMessage").textContent = job.message + (job.total_bytes ? ` · ${ui.size(job.done_bytes)} / ${ui.size(job.total_bytes)}` : "");
    const progress = $("backupProgress");
    progress.hidden = !["queued", "running"].includes(job.state);
    if (job.total_bytes) { progress.max = job.total_bytes; progress.value = job.done_bytes; }
    else progress.removeAttribute("value");
    $("backupJobError").hidden = !job.error;
    $("backupJobError").textContent = job.error || "";
    const result = job.result;
    const messages = [];
    if (result?.live_data_changed === false) messages.push("隔離還原完成，正式資料未變更。位置：" + result.path);
    if (result?.legacy_baseline_created) messages.push("舊版備份已建立本次校驗基準，後續可比對檔案雜湊。");
    $("backupJobResult").hidden = !messages.length;
    $("backupJobResult").textContent = messages.join(" ");
  }
  async function load() {
    const generation = ++loadGeneration;
    $("backupList").setAttribute("aria-busy", "true");
    try {
      const data = await api("/backups");
      if (generation !== loadGeneration) return;
      rows = data.items; trash = data.trash;
      loaded = true;
      $("backupUsage").textContent = `實際占用 ${ui.size(data.capacity.physical_bytes)} · 含共用檔案的資料量 ${ui.size(data.capacity.logical_bytes)}`;
      render();
    } catch (error) { if (generation === loadGeneration) $("backupList").replaceChildren(ui.element("div", "empty-state", error.message)); }
    finally { if (generation === loadGeneration) $("backupList").setAttribute("aria-busy", "false"); }
  }
  async function status() {
    if (updating || document.hidden) return;
    updating = true;
    try {
      const data = await api("/status");
      const firstStatus = !statusLoaded;
      statusLoaded = true;
      $("backupRisk").hidden = !data.same_device;
      $("backupFree").textContent = "可用空間 " + ui.size(data.free_bytes);
      const latest = data.jobs[0] || null;
      const changed = latest?.id !== lastJob?.id || latest?.state !== lastJob?.state;
      const completed = latest && ["complete", "failed", "interrupted"].includes(latest.state) && (!lastJob || latest.id !== lastJob.id || latest.state !== lastJob.state);
      lastJob = latest;
      renderJob(lastJob);
      if (changed || firstStatus) render();
      if (completed) await load();
    } catch (error) { $("backupFree").textContent = "無法連接備份服務"; }
    finally { updating = false; }
  }
  $("backupCreate").addEventListener("click", () => perform("create"));
  $("backupTrash").addEventListener("click", () => { trashMode = !trashMode; render(); });
  $("backupRefresh").addEventListener("click", () => { load(); status(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) status(); });
  window.addEventListener("pageshow", event => { if (event.persisted) { clearInterval(timer); timer = setInterval(status, 2000); load(); status(); } });
  window.addEventListener("pagehide", () => clearInterval(timer));
  load(); status(); timer = setInterval(status, 2000);
})();
