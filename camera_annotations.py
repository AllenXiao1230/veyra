"""Bounded optional AI work; motion capture never waits for model inference."""
import logging
import queue
import threading


class AnnotationWorker:
    def __init__(self, detect, save):
        self.detect,self.save=detect,save
        self.queue=queue.Queue(maxsize=2)
        self.stop=threading.Event()
        self.thread=None
        self.failures=0

    @property
    def accepting(self):return not self.stop.is_set() and not self.queue.full()

    def submit(self, identifier, frame):
        if not self.accepting:return False
        if self.thread is None:
            self.thread=threading.Thread(target=self.run,name='person-annotation',daemon=True)
            self.thread.start()
        try:self.queue.put_nowait((identifier,frame.copy()));return True
        except queue.Full:return False

    def run(self):
        while not self.stop.is_set() or not self.queue.empty():
            try:identifier,frame=self.queue.get(timeout=.2)
            except queue.Empty:continue
            try:
                try:result=self.detect(frame)
                except Exception:result=(False,None,0,False)
                saved=False
                for attempt in range(3):
                    try:self.save(identifier,*result);saved=True;break
                    except Exception:
                        self.stop.wait(.25*2**attempt)
                if not saved:
                    self.failures+=1
                    self.save(identifier,False,None,0,False)
            except Exception:
                self.failures+=1
                logging.exception('Optional event annotation failed')
            finally:self.queue.task_done()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=3)
