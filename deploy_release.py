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
import sys
sys.path.append('/srv/camera-monitor/app/common')
from camera_deployment import deployment_guard

SERVICES = ["camera-ai", "camera-recorder-api", "camera-backup-api", "camera-timestamp",'camera-stream']
USER_ENV=['runuser','-u','xuan','--','env','XDG_RUNTIME_DIR=/run/user/1000',
          'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus','systemctl','--user']


def affected_services(files):
    names={Path(row['path']).name for row in files}
    paths=[row['path'] for row in files]
    shared={'camera_state.py','camera_runtime.py'}
    result=[]
    if any(p.startswith('/srv/camera-monitor/app/ai/') for p in paths) or names & (shared|{'camera_motion.py','camera_annotations.py','camera_media.py','camera_index.py','camera_storage.py','camera_person.py','camera_jobs.py'}): result.append('camera-ai')
    if any(p.startswith('/srv/camera-monitor/app/recorder/') for p in paths) or names & (shared|{'camera_deployment.py','camera_media.py','camera_index.py','camera_storage.py','camera_sessions.py','camera_http.py','camera_catalog.py','camera_playback.py','camera_jobs.py'}): result.append('camera-recorder-api')
    if names & (shared|{'camera_deployment.py','backup_api.py','camera_maintenance.py','camera-maintenance','camera_backups.py','camera_media.py','camera_http.py','camera_index.py','camera_timeline.py','camera_storage.py','camera_diagnostics.py','camera_jobs.py'}): result.append('camera-backup-api')
    if any(p.startswith('/srv/camera-monitor/app/timestamp/') for p in paths) or names & shared: result.append('camera-timestamp')
    for name in SERVICES:
        if name+'.service' in names and name not in result:result.append(name)
    if names & {'camera_stream.py','mediamtx.yml'} and 'camera-stream' not in result:result.append('camera-stream')
    if names & {'camera_work.py','camera_notifications.py'} and 'camera-backup-api' not in result:result.append('camera-backup-api')
    return result


def run(*args):
    subprocess.run(args, check=True, timeout=100)


def ensure_no_exports(directory=Path('/srv/camera-monitor/data/maintenance/event-exports')):
    for job in directory.glob('*.job.json'):
        try:
            data=json.loads(job.read_text())
            active=isinstance(data,dict) and data.get('state') in {'queued','running'}
        except (OSError,ValueError):active=False
        if active:raise RuntimeError('Wait for event export before deploying or rolling back')


def install(stage, manifest):
    with deployment_guard():
        return _install(stage,manifest)


