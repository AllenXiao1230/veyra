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
from camera_state import atomic_json, media_guard, sync_directory
from camera_index import MediaIndex
from camera_jobs import ThumbnailJobs

TAIPEI = timezone(timedelta(hours=8))
_thumbnail_jobs = ThumbnailJobs()


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


def send_file(handler, path, cache="no-store", attachment=None):
    # Open before sending headers, so a concurrent trash operation cannot truncate the response.
    with pathlib.Path(path).open("rb") as stream:
        handler.send_response(200)
        handler.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        handler.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
        handler.send_header("Cache-Control", cache)
        handler.send_header("X-Content-Type-Options", "nosniff")
        if attachment:
            handler.send_header("Content-Disposition", 'attachment; filename="' + attachment + '"')
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
        self.recovery_issues = []
        self.index = MediaIndex(self.root)
        self.recover()

    def valid_name(self, name):
        if not isinstance(name, str) or not name or len(name) > 240 or name.startswith("."):
            raise ValueError("檔案名稱不正確")
        if any(c in name for c in ("/", "\\", "\0")) or any(ord(c) < 32 for c in name):
            raise ValueError("檔案名稱不正確")
        if self.kind == "recording":
            valid = re.fullmatch(r"recording-\d{8}-\d{6}(?:-[0-9a-f]{12})?\.(mp4|mkv)", name)
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

    def event_dates(self, details=False):
        if not self.db_path:
            return {}
        with closing(sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(events)")}
            if details and {"person_detected", "confidence", "metadata_json"}.issubset(columns):
                result = {}
                for row in db.execute("SELECT snapshot_name,id,person_detected,confidence,metadata_json FROM events WHERE snapshot_name != '' ORDER BY id"):
                    try:
                        metadata = json.loads(row[4] or "{}")
                        ready = metadata.get("ai_ready") if isinstance(metadata, dict) else None
                    except (ValueError, TypeError):
                        ready = None
                    result[row[0]] = {"event_id": row[1], "person_detected": bool(row[2]), "confidence": row[3], "ai_ready": ready}
                return result
            if details:
                return {}
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
                return datetime.strptime(path.stem[:25], "recording-%Y%m%d-%H%M%S").replace(tzinfo=TAIPEI)
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
        page, total, filtered, counts, has_more = self.index.page(self.valid_name, self.created_at, dates, date, cursor, limit)
        annotations = self.event_dates(details=True)
        for row in page:
            name = row["filename"]
            row["active"] = bool(self.active(name))
            if self.kind == "recording":
                match = re.search(r"-([0-9a-f]{12})\.mp4$", name)
                if match:
                    try:
                        session = json.loads((self.root / "sessions" / (match[1] + ".json")).read_text())
                        row.update(session_id=match[1], trigger=session.get("trigger"), event_ids=session.get("event_ids", []))
                    except (OSError, ValueError):
                        pass
                row["url"] = "/recordings-api/file/" + quote(name)
                row["playback_url"] = "/recordings-api/playback/" + quote(name)
                row["thumbnail_url"] = "/recordings-api/thumbnail/" + quote(name)
                if self.inspect:
                    try:
                        row.update(self.inspect(str(self.root / name), active=row["active"]))
                    except OSError:
                        row.update(health="missing", playable=False, health_message="檔案已移除，請重新整理")
            else:
                row.update(annotations.get(name, {}))
                row["url"] = row["thumbnail_url"] = "/ai-api/snapshots/" + quote(name)
        following = None
        if has_more and page:
            following = base64.urlsafe_b64encode(json.dumps(
                [page[-1]["created_at"], page[-1]["filename"]]).encode()).decode().rstrip("=")
        return {"items": page, "total": total, "filtered_total": filtered, "next_before": following,
                "dates": counts,
                "trash_count": len(self.list_trash()), "recovery_issues": self.recovery_issues}

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

    def _event_reference(self, receipt, restore):
        if not self.db_path:
            return
        with closing(sqlite3.connect(self.db_path, timeout=5)) as db, db:
            if restore:
                for event_id in receipt.get("event_ids", []):
                    db.execute("UPDATE events SET snapshot_name=? WHERE id=? AND snapshot_name=''",
                               (receipt["filename"], event_id))
            else:
                db.execute("UPDATE events SET snapshot_name='' WHERE snapshot_name=?", (receipt["filename"],))

    def _finish_receipt(self, folder):
        (folder / "manifest.json").unlink(missing_ok=True)
        sync_directory(folder)
        folder.rmdir()
        sync_directory(self.trash)

    def recover(self):
        """Reconcile durable rename intents after an interrupted process/transaction."""
        if not self.trash.is_dir():
            return []
        issues = []
        with self.lock, media_guard(self.root):
            for folder in self.trash.iterdir():
                if folder.is_symlink() or not folder.is_dir():
                    continue
                try:
                    data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
                    self.valid_name(data["filename"])
                    if data.get("kind") != self.kind or not re.fullmatch(r"[0-9a-f]{32}", folder.name):
                        raise ValueError("invalid receipt")
                    original = self.root / data["filename"]
                    saved = folder / ("media" + pathlib.Path(data["filename"]).suffix.lower())
                    if original.is_symlink() or saved.is_symlink():
                        raise ValueError("symbolic link in recovery")
                    phase = data.get("state", "trashed")
                    if phase == "purging":
                        if saved.is_file():
                            self._purge_file(folder, saved, data)
                        else:
                            self._finish_receipt(folder)
                        continue
                    if original.is_file() and not saved.exists():
                        identity = data.get("identity")
                        stat = original.stat()
                        same = identity == [stat.st_dev, stat.st_ino] and stat.st_size == data["size"]
                        if phase == "preparing_trash":
                            self._finish_receipt(folder)
                        elif phase == "restoring" and same:
                            self._event_reference(data, True)
                            self._finish_receipt(folder)
                        else:
                            raise ValueError("cannot prove ownership of restored file")
                    elif saved.is_file():
                        if not original.exists():
                            self._event_reference(data, False)
                        if phase != "trashed":
                            data["state"] = "trashed"
                            atomic_json(folder / "manifest.json", data)
                    else:
                        raise FileNotFoundError("receipt media missing")
                except (OSError, ValueError, KeyError, sqlite3.Error) as error:
                    issues.append({"token": folder.name, "error": str(error)})
        self.recovery_issues = issues
        return issues

    def list_trash(self):
        if not self.trash.exists():
            return []
        self.recover()
        result = []
        with self.lock:
            for folder in self.trash.iterdir():
                try:
                    _, _, receipt = self.receipt(folder.name)
                    result.append({k: receipt[k] for k in ("token", "filename", "deleted_at", "size", "kind")})
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(result, key=lambda r: r["deleted_at"], reverse=True)

    def trash_page(self, query):
        params = parse_qs(query)
        limit = int(params.get('limit', ['24'])[0])
        if not 1 <= limit <= 100:
            raise ValueError('分頁範圍不正確')
        rows = self.list_trash()
        cursor = params.get('before', [''])[0]
        offset = 0
        if cursor:
            positions = [i for i, r in enumerate(rows) if r['token'] == cursor]
            if not positions:
                raise ValueError('回收桶已更新，請重新整理')
            offset = positions[0] + 1
        page = rows[offset:offset + limit]
        return {'items': page, 'total': len(rows), 'next_before': page[-1]['token'] if page and offset + limit < len(rows) else None}

    def move_to_trash(self, name):
        with self.lock, media_guard(self.root):
            source = self.file(name)
            if self.active(name):
                raise MediaConflict("此影片正在錄影，請完成錄影後再刪除")
            token = uuid.uuid4().hex
            self.trash.mkdir(mode=0o700, parents=True, exist_ok=True)
            folder = self.trash / token
            folder.mkdir(mode=0o700)
            destination = folder / ("media" + source.suffix.lower())
            stat = source.stat()
            receipt = {"token": token, "kind": self.kind, "filename": name, "state": "preparing_trash",
                       "identity": [stat.st_dev, stat.st_ino],
                       "deleted_at": datetime.now(timezone.utc).isoformat(), "size": stat.st_size,
                       "event_ids": []}
            db = sqlite3.connect(self.db_path, timeout=5) if self.db_path else None
            try:
                if db:
                    db.execute("BEGIN IMMEDIATE")
                    receipt["event_ids"] = [r[0] for r in db.execute("SELECT id FROM events WHERE snapshot_name=?", (name,))]
                atomic_json(folder / "manifest.json", receipt)
                source.rename(destination)
                sync_directory(source.parent)
                sync_directory(destination.parent)
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
            receipt["state"] = "trashed"
            atomic_json(folder / "manifest.json", receipt)
            return {"ok": True, "token": token, "filename": name, "message": "已移到回收桶"}

    def restore(self, token):
        with self.lock, media_guard(self.root):
            folder, source, receipt = self.receipt(token)
            destination = self.root / receipt["filename"]
            if destination.exists() or destination.is_symlink():
                raise MediaConflict("同名檔案已存在，無法覆蓋復原")
            receipt["state"] = "restoring"
            stat = source.stat()
            receipt["identity"] = [stat.st_dev, stat.st_ino]
            atomic_json(folder / "manifest.json", receipt)
            db = sqlite3.connect(self.db_path, timeout=5) if self.db_path else None
            try:
                if db:
                    db.execute("BEGIN IMMEDIATE")
                source.rename(destination)
                sync_directory(source.parent)
                sync_directory(destination.parent)
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
                receipt["state"] = "trashed"
                atomic_json(folder / "manifest.json", receipt)
                raise
            finally:
                if db:
                    db.close()
            self._finish_receipt(folder)
            return {"ok": True, "filename": receipt["filename"], "message": "已復原"}

    def _purge_file(self, folder, source, receipt):
        stat = source.stat()
        if receipt.get('identity') != [stat.st_dev, stat.st_ino] or stat.st_size != receipt['size']:
            raise MediaConflict('無法確認回收桶檔案身分，已保留')
        key = hashlib.sha256(f"{receipt['filename']}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
        (self.root / '.thumbnails' / (key + '.jpg')).unlink(missing_ok=True)
        if self.kind == 'recording':
            from camera_playback import CACHE
            identity = (str(self.root / receipt['filename']), stat.st_mtime_ns, stat.st_size)
            key = hashlib.sha256(repr(identity).encode()).hexdigest()
            (CACHE / (key + '.mp4')).unlink(missing_ok=True)
        source.unlink()
        sync_directory(folder)
        self._finish_receipt(folder)

    def purge(self, token, confirmation):
        if confirmation != '永久刪除':
            raise ValueError('請輸入「永久刪除」確認無法復原的操作')
        with self.lock, media_guard(self.root):
            folder, source, receipt = self.receipt(token)
            receipt['state'] = 'purging'
            atomic_json(folder / 'manifest.json', receipt)
            self._purge_file(folder, source, receipt)
        return {'ok': True, 'token': token}

    def batch(self, payload):
        action, items = payload.get('action'), payload.get('items')
        if action not in ('trash', 'restore', 'purge') or not isinstance(items, list) or not 1 <= len(items) <= 100 or any(not isinstance(i, str) for i in items):
            raise ValueError('每次請選取 1–100 個有效項目')
        if action == 'purge' and payload.get('confirmation') != '永久刪除':
            raise ValueError('永久刪除尚未確認')
        results = []
        for item in dict.fromkeys(items):
            try:
                result = self.move_to_trash(item) if action == 'trash' else self.restore(item) if action == 'restore' else self.purge(item, payload['confirmation'])
                results.append({'item': item, 'ok': True, 'result': result})
            except (OSError, ValueError, MediaConflict, sqlite3.Error) as error:
                results.append({'item': item, 'ok': False, 'error': str(error)})
        return {'results': results, 'succeeded': sum(r['ok'] for r in results), 'failed': sum(not r['ok'] for r in results)}

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
        def generate():
            if target.is_file():
                return target
            seek = [] if self.kind == 'snapshot' else ['-ss', '0.2']
            result = subprocess.run(["/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", '-threads','1','-filter_threads','1',
                                     *seek, "-i", str(path), "-frames:v", "1", "-vf", "scale=480:-2",
                                     "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                                    capture_output=True, timeout=12)
            if result.returncode or not result.stdout.startswith(b"\xff\xd8"):
                raise ValueError("無法產生縮圖，原檔仍可下載")
            temporary = cache / (digest + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_bytes(result.stdout)
            os.replace(temporary, target)
            return target
        try:
            return _thumbnail_jobs.get(str(target), generate)
        except (RuntimeError, TimeoutError) as error:
            raise MediaConflict("縮圖處理中，請稍後重新整理") from error
