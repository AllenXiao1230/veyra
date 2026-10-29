#!/usr/bin/python3
"""Camera health supervision, verified backups and explicit retention planning."""
import argparse
import fcntl
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import tarfile
import time
import sys
import tempfile
import errno
from contextlib import closing
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
sys.path.append("/opt/camera-common")
from camera_state import media_guard, atomic_json
from camera_backups import index_files
from camera_storage import check_backup_budget

STATE = pathlib.Path("/var/lib/camera-maintenance")
BACKUPS = pathlib.Path("/var/backups/camera-monitor/snapshots")
CONFIG = pathlib.Path("/etc/camera-monitor-maintenance.json")
DATA = pathlib.Path("/var/lib/camera-ai")
RECORDINGS = pathlib.Path("/var/lib/camera-recorder")


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def http_json(url):
    try:
        response = urllib.request.urlopen(url, timeout=4)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return json.load(response)


def health(repair=False):
    previous = read_json(STATE / "status.json", {})
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "services": {}}
    for name, port in [("camera-ai", 8092), ("camera-recorder-api", 8091), ("camera-timestamp", 8093),
                       ("camera-audio", 8094)]:
        try:
            state = http_json(f"http://127.0.0.1:{port}/health")
            healthy = bool(state.get("healthy"))
            if name == "camera-recorder-api":
                # Do not erase an encoder failure by restarting a healthy capture worker.
                restartable = state.get("frame_age_seconds") is None or state["frame_age_seconds"] > 30
            elif name == "camera-audio":
                # Its supervisor retries stalled/absent sources without losing the API or backoff.
                restartable = False
            else:
                restartable = not healthy
        except Exception as exc:
            state, healthy, restartable = {"error": str(exc)}, False, True
        old = previous.get("services", {}).get(name, {})
        failures = 0 if healthy else old.get("failures", 0) + 1
        last_restart = old.get("last_restart", 0)
        if repair and restartable and failures >= 2 and time.time() - last_restart > 300:
            command = (["runuser", "-u", "xuan", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000",
                        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus", "systemctl", "--user",
                        "restart", name] if name == "camera-audio" else ["systemctl", "restart", name])
            subprocess.run(command, check=True, timeout=50)
            last_restart = time.time()
            state["action"] = "restarted after repeated failed checks"
        result["services"][name] = {"healthy": healthy, "failures": failures,
                                     "last_restart": last_restart, "detail": state}
    usage = shutil.disk_usage(DATA)
    result["disk"] = {"free_bytes": usage.free, "used_percent": round(100 * usage.used / usage.total, 1),
                      "warning": usage.free < 10 * 1024 ** 3 or usage.used / usage.total > 0.85}
    result["healthy"] = all(s["healthy"] for s in result["services"].values()) and not result["disk"]["warning"]
    atomic_json(STATE / "status.json", result)
    return result


