"""A bounded server-side catalog of export/backup jobs and download shortcuts."""
import base64
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid
from urllib.parse import quote
from camera_state import atomic_json


class WorkCenter:
    def __init__(self, timeline, jobs, recorder_status):
        self.timeline,self.jobs,self.recorder_status=timeline,jobs,recorder_status
        self.downloads=timeline.state/'downloads';self.downloads.mkdir(exist_ok=True)
        self.lock=threading.Lock()

    def register(self, kind, name):
        if kind not in ('snapshot','recording'):raise ValueError('下載種類不正確')
        if not isinstance(name,str) or not name or len(name)>240 or name!=Path(name).name or any(c in name for c in ('/',chr(92),'\0')) or any(ord(c)<32 for c in name):
            raise ValueError('檔案名稱不正確')
        if kind=='recording' and not re.fullmatch(r'recording-\d{8}-\d{6}(?:-[0-9a-f]{12})?\.(mp4|mkv)',name):raise ValueError('影片名稱不正確')
        if kind=='snapshot' and Path(name).suffix.lower() not in ('.jpg','.jpeg','.png'):raise ValueError('快照名稱不正確')
        path=(self.timeline.data/'snapshots' if kind=='snapshot' else self.timeline.videos)/name
        if path.is_symlink() or not path.is_file():raise FileNotFoundError('原檔已不在媒體庫')
        if kind=='recording' and name in self.recorder_status().get('active_files',[]):
            raise RuntimeError('影片仍在錄製，請完成後下載')
        job={'id':uuid.uuid4().hex,'kind':kind,'filename':name,'created_at':time.time(),
             'size':path.stat().st_size,'state':'available',
             'download_url':('/ai-api/snapshots/' if kind=='snapshot' else '/recordings-api/file/')+quote(name)}
        with self.lock:
            atomic_json(self.downloads/(job['id']+'.json'),job)
            for old in sorted(self.downloads.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[500:]:
                old.unlink(missing_ok=True)
        return {'job':job}

    def list(self, query):
        from urllib.parse import parse_qs
        params=parse_qs(query);get=lambda k,d='':params.get(k,[d])[0]
        limit=int(get('limit','20'));kind=get('kind','all');state=get('state','all')
        if not 1<=limit<=50 or kind not in ('all','export','backup','download') or state not in ('all','active','complete','failed','expired'):
            raise ValueError('工作篩選不正確')
        cursor=None
        if get('before'):
            try:
                cursor=json.loads(base64.urlsafe_b64decode(get('before')+'='*((-len(get('before')))%4)))
                if not isinstance(cursor,list) or len(cursor)!=2 or not isinstance(cursor[0],(int,float)) or not math.isfinite(cursor[0]) or not isinstance(cursor[1],str):raise ValueError()
                cursor=tuple(cursor)
            except Exception:raise ValueError('工作分頁不正確')
        rows=[]
        groups=(('export',self.timeline.exports,'*.job.json'),('backup',self.jobs.directory,'*.json'),('download',self.downloads,'*.json'))
        for category,directory,pattern in groups:
            if kind!='all' and category!=kind:continue
            for path in directory.glob(pattern):
                try:
                    if path.is_symlink():continue
                    job=json.loads(path.read_text())
                    if not isinstance(job,dict) or not re.fullmatch(r'[0-9a-f]{32}',str(job.get('id',''))):continue
                    created=job.get('created_at',0)
                    if type(created) not in (int,float) or not math.isfinite(created):continue
                    row={k:job[k] for k in ('id','state','created_at','expires_at','format','count','size','done_bytes','total_bytes','message','error','download_url','filename','action','name','cancel_requested') if k in job}
                    row.update(kind=category,key=category+':'+job['id'])
                    if row.get('error'):
                        detail=str(row['error'])
                        if 'Less than 20 GiB' in detail:row['error']='可用空間不足 20GiB，請管理儲存空間後重試。'
                        elif re.search(r'\[Errno|Traceback|/(srv|var|home|opt)/',detail) or not re.search('[\u3400-\u9fff]',detail):
                            row['error']='工作未完成，可按重試；若仍失敗，請查看備份頁的服務狀態。'
                    if category=='export':
                        if row.get('state')=='complete':row['download_url']='/events-api/exports/'+job['id']+'/download'
                        if job.get('state')=='complete' and job.get('expires_at',0)<time.time():row['state']='expired'
                        row['retryable']=bool(job.get('ids')) and row['state'] in ('failed','cancelled','interrupted','expired')
                        row['cancellable']=row['state'] in ('queued','running')
                        if row['state']!='complete':row.pop('download_url',None)
                    elif category=='backup':
                        row['retryable']=row['state'] in ('failed','interrupted')
                    else:
                        source=(self.timeline.data/'snapshots' if job.get('kind')=='snapshot' else self.timeline.videos)/str(job.get('filename',''))
                        source.resolve().relative_to((self.timeline.data/'snapshots' if job.get('kind')=='snapshot' else self.timeline.videos).resolve())
                        if not source.is_file() or source.is_symlink():row.update(state='missing');row.pop('download_url',None)
                        row['retryable']=False
                    matches=(state=='all' or state=='active' and row['state'] in ('queued','running') or state=='failed' and row['state'] in ('failed','cancelled','interrupted','missing') or state=='expired' and row['state']=='expired' or state=='complete' and row['state'] in ('complete','available'))
                    if matches:rows.append(row)
                except (OSError,ValueError,TypeError,KeyError):continue
        rows.sort(key=lambda row:(row.get('created_at',0),row['key']),reverse=True)
        total=len(rows)
        if cursor:rows=[r for r in rows if (r.get('created_at',0),r['key'])<cursor]
        page=rows[:limit]
        token=base64.urlsafe_b64encode(json.dumps([page[-1]['created_at'],page[-1]['key']]).encode()).decode().rstrip('=') if len(rows)>limit else None
        return {'items':page,'total':total,'next_before':token}

    def retry(self, kind, token):
        if kind=='export':
            job=self.timeline.job(token)
            if job.get('state') not in ('failed','cancelled','interrupted','expired','complete'):raise RuntimeError('工作仍在進行')
            return self.timeline.export(job.get('ids'),job.get('format','zip'),job.get('include_videos',False))
        if kind=='backup':
            job=self.jobs.get(token)
            if job.get('state') not in ('failed','interrupted'):raise RuntimeError('這個工作不需要重試')
            return self.jobs.submit(job['action'],job.get('name'))
        raise ValueError('不支援重試此工作')
