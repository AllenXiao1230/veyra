#!/usr/bin/python3
"""Loopback-only backup/event jobs and explicitly confirmed trash deletion."""
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.append("/srv/camera-monitor/app/common")
import camera_maintenance as maintenance
from camera_backups import BackupStore, verify_backup
from camera_media import read_body
from camera_state import atomic_json
from camera_storage import backup_policy, save_backup_policy
from camera_timeline import Timeline
from camera_media import send_file
from camera_storage import storage_summary
from camera_diagnostics import PROBE, read_state
from camera_notifications import Notifications
from camera_work import WorkCenter


class Jobs:
    def __init__(self, store, create=None):
        self.store = store
        self.create = create or maintenance.backup
        self.directory = store.state / "jobs"
        self.directory.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.Lock()
        self.audit_lock = threading.Lock()
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                if not isinstance(data,dict):raise ValueError('Invalid job')
                if data.get("state") in {"queued", "running"}:
                    data.update(state="interrupted", error="服務曾重新啟動，請重新執行工作", ended_at=time.time())
                    atomic_json(path, data)
            except (OSError, ValueError):
                try:os.replace(path,path.with_suffix('.invalid'))
                except OSError:pass
                continue

    def audit(self, action, result, name=None):
        with self.audit_lock:
            path = self.store.state / "operations.jsonl"
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                os.replace(path, path.with_suffix(".previous.jsonl"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": time.time(), "action": action, "name": name, "result": result}, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def recent(self):
        rows = []
        for path in sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
            try:
                rows.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
        return rows

    def get(self, job_id):
        if not isinstance(job_id,str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("工作編號不正確")
        return json.loads((self.directory / (job_id + ".json")).read_text())

    def submit(self, action, name=None):
        if action not in {"create", "verify", "recover"}:
            raise ValueError("不支援的工作")
        if action != "create":
            self.store.folder(name)
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("已有備份工作進行中")
        # Reserve the cross-process lock before acknowledging the request.
        guard = self.store.mutation()
        try:
            guard.__enter__()
        except Exception:
            self.lock.release()
            raise
        job = {"id": uuid.uuid4().hex, "action": action, "name": name,
               "state": "queued", "created_at": time.time(), "message": "等待執行", "done_bytes": 0, "total_bytes": 0}
        path = self.directory / (job["id"] + ".json")
        try:
            atomic_json(path, job)
            self.audit(action, "queued", name)
        except Exception:
            guard.__exit__(None, None, None)
            self.lock.release()
            raise
        def worker():
            last_save = 0
            def progress(message, done, total):
                nonlocal last_save
                job.update(message=message, done_bytes=done, total_bytes=total)
                if time.monotonic() - last_save >= 0.5 or done == total:
                    atomic_json(path, job)
                    last_save = time.monotonic()
            try:
                job.update(state="running", started_at=time.time())
                atomic_json(path, job)
                if action == "create":
                    result = {k: v for k, v in self.create(progress=progress).items() if k != "files"}
                elif action == "verify":
                    result = verify_backup(self.store.folder(name), progress)
                else:
                    result = self.store.recover_isolated(name, progress)
                job.update(state="complete", result=result, message="已完成")
            except Exception as error:
                job.update(state="failed", error=str(error)[-500:], message="工作失敗")
            finally:
                job["ended_at"] = time.time()
                try:
                    atomic_json(path, job)
                    self.audit(action, job["state"], name)
                finally:
                    guard.__exit__(None, None, None)
                    self.lock.release()
        threading.Thread(target=worker, name="backup-job", daemon=True).start()
        return {"ok": True, "job": dict(job)}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            if self.command != 'HEAD':
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def failure(self, error):
        status = 403 if isinstance(error, PermissionError) else 404 if isinstance(error, FileNotFoundError) else 400 if isinstance(error, (ValueError, UnicodeError)) else 409 if isinstance(error, RuntimeError) else 503
        if status == 503:
            logging.exception('Camera management request failed')
        message = '事件服務暫時無法讀取，請稍後重試' if self.path.startswith('/events/') else '備份服務暫時失敗，請稍後重試'
        self.send_json(status, {"error": str(error) if status != 503 else message})

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path.startswith('/events/exports/') and path.endswith('/download'):
            self.do_GET()
        else:
            self.send_json(404, {'error': '找不到下載路徑'})

    def do_GET(self):
        path = urlparse(self.path).path
        jobs, store = self.server.jobs, self.server.jobs.store
        try:
            if path=='/health':
                self.send_json(200,{'healthy':True,'service':'camera-backup-api'});return
            if path=='/notifications':
                from urllib.parse import parse_qs
                params=parse_qs(urlparse(self.path).query)
                self.send_json(200,self.server.notifications.list(int(params.get('before',['0'])[0]),
                    int(params.get('limit',['30'])[0]),params.get('unread',['0'])[0]=='1'))
                return
            if path=='/notifications/policy':
                self.send_json(200,self.server.notifications.policy());return
            if path=='/work':
                self.send_json(200,self.server.work.list(urlparse(self.path).query));return
            if path == '/probe':
                send_file(self,store.state/'transport-probe.bin')
                return
            if path == '/storage':
                self.send_json(200,read_state(store.state/'storage-summary.json',{'pending':True}))
                return
            if path.startswith('/events/'):
                timeline = self.server.timeline
                if path == '/events/list':
                    self.send_json(200, timeline.list(urlparse(self.path).query))
                elif path.startswith('/events/exports/'):
                    parts = path.split('/')
                    job = timeline.job(parts[3])
                    if len(parts) == 5 and parts[4] == 'download':
                        if job['state'] != 'complete' or job['expires_at'] < time.time():
                            raise ValueError('匯出尚未完成或已過期，請重新匯出')
                        send_file(self, timeline.exports / (job['id'] + '.data.' + job['format']),
                                  attachment='camera-events-' + job['id'][:12] + '.' + job['format'])
                    else:
                        self.send_json(200, {'job': job})
                else:
                    self.send_json(404, {'error': '找不到事件 API'})
                return
            if path == "/policy":
                self.send_json(200, backup_policy(store.state))
            elif path in {"/health", "/status"}:
                disk = shutil.disk_usage(store.root)
                data_disk = shutil.disk_usage(maintenance.DATA)
                self.send_json(200, {"healthy": True, "jobs": jobs.recent(), "backup_root": str(store.root),
                                     'diagnostics':read_state(store.state/'status.json'),
                                     'storage':read_state(store.state/'storage-summary.json',{'pending':True}),
                                     "external_configured": False,
                                     "same_device": store.root.stat().st_dev == maintenance.DATA.stat().st_dev,
                                     "free_bytes": disk.free, "total_bytes": disk.total,
                                     "data_free_bytes": data_disk.free, "latest": store.latest_name(),
                                     "retention_days": 0, "permanent_delete_available": True})
            elif path == "/backups":
                self.send_json(200, {"items": store.list(), "trash": store.list_trash(), "capacity": store.capacity()})
            elif path.startswith("/jobs/"):
                self.send_json(200, jobs.get(path[6:]))
            else:
                self.send_json(404, {"error": "找不到 API"})
        except Exception as error:
            self.failure(error)

    def do_POST(self):
        path = urlparse(self.path).path
        jobs, store = self.server.jobs, self.server.jobs.store
        try:
            data = read_body(self)
            if path=='/notifications/read':
                self.send_json(200,self.server.notifications.acknowledge(data.get('through')));return
            if path=='/notifications/policy':
                self.send_json(200,self.server.notifications.policy(data));return
            if path=='/work/download':
                self.send_json(201,self.server.work.register(data.get('kind'),data.get('filename')));return
            if path=='/work/cancel':
                self.send_json(200,self.server.timeline.cancel(data.get('id')));return
            if path=='/work/retry':
                self.send_json(202,self.server.work.retry(data.get('kind'),data.get('id')));return
            if path=='/events/annotate':
                self.send_json(200,self.server.timeline.annotate(data.get('id'),data.get('note'),data.get('tags')));return
            if path == '/events/favorite':
                self.send_json(200, self.server.timeline.favorite(data.get('ids'), data.get('favorite')))
                return
            if path == '/events/export':
                self.send_json(202, self.server.timeline.export(data.get('ids'), data.get('format', 'zip'), data.get('include_videos', False)))
                return
            if path == "/policy":
                with store.mutation():
                    result = save_backup_policy(store.state, data)
                    jobs.audit("policy", "complete")
                self.send_json(200, result)
                return
            if path in {"/create", "/verify", "/recover"}:
                self.send_json(202, jobs.submit(path[1:], data.get("name")))
                return
            if path in {"/trash", "/restore", "/purge"}:
                with store.mutation():
                    result = store.move_to_trash(data.get("name")) if path == "/trash" else store.purge(data.get('token'),data.get('confirmation')) if path == '/purge' else store.restore(data.get("token"))
                    jobs.audit(path[1:], "complete", data.get("name") or data.get("token"))
                self.send_json(200, result)
                return
            self.send_json(404, {"error": "找不到 API"})
        except Exception as error:
            self.failure(error)


def main():
    os.umask(0o077)
    store = BackupStore(maintenance.BACKUPS, maintenance.STATE)
    server = ThreadingHTTPServer(("127.0.0.1", 8095), Handler)
    server.daemon_threads = True
    server.jobs = Jobs(store)
    server.timeline = Timeline(maintenance.DATA, maintenance.RECORDINGS, maintenance.STATE)
    server.notifications=Notifications(store.state)
    server.work=WorkCenter(server.timeline,server.jobs,lambda:maintenance.http_json('http://127.0.0.1:8091/status'))
    probe = store.state/'transport-probe.bin'
    probe.write_bytes(PROBE); probe.chmod(0o600)
    maintenance_stop = threading.Event()
    def maintain():
        while not maintenance_stop.is_set():
            try:
                server.timeline.cleanup_exports()
                atomic_json(store.state/'storage-summary.json',storage_summary(maintenance.DATA,maintenance.RECORDINGS,store.root,store.state))
            except Exception:
                logging.exception('Derived storage maintenance failed')
            maintenance_stop.wait(60)
    threading.Thread(target=maintain,name='storage-maintenance',daemon=True).start()
    def stop(*args):
        maintenance_stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        maintenance_stop.set()
        server.server_close()
        # Jobs hold the maintenance lock until their durable result is saved.
        if server.jobs.lock.acquire(timeout=3500):
            server.jobs.lock.release()
        if server.timeline.export_lock.acquire(timeout=3500):
            server.timeline.export_lock.release()


if __name__ == "__main__":
    main()
