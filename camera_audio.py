#!/usr/bin/python3
"""User-scoped microphone selection, RTSP publishing and bounded recovery."""
import collections
import json
import math
import os
import pathlib
import signal
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

CONFIG = pathlib.Path('/srv/camera-monitor/config/audio/audio.json')
RTSP = "rtsp://127.0.0.1:8554/mic"


def run(args, timeout=3):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, "LC_ALL": "C.UTF-8"})
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-500:] or "音訊指令失敗")
    return result.stdout


def sources():
    result = []
    for item in json.loads(run(["pactl", "-f", "json", "list", "sources"])):
        if item.get("monitor_of_sink") not in (None, 4294967295, "4294967295") or item["name"].endswith(".monitor"):
            continue
        volumes = list(item.get("volume", {}).values())
        volume = round(max((v.get("value", 0) for v in volumes), default=0) * 100 / 65536)
        active = item.get("active_port")
        ports = item.get("ports", [])
        if isinstance(ports, dict):
            ports = list(ports.values())
        port = next((p for p in ports if p.get("name") == active), {})
        description = item.get("description")
        if not description or description == "(null)":
            description = item.get("properties", {}).get("device.product.name") or item["name"]
        result.append({"name": item["name"], "index": item["index"],
                       "description": description,
                       "volume": volume, "muted": bool(item.get("mute")),
                       "state": item.get("state"),
                       "available": port.get("availability") not in ("not available", "not_available", "no")})
    return result


def recording_active():
    with urllib.request.urlopen("http://127.0.0.1:8091/status", timeout=2) as response:
        state = json.load(response)
    return state.get("recording") or state.get("recording_state") in ("starting", "recording", "stopping")


def probe_packets():
    data = json.loads(run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                           "-timeout", "2500000", "-read_intervals", "%+0.2",
                           "-select_streams", "a:0", "-show_entries", "packet=pts_time",
                           "-of", "json", RTSP], timeout=4))
    if not data.get("packets"):
        raise RuntimeError("RTSP 沒有收到音訊封包")


