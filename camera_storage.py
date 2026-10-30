"""Non-destructive storage budgets; trash still counts toward occupied space."""
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from camera_state import atomic_json

SNAPSHOT_RESERVE = 2 * 1024 ** 3


def process_identity(pid):
    try:return Path('/proc',str(pid),'stat').read_text().rpartition(') ')[2].split()[19]
    except (OSError,IndexError):return None


class StoragePressure(RuntimeError):
    pass


@contextmanager
def reserve_space(state, root, estimate, reserve=1024**3):
    """Cross-process admission for backup/export copies on their shared disk."""
    state=Path(state); state.mkdir(parents=True,exist_ok=True)
    ledger=state/'space-reservations.json'; token=uuid.uuid4().hex
    def update(add):
        with (state/'storage.lock').open('a') as guard:
            if os.name!='nt':
                import fcntl
                fcntl.flock(guard,fcntl.LOCK_EX)
            try:
                try: rows=json.loads(ledger.read_text())
                except (OSError,ValueError): rows={}
                if not isinstance(rows,dict):raise StoragePressure('儲存預留紀錄格式錯誤，請檢查服務狀態。')
                rows={k:v for k,v in rows.items() if isinstance(v,dict) and v.get('expires',0)>time.time() and
                      (os.name=='nt' or (Path('/proc',str(v.get('pid'))).exists() and
                       (not v.get('start') or v['start']==process_identity(v.get('pid')))))}
                if add:
                    used=sum(v['bytes'] for v in rows.values())
                    if shutil.disk_usage(root).free < used+estimate+reserve:
                        raise StoragePressure('其他工作已保留儲存空間，請稍後再試或管理資料。')
                    rows[token]={'pid':os.getpid(),'start':process_identity(os.getpid()),'bytes':estimate,'expires':time.time()+21600}
                else: rows.pop(token,None)
                atomic_json(ledger,rows)
            finally:
                if os.name!='nt': fcntl.flock(guard,fcntl.LOCK_UN)
    update(True)
    try: yield
    finally: update(False)


def require_space(root, reserve=SNAPSHOT_RESERVE, extra=0):
    if shutil.disk_usage(root).free < reserve + extra:
        raise StoragePressure('儲存保留空間不足，已暫停新增快照；事件與錄影狀態仍保留。')


def tree_usage(root, exclude=()):
    count = logical = physical = 0
    seen = set()
    for directory, dirs, files in os.walk(root,followlinks=False):
        base = Path(directory)
        dirs[:] = [d for d in dirs if d not in exclude and not (base/d).is_symlink()]
        for name in files:
            path = base/name
            try:
                if path.is_symlink() or not path.is_file(): continue
                info = path.stat(); count += 1; logical += info.st_size
                key = (info.st_dev,info.st_ino)
                if key not in seen:
                    seen.add(key); physical += getattr(info,'st_blocks',0)*512 or info.st_size
            except FileNotFoundError:
                continue
    return {'files':count,'logical_bytes':logical,'physical_bytes':physical}


def storage_summary(data, recordings, backups, state, playback=Path('/var/cache/camera-monitor/playback')):
    data,recordings,backups,state = map(Path,(data,recordings,backups,state))
    categories = {'recordings':tree_usage(recordings,('.trash','.metadata')),
                  'snapshots':tree_usage(data/'snapshots'),
                  'snapshot_trash':tree_usage(data/'trash'), 'recording_trash':tree_usage(recordings/'.trash'),
                  'backups':tree_usage(backups), 'exports':tree_usage(state/'event-exports'),
                  'recoveries':tree_usage(state/'recoveries'), 'playback_cache':tree_usage(playback)}
    disk = shutil.disk_usage(data)
    now=time.time(); days=None
    try:
        previous=json.loads((state/'storage-summary.json').read_text())
        elapsed=now-previous['checked_at']; growth=categories['recordings']['logical_bytes']-previous['categories']['recordings']['logical_bytes']
        if elapsed>=30 and growth>=1024**2:
            days=round(max(0,disk.free-1024**3)/(growth/elapsed)/86400,2)
    except (OSError,ValueError,KeyError,ZeroDivisionError): pass
    return {'checked_at':now,'categories':categories,'free_bytes':disk.free,'total_bytes':disk.total,
            'warning':disk.free < 10*1024**3 or disk.free/disk.total < .15,
            'critical':disk.free < 1024**3,'snapshot_reserve_bytes':SNAPSHOT_RESERVE,
            'recording_days_estimate':days,
            'external_configured':False,'same_device_backup':data.stat().st_dev == backups.stat().st_dev}


def media_bytes(root):
    return sum(p.stat().st_size for p in Path(root).rglob("*")
               if not p.is_symlink() and p.is_file() and p.suffix.lower() in {".mp4", ".mkv"})


def backup_policy(state):
    path = Path(state) / "storage-policy.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    return {"max_backup_gb": data.get("max_backup_gb", 0)}


def save_backup_policy(state, payload):
    limit = payload.get("max_backup_gb")
    if type(limit) is not int or not 0 <= limit <= 102400:
        raise ValueError("備份容量預算須為 0–102400 GB，0 表示不限")
    result = {"max_backup_gb": limit}
    atomic_json(Path(state) / "storage-policy.json", result)
    return result


def check_backup_budget(root, state, estimate):
    limit = backup_policy(state)["max_backup_gb"]
    if limit:
        from camera_backups import BackupStore
        occupied = BackupStore(root, state).capacity()["physical_bytes"]
        if occupied + estimate > limit * 1024 ** 3:
            raise RuntimeError("備份容量預算不足；請提高預算或管理資料。既有備份與回收桶均保留。")
