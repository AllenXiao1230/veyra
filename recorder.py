#!/usr/bin/env python3
import json
import os
import re
import signal
import subprocess
import threading
import time
import math
import shutil
import uuid
import urllib.request
import sys
sys.path.append("/srv/camera-monitor/app/common")
from camera_catalog import inspect_recording
from camera_media import MediaLibrary, media_error, send_file
from camera_state import atomic_json, media_guard
from camera_http import serve_video
from camera_storage import media_bytes
from camera_playback import playback_file
from camera_sessions import file_states, reconcile_sessions
from camera_deployment import deployment_active
from pathlib import Path
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HOST = "127.0.0.1"
PORT = 8091
VIDEO_DIR = "/srv/camera-monitor/data/recordings"
SOURCE = "http://127.0.0.1:8080/stream"
RECORDING_SOURCE = "http://127.0.0.1:8093/stream"
AUDIO_SOURCE = "rtsp://127.0.0.1:8554/mic"
FILENAME_RE = re.compile(r"^recording-\d{8}-\d{6}(?:-[0-9a-f]{12})?\.(mp4|mkv)$")
LOG_FILE = "/srv/camera-monitor/logs/camera-recorder-ffmpeg.log"
lock = threading.RLock()
operation_lock = threading.RLock()
session_lock = threading.RLock()
action_gate = threading.Lock()
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
recording_trigger = None
active_session_id = None
session_created_at = None
session_event_ids = []
motion_suppressed = False
detection_detail = {}
MIN_FREE_MB = 1024
MAX_STORAGE_MB = 0
recording_requested = False
recording_audio = True
recording_warning = None
recovery_after = 0.0
recovery_attempts = 0
audio_checked_at = 0.0
session_recovery_issues = []


def load_settings():
    global motion_enabled, MOTION_STOP_DELAY, MIN_FREE_MB, MAX_STORAGE_MB
    path = Path(VIDEO_DIR) / "settings.json"
    if path.exists():
        data = json.loads(path.read_text())
        if type(data.get("motion_enabled")) is not bool:
            raise ValueError("錄影設定格式錯誤")
        delay = data.get("stop_delay", 30)
        reserve = data.get("min_free_mb", 1024)
        if type(delay) is not int or not 5 <= delay <= 600 or type(reserve) is not int or not 256 <= reserve <= 102400:
            raise ValueError("錄影設定範圍錯誤")
        motion_enabled, MOTION_STOP_DELAY, MIN_FREE_MB = data["motion_enabled"], delay, reserve
        quota = data.get("max_storage_mb", 0)
        if type(quota) is not int or not 0 <= quota <= 104857600:
            raise ValueError("錄影容量上限範圍錯誤")
        MAX_STORAGE_MB = quota


def configure_motion(payload):
    global motion_enabled, MOTION_STOP_DELAY, MIN_FREE_MB, MAX_STORAGE_MB
    with media_guard(VIDEO_DIR), lock:
        enabled = payload.get("enabled", motion_enabled)
        delay = payload.get("stop_delay", int(MOTION_STOP_DELAY))
        reserve = payload.get("min_free_mb", MIN_FREE_MB)
        quota = payload.get("max_storage_mb", MAX_STORAGE_MB)
        if type(quota) is not int or not 0 <= quota <= 104857600:
            raise ValueError("錄影容量上限須為 0–104857600 MB，0 表示不限")
        if type(enabled) is not bool or type(delay) is not int or not 5 <= delay <= 600:
            raise ValueError("啟用狀態必須為布林值，停止延遲須為 5–600 秒")
        if type(reserve) is not int or not 256 <= reserve <= 102400:
            raise ValueError("保留空間須為 256–102400 MB")
        atomic_json(Path(VIDEO_DIR) / "settings.json", {"motion_enabled": enabled, "stop_delay": delay, "min_free_mb": reserve, "max_storage_mb": quota})
        motion_enabled, MOTION_STOP_DELAY, MIN_FREE_MB = enabled, delay, reserve
        MAX_STORAGE_MB = quota


