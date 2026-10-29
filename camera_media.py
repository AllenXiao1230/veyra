"""Dated media catalog, stat-keyed thumbnails and reversible same-volume trash."""
import base64
import hashlib
import json
import mimetypes
import os
import pathlib
import re
import sqlite3
import subprocess
import threading
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote

TAIPEI = timezone(timedelta(hours=8))
_thumbnail_slots = threading.BoundedSemaphore(2)


class MediaConflict(Exception):
    pass


def read_body(handler):
    origin = handler.headers.get("Origin")
    from urllib.parse import urlparse
    if origin and (urlparse(origin).scheme not in {"http", "https"} or
                   urlparse(origin).netloc.lower() != handler.headers.get("Host", "").lower()):
        raise PermissionError("拒絕跨網站操作")
    length = int(handler.headers.get("Content-Length", "0"))
    if not 0 <= length <= 4096:
        raise ValueError("請求長度不正確")
    if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ValueError("請使用 JSON 格式")
    data = json.loads(handler.rfile.read(length).decode("utf-8") if length else "{}")
    if not isinstance(data, dict):
        raise ValueError("請提供 JSON 物件")
    return data


def media_error(handler, error):
    code = (409 if isinstance(error, MediaConflict) else
            403 if isinstance(error, PermissionError) else
            404 if isinstance(error, FileNotFoundError) else
            400 if isinstance(error, (ValueError, UnicodeError)) else 503)
    handler.send_json(code, {"error": str(error) if code != 503 else "媒體操作暫時失敗，請稍後重試"})


def send_file(handler, path, cache="no-store"):
    # Open before sending headers, so a concurrent trash operation cannot truncate the response.
    with pathlib.Path(path).open("rb") as stream:
        handler.send_response(200)
        handler.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        handler.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
        handler.send_header("Cache-Control", cache)
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        try:
            while chunk := stream.read(65536):
                handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


