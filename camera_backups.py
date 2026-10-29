"""Local backup catalog, verification, isolated recovery and reversible management."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time
import uuid
from contextlib import closing, contextmanager

from camera_state import atomic_json, sync_directory

BACKUP_NAME = re.compile(r"^\d{8}-\d{6}-\d{6}$")


def files_under(root, allow_latest=False):
    root = Path(root)
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in list(dirs):
            if (base / name).is_symlink():
                if allow_latest and base == root and (name == "latest" or name.startswith("latest-")):
                    dirs.remove(name)
                    continue
                raise ValueError("備份目錄含符號連結，已停止操作")
        for name in files:
            path = base / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("備份含不支援的檔案")
            yield path


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            checksum.update(block)
    return checksum.hexdigest()


def index_files(root, progress=None, cache_path=None):
    """Hash immutable backup inodes; never hardlink an archive to live writable media."""
    root = Path(root)
    cache = {}
    if cache_path and Path(cache_path).is_file():
        try:
            cache = json.loads(Path(cache_path).read_text())
        except (OSError, ValueError):
            pass
    files = [p for p in files_under(root) if p.relative_to(root).as_posix() not in {"manifest.json", "verification.json"}]
    total = sum(p.stat().st_size for p in files)
    done, rows = 0, []
    for path in files:
        stat = path.stat()
        key = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
        checksum = cache.get(key) if cache_path else None
        checksum = checksum or digest(path)
        cache[key] = checksum
        rows.append({"path": path.relative_to(root).as_posix(), "size": stat.st_size, "sha256": checksum})
        done += stat.st_size
        if progress:
            progress("校驗備份檔案", done, total)
    if cache_path:
        atomic_json(cache_path, dict(list(cache.items())[-50000:]))
    return rows


def checked_member(root, relative):
    if not isinstance(relative, str) or "\\" in relative:
        raise ValueError("備份檔案路徑不正確")
    part = Path(relative)
    if part.is_absolute() or not part.parts or any(p in {"..", "."} for p in part.parts):
        raise ValueError("備份檔案路徑不正確")
    path = Path(root) / part
    path.resolve().relative_to(Path(root).resolve())
    if any((Path(root).joinpath(*part.parts[:i])).is_symlink() for i in range(1, len(part.parts) + 1)):
        raise ValueError("備份符號連結不允許存取")
    return path


def verify_backup(root, progress=None):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("complete") is not True:
        raise ValueError("此備份尚未完成")
    # Enumerating also rejects links omitted from a malicious/partial manifest.
    present = {p.relative_to(root).as_posix() for p in files_under(root)} - {"manifest.json", "verification.json"}
    rows = manifest.get("files")
    legacy = not isinstance(rows, list)
    if legacy:
        rows = index_files(root, progress)
    if {r["path"] for r in rows} != present:
        raise ValueError("備份檔案清單不一致")
    total = sum(r["size"] for r in rows)
    done = 0
    for row in rows:
        path = checked_member(root, row["path"])
        if path.stat().st_size != row["size"] or (not legacy and digest(path) != row["sha256"]):
            raise ValueError("備份檔案校驗失敗：" + row["path"])
        done += row["size"]
        if progress:
            progress("驗證備份", done, total)
    with closing(sqlite3.connect(f"file:{root / 'events.sqlite3'}?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("備份資料庫檢查失敗")
        for (name,) in db.execute("SELECT snapshot_name FROM events"):
            if name and not checked_member(root / "snapshots", name).is_file():
                raise ValueError("備份缺少事件快照")
    with tarfile.open(root / "configuration.tar.gz") as archive:
        if "opt/camera-ai/camera_ai.py" not in archive.getnames():
            raise ValueError("備份缺少服務設定")
    # Legacy checks establish a new baseline, not proof against pre-existing tampering.
    if legacy:
        manifest.update(schema=2, files=rows, total_bytes=total)
        atomic_json(root / "manifest.json", manifest)
    result = {"verified_at": time.time(), "files": len(rows), "total_bytes": total,
              "legacy_baseline_created": legacy, "ok": True}
    atomic_json(root / "verification.json", result)
    return result


class BackupStore:
    def __init__(self, root, state):
        self.root, self.state = Path(root), Path(state)
        self.trash = self.root / ".trash"
        self.recoveries = self.state / "recoveries"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.reconcile()

    @contextmanager
    def mutation(self):
        import fcntl
        path = self.state / "data-maintenance.lock"
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("備份或驗證進行中，請稍後再試") from error
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def folder(self, name):
        if not isinstance(name, str) or not BACKUP_NAME.fullmatch(name):
            raise ValueError("備份名稱不正確")
        path = self.root / name
        if path.is_symlink() or path.resolve().parent != self.root.resolve() or not path.is_dir():
            raise FileNotFoundError("找不到備份")
        return path

    def latest_name(self):
        link = self.root / "latest"
        if link.is_symlink():
            target = link.resolve()
            if target.parent == self.root.resolve() and target.is_dir():
                return target.name
        return None

    def list(self):
        latest = self.latest_name()
        rows = []
        for path in self.root.iterdir():
            if not BACKUP_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
                continue
            try:
                manifest = json.loads((path / "manifest.json").read_text())
                verification = json.loads((path / "verification.json").read_text()) if (path / "verification.json").is_file() else {}
                total = manifest.get("total_bytes")
                if total is None:
                    total = sum(p.stat().st_size for p in files_under(path))
                rows.append({"name": path.name, "created_at": manifest.get("created_at", path.stat().st_mtime),
                             "complete": bool(manifest.get("complete")), "size": total, "latest": path.name == latest,
                             "verified_at": verification.get("verified_at"), "schema": manifest.get("schema", 1),
                             "skipped_active_recordings": manifest.get("skipped_active_recordings", [])})
            except (OSError, ValueError, KeyError):
                rows.append({"name": path.name, "created_at": path.stat().st_mtime,
                             "complete": False, "size": 0, "latest": False, "verified_at": None})
        return sorted(rows, key=lambda r: r["name"], reverse=True)

    def capacity(self):
        seen, logical, physical = set(), 0, 0
        for path in files_under(self.root, allow_latest=True):
            # latest is the only permitted link and os.walk does not follow it.
            stat = path.stat()
            logical += stat.st_size
            key = (stat.st_dev, stat.st_ino)
            if key not in seen:
                seen.add(key)
                physical += getattr(stat, "st_blocks", 0) * 512 or stat.st_size
        return {"logical_bytes": logical, "physical_bytes": physical}

    def _receipt(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ValueError("回收桶項目不正確")
        folder = self.trash / token
        if folder.is_symlink() or folder.resolve().parent != self.trash.resolve():
            raise ValueError("回收桶路徑不正確")
        data = json.loads((folder / "receipt.json").read_text())
        if not BACKUP_NAME.fullmatch(data["name"]):
            raise ValueError("回收桶備份名稱不正確")
        return folder, data

    def _clear(self, folder):
        (folder / "receipt.json").unlink()
        folder.rmdir()
        sync_directory(self.trash)

    def reconcile(self):
        if not self.trash.is_dir():
            return
        for folder in self.trash.iterdir():
            try:
                folder, data = self._receipt(folder.name)
                original, saved = self.root / data["name"], folder / "data"
                if original.is_dir() and not original.is_symlink() and not saved.exists():
                    stat = original.stat()
                    if data.get("identity") == [stat.st_dev, stat.st_ino]:
                        self._clear(folder)
            except (OSError, ValueError, KeyError):
                continue

    def list_trash(self):
        rows = []
        if not self.trash.exists():
            return rows
        for folder in self.trash.iterdir():
            try:
                folder, data = self._receipt(folder.name)
                if (folder / "data").is_dir() and not (folder / "data").is_symlink():
                    rows.append({k: data[k] for k in ("name", "token", "deleted_at", "size")})
            except (OSError, ValueError, KeyError):
                continue
        return sorted(rows, key=lambda r: r["deleted_at"], reverse=True)

    def move_to_trash(self, name):
        source = self.folder(name)
        if name == self.latest_name():
            raise ValueError("最近成功的備份受保護；請先建立新備份")
        token = uuid.uuid4().hex
        self.trash.mkdir(exist_ok=True, mode=0o700)
        folder = self.trash / token
        folder.mkdir(mode=0o700)
        stat = source.stat()
        receipt = {"name": name, "token": token, "deleted_at": time.time(),
                   "size": sum(p.stat().st_size for p in files_under(source)), "identity": [stat.st_dev, stat.st_ino]}
        atomic_json(folder / "receipt.json", receipt)
        source.rename(folder / "data")
        sync_directory(self.root)
        sync_directory(folder)
        return {"ok": True, "token": token}

    def restore(self, token):
        folder, data = self._receipt(token)
        destination = self.root / data["name"]
        if destination.exists() or destination.is_symlink():
            raise ValueError("同名備份已存在，不會覆寫")
        source = folder / "data"
        if source.is_symlink() or not source.is_dir():
            raise FileNotFoundError("回收桶備份不存在")
        source.rename(destination)
        sync_directory(self.root)
        self._clear(folder)
        return {"ok": True, "name": data["name"]}

    def recover_isolated(self, name, progress=None):
        source = self.folder(name)
        verified = verify_backup(source, progress)
        self.recoveries.mkdir(mode=0o700, exist_ok=True)
        if shutil.disk_usage(self.recoveries).free < verified["total_bytes"] + 1024 ** 3:
            raise RuntimeError("空間不足以建立獨立還原副本")
        target = self.recoveries / (name + "-" + uuid.uuid4().hex[:8])
        target.mkdir(mode=0o700)
        if progress:
            progress("建立隔離還原副本", 0, verified["total_bytes"])
        # Copy rather than hardlink: editing a recovery must never mutate the backup.
        subprocess.run(["rsync", "-a", str(source) + "/", str(target) + "/"], check=True, timeout=3600)
        result = verify_backup(target, progress)
        return {"ok": True, "path": str(target), "verified": result, "live_data_changed": False,
                "configuration": "configuration.tar.gz 保留於隔離目錄，未解開或套用至系統"}
