import dataclasses
import http.client
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import camera_ai as ai
import recorder as rec
from camera_media import MediaLibrary


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video_patch = patch.object(rec, "VIDEO_DIR", str(self.root / "videos"))
        self.video_patch.start()
        Path(rec.VIDEO_DIR).mkdir()
        rec.motion_enabled = True
        rec.MOTION_STOP_DELAY = 30
        rec.MIN_FREE_MB = 1024
        rec.MAX_STORAGE_MB = 0
        rec.recorder = None
        rec.recording_state = "idle"
        rec.recording_trigger = None
        rec.active_session_id = None
        rec.session_created_at = None
        rec.session_event_ids = []
        rec.last_motion_at = 0
        rec.last_start_attempt = 0
        rec.motion_suppressed = False
        rec.motion_error = None
        rec.motion_detected = False

    def tearDown(self):
        self.video_patch.stop()
        self.temp.cleanup()

    def test_armed_delay_and_reserve_survive_service_restart(self):
        rec.configure_motion({"enabled": False, "stop_delay": 45, "min_free_mb": 2048, "max_storage_mb": 102400})
        rec.motion_enabled, rec.MOTION_STOP_DELAY, rec.MIN_FREE_MB = True, 30, 1024
        rec.load_settings()
        self.assertEqual((rec.motion_enabled, rec.MOTION_STOP_DELAY, rec.MIN_FREE_MB), (False, 45, 2048))
        self.assertEqual(rec.MAX_STORAGE_MB, 102400)
        for body in ({"enabled": "yes"}, {"stop_delay": True}, {"stop_delay": 601}, {"min_free_mb": -1}):
            with self.assertRaises(ValueError):
                rec.configure_motion(body)
        self.assertEqual(json.loads((Path(rec.VIDEO_DIR) / "settings.json").read_text())["stop_delay"], 45)

    def server(self):
        server = rec.ThreadingHTTPServer(("127.0.0.1", 0), rec.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def post(self, server, path):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("POST", path, "{}", {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200, data)

    def test_manual_commands_do_not_disarm_the_saved_policy(self):
        server = self.server()
        rec.configure_motion({"enabled": True})
        with patch.object(rec, "start_recording", return_value=(True, "started")), \
             patch.object(rec, "stop_recording", return_value=(True, "stopped")), \
             patch.object(rec, "is_recording", return_value=True):
            self.post(server, "/start")
            self.assertTrue(rec.motion_enabled)
            self.post(server, "/stop")
            self.assertTrue(rec.motion_enabled)
            self.assertTrue(rec.motion_suppressed)
        rec.motion_enabled = False
        rec.load_settings()
        self.assertTrue(rec.motion_enabled)

    def test_status_responds_while_encoder_operation_waits(self):
        server = self.server()
        rec.last_frame_tick = time.monotonic()
        held, release = threading.Event(), threading.Event()
        def long_operation():
            with rec.operation_lock:
                held.set()
                release.wait(3)
        worker = threading.Thread(target=long_operation)
        worker.start()
        held.wait(1)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)
            connection.request("GET", "/status")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["healthy"])
            connection.close()
        finally:
            release.set()
            worker.join(2)

    def coordinator_step(self, state):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(state).encode()
        with patch.object(rec.urllib.request, "urlopen", return_value=response), \
             patch.object(rec.time, "sleep", side_effect=KeyboardInterrupt), \
             patch.object(rec, "background_action") as action:
            with self.assertRaises(KeyboardInterrupt):
                rec.motion_detector_worker()
            return action.call_args_list

    def test_motion_records_even_without_person_or_working_ai(self):
        calls = self.coordinator_step({"healthy": True, "frame_age_seconds": 0.1,
                                      "motion_active": True, "motion_sequence": 1,
                                      "last_motion_at": "2026-10-29T00:00:00+00:00",
                                      "person_detector_enabled": False, "trigger_policy": "motion"})
        self.assertEqual(len(calls), 1)
        self.assertTrue(rec.motion_detected)
        self.assertGreater(rec.last_motion_at, 0)

    def test_quiet_countdown_applies_to_motion_not_manual_sessions(self):
        state = {"healthy": True, "frame_age_seconds": 0.1, "motion_active": False}
        rec.last_motion_at = time.monotonic() - 31
        with patch.object(rec, "is_recording", return_value=True):
            rec.recording_trigger = "manual"
            self.assertEqual(self.coordinator_step(state), [])
            rec.recording_trigger = "motion"
            self.assertEqual(self.coordinator_step(state)[0].args[0], rec.stop_after_quiet)

    def test_manual_stop_waits_for_a_full_quiet_period_before_rearming(self):
        rec.motion_suppressed = True
        rec.last_motion_at = time.monotonic() - 2
        state = {"healthy": True, "frame_age_seconds": 0.1, "motion_active": False}
        self.coordinator_step(state)
        self.assertTrue(rec.motion_suppressed)
        rec.last_motion_at -= 30
        self.coordinator_step(state)
        self.assertFalse(rec.motion_suppressed)

    def test_capacity_stop_preserves_armed_policy_and_reports_warning(self):
        from types import SimpleNamespace
        with patch.object(rec, "is_recording", return_value=True), \
             patch.object(rec.shutil, "disk_usage", return_value=SimpleNamespace(free=100)), \
             patch.object(rec, "stop_recording") as stop:
            rec.check_recording_capacity()
            stop.assert_called_once()
        self.assertTrue(rec.motion_enabled)
        self.assertEqual(rec.recording_state, "failed")
        self.assertIn("磁碟", rec.last_error)

    def test_negative_or_failing_ai_still_writes_motion_snapshot(self):
        for failing in (False, True):
            with self.subTest(failing=failing):
                settings = dataclasses.replace(ai.Settings.from_file(self.root / "missing.env"),
                                               data_dir=self.root / str(failing), motion_min_frames=1,
                                               warmup_frames=0, event_cooldown_seconds=0)
                service = ai.DetectorService(settings)
                frame = np.zeros((240, 320, 3), np.uint8)
                capture = MagicMock()
                capture.isOpened.return_value = True
                def read():
                    service.stop_requested.set()
                    return True, frame
                capture.read.side_effect = read
                analyzer = MagicMock(frames_seen=1)
                analyzer.inspect.return_value = (True, 5000)
                service.person_detector.detect = MagicMock(side_effect=RuntimeError("injected AI failure")) if failing else MagicMock(return_value=(False, None, 0))
                with patch.object(ai, "MJPEGCapture", return_value=capture), patch.object(ai, "MotionAnalyzer", return_value=analyzer):
                    service.run()
                rows = service.store.list_events(10)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["event_type"], "motion")
                self.assertFalse(rows[0]["person_detected"])
                self.assertIsNotNone(rows[0]["snapshot_url"])
                self.assertEqual(rows[0]["metadata"]["trigger"], "motion")

    def test_restore_reconciles_process_death_before_or_after_database_commit(self):
        for after in (False, True):
            with self.subTest(after=after):
                root = self.root / str(after)
                snaps = root / "snapshots"
                snaps.mkdir(parents=True)
                db = root / "events.sqlite3"
                with sqlite3.connect(db) as connection:
                    connection.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,snapshot_name TEXT)")
                    connection.execute("INSERT INTO events VALUES(1,'sample.jpg')")
                connection.close()
                (snaps / "sample.jpg").write_bytes(b"preserved fixture bytes")
                library = MediaLibrary(snaps, "snapshot", db)
                receipt = library.move_to_trash("sample.jpg")
                script = '''import sys,sqlite3,os,pathlib
sys.path.insert(0,sys.argv[1])
from camera_media import MediaLibrary
real=sqlite3.connect
class Crash(sqlite3.Connection):
 def commit(self):
  if sys.argv[4]=='True': super().commit()
  os._exit(73)
sqlite3.connect=lambda *a,**k:real(*a,factory=Crash,**k)
p=pathlib.Path(sys.argv[2]);MediaLibrary(p/'snapshots','snapshot',p/'events.sqlite3').restore(sys.argv[3])
'''
                child = subprocess.run([sys.executable, "-c", script, str(Path(__file__).parent), str(root), receipt["token"], str(after)], timeout=15)
                self.assertEqual(child.returncode, 73)
                recovered = MediaLibrary(snaps, "snapshot", db)
                self.assertEqual((snaps / "sample.jpg").read_bytes(), b"preserved fixture bytes")
                with sqlite3.connect(db) as connection:
                    self.assertEqual(connection.execute("SELECT snapshot_name FROM events").fetchone()[0], "sample.jpg")
                connection.close()
                self.assertEqual(recovered.list_trash(), [])
                self.assertEqual(recovered.recovery_issues, [])


if __name__ == "__main__":
    unittest.main()
