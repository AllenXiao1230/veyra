import dataclasses
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import threading
import urllib.request
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import camera_ai as ai
import camera_playback as playback
import recorder as rec
import timestamp_v2 as stamp
import test_camera_hybrid as hybrid


class MotionStabilityTests(unittest.TestCase):
    setUp = hybrid.HybridTests.setUp
    tearDown = hybrid.HybridTests.tearDown
    coordinator_step = hybrid.HybridTests.coordinator_step
    def test_small_continuous_motion_survives_background_adaptation(self):
        settings = dataclasses.replace(ai.Settings.from_file(self.root / 'absent'), warmup_frames=10)
        analyzer = ai.MotionAnalyzer(settings)
        still = np.zeros((360, 640, 3), np.uint8)
        for _ in range(600):
            analyzer.inspect(still)
        for index in range(100):
            frame = still.copy()
            x = 100 + (index % 2) * 20
            cv2.rectangle(frame, (x, 100), (x + 20, 120), (255, 255, 255), -1)
            analyzer.inspect(frame)
            self.assertTrue(analyzer.activity_active, index)
        analyzer.inspect(frame)
        self.assertFalse(analyzer.activity_active)
        noisy = frame.copy()
        noisy[5, 5] = 255
        analyzer.inspect(noisy)
        self.assertFalse(analyzer.activity_active)

    def test_small_motion_extends_recording_without_requiring_a_new_trigger(self):
        rec.last_motion_at = time.monotonic() - 40
        rec.recording_trigger = 'motion'
        with patch.object(rec, 'is_recording', return_value=True):
            calls = self.coordinator_step({'healthy': True, 'frame_age_seconds': .1,
                                          'motion_active': False, 'activity_active': True})
        self.assertEqual(calls, [])
        self.assertLess(time.monotonic() - rec.last_motion_at, 1)

    def test_status_token_does_not_need_to_change_to_extend_activity(self):
        rec.last_motion_at = time.monotonic() - 40
        with patch.object(rec, 'is_recording', return_value=True):
            self.coordinator_step({'healthy': True, 'frame_age_seconds': .1,
                                   'motion_active': True, 'motion_sequence': 1})
        self.assertLess(time.monotonic() - rec.last_motion_at, 1)

    def test_unhealthy_observations_do_not_stop_a_recording(self):
        rec.last_motion_at = time.monotonic() - 40
        rec.recording_trigger = 'motion'
        with patch.object(rec, 'is_recording', return_value=True):
            self.assertEqual(self.coordinator_step({'healthy': False, 'frame_age_seconds': 20}), [])
        self.assertIsNotNone(rec.motion_error)
        self.assertLess(time.monotonic() - rec.last_motion_at, 1)

    def test_network_error_does_not_count_as_quiet(self):
        rec.last_motion_at = time.monotonic() - 40
        with patch.object(rec.urllib.request, 'urlopen', side_effect=TimeoutError('injected')):
            with patch.object(rec.time, 'sleep', side_effect=KeyboardInterrupt), patch.object(rec, 'background_action') as action:
                with self.assertRaises(KeyboardInterrupt):
                    rec.motion_detector_worker()
        action.assert_not_called()
        self.assertLess(time.monotonic() - rec.last_motion_at, 1)

    def test_queued_stop_rechecks_new_motion_and_freshness(self):
        rec.recording_trigger = 'motion'
        rec.last_motion_at = time.monotonic() - 40
        rec.last_frame_tick = time.monotonic()
        with patch.object(rec, 'stop_recording') as stop:
            rec.motion_detected = True
            rec.stop_after_quiet()
            stop.assert_not_called()
            rec.motion_detected = False
            rec.last_frame_tick -= 10
            rec.stop_after_quiet()
            stop.assert_not_called()
            rec.last_frame_tick = time.monotonic()
            rec.stop_after_quiet()
            stop.assert_called_once()

    def test_ninety_seconds_of_small_motion_never_queues_stop(self):
        clock = [100.0]
        rec.recording_trigger = 'motion'
        rec.last_motion_at = 99
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'healthy': True, 'frame_age_seconds': .1, 'motion_active': False,
            'activity_active': True, 'activity_age_seconds': .1}).encode()
        def tick(_):
            clock[0] += 1
            if clock[0] >= 190:
                raise KeyboardInterrupt
        with patch.object(rec.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(rec.time, 'sleep', side_effect=tick), \
             patch.object(rec.urllib.request, 'urlopen', return_value=response), \
             patch.object(rec, 'is_recording', return_value=True), \
             patch.object(rec, 'storage_exhausted', return_value=False), \
             patch.object(rec, 'background_action') as action:
            with self.assertRaises(KeyboardInterrupt):
                rec.motion_detector_worker()
        action.assert_not_called()
        self.assertEqual(rec.last_motion_at, 189)

    def test_manual_stop_is_not_rearmed_when_a_brief_motion_was_between_polls(self):
        rec.last_motion_at = time.monotonic() - 40
        rec.motion_suppressed = True
        self.coordinator_step({'healthy': True, 'frame_age_seconds': .1,
                               'motion_active': False, 'activity_active': False,
                               'activity_age_seconds': .5})
        self.assertTrue(rec.motion_suppressed)


class StreamGapTests(unittest.TestCase):
    def test_low_bandwidth_jpeg_etag_and_stale_frame(self):
        import http.client
        with patch.multiple(stamp, lock=threading.Condition(),stop_requested=threading.Event(),
                latest_frame=b'full',low_frame=b'jpeg fixture',low_frame_id=8,
                low_frame_tick=time.monotonic(),last_frame_tick=time.monotonic(),worker=MagicMock()):
            server=stamp.ThreadingHTTPServer(('127.0.0.1',0),stamp.Handler)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                for requested,expected in ((None,200),('"low-8"',304)):
                    conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                    conn.request('GET','/frame.jpg',headers={'If-None-Match':requested} if requested else {})
                    response=conn.getresponse();body=response.read()
                    self.assertEqual(response.status,expected)
                    self.assertEqual(body,b'jpeg fixture' if expected==200 else b'');conn.close()
                stamp.low_frame_tick-=5
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3);conn.request('GET','/frame.jpg')
                response=conn.getresponse();self.assertEqual(response.status,503);response.read();conn.close()
            finally:server.shutdown();server.server_close()

    def test_two_second_source_gap_keeps_preview_and_recording_stream_open(self):
        for route in ('/stream', '/preview'):
            with self.subTest(route=route), patch.multiple(stamp, lock=threading.Condition(),
                    stop_requested=threading.Event(), latest_frame=b'original', preview_frame=b'small',
                    frame_id=1, preview_frame_id=1, last_frame_tick=time.monotonic(), worker=MagicMock()):
                server = stamp.ThreadingHTTPServer(('127.0.0.1', 0), stamp.Handler)
                server.daemon_threads = True
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                def payload(response):
                    self.assertTrue(response.readline().startswith(b'--timestampframe'))
                    response.readline()
                    size = int(response.readline().split(b':')[1])
                    response.readline()
                    data = response.read(size)
                    response.read(2)
                    return data
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}{route}', timeout=5) as response:
                        self.assertEqual(payload(response), b'small' if route == '/preview' else b'original')
                        def resume():
                            time.sleep(2.3)
                            with stamp.lock:
                                stamp.frame_id = stamp.preview_frame_id = 2
                                stamp.latest_frame = stamp.preview_frame = b'resumed'
                                stamp.last_frame_tick = time.monotonic()
                                stamp.lock.notify_all()
                        resumed = threading.Thread(target=resume)
                        resumed.start()
                        self.assertEqual(payload(response), b'resumed')
                        resumed.join()
                finally:
                    stamp.stop_requested.set()
                    with stamp.lock:
                        stamp.lock.notify_all()
                    server.shutdown()
                    server.server_close()


