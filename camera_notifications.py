"""Durable, local-only incident notifications with confirmation and cooldown."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

LABELS = {'camera-ai':'動作偵測','camera-recorder-api':'錄影服務','camera-timestamp':'影像來源',
          'camera-audio':'麥克風','camera-stream':'音畫串流','camera-backup-api':'備份服務',
          'storage':'儲存空間','backup-stale':'備份新鮮度','ingress':'網站下載路徑'}


class Notifications:
    def __init__(self, state):
        self.path=Path(state)/'notifications.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.db()) as db,db:
            db.executescript('''CREATE TABLE IF NOT EXISTS notices(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, code TEXT NOT NULL,
                kind TEXT NOT NULL, message TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS incidents(code TEXT PRIMARY KEY, failures INTEGER NOT NULL,
                active INTEGER NOT NULL, last_fault REAL NOT NULL, message TEXT NOT NULL, announced INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL,
                recoveries INTEGER NOT NULL,cooldown INTEGER NOT NULL);
                INSERT OR IGNORE INTO settings VALUES(1,1,1,300);''')

    def db(self):
        db=sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row
        return db

    def policy(self, payload=None):
        with closing(self.db()) as db,db:
            if payload is not None:
                if not isinstance(payload,dict) or type(payload.get('enabled')) is not bool or type(payload.get('recoveries')) is not bool:
                    raise ValueError('通知設定不正確')
                cooldown=payload.get('cooldown',300)
                if type(cooldown) is not int or not 60<=cooldown<=3600:raise ValueError('通知冷卻須為 60–3600 秒')
                db.execute('UPDATE settings SET enabled=?,recoveries=?,cooldown=? WHERE id=1',
                           (int(payload['enabled']),int(payload['recoveries']),cooldown))
            row=dict(db.execute('SELECT enabled,recoveries,cooldown FROM settings WHERE id=1').fetchone())
        return {**row,'enabled':bool(row['enabled']),'recoveries':bool(row['recoveries']),'channel':'in_app'}

    def observe(self, issues, now=None):
        now=time.time() if now is None else now
        current={row['id']:str(row.get('message','服務異常'))[:300] for row in issues
                 if isinstance(row,dict) and isinstance(row.get('id'),str) and len(row['id'])<=100}
        current={code:message.replace(code,LABELS.get(code,code),1) if message.startswith(code) else message for code,message in current.items()}
        with closing(self.db()) as db,db:
            # All observers serialize the transition and insertion in one transaction.
            db.execute('BEGIN IMMEDIATE')
            policy=db.execute('SELECT * FROM settings WHERE id=1').fetchone()
            previous={row['code']:dict(row) for row in db.execute('SELECT * FROM incidents')}
            for code in set(previous)|set(current):
                old=previous.get(code,{'failures':0,'active':0,'last_fault':0,'message':'','announced':0})
                failures=old['failures']+1 if code in current else 0
                active=bool(old['active']); last=old['last_fault']; announced=bool(old['announced']); message=current.get(code,old['message'])
                if code in current and not active and failures>=2:
                    active=True
                    announced=False
                    if policy['enabled'] and (not last or now-last>=policy['cooldown']):
                        db.execute('INSERT INTO notices(at,code,kind,message) VALUES(?,?,?,?)',
                                   (now,code,'fault',message))
                        last=now
                        announced=True
                elif code not in current and active:
                    active=False
                    if policy['enabled'] and policy['recoveries'] and announced:
                        label=LABELS.get(code,LABELS.get(code.removesuffix('-warning'),'服務'))
                        db.execute('INSERT INTO notices(at,code,kind,message) VALUES(?,?,?,?)',
                                   (now,code,'recovery',label+'已恢復'))
                    announced=False
                db.execute('INSERT OR REPLACE INTO incidents VALUES(?,?,?,?,?,?)',(code,failures,int(active),last,message,int(announced)))
            # Notification history is derived metadata, never source media.
            db.execute('DELETE FROM notices WHERE seq NOT IN (SELECT seq FROM notices ORDER BY seq DESC LIMIT 2000)')

    def list(self, before=0, limit=30, unread=False):
        if type(before) is not int or before<0 or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('通知分頁不正確')
        with closing(self.db()) as db:
            where=[]; values=[]
            if before:where.append('seq<?');values.append(before)
            if unread:where.append('is_read=0')
            rows=[dict(r) for r in db.execute('SELECT * FROM notices'+(' WHERE '+' AND '.join(where) if where else '')+
                 ' ORDER BY seq DESC LIMIT ?',values+[limit+1])]
            count=db.execute('SELECT count(*) FROM notices WHERE is_read=0').fetchone()[0]
            latest=db.execute('SELECT coalesce(max(seq),0) FROM notices').fetchone()[0]
            active=[dict(r) for r in db.execute('SELECT code,message FROM incidents WHERE active=1 ORDER BY code')]
        return {'items':rows[:limit],'unread':count,'latest':latest,'active':active,
                'next_before':rows[limit-1]['seq'] if len(rows)>limit else None}

    def acknowledge(self, through):
        if type(through) is not int or through<0:raise ValueError('通知編號不正確')
        with closing(self.db()) as db,db:
            db.execute('UPDATE notices SET is_read=1 WHERE seq<=?',(through,))
        return {'ok':True}
