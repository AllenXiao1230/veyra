import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

spec = importlib.util.spec_from_file_location("maintenance", Path(__file__).with_name("camera_maintenance.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.recordings = self.root / "recordings"
        (self.data / "snapshots").mkdir(parents=True)
        self.recordings.mkdir()
        self.config = self.root / "policy.json"
        self.config.write_text('{"retention_days":30}')
        self.old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self.new = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.data / "events.sqlite3") as db:
            db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,created_at TEXT,snapshot_name TEXT)")
            db.executemany("INSERT INTO events VALUES(?,?,?)", [(1,self.old,"old.jpg"),(2,self.new,"new.jpg")])
        for name in ["old.jpg", "new.jpg"]:
            (self.data / "snapshots" / name).write_bytes(b"fixture")
        self.video = self.recordings / "recording-20200101-000000.mp4"
        self.video.write_bytes(b"original")
        past = time.time() - 31*86400
        os.utime(self.video, (past,past))
        self.patcher = patch.multiple(m, DATA=self.data, RECORDINGS=self.recordings, CONFIG=self.config,
                                      STATE=self.root / "state", BACKUPS=self.root / "backups")
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temporary.cleanup()

    def test_retention_preview_is_read_only_and_uses_utc(self):
        plan = m.cleanup()
        self.assertEqual([row[0] for row in plan["events"]], [1])
        self.assertEqual(plan["recordings"], [str(self.video)])
        self.assertTrue(self.video.exists())
        with sqlite3.connect(self.data / "events.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 2)

    def test_disabled_cleanup_cannot_delete_or_backup(self):
        self.config.write_text('{"retention_days":0}')
        with patch.object(m, "backup") as backup:
            self.assertFalse(m.cleanup(apply=True)["enabled"])
            backup.assert_not_called()
        self.assertTrue(self.video.exists())

    def test_invalid_snapshot_path_is_rejected_before_database_mutation(self):
        with sqlite3.connect(self.data / "events.sqlite3") as db:
            db.execute("UPDATE events SET snapshot_name='../escape' WHERE id=1")
        with patch.object(m, "backup") as backup:
            with self.assertRaises(RuntimeError):
                m.cleanup(apply=True)
            backup.assert_not_called()
        with sqlite3.connect(self.data / "events.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 2)

    def test_two_failures_restart_with_cooldown_but_preserve_encoder_failure(self):
        def state(url):
            if ":8092" in url:
                return {"healthy":False}
            if ":8091" in url:
                return {"healthy":False,"frame_age_seconds":0.1,"recording_state":"failed"}
            return {"healthy":True}
        with patch.object(m, "http_json", side_effect=state), patch.object(m.subprocess, "run") as restart:
            m.health(repair=True)
            restart.assert_not_called()
            m.health(repair=True)
            self.assertEqual(restart.call_count, 1)
            self.assertEqual(restart.call_args.args[0], ["systemctl","restart","camera-ai"])
            m.health(repair=True)
            self.assertEqual(restart.call_count, 1)

    def test_backup_incremental_links_never_link_live_files(self):
        with patch.object(m, "http_json", return_value={"recording":False}):
            first = Path(m.backup()["path"])
            second = Path(m.backup()["path"])
            a = first / "recordings" / self.video.name
            b = second / "recordings" / self.video.name
            self.assertEqual(a.stat().st_ino, b.stat().st_ino)
            self.assertNotEqual(a.stat().st_ino, self.video.stat().st_ino)
            self.video.write_bytes(b"updated-data")
            third = Path(m.backup()["path"])
            self.assertEqual(a.read_bytes(), b"original")
            self.assertEqual((third / "recordings" / self.video.name).read_bytes(), b"updated-data")
            with sqlite3.connect(first / "events.sqlite3") as db:
                self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
