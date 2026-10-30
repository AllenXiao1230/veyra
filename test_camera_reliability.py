import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, patch

import recorder as rec
from camera_sessions import reconcile_sessions
from camera_storage import reserve_space, StoragePressure
from camera_diagnostics import ingress_check, PROBE
from camera_timeline import Timeline
import test_camera_events as events
import test_camera_maintenance as maintenance
m=maintenance.m
import test_camera_hybrid as hybrid


class RecordingReliabilityTests(unittest.TestCase):
    def setUp(self):
        hybrid.HybridTests.setUp(self)
        rec.recording_requested=False
        rec.recording_warning=None
        rec.recovery_after=0
        rec.recovery_attempts=0
        rec.recording_audio=True
        rec.audio_checked_at=time.monotonic()

    tearDown=hybrid.HybridTests.tearDown

    def proc(self):
        now=time.monotonic()
        return SimpleNamespace(poll=lambda:None,_camera_created_at=now-50,
            _camera_last_progress=now-30,_camera_growth_at=now-30,
            _camera_output_bytes=0,_camera_out_time=100)

    def test_stalled_output_restarts_even_when_input_frames_are_fresh(self):
        rec.recorder=self.proc();rec.recording_requested=True;rec.recording_state='recording'
        rec.recording_trigger='manual';rec.last_frame_tick=time.monotonic()
        self.assertTrue(rec.encoder_status()['stalled'])
        with patch.object(rec,'stop_recording') as stop,patch.object(rec,'start_recording') as start,patch.object(rec,'persist_session'):
            rec.check_encoder()
            stop.assert_called_once_with(preserve_intent=True)
            start.assert_called_once_with('manual',_audio=False)
        self.assertGreater(rec.recovery_after,time.monotonic())

    def test_manual_stop_cancels_a_waiting_recovery(self):
        rec.recorder=self.proc();rec.recording_requested=True;rec.recording_state='recording'
        def stopped(**_):rec.recording_requested=False
        with patch.object(rec,'stop_recording',side_effect=stopped),patch.object(rec,'start_recording') as start:
            rec.check_encoder();start.assert_not_called()

    def test_manual_recovery_retries_after_source_preflight_failure(self):
        rec.recorder=None;rec.recording_requested=True;rec.recording_trigger='manual'
        with patch.object(rec,'start_recording',side_effect=[RuntimeError('source offline'),(True,'ok')]) as start,patch.object(rec,'persist_session'):
            rec.check_encoder()
            self.assertTrue(rec.recording_requested)
            rec.check_encoder();self.assertEqual(start.call_count,1)
            rec.recovery_after=0
            rec.check_encoder();self.assertEqual(start.call_count,2)

    def test_video_only_command_does_not_open_an_unavailable_rtsp_input(self):
        class Captured(Exception):pass
        captured=[]
        def launch(cmd,**_):captured.extend(cmd);raise Captured()
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock();response.status=200
        with patch.object(rec,'LOG_FILE',str(self.root/'ffmpeg.log')),patch.object(rec.urllib.request,'urlopen',return_value=response),patch.object(rec.subprocess,'Popen',side_effect=launch):
            with self.assertRaises(Captured):rec._start_recording('manual',False)
        self.assertNotIn(rec.AUDIO_SOURCE,captured);self.assertNotIn('1:a:0',captured);self.assertIn('-an',captured)

    def test_progress_not_process_presence_controls_encoder_health(self):
        rec.recorder=self.proc();rec.recording_state='recording';rec.recording_requested=True
        rec.recorder._camera_last_progress=time.monotonic();rec.recorder._camera_growth_at=time.monotonic()
        self.assertFalse(rec.encoder_status()['stalled'])
        rec.recorder._camera_growth_at-=40
        self.assertTrue(rec.encoder_status()['stalled'])

    def test_unavailable_audio_starts_video_only(self):
        rec.recorder=None
        with patch.object(rec,'audio_available',return_value=False),patch.object(rec,'_start_recording',return_value=(True,'ok')) as start,patch.object(rec,'persist_session'):
            rec.recording_audio=False
            rec.start_recording()
            start.assert_called_once_with('manual',False)
        self.assertTrue(rec.recording_requested)
        self.assertIn('純影像',rec.recording_warning)

    def test_failed_audio_open_retries_without_audio(self):
        def attempt(trigger,audio):
            rec.recorder=SimpleNamespace(poll=lambda:8)
            rec.recording_audio=audio
            if audio:raise RuntimeError('RTSP input unavailable')
            return True,'ok'
        with patch.object(rec,'audio_available',return_value=True),patch.object(rec,'_start_recording',side_effect=attempt) as start,patch.object(rec,'persist_session'):
            rec.start_recording();self.assertEqual(start.call_count,2)
        self.assertFalse(rec.recording_audio)

    def test_reconcile_preserves_originals_and_reports_damaged_segments(self):
        root=Path(rec.VIDEO_DIR);(root/'sessions').mkdir()
        sid='012345abcdef';name=f'recording-20261030-010000-{sid}.mp4'
        (root/name).write_bytes(b'original')
        path=root/'sessions'/f'{sid}.json'
        path.write_text(json.dumps({'files':[name],'state':'recording'}))
        reconcile_sessions(root,lambda *_a,**_k:{'health':'damaged'})
        result=json.loads(path.read_text())
        self.assertEqual(result['state'],'interrupted');self.assertEqual(result['file_states'][name],'damaged')
        self.assertEqual((root/name).read_bytes(),b'original')

    def test_reconcile_discovers_the_segment_created_just_before_process_death(self):
        root=Path(rec.VIDEO_DIR);(root/'sessions').mkdir();sid='012345abcdef'
        names=[f'recording-20261030-{h}0000-{sid}.mp4' for h in ('01','02')]
        for name in names:(root/name).write_bytes(b'preserved')
        path=root/'sessions'/f'{sid}.json';path.write_text(json.dumps({'files':[names[0]]}))
        reconcile_sessions(root,lambda *_a,**_k:{'health':'ok'})
        result=json.loads(path.read_text());self.assertEqual(result['files'],names)
        self.assertEqual(result['file_states'][names[1]],'complete')

    def test_transient_probe_failure_is_unknown_and_can_be_rechecked(self):
        root=Path(rec.VIDEO_DIR);(root/'sessions').mkdir();sid='012345abcdef'
        name=f'recording-20261030-010000-{sid}.mp4';(root/name).write_bytes(b'original')
        path=root/'sessions'/f'{sid}.json';path.write_text(json.dumps({'files':[name]}))
        reconcile_sessions(root,lambda *_a,**_k:{'health':'unknown'})
        self.assertEqual(json.loads(path.read_text())['file_states'][name],'unknown')
        reconcile_sessions(root,lambda *_a,**_k:{'health':'ok'})
        self.assertEqual(json.loads(path.read_text())['file_states'][name],'complete')

    def test_removing_a_closed_segment_does_not_make_growing_encoder_stall(self):
        root=Path(rec.VIDEO_DIR);sid='012345abcdef';rec.active_session_id=sid
        old=root/f'recording-20261030-010000-{sid}.mp4';active=root/f'recording-20261030-020000-{sid}.mp4'
        old.write_bytes(b'a'*100);active.write_bytes(b'b'*10)
        proc=self.proc();proc._camera_last_progress=time.monotonic();rec.recorder=proc
        rec.recording_requested=True;rec.recording_state='recording';rec.recording_trigger='manual'
        with patch.object(rec,'persist_session'),patch.object(rec,'start_recording') as start:
            rec.check_encoder();old.unlink();active.write_bytes(b'b'*20)
            proc._camera_growth_at=time.monotonic()-40
            rec.check_encoder();start.assert_not_called()
        self.assertFalse(rec.encoder_status()['stalled'])