class PlaybackTests(unittest.TestCase):
    def test_fragmented_video_gets_complete_index_without_changing_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'original.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=15',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '4.2',
                            '-c:v', 'mpeg4', '-g', '15', '-c:a', 'aac', '-movflags',
                            '+frag_keyframe+empty_moov+default_base_moof', str(source)], check=True)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with patch.object(playback, 'CACHE', root / 'cache'):
                target = playback.playback_file(source)
                self.assertEqual(playback.playback_file(source), target)
                before, after = playback.probe(source), playback.probe(target)
                self.assertLess(abs(float(before['format']['duration']) - float(after['format']['duration'])), .15)
                data = target.read_bytes()
                self.assertLess(data.index(b'moov'), data.index(b'mdat'))
                self.assertNotIn(b'moof', data)
                def decoded(path):
                    return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v:0',
                                                    '-f', 'hash', '-hash', 'sha256', '-'])
                self.assertEqual(decoded(source), decoded(target))
                target.unlink()
                # A stream-copy muxer can produce a slightly larger file than
                # its source; enforce the budget on the final bytes too.
                with patch.object(playback, 'MAX_BYTES', source.stat().st_size + 1), \
                     patch.object(playback.subprocess, 'run') as command:
                    def grow(args, **kwargs):
                        Path(args[-1]).write_bytes(b'x' * (source.stat().st_size + 2))
                    command.side_effect = grow
                    with patch.object(playback, 'probe', return_value=before):
                        with self.assertRaisesRegex(RuntimeError, '上限'):
                            playback.playback_file(source)
                self.assertEqual(list((root / 'cache').iterdir()), [])
            self.assertEqual(digest, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_failed_remux_keeps_original_and_no_partial_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'original.mp4'
            source.write_bytes(b'invalid fixture')
            with patch.object(playback, 'CACHE', root / 'cache'):
                with self.assertRaises(subprocess.CalledProcessError):
                    playback.playback_file(source)
            self.assertEqual(source.read_bytes(), b'invalid fixture')
            self.assertEqual(list((root / 'cache').iterdir()), [])
