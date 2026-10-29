from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import camera_catalog as catalog
from camera_jobs import ThumbnailJobs
from camera_media import MediaLibrary
from camera_storage import media_bytes, save_backup_policy, check_backup_budget
import recorder


class ScaleTests(unittest.TestCase):
    def test_sql_index_reconciles_add_trash_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = root / "recording-20261029-000001.mp4"
            one.write_bytes(b"one")
            library = MediaLibrary(root, "recording")
            self.assertEqual(library.list()["total"], 1)
            two = root / "recording-20261029-000002.mp4"
            two.write_bytes(b"two")
            self.assertEqual(library.list("limit=1")["items"][0]["filename"], two.name)
            receipt = library.move_to_trash(two.name)
            self.assertEqual(library.list()["total"], 1)
            library.restore(receipt["token"])
            self.assertEqual(MediaLibrary(root, "recording").list()["total"], 2)

    def test_thumbnail_jobs_deduplicate_and_bound_waiting_work(self):
        jobs = ThumbnailJobs(workers=1, capacity=1)
        entered, release = threading.Event(), threading.Event()
        calls = []
        first_identity = []
        def task():
            calls.append(1)
            entered.set()
            release.wait(3)
            return "ready"
        try:
            with ThreadPoolExecutor(max_workers=2) as clients:
                def first_request():
                    first_identity.append(threading.get_ident())
                    return jobs.get("same", task)
                first = clients.submit(first_request)
                entered.wait(1)
                with jobs.lock:
                    work = jobs.pending["same"]
                original_result = work.result
                second_waiting = threading.Event()
                def observed_result(*args, **kwargs):
                    if threading.get_ident() != first_identity[0]:
                        second_waiting.set()
                    return original_result(*args, **kwargs)
                with patch.object(work, "result", side_effect=observed_result):
                    second = clients.submit(jobs.get, "same", task)
                    self.assertTrue(second_waiting.wait(2))
                    with self.assertRaises(RuntimeError):
                        jobs.get("other", task)
                    release.set()
                self.assertEqual((first.result(), second.result()), ("ready", "ready"))
                self.assertEqual(len(calls), 1)
        finally:
            release.set()
            jobs.pool.shutdown()

    def test_metadata_survives_memory_cache_reset_and_invalidates_on_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording-20261029-000001.mp4"
            path.write_bytes(b"fixture")
            response = type("Result", (), {"returncode": 0, "stdout": json.dumps({"streams":[{"codec_type":"video"}],"format":{"duration":"4"}})})()
            with patch.object(catalog.subprocess, "run", return_value=response) as probe:
                self.assertEqual(catalog.inspect_recording(str(path))["duration_seconds"], 4)
                catalog._cache.clear()
                self.assertEqual(catalog.inspect_recording(str(path))["health"], "ok")
                self.assertEqual(probe.call_count, 1)
                path.write_bytes(b"changed fixture")
                catalog.inspect_recording(str(path))
                self.assertEqual(probe.call_count, 2)

    def test_recording_budget_counts_trash_and_rejects_without_deleting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trash = root / ".trash"
            trash.mkdir()
            (trash / "media.mp4").write_bytes(b"x" * 1048576)
            self.assertEqual(media_bytes(root), 1048576)
            with patch.object(recorder, "VIDEO_DIR", directory), patch.object(recorder, "MAX_STORAGE_MB", 1):
                self.assertTrue(recorder.storage_exhausted())
                with self.assertRaises(RuntimeError):
                    recorder.start_recording()
            self.assertTrue((trash / "media.mp4").exists())

    def test_backup_budget_is_persistent_and_never_removes_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "backups"
            store.mkdir()
            (store / "keep.txt").write_bytes(b"keep")
            state = root / "state"
            save_backup_policy(state, {"max_backup_gb":1})
            with self.assertRaises(RuntimeError):
                check_backup_budget(store, state, 2 * 1024 ** 3)
            self.assertEqual((store / "keep.txt").read_bytes(), b"keep")
            for value in (-1, True, 0.1, "1"):
                with self.assertRaises(ValueError):
                    save_backup_policy(state, {"max_backup_gb":value})
            save_backup_policy(state, {"max_backup_gb":0})
            check_backup_budget(store, state, 2 * 1024 ** 3)