class EventReliabilityTests(unittest.TestCase):
    setUp=events.EventTests.setUp
    wait=events.EventTests.wait

    def test_closed_segment_is_playable_before_whole_session_ends(self):
        sid='012345abcdef';names=[f'recording-20261030-{h}0000-{sid}.mp4' for h in ('01','02')]
        for name in names:(self.videos/name).write_bytes(b'fixture')
        (self.videos/'sessions'/f'{sid}.json').write_text(json.dumps({'files':names,'event_ids':[1]}))
        result=self.timeline.selected([1])[0]['recordings']
        self.assertEqual([v['complete'] for v in result],[True,False])

    def test_bad_old_job_does_not_poison_new_export(self):
        bad=self.timeline.exports/('f'*32+'.job.json');bad.write_text('{broken')
        self.wait(self.timeline.export([1],format='json')['job'])
        self.assertFalse(bad.exists())
        self.assertTrue(bad.with_suffix('.invalid').exists())

    def test_null_and_array_job_records_cannot_prevent_startup(self):
        for value,token in ((None,'a'*32),([],'b'*32)):
            (self.timeline.exports/(token+'.job.json')).write_text(json.dumps(value))
        restored=Timeline(self.data,self.videos,self.state)
        self.assertEqual(len(list(restored.exports.glob('*.job.invalid'))),2)

    def test_links_schema_keeps_legacy_four_value_inserts_compatible(self):
        with closing(self.timeline.prefs()) as db,db:
            self.assertEqual(len(db.execute('PRAGMA table_info(links)').fetchall()),4)
            db.execute('INSERT INTO links VALUES(?,?,?,?)',(1,'legacy.mp4','old',1))

    def test_expiry_is_cleaned_without_creating_a_new_export(self):
        job=self.timeline.export([1],format='json')['job'];target=self.wait(job)
        self.timeline.cleanup_exports(now=time.time()+90000)
        self.assertFalse(target.exists())

    def test_ai_unknown_is_separate_from_completed_negative_annotation(self):
        with closing(sqlite3.connect(self.data/'events.sqlite3')) as db,db:
            db.execute('ALTER TABLE events ADD COLUMN metadata_json TEXT')
            db.execute('UPDATE events SET metadata_json=? WHERE id=2',(json.dumps({'ai_ready':True}),))
            db.execute('UPDATE events SET metadata_json=? WHERE id=4',(json.dumps({'ai_ready':False}),))
        self.assertEqual(self.timeline.list('type=motion')['total'],1)
        self.assertEqual(self.timeline.selected([4])[0]['ai_status'],'unknown')
        with closing(sqlite3.connect(self.data/'events.sqlite3')) as db,db:
            db.execute('UPDATE events SET metadata_json=? WHERE id=4',(json.dumps({'ai_ready':None,'ai_status':'pending'}),))
        self.assertEqual(self.timeline.selected([4])[0]['ai_status'],'unknown')


