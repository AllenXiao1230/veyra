#!/usr/bin/env python3
import json
import os
import re
import signal
import subprocess
import threading
import time
import sys
sys.path.insert(0, "/opt/camera-common")
from camera_runtime import MJPEGCapture
from camera_catalog import inspect_recording
from camera_media import MediaLibrary, media_error, send_file
from pathlib import Path
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HOST = "127.0.0.1"
PORT = 8091
VIDEO_DIR = "/var/lib/camera-recorder"
SOURCE = "http://127.0.0.1:8080/stream"
AUDIO_SOURCE = "rtsp://127.0.0.1:8554/mic"
FILENAME_RE = re.compile(r"^recording-\d{8}-\d{6}\.(mp4|mkv)$")
LOG_FILE = "/var/log/camera-recorder-ffmpeg.log"
lock = threading.RLock()
recorder = None
started_at = None
last_error = None
recording_state = "idle"
last_frame_tick = 0.0
motion_error = None
last_start_attempt = 0.0
motion_enabled = True
motion_detected = False
last_motion_at = 0.0
MOTION_STOP_DELAY = 30.0
_media_library = None


def active_media(name):
    proc = recorder
    if proc is None or proc.poll() is not None:
        return False
    try:
        for descriptor in Path(f"/proc/{proc.pid}/fd").iterdir():
            try:
                if os.readlink(descriptor) == os.path.join(VIDEO_DIR, name):
                    return True
            except OSError:
                pass
        # Protect the just-created segment before FFmpeg has opened its output.
        newest = max((p.name for p in Path(VIDEO_DIR).glob("recording-*.mp4")), default=None)
        return name == newest
    except OSError:
        return True


def media_library():
    global _media_library
    with lock:
        if _media_library is None or _media_library.root != Path(VIDEO_DIR):
            _media_library = MediaLibrary(VIDEO_DIR, "recording", active=active_media, inspect=inspect_recording)
        return _media_library


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def is_recording():
    global started_at, last_error, recording_state
    if recorder is None:
        return False
    code = recorder.poll()
    if code is None:
        return recording_state == "recording"
    if not getattr(recorder, "_camera_expected_stop", False):
        recording_state = "failed"
        started_at = None
        last_error = recorder_error(recorder)
    return False


def recorder_error(proc):
    try:
        with open(LOG_FILE, "rb") as handle:
            handle.seek(max(getattr(proc, "_camera_log_start", 0), os.path.getsize(LOG_FILE) - 2048))
            text = handle.read().decode("utf-8", "replace").strip()
    except OSError:
        text = ""
    return f"FFmpeg 錄影失敗（結束代碼 {proc.poll()}）：{text[-1000:]}"


def watch_progress(proc, ready):
    global last_error, recording_state, started_at
    try:
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    if int(line.split("=", 1)[1]) > 0:
                        ready.set()
                except ValueError:
                    pass
        proc.wait()
        with lock:
            if recorder is proc and not getattr(proc, "_camera_expected_stop", False):
                last_error = recorder_error(proc)
                recording_state = "failed"
                started_at = None
    finally:
        proc.stdout.close()
        proc._camera_log.close()


