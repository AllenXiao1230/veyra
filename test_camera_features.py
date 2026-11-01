from contextlib import contextmanager, closing
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from camera_notifications import Notifications
from camera_timeline import Timeline
from camera_work import WorkCenter
from camera_stream import command
from camera_stream_config import render
from camera_state import atomic_json


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.data,self.videos,self.state=[self.root/name for name in ('data','videos','state')]
        (self.data/'snapshots').mkdir(parents=True);(self.videos/'sessions').mkdir(parents=True)
        with closing(sqlite3.connect(self.data/'events.sqlite3')) as db,db:
            db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,created_at TEXT,event_type TEXT,person_detected INTEGER,confidence REAL,snapshot_name TEXT)')
            for i in range(1,13):
                db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',(i,f'2026-10-30T00:{i:02d}:00+00:00','motion',i%2,.9 if i%2 else .4,f'event-{i}.jpg'))
                if i<=8:(self.data/'snapshots'/f'event-{i}.jpg').write_bytes(b'fixture')
        self.timeline=Timeline(self.data,self.videos,self.state)

    def finished(self,job,state='complete'):
        self.assertTrue(self.timeline.export_lock.acquire(timeout=8));self.timeline.export_lock.release()
        result=self.timeline.job(job['id']);self.assertEqual(result['state'],state,result);return result

    def test_incidents_confirm_deduplicate_recover_and_ack_with_a_race(self):
        notices=Notifications(self.state);issue=[{'id':'camera-audio','message':'音訊中斷'}]
        notices.observe(issue,1000);self.assertEqual(notices.list()['items'],[])
        notices.observe(issue,1060);notices.observe(issue,1120)
        self.assertEqual(notices.list()['unread'],1)
        notices=Notifications(self.state);notices.observe([],1180)
        page=notices.list();self.assertEqual([r['kind'] for r in page['items']],['recovery','fault'])
        through=page['latest']
        notices.observe([{'id':'storage','message':'空間不足'}],1300);notices.observe([{'id':'storage','message':'空間不足'}],1360)
        notices.acknowledge(through);self.assertEqual(notices.list()['unread'],1)

    def test_cooldown_does_not_emit_unpaired_recoveries(self):
        notices=Notifications(self.state);issue=[{'id':'ingress','message':'入口異常'}]
        for value in (1000,1060):notices.observe(issue,value)
        notices.observe([],1120)
        for value in (1180,1240):notices.observe(issue,value)
        notices.observe([],1250)
        self.assertEqual(len(notices.list()['items']),2)

    def test_disabled_notices_keep_incident_state_but_do_not_send(self):
        notices=Notifications(self.state);notices.policy({'enabled':False,'recoveries':True,'cooldown':60})
        notices.observe([{'id':'storage','message':'不足'}],1);notices.observe([{'id':'storage','message':'不足'}],2)
        self.assertEqual(len(notices.list()['active']),1);self.assertEqual(notices.list()['unread'],0)
        with self.assertRaises(ValueError):notices.acknowledge(True)
        with self.assertRaises(ValueError):notices.policy({'enabled':True,'recoveries':True,'cooldown':0})

    def test_precise_taipei_range_includes_end_minute_and_oldest_pagination(self):
        result=self.timeline.list('from=2026-10-30T08:02&until=2026-10-30T08:03&sort=oldest&limit=1')
        self.assertEqual(result['total'],2);self.assertEqual(result['items'][0]['id'],2)
        result=self.timeline.list('from=2026-10-30T08:02&until=2026-10-30T08:03&sort=oldest&limit=1&before='+result['next_before'])
        self.assertEqual(result['items'][0]['id'],3)
        with self.assertRaises(ValueError):self.timeline.list('from=2026-11-01T00:00&until=2026-10-30T00:00')

    def test_notes_tags_persist_and_search_treats_sql_wildcards_as_text(self):
        self.timeline.annotate(1,'完成 100% 檢查',['門口','Review'])
        other=Timeline(self.data,self.videos,self.state)
        self.assertEqual(other.list('tag=review')['total'],1)
        self.assertEqual(other.list('q=%25')['total'],1)
        self.assertEqual(other.list("q='OR1=1")['total'],0)
        self.assertEqual(other.selected([1])[0]['note'],'完成 100% 檢查')
        with self.assertRaises(ValueError):other.annotate(1,'x',['a']*11)

    def test_media_filters_use_current_files_not_only_old_database_names(self):
        self.assertEqual(self.timeline.list('media=missing')['total'],4)
        (self.data/'snapshots/event-1.jpg').rename(self.data/'removed.jpg')
        self.assertEqual(self.timeline.list('media=missing')['total'],5)
        name='recording-20261030-080000-aaaaaaaaaaaa.mp4';(self.videos/name).write_bytes(b'movie')
        atomic_json(self.videos/'sessions/aaaaaaaaaaaa.json',{'event_ids':[1],'files':[name],'ended_at':1})
        self.assertEqual(self.timeline.list('media=video')['total'],1)
        (self.videos/name).unlink();self.assertEqual(self.timeline.list('media=video')['total'],0)

    def test_export_retains_notes_and_prevents_csv_formula_execution(self):
        self.timeline.annotate(1,'=SUM(1,2)',['門口'])
        job=self.finished(self.timeline.export([1],format='csv')['job'])
        body=(self.timeline.exports/(job['id']+'.data.csv')).read_text(encoding='utf-8-sig')
        row=next(csv.DictReader(io.StringIO(body)));self.assertEqual(row['note'],"'=SUM(1,2)")
        self.assertEqual(row['tags'],'門口')

    def test_cancel_export_releases_reservation_and_keeps_source(self):
        entered=threading.Event();release=threading.Event()
        @contextmanager
        def reserve(*args,**kwargs):
            entered.set();release.wait(5);yield
        with patch('camera_timeline.reserve_space',reserve):
            job=self.timeline.export([1])['job'];self.assertTrue(entered.wait(5))
            self.timeline.cancel(job['id']);release.set();self.finished(job,'cancelled')
        self.assertTrue((self.data/'snapshots/event-1.jpg').is_file())
        self.assertFalse(list(self.timeline.exports.glob('*.data.*')))

    def test_expired_export_metadata_can_be_retried_from_work_center(self):
        job=self.finished(self.timeline.export([1],format='json')['job'])
        self.timeline.cleanup_exports(now=job['expires_at']+1)
        self.assertEqual(self.timeline.job(job['id'])['state'],'expired')
        class Jobs:
            directory=self.state/'jobs'
        Jobs.directory.mkdir()
        center=WorkCenter(self.timeline,Jobs(),lambda:{'active_files':[]})
        result=center.list('kind=export&state=expired')
        self.assertEqual(result['total'],1);self.assertTrue(result['items'][0]['retryable'])
        self.finished(center.retry('export',job['id'])['job'])

    def test_download_catalog_rejects_traversal_active_files_and_preserves_sources(self):
        class Jobs:
            directory=self.state/'jobs'
        Jobs.directory.mkdir()
        name='recording-20261030-080000-aaaaaaaaaaaa.mp4';(self.videos/name).write_bytes(b'fixture')
        center=WorkCenter(self.timeline,Jobs(),lambda:{'active_files':[name]})
        with self.assertRaises(ValueError):center.register('snapshot','../removed.jpg')
        with self.assertRaises(RuntimeError):center.register('recording',name)
        center.register('snapshot','event-1.jpg');self.assertEqual(center.list('kind=download')['total'],1)
        self.assertTrue((self.data/'snapshots/event-1.jpg').is_file())

    def test_stream_uses_one_video_encode_and_omits_failed_audio_input_completely(self):
        video_only=command(False)
        self.assertNotIn('rtsp://127.0.0.1:8554/mic',video_only)
        with_audio=command(True)
        self.assertEqual(with_audio.count('h264_vaapi'),1)
        self.assertIn('libopus',with_audio);self.assertIn('aac',with_audio)
        self.assertIn('live-hls',with_audio[-1])

    def test_stream_configuration_merge_preserves_hosts_mic_and_rejects_conflicts(self):
        import yaml
        original={'paths':{'mic':{'maxReaders':7}},'webrtcAdditionalHosts':['private-host'],'webrtcLocalUDPAddress':'100.1.2.3:8189','rtspAddress':'127.0.0.1:8554'}
        merged=yaml.safe_load(render(yaml.safe_dump(original)))
        self.assertEqual(merged['paths']['mic'],{'maxReaders':7})
        self.assertEqual(merged['webrtcAdditionalHosts'],['private-host'])
        self.assertEqual(merged['hlsAddress'],'127.0.0.1:8888')
        self.assertEqual(render(yaml.safe_dump(merged)),render(yaml.safe_dump(merged)))
        with self.assertRaises(ValueError):render('paths:\n  live:\n    source: rtsp://other\n')

    @unittest.skipIf(os.name=='nt','Linux rsync backup')
    def test_backup_keeps_notification_policy_and_history_with_sqlite_snapshot(self):
        import camera_maintenance as maintenance
        from types import SimpleNamespace
        notices=Notifications(self.state);notices.observe([{'id':'storage','message':'不足'}],1000);notices.observe([{'id':'storage','message':'不足'}],1060)
        notices.policy({'enabled':False,'recoveries':True,'cooldown':900})
        with closing(sqlite3.connect(self.data/'events.sqlite3')) as db,db:db.execute("UPDATE events SET snapshot_name='' WHERE id>8")
        code=self.root/'camera_ai.py';code.write_text('# fixture')
        (self.data/'motion-settings.json').write_text(json.dumps({'area_percent':2.1,'difference':24,'confirm_frames':4,'light_compensation':True}))
        with patch.multiple(maintenance,DATA=self.data,RECORDINGS=self.videos,STATE=self.state,BACKUPS=self.root/'backups'), \
             patch.object(maintenance,'configuration_entries',return_value=[(code,'srv/camera-monitor/app/ai/camera_ai.py')]), \
             patch.object(maintenance.shutil,'disk_usage',return_value=SimpleNamespace(total=100*1024**3,used=10*1024**3,free=90*1024**3)), \
             patch.object(maintenance,'http_json',return_value={'recording':False,'active_files':[]}):
            backup=Path(maintenance.backup()['path'])
        with closing(sqlite3.connect(f'file:{backup/"maintenance-state/notifications.sqlite3"}?mode=ro',uri=True)) as db:
            self.assertEqual(db.execute('SELECT enabled,cooldown FROM settings').fetchone(),(0,900))
            self.assertEqual(db.execute('SELECT count(*) FROM notices').fetchone()[0],1)
        self.assertEqual(json.loads((backup/'detector-state/motion-settings.json').read_text())['area_percent'],2.1)


if __name__=='__main__':unittest.main()
