import http.client
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import threading
import time
import unittest

from camera_backups import BackupStore, index_files, verify_backup
from camera_state import atomic_json
from backup_api import Handler, Jobs, ThreadingHTTPServer


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = BackupStore(self.root / "backups", self.root / "state")

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, name="20261029-010000-000001", legacy=False):
        folder = self.store.root / name
        (folder / "snapshots").mkdir(parents=True)
        (folder / "snapshots" / "sample.jpg").write_bytes(b"fixture bytes")
        with sqlite3.connect(folder / "events.sqlite3") as db:
            db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,snapshot_name TEXT)")
            db.execute("INSERT INTO events VALUES(1,'sample.jpg')")
        db.close()
        with tarfile.open(folder / "configuration.tar.gz", "w:gz") as archive:
            entry = tarfile.TarInfo("opt/camera-ai/camera_ai.py")
            entry.size = 7
            archive.addfile(entry, io.BytesIO(b"fixture"))
        manifest = {"complete": True, "created_at": time.time()}
        if not legacy:
            manifest.update(schema=2, files=index_files(folder))
        atomic_json(folder / "manifest.json", manifest)
        return folder

    def test_verification_detects_tampering_and_extra_files(self):
        folder = self.fixture()
        self.assertTrue(verify_backup(folder)["ok"])
        (folder / "snapshots" / "sample.jpg").write_bytes(b"tampered!!!!!")
        with self.assertRaises(ValueError):
            verify_backup(folder)
        (folder / "snapshots" / "sample.jpg").write_bytes(b"fixture bytes")
        (folder / "unexpected.txt").write_text("extra")
        with self.assertRaises(ValueError):
            verify_backup(folder)

    def test_legacy_verification_establishes_baseline_explicitly(self):
        folder = self.fixture(legacy=True)
        first = verify_backup(folder)
        self.assertTrue(first["legacy_baseline_created"])
        self.assertFalse(verify_backup(folder)["legacy_baseline_created"])

    def test_trash_is_reversible_and_latest_is_protected(self):
        old = self.fixture()
        latest = self.fixture("20261029-020000-000001")
        (self.store.root / "latest").symlink_to(latest.name, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.store.move_to_trash(latest.name)
        before = (old / "snapshots" / "sample.jpg").read_bytes()
        receipt = self.store.move_to_trash(old.name)
        self.assertFalse(old.exists())
        self.assertEqual(len(self.store.list_trash()), 1)
        self.store.restore(receipt["token"])
        self.assertEqual((old / "snapshots" / "sample.jpg").read_bytes(), before)
        self.assertEqual(self.store.list_trash(), [])
        self.assertEqual(len(self.store.list()), 2)
        self.assertGreater(self.store.capacity()["physical_bytes"], 0)

    def test_backup_name_manifest_traversal_and_symlink_are_rejected(self):
        folder = self.fixture()
        for name in ("../outside", str(folder), "latest", "", None):
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.store.folder(name)
        manifest = json.loads((folder / "manifest.json").read_text())
        manifest["files"][0]["path"] = "../outside"
        atomic_json(folder / "manifest.json", manifest)
        with self.assertRaises(ValueError):
            verify_backup(folder)

    def test_isolated_restore_copies_without_linking_or_touching_live_data(self):
        folder = self.fixture()
        result = self.store.recover_isolated(folder.name)
        restored = Path(result["path"])
        self.assertFalse(result["live_data_changed"])
        a, b = folder / "snapshots" / "sample.jpg", restored / "snapshots" / "sample.jpg"
        self.assertNotEqual(a.stat().st_ino, b.stat().st_ino)
        b.write_bytes(b"edited recovery")
        self.assertEqual(a.read_bytes(), b"fixture bytes")

    def test_interrupted_restore_receipt_is_reconciled(self):
        folder = self.fixture()
        receipt = self.store.move_to_trash(folder.name)
        saved = self.store.trash / receipt["token"]
        (saved / "data").rename(folder)
        recovered = BackupStore(self.store.root, self.store.state)
        self.assertEqual(recovered.list_trash(), [])
        self.assertTrue(folder.exists())

    def test_jobs_are_durable_exclusive_and_api_rejects_cross_origin(self):
        entered, release = threading.Event(), threading.Event()
        def create(progress):
            entered.set()
            release.wait(3)
            return {"ok": True}
        jobs = Jobs(self.store, create=create)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.jobs = jobs
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("POST", "/create", "{}", {"Content-Type": "application/json", "Origin": "https://other.invalid"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()
            self.assertEqual(jobs.recent(), [])
            job = jobs.submit("create")["job"]
            entered.wait(1)
            with self.assertRaises(RuntimeError):
                jobs.submit("create")
            with self.assertRaises(RuntimeError):
                with self.store.mutation():
                    pass
            release.set()
            self.assertTrue(jobs.lock.acquire(timeout=3))
            jobs.lock.release()
            self.assertEqual(jobs.get(job["id"])["state"], "complete")
            self.assertTrue((self.store.state / "operations.jsonl").is_file())
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_restart_marks_pending_jobs_interrupted(self):
        directory = self.store.state / "jobs"
        directory.mkdir()
        path = directory / ("a" * 32 + ".json")
        atomic_json(path, {"id": "a" * 32, "state": "running"})
        jobs = Jobs(self.store)
        self.assertEqual(jobs.get("a" * 32)["state"], "interrupted")


if __name__ == "__main__":
    unittest.main()
