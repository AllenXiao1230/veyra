import dataclasses
import http.client
import importlib.util
import io
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from http.server import ThreadingHTTPServer


def load(name, file):
    import sys
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ai = load("candidate_ai", "camera_ai.py")
rec = load("candidate_rec", "recorder.py")
preview = load("candidate_preview", "timestamp_v2.py")


class FixTests(unittest.TestCase):
    def test_ai_supervisor_recovers_after_worker_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = dataclasses.replace(ai.Settings.from_file(pathlib.Path(folder) / "missing.env"),
                                           data_dir=pathlib.Path(folder))
            service = ai.DetectorService(settings)
            calls = []

            def run():
                calls.append(1)
                if len(calls) == 1:
                    raise OSError("injected disk failure")
                service.update_state(running=True, stream_connected=True, last_frame_at=ai.utc_now(), last_error=None)
                service.stop_requested.set()

            service.run = run
            service.supervise()
            self.assertEqual(len(calls), 2)
            self.assertEqual(service.recoveries, 1)
            self.assertTrue(service.status()["healthy"])
            service.last_frame_tick -= 60
            self.assertFalse(service.status()["healthy"])

    def test_legacy_post_is_gone_and_writes_nothing(self):
        handler = object.__new__(ai.ApiHandler)
        handler.path = "/events/browser-motion"
        handler.send_json = MagicMock()
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[0], 410)
        self.assertFalse(hasattr(ai.DetectorService, "record_browser_motion"))

    def test_preview_stale_frame_and_dead_worker_are_unhealthy(self):
        preview.worker = MagicMock()
        preview.worker.is_alive.return_value = True
        preview.latest_frame = b"jpeg"
        preview.last_frame_tick = time.monotonic()
        self.assertTrue(preview.health()["healthy"])
        preview.last_frame_tick -= 20
        self.assertFalse(preview.health()["healthy"])
        preview.last_frame_tick = time.monotonic()
        preview.worker.is_alive.return_value = False
        self.assertFalse(preview.health()["healthy"])

    def test_failed_ffmpeg_never_reports_recording_success(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = MagicMock()
            proc.poll.return_value = 1
            proc.wait.return_value = 1
            proc.stdout = io.StringIO("")
            response = MagicMock()
            response.__enter__.return_value.status = 200
            rec.recorder = None
            rec.recording_state = "idle"
            with patch.object(rec, "VIDEO_DIR", folder), patch.object(rec, "LOG_FILE", folder + "/ffmpeg.log"), \
                 patch("urllib.request.urlopen", return_value=response), patch.object(rec.subprocess, "Popen", return_value=proc):
                with self.assertRaises(RuntimeError):
                    rec.start_recording()
                self.assertEqual(rec.recording_state, "failed")
                self.assertFalse(rec.is_recording())
                self.assertIn("FFmpeg", rec.last_error)
            rec.recorder = None

    def test_missing_timestamp_stream_fails_without_raw_fallback(self):
        rec.recorder = None
        with tempfile.TemporaryDirectory() as folder, patch.object(rec, "VIDEO_DIR", folder), \
             patch("urllib.request.urlopen", side_effect=OSError("timestamp source offline")), \
             patch.object(rec.subprocess, "Popen") as encoder:
            with self.assertRaisesRegex(RuntimeError, "時間戳"):
                rec.start_recording()
            encoder.assert_not_called()
            self.assertEqual(rec.recording_state, "failed")
        rec.recorder = None

    def test_unexpected_recorder_exit_is_persistent_failure(self):
        proc = MagicMock()
        proc.poll.return_value = 7
        proc._camera_expected_stop = False
        rec.recorder = proc
        rec.recording_state = "recording"
        with patch.object(rec, "recorder_error", return_value="injected encoder failure"):
            self.assertFalse(rec.is_recording())
            self.assertEqual(rec.recording_state, "failed")
            self.assertEqual(rec.last_error, "injected encoder failure")
        rec.recorder = None

    def test_recorder_rejects_bad_input_and_cross_origin(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), rec.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host = f"127.0.0.1:{server.server_port}"
            cases = [
                ({"Content-Type": "text/plain"}, "{}", 400),
                ({"Content-Type": "application/json"}, "[]", 400),
                ({"Content-Type": "application/json", "Origin": "https://other.invalid"}, "{}", 403),
                ({"Content-Type": "application/json", "Content-Length": "-1"}, "", 400),
            ]
            for headers, body, code in cases:
                conn = http.client.HTTPConnection(host, timeout=3)
                conn.request("POST", "/motion", body=body, headers=headers)
                response = conn.getresponse()
                self.assertEqual(response.status, code)
                response.read()
                conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