def start_recording():
    global recorder, started_at, last_error, recording_state
    with lock:
        if recorder is not None and recorder.poll() is None:
            return False, "錄影已在進行中"

        os.makedirs(VIDEO_DIR, exist_ok=True)
        # Check that the camera stream is reachable before starting FFmpeg.
        try:
            import urllib.request
            request = urllib.request.Request(
                SOURCE, headers={"User-Agent": "CameraRecorder/1.0"}
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError("攝影機串流回應非 200")
        except Exception as exc:
            last_error = f"無法連接 uStreamer：{exc}"
            recording_state = "failed"
            raise RuntimeError(last_error) from exc

        output = os.path.join(
            VIDEO_DIR, "recording-%Y%m%d-%H%M%S.mp4"
        )
        cmd = [
            "/usr/bin/ffmpeg",
            "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-progress", "pipe:1", "-stats_period", "0.5", "-nostats",
            "-y",
            "-vaapi_device", "/dev/dri/renderD128",

            "-thread_queue_size", "512",
            "-f", "mpjpeg",
            "-rw_timeout", "5000000",
            "-i", SOURCE,

            "-thread_queue_size", "512",
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-i", AUDIO_SOURCE,

            "-map", "0:v:0",
            "-map", "1:a:0",

            "-vf", "fps=15,format=nv12,hwupload",
            "-c:v", "h264_vaapi",
            "-rc_mode", "CQP",
            "-qp", "23",
            "-g", "30",

            "-c:a", "aac",
            "-b:a", "96k",
            "-ar", "48000",
            "-ac", "2",
            "-f", "segment",
            "-segment_time", "3600",
            "-segment_atclocktime", "1",
            "-reset_timestamps", "1",
            "-strftime", "1",
            "-segment_format", "mp4",
            "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
            output
        ]
        log = open(LOG_FILE, "ab", buffering=0)
        log_start = log.tell()
        recording_state = "starting"
        try:
            recorder = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=log,
                start_new_session=True, text=True, bufsize=1
            )
        except Exception:
            recording_state = "failed"
            log.close()
            raise
        # Keep the log file descriptor open while FFmpeg runs.
        recorder._camera_log = log
        recorder._camera_log_start = log_start
        recorder._camera_expected_stop = False
        ready = threading.Event()
        threading.Thread(target=watch_progress, args=(recorder, ready), daemon=True).start()
        deadline = time.monotonic() + 15
        while not ready.is_set() and recorder.poll() is None and time.monotonic() < deadline:
            ready.wait(0.1)
        if not ready.is_set() or recorder.poll() is not None:
            if recorder.poll() is None:
                recorder._camera_expected_stop = True
                recorder.terminate()
                try:
                    recorder.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    recorder.kill()
                    recorder.wait(timeout=3)
            last_error = recorder_error(recorder)
            recording_state = "failed"
            raise RuntimeError(last_error)
        recording_state = "recording"
        started_at = time.time()
        last_error = None
        return True, "錄影已啟動"


def stop_recording():
    global recorder, started_at, last_error, recording_state
    with lock:
        proc = recorder
        if proc is None or proc.poll() is not None:
            is_recording()
            recorder = None
            started_at = None
            return False, "目前沒有正在執行的錄影"

        proc._camera_expected_stop = True
        recording_state = "stopping"
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        finally:
            log = getattr(proc, "_camera_log", None)
            if log:
                log.close()
            recorder = None
            started_at = None
            recording_state = "idle"

        if proc.returncode not in (0, 255, -signal.SIGINT):
            last_error = f"FFmpeg 結束代碼：{proc.returncode}"
            recording_state = "failed"
        else:
            last_error = None
        return True, "錄影已停止"


