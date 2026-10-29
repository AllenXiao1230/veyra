"use strict";
// One request, one decoded frame and at most two live object URLs.
class CameraFrames {
  constructor(options = {}) { this.io = options; this.generation = 0; this.urls = new Set(); }
  stop() {
    this.generation++;
    clearTimeout(this.timer); clearTimeout(this.deadline);
    this.controller?.abort();
    if (this.image) this.image.removeAttribute("src");
    for (const url of this.urls) (this.io.URL || URL).revokeObjectURL(url);
    this.urls.clear(); this.current = null; this.etag = null;
  }
  start(image, frame, error, retry = () => true) {
    this.stop(); this.image = image;
    const generation = this.generation;
    let failures = 0;
    const tick = async () => {
      if (generation !== this.generation) return;
      const controller = new AbortController();
      this.controller = controller;
      const deadline = setTimeout(() => controller.abort(), 5000);
      this.deadline = deadline;
      let next;
      try {
        const response = await (this.io.fetch || fetch)("/camera/frame.jpg", {
          cache: "no-store", signal: controller.signal,
          headers: this.etag ? {"If-None-Match": this.etag} : {}});
        if (generation !== this.generation) return;
        if (response.status !== 304) {
          if (!response.ok || !response.headers.get("Content-Type")?.startsWith("image/jpeg")) throw new Error("影格暫時無法讀取");
          const blob = await response.blob();
          if (blob.size > 512 * 1024) throw new Error("影格超過預覽大小上限");
          if (generation !== this.generation) return;
          next = (this.io.URL || URL).createObjectURL(blob); this.urls.add(next);
          image.src = next;
          await image.decode();
          if (generation !== this.generation) return;
          const previous = this.current; this.current = next;
          if (previous) { (this.io.URL || URL).revokeObjectURL(previous); this.urls.delete(previous); }
          this.etag = response.headers.get("ETag");
          frame();
        }
        failures = 0;
      } catch (failure) {
        if (next && next !== this.current) { (this.io.URL || URL).revokeObjectURL(next); this.urls.delete(next); }
        if (generation !== this.generation) return;
        failures++;
        error(failure);
        if (!retry()) { this.stop(); return; }
      } finally {
        clearTimeout(deadline);
        if (generation === this.generation) this.timer = setTimeout(tick, failures ? Math.min(8000, 500 * 2 ** Math.min(failures, 4)) : 500);
      }
    };
    tick();
  }
}
if (typeof module !== "undefined") module.exports = CameraFrames;
else window.CameraFrames = CameraFrames;
