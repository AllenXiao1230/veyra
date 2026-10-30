#!/usr/bin/python3
"""Freshness-aware timestamp preview with bounded capture recovery."""
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import cv2

sys.path.insert(0, "/srv/camera-monitor/app/common")
from camera_runtime import MJPEGCapture

SOURCE = "http://127.0.0.1:8080/stream"
HOST, PORT = "127.0.0.1", 8093
FPS = max(1, min(30, int(os.environ.get("CAMERA_PREVIEW_FPS", "15"))))
lock = threading.Condition()
stop_requested = threading.Event()
latest_frame = None
preview_frame = None
preview_frame_id = 0
low_frame = None
low_frame_id = 0
low_frame_tick = 0.0
frame_id = 0
last_frame_tick = 0.0
last_frame_at = None
last_error = None
worker = None
LOG = logging.getLogger("camera-timestamp")


def health():
    with lock:
        age = time.monotonic() - last_frame_tick if last_frame_tick else None
        ready = bool(worker and worker.is_alive() and latest_frame is not None
                     and age is not None and age < 10 and not stop_requested.is_set())
        return {"healthy": ready, "frame_id": frame_id, "last_frame_at": last_frame_at,
                "frame_age_seconds": round(age, 2) if age is not None else None,
                "last_error": last_error, "fps_limit": FPS}


def capture():
    global latest_frame, frame_id, last_frame_tick, last_frame_at, last_error
    global preview_frame, preview_frame_id
    global low_frame, low_frame_id, low_frame_tick
    preview_tick = 0.0
    while not stop_requested.is_set():
        cap = None
        try:
            cap = MJPEGCapture(SOURCE)
            if not cap.isOpened():
                raise RuntimeError(cap.error or "Camera source unavailable")
            while not stop_requested.is_set():
                began = time.monotonic()
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError(cap.error or "No fresh camera frame")
                text = time.strftime("%Y-%m-%d %H:%M:%S")
                font, scale = cv2.FONT_HERSHEY_SIMPLEX, 0.7
                (width, height), baseline = cv2.getTextSize(text, font, scale, 1)
                y2, x2 = min(frame.shape[0], 32 + height + baseline), min(frame.shape[1], 32 + width)
                roi = frame[12:y2, 12:x2]
                roi[:] = (roi * 0.35).astype("uint8")
                cv2.putText(frame, text, (20, 22 + height), font, scale, (255, 255, 255), 1, cv2.LINE_AA)
                ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
                if not ok:
                    raise RuntimeError("Timestamp JPEG encoding failed")
                small_jpeg = None
                if time.monotonic() - preview_tick >= 1 / 8:
                    height, width = frame.shape[:2]
                    small = cv2.resize(frame, (960, round(height * 960 / width)), interpolation=cv2.INTER_AREA) if width > 960 else frame
                    success, encoded = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    if success:
                        small_jpeg = encoded.tobytes()
                        preview_tick = time.monotonic()
                low_jpeg = None
                if time.monotonic() - low_frame_tick >= 0.5:
                    height, width = frame.shape[:2]
                    low = cv2.resize(frame, (640, round(height * 640 / width)), interpolation=cv2.INTER_AREA) if width > 640 else frame
                    success, encoded = cv2.imencode('.jpg', low, [cv2.IMWRITE_JPEG_QUALITY, 55])
                    if success:
                        low_jpeg = encoded.tobytes()
                with lock:
                    latest_frame = jpeg.tobytes()
                    frame_id += 1
                    if small_jpeg is not None:
                        preview_frame = small_jpeg
                        preview_frame_id += 1
                    if low_jpeg is not None:
                        low_frame = low_jpeg
                        low_frame_id += 1
                        low_frame_tick = time.monotonic()
                    last_frame_tick = time.monotonic()
                    last_frame_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    last_error = None
                    lock.notify_all()
                stop_requested.wait(max(0, 1 / FPS - (time.monotonic() - began)))
        except Exception as exc:
            LOG.warning("preview capture reconnecting: %s", exc)
            with lock:
                latest_frame = None
                preview_frame = None
                low_frame = None
                last_error = str(exc)
                lock.notify_all()
            stop_requested.wait(2)
        finally:
            if cap is not None:
                cap.release()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/frame.jpg':
            with lock:
                frame, identity = low_frame, low_frame_id
                ready = health()['healthy'] and frame is not None and time.monotonic() - low_frame_tick < 3
            if not ready:
                self.send_error(503, 'Camera is reconnecting')
                return
            etag = '"low-' + str(identity) + '"'
            unchanged = self.headers.get('If-None-Match') == etag
            self.send_response(304 if unchanged else 200)
            self.send_header('Content-Type', 'image/jpeg')
            if not unchanged:
                self.send_header('Content-Length', str(len(frame)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('ETag', etag)
            self.end_headers()
            if not unchanged:
                try:
                    self.wfile.write(frame)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return
        if path == "/health":
            state = health()
            body = json.dumps(state).encode()
            self.send_response(200 if state["healthy"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path not in {"/stream", "/preview"}:
            self.send_error(404)
            return
        if not health()["healthy"]:
            self.send_error(503, "Camera preview is reconnecting")
            return
        self.connection.settimeout(10)
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=timestampframe")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        previous = -1
        compact = path == "/preview"
        try:
            while not stop_requested.is_set():
                with lock:
                    lock.wait_for(lambda: (preview_frame_id if compact else frame_id) != previous or stop_requested.is_set(), timeout=2)
                    if stop_requested.is_set():
                        break
                    current, frame = (preview_frame_id, preview_frame) if compact else (frame_id, latest_frame)
                    if frame is None or time.monotonic() - last_frame_tick >= 10:
                        break
                    if current == previous:
                        # A short source pause is not EOF. Keep the stream open
                        # until the same freshness deadline as /health.
                        continue
                previous = current
                self.wfile.write(b"--timestampframe\r\nContent-Type: image/jpeg\r\n" +
                                 f"Content-Length: {len(frame)}\r\n\r\n".encode() + frame + b"\r\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def log_message(self, fmt, *args):
        pass


def main():
    global worker
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = threading.Thread(target=capture, name="timestamp-capture", daemon=True)
    worker.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True

    def stop(_signum, _frame):
        stop_requested.set()
        with lock:
            lock.notify_all()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop_requested.set()
        server.server_close()
        worker.join(timeout=7)


if __name__ == "__main__":
    main()
