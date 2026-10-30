"""Canonical on-disk layout and legacy path mapping for the /srv migration."""
from pathlib import PurePosixPath as Path

ROOT = Path('/srv/camera-monitor')
PREFIXES = {
    '/opt/camera-ai': 'app/ai',
    '/opt/camera-common': 'app/common',
    '/opt/camera-recorder': 'app/recorder',
    '/opt/camera-timestamp': 'app/timestamp',
    '/opt/camera-monitor': 'app/management',
    '/var/lib/camera-ai': 'data/ai',
    '/var/lib/camera-recorder': 'data/recordings',
    '/var/lib/camera-timestamp': 'data/timestamp',
    '/var/lib/camera-maintenance': 'data/maintenance',
    '/var/cache/camera-monitor': 'cache',
    '/var/backups/camera-monitor': 'backups',
    '/var/www/camera-monitor': 'web',
    '/etc/camera-ai': 'config/ai',
    '/etc/camera-monitor-maintenance.json': 'config/maintenance.json',
    '/etc/logrotate.d/camera-recorder': 'config/logrotate/camera-recorder',
    '/etc/tmpfiles.d/camera-monitor.conf': 'config/tmpfiles/camera-monitor.conf',
    '/etc/nginx/sites-available/camera-monitor': 'config/nginx/camera-monitor',
    '/etc/nginx/sites-enabled/camera-monitor': 'config/nginx/camera-monitor',
    '/home/xuan/.config/camera-monitor': 'config/audio',
    '/home/xuan/.config/mediamtx': 'config/mediamtx',
    '/home/xuan/.local/opt/mediamtx': 'vendor/mediamtx',
    '/home/xuan/camera-monitor': 'source',
    '/usr/local/sbin/camera-maintenance': 'bin/camera-maintenance',
}


def canonical(path):
    path = str(path)
    for old, relative in sorted(PREFIXES.items(), key=lambda row: -len(row[0])):
        if path == old or path.startswith(old + '/'):
            return str(ROOT / relative) + path[len(old):]
    for old, relative in (
        ('/etc/systemd/system/', 'config/systemd/system'),
        ('/home/xuan/.config/systemd/user/', 'config/systemd/user'),
    ):
        if path.startswith(old + 'camera-'):
            return str(ROOT / relative / path[len(old):])
    if path.startswith('/var/log/camera-recorder-ffmpeg.log'):
        return str(ROOT / 'logs' / Path(path).name)
    return path
