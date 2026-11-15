# -*- coding: utf-8 -*-
"""Videoschnipsel aufnehmen - mit einstellbarem Vorlauf.

Ohne Vorlauf: bei einem Ereignis startet ein ffmpeg-Prozess und nimmt direkt von der Kamera auf.

Mit Vorlauf (record_pre_s > 0): Fuer den Stream laeuft dauerhaft ein Ringpuffer - ffmpeg kopiert den
Kamerastream ohne Umrechnen in kurze Stuecke (ca. 2 s, im Arbeitsspeicher unter /dev/shm), die
reihum ueberschrieben werden. Bei einem Ereignis werden die Stuecke ab "Ereignis minus Vorlauf"
gesichert, waehrend der Aufnahme laufend weitere eingesammelt und am Ende zu einer MP4-Datei
zusammengefuegt. Da nur an Schluesselbildern geschnitten werden kann, beginnt das Video bis zu
einem Stueck (bzw. einen Keyframe-Abstand) vor dem eingestellten Vorlauf.

H.264/H.265 werden ohne Umrechnen gespeichert (praktisch keine Rechenlast), MJPEG u. a. erst beim
Zusammenfuegen in H.264 umgerechnet, damit Browser sie abspielen.
"""

import glob
import json
import math
import os
import shutil
import subprocess
import threading
import time

from .streams import ffmpeg_major, find_ffmpeg, find_tool, mask_url

SEG_S = 2.0


def _rtsp_opts(ff, url, transport):
    if not url.lower().startswith("rtsp"):
        return []
    return ["-rtsp_transport", transport or "tcp"] + (
        ["-timeout", "10000000"] if ffmpeg_major(ff) >= 5 else ["-stimeout", "10000000"])


def _enc_opts(codec, scfg):
    codec = (codec or "").lower()
    if codec == "h264":
        return ["-c:v", "copy"]
    if codec in ("hevc", "h265"):
        return ["-c:v", "copy", "-tag:v", "hvc1"]
    fps = max(2, min(15, int(scfg.get("record_fps", 10) or 10)))
    return ["-vf", "fps=%d,scale='min(1280,iw)':-2" % fps, "-c:v", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency", "-crf", "27", "-pix_fmt", "yuv420p", "-g", str(fps * 2)]


def _duration(path):
    fp = find_tool("ffprobe")
    if not fp:
        return None
    try:
        p = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        return float(json.loads(p.stdout.decode() or "{}").get("format", {}).get("duration"))
    except Exception:
        return None


def ring_base(data_dir):
    """Ringpuffer im Arbeitsspeicher (schont SD-Karte/eMMC), sonst im Datenordner."""
    shm = "/dev/shm"
    if os.path.isdir(shm) and os.access(shm, os.W_OK):
        return os.path.join(shm, "sentinel-ring")
    return os.path.join(data_dir, "ring")


# ----------------------------------------------------------------------------
class Ring(object):
    """Dauerhafter Ringpuffer eines Streams."""

    def __init__(self, sid, base, log):
        self.sid = sid
        self.dir = os.path.join(base, sid)
        self.log = log
        self.proc = None
        self.sig = None
        self.started = 0.0
        self.fails = 0
        self.next_try = 0.0
        self.err = ""
        self.codec = ""

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def healthy(self):
        """laeuft und schreibt (neuestes Stueck juenger als 20 s)"""
        if not self.running():
            return False
        segs = self.segments()
        return bool(segs) and time.time() - segs[-1][0] < 20

    def segments(self):
        """[(mtime, pfad, groesse)] nach Zeit sortiert"""
        out = []
        for p in glob.glob(os.path.join(self.dir, "seg*.mkv")):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size > 0:
                out.append((st.st_mtime, p, st.st_size))
        out.sort()
        return out

    def complete(self):
        """fertig geschriebene Stuecke (alle ausser dem gerade wachsenden)"""
        segs = self.segments()
        return segs[:-1] if segs else []

    def start(self, scfg, codec, transport):
        ff = find_ffmpeg()
        if not ff:
            return False
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)
        pre = float(scfg.get("record_pre_s", 5) or 0)
        # Platz fuer Vorlauf + Reserve; waehrend einer Aufnahme werden Stuecke sofort herauskopiert
        wrap = int(max(6, math.ceil((pre + 4 * SEG_S + 6) / SEG_S)))
        url = scfg.get("url", "")
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin"] + _rtsp_opts(ff, url, transport)
        cmd += ["-fflags", "+genpts", "-i", url, "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "copy",
                "-f", "segment", "-segment_time", "%g" % SEG_S, "-segment_wrap", str(wrap),
                "-reset_timestamps", "1", "-segment_format", "matroska",
                os.path.join(self.dir, "seg%03d.mkv")]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE)
        except Exception as e:
            self.err = str(e)
            return False
        self.err = ""
        self.codec = codec
        self.started = time.time()
        threading.Thread(target=self._read_err, args=(self.proc,), daemon=True).start()
        return True

    def _read_err(self, p):
        try:
            for line in p.stderr:
                self.err = (self.err + line.decode("utf-8", "replace"))[-500:]
        except Exception:
            pass

    def stop(self, remove=False):
        p = self.proc
        self.proc = None
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        if remove:
            shutil.rmtree(self.dir, ignore_errors=True)


