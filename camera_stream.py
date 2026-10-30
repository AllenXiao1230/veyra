#!/usr/bin/python3
"""One hardware video encoder, Opus/AAC audio and bounded live-publish recovery."""
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import signal
import subprocess
import threading
import time
import urllib.request

VIDEO='http://127.0.0.1:8093/stream'
MIC='rtsp://127.0.0.1:8554/mic'


def command(audio=True,video=VIDEO,mic=MIC,output='rtsp://127.0.0.1:8554'):
    args=['ffmpeg','-hide_banner','-loglevel','warning','-nostdin','-progress','pipe:1',
          '-vaapi_device','/dev/dri/renderD128','-thread_queue_size','32','-use_wallclock_as_timestamps','1',
          '-rw_timeout','8000000','-f','mpjpeg','-i',video]
    if audio:
        args += ['-thread_queue_size','128','-use_wallclock_as_timestamps','1','-rtsp_transport','tcp',
                 '-timeout','8000000','-i',mic]
    args += ['-map','0:v:0']
    if audio:args += ['-map','1:a:0','-map','1:a:0']
    args += ['-vf','fps=15,scale=960:540,format=nv12,hwupload','-c:v','h264_vaapi',
             '-profile:v','constrained_baseline','-level:v','3.1','-bf','0','-g','30',
             '-rc_mode','CQP','-qp','29','-fps_mode','cfr','-flags','+global_header']
    if audio:
        args += ['-filter:a','aresample=async=1000:first_pts=0','-ac','1','-ar','48000',
                 '-c:a:0','libopus','-b:a:0','32k','-c:a:1','aac','-b:a:1','48k']
    else:args += ['-an']
    selectors=("v:0,a:0","v:0,a:1") if audio else ('v:0','v:0')
    targets=[f"[select=\\'{selection}\\':f=rtsp:rtsp_transport=tcp]{output}/{name}"
             for selection,name in zip(selectors,('live','live-hls'))]
    return args+['-f','tee','|'.join(targets)]


def audio_ready():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8094/health',timeout=3) as response:
            return bool(json.load(response).get('healthy'))
    except (OSError,ValueError):return False


class Publisher:
    def __init__(self):
        self.lock=threading.RLock();self.stop=threading.Event();self.process=None
        self.progress=0;self.audio=False;self.started=0;self.recoveries=0;self.error=None
        self.lines=deque(maxlen=6)
        self.worker=threading.Thread(target=self.loop,name='live-publisher',daemon=True)

    def status(self):
        with self.lock:
            age=time.monotonic()-self.progress if self.progress else None
            healthy=bool(self.process and self.process.poll() is None and age is not None and age<20)
            return {'healthy':healthy,'state':'running' if healthy else 'recovering',
                    'audio_included':self.audio if healthy else None,'progress_age_seconds':round(age,2) if age is not None else None,
                    'recoveries':self.recoveries,'last_error':self.error,
                    'warning':None if self.audio else '音訊未就緒，音畫串流暫時只有影像',
                    'width':960,'height':540,'fps':15,'rate_control':'CQP','qp':29}

    def read_progress(self,process):
        for line in process.stdout:
            if line.startswith('out_time_us='):
                try:valid=int(line.split('=',1)[1])>0
                except ValueError:valid=False
                if valid:
                    with self.lock:
                        if self.process is process:self.progress=time.monotonic();self.error=None

    def read_errors(self,process):
        for line in process.stderr:
            with self.lock:self.lines.append(line.strip()[-300:])

    @staticmethod
    def finish(process):
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)

    def loop(self):
        attempts=0;audio_after=0
        while not self.stop.is_set():
            use_audio=time.monotonic()>=audio_after and audio_ready()
            process=None
            try:
                with self.lock:
                    self.progress=0;self.audio=use_audio;self.started=time.monotonic()
                    self.process=process=subprocess.Popen(command(use_audio),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,text=True,bufsize=1)
                threading.Thread(target=self.read_progress,args=(process,),daemon=True).start()
                threading.Thread(target=self.read_errors,args=(process,),daemon=True).start()
                check_audio=time.monotonic()+30
                while not self.stop.wait(1):
                    if process.poll() is not None:raise RuntimeError('串流編碼器已停止')
                    with self.lock:stalled=time.monotonic()-(self.progress or self.started)>20
                    if stalled:raise RuntimeError('串流輸出暫停更新')
                    if time.monotonic()-self.started>30:attempts=0
                    if not use_audio and time.monotonic()>=check_audio:
                        check_audio=time.monotonic()+30
                        if time.monotonic()>=audio_after and audio_ready():break
                if self.stop.is_set():break
            except Exception as error:
                with self.lock:self.error=str(error)[:200];self.recoveries+=1
                if use_audio:audio_after=time.monotonic()+60
                attempts+=1
            finally:
                if process:self.finish(process)
                with self.lock:self.process=None
            self.stop.wait(min(30,2**min(attempts,5)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split('?',1)[0] not in ('/health','/status'):
            self.send_error(404);return
        data=json.dumps(self.server.publisher.status()).encode()
        self.send_response(200);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers()
        try:self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass

    def log_message(self,*args):pass


def main():
    publisher=Publisher();server=ThreadingHTTPServer(('127.0.0.1',8096),Handler)
    server.publisher=publisher;server.daemon_threads=True
    def stop(*args):
        publisher.stop.set();threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    publisher.worker.start()
    try:server.serve_forever(poll_interval=.5)
    finally:
        publisher.stop.set();server.server_close();publisher.worker.join(timeout=12)


if __name__=='__main__':main()
