"""Read-only media inspection with a bounded, stat-keyed cache."""
import json
import os
import subprocess
import threading
import hashlib
from pathlib import Path
from camera_state import atomic_json

_cache = {}
_lock = threading.Lock()


def inspect_recording(path, active=False):
    if active:
        return {"health": "recording", "playable": False, "health_message": "正在錄製，完成後可回放"}
    stat = os.stat(path)
    key = (path, stat.st_mtime_ns, stat.st_size)
    disk_key = hashlib.sha256(str(key).encode()).hexdigest()
    saved = Path(path).parent / ".metadata" / (disk_key + ".json")
    with _lock:
        if key in _cache:
            return dict(_cache[key])
    try:
        state = json.loads(saved.read_text())
        if state.get("health") in {"ok", "damaged"}:
            with _lock:
                _cache[key] = state
            return dict(state)
    except (OSError, ValueError):
        pass
    try:
        result = subprocess.run(
            ["/usr/bin/ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_name,codec_type", "-of", "json", path],
            capture_output=True, text=True, timeout=8,
        )
        metadata = json.loads(result.stdout or "{}")
        if result.returncode or not metadata.get("streams"):
            state = {"health": "damaged", "playable": False,
                     "health_message": "檔案損壞或未完成封裝；原檔已保留"}
        else:
            mp4 = path.lower().endswith(".mp4")
            state = {"health": "ok", "playable": mp4,
                     "duration_seconds": float(metadata.get("format", {}).get("duration", 0)),
                     "health_message": "可回放" if mp4 else "MKV 舊格式，請下載播放"}
    except Exception:
        state = {"health": "unknown", "playable": False, "health_message": "暫時無法驗證，請稍後重試"}
    with _lock:
        if len(_cache) > 1000:
            _cache.clear()
        if state["health"] != "unknown":
            _cache[key] = state
            try:
                atomic_json(saved, state)
            except OSError:
                pass
    return dict(state)