# ----------------------------------------------------------------------------
class Recording(object):
    def __init__(self, sid, path, rel, event_id, cfg, on_done, t_event):
        self.sid = sid
        self.path = path
        self.rel = rel
        self.event_id = event_id
        self.cfg = cfg
        self.on_done = on_done
        self.proc = None            # Direktaufnahme
        self.ring = None            # Aufnahme aus dem Ringpuffer
        self.work = None
        self.taken = {}             # (pfad, mtime) -> Kopie
        self.started = t_event
        self.last_motion = t_event
        self.err = ""
        self.ended = None

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

    def done(self, now):
        if self.ring is None:
            return self.proc.poll() is not None or self.due(now)
        return self.due(now) or not self.ring.running()

    def collect(self):
        """Ringpuffer: fertige Stuecke ab Ereignis minus Vorlauf herauskopieren."""
        t0 = self.started - float(self.cfg.get("pre_s", 0) or 0)
        for mt, p, _ in self.ring.complete():
            if mt <= t0 or (p, mt) in self.taken:
                continue
            dst = os.path.join(self.work, "part%05d.mkv" % (len(self.taken) + 1))
            try:
                shutil.copyfile(p, dst)
                self.taken[(p, mt)] = (mt, dst)
            except OSError:
                pass


class Recorder(object):
    def __init__(self, log, data_dir="/tmp"):
        self.log = log
        self.active = {}            # stream_id -> Recording
        self.rings = {}             # stream_id -> Ring
        self.base = ring_base(data_dir)
        self.work_base = os.path.join(data_dir, "rec_tmp")
        shutil.rmtree(self.base, ignore_errors=True)
        shutil.rmtree(self.work_base, ignore_errors=True)
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    # ------------------------------------------------------------ Ringpuffer
    def ensure_ring(self, scfg, codec, transport):
        """Ringpuffer fuer einen Stream mit Vorlauf starten/am Laufen halten."""
        sid = scfg["id"]
        sig = json.dumps([scfg.get("url"), transport, scfg.get("record_pre_s"), scfg.get("record_max_s")])
        r = self.rings.get(sid)
        if r is None:
            r = self.rings[sid] = Ring(sid, self.base, self.log)
        if r.sig == sig and (r.healthy() or (r.running() and time.time() - r.started < 25)):
            return
        if sid in self.active and self.active[sid].ring is r:
            return                              # waehrend einer Aufnahme nicht neu starten
        if time.time() < r.next_try:
            return
        if r.sig == sig and r.started:
            r.fails += 1
            if r.fails in (1, 5):
                self.log("Vorlauf-Puffer %s: neu gestartet%s" % (scfg.get("name", sid), (
                    " (%s)" % mask_url(r.err.strip().splitlines()[-1])) if r.err.strip() else ""))
            r.next_try = time.time() + min(120, 5 * r.fails)
        else:
            r.fails = 0
        r.sig = sig
        r.start(scfg, codec, transport)

    def stop_ring(self, sid):
        r = self.rings.pop(sid, None)
        if r:
            r.stop(remove=True)

    def ring_ids(self):
        return list(self.rings)

    def ring_info(self, sid):
        r = self.rings.get(sid)
        if not r:
            return None
        segs = r.segments()
        return dict(ok=r.healthy(), seconds=round(segs[-1][0] - segs[0][0], 1) if len(segs) > 1 else 0,
                    mb=round(sum(s[2] for s in segs) / 1048576.0, 1))

    # ------------------------------------------------------------ Aufnahmen
    def busy(self, sid):
        with self.lock:
            return sid in self.active

    def touch(self, sid):
        with self.lock:
            r = self.active.get(sid)
        if r:
            r.touch()

    def start(self, scfg, codec, transport, path, rel, event_id, on_done, t_event=None):
        """Aufnahme starten. scfg = Stream-Konfiguration (url, record_*). -> True/False"""
        ff = find_ffmpeg()
        if not ff:
            self.log("Aufnahme nicht möglich: ffmpeg fehlt")
            return False
        sid = scfg["id"]
        with self.lock:
            if sid in self.active:
                return False
        t_event = t_event or time.time()
        rc = dict(clip_s=scfg.get("record_s", 15), extend=scfg.get("record_extend", True),
                  max_s=scfg.get("record_max_s", 60), post_s=scfg.get("record_post_s", 5),
                  pre_s=float(scfg.get("record_pre_s", 0) or 0), codec=codec, scfg=scfg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = Recording(sid, path, rel, event_id, rc, on_done, t_event)
        ring = self.rings.get(sid)
        if rc["pre_s"] > 0 and ring is not None and ring.healthy():
            r.ring = ring
            r.work = os.path.join(self.work_base, "%s_%d" % (sid, int(t_event * 1000)))
            os.makedirs(r.work, exist_ok=True)
            r.collect()
        else:
            if rc["pre_s"] > 0:
                self.log("Aufnahme %s: Vorlauf-Puffer noch nicht bereit - Aufnahme ohne Vorlauf"
                         % scfg.get("name", sid))
            url = scfg.get("url", "")
            maxs = float(rc["max_s"] if rc["extend"] else rc["clip_s"]) + 5
            cmd = [ff, "-hide_banner", "-loglevel", "error", "-y"] + _rtsp_opts(ff, url, transport)
            cmd += ["-i", url, "-an", "-sn", "-dn", "-map", "0:v:0"] + _enc_opts(codec, scfg)
            cmd += ["-t", "%d" % maxs, "-movflags", "+faststart", "-f", "mp4", path + ".part"]
            try:
                # kein -nostdin: "q" auf stdin beendet ffmpeg sauber
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

    def _finish_direct(self, r):
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
        return time.time() - r.started

    def _finish_ring(self, r):
        """Auf das Stueck mit dem Aufnahmeende warten, einsammeln und zusammenfuegen."""
        end = r.ended or time.time()
        deadline = time.time() + SEG_S * 4 + 8
        while time.time() < deadline and r.ring.running():
            r.collect()
            done = [mt for mt, _, _ in r.ring.complete() if mt >= end]
            if done:
                break
            time.sleep(0.4)
        r.collect()
        parts = sorted(r.taken.values())
        if not parts:
            r.err = "keine Daten im Vorlauf-Puffer"
            return 0
        lst = os.path.join(r.work, "list.txt")
        with open(lst, "w") as f:
            for _, p in parts:
                f.write("file '%s'\n" % p.replace("'", "'\\''"))
        ff = find_ffmpeg()
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-nostdin", "-f", "concat", "-safe", "0",
               "-i", lst, "-map", "0:v:0"] + _enc_opts(r.cfg.get("codec"), r.cfg.get("scfg") or {})
        cmd += ["-movflags", "+faststart", "-f", "mp4", r.path + ".part"]
        try:
            p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
            r.err = p.stderr.decode("utf-8", "replace")[-600:]
        except Exception as e:
            r.err = str(e)
        return None

    def _finish(self, r):
        ok, size, dur = False, 0, 0
        try:
            if r.ring is not None:
                dur = self._finish_ring(r)
            else:
                dur = self._finish_direct(r)
            part = r.path + ".part"
            size = os.path.getsize(part) if os.path.exists(part) else 0
            ok = size > 2000
            if ok:
                os.rename(part, r.path)
                d = _duration(r.path)
                if d:
                    dur = d
                elif dur is None:
                    dur = time.time() - r.started + float(r.cfg.get("pre_s") or 0)
            else:
                try:
                    os.remove(part)
                except OSError:
                    pass
                self.log("Aufnahme fehlgeschlagen: %s" % (mask_url(r.err.strip().splitlines()[-1]) if r.err.strip()
                                                          else "keine Daten"))
        finally:
            if r.work:
                shutil.rmtree(r.work, ignore_errors=True)
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
                if r.ring is not None:
                    r.collect()                 # Stuecke sichern, bevor der Ring sie ueberschreibt
                if r.done(now):
                    r.ended = now
                    with self.lock:
                        self.active.pop(sid, None)
                    threading.Thread(target=self._finish, args=(r,), daemon=True).start()

    def stop_all(self):
        with self.lock:
            items = list(self.active.values())
            self.active.clear()
        for r in items:
            r.ended = time.time()
            self._finish(r)
        self.stop_ev.set()
        for sid in list(self.rings):
            self.stop_ring(sid)
