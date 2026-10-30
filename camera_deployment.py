"""A short-lived deployment gate prevents recording/job admission races."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from camera_state import atomic_json, media_guard

MARKER=Path('/run/camera-monitor/deployment.json')


def deployment_active(path=MARKER):
    if not path.exists():return False
    try:
        data=json.loads(path.read_text())
        return data['expires']>time.monotonic() and Path('/proc',str(data['pid'])).exists()
    except (OSError,ValueError,TypeError,KeyError):return True


@contextmanager
def deployment_guard(state=Path('/var/lib/camera-maintenance'), marker=MARKER):
    import fcntl
    state.mkdir(parents=True,exist_ok=True);marker.parent.mkdir(parents=True,exist_ok=True)
    with (state/'data-maintenance.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:raise RuntimeError('Wait for backup/maintenance before deploying') from error
        try:
            with media_guard('/var/lib/camera-ai'):
                atomic_json(marker,{'pid':os.getpid(),'expires':time.monotonic()+7200})
                marker.chmod(0o644)
            yield
        finally:
            marker.unlink(missing_ok=True)
            fcntl.flock(lock,fcntl.LOCK_UN)