class AudioService:
    def __init__(self, config=CONFIG):
        self.config_path = pathlib.Path(config)
        try:
            self.config = json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            self.config = {"source": None, "volume": 80, "muted": False}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.revision = 0
        self.process_revision = -1
        self.process = None
        self.devices = []
        self.errors = collections.deque(maxlen=6)
        self.error = None
        self.state = "starting"
        self.last_progress = 0.0
        self.last_out_time = 0
        self.last_packet = 0.0
        self.started = 0.0
        self.next_attempt = 0.0
        self.restart_count = 0
        self.failures = 0
        self.actual_source = None
        self.level_dbfs = None
        self.level_at = 0.0

    def level(self):
        with self.lock:
            available = bool(self.process is not None and self.process.poll() is None and
                             self.process_revision == self.revision and self.actual_source == self.config.get("source") and
                             self.level_at and time.monotonic() - self.level_at < 2)
            db = self.level_dbfs if available else None
            value = round(max(0, min(100, (db + 60) / 60 * 100))) if db is not None else 0
            return {"available": available, "level": value, "dbfs": db}

    def save(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.config, ensure_ascii=False) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, self.config_path)

    def status(self):
        with self.lock:
            now = time.monotonic()
            age = now - self.last_progress if self.last_progress else None
            packet_age = now - self.last_packet if self.last_packet else None
            alive = self.process is not None and self.process.poll() is None
            route_ok = self.actual_source == self.config.get("source")
            selected = next((d for d in self.devices if d["name"] == self.config.get("source")), {})
            healthy = bool(alive and self.process_revision == self.revision and age is not None and age < 10 and
                           packet_age is not None and packet_age < 20 and route_ok)
            return {"healthy": healthy, "state": "running" if healthy else self.state,
                    "selected_source": self.config.get("source"), "actual_source": self.actual_source,
                    "volume": self.config.get("volume", 80), "muted": self.config.get("muted", False),
                    "actual_volume": selected.get("volume"), "actual_muted": selected.get("muted"),
                    "sources": list(self.devices), "last_error": self.error,
                    "progress_age_seconds": round(age, 1) if age is not None else None,
                    "packet_age_seconds": round(packet_age, 1) if packet_age is not None else None,
                    "pid": self.process.pid if alive else None, "restart_count": self.restart_count,
                    "retry_in_seconds": max(0, round(self.next_attempt - now)),
                    "auto_restart": True}

    def configure(self, payload):
        devices = sources()
        name = payload.get("source")
        if not isinstance(name, str) or not any(d["name"] == name for d in devices):
            raise ValueError("請選擇目前存在的麥克風輸入來源")
        volume = payload.get("volume", 80)
        muted = payload.get("muted", False)
        if type(volume) is not int or not 0 <= volume <= 100 or type(muted) is not bool:
            raise ValueError("音量必須是 0–100 的整數，靜音必須是布林值")
        with self.lock:
            previous = self.config
            self.config = {"source": name, "volume": volume, "muted": muted}
            try:
                self.save()
            except Exception:
                self.config = previous
                raise
            self.request_restart()

    def request_restart(self):
        with self.lock:
            self.revision += 1
            self.last_packet = self.last_progress = 0.0
            self.actual_source = None
            self.state = "restarting"
            self.error = None
            self.next_attempt = 0.0
            self.failures = 0
            self.wake.set()

    def read_progress(self, proc):
        try:
            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    try:
                        value = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    with self.lock:
                        if self.process is proc and value > self.last_out_time:
                            self.last_out_time = value
                            self.last_progress = time.monotonic()
        finally:
            proc.stdout.close()

    def read_errors(self, proc):
        try:
            for line in proc.stderr:
                text = line.strip()
                if text.startswith("lavfi.astats.Overall.RMS_level="):
                    try:
                        value = float(text.split("=", 1)[1])
                    except ValueError:
                        continue
                    if math.isnan(value) or value == math.inf:
                        continue
                    with self.lock:
                        if self.process is proc:
                            self.level_dbfs = max(-120.0, min(0.0, value)) if math.isfinite(value) else None
                            self.level_at = time.monotonic()
                    continue
                if text.startswith("frame:"):
                    continue
                with self.lock:
                    if self.process is proc:
                        self.errors.append(text[-300:])
        finally:
            proc.stderr.close()

    def launch(self, config, revision):
        run(["pactl", "set-source-volume", config["source"], str(config["volume"]) + "%"])
        run(["pactl", "set-source-mute", config["source"], "1" if config["muted"] else "0"])
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
               "-progress", "pipe:1", "-stats_period", "0.5", "-nostats",
               "-f", "pulse", "-i", config["source"], "-vn", "-ac", "1", "-ar", "48000",
               "-af", "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
                      "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=/dev/stderr:direct=1",
               "-c:a", "libopus", "-b:a", "32k", "-application", "lowdelay",
               "-f", "rtsp", "-rtsp_transport", "tcp", RTSP]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
        with self.lock:
            self.process = proc
            self.process_revision = revision
            self.started = time.monotonic()
            self.last_progress = self.last_packet = 0.0
            self.last_out_time = 0
            self.level_at = 0.0
            self.level_dbfs = None
            self.errors.clear()
            self.actual_source = None
            self.state = "starting"
            self.restart_count += 1
        threading.Thread(target=self.read_progress, args=(proc,), daemon=True).start()
        threading.Thread(target=self.read_errors, args=(proc,), daemon=True).start()

    def stop_process(self):
        with self.lock:
            proc, self.process = self.process, None
            self.last_progress = self.last_packet = 0.0
            self.level_at = 0.0
            self.level_dbfs = None
            self.actual_source = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

    def route(self):
        outputs = json.loads(run(["pactl", "-f", "json", "list", "source-outputs"]))
        with self.lock:
            proc, name, devices = self.process, self.config["source"], list(self.devices)
        if proc is None:
            return
        device = next((d for d in devices if d["name"] == name), None)
        if not device:
            raise RuntimeError("選定的麥克風已離線；保留選擇並等待裝置恢復")
        own = [o for o in outputs if str(o.get("properties", {}).get("application.process.id")) == str(proc.pid)]
        if not own:
            return
        output = own[0]
        if output["source"] != device["index"]:
            run(["pactl", "move-source-output", str(output["index"]), name])
            # Only a later observation of the route counts as healthy.
            with self.lock:
                self.actual_source = None
            return
        with self.lock:
            self.actual_source = name

    def failed(self, message):
        self.stop_process()
        with self.lock:
            self.failures += 1
            self.error = message[-700:]
            self.state = "retrying"
            self.next_attempt = time.monotonic() + min(60, 3 * 2 ** min(self.failures - 1, 5))
        print("audio recovery: " + message, flush=True)

    def supervise(self):
        applied_revision = -1
        refreshed = probed = routed = 0.0
        try:
            while not self.stop_event.is_set():
                try:
                    now = time.monotonic()
                    with self.lock:
                        revision = self.revision
                    if revision != applied_revision:
                        self.stop_process()
                        applied_revision = revision
                        with self.lock:
                            self.next_attempt = 0.0
                        probed = routed = 0.0
                    if now - refreshed >= 5:
                        devices = sources()
                        with self.lock:
                            self.devices = devices
                            if not self.config.get("source") and devices:
                                preferred = next((d for d in devices if "Digital Microphone" in d["description"]), devices[0])
                                self.config["source"] = preferred["name"]
                                self.save()
                        refreshed = now
                    with self.lock:
                        proc, config = self.process, dict(self.config)
                        due = now >= self.next_attempt
                    if proc is None and due:
                        if not any(d["name"] == config.get("source") for d in self.devices):
                            raise RuntimeError("選定的麥克風不存在，等待重新連接")
                        self.launch(config, revision)
                        probed = routed = 0.0
                    elif proc is not None:
                        if proc.poll() is not None:
                            raise RuntimeError("音訊 FFmpeg 已退出：" + " | ".join(self.errors))
                        if now - routed >= 3:
                            self.route()
                            routed = now
                        with self.lock:
                            stalled = now - (self.last_progress or self.started) > (10 if self.last_progress else 15)
                        if stalled:
                            raise RuntimeError("音訊擷取或發布停止前進，正在重新連接所選來源")
                        if self.last_progress and now - probed >= 8:
                            probe_packets()
                            with self.lock:
                                self.last_packet = time.monotonic()
                                self.error = None
                                self.state = "running"
                                if now - self.started > 60:
                                    self.failures = 0
                            probed = time.monotonic()
                except Exception as exc:
                    self.failed(str(exc))
                self.wake.wait(0.5)
                self.wake.clear()
        finally:
            self.stop_process()


