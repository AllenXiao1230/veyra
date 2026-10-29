import io
import json
import pathlib
import sqlite3
import tempfile
import unittest
from urllib.parse import urlencode
from unittest.mock import patch
from contextlib import closing, contextmanager

from camera_media import MediaLibrary, MediaConflict, read_body


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.snapshots = self.root / "snapshots"
        self.snapshots.mkdir()
        self.db = self.root / "events.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, snapshot_name TEXT NOT NULL, created_at TEXT)")
        self.library = MediaLibrary(self.snapshots, "snapshot", self.db)

    def tearDown(self):
        self.temp.cleanup()

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            yield db

    def snapshot(self, name="event-20261028T180000_000001Z.jpg", event_id=1):
        file = self.snapshots / name
        file.write_bytes(b"snapshot fixture")
        with self.connect() as db:
            db.execute("INSERT INTO events VALUES(?,?,?)", (event_id, name, "2026-10-28T18:00:00+00:00"))
        return file

    def test_all_files_are_reachable_in_pagination_including_unreferenced_snapshot(self):
        for i in range(123):
            (self.snapshots / f"event-20261028T180000_{i:06d}Z.jpg").write_bytes(b"image")
        (self.snapshots / "unreferenced.png").write_bytes(b"image")
        seen, query = [], "limit=17"
        while True:
            page = self.library.list(query)
            self.assertEqual(page["total"], 124)
            seen.extend(row["filename"] for row in page["items"])
            if not page["next_before"]:
                break
            query = urlencode({"limit":17,"before":page["next_before"]})
        self.assertEqual(len(seen), 124)
        self.assertEqual(len(set(seen)), 124)
        self.assertIn("unreferenced.png", seen)

    def test_dates_use_taipei_and_filter_does_not_drop_total(self):
        self.snapshot()
        result = self.library.list("date=2026-10-29")
        self.assertEqual(result["filtered_total"], 1)
        self.assertEqual(result["items"][0]["date"], "2026-10-29")
        self.assertEqual(self.library.list("date=2026-10-28")["filtered_total"], 0)
        self.assertEqual(self.library.list("date=2026-10-28")["total"], 1)

    def test_trash_and_restore_preserve_bytes_and_event_references(self):
        file = self.snapshot()
        receipt = self.library.move_to_trash(file.name)
        self.assertFalse(file.exists())
        self.assertEqual(len(self.library.list_trash()), 1)
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT snapshot_name FROM events").fetchone()[0], "")
        self.library.restore(receipt["token"])
        self.assertEqual(file.read_bytes(), b"snapshot fixture")
        self.assertEqual(self.library.list_trash(), [])
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT snapshot_name FROM events").fetchone()[0], file.name)

    def test_database_failure_rolls_back_the_file_move(self):
        file = self.snapshot()
        with self.connect() as db:
            db.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.library.move_to_trash(file.name)
        self.assertEqual(file.read_bytes(), b"snapshot fixture")
        self.assertEqual(self.library.list_trash(), [])

    def test_restore_does_not_overwrite_a_new_same_name_file(self):
        file = self.snapshot()
        receipt = self.library.move_to_trash(file.name)
        file.write_bytes(b"new file")
        with self.assertRaises(MediaConflict):
            self.library.restore(receipt["token"])
        self.assertEqual(file.read_bytes(), b"new file")
        self.assertEqual(len(self.library.list_trash()), 1)

    def test_path_traversal_and_bad_cursor_cannot_reach_outside(self):
        for name in ["../escape.jpg", "/outside.jpg", "sub/file.jpg", "..\\file.jpg", ".hidden.jpg", "bad\x00.jpg"]:
            with self.assertRaises(ValueError):
                self.library.move_to_trash(name)
        for query in ["before=broken", "limit=100000", "date=invalid"]:
            with self.assertRaises(ValueError):
                self.library.list(query)
        with self.assertRaises(ValueError):
            self.library.restore("../escape")

    def test_symlinks_are_not_listed_or_deleted(self):
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"keep")
        link = self.snapshots / "link.jpg"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks need platform permission")
        self.assertEqual(self.library.list()["total"], 0)
        with self.assertRaises(ValueError):
            self.library.move_to_trash(link.name)
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_active_recording_is_protected_and_finished_recording_is_reversible(self):
        directory = self.root / "recordings"
        directory.mkdir()
        file = directory / "recording-20261029-120000.mp4"
        file.write_bytes(b"recording fixture")
        library = MediaLibrary(directory, "recording", active=lambda _: True)
        with self.assertRaises(MediaConflict):
            library.move_to_trash(file.name)
        self.assertTrue(file.exists())
        library.active = lambda _: False
        receipt = library.move_to_trash(file.name)
        library.restore(receipt["token"])
        self.assertEqual(file.read_bytes(), b"recording fixture")

    def test_thumbnail_cache_is_invalidated_when_media_changes(self):
        directory = self.root / "recordings"
        directory.mkdir()
        file = directory / "recording-20261029-120000.mp4"
        file.write_bytes(b"one")
        library = MediaLibrary(directory, "recording")
        with patch("camera_media.subprocess.run") as ffmpeg:
            ffmpeg.return_value.returncode = 0
            ffmpeg.return_value.stdout = b"\xff\xd8jpeg fixture"
            first = library.thumbnail(file.name)
            self.assertEqual(first, library.thumbnail(file.name))
            self.assertEqual(ffmpeg.call_count, 1)
            file.write_bytes(b"two changed")
            second = library.thumbnail(file.name)
            self.assertNotEqual(first, second)
            self.assertEqual(ffmpeg.call_count, 2)

    def test_write_body_rejects_cross_site_and_invalid_payloads(self):
        class Handler:
            pass
        handler = Handler()
        for headers, body, exception in [
            ({"Origin":"https://evil.invalid","Host":"localhost","Content-Type":"application/json"}, b"{}", PermissionError),
            ({"Content-Type":"text/plain"}, b"{}", ValueError),
            ({"Content-Type":"application/json","Content-Length":"-1"}, b"", ValueError),
            ({"Content-Type":"application/json","Content-Length":"2"}, b"[]", ValueError),
        ]:
            handler.headers, handler.rfile = headers, io.BytesIO(body)
            with self.assertRaises(exception):
                read_body(handler)


if __name__ == "__main__":
    unittest.main()
