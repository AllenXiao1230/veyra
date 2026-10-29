"use strict";
(() => {
  const el = id => document.getElementById(id);
  const select = el("audioSource");
  const volume = el("audioVolume");
  const muted = el("audioMuted");
  const status = el("audioServiceStatus");
  const message = el("audioActionStatus");
  const apply = el("audioApply");
  const restart = el("audioRestart");
  const refresh = el("audioRefresh");
  if (!select) return;
  let dirty = false;
  let busy = false;
  let reading = false;
  let initialized = false;
  let healthyBefore = false;
  let publisherPid = null;
  let reloadWhenReady = false;

  async function request(path, body) {
    const response = await fetch("/audio-api" + path, {
      cache: "no-store", signal: AbortSignal.timeout(8000),
      ...(body === undefined ? {} : {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
      })
    });
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error("音訊服務暫時無法回應"); }
    if (!response.ok) throw new Error(data.error || "音訊請求失敗");
    return data;
  }

  function render(data) {
    const desired = dirty ? select.value : data.selected_source;
    const choices = data.sources || [];
    const options = choices.map(source => {
      const option = document.createElement("option");
      option.value = source.name;
      option.textContent = source.description + (source.available ? "" : "（未插入）");
      return option;
    });
    if (desired && !choices.some(s => s.name === desired)) {
      const option = document.createElement("option");
      option.value = desired;
      option.textContent = desired + "（已離線，等待重新連接）";
      options.push(option);
    }
    const unchanged = select.options.length === options.length &&
      options.every((option, i) => select.options[i].value === option.value && select.options[i].textContent === option.textContent);
    if (!unchanged) select.replaceChildren(...options);
    if (desired) select.value = desired;
    if (!dirty) {
      volume.value = data.volume;
      muted.checked = data.muted;
      el("audioVolumeValue").textContent = volume.value + "%";
    }
    select.disabled = busy || !options.length;
    apply.disabled = busy || !choices.some(s => s.name === select.value);
    restart.disabled = busy || !data.selected_source;
    const selected = choices.find(s => s.name === data.selected_source);
    const title = selected?.description || data.selected_source || "尚未選定麥克風";
    const actualVolume = data.actual_volume ?? data.volume;
    const actualMuted = data.actual_muted ?? data.muted;
    const states = {starting: "正在連接", restarting: "正在重啟", retrying: "等待自動重連", running: "音訊資料已過期"};
    status.textContent = data.healthy ?
      `音訊串流正常 · ${title} · ${actualMuted ? "已靜音" : `音量 ${actualVolume}%`}` :
      `${states[data.state] || "音訊尚未就緒"} · ${title}${data.last_error ? " · " + data.last_error : ""}` +
      (data.retry_in_seconds ? ` · 約 ${data.retry_in_seconds} 秒後重試` : "");
    if (data.healthy && initialized &&
        (reloadWhenReady || !healthyBefore || publisherPid !== data.pid)) {
      el("micFrame").src = "/mic/?reconnect=" + Date.now();
      if (reloadWhenReady) message.textContent = "音訊已恢復，請按「開啟監聽聲音」開始監聽。";
      reloadWhenReady = false;
    }
    initialized = true;
    healthyBefore = data.healthy;
    publisherPid = data.pid;
  }

  async function update() {
    if (reading || document.hidden) return;
    reading = true;
    try { render(await request("/status")); }
    catch (error) {
      status.textContent = "無法取得音訊狀態：" + error.message;
      apply.disabled = restart.disabled = true;
    } finally { reading = false; }
  }

  async function action(path, body) {
    if (busy) return;
    busy = true;
    apply.disabled = restart.disabled = refresh.disabled = select.disabled = true;
    message.textContent = "正在送出音訊設定…";
    try {
      const result = await request(path, body);
      dirty = false;
      reloadWhenReady = true;
      message.textContent = result.message;
    } catch (error) { message.textContent = error.message; }
    finally {
      busy = false;
      refresh.disabled = false;
      await update();
    }
  }

  select.addEventListener("change", () => { dirty = true; apply.disabled = busy || !select.value; });
  volume.addEventListener("input", () => {
    dirty = true;
    el("audioVolumeValue").textContent = volume.value + "%";
  });
  muted.addEventListener("change", () => { dirty = true; });
  apply.addEventListener("click", () => action("/source", {
    source: select.value, volume: Number(volume.value), muted: muted.checked
  }));
  restart.addEventListener("click", () => action("/restart", {}));
  refresh.addEventListener("click", update);
  el("audioListen").addEventListener("click", async () => {
    try {
      const video = el("micFrame").contentDocument?.querySelector("video");
      if (!video) throw new Error("播放器尚未載入，請稍後再試");
      video.muted = false;
      await video.play();
      message.textContent = "已開啟監聽聲音；請確認手機或電腦的媒體音量。";
    } catch (error) { message.textContent = "無法開始播放：" + error.message; }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) update(); });
  update();
  const timer = setInterval(update, 3000);
  window.addEventListener("pagehide", event => { if (!event.persisted) clearInterval(timer); });
})();