class Handler(BaseHTTPRequestHandler):
    def log_request(self, code="-", size="-"):
        # High-frequency read-only meter updates should not fill the journal.
        if self.command == "GET" and urlparse(self.path).path == "/level" and code == 200:
            return
        super().log_request(code, size)

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def respond(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/level":
            self.respond(200, self.server.audio.level())
            return
        if path not in ("/status", "/health"):
            self.respond(404, {"error": "找不到 API"})
            return
        state = self.server.audio.status()
        self.respond(503 if path == "/health" and not state["healthy"] else 200, state)

    def do_POST(self):
        try:
            origin = self.headers.get("Origin")
            if origin and (urlparse(origin).scheme not in ("http", "https") or
                           urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower()):
                self.respond(403, {"error": "origin rejected"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 4096:
                raise ValueError("無效的請求長度")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("Content-Type 必須為 application/json")
            payload = json.loads(self.rfile.read(length).decode() if length else "{}")
            if not isinstance(payload, dict):
                raise ValueError("JSON 必須為物件")
            path = urlparse(self.path).path
            if path not in ("/source", "/restart"):
                self.respond(404, {"error": "找不到 API"})
                return
            if recording_active():
                self.respond(409, {"error": "請先停止錄影，再切換或重啟音訊"})
                return
            if path == "/source":
                self.server.audio.configure(payload)
            else:
                self.server.audio.request_restart()
            self.respond(202, {"ok": True, "message": "正在重新連接音訊，請等待狀態更新"})
        except (ValueError, UnicodeDecodeError) as exc:
            self.respond(400, {"error": str(exc)})
        except Exception as exc:
            self.respond(503, {"error": str(exc)})


def main():
    service = AudioService()
    server = ThreadingHTTPServer(("127.0.0.1", 8094), Handler)
    server.audio = service
    server.daemon_threads = True
    worker = threading.Thread(target=service.supervise, daemon=True)
    worker.start()

    def stop(*_):
        service.stop_event.set()
        service.wake.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        service.stop_event.set()
        service.wake.set()
        worker.join(timeout=10)
        server.server_close()


if __name__ == "__main__":
    main()