def list_recordings():
    items = []
    names = [name for name in os.listdir(VIDEO_DIR) if FILENAME_RE.fullmatch(name)]
    latest = max(names, default=None)
    for name in names:
        if not FILENAME_RE.fullmatch(name):
            continue
        path = os.path.join(VIDEO_DIR, name)
        try:
            stat = os.stat(path)
            if not os.path.isfile(path):
                continue
            health = inspect_recording(path, active=bool(name == latest and recorder is not None and recorder.poll() is None))
            items.append({
                "filename": name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(
                    stat.st_mtime
                ).astimezone().isoformat(timespec="seconds"),
                **health,
            })
        except OSError:
            continue
    items.sort(key=lambda x: x["filename"], reverse=True)
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "CameraRecorderAPI/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def send_json(self, status, data):
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("無效的 Content-Length")
        if not 0 <= length <= 4096:
            raise ValueError("Content-Length 必須介於 0 到 4096")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Content-Type 必須為 application/json")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON 必須為物件")
            return value
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("JSON 格式錯誤") from exc

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in {"/library", "/trash"} or path.startswith("/thumbnail/"):
            try:
                library = media_library()
                if path == "/library":
                    self.send_json(200, library.list(parsed.query))
                elif path == "/trash":
                    self.send_json(200, {"items": library.list_trash()})
                else:
                    send_file(self, library.thumbnail(unquote(path[len("/thumbnail/"):])), "private, max-age=3600")
            except Exception as exc:
                media_error(self, exc)
            return

        if path in {"/status", "/health"}:
            with lock:
                active = is_recording()
                fresh = bool(last_frame_tick and time.monotonic() - last_frame_tick < 15)
                healthy = fresh and recording_state != "failed"
                self.send_json(503 if path == "/health" and not healthy else 200, {
                    "recording": active,
                    "recording_state": recording_state,
                    "healthy": healthy,
                    "last_error": last_error,
                    "motion_error": motion_error,
                    "frame_age_seconds": round(time.monotonic() - last_frame_tick, 2) if last_frame_tick else None,
                    "motion_enabled": motion_enabled,
                    "motion_detected": motion_detected,
                    "started_at": (
                        datetime.fromtimestamp(started_at).astimezone().isoformat(
                            timespec="seconds"
                        ) if active and started_at else None
                    ),
                    "message": (
                        "錄影進行中" if active else
                        (last_error or motion_error or "錄影服務正常，尚未開始錄影")
                    )
                })
            return

        if path == "/recordings":
            self.send_json(200, {"recordings": list_recordings()})
            return

        prefix = "/file/"
        if path.startswith(prefix):
            name = unquote(path[len(prefix):])
            if not FILENAME_RE.fullmatch(name) or "/" in name or "\\" in name:
                self.send_json(400, {"error": "不合法的檔案名稱"})
                return

            file_path = os.path.join(VIDEO_DIR, name)
            try:
                size = os.path.getsize(file_path)
                if not os.path.isfile(file_path):
                    raise FileNotFoundError
            except OSError:
                self.send_json(404, {"error": "找不到錄影檔"})
                return

            # Support byte ranges for browser seeking.
            start, end = 0, size - 1
            status = 200
            range_header = self.headers.get("Range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                if not match:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                a, b = match.groups()
                if not a and not b:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                if not a:
                    count = int(b)
                    start = max(0, size - count)
                else:
                    start = int(a)
                    end = min(int(b), size - 1) if b else size - 1
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206

            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", "video/mp4" if name.endswith(".mp4") else "video/x-matroska")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Disposition",
                f'inline; filename="{name}"'
            )
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()

            try:
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        self.send_json(404, {"error": "找不到 API 路徑"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            origin = self.headers.get("Origin")
            if origin and (urlparse(origin).scheme not in {"http", "https"}
                           or urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower()):
                self.send_json(403, {"error": "origin rejected"})
                return
            payload = self.read_json()
            global motion_enabled
            if path in {"/trash", "/restore"}:
                try:
                    with lock:
                        library = media_library()
                        result = (library.move_to_trash(payload.get("filename")) if path == "/trash" else
                                  library.restore(payload.get("token")))
                    self.send_json(200, result)
                except Exception as exc:
                    media_error(self, exc)
                return
            if path == "/motion":
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled 必須是布林值")
                with lock:
                    motion_enabled = enabled
                self.send_json(200, {"ok": True, "motion_enabled": enabled,
                                     "message": "移動偵測已啟用" if enabled else "移動偵測已停用"})
                return
            if path == "/start":
                with lock:
                    previous_motion = motion_enabled
                    motion_enabled = False
                    try:
                        changed, message = start_recording()
                    except Exception:
                        motion_enabled = previous_motion
                        raise
                self.send_json(200, {
                    "ok": True, "recording": is_recording(), "message": message
                })
                return
            if path == "/stop":
                with lock:
                    motion_enabled = False
                changed, message = stop_recording()
                self.send_json(200, {
                    "ok": True, "recording": False, "message": message
                })
                return
            self.send_json(404, {"error": "找不到 API 路徑"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})



def motion_detector_worker():
    """以 OpenCV 分析 uStreamer 影像；有動作即錄影，靜止 30 秒後停止。"""
    global motion_enabled, motion_detected, last_motion_at, motion_error, last_frame_tick, last_start_attempt, last_error
    try:
        import cv2
    except ImportError:
        with lock:
            last_error = "缺少 OpenCV；請安裝 Ubuntu 套件 python3-opencv"
        print(f"[motion] {last_error}", flush=True)
        return

    previous = None
    consecutive_motion = 0
    cap = None
    while True:
        try:
            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                cap = MJPEGCapture(SOURCE)
                if not cap.isOpened():
                    raise RuntimeError("無法開啟 uStreamer 影像來源")
                previous = None
                time.sleep(1)
                continue

            ok, frame = cap.read()
            if not ok or frame is None:
                motion_error = "影像來源中斷，正在重新連線"
                cap.release()
                cap = None
                previous = None
                time.sleep(2)
                continue

            frame = cv2.resize(frame, (320, 240), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)
            now = time.monotonic()
            last_frame_tick = now
            motion_error = None
            detected = False
            if previous is not None:
                delta = cv2.absdiff(previous, gray)
                _, mask = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
                mask = cv2.dilate(mask, None, iterations=2)
                changed_ratio = cv2.countNonZero(mask) / float(mask.size)
                # 初始門檻：至少約 1.5% 影像像素有明顯變化。
                detected = changed_ratio >= 0.015
            previous = gray

            with lock:
                enabled = motion_enabled
                if detected:
                    consecutive_motion += 1
                else:
                    consecutive_motion = 0
                # 連續兩張畫面達門檻才算動作，降低單幀雜訊誤觸發。
                if enabled and consecutive_motion >= 2:
                    motion_detected = True
                    last_motion_at = now
                    if not is_recording() and now - last_start_attempt >= 15:
                        last_start_attempt = now
                        try:
                            changed, message = start_recording()
                            print(f"[motion] {message}", flush=True)
                        except Exception as exc:
                            motion_error = f"移動觸發錄影失敗：{exc}"
                            print(f"[motion] {motion_error}", flush=True)
                else:
                    motion_detected = False

                if enabled and is_recording() and last_motion_at and (now - last_motion_at) >= MOTION_STOP_DELAY:
                    changed, message = stop_recording()
                    last_motion_at = 0.0
                    print(f"[motion] 靜止 30 秒，{message}", flush=True)
                if not enabled:
                    last_motion_at = 0.0
            time.sleep(0.15)
        except Exception as exc:
            with lock:
                motion_error = f"移動偵測錯誤：{exc}"
            print(f"[motion] {motion_error}", flush=True)
            if cap is not None:
                cap.release()
                cap = None
            previous = None
            time.sleep(3)


def graceful_shutdown(signum, frame):
    """服務關閉或重啟時，先讓 FFmpeg 正常完成 MP4 封裝。"""
    print(f"[shutdown] received signal {signum}; stopping recorder", flush=True)
    try:
        changed, message = stop_recording()
        print(f"[shutdown] {message}", flush=True)
    except Exception as exc:
        print(f"[shutdown] recorder stop error: {exc}", flush=True)
    raise SystemExit(0)


if __name__ == "__main__":
    os.makedirs(VIDEO_DIR, exist_ok=True)

    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"Camera Recorder API listening on {HOST}:{PORT}", flush=True)

    # 預設啟用移動偵測；不再於服務啟動時無條件開始錄影。
    threading.Thread(
        target=motion_detector_worker,
        name="camera-motion-detector",
        daemon=True,
    ).start()

    server.serve_forever()
