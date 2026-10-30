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
from camera_storage import reserve_space
from camera_deployment import deployment_active

TAIPEI = timezone(timedelta(hours=8))
VIDEO = re.compile(r'recording-\d{8}-\d{6}(?:-[0-9a-f]{12})?\.(mp4|mkv)')


class ExportCancelled(Exception):
    pass


class Timeline:
    def __init__(self, data, videos, state):
        self.data, self.videos, self.state = map(Path, (data, videos, state))
        self.state.mkdir(parents=True, exist_ok=True)
        self.preferences = self.state / 'event-preferences.sqlite3'
        self.exports = self.state / 'event-exports'
        self.exports.mkdir(exist_ok=True)
        self.lock, self.export_lock = threading.RLock(), threading.Lock()
        self.cancellations = {}
        self.synced_at = 0
        self.date_cache = {}
        with closing(self.prefs()) as db, db:
            db.executescript('''CREATE TABLE IF NOT EXISTS favorites(event_id INTEGER PRIMARY KEY, favorite INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, mtime TEXT);
              CREATE TABLE IF NOT EXISTS links(event_id INTEGER, filename TEXT, session_id TEXT, complete INTEGER,
              PRIMARY KEY(event_id, filename)); CREATE INDEX IF NOT EXISTS links_event ON links(event_id);''')
            db.execute('CREATE TABLE IF NOT EXISTS link_states(event_id INTEGER,filename TEXT,state TEXT,PRIMARY KEY(event_id,filename))')
            db.execute("CREATE TABLE IF NOT EXISTS event_details(event_id INTEGER PRIMARY KEY,note TEXT NOT NULL DEFAULT '',tags TEXT NOT NULL DEFAULT '[]',updated_at REAL NOT NULL)")
        for path in self.exports.glob('*.job.json'):
            try:
                job = json.loads(path.read_text())
                if not isinstance(job,dict) or type(job.get('expires_at')) not in (int,float):
                    raise ValueError('Invalid job record')
            except (OSError, ValueError):
                try: os.replace(path,path.with_suffix('.invalid'))
                except OSError: pass
                continue
            if job.get('state') in ('queued', 'running'):
                job.update(state='cancelled' if job.get('cancel_requested') else 'interrupted', error='服務曾重新啟動，請重新匯出')
                atomic_json(path, job)

    def prefs(self):
        return sqlite3.connect(self.preferences, timeout=10)

    def events(self):
        db = sqlite3.connect(f'file:{self.data / "events.sqlite3"}?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('ATTACH DATABASE ? AS prefs', (f'file:{self.preferences}?mode=ro',))
        columns = {r[1] for r in db.execute('PRAGMA table_info(events)')}
        self.ai_expression = ("CASE WHEN e.person_detected=1 THEN 'person' WHEN json_valid(e.metadata_json) AND json_extract(e.metadata_json,'$.ai_status')='pending' AND julianday(e.created_at)>julianday('now','-60 seconds') THEN 'pending' WHEN json_valid(e.metadata_json) "
                              "AND json_extract(e.metadata_json,'$.ai_ready')=1 THEN 'motion' ELSE 'unknown' END"
                              if 'metadata_json' in columns else "CASE WHEN e.person_detected=1 THEN 'person' ELSE 'unknown' END")
        return db

    def sync_links(self, force=False):
        with self.lock:
            if not force and time.monotonic() - self.synced_at < 5:
                return
            with closing(self.prefs()) as db, db:
                for path in (self.videos / 'sessions').glob('*.json'):
                    if path.is_symlink() or not re.fullmatch(r'[0-9a-f]{12}', path.stem):
                        continue
                    try:
                        stat = path.stat()
                        identity = f'v2:{stat.st_mtime_ns}:{stat.st_size}'
                        old = db.execute('SELECT mtime FROM sessions WHERE id=?', (path.stem,)).fetchone()
                        if old and old[0] == identity:
                            continue
                        session = json.loads(path.read_text())
                        if not isinstance(session, dict) or not isinstance(session.get('event_ids', []), list) or not isinstance(session.get('files', []), list):
                            continue
                    except (OSError, ValueError):
                        continue
                    db.execute('DELETE FROM link_states WHERE (event_id,filename) IN (SELECT event_id,filename FROM links WHERE session_id=?)',(path.stem,))
                    db.execute('DELETE FROM links WHERE session_id=?', (path.stem,))
                    valid_names = [n for n in session['files'] if isinstance(n,str) and VIDEO.fullmatch(n)]
                    for event_id in session.get('event_ids', []):
                        if type(event_id) is not int or event_id <= 0:
                            continue
                        for name in session.get('files', []):
                            if isinstance(name, str) and VIDEO.fullmatch(name):
                                states = session.get('file_states', {})
                                state = states.get(name) if isinstance(states, dict) else None
                                if state not in ('complete','recording','damaged','missing','unknown'):
                                    state = 'complete' if session.get('ended_at') or name != max(valid_names) else 'recording'
                                db.execute('INSERT OR REPLACE INTO links(event_id,filename,session_id,complete) VALUES(?,?,?,?)',
                                           (event_id, name, path.stem, int(state == 'complete')))
                                db.execute('INSERT OR REPLACE INTO link_states VALUES(?,?,?)',(event_id,name,state))
                    db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)', (path.stem, identity))
            self.synced_at = time.monotonic()

    def decorate(self, rows):
        self.sync_links()
        result = []
        with closing(self.prefs()) as db:
            for row in rows:
                item = dict(row)
                name = item.pop('snapshot_name')
                snapshot = self.data / 'snapshots' / (name or '')
                valid = bool(name and Path(name).name == name and '\\' not in name and
                             Path(name).suffix.lower() in ('.jpg','.jpeg','.png') and
                             not (self.data / 'snapshots').is_symlink() and not snapshot.is_symlink() and snapshot.is_file())
                item.update(favorite=bool(item.get('favorite')), date=(datetime.fromisoformat(item['created_at']).astimezone(TAIPEI).date().isoformat()),
                            snapshot_name=name if valid else None,
                            snapshot_url='/ai-api/snapshots/' + quote(name) if valid else None, recordings=[])
                detail=db.execute('SELECT note,tags FROM event_details WHERE event_id=?',(item['id'],)).fetchone()
                item.update(note=detail[0] if detail else '',tags=json.loads(detail[1]) if detail else [])
                for filename, complete, state in db.execute("SELECT l.filename,l.complete,coalesce(s.state,CASE WHEN l.complete=1 THEN 'complete' ELSE 'recording' END) FROM links l LEFT JOIN link_states s ON s.event_id=l.event_id AND s.filename=l.filename WHERE l.event_id=? ORDER BY l.filename", (item['id'],)):
                    video = self.videos / filename
                    if not video.is_symlink() and video.is_file():
                        item['recordings'].append({'filename': filename, 'complete': bool(complete), 'state':state,
                            'url': '/recordings-api/file/' + quote(filename),
                            'playback_url': '/recordings-api/playback/' + quote(filename)})
                result.append(item)
        return result

    def list(self, query=''):
        params=parse_qs(query)
        value=lambda key,default='':params.get(key,[default])[0]
        limit=int(value('limit','24')); before=int(value('before','0'))
        if not 1<=limit<=100 or before<0:raise ValueError('分頁範圍不正確')
        kind=value('type','all'); order=value('sort','newest'); media=value('media','all')
        if kind not in ('all','person','motion','unknown') or order not in ('newest','oldest') or media not in ('all','snapshot','video','missing'):
            raise ValueError('事件篩選不正確')
        query_text=value('q').strip(); tag=value('tag').strip()
        if len(query_text)>100 or len(tag)>24:raise ValueError('搜尋內容太長')
        where=[]; values=[]
        if value('favorite')=='1':where.append('p.favorite=1')
        join=' FROM events e LEFT JOIN prefs.favorites p ON p.event_id=e.id LEFT JOIN prefs.event_details d ON d.event_id=e.id'
        with closing(self.events()) as db:
            if kind!='all':
                if kind=='unknown':where.append(self.ai_expression+" IN ('unknown','pending')")
                else:where.append(self.ai_expression+'=?');values.append(kind)
            if query_text:
                escaped=query_text.replace('!','!!').replace('%','!%').replace('_','!_')
                where.append("(CAST(e.id AS TEXT)=? OR coalesce(e.snapshot_name,'') LIKE ? ESCAPE '!' OR coalesce(d.note,'') LIKE ? ESCAPE '!' OR coalesce(d.tags,'') LIKE ? ESCAPE '!')")
                values += [query_text]+['%'+escaped+'%']*3
            if tag:
                where.append("EXISTS(SELECT 1 FROM json_each(coalesce(d.tags,'[]')) WHERE value=? COLLATE NOCASE)");values.append(tag)
            if value('confidence'):
                confidence=float(value('confidence'))
                if not 0<=confidence<=100:raise ValueError('AI 信心值須為 0–100')
                where.append('e.confidence>=?');values.append(confidence/100)
            start=end=None
            for key in ('from','until'):
                raw=value(key)
                if not raw:continue
                try:
                    moment=datetime.fromisoformat(raw)
                    if moment.tzinfo is None:moment=moment.replace(tzinfo=TAIPEI)
                    moment=moment.astimezone(timezone.utc)
                except (ValueError,OverflowError):raise ValueError('時間範圍不正確')
                if key=='from':start=moment;where.append('e.created_at>=?')
                else:
                    # datetime-local minute fields include the chosen end minute.
                    if len(raw)==16:moment+=timedelta(minutes=1)
                    end=moment;where.append('e.created_at<?')
                values.append(moment.isoformat())
            if start and end and start>=end:raise ValueError('結束時間須晚於開始時間')
            if media!='all':
                self.sync_links(force=True)
                presence={}
                def exists(kind,name):
                    key=(kind,name)
                    if key not in presence:
                        root=self.data/'snapshots' if kind=='snapshot' else self.videos
                        valid=isinstance(name,str) and name==Path(name).name and '/' not in name and chr(92) not in name
                        path=root/(name or '') if valid else root
                        presence[key]=bool(valid and not path.is_symlink() and path.is_file())
                    return int(presence[key])
                db.create_function('snapshot_exists',1,lambda name:exists('snapshot',name))
                db.create_function('video_exists',1,lambda name:exists('video',name))
                if media=='snapshot':where.append('snapshot_exists(e.snapshot_name)=1')
                elif media=='missing':where.append('snapshot_exists(e.snapshot_name)=0')
                else:where.append('EXISTS(SELECT 1 FROM prefs.links l WHERE l.event_id=e.id AND l.complete=1 AND video_exists(l.filename)=1)')
            base=' AND '.join(where) or '1'
            # Actual media existence cannot use a metadata-only result cache.
            signature=tuple((p.stat().st_mtime_ns,p.stat().st_size) if p.exists() else None for p in
                            (self.data/'events.sqlite3',self.data/'events.sqlite3-wal',self.preferences))
            key=(base,tuple(values),signature)
            cached=self.date_cache.get(key) if media=='all' else None
            if cached and time.monotonic()-cached[0]<30:dates=cached[1]
            else:
                dates=[{'date':r[0],'count':r[1]} for r in db.execute("SELECT date(e.created_at,'+8 hours'),count(*)"+join+' WHERE '+base+' GROUP BY 1 ORDER BY 1 DESC',values)]
                if len(self.date_cache)>16:self.date_cache.clear()
                if media=='all':self.date_cache[key]=(time.monotonic(),dates)
            date=value('date')
            if date:
                day=datetime.strptime(date,'%Y-%m-%d').replace(tzinfo=TAIPEI).astimezone(timezone.utc)
                where.append('e.created_at>=? AND e.created_at<?');values += [day.isoformat(),(day+timedelta(days=1)).isoformat()]
            total=sum(r['count'] for r in dates if not date or r['date']==date)
            if before:where.append('e.id'+('>?' if order=='oldest' else '<?'));values.append(before)
            rows=db.execute('SELECT e.id,e.created_at,e.event_type,e.person_detected,e.confidence,e.snapshot_name,coalesce(p.favorite,0) favorite,'+self.ai_expression+' ai_status'+join+
                ' WHERE '+(' AND '.join(where) or '1')+' ORDER BY e.id '+('ASC' if order=='oldest' else 'DESC')+' LIMIT ?',values+[limit+1]).fetchall()
        return {'items':self.decorate(rows[:limit]),'dates':dates,'total':total,
                'next_before':str(rows[limit-1]['id']) if len(rows)>limit else None}

    def annotate(self,event_id,note,tags):
        if type(event_id) is not int or not isinstance(note,str) or len(note)>1000 or not isinstance(tags,list) or len(tags)>10:
            raise ValueError('備註最多 1000 字、標籤最多 10 個')
        if any(not isinstance(t,str) or not t.strip() or len(t.strip())>24 or any(ord(c)<32 for c in t) for t in tags):
            raise ValueError('每個標籤須為 1–24 字')
        self.selected([event_id]);tags=list(dict.fromkeys(t.strip() for t in tags))
        with self.lock,media_guard(self.data),closing(self.prefs()) as db,db:
            db.execute('INSERT OR REPLACE INTO event_details VALUES(?,?,?,?)',(event_id,note.strip(),json.dumps(tags,ensure_ascii=False),time.time()))
        self.date_cache.clear()
        return {'ok':True,'note':note.strip(),'tags':tags}

    def selected(self, ids):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(i) is not int or i <= 0 for i in ids):
            raise ValueError('每次請選取 1–100 個事件')
        ids = list(dict.fromkeys(ids))
        with closing(self.events()) as db:
            rows = db.execute('SELECT e.id,e.created_at,e.event_type,e.person_detected,e.confidence,e.snapshot_name,coalesce(p.favorite,0) favorite,'+self.ai_expression+' ai_status'
                + ' FROM events e LEFT JOIN prefs.favorites p ON p.event_id=e.id WHERE e.id IN (' + ','.join('?' * len(ids)) + ') ORDER BY e.id DESC', ids).fetchall()
        if len(rows) != len(ids):
            raise ValueError('部分事件不存在，請重新整理')
        return self.decorate(rows)

    def favorite(self, ids, enabled):
        if type(enabled) is not bool:
            raise ValueError('收藏狀態不正確')
        self.selected(ids)
        with self.lock, media_guard(self.data), closing(self.prefs()) as db, db:
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

    def cleanup_exports(self, now=None, locked=False):
        if not locked and not self.export_lock.acquire(blocking=False):
            return {'busy':True}
        try:
            now = time.time() if now is None else now
            removed, damaged = 0, 0
            for old in self.exports.glob('*.job.json'):
                token = old.name.split('.')[0]
                if not re.fullmatch(r'[0-9a-f]{32}',token): continue
                try:
                    job = json.loads(old.read_text())
                    if not isinstance(job,dict) or type(job.get('expires_at')) not in (int,float): raise ValueError('invalid job')
                except (OSError,ValueError):
                    damaged += 1
                    try: os.replace(old,old.with_suffix('.invalid'))
                    except OSError: pass
                    continue
                if job.get('expires_at',0) >= now: continue
                for suffix in ('zip','csv','json','tmp'):
                    (self.exports/(token+'.data.'+suffix)).unlink(missing_ok=True)
                if now>job['expires_at']+7*86400:old.unlink(missing_ok=True)
                elif job.get('state')=='complete':job.update(state='expired');atomic_json(old,job)
                removed += 1
            for temporary in self.exports.glob('*.data.tmp'):
                if temporary.stat().st_mtime < now-3600:
                    temporary.unlink(missing_ok=True)
            for artifact in self.exports.glob('*.data.*'):
                token=artifact.name.split('.')[0]
                if (re.fullmatch(r'[0-9a-f]{32}',token) and artifact.suffix in ('.zip','.csv','.json')
                        and not (self.exports/(token+'.job.json')).exists() and artifact.stat().st_mtime < now-86400):
                    artifact.unlink(missing_ok=True)
            for invalid in self.exports.glob('*.job.invalid'):
                if invalid.stat().st_mtime < now-7*86400: invalid.unlink(missing_ok=True)
            return {'removed':removed,'damaged':damaged}
        finally:
            if not locked: self.export_lock.release()

    def cancel(self, token):
        with self.lock:
            job=self.job(token)
            if job.get('state') not in ('queued','running'):
                raise RuntimeError('工作已結束，無法取消')
            event=self.cancellations.get(token)
            if event is None:raise RuntimeError('工作已中斷，請重新整理')
            event.set();job['cancel_requested']=True;atomic_json(self.exports/(token+'.job.json'),job)
        return {'ok':True}

    def export(self, ids, format='zip', include_videos=False):
        if deployment_active():raise RuntimeError('系統更新中，請稍後再匯出。')
        if format not in ('zip', 'csv', 'json') or type(include_videos) is not bool:
            raise ValueError('匯出格式不正確')
        rows = self.selected(ids)
        if not self.export_lock.acquire(blocking=False):
            raise RuntimeError('已有匯出工作進行中，請稍後重試')
        if deployment_active():
            self.export_lock.release()
            raise RuntimeError('系統更新中，請稍後再匯出。')
        token = uuid.uuid4().hex
        job = {'id': token, 'state': 'queued', 'format': format, 'count': len(rows), 'ids':list(dict.fromkeys(ids)),
               'include_videos':include_videos,'done_bytes':0,'total_bytes':0,
               'created_at': time.time(), 'expires_at': time.time() + 86400}
        cancellation=threading.Event()
        with self.lock:self.cancellations[token]=cancellation
        path = self.exports / (token + '.job.json')
        try:
            with media_guard(self.data):
                if deployment_active():raise RuntimeError('系統更新中，請稍後再匯出。')
                atomic_json(path, job)
        except Exception:
            with self.lock:self.cancellations.pop(token,None)
            self.export_lock.release()
            raise
        def worker():
            target = self.exports / (token + '.data.' + format)
            temporary = target.with_suffix('.tmp')
            last_save=0
            def checkpoint(done=None):
                nonlocal last_save
                if cancellation.is_set():raise ExportCancelled('匯出已取消')
                if done is not None:job['done_bytes']=done
                if time.monotonic()-last_save>0.5:
                    atomic_json(path,job);last_save=time.monotonic()
            try:
                checkpoint()
                job['state'] = 'running'; atomic_json(path, job)
                # Export copies are reproducible, expire after 24h and share a 4GiB budget.
                self.cleanup_exports(locked=True)
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
                    stack.enter_context(reserve_space(self.state,self.exports,estimate))
                    job['total_bytes']=sum(os.fstat(f.fileno()).st_size for f in files.values())
                    for row in rows:
                        row['created_at_local'] = datetime.fromisoformat(row['created_at']).astimezone(TAIPEI).isoformat(timespec='seconds')
                    text = io.StringIO(newline='')
                    writer = csv.DictWriter(text, fieldnames=['id','created_at_local','event_type','person_detected','confidence','favorite','snapshot_name','note','tags'], extrasaction='ignore')
                    def csv_value(value):
                        if isinstance(value,list):value=', '.join(value)
                        return "'"+value if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')) else value
                    writer.writeheader(); writer.writerows({k:csv_value(row.get(k)) for k in writer.fieldnames} for row in rows)
                    encoded_json = json.dumps({'timezone': 'Asia/Taipei', 'events': rows}, ensure_ascii=False, indent=2).encode()
                    if format == 'zip':
                        manifest = []
                        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                            archive.writestr('events.json', encoded_json)
                            archive.writestr('events.csv', b'\xef\xbb\xbf' + text.getvalue().encode())
                            for name, source in files.items():
                                checkpoint()
                                before = os.fstat(source.fileno())
                                digest = hashlib.sha256()
                                with archive.open(name, 'w', force_zip64=True) as output:
                                    while chunk := source.read(1024 * 1024):
                                        output.write(chunk); digest.update(chunk)
                                        checkpoint(job['done_bytes']+len(chunk))
                                after = os.fstat(source.fileno())
                                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                    raise RuntimeError('來源檔案仍在變動，請完成錄影後重試')
                                manifest.append({'path': name, 'size': before.st_size, 'sha256': digest.hexdigest()})
                            archive.writestr('manifest.json', json.dumps({'files': manifest, 'note': '未存在的快照及未完成的錄影不包含在封存內；完整事件紀錄保留於 events.json。'}, ensure_ascii=False))
                    else:
                        content=encoded_json if format=='json' else b'\xef\xbb\xbf'+text.getvalue().encode()
                        job['total_bytes']=len(content);temporary.write_bytes(content);checkpoint(len(content))
                with self.lock:
                    checkpoint()
                    os.replace(temporary, target)
                    job.update(state='complete', size=target.stat().st_size, download_url='/events-api/exports/' + token + '/download')
                    atomic_json(path,job)
            except ExportCancelled:
                job.update(state='cancelled',error='匯出已取消')
            except Exception as error:
                job.update(state='failed', error=str(error)[-300:])
            finally:
                temporary.unlink(missing_ok=True)
                try:
                    atomic_json(path, job)
                finally:
                    with self.lock:self.cancellations.pop(token,None)
                    self.export_lock.release()
        threading.Thread(target=worker, name='event-export', daemon=True).start()
        return {'job': dict(job)}
