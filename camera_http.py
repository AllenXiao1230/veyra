"""File-descriptor based video ranges; a concurrent rename never truncates a reply."""
import os
from pathlib import Path
import re


def serve_video(handler, path, head=False):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(str(path), flags), "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        start, end, status = 0, size - 1, 200
        requested = handler.headers.get("Range")
        if requested:
            try:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
                if not match or not any(match.groups()):
                    raise ValueError()
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    start = max(0, size - int(last))
                if start >= size or start > end:
                    raise ValueError()
                status = 206
            except ValueError:
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{size}")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
        length = max(0, end - start + 1)
        handler.send_response(status)
        handler.send_header("Content-Type", "video/mp4" if path.suffix == ".mp4" else "video/x-matroska")
        handler.send_header("Content-Length", str(length))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Content-Disposition", f'inline; filename="{path.name}"')
        if status == 206:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        handler.end_headers()
        if head:
            return
        stream.seek(start)
        try:
            while length:
                block = stream.read(min(1024 * 1024, length))
                if not block:
                    break
                handler.wfile.write(block)
                length -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass
