import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
import threading
from types import SimpleNamespace
from unittest.mock import patch
from camera_timeline import Timeline
from camera_media import MediaLibrary
from camera_state import atomic_json


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data, self.videos, self.state = [self.root / name for name in ('data','videos','state')]
        (self.data / 'snapshots').mkdir(parents=True)
        (self.videos / 'sessions').mkdir(parents=True)
        with sqlite3.connect(self.data / 'events.sqlite3') as db:
            db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,created_at TEXT,event_type TEXT,person_detected INTEGER,confidence REAL,snapshot_name TEXT)')
            for i in range(1,31):
                db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',(i,'2026-10-29T17:00:00+00:00','motion',i%2,None,f'event-{i}.jpg'))
                (self.data / 'snapshots' / f'event-{i}.jpg').write_bytes(b'private fixture ' + str(i).encode())
        self.timeline = Timeline(self.data,self.videos,self.state)

    def wait(self, job):
        self.assertTrue(self.timeline.export_lock.acquire(timeout=10))
        self.timeline.export_lock.release()
        result = self.timeline.job(job['id'])
        self.assertEqual(result['state'],'complete',result)
        return self.timeline.exports / (job['id'] + '.data.' + job['format'])

    def test_pagination_date_filter_and_durable_favorites(self):
        page=self.timeline.list('limit=12&date=2026-10-30')
        self.assertEqual((len(page['items']),page['total']),(12,30))
        next_page=self.timeline.list('limit=12&before='+page['next_before'])
        self.assertFalse(set(r['id'] for r in page['items']) & set(r['id'] for r in next_page['items']))
        self.timeline.favorite([1,30],True)
        another=Timeline(self.data,self.videos,self.state)
        self.assertEqual(another.list('favorite=1')['total'],2)
        self.assertEqual(another.list('favorite=1&type=person')['total'],1)
        self.assertEqual(another.list('date=2026-10-29')['total'],0)
        another.favorite([30],False)
        self.assertEqual(another.list('favorite=1')['total'],1)
        for payload in ([],[True],[999],list(range(1,102))):
            with self.assertRaises(ValueError): another.favorite(payload,True)

    def test_export_zip_links_videos_only_when_selected_and_preserves_sources(self):
        name='recording-20261030-010000-aaaaaaaaaaaa.mp4'
        video=self.videos/name; video.write_bytes(b'private movie fixture')
        atomic_json(self.videos/'sessions/aaaaaaaaaaaa.json',{'event_ids':[30],'files':[name],'ended_at':1})
        self.assertEqual(len(self.timeline.selected([30])[0]['recordings']),1)
        job=self.timeline.export([30])['job']; target=self.wait(job)
        with zipfile.ZipFile(target) as archive:
            self.assertIn('snapshots/event-30.jpg',archive.namelist())
            self.assertFalse(any(n.startswith('recordings/') for n in archive.namelist()))
            self.assertEqual(json.loads(archive.read('events.json'))['events'][0]['created_at_local'],'2026-10-30T01:00:00+08:00')
        job=self.timeline.export([30],include_videos=True)['job']; target=self.wait(job)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read('recordings/'+name),video.read_bytes())
            manifest=json.loads(archive.read('manifest.json'))['files']
            row=next(r for r in manifest if r['path'].startswith('recordings/'))
            self.assertEqual(row['sha256'],hashlib.sha256(video.read_bytes()).hexdigest())
        self.assertTrue(video.exists())

    def test_zip_uses_the_shared_production_lock_once(self):
        # Isolated subprocess: a second flock on the same file description
        # path must fail this bounded test, rather than hang the test runner.
        script = '''import sys
from camera_timeline import Timeline
from pathlib import Path
root=Path(sys.argv[1]); timeline=Timeline(root/'data',root/'videos',root/'state')
job=timeline.export([1])['job']
assert timeline.export_lock.acquire(timeout=3), 'export deadlocked'
timeline.export_lock.release()
assert timeline.job(job['id'])['state']=='complete'
'''
        environment=dict(os.environ,CAMERA_MEDIA_LOCK=str(self.root/'media.lock'))
        subprocess.run([sys.executable,'-c',script,str(self.root)],env=environment,check=True,timeout=8)

    def test_csv_and_json_outputs_do_not_collide_with_job_metadata(self):
        for format in ('csv','json'):
            job=self.timeline.export([1],format)['job']; target=self.wait(job)
            self.assertNotEqual(target,self.timeline.exports/(job['id']+'.job.json'))
            if format=='json': self.assertEqual(json.loads(target.read_text())['events'][0]['id'],1)
            else: self.assertIn('2026-10-30T01:00:00+08:00',target.read_text(encoding='utf-8-sig'))
            self.assertEqual(self.timeline.job(job['id'])['state'],'complete')

    def test_missing_and_symlink_media_are_never_exported(self):
        source=self.data/'snapshots/event-1.jpg';source.unlink()
        secret=self.root/'secret';secret.write_bytes(b'outside fixture')
        source.symlink_to(secret)
        self.assertIsNone(self.timeline.selected([1])[0]['snapshot_url'])
        job=self.timeline.export([1])['job']; target=self.wait(job)
        with zipfile.ZipFile(target) as archive:
            self.assertFalse(any(n.startswith('snapshots/') for n in archive.namelist()))

    def test_export_fails_cleanly_without_disk_space(self):
        disk=type('Disk',(),{'free':0})()
        with patch('camera_timeline.shutil.disk_usage',return_value=disk):
            job=self.timeline.export([1])['job']
            self.assertTrue(self.timeline.export_lock.acquire(timeout=10));self.timeline.export_lock.release()
        result=self.timeline.job(job['id'])
        self.assertEqual(result['state'],'failed')
        self.assertFalse(list(self.timeline.exports.glob('*.tmp')))
        self.assertTrue((self.data/'snapshots/event-1.jpg').exists())

    def test_http_favorites_export_and_cross_origin_rejection(self):
        from backup_api import Handler, ThreadingHTTPServer
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.timeline=self.timeline;server.jobs=SimpleNamespace(store=None)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def call(method,path,body=None,origin=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            headers={'Content-Type':'application/json'}
            if origin:headers['Origin']=origin
            conn.request(method,path,json.dumps(body) if body is not None else None,headers)
            response=conn.getresponse();result=(response.status,dict(response.getheaders()),response.read());conn.close();return result
        self.assertEqual(call('POST','/events/favorite',{'ids':[1],'favorite':True},'https://invalid.test')[0],403)
        self.assertEqual(call('POST','/events/favorite',{'ids':[1],'favorite':True})[0],200)
        self.assertEqual(json.loads(call('GET','/events/list?favorite=1')[2])['total'],1)
        code,_,body=call('POST','/events/export',{'ids':[1],'format':'json'})
        self.assertEqual(code,202);job=json.loads(body)['job'];self.wait(job)
        code,headers,body=call('GET','/events/exports/'+job['id']+'/download')
        self.assertEqual(code,200);self.assertIn('attachment',headers['Content-Disposition'])
        self.assertEqual(json.loads(body)['events'][0]['id'],1)


class PurgeTests(unittest.TestCase):
    def test_still_snapshot_thumbnail_does_not_seek_past_the_only_frame(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            cv2.imwrite(str(root/'fixture.jpg'),np.zeros((180,320,3),np.uint8))
            library=MediaLibrary(root,'snapshot')
            thumb=library.thumbnail('fixture.jpg')
            self.assertTrue(thumb.read_bytes().startswith(b'\xff\xd8'))
            self.assertEqual(cv2.imread(str(thumb)).shape[1],480)

    def test_partial_batch_and_typed_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); library=MediaLibrary(root,'snapshot')
            (root/'one.jpg').write_bytes(b'one');(root/'two.jpg').write_bytes(b'two')
            result=library.batch({'action':'trash','items':['one.jpg','missing.jpg','two.jpg']})
            self.assertEqual((result['succeeded'],result['failed']),(2,1))
            token=result['results'][0]['result']['token']
            with self.assertRaises(ValueError):library.purge(token,'yes')
            self.assertEqual(library.list_trash()[0]['size'],3)
            library.purge(token,'永久刪除')
            self.assertEqual(len(library.list_trash()),1)
            remaining=library.list_trash()[0]['token']; library.restore(remaining)
            self.assertEqual((root/'two.jpg').read_bytes(),b'two')

    def test_interrupted_confirmed_purge_finishes_without_touching_new_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);library=MediaLibrary(root,'snapshot')
            (root/'one.jpg').write_bytes(b'old');token=library.move_to_trash('one.jpg')['token']
            (root/'one.jpg').write_bytes(b'new')
            with patch.object(library,'_finish_receipt',side_effect=RuntimeError('injected interruption')):
                with self.assertRaises(RuntimeError):library.purge(token,'永久刪除')
            recovered=MediaLibrary(root,'snapshot')
            self.assertEqual(recovered.list_trash(),[])
            self.assertEqual((root/'one.jpg').read_bytes(),b'new')
