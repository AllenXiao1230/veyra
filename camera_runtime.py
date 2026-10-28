"""Bounded MJPEG reader shared by camera analyzers.

The latest complete JPEG replaces older frames before decoding, so slow
analysis never builds a queue of stale frames. OpenCV's JPEG decoder ignores
nonessential camera APP metadata without suppressing unrelated FFmpeg errors.
"""
import threading
import time
import urllib.request

import cv2
import numpy as np


class MJPEGCapture:
    def __init__(self, url, timeout=5.0):
        self.url = url
        self.timeout = timeout
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.response = None
        self.latest = None
        self.frame_id = 0
        self.last_read_id = -1
        self.last_frame_monotonic = 0.0
        self.error = None
        self.worker = threading.Thread(target=self._receive, daemon=True)
        self.worker.start()

    def _receive(self):
        try:
            request = urllib.request.Request(self.url, headers={"User-Agent": "CameraMonitor/2"})
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self.response = response
                buffer = bytearray()
                while not self.stop.is_set():
                    chunk = response.read1(65536)
                    if not chunk:
                        raise EOFError("MJPEG source ended")
                    buffer.extend(chunk)
                    if len(buffer) > 4 * 1024 * 1024:
                        raise ValueError("MJPEG frame exceeds 4 MiB")
                    while True:
                        start = buffer.find(b"\xff\xd8")
                        if start < 0:
                            buffer[:] = buffer[-1:]
                            break
                        if start:
                            del buffer[:start]
                        end = buffer.find(b"\xff\xd9", 2)
                        if end < 0:
                            break
                        jpeg = bytes(buffer[:end + 2])
                        del buffer[:end + 2]
                        with self.condition:
                            self.latest = jpeg
                            self.frame_id += 1
                            self.last_frame_monotonic = time.monotonic()
                            self.condition.notify_all()
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.response = None
            with self.condition:
                self.condition.notify_all()

    def isOpened(self):
        with self.condition:
            self.condition.wait_for(
                lambda: self.latest is not None or self.error or self.stop.is_set(),
                timeout=self.timeout + 1,
            )
            return bool(self.latest and not self.error and not self.stop.is_set()
                        and time.monotonic() - self.last_frame_monotonic < self.timeout + 1)

    def read(self):
        with self.condition:
            self.condition.wait_for(
                lambda: self.frame_id != self.last_read_id or self.error or self.stop.is_set(),
                timeout=self.timeout + 1,
            )
            if (self.stop.is_set() or self.error or self.latest is None
                    or self.frame_id == self.last_read_id
                    or time.monotonic() - self.last_frame_monotonic >= self.timeout + 1):
                return False, None
            jpeg = self.latest
            self.last_read_id = self.frame_id
        frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        return frame is not None, frame

    def release(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
        # The socket has a bounded read timeout. Avoid blocking the supervisor
        # on a close() racing with HTTPResponse.read1() in another thread.
        self.worker.join(timeout=0.2)
