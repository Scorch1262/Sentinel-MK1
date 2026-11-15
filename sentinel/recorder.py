# -*- coding: utf-8 -*-
"""Videoschnipsel aufnehmen.

Pro Stream laeuft hoechstens eine Aufnahme: ein eigener ffmpeg-Prozess holt den Kamerastream und
schreibt ihn in eine MP4-Datei. H.264/H.265 werden ohne Umrechnen kopiert (praktisch keine
Rechenlast), MJPEG u. a. werden mit libx264 (ultrafast) umgerechnet, damit Browser sie abspielen.

Laenge: mindestens 'clip_s' Sekunden; mit 'extend' verlaengert sich die Aufnahme, solange
Bewegung anhaelt (Nachlauf 'post_s'), hoechstens 'max_s' Sekunden.
"""

import os
import subprocess
import threading
import time

from .streams import ffmpeg_major, find_ffmpeg, mask_url


class Recording(object):
    def __init__(self, sid, path, rel, event_id, cfg, log, on_done):
        self.sid = sid
        self.path = path
        self.rel = rel
        self.event_id = event_id
        self.cfg = cfg
        self.log = log
        self.on_done = on_done
        self.proc = None
        self.started = time.time()
        self.last_motion = self.started
        self.err = ""

    def touch(self):
        self.last_motion = time.time()

    def due(self, now):
        c = self.cfg
        clip_s = max(3.0, float(c.get("clip_s", 15) or 15))
        if not c.get("extend", True):
            return now - self.started >= clip_s
        max_s = max(clip_s, float(c.get("max_s", 60) or 60))
        post_s = float(c.get("post_s", 5) or 5)
        el = now - self.started
        return el >= max_s or (el >= clip_s and now - self.last_motion >= post_s)


class Recorder(object):
    def __init__(self, log):
        self.log = log
        self.active = {}            # stream_id -> Recording
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def busy(self, sid):
        with self.lock:
            return sid in self.active

    def touch(self, sid):
        with self.lock:
            r = self.active.get(sid)
        if r:
            r.touch()

    def start(self, scfg, codec, transport, path, rel, event_id, on_done):
        """Aufnahme starten. scfg = Stream-Konfiguration (url, record_*). -> True/False"""
        ff = find_ffmpeg()
        if not ff:
            self.log("Aufnahme nicht möglich: ffmpeg fehlt")
            return False
        sid = scfg["id"]
        with self.lock:
            if sid in self.active:
                return False
        url = scfg.get("url", "")
        rc = dict(clip_s=scfg.get("record_s", 15), extend=scfg.get("record_extend", True),
                  max_s=scfg.get("record_max_s", 60), post_s=scfg.get("record_post_s", 5))
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y"]   # kein -nostdin: "q" beendet sauber
        if url.lower().startswith("rtsp"):
            cmd += ["-rtsp_transport", transport or "tcp"]
            cmd += (["-timeout", "10000000"] if ffmpeg_major(ff) >= 5 else ["-stimeout", "10000000"])
        cmd += ["-i", url, "-an", "-sn", "-dn", "-map", "0:v:0"]
        codec = (codec or "").lower()
        maxs = float(rc["max_s"] if rc["extend"] else rc["clip_s"]) + 5
        if codec == "h264":
            cmd += ["-c:v", "copy"]
        elif codec in ("hevc", "h265"):
            cmd += ["-c:v", "copy", "-tag:v", "hvc1"]
        else:
            fps = max(2, min(15, int(scfg.get("record_fps", 10) or 10)))
            cmd += ["-vf", "fps=%d,scale='min(1280,iw)':-2" % fps, "-c:v", "libx264", "-preset", "ultrafast",
                    "-tune", "zerolatency", "-crf", "27", "-pix_fmt", "yuv420p", "-g", str(fps * 2)]
        cmd += ["-t", "%d" % maxs, "-movflags", "+faststart", "-f", "mp4", path + ".part"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = Recording(sid, path, rel, event_id, rc, self.log, on_done)
        try:
            r.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)
        except Exception as e:
            self.log("Aufnahme %s: Start fehlgeschlagen: %s" % (scfg.get("name", sid), e))
            return False
        threading.Thread(target=self._read_err, args=(r,), daemon=True).start()
        with self.lock:
            self.active[sid] = r
        return True

    def _read_err(self, r):
        try:
            for line in r.proc.stderr:
                r.err = (r.err + line.decode("utf-8", "replace"))[-600:]
        except Exception:
            pass

    def _finish(self, r):
        p = r.proc
        if p.poll() is None:
            try:
                p.stdin.write(b"q")       # ffmpeg sauber beenden -> MP4 wird abgeschlossen
                p.stdin.flush()
            except Exception:
                pass
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        dur = time.time() - r.started
        part = r.path + ".part"
        size = os.path.getsize(part) if os.path.exists(part) else 0
        ok = size > 2000
        if ok:
            os.rename(part, r.path)
        else:
            try:
                os.remove(part)
            except OSError:
                pass
            self.log("Aufnahme fehlgeschlagen: %s" % (mask_url(r.err.strip().splitlines()[-1]) if r.err.strip()
                                                      else "keine Daten"))
        try:
            r.on_done(r.event_id, r.rel if ok else None, dur if ok else 0, size if ok else 0)
        except Exception as e:
            self.log("Aufnahme: %s" % e)

    def _loop(self):
        while not self.stop_ev.wait(0.5):
            now = time.time()
            with self.lock:
                items = list(self.active.items())
            for sid, r in items:
                if r.proc.poll() is not None or r.due(now):
                    with self.lock:
                        self.active.pop(sid, None)
                    threading.Thread(target=self._finish, args=(r,), daemon=True).start()

    def stop_all(self):
        with self.lock:
            items = list(self.active.values())
            self.active.clear()
        for r in items:
            self._finish(r)
        self.stop_ev.set()