def persist_session(finished=False):
    with session_lock:
        with lock:
            sid = active_session_id
            if not sid:
                return
            data = {"id": sid, "trigger": recording_trigger, "started_at": session_created_at,
                    "state": recording_state, "event_ids": list(session_event_ids), "last_error": last_error,
                    "audio": recording_audio, "warning": recording_warning}
        path = Path(VIDEO_DIR) / "sessions" / (sid + ".json")
        if path.exists():
            previous = json.loads(path.read_text())
            if "ended_at" in previous:
                data["ended_at"] = previous["ended_at"]
        if finished:
            data["ended_at"] = time.time()
        data["files"] = sorted(p.name for p in Path(VIDEO_DIR).glob(f"recording-*-{sid}.mp4"))
        data['file_states'] = file_states(VIDEO_DIR, data, active_media)
        with media_guard(VIDEO_DIR):
            atomic_json(path, data)


def background_action(action):
    if not action_gate.acquire(blocking=False):
        return False
    def run():
        try:
            action()
        except Exception as error:
            print("recording action failed: " + str(error), flush=True)
        finally:
            action_gate.release()
    threading.Thread(target=run, name="recording-action", daemon=True).start()
    return True


def check_recording_capacity():
    global last_error, recording_state
    if is_recording() and storage_exhausted():
        stop_recording()
        with lock:
            last_error = "已停止錄影：磁碟保留空間或錄影容量上限已達；請管理資料或調整設定。"
            recording_state = "failed"
        persist_session(finished=True)


def storage_exhausted():
    return (shutil.disk_usage(VIDEO_DIR).free < MIN_FREE_MB * 1024 * 1024 or
            bool(MAX_STORAGE_MB and media_bytes(VIDEO_DIR) >= MAX_STORAGE_MB * 1024 * 1024))


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
        pattern = f"recording-*-{active_session_id}.mp4" if active_session_id else "recording-*.mp4"
        newest = max((p.name for p in Path(VIDEO_DIR).glob(pattern)), default=None)
        return name == newest
    except OSError:
        return True


def media_library():
    global _media_library
    if _media_library is None or _media_library.root != Path(VIDEO_DIR):
        candidate = MediaLibrary(VIDEO_DIR, "recording", active=active_media, inspect=inspect_recording)
        with lock:
            _media_library = candidate
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
                    value = int(line.split("=", 1)[1])
                    if value > getattr(proc, '_camera_out_time', 0):
                        proc._camera_out_time = value
                        proc._camera_last_progress = time.monotonic()
                    if value > 0:
                        ready.set()
                except ValueError:
                    pass
        proc.wait()
        with lock:
            if recorder is proc and not getattr(proc, "_camera_expected_stop", False):
                last_error = recorder_error(proc)
                recording_state = "failed"
                started_at = None
        if recorder is proc and not getattr(proc, "_camera_expected_stop", False):
            persist_session(finished=True)
    finally:
        proc.stdout.close()
        proc._camera_log.close()


def audio_available():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8094/health', timeout=2) as response:
            return json.load(response).get('healthy') is True
    except (OSError, ValueError, TypeError):
        return False


def start_recording(trigger='manual', _audio=None):
    global recording_requested, recording_warning
    with operation_lock:
        if recorder is not None and recorder.poll() is None:
            return _start_recording(trigger, recording_audio)
        audio = audio_available() if _audio is None else _audio
        previous = recorder
        try:
            result = _start_recording(trigger, audio)
        except RuntimeError:
            # An inaccessible input fails before optional stream mapping helps.
            # Only retry a launched audio encoder, never capacity/camera failures.
            if not audio or recorder is previous or recorder is None:
                raise
            result = _start_recording(trigger, False)
        if result[0]:
            recording_requested = True
            recording_warning = None if recording_audio else '音訊暫時不可用，正在錄製純影像；音訊恢復後自動接續。'
            persist_session()
        return result


