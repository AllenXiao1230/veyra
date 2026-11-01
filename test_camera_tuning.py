import dataclasses
import json
import tempfile
import array
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from camera_ai import Settings, MotionAnalyzer
from camera_motion import MotionPolicy
from camera_stream import command, Publisher, output_audio_level, Handler


class MotionTuningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = dataclasses.replace(Settings.from_file(self.root / 'missing'), data_dir=self.root, warmup_frames=8)
        self.policy = MotionPolicy(self.settings)
        self.still = np.random.default_rng(17).integers(75, 90, (360, 640, 3), dtype=np.uint8)

    def tearDown(self):
        self.temp.cleanup()

    def analyzer(self, **changes):
        analyzer = MotionAnalyzer(self.settings, {**self.policy.get(), **changes})
        for _ in range(60):
            analyzer.inspect(self.still)
        return analyzer

    def moving(self, index, contrast=130):
        frame = self.still.copy()
        x = 150 + index % 2 * 20
        cv2.rectangle(frame, (x, 100), (x + 70, 160), (contrast,) * 3, -1)
        return frame

    def test_global_exposure_changes_and_sensor_noise_do_not_trigger(self):
        analyzer = self.analyzer()
        for brightness in (20, 60, 0, 40, 0):
            active, _ = analyzer.inspect(cv2.add(self.still, np.full_like(self.still, brightness)))
            self.assertFalse(active)
            self.assertFalse(analyzer.activity_active)
        for index in range(5):
            frame = self.still.copy()
            frame[index * 10, index * 10] = 255
            self.assertFalse(analyzer.inspect(frame)[0])

    def test_local_motion_triggers_but_stricter_area_and_difference_reject_it(self):
        normal, area, difference = self.analyzer(), self.analyzer(area_percent=10), self.analyzer(difference=80)
        for index in range(8):
            frame = self.moving(index)
            self.assertTrue(normal.inspect(frame)[0])
            self.assertFalse(area.inspect(frame)[0])
            self.assertFalse(difference.inspect(frame)[0])

    def test_thresholds_persist_and_bad_inputs_do_not_replace_saved_value(self):
        saved = {**self.policy.get(), 'area_percent': 2.1, 'confirm_frames': 5}
        self.policy.save(saved)
        self.assertEqual(MotionPolicy(self.settings).get(), saved)
        for bad in ({**saved, 'area_percent': float('nan')}, {**saved, 'difference': True}, {**saved, 'confirm_frames': 2.5}, {**saved, 'light_compensation': 1}, {**saved, 'unknown': 7}):
            with self.assertRaises(ValueError):
                self.policy.save(bad)
        self.assertEqual(json.loads(self.policy.path.read_text()), saved)
        with patch('camera_motion.atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.policy.save({**saved, 'difference': 24})
        self.assertEqual(self.policy.get(), saved)

    def test_audio_and_video_clock_origins_are_reset_before_async_resampling(self):
        args = command(True)
        self.assertTrue(args[args.index('-vf') + 1].startswith('setpts=PTS-STARTPTS,'))
        self.assertTrue(args[args.index('-filter:a') + 1].startswith('asetpts=PTS-STARTPTS,'))
        # Only arrival-timed JPEG needs a wallclock; preserve microphone RTP timing.
        self.assertEqual(args.count('-use_wallclock_as_timestamps'), 1)
        self.assertLess(args.index('-use_wallclock_as_timestamps'), args.index('-i'))

    def test_silence_watchdog_needs_two_checks_and_live_input_not_a_quiet_room(self):
        publisher = Publisher()
        publisher.session_id = 'current'
        publisher.audio = True
        publisher.observe_audio('old', -120, {'available': True, 'dbfs': -30})
        self.assertFalse(publisher.restart_requested.is_set())
        publisher.observe_audio('current', -120, {'available': True, 'dbfs': -95})
        publisher.observe_audio('current', -120, {'available': True, 'dbfs': -30})
        self.assertFalse(publisher.restart_requested.is_set())
        publisher.observe_audio('current', -120, {'available': True, 'dbfs': -30})
        self.assertTrue(publisher.restart_requested.is_set())
        self.assertEqual(publisher.status()['audio_probe']['state'], 'silent')

    def test_output_probe_measures_samples_and_rejects_empty_reads(self):
        from types import SimpleNamespace
        samples=array.array('h',[1000,-1000]*12000).tobytes()
        with patch('camera_stream.subprocess.run',return_value=SimpleNamespace(stdout=samples)):
            self.assertAlmostEqual(output_audio_level(),-30.3,places=1)
        with patch('camera_stream.subprocess.run',return_value=SimpleNamespace(stdout=b'')):
            with self.assertRaises(ValueError):output_audio_level()

    def test_restart_api_requires_same_origin_json_before_requesting_restart(self):
        from http.server import ThreadingHTTPServer
        import threading
        import http.client
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.publisher=Publisher()
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for origin,status in (('https://unrelated.invalid',403),('http://127.0.0.1:'+str(server.server_port),200)):
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                connection.request('POST','/restart','{}',{'Content-Type':'application/json','Origin':origin})
                response=connection.getresponse();self.assertEqual(response.status,status);response.read();connection.close()
                self.assertEqual(server.publisher.restart_requested.is_set(),status==200)
        finally:server.shutdown();server.server_close();thread.join(3)


if __name__ == '__main__':
    unittest.main()
