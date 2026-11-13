"""RTSP-/Videoquellen ueber ffmpeg.

Je Quelle laeuft ein ffmpeg-Prozess mit zwei Ausgaben aus derselben Bildfolge:
  * MJPEG (stdout)   -> Live-Ansicht im Browser und gespeicherte Vollbilder
  * Rohbild RGB24    -> Objekterkennung (numpy)
Beide Ausgaben werden mitgezaehlt, so dass zu jedem Rohbild das passende JPEG gehoert.
"""
import collections
import os
import re
import shutil
import subprocess
import threading
import time

import numpy as np

from .imaging import jpeg_size


def find_ffmpeg():
    for c in (shutil.which("ffmpeg"), "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if c and os.path.exists(c):
            return c
    return None


_FFVER = {}


def ffmpeg_major(ff):
    if ff not in _FFVER:
        v = 0
        try:
            out = subprocess.run([ff, "-version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=10).stdout.decode("utf-8", "replace")
            m = re.search(r"ffmpeg version n?(\d+)\.", out)
            v = int(m.group(1)) if m else 0
        except Exception:
            pass
        _FFVER[ff] = v
    return _FFVER[ff]


def mask_url(url):
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url or "")


class StreamWorker(threading.Thread):
    def __init__(self, cfg, log):
        super().__init__(daemon=True)
        self.cfg = dict(cfg)
        self.id = cfg["id"]
        self.log = log
        self.stop_ev = threading.Event()
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.proc = None
        self.status = "startet"
        self.error = ""
        self.size = None
        self.jpeg = None          # (idx, ts, bytes)
        self.jpegs = collections.OrderedDict()   # idx -> bytes (kurzer Puffer)
        self.raw = None           # (idx, ts, ndarray)
        self.fps = 0.0
        self.started = 0.0
        self._fps_t = time.time()
        self._fps_n = 0
        self.restarts = 0

    # ------------------------------------------------------------------
    def stop(self):
        self.stop_ev.set()
        self._kill()

    def _kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def _cmd(self, ff, wfd):
        c = self.cfg
        url = c.get("url", "").strip()
        fps = max(0.2, min(15.0, float(c.get("fps", 2) or 2)))
        width = int(c.get("width", 640) or 640)
        width = max(160, min(1920, width - width % 2))
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if c.get("keyframes_only"):
            cmd += ["-skip_frame", "nokey"]
        is_file = "://" not in url
        if url.lower().startswith("rtsp"):
            cmd += ["-rtsp_transport", c.get("transport", "tcp") or "tcp"]
            # ffmpeg < 5: -stimeout (bei 4.x wuerde -timeout den Empfangsmodus einschalten)
            cmd += ["-stimeout" if 0 < ffmpeg_major(ff) < 5 else "-timeout", "10000000"]
        if is_file:
            cmd += ["-re", "-stream_loop", "-1"]
        cmd += ["-fflags", "nobuffer+discardcorrupt", "-i", url, "-an", "-sn", "-dn",
                "-filter_complex", "[0:v]fps=%g,scale=%d:-2,split=2[a][b]" % (fps, width),
                "-map", "[a]", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", str(int(c.get("jpeg_q", 5) or 5)),
                "pipe:1",
                "-map", "[b]", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:%d" % wfd]
        return cmd

    def run(self):
        backoff = 2
        while not self.stop_ev.is_set():
            ff = find_ffmpeg()
            if not ff:
                self.status, self.error = "fehler", "ffmpeg nicht gefunden (opkg install ffmpeg)"
                self.stop_ev.wait(15)
                continue
            if not self.cfg.get("url"):
                self.status, self.error = "fehler", "keine URL eingetragen"
                self.stop_ev.wait(10)
                continue
            r, w = os.pipe()
            try:
                self.status, self.error = "verbinde", ""
                self.size = None
                self.proc = subprocess.Popen(self._cmd(ff, w), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             stdin=subprocess.DEVNULL, pass_fds=(w,), bufsize=0)
            except Exception as e:
                os.close(r)
                os.close(w)
                self.status, self.error = "fehler", "ffmpeg-Start: %s" % e
                self.stop_ev.wait(backoff)
                continue
            os.close(w)
            self.started = time.time()
            errbuf = collections.deque(maxlen=6)
            t_err = threading.Thread(target=self._read_err, args=(self.proc.stderr, errbuf), daemon=True)
            t_raw = threading.Thread(target=self._read_raw, args=(r,), daemon=True)
            t_err.start()
            t_raw.start()
            got = self._read_jpeg(self.proc.stdout)
            self._kill()
            t_raw.join(timeout=3)
            if self.stop_ev.is_set():
                break
            msg = " | ".join(errbuf) or "Verbindung beendet"
            self.status, self.error = "getrennt", msg[-300:]
            self.restarts += 1
            self.log("Stream %s: %s - neuer Versuch" % (self.cfg.get("name", self.id), msg[-160:]))
            backoff = 2 if got else min(60, backoff * 2)
            self.stop_ev.wait(backoff)
        self.status = "gestoppt"

    def _read_err(self, f, buf):
        for line in iter(f.readline, b""):
            t = line.decode("utf-8", "replace").strip()
            if t:
                buf.append(mask_url(t))

    def _read_jpeg(self, f):
        data = b""
        idx = 0
        got = False
        while not self.stop_ev.is_set():
            chunk = f.read(65536)
            if not chunk:
                break
            data += chunk
            while True:
                s = data.find(b"\xff\xd8")
                if s < 0:
                    data = b""
                    break
                e = data.find(b"\xff\xd9", s + 2)
                if e < 0:
                    if s > 0:
                        data = data[s:]
                    break
                frame = data[s:e + 2]
                data = data[e + 2:]
                if self.size is None:
                    self.size = jpeg_size(frame)
                now = time.time()
                with self.cond:
                    self.jpeg = (idx, now, frame)
                    self.jpegs[idx] = frame
                    while len(self.jpegs) > 12:
                        self.jpegs.popitem(last=False)
                    self.cond.notify_all()
                idx += 1
                got = True
                self.status = "live"
                self._fps_n += 1
                if now - self._fps_t >= 3:
                    self.fps = self._fps_n / (now - self._fps_t)
                    self._fps_n, self._fps_t = 0, now
        return got

    def _read_raw(self, fd):
        f = os.fdopen(fd, "rb", buffering=0)
        idx = 0
        try:
            while not self.stop_ev.is_set():
                t0 = time.time()
                while self.size is None and not self.stop_ev.is_set():
                    if self.proc is None or self.proc.poll() is not None or time.time() - t0 > 30:
                        return
                    time.sleep(0.05)
                w, h = self.size
                n = w * h * 3
                buf = bytearray(n)
                mv = memoryview(buf)
                got = 0
                while got < n:
                    k = f.readinto(mv[got:])
                    if not k:
                        return
                    got += k
                arr = np.frombuffer(bytes(buf), np.uint8).reshape(h, w, 3)
                with self.lock:
                    self.raw = (idx, time.time(), arr)
                idx += 1
        finally:
            try:
                f.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def latest_raw(self):
        with self.lock:
            return self.raw

    def jpeg_for(self, idx):
        with self.lock:
            j = self.jpegs.get(idx)
            if j is None and self.jpeg:
                j = self.jpeg[2]
            return j

    def latest_jpeg(self):
        with self.lock:
            return self.jpeg

    def wait_jpeg(self, last_idx, timeout=5.0):
        with self.cond:
            if self.jpeg is None or self.jpeg[0] == last_idx:
                self.cond.wait(timeout)
            return self.jpeg

    def info(self):
        return dict(id=self.id, name=self.cfg.get("name", self.id), status=self.status, error=self.error,
                    fps=round(self.fps, 1), size=self.size, restarts=self.restarts,
                    online=self.status == "live" and self.jpeg is not None and time.time() - self.jpeg[1] < 15)
