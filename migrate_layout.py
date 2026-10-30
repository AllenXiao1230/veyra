#!/usr/bin/python3
"""One-time same-filesystem relocation, with a durable reverse-move journal.

The caller must use a trusted local stage. Never deletes media or recursively
changes ownership. OS registration/legacy aliases are links to canonical files.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

from camera_layout import PREFIXES, ROOT as POSIX_ROOT, canonical

ROOT = Path(str(POSIX_ROOT))
SYSTEM = ['camera-ai', 'camera-recorder-api', 'camera-backup-api', 'camera-timestamp']
TIMERS = ['camera-health.timer', 'camera-backup.timer', 'camera-cleanup.timer']
USER = ['camera-ustreamer', 'camera-mediamtx', 'camera-audio']
USER_ENV = ['runuser', '-u', 'xuan', '--', 'env', 'XDG_RUNTIME_DIR=/run/user/1000',
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus']


def run(*args, timeout=120):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Command failed: ' + repr(args) + '\n' + result.stderr[-4000:] + result.stdout[-1000:])
    return result.stdout.strip()


def http(url):
    return json.load(urllib.request.urlopen(url, timeout=6))


def inventory(root):
    """Inode identities prove rename preserved bytes, ownership and hardlinks."""
    rows = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            stat = path.lstat()
            rows[path.relative_to(root).as_posix()] = [stat.st_dev, stat.st_ino, stat.st_size,
                stat.st_mode, stat.st_uid, stat.st_gid, stat.st_mtime_ns]
    return rows


def active_exports(state):
    for path in (state / 'event-exports').glob('*.job.json'):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get('state') in ('queued', 'running'):
            return True
    return False


class Transaction:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, mode=0o700)
        self.journal = self.directory / 'journal.json'
        self.operations = []
        self.save()

    def save(self):
        temporary = self.journal.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump(self.operations, stream, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, self.journal)

    def move(self, old, new, alias=True):
        old, new = Path(old), Path(new)
        if not old.exists() and not old.is_symlink(): return
        if new.exists() or new.is_symlink(): raise RuntimeError('Destination exists: ' + str(new))
        new.resolve().relative_to(ROOT.resolve())
        new.parent.mkdir(parents=True, exist_ok=True)
        if old.lstat().st_dev != new.parent.stat().st_dev:
            raise RuntimeError('Refusing a cross-filesystem copy: ' + str(old))
        operation = {'op': 'move', 'old': str(old), 'new': str(new), 'alias': alias}
        self.operations.append(operation); self.save()
        os.rename(old, new)
        if alias: old.symlink_to(new, target_is_directory=new.is_dir())

    def link(self, path, target):
        path, target = Path(path), Path(target)
        if path.exists() or path.is_symlink(): raise RuntimeError('Link occupied: ' + str(path))
        self.operations.append({'op': 'link', 'path': str(path), 'target': str(target)}); self.save()
        path.symlink_to(target, target_is_directory=target.is_dir())

    def write(self, path, contents, mode=None):
        path = Path(path)
        path.resolve().relative_to(ROOT.resolve())
        if path.is_symlink(): raise RuntimeError('Refusing to replace a symlink: ' + str(path))
        backup = self.directory / 'overwritten' / path.relative_to(ROOT)
        old = path.stat() if path.exists() else None
        if old:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        self.operations.append({'op': 'write', 'path': str(path), 'backup': str(backup),
            'existed': old is not None, 'uid': old.st_uid if old else 0,
            'gid': old.st_gid if old else 0, 'mode': old.st_mode & 0o777 if old else 0o644})
        self.save()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.layout-tmp')
        temporary.write_bytes(contents)
        temporary.chmod(mode if mode is not None else (old.st_mode & 0o777 if old else 0o644))
        if old: os.chown(temporary, old.st_uid, old.st_gid)
        os.replace(temporary, path)

    def undo(self):
        for item in reversed(self.operations):
            if item['op'] == 'write':
                target = Path(item['path'])
                if item['existed']:
                    shutil.copy2(item['backup'], target)
                    os.chown(target, item['uid'], item['gid']); target.chmod(item['mode'])
                else:
                    target.unlink(missing_ok=True)
            elif item['op'] == 'link':
                target = Path(item['path'])
                if target.is_symlink() and os.readlink(target) == item['target']: target.unlink()
            elif item['op'] == 'move':
                old, new = Path(item['old']), Path(item['new'])
                if new.exists() or new.is_symlink():
                    if old.is_symlink() and os.readlink(old) == str(new): old.unlink()
                    if old.exists() or old.is_symlink(): raise RuntimeError('Rollback destination occupied: ' + str(old))
                    os.rename(new, old)


def move_plan():
    rows = [(Path(old), Path(canonical(old))) for old in PREFIXES
            if old != '/etc/nginx/sites-enabled/camera-monitor']
    for parent in ('/etc/systemd/system', '/home/xuan/.config/systemd/user'):
        rows += [(p, Path(canonical(p))) for p in sorted(Path(parent).glob('camera-*'))
                 if p.is_file() or p.is_dir()]
    rows += [(p, Path(canonical(p))) for p in sorted(Path('/var/log').glob('camera-recorder-ffmpeg.log*'))]
    # Drop-ins are moved as whole directories, not duplicated child operations.
    return rows


def stop_services():
    run('systemctl', 'stop', *TIMERS)
    run('systemctl', 'stop', 'camera-health.service', 'camera-backup.service', 'camera-cleanup.service', *SYSTEM)
    run(*USER_ENV, 'systemctl', '--user', 'stop', *USER)


def reload_services():
    run('systemctl', 'daemon-reload')
    run(*USER_ENV, 'systemctl', '--user', 'daemon-reload')


def start_services(system, user):
    reload_services()
    if user: run(*USER_ENV, 'systemctl', '--user', 'start', *user)
    if system: run('systemctl', 'start', *system)


def migrate(stage, manifest):
    if os.geteuid() != 0: raise RuntimeError('Run as root')
    if ROOT.exists(): raise RuntimeError('Project root already exists; inspect before retrying')
    if active_exports(Path('/var/lib/camera-maintenance')): raise RuntimeError('An event export is active')
    data = json.loads(manifest.read_text())
    for row in data['files']:
        source = stage / row['source']; source.resolve().relative_to(stage.resolve())
        if hashlib.sha256(source.read_bytes()).hexdigest() != row['sha256']:
            raise RuntimeError('Stage mismatch: ' + row['source'])
        Path(row['path']).relative_to(ROOT)
    for asset in data.get('external_assets', []):
        old = next((Path(prefix + asset['path'][len(canonical(prefix)):])
                    for prefix in PREFIXES if asset['path'].startswith(canonical(prefix) + '/')), None)
        if old is None or not old.is_file() or hashlib.sha256(old.read_bytes()).hexdigest() != asset['sha256']:
            raise RuntimeError('External model checksum mismatch')
    # Use current shared lock/old state before moving it. A rename preserves the
    # held lock inode. Compatibility aliases keep finally/rollback paths valid.
    from camera_deployment import deployment_guard
    active_system = [s for s in SYSTEM if subprocess.run(['systemctl','is-active','--quiet',s]).returncode == 0]
    active_user = [s for s in USER if subprocess.run(USER_ENV+['systemctl','--user','is-active','--quiet',s]).returncode == 0]
    active_timers = [s for s in TIMERS if subprocess.run(['systemctl','is-active','--quiet',s]).returncode == 0]
    with deployment_guard(state=Path('/var/lib/camera-maintenance')):
        status = http('http://127.0.0.1:8091/status')
        if status.get('recording') or status.get('recording_state') in ('starting','stopping'):
            raise RuntimeError('Wait for the active recording to finish')
        if active_exports(Path('/var/lib/camera-maintenance')): raise RuntimeError('An event export is active')
        ROOT.mkdir(mode=0o755)
        for name in ('app','config','data','cache','logs','backups','bin','vendor'):
            (ROOT/name).mkdir(mode=0o755)
        (ROOT/'archive').mkdir(mode=0o700)
        # backups itself is an incoming existing directory, not an empty target.
        (ROOT/'backups').rmdir(); (ROOT/'cache').rmdir()
        transaction = Transaction(ROOT/'archive'/('layout-migration-'+time.strftime('%Y%m%d-%H%M%S')))
        try:
            print('Stopping services and freezing inventories', flush=True)
            stop_services()
            preserved = {old: inventory(old) for old in (
                '/var/lib/camera-ai','/var/lib/camera-recorder','/var/lib/camera-maintenance',
                '/var/lib/camera-timestamp','/var/backups/camera-monitor','/var/cache/camera-monitor')}
            (transaction.directory/'inventory-before.json').write_text(json.dumps(preserved))
            for old, new in move_plan(): transaction.move(old, new)
            enabled = Path('/etc/nginx/sites-enabled/camera-monitor')
            transaction.move(enabled, transaction.directory/'legacy/etc/nginx/sites-enabled/camera-monitor', alias=False)
            transaction.link(enabled, canonical(enabled))
            for old, rows in preserved.items():
                if inventory(Path(canonical(old))) != rows: raise RuntimeError('Data inventory changed: ' + old)
            print('All data inodes, sizes, ownership and timestamps preserved', flush=True)
            for name in ('config/ai/camera-ai.env','config/mediamtx/mediamtx.yml'):
                target = ROOT/name; text = target.read_text()
                for old in sorted(PREFIXES, key=len, reverse=True): text = text.replace(old, canonical(old))
                transaction.write(target, text.encode())
            for row in data['files']:
                target = Path(row['path'])
                if row.get('preserve_existing') and target.exists(): continue
                contents = (stage/row['source']).read_bytes()
                if target.is_file() and target.read_bytes() == contents: continue
                transaction.write(target, contents, mode=0o755 if target.parent == ROOT/'bin' else None)
            transaction.write(ROOT/'app/management/current-release.json', manifest.read_bytes())
            transaction.link(ROOT/'README.md', ROOT/'source/LAYOUT.md')
            run('systemd-tmpfiles', '--create', str(ROOT/'config/tmpfiles/camera-monitor.conf'))
            run('nginx', '-t')
            print('Starting services from /srv/camera-monitor', flush=True)
            start_services(active_system, active_user)
            deadline = time.monotonic()+90
            while True:
                try:
                    states = {str(port):http(f'http://127.0.0.1:{port}/health').get('healthy') for port in range(8091,8096)}
                    if not all(states.values()): raise RuntimeError(str(states))
                    break
                except Exception:
                    if time.monotonic()>deadline: raise
                    time.sleep(1)
            for row in data['files']:
                if row.get('preserve_existing'): continue
                if hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()!=row['sha256']:
                    raise RuntimeError('Installed hash mismatch: '+row['path'])
            if active_timers: run('systemctl','start',*active_timers)
            result = {'ok':True,'root':str(ROOT),'release':data['release'],'files':len(data['files']),
                'data_inventory_unchanged':True,'services':states,'journal':str(transaction.journal),
                'active_timers':active_timers,'completed_at':time.time()}
            (transaction.directory/'result.json').write_text(json.dumps(result, indent=2))
            print(json.dumps(result), flush=True)
        except BaseException:
            print('Migration failed; stopping and restoring original paths', flush=True)
            stop_services(); transaction.undo(); start_services(active_system,active_user)
            if active_timers: run('systemctl','start',*active_timers)
            raise


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--stage',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if args.apply: migrate(args.stage.resolve(),args.manifest.resolve())
    else: print(json.dumps([{'old':str(a),'new':str(b)} for a,b in move_plan()],indent=2))
