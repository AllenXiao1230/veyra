"""Non-destructive storage budgets; trash still counts toward occupied space."""
import json
from pathlib import Path
from camera_state import atomic_json


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
