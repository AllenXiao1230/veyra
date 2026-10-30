"""Resumable file responses with bounded memory and download-specific timeouts."""
from email.utils import formatdate, parsedate_to_datetime
import mimetypes
import os
from pathlib import Path
import re
import stat

TRANSFER_TIMEOUT = 300
TRANSFER_CHUNK = 256 * 1024


def serve_video(handler, path, head=False):
    serve_file(handler, path, head=head, disposition='inline')


def serve_file(handler, path, *, head=False, cache='no-store', attachment=None, disposition=None):
    path = Path(path)
    name = attachment or path.name
    if (attachment or disposition) and (any(c in name for c in '"\\') or
                                        any(ord(c) < 32 or ord(c) == 127 for c in name) or not name.isascii()):
        raise ValueError('Unsafe download filename')
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(str(path), flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Not a regular file')
        size = info.st_size
        etag = '"%x-%x-%x-%x-%x"' % (info.st_dev, info.st_ino, size, info.st_mtime_ns, info.st_ctime_ns)
        start, end, status = 0, size - 1, 200
        requested = handler.headers.get("Range")
        validator = handler.headers.get('If-Range')
        if requested and validator:
            matches = validator == etag
            if not validator.startswith(('"', 'W/')):
                try:
                    date = parsedate_to_datetime(validator)
                    matches = date.tzinfo is not None and int(date.timestamp()) == int(info.st_mtime)
                except (TypeError, ValueError, OverflowError):
                    matches = False
            if not matches:
                requested = None
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
                    if int(last) == 0:
                        raise ValueError()
                    start = max(0, size - int(last))
                if start >= size or start > end:
                    raise ValueError()
                status = 206
            except ValueError:
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{size}")
                handler.send_header("Content-Length", "0")
                handler.send_header('ETag', etag)
                handler.send_header('Accept-Ranges', 'bytes')
                handler.end_headers()
                return
        length = max(0, end - start + 1)
        handler.send_response(status)
        content_type = {'.zip': 'application/zip', '.json': 'application/json', '.csv': 'text/csv',
                        '.mp4': 'video/mp4', '.mkv': 'video/x-matroska'}.get(path.suffix.lower())
        handler.send_header("Content-Type", content_type or mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
        handler.send_header("Content-Length", str(length))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", cache)
        handler.send_header('ETag', etag)
        handler.send_header('Last-Modified', formatdate(info.st_mtime, usegmt=True))
        handler.send_header("X-Content-Type-Options", "nosniff")
        if attachment or disposition:
            handler.send_header('Content-Disposition', f'{"attachment" if attachment else disposition}; filename="{name}"')
        if status == 206:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        handler.end_headers()
        if head:
            return
        stream.seek(start)
        connection = getattr(handler, 'connection', None)
        previous_timeout = connection.gettimeout() if connection else None
        try:
            if connection:
                connection.settimeout(TRANSFER_TIMEOUT)
            # Flush headers before bypassing wfile with the kernel's sendfile path.
            handler.wfile.flush()
            native = connection and callable(getattr(connection, 'sendfile', None))
            while length:
                if native:
                    sent = connection.sendfile(stream, offset=stream.tell(), count=min(TRANSFER_CHUNK, length))
                else:
                    block = stream.read(min(TRANSFER_CHUNK, length))
                    sent = len(block)
                    if sent:
                        handler.wfile.write(block)
                if not sent:
                    handler.close_connection = True
                    break
                length -= sent
        except OSError as exc:
            # Headers/body already started: a second JSON/503 response corrupts
            # the download. Close it cleanly and allow Range + If-Range retry.
            handler.close_connection = True
            if hasattr(handler, 'log_error'):
                handler.log_error('File transfer interrupted (%s); remaining=%d', type(exc).__name__, length)
        finally:
            if connection:
                try:
                    connection.settimeout(previous_timeout)
                except OSError:
                    handler.close_connection = True
