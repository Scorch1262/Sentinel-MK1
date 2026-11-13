"""Datenbank (SQLite) fuer Objekte, Sichtungen, Kategorien, Lernbeispiele und Alarme."""
import sqlite3
import threading
import time

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, color TEXT DEFAULT '#ff9142', sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS objects(
  id INTEGER PRIMARY KEY AUTOINCREMENT, cls TEXT NOT NULL, name TEXT DEFAULT '', category_id INTEGER,
  notes TEXT DEFAULT '', created REAL, first_seen REAL, last_seen REAL, sightings INTEGER DEFAULT 0,
  detections INTEGER DEFAULT 0, avatar_det INTEGER, has_face INTEGER DEFAULT 0, seq INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS detections(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, stream_id TEXT, stream_name TEXT, cls TEXT, score REAL,
  x1 REAL, y1 REAL, x2 REAL, y2 REAL, object_id INTEGER, track TEXT, sighting INTEGER DEFAULT 1,
  face INTEGER DEFAULT 0, match TEXT DEFAULT '', sim REAL DEFAULT 0, img TEXT, crop TEXT, manual INTEGER DEFAULT 0,
  app BLOB, fvec BLOB);
CREATE INDEX IF NOT EXISTS det_ts ON detections(ts);
CREATE INDEX IF NOT EXISTS det_obj ON detections(object_id, ts);
CREATE INDEX IF NOT EXISTS det_stream ON detections(stream_id, ts);
CREATE TABLE IF NOT EXISTS samples(
  id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER, kind TEXT, neg INTEGER DEFAULT 0,
  vec BLOB, det_id INTEGER, pinned INTEGER DEFAULT 0, ts REAL);
CREATE INDEX IF NOT EXISTS smp_obj ON samples(object_id);
CREATE TABLE IF NOT EXISTS alarms(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, enabled INTEGER DEFAULT 1, target_type TEXT, target TEXT,
  count_mode TEXT DEFAULT 'per_object', min_count INTEGER DEFAULT 3, window_min REAL DEFAULT 30,
  min_streams INTEGER DEFAULT 1, cooldown_min REAL DEFAULT 30, only_named INTEGER DEFAULT 0,
  mqtt INTEGER DEFAULT 1, webhook TEXT DEFAULT '', sound INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS alarm_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, alarm_id INTEGER, alarm_name TEXT, object_id INTEGER,
  object_name TEXT, cls TEXT, count INTEGER, streams TEXT, det_id INTEGER, ack INTEGER DEFAULT 0, text TEXT);
CREATE INDEX IF NOT EXISTS ev_ts ON alarm_events(ts);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def vec_blob(v):
    return None if v is None else np.asarray(v, np.float16).tobytes()


def blob_vec(b):
    return None if not b else np.frombuffer(b, np.float16).astype(np.float32)


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        self.db.executescript(SCHEMA)
        self.db.commit()

    def q(self, sql, args=(), one=False):
        with self.lock:
            cur = self.db.execute(sql, args)
            rows = cur.fetchall()
        rows = [dict(r) for r in rows]
        return (rows[0] if rows else None) if one else rows

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    def xmany(self, items):
        with self.lock:
            for sql, args in items:
                self.db.execute(sql, args)
            self.db.commit()

    def meta(self, k, default=None):
        r = self.q("SELECT v FROM meta WHERE k=?", (k,), one=True)
        return r["v"] if r else default

    def set_meta(self, k, v):
        self.x("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))

    def next_seq(self, cls):
        with self.lock:
            k = "seq_" + cls
            n = int(self.meta(k, "0")) + 1
            self.set_meta(k, n)
            return n

    def close(self):
        with self.lock:
            try:
                self.db.commit()
                self.db.close()
            except Exception:
                pass

    @staticmethod
    def now():
        return time.time()
