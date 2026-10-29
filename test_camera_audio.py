import http.client
import json
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from http.server import ThreadingHTTPServer

import camera_audio as audio


DEVICE = {"name": "test.microphone", "index": 12, "description": "Test microphone"}


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temp.name) / "audio.json"
        self.service = audio.AudioService(self.path)
        self.service.config = {"source": DEVICE["name"], "volume": 80, "muted": False}

    def tearDown(self):
        self.temp.cleanup()

    def healthy_process(self):
        self.service.process = MagicMock(pid=100)
        self.service.process.poll.return_value = None
        self.service.process_revision = self.service.revision
        self.service.last_progress = self.service.last_packet = time.monotonic()
        self.service.actual_source = DEVICE["name"]

    def test_alive_process_without_fresh_packets_or_progress_is_unhealthy(self):
        self.healthy_process()
        self.assertTrue(self.service.status()["healthy"])
        self.service.last_progress -= 11
        self.assertFalse(self.service.status()["healthy"])
        self.service.last_progress = time.monotonic()
        self.service.last_packet -= 21
        self.assertFalse(self.service.status()["healthy"])
        self.service.last_packet = time.monotonic()
        self.service.actual_source = "other.microphone"
        self.assertFalse(self.service.status()["healthy"])

    def test_configuration_rejects_unlisted_sources_and_invalid_volume(self):
        with patch.object(audio, "sources", return_value=[DEVICE]):
            for payload in [{"source": "test;touch /tmp/bad"},
                            {"source": DEVICE["name"], "volume": True},
                            {"source": DEVICE["name"], "volume": 101},
                            {"source": DEVICE["name"], "muted": "false"}]:
                with self.assertRaises(ValueError):
                    self.service.configure(payload)
        self.assertFalse(self.path.exists())

    def test_selection_survives_restart_and_invalidates_old_health(self):
        self.healthy_process()
        with patch.object(audio, "sources", return_value=[DEVICE]):
            self.service.configure({"source": DEVICE["name"], "volume": 65, "muted": True})
        self.assertFalse(self.service.status()["healthy"])
        # A late stdout/probe update from the old publisher cannot restore healthy.
        self.service.last_progress = self.service.last_packet = time.monotonic()
        self.service.actual_source = DEVICE["name"]
        self.assertFalse(self.service.status()["healthy"])
        recreated = audio.AudioService(self.path)
        self.assertEqual(recreated.config, {"source": DEVICE["name"], "volume": 65, "muted": True})

    def test_hung_process_is_killed_and_recovery_backoff_is_bounded(self):
        self.healthy_process()
        proc = self.service.process
        proc.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 2), 0]
        self.service.failed("stalled")
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertIsNone(self.service.process)
        for _ in range(8):
            self.service.failed("unavailable")
        retry = self.service.status()["retry_in_seconds"]
        self.assertGreaterEqual(retry, 59)
        self.assertLessEqual(retry, 60)
        self.service.request_restart()
        self.assertEqual(self.service.status()["retry_in_seconds"], 0)

    def test_supervisor_detects_stall_even_when_process_does_not_exit(self):
        self.healthy_process()
        self.service.revision = -1
        self.service.last_progress -= 25
        self.service.started = time.monotonic() - 30
        def failed(message):
            self.assertIn("停止前進", message)
            self.service.stop_event.set()
        with patch.object(audio, "sources", return_value=[DEVICE]), \
             patch.object(self.service, "route"), patch.object(self.service, "failed", side_effect=failed) as fail, \
             patch.object(self.service, "stop_process"):
            self.service.supervise()
        fail.assert_called_once()

    def test_route_drift_is_corrected_before_health_can_pass(self):
        self.healthy_process()
        self.service.devices = [DEVICE]
        moved = [{"index": 9, "source": 15, "properties": {"application.process.id": "100"}}]
        with patch.object(audio, "run", side_effect=[json.dumps(moved), ""]) as run:
            self.service.route()
            self.assertEqual(run.call_args.args[0], ["pactl", "move-source-output", "9", DEVICE["name"]])
        self.assertFalse(self.service.status()["healthy"])

    def test_rtsp_metadata_alone_is_not_packet_health(self):
        with patch.object(audio, "run", return_value='{"streams":[{"codec_type":"audio"}]}'):
            with self.assertRaises(RuntimeError):
                audio.probe_packets()
        with patch.object(audio, "run", return_value='{"packets":[{"pts_time":"0.02"}]}'):
            audio.probe_packets()

    def test_api_rejects_cross_origin_bad_payload_and_recording_conflict(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), audio.Handler)
        server.audio = self.service
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            cases = [({"Content-Type": "text/plain"}, "{}", 400),
                     ({"Content-Type": "application/json"}, "[]", 400),
                     ({"Content-Type": "application/json", "Content-Length": "-1"}, "", 400),
                     ({"Content-Type": "application/json", "Origin": "https://other.invalid"}, "{}", 403),
                     ({"Content-Type": "application/json"}, "{}", 409)]
            with patch.object(audio, "recording_active", return_value=True), \
                 patch.object(self.service, "request_restart") as restart:
                for headers, body, expected in cases:
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    connection.request("POST", "/restart", body=body, headers=headers)
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                    connection.close()
                restart.assert_not_called()
            with patch.object(audio, "recording_active", return_value=False):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("POST", "/restart", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                response.read()
                connection.close()
                self.assertTrue(self.service.wake.is_set())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
