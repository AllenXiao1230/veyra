"""Durable small state, cross-process media coordination and bounded audit history."""
import contextlib
import json
import os
from pathlib import Path
import time


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o640)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def media_guard(root):
    """Production processes share a read-openable flock; fixtures stay isolated."""
    target = os.environ.get("CAMERA_MEDIA_LOCK")
    if not target and str(Path(root).resolve()).startswith("/var/lib/camera"):
        target = "/run/camera-monitor/media.lock"
    if not target or os.name == "nt":
        yield
        return
    import fcntl
    # tmpfiles creates the production lock, readable by the unprivileged detector.
    path = Path(target)
    if not path.exists() and str(path) != "/run/camera-monitor/media.lock":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    with path.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
