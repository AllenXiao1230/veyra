"""Bounded, deduplicated thumbnail work, independent of HTTP request threads."""
from concurrent.futures import ThreadPoolExecutor
import threading


class ThumbnailJobs:
    def __init__(self, workers=2, capacity=32):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="thumbnail")
        self.slots = threading.BoundedSemaphore(capacity)
        self.lock = threading.RLock()
        self.pending = {}

    def get(self, key, operation, timeout=30):
        with self.lock:
            future = self.pending.get(key)
            if future is None:
                if not self.slots.acquire(blocking=False):
                    raise RuntimeError("縮圖佇列已滿，請稍後重新整理")
                future = self.pool.submit(operation)
                self.pending[key] = future
                def complete(_):
                    with self.lock:
                        self.pending.pop(key, None)
                        self.slots.release()
                future.add_done_callback(complete)
        return future.result(timeout=timeout)