class RetentionReliabilityTests(unittest.TestCase):
    setUp=maintenance.MaintenanceTests.setUp
    tearDown=maintenance.MaintenanceTests.tearDown

    def test_expiry_uses_recoverable_trash_and_preserves_event_rows(self):
        with patch.object(m,'backup'),patch.object(m,'http_json',return_value={'active_files':[]}):
            result=m.cleanup(apply=True)
        self.assertEqual(result['moved'],2)
        self.assertFalse(self.video.exists())
        with closing(sqlite3.connect(self.data/'events.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT snapshot_name FROM events WHERE id=1').fetchone()[0],'')
        self.assertTrue(list((self.recordings/'.trash').glob('*/manifest.json')))

    def test_expiry_skips_current_recording_and_favorite_snapshot(self):
        m.STATE.mkdir()
        with closing(sqlite3.connect(m.STATE/'event-preferences.sqlite3')) as db,db:
            db.execute('CREATE TABLE favorites(event_id INTEGER,favorite INTEGER)')
            db.execute('INSERT INTO favorites VALUES(1,1)')
        with patch.object(m,'backup'),patch.object(m,'http_json',return_value={'active_files':[self.video.name]}):
            result=m.cleanup(apply=True)
        self.assertEqual(result['moved'],0)
        self.assertTrue(self.video.exists());self.assertTrue((self.data/'snapshots/old.jpg').exists())

    def test_a_favorite_added_during_backup_is_rechecked_before_removal(self):
        def backup():
            m.STATE.mkdir()
            with closing(sqlite3.connect(m.STATE/'event-preferences.sqlite3')) as db,db:
                db.execute('CREATE TABLE favorites(event_id INTEGER,favorite INTEGER)')
                db.execute('INSERT INTO favorites VALUES(1,1)')
        with patch.object(m,'backup',side_effect=backup),patch.object(m,'http_json',return_value={'active_files':[]}):
            m.cleanup(apply=True)
        self.assertTrue((self.data/'snapshots/old.jpg').exists())


