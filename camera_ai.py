#!/usr/bin/env python3
"""Loopback-only motion and optional person-event service for Camera Monitor.

The service deliberately consumes uStreamer over HTTP instead of touching the
camera device, so it cannot disrupt uStreamer or the existing recorder.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import threading
import time
import sys
sys.path.insert(0, "/opt/camera-common")
from camera_runtime import MJPEGCapture
from camera_media import MediaLibrary, media_error, read_body, send_file
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import cv2
import numpy as np


LOG = logging.getLogger("camera-ai")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_env(path: Path) -> dict[str, str]:
    values = dict(os.environ)
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def int_value(values: dict[str, str], name: str, default: int, minimum: int = 0) -> int:
    try:
        result = int(values.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, result)


def float_value(values: dict[str, str], name: str, default: float, minimum: float = 0.0) -> float:
    try:
        result = float(values.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, result)


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    stream_url: str
    data_dir: Path
    capture_width: int
    frame_interval_seconds: float
    warmup_frames: int
    motion_area_min: float
    motion_min_frames: int
    event_cooldown_seconds: float
    jpeg_quality: int
    person_detector: str
    onnx_model: Path
    person_threshold: float
    onnx_input_size: int

    @classmethod
    def from_file(cls, path: Path) -> "Settings":
        values = load_env(path)
        return cls(
            host=values.get("LISTEN_HOST", "127.0.0.1"),
            port=int_value(values, "LISTEN_PORT", 8092, 1),
            stream_url=values.get("STREAM_URL", "http://127.0.0.1:8080/stream"),
            data_dir=Path(values.get("DATA_DIR", "/var/lib/camera-ai")),
            capture_width=int_value(values, "CAPTURE_WIDTH", 640, 160),
            frame_interval_seconds=float_value(values, "FRAME_INTERVAL_SECONDS", 0.5, 0.05),
            warmup_frames=int_value(values, "WARMUP_FRAMES", 60, 0),
            motion_area_min=float_value(values, "MOTION_AREA_MIN", 2500.0, 1.0),
            motion_min_frames=int_value(values, "MOTION_MIN_FRAMES", 3, 1),
            event_cooldown_seconds=float_value(values, "EVENT_COOLDOWN_SECONDS", 15.0, 0.0),
            jpeg_quality=int_value(values, "JPEG_QUALITY", 88, 30),
            person_detector=values.get("PERSON_DETECTOR", "off").strip().lower(),
            onnx_model=Path(values.get("ONNX_MODEL", "/opt/camera-ai/models/person-detection.onnx")),
            person_threshold=float_value(values, "PERSON_THRESHOLD", 0.50, 0.05),
            onnx_input_size=int_value(values, "ONNX_INPUT_SIZE", 320, 160),
        )


class EventStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.snapshots_dir = data_dir / "snapshots"
        self.db_path = data_dir / "events.sqlite3"
        self.data_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    motion_area REAL NOT NULL,
                    person_detected INTEGER NOT NULL DEFAULT 0,
                    confidence REAL,
                    snapshot_name TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS events_created_at ON events(created_at DESC)")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def save_snapshot(self, frame: np.ndarray, quality: int) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        name = f"event-{stamp}.jpg"
        destination = self.snapshots_dir / name
        encoded, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not encoded:
            raise RuntimeError("Unable to encode event snapshot")
        temporary = destination.with_suffix(".jpg.tmp")
        temporary.write_bytes(data.tobytes())
        os.replace(temporary, destination)
        os.chmod(destination, 0o640)
        return name

    def add_event(
        self,
        event_type: str,
        motion_area: float,
        person_detected: bool,
        confidence: float | None,
        snapshot_name: str,
        metadata: dict[str, Any],
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO events
                    (created_at, event_type, motion_area, person_detected, confidence, snapshot_name, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    event_type,
                    motion_area,
                    int(person_detected),
                    confidence,
                    snapshot_name,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            return int(cursor.lastrowid)

    def list_events(self, limit: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id, created_at, event_type, motion_area, person_detected,
                       confidence, snapshot_name, metadata_json
                FROM events ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        events = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
                if not isinstance(metadata, dict):
                    metadata = {}
            except (TypeError, ValueError):
                metadata = {}

            snapshot_name = str(row["snapshot_name"] or "")
            has_snapshot = bool(snapshot_name and (self.snapshots_dir / snapshot_name).is_file())
            events.append({
                "id": row["id"],
                "created_at": row["created_at"],
                "event_type": row["event_type"],
                "motion_area": round(float(row["motion_area"]), 1),
                "person_detected": bool(row["person_detected"]),
                "confidence": round(float(row["confidence"]), 3)
                    if row["confidence"] is not None else None,
                "snapshot_url": (
                    f"/events/{row['id']}/snapshot" if has_snapshot else None
                ),
                "metadata": metadata,
                "source": metadata.get("source", "server"),
            })
        return events

    def snapshot_for_event(self, event_id: int) -> Path | None:
        with self.connect() as db:
            row = db.execute("SELECT snapshot_name FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        candidate = self.snapshots_dir / str(row["snapshot_name"])
        try:
            candidate.resolve().relative_to(self.snapshots_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None


class OnnxPersonDetector:
    """Optional detector for COCO-compatible YOLOv5/YOLOv8-style ONNX models."""

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.person_detector == "onnx"
        self.ready = False
        self.reason = "disabled by configuration"
        self.threshold = settings.person_threshold
        self.size = settings.onnx_input_size
        self.net: cv2.dnn_Net | None = None
        if not self.enabled:
            return
        if not settings.onnx_model.is_file():
            self.reason = f"model not found: {settings.onnx_model}"
            return
        try:
            self.net = cv2.dnn.readNetFromONNX(str(settings.onnx_model))
            self.ready = True
            self.reason = "ready"
        except cv2.error as exc:
            self.reason = f"model load failed: {exc}"[-240:]

    def detect(self, frame: np.ndarray) -> tuple[bool, float | None, int]:
        if not self.ready or self.net is None:
            return False, None, 0
        height, width = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(frame, 1.0 / 255.0, (self.size, self.size), swapRB=True, crop=False)
        self.net.setInput(blob)
        output = self.net.forward()
        rows = np.squeeze(output)
        if rows.ndim == 1:
            rows = rows.reshape(1, -1)
        if rows.ndim != 2:
            return False, None, 0
        # YOLOv8 exports are often (84, 8400); YOLOv5-style exports are (25200, 85).
        if rows.shape[0] in (84, 85) and rows.shape[1] > rows.shape[0]:
            rows = rows.T

        boxes: list[list[int]] = []
        scores: list[float] = []
        for row in rows:
            if row.size == 6:
                # Common post-NMS layout: x1, y1, x2, y2, score, class_id.
                if int(round(float(row[5]))) != 0:
                    continue
                score = float(row[4])
                x1, y1, x2, y2 = (float(value) for value in row[:4])
                x = int(x1 * width / self.size)
                y = int(y1 * height / self.size)
                box_width = int((x2 - x1) * width / self.size)
                box_height = int((y2 - y1) * height / self.size)
            elif row.size >= 84:
                x_center, y_center, box_width_raw, box_height_raw = (float(value) for value in row[:4])
                if row.size == 84:  # YOLOv8: first class score is COCO person.
                    score = float(row[4])
                else:  # YOLOv5: objectness multiplied by COCO person probability.
                    score = float(row[4]) * float(row[5])
                x = int((x_center - box_width_raw / 2) * width / self.size)
                y = int((y_center - box_height_raw / 2) * height / self.size)
                box_width = int(box_width_raw * width / self.size)
                box_height = int(box_height_raw * height / self.size)
            else:
                continue
            if score >= self.threshold and box_width > 2 and box_height > 2:
                boxes.append([x, y, box_width, box_height])
                scores.append(score)

        if not boxes:
            return False, None, 0
        kept = cv2.dnn.NMSBoxes(boxes, scores, self.threshold, 0.45)
        if len(kept) == 0:
            return False, None, 0
        indexes = np.array(kept).reshape(-1).tolist()
        return True, max(scores[index] for index in indexes), len(indexes)


class MotionAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.subtractor = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=20, detectShadows=False)
        self.frames_seen = 0

    def inspect(self, frame: np.ndarray) -> tuple[bool, float]:
        foreground = self.subtractor.apply(frame)
        foreground = cv2.morphologyEx(foreground, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        foreground = cv2.dilate(foreground, None, iterations=2)
        contours, _ = cv2.findContours(foreground, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        largest = max((cv2.contourArea(contour) for contour in contours), default=0.0)
        self.frames_seen += 1
        active = self.frames_seen > self.settings.warmup_frames and largest >= self.settings.motion_area_min
        return active, float(largest)


class DetectorService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = EventStore(settings.data_dir)
        self.media = MediaLibrary(self.store.snapshots_dir, "snapshot", self.store.db_path)
        self.person_detector = OnnxPersonDetector(settings)
        self.stop_requested = threading.Event()
        self.state_lock = threading.Lock()
        self.last_frame_tick = 0.0
        self.recoveries = 0
        self.state: dict[str, Any] = {
            "running": False,
            "stream_connected": False,
            "last_frame_at": None,
            "last_event_at": None,
            "last_error": None,
            "person_detector": self.person_detector.reason,
        }

    def update_state(self, **values: Any) -> None:
        with self.state_lock:
            if values.get("last_frame_at"):
                self.last_frame_tick = time.monotonic()
            self.state.update(values)

    def status(self) -> dict[str, Any]:
        with self.state_lock:
            result = dict(self.state)
            age = time.monotonic() - self.last_frame_tick if self.last_frame_tick else None
            result["frame_age_seconds"] = round(age, 2) if age is not None else None
            result["healthy"] = bool(result["running"] and result["stream_connected"] and age is not None and age < 15)
            result["recoveries"] = self.recoveries
        result["person_detector_enabled"] = self.person_detector.ready
        result["stream_url"] = self.settings.stream_url
        return result

    def supervise(self) -> None:
        delay = 1.0
        while not self.stop_requested.is_set():
            started = time.monotonic()
            try:
                self.run()
            except Exception as exc:
                LOG.exception("detector initialization failed")
                self.update_state(running=False, stream_connected=False, last_error=str(exc))
            if self.stop_requested.is_set():
                break
            self.recoveries += 1
            LOG.error("restarting detector worker after %.1f seconds", delay)
            self.stop_requested.wait(delay)
            delay = 1.0 if time.monotonic() - started > 60 else min(30.0, delay * 2)

    def run(self) -> None:
        analyzer = MotionAnalyzer(self.settings)
        capture: MJPEGCapture | None = None
        consecutive_motion = 0
        last_event_monotonic = 0.0
        self.update_state(running=True)
        try:
            while not self.stop_requested.is_set():
                if capture is None:
                    capture = MJPEGCapture(self.settings.stream_url)
                    if not capture.isOpened():
                        capture.release()
                        capture = None
                        self.update_state(stream_connected=False, last_error="Unable to open uStreamer MJPEG stream")
                        self.stop_requested.wait(3)
                        continue
                    self.update_state(stream_connected=True, last_error=None)

                success, source_frame = capture.read()
                if not success or source_frame is None:
                    capture.release()
                    capture = None
                    self.update_state(stream_connected=False, last_error="uStreamer MJPEG stream ended; reconnecting")
                    self.stop_requested.wait(1)
                    continue

                height, width = source_frame.shape[:2]
                target_width = min(self.settings.capture_width, width)
                if target_width < width:
                    target_height = max(1, int(height * target_width / width))
                    frame = cv2.resize(source_frame, (target_width, target_height), interpolation=cv2.INTER_AREA)
                else:
                    frame = source_frame
                active, motion_area = analyzer.inspect(frame)
                self.update_state(stream_connected=True, last_frame_at=utc_now(), last_error=None)
                consecutive_motion = consecutive_motion + 1 if active else 0

                now = time.monotonic()
                if consecutive_motion >= self.settings.motion_min_frames and now - last_event_monotonic >= self.settings.event_cooldown_seconds:
                    person, confidence, person_count = self.person_detector.detect(frame)
                    event_type = "person" if person else "motion"
                    # Keep a just-written snapshot and its event indivisible to trash requests.
                    with self.media.lock:
                        snapshot_name = self.store.save_snapshot(frame, self.settings.jpeg_quality)
                        event_id = self.store.add_event(
                            event_type=event_type,
                            motion_area=motion_area,
                            person_detected=person,
                            confidence=confidence,
                            snapshot_name=snapshot_name,
                            metadata={"person_count": person_count, "capture_width": int(frame.shape[1])},
                        )
                    LOG.info("stored %s event id=%s motion_area=%.1f", event_type, event_id, motion_area)
                    last_event_monotonic = now
                    self.update_state(last_event_at=utc_now())
                    consecutive_motion = 0
                self.stop_requested.wait(self.settings.frame_interval_seconds)
        except Exception as exc:  # Keep the HTTP health endpoint alive for diagnostics.
            LOG.exception("detector stopped unexpectedly")
            self.update_state(last_error=str(exc), stream_connected=False)
        finally:
            if capture is not None:
                capture.release()
            self.update_state(running=False)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "CameraAI/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    @property
    def service(self) -> DetectorService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_snapshot(self, path: Path) -> None:
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "private, max-age=60")
        self.end_headers()
        try:
            with path.open("rb") as image:
                while chunk := image.read(1024 * 1024):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:  # noqa: N802
        # Scheme B: reject cached legacy clients before reading or storing a body.
        path = urlparse(self.path).path
        if path in {"/trash", "/restore"}:
            try:
                data = read_body(self)
                result = (self.service.media.move_to_trash(data.get("filename")) if path == "/trash" else
                          self.service.media.restore(data.get("token")))
                self.send_json(200, result)
            except Exception as exc:
                media_error(self, exc)
            return
        self.send_json(
            HTTPStatus.GONE if urlparse(self.path).path == "/events/browser-motion" else HTTPStatus.NOT_FOUND,
            {"error": "Browser motion reporting is disabled; events are generated by the server"},
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/library", "/trash"} or parsed.path.startswith("/snapshots/"):
            try:
                if parsed.path == "/library":
                    self.send_json(200, self.service.media.list(parsed.query))
                elif parsed.path == "/trash":
                    self.send_json(200, {"items": self.service.media.list_trash()})
                else:
                    send_file(self, self.service.media.file(unquote(parsed.path[len("/snapshots/"):])))
            except Exception as exc:
                media_error(self, exc)
            return
        if parsed.path in {"/status", "/health"}:
            status = self.service.status()
            if parsed.path == "/health":
                self.send_json(HTTPStatus.OK if status["healthy"] else HTTPStatus.SERVICE_UNAVAILABLE, status)
                return
            self.send_json(HTTPStatus.OK, self.service.status())
            return
        if parsed.path == "/events":
            raw_limit = parse_qs(parsed.query).get("limit", ["30"])[0]
            try:
                limit = max(1, min(int(raw_limit), 100))
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "limit must be an integer"})
                return
            self.send_json(HTTPStatus.OK, {"events": self.service.store.list_events(limit)})
            return
        pieces = parsed.path.split("/")
        if len(pieces) == 4 and pieces[1] == "events" and pieces[3] == "snapshot":
            try:
                event_id = int(pieces[2])
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid event id"})
                return
            snapshot = self.service.store.snapshot_for_event(event_id)
            if snapshot is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "snapshot not found"})
                return
            self.send_snapshot(snapshot)
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_file(Path(os.environ.get("CAMERA_AI_CONFIG", "/etc/camera-ai/camera-ai.env")))
    if settings.host not in {"127.0.0.1", "::1"}:
        raise RuntimeError("LISTEN_HOST must remain loopback-only")
    service = DetectorService(settings)
    worker = threading.Thread(target=service.supervise, name="camera-ai-detector", daemon=True)
    worker.start()
    server = ThreadingHTTPServer((settings.host, settings.port), ApiHandler)
    server.daemon_threads = True
    server.service = service  # type: ignore[attr-defined]

    def stop(_signum: int, _frame: Any) -> None:
        service.stop_requested.set()
        # shutdown() must run outside serve_forever's thread to avoid a
        # signal-handler deadlock during systemd stop.
        threading.Thread(target=server.shutdown, name="camera-ai-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOG.info("Camera AI API listening on %s:%s", settings.host, settings.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        service.stop_requested.set()
        worker.join(timeout=8)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
