"""Read-only event aggregation, durable favorites and bounded disk-based exports."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from contextlib import closing, ExitStack
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote
from camera_state import atomic_json, media_guard

TAIPEI = timezone(timedelta(hours=8))
VIDEO = re.compile(r'recording-\d{8}-\d{6}(?:-[0-9a-f]{12})?\.(mp4|mkv)')


class Timeline:
    def __init__(self, data, videos, state):
        self.data, self.videos, self.state = map(Path, (data, videos, state))
        self.state.mkdir(parents=True, exist_ok=True)
        self.preferences = self.state / 'event-preferences.sqlite3'
        self.exports = self.state / 'event-exports'
        self.exports.mkdir(exist_ok=True)
        self.lock, self.export_lock = threading.RLock(), threading.Lock()
        self.synced_at = 0
        with closing(self.prefs()) as db, db:
            db.executescript('''CREATE TABLE IF NOT EXISTS favorites(event_id INTEGER PRIMARY KEY, favorite INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, mtime TEXT);
              CREATE TABLE IF NOT EXISTS links(event_id INTEGER, filename TEXT, session_id TEXT, complete INTEGER,
              PRIMARY KEY(event_id, filename)); CREATE INDEX IF NOT EXISTS links_event ON links(event_id);''')
        for path in self.exports.glob('*.job.json'):
            try:
                job = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if job.get('state') in ('queued', 'running'):
                job.update(state='interrupted', error='服務曾重新啟動，請重新匯出')
                atomic_json(path, job)

    def prefs(self):
        return sqlite3.connect(self.preferences, timeout=10)

    def events(self):
        db = sqlite3.connect(f'file:{self.data / "events.sqlite3"}?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('ATTACH DATABASE ? AS prefs', (f'file:{self.preferences}?mode=ro',))
        return db

    def sync_links(self):
        with self.lock:
            if time.monotonic() - self.synced_at < 5:
                return
            with closing(self.prefs()) as db, db:
                for path in (self.videos / 'sessions').glob('*.json'):
                    if path.is_symlink() or not re.fullmatch(r'[0-9a-f]{12}', path.stem):
                        continue
                    try:
                        stat = path.stat()
                        session = json.loads(path.read_text())
                        if not isinstance(session, dict) or not isinstance(session.get('event_ids', []), list) or not isinstance(session.get('files', []), list):
                            continue
                    except (OSError, ValueError):
                        continue
                    identity = f'{stat.st_mtime_ns}:{stat.st_size}'
                    old = db.execute('SELECT mtime FROM sessions WHERE id=?', (path.stem,)).fetchone()
                    if old and old[0] == identity:
                        continue
                    db.execute('DELETE FROM links WHERE session_id=?', (path.stem,))
                    for event_id in session.get('event_ids', []):
                        if type(event_id) is not int or event_id <= 0:
                            continue
                        for name in session.get('files', []):
                            if isinstance(name, str) and VIDEO.fullmatch(name):
                                db.execute('INSERT OR REPLACE INTO links VALUES(?,?,?,?)',
                                           (event_id, name, path.stem, int(bool(session.get('ended_at')))))
                    db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)', (path.stem, identity))
            self.synced_at = time.monotonic()

    def decorate(self, rows):
        self.sync_links()
        result = []
        with closing(self.prefs()) as db:
            for row in rows:
                item = dict(row)
                name = item.pop('snapshot_name')
                snapshot = self.data / 'snapshots' / name
                valid = bool(name and Path(name).name == name and '\\' not in name and
                             Path(name).suffix.lower() in ('.jpg','.jpeg','.png') and
                             not (self.data / 'snapshots').is_symlink() and not snapshot.is_symlink() and snapshot.is_file())
                item.update(favorite=bool(item.get('favorite')), date=(datetime.fromisoformat(item['created_at']).astimezone(TAIPEI).date().isoformat()),
                            snapshot_name=name if valid else None,
                            snapshot_url='/ai-api/snapshots/' + quote(name) if valid else None, recordings=[])
                for filename, complete in db.execute('SELECT filename,complete FROM links WHERE event_id=? ORDER BY filename', (item['id'],)):
                    video = self.videos / filename
                    if not video.is_symlink() and video.is_file():
                        item['recordings'].append({'filename': filename, 'complete': bool(complete),
                            'url': '/recordings-api/file/' + quote(filename),
                            'playback_url': '/recordings-api/playback/' + quote(filename)})
                result.append(item)
        return result

    def list(self, query=''):
        params = parse_qs(query)
        limit = int(params.get('limit', ['24'])[0])
        before = int(params.get('before', ['0'])[0])
        if not 1 <= limit <= 100 or before < 0:
            raise ValueError('分頁範圍不正確')
        where, values = [], []
        if params.get('favorite', ['0'])[0] == '1':
            where.append('p.favorite=1')
        kind = params.get('type', ['all'])[0]
        if kind not in ('all', 'person', 'motion'):
            raise ValueError('事件分類不正確')
        if kind != 'all':
            where.append('e.person_detected=?')
            values.append(int(kind == 'person'))
        join = ' FROM events e LEFT JOIN prefs.favorites p ON p.event_id=e.id'
        base = ' AND '.join(where) or '1'
        with closing(self.events()) as db:
            dates = [{'date': r[0], 'count': r[1]} for r in db.execute(
                "SELECT date(e.created_at,'+8 hours'),count(*)" + join + ' WHERE ' + base + ' GROUP BY 1 ORDER BY 1 DESC', values)]
            date = params.get('date', [''])[0]
            if date:
                start = datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=TAIPEI).astimezone(timezone.utc)
                where.append('e.created_at>=? AND e.created_at<?')
                values += [start.isoformat(), (start + timedelta(days=1)).isoformat()]
            filtered = ' AND '.join(where) or '1'
            total = db.execute('SELECT count(*)' + join + ' WHERE ' + filtered, values).fetchone()[0]
            if before:
                where.append('e.id<?'); values.append(before)
            rows = db.execute('SELECT e.id,e.created_at,e.event_type,e.person_detected,e.confidence,e.snapshot_name,coalesce(p.favorite,0) favorite'
                + join + ' WHERE ' + (' AND '.join(where) or '1') + ' ORDER BY e.id DESC LIMIT ?', values + [limit + 1]).fetchall()
        return {'items': self.decorate(rows[:limit]), 'dates': dates, 'total': total,
                'next_before': str(rows[limit - 1]['id']) if len(rows) > limit else None}

    def selected(self, ids):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(i) is not int or i <= 0 for i in ids):
            raise ValueError('每次請選取 1–100 個事件')
        ids = list(dict.fromkeys(ids))
        with closing(self.events()) as db:
            rows = db.execute('SELECT e.id,e.created_at,e.event_type,e.person_detected,e.confidence,e.snapshot_name,coalesce(p.favorite,0) favorite'
                + ' FROM events e LEFT JOIN prefs.favorites p ON p.event_id=e.id WHERE e.id IN (' + ','.join('?' * len(ids)) + ') ORDER BY e.id DESC', ids).fetchall()
        if len(rows) != len(ids):
            raise ValueError('部分事件不存在，請重新整理')
        return self.decorate(rows)

    def favorite(self, ids, enabled):
        if type(enabled) is not bool:
            raise ValueError('收藏狀態不正確')
        self.selected(ids)
        with self.lock, closing(self.prefs()) as db, db:
            for event_id in ids:
                if enabled:
                    db.execute('INSERT OR REPLACE INTO favorites VALUES(?,1)', (event_id,))
                else:
                    db.execute('DELETE FROM favorites WHERE event_id=?', (event_id,))
        return {'ok': True, 'count': len(set(ids))}

    def job(self, token):
        if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{32}', token):
            raise ValueError('匯出編號不正確')
        return json.loads((self.exports / (token + '.job.json')).read_text())

    def export(self, ids, format='zip', include_videos=False):
        if format not in ('zip', 'csv', 'json') or type(include_videos) is not bool:
            raise ValueError('匯出格式不正確')
        rows = self.selected(ids)
        if not self.export_lock.acquire(blocking=False):
            raise RuntimeError('已有匯出工作進行中，請稍後重試')
        token = uuid.uuid4().hex
        job = {'id': token, 'state': 'queued', 'format': format, 'count': len(rows), 'created_at': time.time(), 'expires_at': time.time() + 86400}
        path = self.exports / (token + '.job.json')
        try:
            atomic_json(path, job)
        except Exception:
            self.export_lock.release()
            raise
        def worker():
            target = self.exports / (token + '.data.' + format)
            temporary = target.with_suffix('.tmp')
            try:
                job['state'] = 'running'; atomic_json(path, job)
                # Export copies are reproducible, expire after 24h and share a 4GiB budget.
                for old in self.exports.glob('*.job.json'):
                    old_job = json.loads(old.read_text())
                    if old_job.get('expires_at', 0) < time.time():
                        old_id = old.name.split('.')[0]
                        if re.fullmatch(r'[0-9a-f]{32}', old_id):
                            for suffix in ('zip', 'csv', 'json', 'tmp'):
                                (self.exports / (old_id + '.data.' + suffix)).unlink(missing_ok=True)
                        if old.name != path.name:
                            old.unlink(missing_ok=True)
                used = sum(p.stat().st_size for p in self.exports.iterdir() if p.is_file())
                with ExitStack() as stack:
                    files = {}
                    if format == 'zip':
                        # Both roots use the same production flock; acquire it once.
                        with media_guard(self.data):
                            for row in rows:
                                candidates = [('snapshots/' + row['snapshot_name'], self.data / 'snapshots' / row['snapshot_name'])] if row['snapshot_name'] else []
                                if include_videos:
                                    candidates += [('recordings/' + v['filename'], self.videos / v['filename']) for v in row['recordings'] if v['complete']]
                                for name, source in candidates:
                                    if name not in files:
                                        try:
                                            parent_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                                            try:
                                                fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                                            finally:
                                                os.close(parent_fd)
                                            files[name] = stack.enter_context(os.fdopen(fd, 'rb'))
                                        except FileNotFoundError:
                                            pass
                    estimate = sum(os.fstat(f.fileno()).st_size for f in files.values()) + 4 * 1024 ** 2
                    if estimate > 2 * 1024 ** 3 or used + estimate > 4 * 1024 ** 3 or shutil.disk_usage(self.exports).free < estimate + 1024 ** 3:
                        raise RuntimeError('匯出空間不足或超過 2GiB，請減少選取事件或取消影片')
                    for row in rows:
                        row['created_at_local'] = datetime.fromisoformat(row['created_at']).astimezone(TAIPEI).isoformat(timespec='seconds')
                    text = io.StringIO(newline='')
                    writer = csv.DictWriter(text, fieldnames=['id','created_at_local','event_type','person_detected','confidence','favorite','snapshot_name'], extrasaction='ignore')
                    writer.writeheader(); writer.writerows(rows)
                    encoded_json = json.dumps({'timezone': 'Asia/Taipei', 'events': rows}, ensure_ascii=False, indent=2).encode()
                    if format == 'zip':
                        manifest = []
                        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                            archive.writestr('events.json', encoded_json)
                            archive.writestr('events.csv', b'\xef\xbb\xbf' + text.getvalue().encode())
                            for name, source in files.items():
                                before = os.fstat(source.fileno())
                                digest = hashlib.sha256()
                                with archive.open(name, 'w', force_zip64=True) as output:
                                    while chunk := source.read(1024 * 1024):
                                        output.write(chunk); digest.update(chunk)
                                after = os.fstat(source.fileno())
                                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                    raise RuntimeError('來源檔案仍在變動，請完成錄影後重試')
                                manifest.append({'path': name, 'size': before.st_size, 'sha256': digest.hexdigest()})
                            archive.writestr('manifest.json', json.dumps({'files': manifest, 'note': '未存在的快照及未完成的錄影不包含在封存內；完整事件紀錄保留於 events.json。'}, ensure_ascii=False))
                    else:
                        temporary.write_bytes(encoded_json if format == 'json' else b'\xef\xbb\xbf' + text.getvalue().encode())
                os.replace(temporary, target)
                job.update(state='complete', size=target.stat().st_size, download_url='/events-api/exports/' + token + '/download')
            except Exception as error:
                job.update(state='failed', error=str(error)[-300:])
            finally:
                temporary.unlink(missing_ok=True)
                try:
                    atomic_json(path, job)
                finally:
                    self.export_lock.release()
        threading.Thread(target=worker, name='event-export', daemon=True).start()
        return {'job': dict(job)}