class StorageAdmissionTests(unittest.TestCase):
    def test_parallel_backup_and_export_reservations_cannot_overcommit(self):
        with tempfile.TemporaryDirectory() as directory,patch('camera_storage.shutil.disk_usage',return_value=SimpleNamespace(free=100)):
            root=Path(directory)
            with reserve_space(root,root,60,reserve=10):
                with self.assertRaises(StoragePressure):
                    with reserve_space(root,root,40,reserve=10):pass
            with reserve_space(root,root,40,reserve=10):pass

    def test_ingress_verifies_response_bytes_not_just_http_status(self):
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock()
        response.status=206;response.read.return_value=b'wrong'
        with patch('camera_diagnostics.urllib.request.urlopen',return_value=response):
            self.assertFalse(ingress_check()['healthy'])
            response.read.return_value=PROBE[1024:2048]
            result=ingress_check();self.assertTrue(result['healthy']);self.assertFalse(result['remote_network_tested'])

    def test_actual_probe_handler_supports_the_ingress_range_check(self):
        import threading
        from http.server import ThreadingHTTPServer
        from backup_api import Handler
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'transport-probe.bin').write_bytes(PROBE)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            server.jobs=SimpleNamespace(store=SimpleNamespace(state=root))
            threading.Thread(target=server.serve_forever,daemon=True).start()
            try:self.assertTrue(ingress_check(f'http://127.0.0.1:{server.server_port}/probe')['healthy'])
            finally:server.shutdown();server.server_close()

    def test_annotation_queue_is_bounded_and_does_not_wait_for_slow_ai(self):
        import threading
        import numpy as np
        from camera_annotations import AnnotationWorker
        started=threading.Event();release=threading.Event();saved=[]
        def detect(_):started.set();release.wait(5);return False,None,0,True
        worker=AnnotationWorker(detect,lambda *result:saved.append(result))
        try:
            frame=np.zeros((8,8,3),dtype=np.uint8)
            self.assertTrue(worker.submit(1,frame));self.assertTrue(started.wait(1))
            self.assertTrue(worker.submit(2,frame));self.assertTrue(worker.submit(3,frame))
            self.assertFalse(worker.submit(4,frame));self.assertEqual(worker.queue.qsize(),2)
        finally:release.set();worker.close()
        self.assertEqual(len(saved),3)

    def test_annotation_save_retries_a_transient_sqlite_failure(self):
        import numpy as np
        from camera_annotations import AnnotationWorker
        saved=[]
        def save(*result):
            if not saved:saved.append('failure');raise sqlite3.OperationalError('injected busy database')
            saved.append(result)
        worker=AnnotationWorker(lambda _:(True,.9,1,True),save)
        worker.submit(7,np.zeros((8,8,3),dtype=np.uint8));worker.close()
        self.assertEqual(saved[-1],(7,True,.9,1,True));self.assertEqual(worker.failures,0)

    def test_static_only_deploy_does_not_restart_camera_services(self):
        from deploy_release import affected_services
        self.assertEqual(affected_services([{'path':'/var/www/camera-monitor/camera-ui.css'}]),[])
        affected=affected_services([{'path':'/opt/camera-common/camera_storage.py'}])
        self.assertNotIn('camera-timestamp',affected);self.assertIn('camera-recorder-api',affected)
        self.assertEqual(affected_services([{'path':'/opt/camera-common/camera_annotations.py'}]),['camera-ai'])
        self.assertEqual(affected_services([{'path':'/opt/camera-common/camera_deployment.py'}]),['camera-recorder-api','camera-backup-api'])

    def test_deployment_gate_is_released_after_failure(self):
        from camera_deployment import deployment_guard,deployment_active
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);marker=root/'run'/'deployment.json'
            with patch.dict(os.environ,{'CAMERA_MEDIA_LOCK':str(root/'media.lock')}):
                with self.assertRaises(RuntimeError):
                    with deployment_guard(root,marker):
                        self.assertTrue(deployment_active(marker));raise RuntimeError('injected deployment failure')
                self.assertFalse(marker.exists())

    def test_rollback_rejects_an_active_export_before_stopping_any_service(self):
        import deploy_release as release
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/('a'*32+'.job.json')).write_text(json.dumps({'state':'running'}))
            check=release.ensure_no_exports
            with patch.object(release,'deployment_guard',return_value=nullcontext()),patch.object(release,'ensure_no_exports',side_effect=lambda:check(root)),patch.object(release,'run') as run:
                with self.assertRaisesRegex(RuntimeError,'export'):release.rollback(root)
                run.assert_not_called()