def _install(stage, manifest):
    data = json.loads(manifest.read_text())
    for asset in data.get('external_assets',[]):
        path=Path(asset['path'])
        if not str(path).startswith('/srv/camera-monitor/app/ai/models/') or path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=asset['sha256']:
            raise ValueError('Required checksum-pinned model must be restored before deployment')
    files = data["files"]+[{'source':str(manifest.resolve().relative_to(stage.resolve())),
                           'path':'/srv/camera-monitor/app/management/current-release.json',
                           'sha256':hashlib.sha256(manifest.read_bytes()).hexdigest()}]
    for row in files:
        source = stage / row["source"]
        source.resolve().relative_to(stage.resolve())
        if hashlib.sha256(source.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Staging checksum mismatch: " + row["source"])
        destination=Path(row['path'])
        if '..' in destination.parts or not any(row['path'].startswith(prefix) for prefix in
                ('/srv/camera-monitor/app/','/srv/camera-monitor/config/','/srv/camera-monitor/web/','/srv/camera-monitor/bin/')):
            raise ValueError("Unexpected deployment destination")
    expected = files
    files=[r for r in files if not (r.get('preserve_existing') and Path(r['path']).is_file()) and
           (not Path(r['path']).is_file() or hashlib.sha256(Path(r['path']).read_bytes()).hexdigest()!=r['sha256'])]
    config_contents=None
    stream_config=Path('/srv/camera-monitor/config/mediamtx/mediamtx.yml')
    if any(r['source']=='camera_stream.py' for r in expected):
        from camera_stream_config import render
        config_contents=render(stream_config.read_text())
        if config_contents!=stream_config.read_bytes():
            files.append({'source':'mediamtx.yml.example','path':str(stream_config),
                          'sha256':hashlib.sha256(config_contents).hexdigest(),'inline':True})
    user_services=['camera-audio','camera-mediamtx'] if any(r.get('inline') for r in files) else []
    services=affected_services(files)
    status = json.load(urllib.request.urlopen("http://127.0.0.1:8091/status", timeout=5))
    if ('camera-recorder-api' in services or user_services) and (status.get("recording") or status.get("recording_state") in {"starting", "stopping"}):
        raise RuntimeError("Stop the active recording before deploying")
    if services:ensure_no_exports()
    backup = Path("/srv/camera-monitor/backups/releases") / (data["release"] + "-" + time.strftime("%Y%m%d-%H%M%S"))
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
    directory = Path("/srv/camera-monitor/app/recorder")
    stat = directory.stat()
    saved.append({"path": str(directory), "directory": True, "existed": True,
                  "uid": stat.st_uid, "gid": stat.st_gid, "mode": stat.st_mode & 0o777})
    (backup / "rollback.json").write_text(json.dumps(saved, indent=2))
    (backup/'release.json').write_text(json.dumps({'release':data['release'],'services':services,'user_services':user_services}))
    print("ROLLBACK=" + str(backup), flush=True)
    try:
        existing=[s for s in services if Path('/etc/systemd/system',s+'.service').exists()]
        if existing:run('systemctl','stop',*existing)
        if user_services:run(*USER_ENV,'stop',*user_services)
        try:
            account = pwd.getpwnam("camera-recorder")
        except KeyError:
            run("useradd", "--system", "--home", "/srv/camera-monitor/data/recordings", "--shell", "/usr/sbin/nologin", "--groups", "render,video", "camera-recorder")
            account = pwd.getpwnam("camera-recorder")
        # Existing media ownership is not a deployment operation.
        os.chown('/srv/camera-monitor/data/recordings',account.pw_uid,account.pw_gid)
        os.chown(directory, 0, account.pw_gid)
        directory.chmod(0o750)
        log = Path("/srv/camera-monitor/logs/camera-recorder-ffmpeg.log")
        log.touch(exist_ok=True)
        os.chown(log, account.pw_uid, account.pw_gid)
        log.chmod(0o640)
        cache = Path('/srv/camera-monitor/cache/playback')
        cache.mkdir(parents=True, exist_ok=True)
        os.chown(cache, account.pw_uid, account.pw_gid)
        cache.chmod(0o750)
        for row, old in zip(files, saved):
            path = Path(row["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".release-tmp")
            if row.get('inline'):temporary.write_bytes(config_contents)
            else:shutil.copyfile(stage / row["source"], temporary)
            temporary.chmod(0o755 if str(path) == "/srv/camera-monitor/bin/camera-maintenance" else old["mode"])
            if str(path).startswith('/srv/camera-monitor/app/'):
                temporary.chmod(0o644)
            os.chown(temporary, old["uid"], old["gid"])
            os.replace(temporary, path)
        run("systemd-tmpfiles", "--create", "/srv/camera-monitor/config/tmpfiles/camera-monitor.conf")
        run("runuser", "-u", "camera-recorder", "--", "python3", "-c",
            "import sys; sys.path.insert(0,'/srv/camera-monitor/app/recorder'); import server; server.load_settings(); server.media_library()")
        run("nginx", "-t")
        for row in expected:
            target=Path(row['path'])
            if target.parent!=Path('/srv/camera-monitor/config/systemd/system'):continue
            link=Path('/etc/systemd/system')/target.name
            if not link.exists() and not link.is_symlink():
                saved.append({'path':str(link),'registration':True,'target':str(target)})
                (backup/'rollback.json').write_text(json.dumps(saved,indent=2))
                link.symlink_to(target)
            elif link.resolve()!=target.resolve():raise RuntimeError('Unexpected systemd registration: '+str(link))
        run("systemctl", "daemon-reload")
        if user_services:
            run(*USER_ENV,'daemon-reload');run(*USER_ENV,'start',*user_services)
        if services: run("systemctl", "start", *services)
        run("systemctl", "reload", "nginx")
        deadline = time.monotonic() + 65
        while True:
            try:
                for port in (8091, 8092, 8093, 8094, 8095,8096):
                    value = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4))
                    if not value.get("healthy"):
                        raise RuntimeError("Health failed: " + str(port))
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        run('systemctl','enable','camera-backup-api','camera-stream')
        for row in expected:
            if row.get('preserve_existing'):continue
            if hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest() != row["sha256"]:
                raise RuntimeError("Installed checksum mismatch")
        print(json.dumps({"deployed": data["release"], "verified_files": len(expected), 'changed_files':len(files),
                          'restarted_services':services,'restarted_user_services':user_services,"rollback": str(backup)}))
    except Exception:
        rollback(backup, guarded=True)
        raise


def rollback(backup, guarded=False):
    if not guarded:
        with deployment_guard():
            ensure_no_exports()
            status=json.load(urllib.request.urlopen('http://127.0.0.1:8091/status',timeout=5))
            if status.get('recording') or status.get('recording_state') in {'starting','stopping'}:
                raise RuntimeError('Stop the active recording before rollback')
            return rollback(backup,guarded=True)
    # This list contains deployment code/configuration only, never user media.
    running=[name for name in SERVICES if Path('/etc/systemd/system',name+'.service').exists()]
    if running:run('systemctl','stop',*running)
    user_services=json.loads((backup/'release.json').read_text()).get('user_services',[]) if (backup/'release.json').exists() else []
    if user_services:run(*USER_ENV,'stop',*user_services)
    for row in json.loads((backup / "rollback.json").read_text()):
        path = Path(row["path"])
        if row.get('registration'):
            if path.is_symlink() and os.readlink(path)==row['target']:path.unlink()
            wanted=Path('/etc/systemd/system/multi-user.target.wants')/path.name
            if wanted.is_symlink() and os.readlink(wanted) in (str(path),row['target']):wanted.unlink()
        elif row.get("directory"):
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
    if user_services:run(*USER_ENV,'daemon-reload');run(*USER_ENV,'start',*user_services)
    run("systemctl", "start", "camera-ai", "camera-recorder-api", "camera-timestamp")
    if Path("/etc/systemd/system/camera-backup-api.service").exists():
        run("systemctl", "start", "camera-backup-api")
    else:
        Path("/etc/systemd/system/multi-user.target.wants/camera-backup-api.service").unlink(missing_ok=True)
    if Path('/etc/systemd/system/camera-stream.service').exists():run('systemctl','start','camera-stream')
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
