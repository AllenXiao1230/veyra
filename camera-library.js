"use strict";
(() => {
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

  const preview = ui.element("dialog", "preview-dialog");
  preview.setAttribute("aria-labelledby", "previewTitle");
  preview.innerHTML = `<div class="dialog-heading"><h2 id="previewTitle">${noun}</h2><button id="closePreview" class="icon-button quiet" aria-label="關閉預覽" type="button">${ui.icon("close")}</button></div><div class="preview-content" id="previewContent"></div><div class="dialog-footer"><span id="previewDetails" class="muted"></span><a class="button" id="previewDownload" download>${ui.icon("download")}下載原檔</a></div>`;
  document.body.append(preview);
  ui.backdrop(preview);
  $("closePreview").addEventListener("click", () => preview.close());
  preview.addEventListener("close", () => { $("previewContent").replaceChildren(); opened = null; });

  function openMedia(item) {
    if (selectionMode) { toggleSelection(item); return; }
    opened = item;
    $("previewTitle").textContent = ui.day(item.date) + " " + ui.time(item.created_at);
    $("previewDownload").href = item.url;
    $("previewDownload").download = item.filename;
    $("previewDetails").textContent = isRecording && !item.playable ? item.health_message : ui.size(item.size);
    const media = document.createElement(isRecording && item.playable ? "video" : "img");
    media.src = isRecording && !item.playable ? item.thumbnail_url : item.url;
    if (media.tagName === "VIDEO") {
      media.controls = true;
      media.playsInline = true;
      media.preload = "metadata";
      media.poster = item.thumbnail_url;
      media.addEventListener("error", () => { $("previewDetails").textContent = "此瀏覽器無法播放，請下載原檔。"; });
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
  $("cancelDelete").addEventListener("click", () => confirmation.close());
  function confirmDelete(entries) {
    pendingDelete = entries.filter(i => !i.active);
    if (!pendingDelete.length) return;
    $("deleteDescription").textContent = `將 ${pendingDelete.length} ${unit}${noun}移到回收桶，之後可以復原。`;
    confirmation.showModal();
    $("cancelDelete").focus();
  }
  $("confirmDelete").addEventListener("click", async () => {
    const receipts = [];
    const failed = [];
    $("confirmDelete").disabled = true;
    $("cancelDelete").disabled = true;
    try {
      for (const item of pendingDelete) {
        try { receipts.push(await ui.api(base + "/trash", {filename: item.filename})); }
        catch (error) { failed.push(error.message); }
      }
      confirmation.close();
      selected.clear();
      selectionMode = false;
      await load(true);
      if (receipts.length) {
        ui.toast(`已移到回收桶：${receipts.length} ${unit}${noun}${failed.length ? `；${failed.length} 個未完成` : ""}`, async () => {
          for (const receipt of receipts) await ui.api(base + "/restore", {token: receipt.token});
          ui.toast("已復原");
          await load(true);
        });
      } else if (failed.length) ui.toast(failed[0]);
    } finally {
      $("confirmDelete").disabled = false;
      $("cancelDelete").disabled = false;
    }
  });

  function updateSelection() {
    $("selectionBar").hidden = !selectionMode;
    $("selectToggle").textContent = selectionMode ? "取消選取" : "選取";
    $("selectionCount").textContent = `已選取 ${selected.size} ${unit}`;
    $("deleteSelected").disabled = !selected.size;
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
    if (selected.has(item.filename)) selected.delete(item.filename);
    else selected.add(item.filename);
    updateSelection();
  }
  function duration(seconds) {
    const whole = Math.max(0, Math.floor(seconds || 0));
    return Math.floor(whole / 60) + ":" + String(whole % 60).padStart(2, "0");
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
      image.src = item.thumbnail_url;
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
    $("selectToggle").hidden = trashMode || !items.length;
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
        const row = ui.element("article", "trash-row");
        row.innerHTML = ui.icon(isRecording ? "video" : "image");
        const copy = ui.element("div", "trash-copy");
        copy.append(ui.element("strong", "", item.filename), ui.element("p", "", `刪除於 ${new Intl.DateTimeFormat("zh-TW", {timeZone:"Asia/Taipei",dateStyle:"short",timeStyle:"short"}).format(new Date(item.deleted_at))} · ${ui.size(item.size)}`));
        const restore = ui.element("button", "", "復原");
        restore.type = "button";
        restore.addEventListener("click", async () => {
          restore.disabled = true;
          try { await ui.api(base + "/restore", {token: item.token}); ui.toast("已復原"); await load(true); }
          catch (error) { restore.disabled = false; ui.toast(error.message); }
        });
        row.append(copy, restore);
        content.append(row);
      }
    } else {
      appendGroups(items);
    }
    }
    $("loadedCount").textContent = trashMode ? "" : `已載入 ${items.length.toLocaleString()} / ${total.toLocaleString()} ${unit}`;
    $("loadMore").hidden = trashMode || !next;
    updateSelection();
  }
  async function load(reset = false) {
    if (loading && !reset) return;
    const currentGeneration = reset ? ++generation : generation;
    loading = true;
    content.setAttribute("aria-busy", "true");
    $("loadMore").disabled = true;
    if (reset) { next = null; selected.clear(); }
    let additions = null;
    try {
      if (trashMode) {
        const result = await ui.api(base + "/trash");
        if (currentGeneration !== generation) return;
        items = result.items;
        total = trashCount = items.length;
      } else {
        const params = new URLSearchParams({limit: isRecording ? "24" : "48"});
        if ($("mediaDate").value) params.set("date", $("mediaDate").value);
        if (!reset && next) params.set("before", next);
        const result = await ui.api(base + "/library?" + params, undefined, 30000);
        if (currentGeneration !== generation) return;
        additions = reset ? null : result.items.filter(i => !items.some(old => old.filename === i.filename));
        items = reset ? result.items : [...items, ...additions];
        dates = result.dates;
        total = result.filtered_total;
        trashCount = result.trash_count;
        next = result.next_before;
      }
      render(additions);
    } catch (error) {
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
  $("selectLoaded").addEventListener("click", () => { items.filter(i => !i.active).forEach(i => selected.add(i.filename)); updateSelection(); });
  $("deleteSelected").addEventListener("click", () => confirmDelete(items.filter(i => selected.has(i.filename))));
  preview.addEventListener("keydown", event => {
    if (!opened || isRecording || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const index = items.indexOf(opened) + (event.key === "ArrowRight" ? 1 : -1);
    if (items[index]) { event.preventDefault(); openMedia(items[index]); }
  });
  load(true);
})();