class MediaLibrary:
    def __init__(self, root, kind, db_path=None, active=None, inspect=None):
        self.root = pathlib.Path(root)
        self.kind = kind
        self.trash = self.root.parent / "trash" if kind == "snapshot" else self.root / ".trash"
        self.db_path = pathlib.Path(db_path) if db_path else None
        self.active = active or (lambda _: False)
        self.inspect = inspect
        self.lock = threading.RLock()

    def valid_name(self, name):
        if not isinstance(name, str) or not name or len(name) > 240 or name.startswith("."):
            raise ValueError("檔案名稱不正確")
        if any(c in name for c in ("/", "\\", "\0")) or any(ord(c) < 32 for c in name):
            raise ValueError("檔案名稱不正確")
        if self.kind == "recording":
            valid = re.fullmatch(r"recording-\d{8}-\d{6}\.(mp4|mkv)", name)
        else:
            valid = pathlib.Path(name).suffix.lower() in {".jpg", ".jpeg", ".png"}
        if not valid:
            raise ValueError("不支援的媒體檔案")
        return name

    def file(self, name):
        path = self.root / self.valid_name(name)
        if path.is_symlink() or path.resolve().parent != self.root.resolve():
            raise ValueError("檔案路徑不正確")
        if not path.is_file():
            raise FileNotFoundError("找不到檔案，可能已移至回收桶")
        return path

    def event_dates(self):
        if not self.db_path:
            return {}
        with closing(sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)) as db:
            return {name: stamp for name, stamp in db.execute(
                "SELECT snapshot_name, created_at FROM events WHERE snapshot_name != '' ORDER BY id")}

    def created_at(self, path, dates):
        try:
            if path.name in dates:
                stamp = datetime.fromisoformat(dates[path.name].replace("Z", "+00:00"))
                return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
            if self.kind == "snapshot" and re.fullmatch(r"event-\d{8}T\d{6}_\d{6}Z.jpg", path.name):
                return datetime.strptime(path.stem, "event-%Y%m%dT%H%M%S_%fZ").replace(tzinfo=timezone.utc)
            if self.kind == "recording":
                return datetime.strptime(path.stem, "recording-%Y%m%d-%H%M%S").replace(tzinfo=TAIPEI)
        except (ValueError, TypeError):
            pass
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)

    def list(self, query=""):
        params = parse_qs(query)
        limit = int(params.get("limit", ["48"])[0])
        if not 1 <= limit <= 100:
            raise ValueError("每次載入數量必須是 1–100")
        date = params.get("date", [""])[0]
        if date:
            if datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
                raise ValueError("日期格式不正確")
        before = params.get("before", [""])[0]
        cursor = None
        if before:
            try:
                cursor = json.loads(base64.urlsafe_b64decode(before + "=" * (-len(before) % 4)))
                if not isinstance(cursor, list) or len(cursor) != 2 or not all(isinstance(x, str) for x in cursor):
                    raise ValueError()
            except Exception as exc:
                raise ValueError("分頁位置不正確，請重新整理") from exc
        dates = self.event_dates()
        rows = []
        for path in self.root.iterdir():
            try:
                self.valid_name(path.name)
                if path.is_symlink() or not path.is_file():
                    continue
                stamp = self.created_at(path, dates)
                rows.append({"filename": path.name, "created_at": stamp.astimezone(timezone.utc).isoformat(),
                             "date": stamp.astimezone(TAIPEI).date().isoformat(), "size": path.stat().st_size})
            except (ValueError, OSError):
                continue
        rows.sort(key=lambda r: (r["created_at"], r["filename"]), reverse=True)
        counts = Counter(r["date"] for r in rows)
        matched = [r for r in rows if not date or r["date"] == date]
        remaining = [r for r in matched if cursor is None or [r["created_at"], r["filename"]] < cursor]
        page = remaining[:limit]
        for row in page:
            name = row["filename"]
            row["active"] = bool(self.active(name))
            if self.kind == "recording":
                row["url"] = "/recordings-api/file/" + quote(name)
                row["thumbnail_url"] = "/recordings-api/thumbnail/" + quote(name)
                if self.inspect:
                    try:
                        row.update(self.inspect(str(self.root / name), active=row["active"]))
                    except OSError:
                        row.update(health="missing", playable=False, health_message="檔案已移除，請重新整理")
            else:
                row["url"] = row["thumbnail_url"] = "/ai-api/snapshots/" + quote(name)
        following = None
        if len(remaining) > limit and page:
            following = base64.urlsafe_b64encode(json.dumps(
                [page[-1]["created_at"], page[-1]["filename"]]).encode()).decode().rstrip("=")
        return {"items": page, "total": len(rows), "filtered_total": len(matched), "next_before": following,
                "dates": [{"date": day, "count": count} for day, count in sorted(counts.items(), reverse=True)],
                "trash_count": len(self.list_trash())}

    def receipt(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ValueError("回收桶項目不正確")
        folder = self.trash / token
        if folder.is_symlink() or folder.resolve().parent != self.trash.resolve():
            raise ValueError("回收桶路徑不正確")
        data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.valid_name(data["filename"])
        if data.get("kind") != self.kind:
            raise ValueError("回收桶類型不正確")
        file = folder / ("media" + pathlib.Path(data["filename"]).suffix.lower())
        if file.is_symlink() or not file.is_file():
            raise FileNotFoundError("回收桶檔案不存在")
        return folder, file, data

    def list_trash(self):
        if not self.trash.exists():
            return []
        result = []
        with self.lock:
            for folder in self.trash.iterdir():
                try:
                    _, _, receipt = self.receipt(folder.name)
                    result.append({k: receipt[k] for k in ("token", "filename", "deleted_at", "size", "kind")})
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(result, key=lambda r: r["deleted_at"], reverse=True)

    def move_to_trash(self, name):
        with self.lock:
            source = self.file(name)
            if self.active(name):
                raise MediaConflict("此影片正在錄影，請完成錄影後再刪除")
            token = uuid.uuid4().hex
            self.trash.mkdir(mode=0o700, parents=True, exist_ok=True)
            folder = self.trash / token
            folder.mkdir(mode=0o700)
            destination = folder / ("media" + source.suffix.lower())
            receipt = {"token": token, "kind": self.kind, "filename": name,
                       "deleted_at": datetime.now(timezone.utc).isoformat(), "size": source.stat().st_size,
                       "event_ids": []}
            db = sqlite3.connect(self.db_path, timeout=5) if self.db_path else None
            try:
                if db:
                    db.execute("BEGIN IMMEDIATE")
                    receipt["event_ids"] = [r[0] for r in db.execute("SELECT id FROM events WHERE snapshot_name=?", (name,))]
                (folder / "manifest.json").write_text(json.dumps(receipt), encoding="utf-8")
                source.rename(destination)
                if db:
                    db.execute("UPDATE events SET snapshot_name='' WHERE snapshot_name=?", (name,))
                    db.commit()
            except Exception:
                if db:
                    db.rollback()
                if destination.exists():
                    destination.rename(source)
                if (folder / "manifest.json").exists():
                    (folder / "manifest.json").unlink()
                folder.rmdir()
                raise
            finally:
                if db:
                    db.close()
            return {"ok": True, "token": token, "filename": name, "message": "已移到回收桶"}

    def restore(self, token):
        with self.lock:
            folder, source, receipt = self.receipt(token)
            destination = self.root / receipt["filename"]
            if destination.exists() or destination.is_symlink():
                raise MediaConflict("同名檔案已存在，無法覆蓋復原")
            db = sqlite3.connect(self.db_path, timeout=5) if self.db_path else None
            try:
                if db:
                    db.execute("BEGIN IMMEDIATE")
                source.rename(destination)
                if db:
                    for event_id in receipt["event_ids"]:
                        db.execute("UPDATE events SET snapshot_name=? WHERE id=? AND snapshot_name=''",
                                   (receipt["filename"], event_id))
                    db.commit()
            except Exception:
                if db:
                    db.rollback()
                if destination.exists():
                    destination.rename(source)
                raise
            finally:
                if db:
                    db.close()
            (folder / "manifest.json").unlink()
            folder.rmdir()
            return {"ok": True, "filename": receipt["filename"], "message": "已復原"}

    def thumbnail(self, name):
        with self.lock:
            path = self.file(name)
            if self.active(name):
                raise MediaConflict("錄影完成後產生縮圖")
            stat = path.stat()
            digest = hashlib.sha256(f"{name}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
            cache = self.root / ".thumbnails"
            cache.mkdir(mode=0o750, exist_ok=True)
            target = cache / (digest + ".jpg")
        if target.is_file():
            return target
        if not _thumbnail_slots.acquire(timeout=15):
            raise MediaConflict("縮圖處理中，請稍後重試")
        try:
            if target.is_file():
                return target
            result = subprocess.run(["/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                                     "-ss", "0.2", "-i", str(path), "-frames:v", "1", "-vf", "scale=480:-2",
                                     "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                                    capture_output=True, timeout=12)
            if result.returncode or not result.stdout.startswith(b"\xff\xd8"):
                raise ValueError("這段影片無法產生縮圖，原檔仍可下載")
            temporary = cache / (digest + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_bytes(result.stdout)
            os.replace(temporary, target)
            return target
        finally:
            _thumbnail_slots.release()
