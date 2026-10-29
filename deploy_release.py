#!/usr/bin/python3
"""Install a checksum manifest, retaining a release-specific rollback of code/config only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import time
import urllib.request

SERVICES = ["camera-ai", "camera-recorder-api", "camera-backup-api"]


def run(*args):
    subprocess.run(args, check=True, timeout=100)


def install(stage, manifest):
    data = json.loads(manifest.read_text())
    files = data["files"]
    for row in files:
        source = stage / row["source"]
        source.resolve().relative_to(stage.resolve())
        if hashlib.sha256(source.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Staging checksum mismatch: " + row["source"])
        if not any(row["path"].startswith(prefix) for prefix in ("/opt/camera-", "/etc/", "/var/www/camera-monitor/", "/usr/local/sbin/camera-maintenance", "/home/xuan/.config/systemd/user/")):
            raise ValueError("Unexpected deployment destination")
    status = json.load(urllib.request.urlopen("http://127.0.0.1:8091/status", timeout=5))
    if status.get("recording") or status.get("recording_state") in {"starting", "stopping"}:
        raise RuntimeError("Stop the active recording before deploying")
    backup = Path("/var/backups/camera-monitor/releases") / (data["release"] + "-" + time.strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True, mode=0o700)
    saved = []
    for row in files:
        path = Path(row["path"])
        if path.is_symlink():
            raise ValueError("Refusing deployment over symbolic link: " + str(path))
        old = path.stat() if path.exists() else None
        if old:
            copy = backup / path.relative_to("/")
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy)
        saved.append({"path": str(path), "existed": bool(old), "uid": old.st_uid if old else 0,
                      "gid": old.st_gid if old else 0, "mode": old.st_mode & 0o777 if old else 0o644})
    directory = Path("/opt/camera-recorder")
    stat = directory.stat()
    saved.append({"path": str(directory), "directory": True, "existed": True,
                  "uid": stat.st_uid, "gid": stat.st_gid, "mode": stat.st_mode & 0o777})
    (backup / "rollback.json").write_text(json.dumps(saved, indent=2))
    print("ROLLBACK=" + str(backup), flush=True)
    run("systemctl", "stop", "camera-recorder-api", "camera-ai")
    if Path("/etc/systemd/system/camera-backup-api.service").exists():
        run("systemctl", "stop", "camera-backup-api")
    try:
        try:
            account = pwd.getpwnam("camera-recorder")
        except KeyError:
            run("useradd", "--system", "--home", "/var/lib/camera-recorder", "--shell", "/usr/sbin/nologin", "--groups", "render,video", "camera-recorder")
            account = pwd.getpwnam("camera-recorder")
        run("chown", "-R", "camera-recorder:camera-recorder", "/var/lib/camera-recorder")
        os.chown(directory, 0, account.pw_gid)
        directory.chmod(0o750)
        log = Path("/var/log/camera-recorder-ffmpeg.log")
        log.touch(exist_ok=True)
        os.chown(log, account.pw_uid, account.pw_gid)
        log.chmod(0o640)
        for row, old in zip(files, saved):
            path = Path(row["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".release-tmp")
            shutil.copyfile(stage / row["source"], temporary)
            temporary.chmod(0o755 if str(path) == "/usr/local/sbin/camera-maintenance" else old["mode"])
            if str(path).startswith("/opt/camera-"):
                temporary.chmod(0o644)
            os.chown(temporary, old["uid"], old["gid"])
            os.replace(temporary, path)
        run("systemd-tmpfiles", "--create", "/etc/tmpfiles.d/camera-monitor.conf")
        run("runuser", "-u", "camera-recorder", "--", "python3", "-c",
            "import sys; sys.path.insert(0,'/opt/camera-recorder'); import server; server.load_settings(); server.media_library()")
        run("nginx", "-t")
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "camera-backup-api")
        run("systemctl", "start", *SERVICES)
        run("systemctl", "reload", "nginx")
        deadline = time.monotonic() + 65
        while True:
            try:
                for port in (8091, 8092, 8093, 8095):
                    value = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4))
                    if not value.get("healthy"):
                        raise RuntimeError("Health failed: " + str(port))
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        for row in files:
            if hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest() != row["sha256"]:
                raise RuntimeError("Installed checksum mismatch")
        print(json.dumps({"deployed": data["release"], "verified_files": len(files), "rollback": str(backup)}))
    except Exception:
        rollback(backup)
        raise


def rollback(backup):
    # This list contains deployment code/configuration only, never user media.
    run("systemctl", "stop", *SERVICES)
    for row in json.loads((backup / "rollback.json").read_text()):
        path = Path(row["path"])
        if row.get("directory"):
            os.chown(path, row["uid"], row["gid"])
            path.chmod(row["mode"])
        elif row["existed"]:
            shutil.copyfile(backup / path.relative_to("/"), path)
            os.chown(path, row["uid"], row["gid"])
            path.chmod(row["mode"])
        else:
            path.unlink(missing_ok=True)
    run("systemctl", "daemon-reload")
    run("nginx", "-t")
    run("systemctl", "start", "camera-ai", "camera-recorder-api")
    if Path("/etc/systemd/system/camera-backup-api.service").exists():
        run("systemctl", "start", "camera-backup-api")
    else:
        Path("/etc/systemd/system/multi-user.target.wants/camera-backup-api.service").unlink(missing_ok=True)
    run("systemctl", "reload", "nginx")
    print("Rolled back code/config; media and saved recording settings preserved", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root")
    if args.rollback:
        rollback(args.rollback)
    else:
        install(args.stage, args.manifest)
