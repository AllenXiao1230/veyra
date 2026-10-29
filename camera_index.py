"""Rebuildable SQLite media index; cursors and date counts use indexed queries."""
from contextlib import closing
import sqlite3
import threading
import time


class MediaIndex:
    def __init__(self, root):
        self.root = root
        self.path = root / ".media-index.sqlite3"
        self.lock = threading.RLock()
        self.signature = None
        self.checked = 0
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS media(filename TEXT PRIMARY KEY,created_at TEXT,date TEXT,size INTEGER,mtime_ns INTEGER)")
            db.execute("CREATE INDEX IF NOT EXISTS media_cursor ON media(created_at DESC,filename DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS media_date ON media(date,created_at DESC,filename DESC)")

    def page(self, valid, created, dates, date, cursor, limit):
        with self.lock, closing(sqlite3.connect(self.path, timeout=5)) as db:
            signature = self.root.stat().st_mtime_ns
            if signature != self.signature or time.monotonic() - self.checked >= 2:
                rows = []
                for path in self.root.iterdir():
                    try:
                        valid(path.name)
                        if path.is_symlink() or not path.is_file():
                            continue
                        stat = path.stat()
                        stamp = created(path, dates)
                        from datetime import timezone, timedelta
                        rows.append((path.name, stamp.astimezone(timezone.utc).isoformat(),
                                     stamp.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
                                     stat.st_size, stat.st_mtime_ns))
                    except (OSError, ValueError):
                        continue
                with db:
                    db.execute("CREATE TEMP TABLE present(filename TEXT PRIMARY KEY)")
                    db.executemany("INSERT INTO present VALUES(?)", [(r[0],) for r in rows])
                    db.executemany("INSERT INTO media VALUES(?,?,?,?,?) ON CONFLICT(filename) DO UPDATE SET created_at=excluded.created_at,date=excluded.date,size=excluded.size,mtime_ns=excluded.mtime_ns WHERE media.created_at!=excluded.created_at OR media.size!=excluded.size OR media.mtime_ns!=excluded.mtime_ns", rows)
                    db.execute("DELETE FROM media WHERE filename NOT IN (SELECT filename FROM present)")
                self.signature = self.root.stat().st_mtime_ns
                self.checked = time.monotonic()
            clause, values = (" WHERE date=?", [date]) if date else ("", [])
            total = db.execute("SELECT count(*) FROM media").fetchone()[0]
            filtered = db.execute("SELECT count(*) FROM media" + clause, values).fetchone()[0]
            counts = [{"date": r[0], "count": r[1]} for r in db.execute("SELECT date,count(*) FROM media GROUP BY date ORDER BY date DESC")]
            if cursor:
                clause += (" AND " if clause else " WHERE ") + "(created_at,filename)<(?,?)"
                values += cursor
            db.row_factory = sqlite3.Row
            entries = [dict(row) for row in db.execute("SELECT filename,created_at,date,size FROM media" + clause + " ORDER BY created_at DESC,filename DESC LIMIT ?", values + [limit + 1])]
            return entries[:limit], total, filtered, counts, len(entries) > limit