def _start_recording(trigger="manual", audio=True):
    global recorder, started_at, last_error, recording_state, recording_trigger
    global active_session_id, session_created_at, session_event_ids
    global recording_audio
    with operation_lock:
        if deployment_active():raise RuntimeError('系統更新中，請稍後再啟動錄影。')
        if trigger == "motion" and (not motion_enabled or motion_suppressed):
            return False, "自動觸發已暫停"
        if recorder is not None and recorder.poll() is None:
            if trigger == "manual":
                recording_trigger = "manual"
                persist_session()
            return False, "錄影已在進行中"

        os.makedirs(VIDEO_DIR, exist_ok=True)
        if storage_exhausted():
            last_error = "磁碟保留空間或錄影容量上限已達，錄影未啟動；請管理資料或調整設定。"
            recording_state = "failed"
            raise RuntimeError(last_error)
        with lock:
            active_session_id = uuid.uuid4().hex[:12]
            session_created_at = time.time()
            session_event_ids = []
            recording_trigger = trigger
            recording_audio = audio
            recording_state = "starting"
        # Check that the camera stream is reachable before starting FFmpeg.
        try:
            import urllib.request
            request = urllib.request.Request(
                RECORDING_SOURCE, headers={"User-Agent": "CameraRecorder/1.0"}
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError("攝影機串流回應非 200")
        except Exception as exc:
            last_error = f"無法連接時間戳影像串流：{exc}"
            recording_state = "failed"
            raise RuntimeError(last_error) from exc

        output = os.path.join(
            VIDEO_DIR, "recording-%Y%m%d-%H%M%S-" + active_session_id + ".mp4"
        )
        cmd = [
            "/usr/bin/ffmpeg",
            "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-progress", "pipe:1", "-stats_period", "0.5", "-nostats",
            "-n",
            "-vaapi_device", "/dev/dri/renderD128",

            "-thread_queue_size", "512",
            "-f", "mpjpeg",
            "-use_wallclock_as_timestamps", "1",
            "-rw_timeout", "5000000",
            "-i", RECORDING_SOURCE,

            *(["-thread_queue_size", "512",
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-i", AUDIO_SOURCE] if audio else []),

            "-map", "0:v:0",
            *(["-map", "1:a:0"] if audio else []),

            # mpjpeg has no reliable frame timestamps; its decoder otherwise
            # invents 25 fps and speeds up a slower live preview.
            "-vf", "setpts=(RTCTIME-RTCSTART)/(TB*1000000),fps=15,format=nv12,hwupload",
            "-c:v", "h264_vaapi",
            "-rc_mode", "CQP",
            "-qp", "23",
            "-g", "30",

            *(["-c:a", "aac",
            "-b:a", "96k",
            "-ar", "48000",
            "-ac", "2"] if audio else ["-an"]),
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
            with media_guard(VIDEO_DIR):
                if deployment_active():raise RuntimeError('系統更新中，請稍後再啟動錄影。')
                recorder = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
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
        recorder._camera_created_at = time.monotonic()
        recorder._camera_last_progress = time.monotonic()
        recorder._camera_out_time = 0
        recorder._camera_growth_at = time.monotonic()
        recorder._camera_output_bytes = 0
        recorder._camera_output_sizes = {}
        persist_session()
        ready = threading.Event()
        recorder._camera_watcher = threading.Thread(target=watch_progress, args=(recorder, ready), daemon=True)
        recorder._camera_watcher.start()
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
            recorder._camera_watcher.join(timeout=5)
            last_error = recorder_error(recorder)
            recording_state = "failed"
            persist_session(finished=True)
            raise RuntimeError(last_error)
        recording_state = "recording"
        started_at = time.time()
        last_error = None
        persist_session()
        return True, "錄影已啟動"


def stop_recording(preserve_intent=False):
    global recorder, started_at, last_error, recording_state
    global recording_requested
    if not preserve_intent:
        with lock:
            recording_requested = False
    with operation_lock:
        if not preserve_intent:
            with lock:
                recording_requested = False
        proc = recorder
        if proc is None or proc.poll() is not None:
            is_recording()
            watcher = getattr(proc, "_camera_watcher", None) if proc is not None else None
            if watcher:
                watcher.join(timeout=5)
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
            watcher = getattr(proc, "_camera_watcher", None)
            if watcher:
                watcher.join(timeout=5)
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
        persist_session(finished=True)
        return True, "錄影已停止"


def encoder_status(now=None):
    now = time.monotonic() if now is None else now
    proc = recorder
    if proc is None or type(getattr(proc, '_camera_created_at', None)) not in (int, float):
        return {'healthy': not recording_requested, 'progress_age_seconds': None, 'stalled': False}
    progress = now - proc._camera_last_progress
    growth = now - proc._camera_growth_at
    stalled = proc.poll() is None and recording_state == 'recording' and (progress > 20 or growth > 30)
    return {'healthy': proc.poll() is None and not stalled, 'stalled': stalled,
            'progress_age_seconds': round(progress, 2), 'growth_age_seconds': round(growth, 2),
            'output_bytes': proc._camera_output_bytes, 'out_time_us': proc._camera_out_time}


def check_encoder():
    global recovery_after, recovery_attempts, audio_checked_at, recording_warning
    with operation_lock:
        proc = recorder
        if not recording_requested:
            return
        now = time.monotonic()
        if (recording_trigger == 'motion' and not motion_detected and not motion_error and last_motion_at
                and last_frame_tick and now-last_frame_tick < 3 and now-last_motion_at >= MOTION_STOP_DELAY):
            stop_recording()
            return
        if proc is not None:
            names = list(Path(VIDEO_DIR).glob(f'recording-*-{active_session_id}.mp4'))
            sizes = {}
            for path in names:
                try:
                    info=path.stat(); sizes[(info.st_dev,info.st_ino)]=info.st_size
                except FileNotFoundError: continue
            previous=getattr(proc,'_camera_output_sizes',{})
            if any(size > previous.get(identity,0) for identity,size in sizes.items()):
                proc._camera_growth_at = now
            proc._camera_output_bytes = sum(sizes.values())
            proc._camera_output_sizes = sizes
        state = encoder_status(now)
        restore_audio = False
        if not recording_audio and now - audio_checked_at >= 60:
            audio_checked_at = now
            restore_audio = audio_available()
        broken = proc is None or proc.poll() is not None or state['stalled']
        if not broken and not restore_audio:
            if now - getattr(proc, '_camera_created_at', now) > 60:
                recovery_attempts = 0
            persist_session()
            return
        if now < recovery_after:
            return
        trigger, remembered = recording_trigger, list(session_event_ids)
        reason = '音訊已恢復，接續錄製' if restore_audio else '錄影輸出停止，正在恢復'
        recording_warning = reason
        stop_recording(preserve_intent=True)
        # Manual stop cancels intent before waiting on this operation lock.
        if not recording_requested:
            return
        recovery_attempts += 1
        recovery_after = now + min(300, 5 * 2 ** min(recovery_attempts, 6))
        try:
            start_recording(trigger, _audio=True if restore_audio else False)
            for identifier in remembered[-1:]:
                if identifier not in session_event_ids:
                    session_event_ids.append(identifier)
            persist_session()
        except Exception as error:
            recording_warning = '錄影恢復失敗，稍後重試：' + str(error)[-160:]


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
                    self.send_json(200, library.trash_page(parsed.query) if parsed.query else {"items": library.list_trash()})
                else:
                    send_file(self, library.thumbnail(unquote(path[len("/thumbnail/"):])), "private, max-age=3600")
            except Exception as exc:
                media_error(self, exc)
            return

        if path in {"/status", "/health"}:
            with lock:
                active = is_recording()
                fresh = bool(last_frame_tick and time.monotonic() - last_frame_tick < 15)
                output = encoder_status()
                healthy = fresh and recording_state != "failed" and output['healthy']
                self.send_json(503 if path == "/health" and not healthy else 200, {
                    "recording": active,
                    "recording_state": recording_state,
                    "healthy": healthy,
                    "encoder": output,
                    "audio_included": recording_audio if active else None,
                    "warning": recording_warning,
                    "recovery_attempts": recovery_attempts,
                    "session_recovery_issues": session_recovery_issues,
                    "last_error": last_error,
                    "motion_error": motion_error,
                    "frame_age_seconds": round(time.monotonic() - last_frame_tick, 2) if last_frame_tick else None,
                    "motion_enabled": motion_enabled,
                    "motion_detected": motion_detected,
                    "stop_delay": int(MOTION_STOP_DELAY),
                    "min_free_mb": MIN_FREE_MB,
                    "max_storage_mb": MAX_STORAGE_MB,
                    "recording_trigger": recording_trigger if active or recording_state in {"starting", "stopping"} else None,
                    "session_id": active_session_id if active else None,
                    "motion_suppressed": motion_suppressed,
                    "stop_in_seconds": math.ceil(max(0, MOTION_STOP_DELAY - (time.monotonic() - last_motion_at)))
                        if active and recording_trigger == "motion" and last_motion_at and not motion_error else None,
                    "detection": detection_detail,
                    "active_files": [p.name for p in Path(VIDEO_DIR).glob("recording-*.mp4") if active_media(p.name)]
                        if recorder is not None and recorder.poll() is None else [],
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

        prefix = "/playback/" if path.startswith("/playback/") else "/file/"
        if path.startswith(prefix):
            name = unquote(path[len(prefix):])
            if not FILENAME_RE.fullmatch(name) or "/" in name or "\\" in name:
                self.send_json(400, {"error": "不合法的檔案名稱"})
                return

            try:
                source = media_library().file(name)
                if prefix == "/playback/":
                    if active_media(name):
                        raise ValueError("錄影完成後才可回放")
                    source = playback_file(source)
                    media_library().file(name)
                serve_video(self, source)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                media_error(self, error)
            return

        self.send_json(404, {"error": "找不到 API 路徑"})

    def do_HEAD(self):
        path = urlparse(self.path).path
        prefix = "/playback/" if path.startswith("/playback/") else "/file/"
        if not path.startswith(prefix):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            name = unquote(path[len(prefix):])
            source = media_library().file(name)
            if prefix == "/playback/":
                if active_media(name):
                    raise ValueError("錄影尚未完成")
                source = playback_file(source)
                media_library().file(name)
            serve_video(self, source, head=True)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            origin = self.headers.get("Origin")
            if origin and (urlparse(origin).scheme not in {"http", "https"}
                           or urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower()):
                self.send_json(403, {"error": "origin rejected"})
                return
            payload = self.read_json()
            global motion_enabled, motion_suppressed
            if path in {"/trash", "/restore", "/purge", "/batch"}:
                try:
                    library = media_library()
                    result = (library.batch(payload) if path == '/batch' else
                              library.purge(payload.get('token'), payload.get('confirmation')) if path == '/purge' else
                              library.move_to_trash(payload.get("filename")) if path == "/trash" else
                              library.restore(payload.get("token")))
                    self.send_json(200, result)
                except Exception as exc:
                    media_error(self, exc)
                return
            if path == "/motion":
                configure_motion(payload)
                self.send_json(200, {"ok": True, "motion_enabled": motion_enabled, "stop_delay": int(MOTION_STOP_DELAY),
                                     "message": "錄影設定已保存"})
                return
            if path == "/start":
                changed, message = start_recording(trigger="manual")
                self.send_json(200, {
                    "ok": True, "recording": is_recording(), "message": message
                })
                return
            if path == "/stop":
                with lock:
                    motion_suppressed = True
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



def stop_after_quiet():
    # A stop may have waited behind startup or another encoder operation. Check
    # observations again after acquiring the operation lock, before signalling.
    with operation_lock:
        with lock:
            now = time.monotonic()
            if (recording_trigger != "motion" or motion_detected or motion_error
                    or not last_frame_tick or now - last_frame_tick >= 3
                    or not last_motion_at or now - last_motion_at < MOTION_STOP_DELAY):
                return False, "動作仍在持續或尚未確認安靜"
        return stop_recording()


def motion_detector_worker():
    """Consume the same confirmed motion used for snapshots; AI only annotates."""
    global motion_detected, last_motion_at, motion_error, last_frame_tick, last_start_attempt
    global motion_suppressed, detection_detail
    capacity_checked = 0
    while True:
        try:
            if time.monotonic() - capacity_checked > 5:
                capacity_checked = time.monotonic()
                if is_recording() and storage_exhausted():
                    background_action(check_recording_capacity)
                elif recording_requested:
                    background_action(check_encoder)
            with urllib.request.urlopen("http://127.0.0.1:8092/status", timeout=3) as response:
                state = json.load(response)
            now = time.monotonic()
            age = state.get("frame_age_seconds")
            healthy = bool(state.get("healthy") and age is not None and age < 3
                           and not state.get("warmup_remaining", 0))
            start = stop = remember = False
            with lock:
                detection_detail = {k: state.get(k) for k in ("healthy", "person_detector_enabled", "person_detector",
                                     "ai_inference_ms", "trigger_policy", "last_event_at", "last_event_id")}
                confirmed = bool(healthy and (state.get("motion_active") or
                                 (state.get("motion_age_seconds") is not None and state["motion_age_seconds"] < 1.5)))
                motion_detected = bool(healthy and (confirmed or state.get("activity_active")))
                if healthy:
                    last_frame_tick = now - age
                    if motion_error:
                        # Unknown observations are never evidence of quiet.
                        last_motion_at = now
                    motion_error = None
                else:
                    motion_error = "共用動作偵測尚未就緒，正在重連"
                    last_motion_at = now
                if motion_detected:
                    last_motion_at = now
                if healthy and state.get("activity_age_seconds") is not None:
                    last_motion_at = max(last_motion_at, now - max(0, state["activity_age_seconds"]))
                if healthy and not motion_detected and (not last_motion_at or now - last_motion_at >= MOTION_STOP_DELAY):
                    motion_suppressed = False
                active = is_recording()
                if motion_enabled and confirmed and not motion_suppressed and not active and now - last_start_attempt >= 15:
                    last_start_attempt = now
                    start = True
                if healthy and active and recording_trigger == "motion" and not motion_detected and last_motion_at and now - last_motion_at >= MOTION_STOP_DELAY:
                    stop = True
                event_id = state.get("last_event_id")
                stamp = state.get("last_event_at")
                if active and event_id and event_id not in session_event_ids and stamp:
                    event_time = datetime.fromisoformat(stamp).timestamp()
                    if session_created_at and event_time >= session_created_at - 2:
                        session_event_ids.append(event_id)
                        remember = True
            if start:
                background_action(lambda: start_recording(trigger="motion"))
            if stop:
                background_action(stop_after_quiet)
            if remember:
                persist_session()
            time.sleep(0.4)
        except Exception as exc:
            with lock:
                motion_detected = False
                motion_error = "無法讀取共用動作偵測：" + str(exc)[-180:]
                last_motion_at = time.monotonic()
            time.sleep(1)


def session_recovery_worker():
    global session_recovery_issues
    while True:
        time.sleep(60)
        if recording_requested:continue
        def reconcile():
            global session_recovery_issues
            session_recovery_issues=reconcile_sessions(VIDEO_DIR,inspect_recording,
                skip=lambda sid:sid==active_session_id and (recording_state=='starting' or
                    (recorder is not None and recorder.poll() is None)),limit=3)
        background_action(reconcile)


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
    load_settings()
    media_library()
    session_recovery_issues = reconcile_sessions(VIDEO_DIR, inspect_recording)

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
    threading.Thread(target=session_recovery_worker,name='session-recovery',daemon=True).start()

    server.serve_forever()
