"use strict";
(() => {
  if (document.body.dataset.page === 'snapshots' && new URLSearchParams(location.search).get('view') === 'events') return;
  const ui = window.CameraUI;
  const $ = id => document.getElementById(id);
  const isRecording = document.body.dataset.page === "recordings";
  const noun = isRecording ? "錄影" : "快照";
  const unit = isRecording ? "部" : "張";
  const base = isRecording ? "/recordings-api" : "/ai-api";
  const content = $("libraryContent");
  let items = [];
  let dates = [];
  let selected = new Set();
  let selectionMode = false;
  let trashMode = false;
  let next = null;
  let total = 0;
  let trashCount = 0;
  let loading = false;
  let generation = 0;
  let opened = null;
  let cursors = [], cursor = null, request, resumeList = false;
  const previousPage = ui.element('button', '', '上一頁');
  previousPage.type = 'button'; previousPage.hidden = true;
  $('loadMore').before(previousPage);
  previousPage.addEventListener('click', () => load(false, true));
  const restoreSelected = ui.element('button', '', '復原選取');
  restoreSelected.type = 'button'; restoreSelected.hidden = true;
  $('deleteSelected').before(restoreSelected);
  restoreSelected.addEventListener('click', async () => {
    restoreSelected.disabled = true;
    try {
      const result = await ui.api(base + '/batch', {action:'restore',items:[...selected]},60000);
      ui.toast(`已復原 ${result.succeeded} 個${result.failed ? `；${result.failed} 個未完成` : ''}`);
      selectionMode = false; await load(true);
    } catch(error) {ui.toast(error.message);} finally {restoreSelected.disabled = false;}
  });

  const preview = ui.element("dialog", "preview-dialog");
  preview.setAttribute("aria-labelledby", "previewTitle");
  preview.innerHTML = `<div class="dialog-heading"><h2 id="previewTitle">${noun}</h2><button id="closePreview" class="icon-button quiet" aria-label="關閉預覽" type="button">${ui.icon("close")}</button></div><div class="preview-content" id="previewContent"></div><div class="dialog-footer"><span id="previewDetails" class="muted"></span><a class="button" id="previewDownload" download>${ui.icon("download")}下載原檔</a></div>`;
  document.body.append(preview);
  ui.backdrop(preview);
  $("closePreview").addEventListener("click", () => preview.close());
  function releasePreview() {
    const video = $('previewContent').querySelector('video');
    if (video) { video.pause(); video.removeAttribute('src'); video.load(); }
    $('previewContent').replaceChildren(); opened = null;
  }
  preview.addEventListener("close", releasePreview);

  function openMedia(item) {
    if (selectionMode) { toggleSelection(item); return; }
    releasePreview(); opened = item;
    $("previewTitle").textContent = ui.day(item.date) + " " + ui.time(item.created_at);
    $("previewDownload").href = item.url;
    $("previewDownload").download = item.filename;
    $("previewDetails").textContent = isRecording ? (item.playable ? duration(item.duration_seconds) + " · " + ui.size(item.size) : item.health_message) : ui.size(item.size);
    const media = document.createElement(isRecording && item.playable ? "video" : "img");
    media.src = isRecording ? (item.playable ? item.playback_url || item.url : item.thumbnail_url) : item.url;
    if (media.tagName === "VIDEO") {
      media.controls = true;
      media.playsInline = true;
      media.preload = "metadata";
      media.poster = item.thumbnail_url;
      media.addEventListener("error", () => { $("previewDetails").textContent = "播放暫時無法開始，請重新開啟或下載原檔。"; });
    }
    else media.alt = noun + " " + ui.time(item.created_at);
    $("previewContent").replaceChildren(media);
    if (!preview.open) preview.showModal();
    if (media.tagName === "VIDEO") media.play().catch(() => {});
  }

  const confirmation = ui.element("dialog", "confirm-dialog");
  confirmation.setAttribute("aria-labelledby", "deleteTitle");
  confirmation.innerHTML = `<div class="dialog-heading"><h2 id="deleteTitle">移到回收桶？</h2></div><p class="dialog-body" id="deleteDescription"></p><div class="dialog-footer"><button id="cancelDelete" type="button">取消</button><button id="confirmDelete" class="danger" type="button">移到回收桶</button></div>`;
  document.body.append(confirmation);
  ui.backdrop(confirmation);
  let pendingDelete = [];
  let deleting = false;
  let permanent = false;
  const purgeInput = ui.element('input');
  purgeInput.type='text';
  const deleteError = ui.element('p','dialog-error'); deleteError.setAttribute('role','alert');deleteError.hidden=true;
  purgeInput.placeholder = '輸入「永久刪除」'; purgeInput.setAttribute('aria-label', '輸入永久刪除以確認');
  purgeInput.autocomplete = 'off';
  $('deleteDescription').after(purgeInput);
  purgeInput.after(deleteError);
  purgeInput.addEventListener('input', () => { $('confirmDelete').disabled = permanent && purgeInput.value !== '永久刪除'; });
  confirmation.addEventListener("cancel", event => { if (deleting) event.preventDefault(); });
  $("cancelDelete").addEventListener("click", () => confirmation.close());
  function confirmDelete(entries) {
    pendingDelete = entries.filter(i => !i.active);
    if (!pendingDelete.length) return;
    permanent = trashMode;
    deleteError.hidden=true;deleteError.textContent='';
    $('deleteTitle').textContent = permanent ? '永久刪除？' : '移到回收桶？';
    purgeInput.hidden = !permanent; purgeInput.value = '';
    $('confirmDelete').disabled = permanent;
    $('confirmDelete').textContent = permanent ? '永久刪除' : '移到回收桶';
    $("deleteDescription").textContent = permanent ? `永久刪除 ${pendingDelete.length} ${unit}${noun}，無法復原。備份與先前匯出副本不受影響。` : `將 ${pendingDelete.length} ${unit}${noun}移到回收桶，之後可以復原。`;
    confirmation.showModal();
    $("cancelDelete").focus();
  }
  $("confirmDelete").addEventListener("click", async () => {
    if (deleting || (permanent && purgeInput.value !== '永久刪除')) return;
    deleting = true;
    const receipts = [];
    const failed = [];
    $("confirmDelete").disabled = true;
    $("cancelDelete").disabled = true;
    try {
      const result = await ui.api(base + '/batch', {action: permanent ? 'purge' : 'trash',
        items: pendingDelete.map(i => permanent ? i.token : i.filename), confirmation: permanent ? purgeInput.value : undefined}, 60000);
      for (const row of result.results) { if (row.ok) receipts.push(row.result); else failed.push(row.error); }
      confirmation.close();
      selected.clear();
      selectionMode = false;
      await load(true);
      if (permanent) { ui.toast(`已永久刪除 ${receipts.length} ${unit}${noun}${failed.length ? `；${failed.length} 個未完成` : ''}`); }
      else if (receipts.length) {
        let remaining = [...receipts];
        const undo = async () => {
          const failedReceipts = [];
          for (const receipt of remaining) {
            try { await ui.api(base + "/restore", {token: receipt.token}); }
            catch (_) { failedReceipts.push(receipt); }
          }
          remaining = failedReceipts;
          if (remaining.length) ui.toast(`${remaining.length} 個尚未復原，可重試或到回收桶復原。`, undo, "重試復原");
          else ui.toast("已復原");
          await load(true);
        };
        ui.toast(`已移到回收桶：${receipts.length} ${unit}${noun}${failed.length ? `；${failed.length} 個未完成，可重新選取重試` : ""}`, undo);
      } else if (failed.length) ui.toast(failed[0]);
    } catch (error) { deleteError.hidden=false;deleteError.textContent=error.message+' 若回應逾時，請關閉後重新整理確認結果。'; } finally {
      deleting = false;
      $("confirmDelete").textContent = permanent ? '永久刪除' : "移到回收桶";
      $("confirmDelete").disabled = permanent && purgeInput.value !== '永久刪除';
      $("cancelDelete").disabled = false;
    }
  });
  confirmation.addEventListener("click", event => { if (deleting && event.target === confirmation) event.stopImmediatePropagation(); }, true);

  function updateSelection() {
    $("selectionBar").hidden = !selectionMode;
    $("selectToggle").textContent = selectionMode ? "取消選取" : "批量管理";
    $("selectionCount").textContent = `已選取 ${selected.size} ${unit}`;
    $("deleteSelected").disabled = !selected.size;
    $('deleteSelected').textContent = trashMode ? '永久刪除' : '移到回收桶';
    restoreSelected.hidden = !trashMode;
    restoreSelected.disabled = !selected.size;
    content.classList.toggle("select-mode", selectionMode);
    content.querySelectorAll(".media-item").forEach(card => {
      const checked = selected.has(card.dataset.name);
      card.classList.toggle("selected", checked);
      const box = card.querySelector('input[type="checkbox"]');
      if (box) box.checked = checked;
    });
  }
  function toggleSelection(item) {
    if (item.active) return;
    const key = item.token || item.filename;
    if (selected.has(key)) selected.delete(key);
    else selected.add(key);
    updateSelection();
  }
  function duration(seconds) {
    const whole = Math.max(0, Math.floor(seconds || 0));
    return (whole >= 3600 ? Math.floor(whole / 3600) + ":" + String(Math.floor(whole / 60) % 60).padStart(2, "0") : Math.floor(whole / 60)) + ":" + String(whole % 60).padStart(2, "0");
  }
  function card(item) {
    const node = ui.element("article", "media-item");
    node.dataset.name = item.filename;
    const checkboxLabel = ui.element("label", "media-selection");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.disabled = !!item.active;
    checkbox.setAttribute("aria-label", `選取${noun} ${ui.day(item.date)} ${ui.time(item.created_at)}`);
    checkbox.addEventListener("change", () => toggleSelection(item));
    checkboxLabel.append(checkbox);
    const imageButton = ui.element("button", "media-image");
    imageButton.type = "button";
    imageButton.setAttribute("aria-label", `查看${noun} ${ui.day(item.date)} ${ui.time(item.created_at)}`);
    imageButton.addEventListener("click", () => openMedia(item));
    const placeholder = ui.element("span", "media-placeholder");
    placeholder.innerHTML = ui.icon(isRecording ? "video" : "image");
    const damaged = isRecording && !["ok", "recording"].includes(item.health);
    if (!item.active && !damaged) {
      const image = document.createElement("img");
      image.src = isRecording ? item.thumbnail_url : '/ai-api/thumbnail/' + encodeURIComponent(item.filename);
      image.alt = noun + " " + ui.time(item.created_at);
      image.loading = "lazy";
      image.decoding = "async";
      image.addEventListener("error", () => { image.classList.add("failed"); placeholder.append(ui.element("span", "", "無法預覽")); }, {once: true});
      imageButton.append(image);
    } else {
      placeholder.append(ui.element("span", "", item.active ? "錄影中" : "無法預覽"));
      imageButton.disabled = true;
    }
    imageButton.append(placeholder);
    if (isRecording && item.duration_seconds) imageButton.append(ui.element("span", "duration", duration(item.duration_seconds)));
    if (isRecording && item.filename.endsWith(".mkv")) imageButton.append(ui.element("span", "media-badge", "MKV"));
    if (item.active) imageButton.append(ui.element("span", "media-badge", "錄影中"));
    const meta = ui.element("div", "media-meta");
    const details = ui.element("div");
    const time = ui.element("time", "media-time", ui.time(item.created_at));
    time.dateTime = item.created_at;
    details.append(time);
    if (isRecording) details.append(ui.element("p", "media-size", ui.size(item.size)));
    if (isRecording && item.trigger) details.append(ui.element("p", "media-size", item.trigger === "motion" ? "動作觸發" : "手動錄影"));
    if (!isRecording && item.ai_ready !== undefined && item.ai_ready !== null) details.append(ui.element("p", "media-size", item.person_detected ? "AI 偵測到人" : item.ai_ready ? "動作" : "動作 · AI 暫停"));
    const actions = ui.element("div", "media-actions");
    const download = ui.element("a", "button icon-button");
    download.href = item.url;
    download.download = item.filename;
    download.setAttribute("aria-label", `下載${noun} ${ui.time(item.created_at)}`);
    download.title = "下載原檔";
    download.innerHTML = ui.icon("download");
    const remove = ui.button(`刪除${noun} ${ui.time(item.created_at)}`, "trash", "icon-button delete");
    remove.disabled = !!item.active;
    if (item.active) remove.title = "錄影完成後可刪除";
    remove.addEventListener("click", () => confirmDelete([item]));
    actions.append(download, remove);
    meta.append(details, actions);
    node.append(checkboxLabel, imageButton, meta);
    if (damaged) {
      const warning = ui.element("p", "media-size", item.health_message || "無法回放");
      node.append(warning);
    }
    return node;
  }
  function appendGroups(entries) {
    for (const item of entries) {
      let section = content.querySelector(`.day-section[data-date="${item.date}"]`);
      if (!section) {
        section = ui.element("section", "day-section");
        section.dataset.date = item.date;
        const heading = ui.element("h2", "day-heading");
        const time = ui.element("time", "", ui.day(item.date));
        time.dateTime = item.date;
        heading.append(time, ui.element("span"));
        section.append(heading, ui.element("div", "media-grid"));
        content.append(section);
      }
      section.querySelector(".media-grid").append(card(item));
    }
    for (const section of content.querySelectorAll(".day-section")) {
      section.querySelector(".day-heading span").textContent = `${dates.find(d => d.date === section.dataset.date)?.count || section.querySelectorAll(".media-item").length} ${unit}`;
    }
  }
  function render(additions = null) {
    $("libraryTitle").textContent = trashMode ? noun + "回收桶" : noun;
    $("libraryCount").textContent = `${total.toLocaleString()} ${unit}`;
    $("trashToggle").innerHTML = ui.icon(trashMode ? "restore" : "trash") + `<span>${trashMode ? "返回" + noun : "回收桶" + (trashCount ? ` (${trashCount})` : "")}</span>`;
    $("mediaDate").disabled = trashMode;
    $("selectToggle").hidden = !items.length;
    const date = $("mediaDate").value;
    const options = [ui.element("option", "", "全部日期"), ...dates.map(day => {
      const option = ui.element("option", "", day.date.replaceAll("-", "/") + ` · ${day.count} ${unit}`);
      option.value = day.date;
      return option;
    })];
    options[0].value = "";
    if (date && !dates.some(day => day.date === date)) {
      const emptyDate = ui.element("option", "", date.replaceAll("-", "/") + ` · 0 ${unit}`);
      emptyDate.value = date;
      options.push(emptyDate);
    }
    $("mediaDate").replaceChildren(...options);
    $("mediaDate").value = date;
    if (additions && !trashMode && content.querySelector(".day-section")) {
      appendGroups(additions);
    } else {
    content.replaceChildren();
    if (!items.length) {
      const empty = ui.element("div", "empty-state");
      empty.innerHTML = ui.icon(trashMode ? "trash" : isRecording ? "video" : "image");
      empty.append(ui.element("h2", "", trashMode ? "回收桶是空的" : "此日期沒有" + noun));
      if (!trashMode) empty.append(ui.element("p", "", date ? "試試其他日期或全部日期。" : isRecording ? "完成錄影後，影片會顯示在這裡。" : "偵測到動作後，快照會顯示在這裡。"));
      content.append(empty);
    } else if (trashMode) {
      for (const item of items) {
        const row = ui.element("article", "trash-row media-item");
        row.dataset.name = item.token;
        row.innerHTML = ui.icon(isRecording ? "video" : "image");
        const choice = ui.element('label', 'media-selection');
        const box = document.createElement('input'); box.type = 'checkbox';
        box.setAttribute('aria-label', '選取 ' + item.filename);
        box.addEventListener('change', () => toggleSelection(item)); choice.append(box); row.append(choice);
        const copy = ui.element("div", "trash-copy");
        copy.append(ui.element("strong", "", item.filename), ui.element("p", "", `刪除於 ${ui.dateTime(item.deleted_at)} · ${ui.size(item.size)}`));
        const restore = ui.element("button", "", "復原");
        restore.type = "button";
        restore.addEventListener("click", async () => {
          restore.disabled = true;
          try { await ui.api(base + "/restore", {token: item.token}); ui.toast("已復原"); await load(true); }
          catch (error) { restore.disabled = false; ui.toast(error.message); }
        });
        const purge = ui.element('button', 'danger quiet', '永久刪除'); purge.type = 'button';
        purge.addEventListener('click', () => confirmDelete([item]));
        const controls = ui.element('div', 'trash-actions'); controls.append(restore, purge);
        row.append(copy, controls);
        content.append(row);
      }
    } else {
      appendGroups(items);
    }
    }
    $("loadedCount").textContent = `本頁 ${items.length.toLocaleString()} / 共 ${total.toLocaleString()} ${unit}`;
    $("loadMore").hidden = !next; $('loadMore').textContent = '下一頁';
    previousPage.hidden = !cursors.length;
    updateSelection();
  }
  async function load(reset = false, previous = false, retry = false) {
    if (loading && !reset && !retry) return;
    request?.abort(); request = new AbortController();
    const currentGeneration = ++generation;
    loading = true;
    content.setAttribute("aria-busy", "true");
    $("loadMore").disabled = true;
    if (reset) { next = null; cursors = []; cursor = null; }
    else if(!retry && previous) cursor = cursors.pop();
    else if(!retry) { cursors.push(cursor); cursor = next; }
    selected.clear(); selectionMode = false;
    let additions = null;
    try {
      if (trashMode) {
        const params = new URLSearchParams({limit:'24'}); if(cursor) params.set('before',cursor);
        const result = await ui.api(base + '/trash?' + params,undefined,30000,request.signal);
        if (currentGeneration !== generation || document.hidden) return;
        items = result.items;
        total = trashCount = result.total; next = result.next_before;
      } else {
        const params = new URLSearchParams({limit: window.matchMedia('(max-width:600px)').matches ? '12' : '24'});
        if ($("mediaDate").value) params.set("date", $("mediaDate").value);
        if (cursor) params.set("before", cursor);
        const result = await ui.api(base + "/library?" + params, undefined, 30000, request.signal);
        if (currentGeneration !== generation || document.hidden) return;
        items = result.items;
        dates = result.dates;
        total = result.filtered_total;
        trashCount = result.trash_count;
        if (result.recovery_issues?.length) ui.toast(`${result.recovery_issues.length} 個回收桶項目需要檢查，檔案已保留。`);
        next = result.next_before;
      }
      render(additions);
      if(!reset&&!retry)content.scrollIntoView({block:'start',behavior:'auto'});
    } catch (error) {
      if(currentGeneration !== generation || document.hidden) return;
      if (!items.length) {
        content.replaceChildren(ui.element("div", "empty-state", error.message));
        $("libraryCount").textContent = "載入失敗";
      }
      ui.toast(error.message);
    } finally {
      if (currentGeneration === generation) {
        loading = false;
        content.setAttribute("aria-busy", "false");
        $("loadMore").disabled = false;
      }
    }
  }
  $("mediaDate").addEventListener("change", () => { selectionMode = false; load(true); });
  $("libraryRefresh").addEventListener("click", () => load(true));
  $("loadMore").addEventListener("click", () => load(false));
  $("trashToggle").addEventListener("click", () => { trashMode = !trashMode; selectionMode = false; items = []; load(true); });
  $("selectToggle").addEventListener("click", () => { selectionMode = !selectionMode; selected.clear(); updateSelection(); });
  $('selectLoaded').textContent = '全選本頁';
  $("selectLoaded").addEventListener("click", () => { items.filter(i => !i.active).forEach(i => selected.add(i.token || i.filename)); updateSelection(); });
  $("deleteSelected").addEventListener("click", () => confirmDelete(items.filter(i => selected.has(i.token || i.filename))));
  preview.addEventListener("keydown", event => {
    if (!opened || isRecording || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const index = items.indexOf(opened) + (event.key === "ArrowRight" ? 1 : -1);
    if (items[index]) { event.preventDefault(); openMedia(items[index]); }
  });
  load(true);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      resumeList=loading;request?.abort(); if(preview.open) preview.close();
      content.querySelectorAll('img').forEach(image=>{image.dataset.resumeSrc=image.getAttribute('src');image.removeAttribute('src');});
    } else {
      content.querySelectorAll('img[data-resume-src]').forEach(image=>{image.src=image.dataset.resumeSrc;delete image.dataset.resumeSrc;});
      if(resumeList||!items.length) {resumeList=false;load(false,false,true);}
    }
  });
  window.addEventListener('pagehide', () => { request?.abort(); if(preview.open) preview.close();releasePreview(); items = []; selected.clear(); content.replaceChildren(); });
  window.addEventListener('pageshow', event => { if(event.persisted) load(true); });
})();