def backup(dry_run=False, progress=None):
    recordings = [p for p in RECORDINGS.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in {".mp4", ".mkv"}]
    plan = {"recording_files": len(recordings), "recording_bytes": sum(p.stat().st_size for p in recordings),
            "destination": str(BACKUPS), "deletes_existing_backups": False}
    if dry_run:
        return plan
    if shutil.disk_usage(DATA).free < 20 * 1024 ** 3:
        raise RuntimeError("Less than 20 GiB free; backup stopped without deleting data")
    BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
    extra_bytes = sum(p.stat().st_size for root in (DATA / "snapshots", DATA / "trash", RECORDINGS / ".trash", RECORDINGS / "sessions")
                      for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    check_backup_budget(BACKUPS, STATE, plan["recording_bytes"] + extra_bytes + 256 * 1024 ** 2)
    destination = BACKUPS / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination.mkdir(mode=0o700)
    STATE.mkdir(parents=True, exist_ok=True)
    pins = pathlib.Path(tempfile.mkdtemp(prefix="backup-pins-", dir=STATE))
    descriptors = []
    def pin_file(source, target):
        try:
            os.link(source, target)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            # systemd's read-only bind mounts can reject hardlinks across mount
            # identities on the same disk. Keep the inode open instead; rsync -L
            # copies its /proc descriptor even if the user trashes the original.
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            target.symlink_to(f"/proc/{os.getpid()}/fd/{descriptor}")
    def pin_tree(source, target, only=None):
        if not source.is_dir():
            return
        target.mkdir(mode=0o700)
        for path in (source.iterdir() if only is not None else source.rglob("*")):
            if path.name.startswith(".media-index.sqlite3"):
                continue
            if path.is_symlink():
                raise RuntimeError("Cannot back up symbolic links in media")
            if not path.is_file() or (only is not None and path.name not in only):
                continue
            output = target / path.relative_to(source)
            output.parent.mkdir(parents=True, exist_ok=True)
            pin_file(path, output)
    try:
        # Freeze database + immutable-file identities briefly, then copy without
        # blocking new events or user trash actions for the duration of rsync.
        with media_guard(DATA):
            try:
                current = http_json("http://127.0.0.1:8091/status")
            except Exception:
                current = {"recording": True, "active_files": []}
            recordings = [p for p in RECORDINGS.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in {".mp4", ".mkv"}]
            active = set(current.get("active_files", []))
            changing = current.get("recording") or current.get("recording_state") in {"starting", "stopping"}
            skipped = [p.name for p in recordings if p.name in active or (changing and p.stat().st_mtime > time.time() - 60)]
            with closing(sqlite3.connect(f"file:{DATA / 'events.sqlite3'}?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(destination / "events.sqlite3")) as target:
                    source.backup(target)
                    assert target.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            pin_tree(DATA / "snapshots", pins / "snapshots")
            pin_tree(DATA / "trash", pins / "snapshot-trash")
            pin_tree(RECORDINGS / ".trash", pins / "recording-trash")
            pin_tree(RECORDINGS, pins / "recordings", {p.name for p in recordings if p.name not in skipped})
            pin_tree(RECORDINGS / "sessions", pins / "sessions")
            settings = RECORDINGS / "settings.json"
            if settings.is_file() and not settings.is_symlink():
                (pins / "recorder-state").mkdir()
                pin_file(settings, pins / "recorder-state" / "settings.json")
            policy = STATE / "storage-policy.json"
            if policy.is_file() and not policy.is_symlink():
                (pins / "maintenance-state").mkdir()
                pin_file(policy, pins / "maintenance-state" / "storage-policy.json")
        if progress:
            progress("封存設定與複製媒體", 0, 0)
        return _finish_backup(destination, pins, plan, skipped, progress)
    finally:
        shutil.rmtree(pins)
        for descriptor in descriptors:
            os.close(descriptor)


def configuration_entries():
    paths = ["/opt/camera-ai", "/opt/camera-recorder", "/opt/camera-timestamp", "/opt/camera-common", "/opt/camera-monitor",
             "/etc/camera-ai", "/etc/nginx", "/etc/camera-monitor-maintenance.json",
             "/etc/logrotate.d/camera-recorder", "/var/www/camera-monitor",
             "/home/xuan/.config/systemd/user", "/home/xuan/.config/mediamtx", "/home/xuan/.config/camera-monitor",
             "/usr/local/sbin/camera-maintenance"]
    paths += [str(p) for p in pathlib.Path("/etc/systemd/system").glob("camera-*")]
    return [(pathlib.Path(p), str(p).lstrip("/")) for p in paths if pathlib.Path(p).exists()]


def _finish_backup(destination, pins, plan, skipped, progress):
    with tarfile.open(destination / "configuration.tar.gz", "w:gz") as archive:
        for path, name in configuration_entries():
            archive.add(path, arcname=name)
    with tarfile.open(destination / "configuration.tar.gz") as archive:
        assert "opt/camera-ai/camera_ai.py" in archive.getnames()
    previous = BACKUPS / "latest"
    baseline = previous.resolve() if previous.is_symlink() else None
    if baseline:
        baseline.relative_to(BACKUPS.resolve())
    for source in pins.iterdir():
        category = source.name
        target = destination / category
        target.mkdir(mode=0o700)
        command = ["rsync", "-aL"]
        if baseline and (baseline / category).is_dir():
            command.append("--link-dest=" + str(baseline / category))
        subprocess.run(command + [str(source) + "/", str(target) + "/"], check=True, timeout=1800)
    with sqlite3.connect(f"file:{destination / 'events.sqlite3'}?mode=ro", uri=True) as db:
        missing = [name for (name,) in db.execute("SELECT snapshot_name FROM events")
                   if name and not (destination / "snapshots" / name).is_file()]
        if missing:
            raise RuntimeError("Backup snapshot verification failed")
    files = index_files(destination, progress, STATE / "backup-checksum-cache.json")
    plan.update({"complete": True, "schema": 2, "files": files, "total_bytes": sum(r["size"] for r in files),
                 "created_at": time.time(), "path": str(destination),
                 "skipped_active_recordings": skipped})
    atomic_json(destination / "manifest.json", plan)
    temporary_link = BACKUPS / ("latest-" + destination.name)
    temporary_link.symlink_to(destination.name)
    os.replace(temporary_link, previous)
    return plan


def retention_plan():
    config = read_json(CONFIG, {})
    days = config.get("retention_days", 0)
    if not isinstance(days, int) or days < 0:
        raise ValueError("retention_days must be a nonnegative integer")
    if not days:
        return {"enabled": False, "reason": "No expiry chosen; all data retained", "events": [], "recordings": []}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with sqlite3.connect(f"file:{DATA / 'events.sqlite3'}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT id,created_at,snapshot_name FROM events").fetchall()
    events = [(i, stamp, name) for i, stamp, name in rows if datetime.fromisoformat(stamp) < cutoff]
    media = [str(p) for p in RECORDINGS.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv"}
             and p.stat().st_mtime < cutoff.timestamp()]
    return {"enabled": True, "days": days, "cutoff": cutoff.isoformat(), "events": events, "recordings": media}


def cleanup(apply=False):
    plan = retention_plan()
    if not apply or not plan["enabled"]:
        return plan
    if not plan["events"] and not plan["recordings"]:
        return {**plan, "applied": True, "deleted": 0}
    snapshots = []
    media = []
    for _identifier, _stamp, name in plan["events"]:
        if name:
            path = DATA / "snapshots" / name
            if path.resolve().parent != (DATA / "snapshots").resolve():
                raise RuntimeError("Snapshot path outside expected directory")
            snapshots.append(path)
    for name in plan["recordings"]:
        path = pathlib.Path(name)
        if path.resolve().parent != RECORDINGS.resolve():
            raise RuntimeError("Recording path outside expected directory")
        media.append(path)
    # A complete recoverable copy is required before any live-data removal.
    backup()
    with sqlite3.connect(DATA / "events.sqlite3") as db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany("DELETE FROM events WHERE id=?", [(row[0],) for row in plan["events"]])
    for path in snapshots:
        path.unlink(missing_ok=True)
    for path in media:
        path.unlink()
    plan["applied"] = True
    return plan


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "backup", "cleanup"])
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_name = "check.lock" if args.action == "check" else "data-maintenance.lock"
    with (STATE / lock_name).open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"busy": True, "message": "Another maintenance job is already running"}))
            return
        if args.action == "check":
            result = health(args.repair)
        elif args.action == "backup":
            result = backup(args.dry_run)
        else:
            result = cleanup(args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.action == "check" and not result["healthy"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
