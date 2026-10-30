"""Bounded fast-start playback copies; original fragmented recordings stay intact."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid

CACHE = Path('/srv/camera-monitor/cache/playback')
MAX_BYTES = 2 * 1024 ** 3
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(3)


def probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                             'format=duration:stream=codec_type', '-of', 'json', str(path)],
                            check=True, capture_output=True, text=True, timeout=15)
    return json.loads(result.stdout)


def playback_file(source):
    source = Path(source)
    stat = source.stat()
    key = hashlib.sha256(repr((str(source), stat.st_mtime_ns, stat.st_size)).encode()).hexdigest()
    target = CACHE / (key + '.mp4')
    if target.is_file():
        return target
    if not _slots.acquire(blocking=False):
        raise RuntimeError('影片正在準備中，請稍後重新開啟。')
    try:
        with _lock:
            if target.is_file():
                return target
            CACHE.mkdir(parents=True, exist_ok=True)
            entries = sorted(CACHE.glob('*.mp4'), key=lambda p: p.stat().st_mtime)
            occupied = sum(p.stat().st_size for p in entries)
            while entries and occupied + stat.st_size > MAX_BYTES:
                old = entries.pop(0)
                occupied -= old.stat().st_size
                old.unlink()  # Generated cache only, never an original recording.
            if stat.st_size > MAX_BYTES or shutil.disk_usage(CACHE).free < stat.st_size + 1024 ** 3:
                raise RuntimeError('播放暫存空間不足，可下載原檔播放。')
            temporary = CACHE / (key + '-' + uuid.uuid4().hex + '.tmp.mp4')
            try:
                # Complete duration and sample tables precede media data.
                subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-i', str(source),
                                '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
                                '-movflags', '+faststart', str(temporary)],
                               check=True, capture_output=True, timeout=90)
                before, after = probe(source), probe(temporary)
                if (sorted(s['codec_type'] for s in before['streams']) != sorted(s['codec_type'] for s in after['streams'])
                        or abs(float(before['format']['duration']) - float(after['format']['duration'])) > 0.15):
                    raise RuntimeError('影片時間軸驗證失敗，請下載原檔。')
                current = source.stat()
                if (current.st_mtime_ns, current.st_size) != (stat.st_mtime_ns, stat.st_size):
                    raise RuntimeError('影片仍在更新，請完成錄影後重試。')
                actual_size = temporary.stat().st_size
                if actual_size > MAX_BYTES:
                    raise RuntimeError('影片超過播放暫存上限，請下載原檔。')
                while entries and occupied + actual_size > MAX_BYTES:
                    old = entries.pop(0)
                    occupied -= old.stat().st_size
                    old.unlink()
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return target
    finally:
        _slots.release()
