"""RTSP-/Videoquellen.

Zwei Wege, je nach Geraet:

* ffmpeg (Rueckfall, MJPEG-Kameras, PC): ein ffmpeg-Prozess mit zwei Ausgaben aus derselben Bildfolge -
  MJPEG fuer Anzeige/Galerie und Rohbild RGB24 fuer die Erkennung.
* GStreamer mit Hardware-Dekoder (NVIDIA Jetson): rtspsrc -> nvv4l2decoder (NVDEC) -> nvvidconv
  (Skalierung auf der GPU-Hardware) -> Rohbild. JPEGs werden nur bei Bedarf mit OpenCV erzeugt
  (Live-Ansicht, gespeicherte Bilder). Die CPU dekodiert dabei kein Video.

Klappt der Hardware-Weg nicht, wird automatisch auf ffmpeg umgeschaltet.
"""
import collections
import json
import os
import re
import select
import shutil
import subprocess
import threading
import time
from fractions import Fraction

import numpy as np

from . import sysinfo
from .imaging import jpeg_size


def find_ffmpeg():
    for c in (shutil.which("ffmpeg"), "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if c and os.path.exists(c):
            return c
    return None


def find_tool(name):
    for c in (shutil.which(name), "/usr/bin/" + name, "/usr/local/bin/" + name):
        if c and os.path.exists(c):
            return c
    return None


_FFVER = {}
_GST = {}


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


def _gst_inspect(element):
    if element not in _GST:
        txt = None
        tool = find_tool("gst-inspect-1.0")
        if tool:
            try:
                p = subprocess.run([tool, element], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
                if p.returncode == 0:
                    txt = p.stdout.decode("utf-8", "replace")
            except Exception:
                txt = None
        _GST[element] = txt
    return _GST[element]


def gst_has(element):
    """Gibt es das GStreamer-Element (z. B. nvv4l2decoder)?"""
    return _gst_inspect(element) is not None


def gst_prop(element, prop):
    """Hat das Element die Eigenschaft (z. B. nvv4l2decoder skip-frames)?"""
    txt = _gst_inspect(element) or ""
    return re.search(r"^\s+%s\s*:" % re.escape(prop), txt, re.M) is not None


def have_cv2():
    try:
        import cv2  # noqa: F401
        return True
    except Exception:
        return False


def nvdec_available():
    return (sysinfo.is_jetson() and find_tool("gst-launch-1.0") is not None and have_cv2()
            and gst_has("nvv4l2decoder") and gst_has("nvvidconv"))


def decoder_info():
    if nvdec_available():
        return "GStreamer + NVDEC (Hardware)"
    if find_ffmpeg():
        return "ffmpeg (CPU)"
    return "fehlt"


def mask_url(url):
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url or "")


def probe(url, timeout=25, transport="tcp"):
    """Codec und Aufloesung des ersten Videostroms ermitteln: (codec, breite, hoehe) oder (None, None, None)."""
    fp = find_tool("ffprobe")
    if fp:
        cmd = [fp, "-v", "error"]
        if url.lower().startswith("rtsp"):
            cmd += ["-rtsp_transport", transport or "tcp"]
        cmd += ["-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height", "-of", "json", url]
        try:
            out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout).stdout
            st = json.loads(out.decode("utf-8", "replace") or "{}").get("streams") or []
            if st and st[0].get("width"):
                return st[0].get("codec_name"), int(st[0]["width"]), int(st[0]["height"])
        except Exception:
            pass
    gd = find_tool("gst-discoverer-1.0")
    if gd:
        try:
            out = subprocess.run([gd, "-t", str(timeout), url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=timeout + 5).stdout.decode("utf-8", "replace")
            w = re.search(r"Width:\s*(\d+)", out)
            h = re.search(r"Height:\s*(\d+)", out)
            codec = "hevc" if re.search(r"H\.265|h265|HEVC", out, re.I) else "h264"
            if w and h:
                return codec, int(w.group(1)), int(h.group(1))
        except Exception:
            pass
    return None, None, None


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
        self.mode = None          # "ffmpeg" | "nvdec" | "gst"
        self.fail_hw = 0
        self.jpeg = None          # (idx, ts, bytes)        nur ffmpeg
        self.jpegs = collections.OrderedDict()   # idx -> bytes (Puffer)
        self.frames = collections.OrderedDict()  # idx -> (ts, rgb)  nur GStreamer
        self.raw = None           # (idx, ts, ndarray RGB)
        self.last_ts = 0.0
        self.fps = 0.0
        self.started = 0.0
        self._fps_t = time.time()
        self._fps_n = 0
        self.restarts = 0
        self.probed = None
        tr = (cfg.get("transport") or "auto").lower()
        self.auto_tr = tr not in ("tcp", "udp")
        self.proto = "tcp" if self.auto_tr else tr   # aktuell genutzter RTSP-Transport
        self.fail_tr = 0

    # ------------------------------------------------------------------
    def stop(self):
        self.stop_ev.set()
        self._kill()
        with self.cond:
            self.cond.notify_all()

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

    def _probe(self):
        if self.probed is None or not self.probed[0]:
            url = self.cfg.get("url", "").strip()
            self.status = "prüfe Stream"
            self.probed = probe(url, transport=self.proto)
            if not self.probed[0] and self.auto_tr and url.lower().startswith("rtsp"):
                other = "udp" if self.proto == "tcp" else "tcp"
                res = probe(url, transport=other)
                if res[0]:
                    self.log("Stream %s: antwortet nur per %s - nutze %s"
                             % (self.cfg.get("name", self.id), other.upper(), other.upper()))
                    self.proto, self.probed = other, res
            if self.probed[0]:
                self.log("Stream %s: %s %dx%d" % (self.cfg.get("name", self.id), self.probed[0], self.probed[1],
                                                     self.probed[2]))
            else:
                self.log("Stream %s: Codec/Größe nicht ermittelbar" % self.cfg.get("name", self.id))
        return self.probed

    def _choose_mode(self):
        url = self.cfg.get("url", "").strip()
        want = self.cfg.get("decoder") or "auto"
        rtsp = url.lower().startswith("rtsp")
        if want == "ffmpeg" or not rtsp:
            return "ffmpeg"
        hw_ok = want in ("auto", "nvdec") and self.fail_hw < 2 and nvdec_available()
        sw_ok = want == "gst" and self.fail_hw < 2 and find_tool("gst-launch-1.0") and have_cv2()
        if hw_ok or sw_ok:
            codec = (self._probe()[0] or "").lower()
            if codec not in ("h264", "hevc", "h265"):
                # MJPEG, MPEG-4 o. ae. bzw. unbekannt: ffmpeg (MJPEG ist ohnehin kaum Rechenarbeit)
                if codec and self.mode != "ffmpeg":
                    self.log("Stream %s: Codec %s - Hardware-Dekoder nur fuer H.264/H.265, nutze ffmpeg"
                             % (self.cfg.get("name", self.id), codec))
                return "ffmpeg"
            return "nvdec" if hw_ok else "gst"
        return "ffmpeg"

    def _out_size(self, w, h):
        width = int(self.cfg.get("width", 640) or 640)
        width = max(160, min(1920, width - width % 2))
        if not w or not h:
            return width, int(round(width * 9 / 16.0)) // 2 * 2
        width = min(width, w - w % 2)
        height = max(2, int(round(width * h / float(w))) // 2 * 2)
        return width, height

    def _fps(self):
        return max(0.2, min(15.0, float(self.cfg.get("fps", 2) or 2)))

    # ---------------------------------------------------------------- ffmpeg
    def _ffmpeg_cmd(self, ff, wfd):
        c = self.cfg
        url = c.get("url", "").strip()
        width = self._out_size(None, None)[0]
        scale = "%d:-2" % width
        pr = self.probed if (self.probed and self.probed[1]) else None
        if pr:
            # Ausgabegroesse vorab festlegen -> Rohbild-Leser startet sofort (kein Warten auf das erste JPEG)
            ow, oh = self._out_size(pr[1], pr[2])
            scale = "%d:%d" % (ow, oh)
            self.size = (ow, oh)
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if c.get("keyframes_only"):
            cmd += ["-skip_frame", "nokey"]
        is_file = "://" not in url
        if url.lower().startswith("rtsp"):
            cmd += ["-rtsp_transport", self.proto]
            v = ffmpeg_major(ff)
            # ffmpeg < 5: -stimeout (bei 4.x wuerde -timeout den Empfangsmodus einschalten)
            if 0 < v < 5:
                cmd += ["-stimeout", "10000000"]
            elif v >= 5:
                cmd += ["-timeout", "10000000"]
        if is_file:
            cmd += ["-re", "-stream_loop", "-1"]
        cmd += ["-fflags", "nobuffer+discardcorrupt", "-i", url, "-an", "-sn", "-dn",
                "-filter_complex", "[0:v]fps=%g,scale=%s,split=2[a0][b];[a0]format=yuvj420p[a]"
                % (self._fps(), scale),
                "-map", "[a]", "-f", "image2pipe", "-flush_packets", "1", "-c:v", "mjpeg", "-q:v", str(int(c.get("jpeg_q", 5) or 5)),
                "pipe:1",
                "-map", "[b]", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:%d" % wfd]
        return cmd

    # ---------------------------------------------------------------- GStreamer
    def _gst_cmd(self, wfd, hw):
        c = self.cfg
        url = c.get("url", "").strip()
        codec, w, h = self._probe()
        ow, oh = self._out_size(w, h)
        self.size = (ow, oh)
        h265 = (codec or "").lower() in ("hevc", "h265")
        # Caps-Filter: nur der Video-Teil des RTSP-Streams wird verbunden (Ton bleibt unberuehrt)
        depay = ("application/x-rtp,media=video,encoding-name=%s ! queue ! %s"
                 % (("H265", "rtph265depay ! h265parse") if h265 else ("H264", "rtph264depay ! h264parse")))
        fr = Fraction(self._fps()).limit_denominator(10)
        q = '"%s"' % url.replace("\\", "\\\\").replace('"', '\\"')
        proto = self.proto
        if hw:
            dec = "nvv4l2decoder"
            if gst_prop("nvv4l2decoder", "enable-max-performance"):
                dec += " enable-max-performance=true"
            if c.get("keyframes_only") and gst_prop("nvv4l2decoder", "skip-frames"):
                dec += " skip-frames=2"
            conv = "nvvidconv ! video/x-raw,format=BGRx,width=%d,height=%d" % (ow, oh)
        else:
            dec = "avdec_%s" % ("h265" if h265 else "h264")
            conv = "videoscale ! videoconvert ! video/x-raw,format=BGRx,width=%d,height=%d" % (ow, oh)
        # Jedes Element als eigenes Argument (gst-launch maskiert Leerzeichen innerhalb eines Arguments)
        tail = ("protocols=%s latency=300 drop-on-latency=true ! %s ! %s ! %s ! videorate drop-only=true ! "
                "video/x-raw,framerate=%d/%d ! fdsink fd=%d sync=false"
                % (proto, depay, dec, conv, fr.numerator, fr.denominator, wfd))
        return [find_tool("gst-launch-1.0"), "-q", "rtspsrc", "location=" + q] + tail.split()

    # ---------------------------------------------------------------- Hauptschleife
    def run(self):
        backoff = 2
        while not self.stop_ev.is_set():
            if not self.cfg.get("url"):
                self.status, self.error = "fehler", "keine URL eingetragen"
                self.stop_ev.wait(10)
                continue
            mode = self._choose_mode()
            if mode == "ffmpeg" and not find_ffmpeg():
                self.status, self.error = "fehler", "ffmpeg nicht gefunden"
                self.stop_ev.wait(15)
                continue
            if mode != self.mode:
                self.mode = mode
                self.log("Stream %s: Dekoder %s" % (self.cfg.get("name", self.id),
                                                    {"nvdec": "GStreamer + NVDEC (Hardware)",
                                                     "gst": "GStreamer (Software)"}.get(mode, "ffmpeg")))
            r, w = os.pipe()
            try:
                self.status, self.error = "verbinde", ""
                if mode == "ffmpeg":
                    self.size = None
                    self._probe()   # Groesse + (bei RTSP "auto") funktionierenden Transport ermitteln
                    cmd = self._ffmpeg_cmd(find_ffmpeg(), w)
                else:
                    cmd = self._gst_cmd(w, mode == "nvdec")
                env = dict(os.environ)
                if mode != "ffmpeg":
                    env.update(GST_DEBUG="2", GST_DEBUG_NO_COLOR="1")
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE if mode == "ffmpeg" else subprocess.DEVNULL,
                                             stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, pass_fds=(w,),
                                             bufsize=0, env=env)
            except Exception as e:
                os.close(r)
                os.close(w)
                self.status, self.error = "fehler", "Start: %s" % e
                self.stop_ev.wait(backoff)
                continue
            os.close(w)
            self.started = time.time()
            self.last_ts = 0.0
            errbuf = collections.deque(maxlen=8)
            threading.Thread(target=self._watch_start, args=(self.proc, errbuf), daemon=True).start()
            t_err = threading.Thread(target=self._read_err, args=(self.proc.stderr, errbuf), daemon=True)
            t_err.start()
            if mode == "ffmpeg":
                t_raw = threading.Thread(target=self._read_raw, args=(r, False), daemon=True)
                t_raw.start()
                got = self._read_jpeg(self.proc.stdout)
                self._kill()
                t_raw.join(timeout=3)
            else:
                got = self._read_raw(r, True)
                self._kill()
            if self.stop_ev.is_set():
                break
            lines = list(errbuf)
            errs = [l for l in lines if "ERROR" in l or "rror" in l or "keine Bilder" in l]
            msg = " | ".join((errs or lines)[-3:]) or "Verbindung beendet"
            low = msg.lower()
            if self.proto == "tcp" and not self.auto_tr and ("protocol not supported" in low or "461" in low):
                msg += " - Tipp: Kamera kann kein RTSP über TCP, Transport auf automatisch oder UDP stellen"
            elif "401" in low or "unauthorized" in low:
                msg += " - Tipp: Benutzer/Passwort im Link prüfen"
            elif "404" in low:
                msg += " - Tipp: Pfad hinter dem Port prüfen"
            if mode in ("nvdec", "gst") and not got:
                self.fail_hw += 1
                if self.fail_hw >= 2:
                    self.log("Stream %s: GStreamer liefert keine Bilder (%s) - wechsle zu ffmpeg"
                             % (self.cfg.get("name", self.id), msg[-200:]))
            elif got:
                self.fail_hw = 0
            if got:
                self.fail_tr = 0
            elif self.auto_tr and self.cfg.get("url", "").lower().startswith("rtsp"):
                self.fail_tr += 1
                if self.fail_tr >= 2:
                    # wie VLC: klappt TCP nicht, mit UDP versuchen (und umgekehrt)
                    self.proto = "udp" if self.proto == "tcp" else "tcp"
                    self.fail_tr = 0
                    self.probed = None
                    self.log("Stream %s: keine Bilder - versuche RTSP ueber %s"
                             % (self.cfg.get("name", self.id), self.proto.upper()))
            self.status, self.error = "getrennt", msg[-400:]
            self.restarts += 1
            self.log("Stream %s: %s - neuer Versuch" % (self.cfg.get("name", self.id), msg[-160:]))
            backoff = 2 if got else min(60, backoff * 2)
            self.stop_ev.wait(backoff)
        self.status = "gestoppt"

    START_TIMEOUT = 25

    def _watch_start(self, proc, errbuf):
        """Kein erstes Bild nach START_TIMEOUT s -> Prozess beenden (Fehlerzaehler/Rueckfall greift)."""
        t0 = time.time()
        while not self.stop_ev.is_set() and proc.poll() is None:
            if self.last_ts:
                return
            if time.time() - t0 > self.START_TIMEOUT:
                errbuf.append("keine Bilder nach %d s" % self.START_TIMEOUT)
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                return
            time.sleep(0.5)

    def _read_err(self, f, buf):
        for line in iter(f.readline, b""):
            t = line.decode("utf-8", "replace").strip()
            if t:
                # GStreamer-Debugzeilen kuerzen: nur die Meldung hinter dem Elementnamen
                t = re.sub(r"^\S+\s+\d+\s+0x[0-9a-f]+\s+(WARN|ERROR)\s+", r"\1 ", t)
                buf.append(mask_url(t)[:300])

    def _tick(self, now):
        self.last_ts = now
        self.status = "live"
        self._fps_n += 1
        if now - self._fps_t >= 3:
            self.fps = self._fps_n / (now - self._fps_t)
            self._fps_n, self._fps_t = 0, now

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
                self._tick(now)
        return got

    def _read_raw(self, fd, gst):
        """Rohbilder lesen. ffmpeg: RGB24 (Groesse aus dem ersten JPEG). GStreamer: BGRx (Groesse bekannt)."""
        f = os.fdopen(fd, "rb", buffering=0)
        idx = 0
        got = False
        pre = bytearray()   # Rohdaten, die vor Bekanntwerden der Bildgroesse ankommen (sonst blockiert ffmpeg)
        try:
            while not self.stop_ev.is_set():
                t0 = time.time()
                while self.size is None and not self.stop_ev.is_set():
                    if self.proc is None or self.proc.poll() is not None or time.time() - t0 > 30:
                        return got
                    rd, _, _ = select.select([fd], [], [], 0.05)
                    if rd:
                        d = os.read(fd, 1 << 20)
                        if not d:
                            return got
                        pre += d
                        if len(pre) > (64 << 20):
                            del pre[:len(pre) - (32 << 20)]
                if self.size is None:
                    return got
                w, h = self.size
                ch = 4 if gst else 3
                n = w * h * ch
                buf = bytearray(n)
                mv = memoryview(buf)
                k = 0
                if pre:
                    k = min(n, len(pre))
                    buf[:k] = pre[:k]
                    del pre[:k]
                while k < n:
                    r = f.readinto(mv[k:])
                    if not r:
                        return got
                    k += r
                arr = np.frombuffer(bytes(buf), np.uint8).reshape(h, w, ch)
                now = time.time()
                if gst:
                    rgb = np.ascontiguousarray(arr[:, :, 2::-1])   # BGRx -> RGB
                    with self.cond:
                        self.raw = (idx, now, rgb)
                        self.frames[idx] = (now, rgb)
                        while len(self.frames) > 10:
                            old, _ = self.frames.popitem(last=False)
                            self.jpegs.pop(old, None)
                        self.cond.notify_all()
                    self._tick(now)
                else:
                    with self.lock:
                        self.raw = (idx, now, arr)
                idx += 1
                got = True
        finally:
            try:
                f.close()
            except Exception:
                pass
        return got

    # ------------------------------------------------------------------
    def _encode(self, idx):
        """JPEG fuer ein Rohbild erzeugen (nur GStreamer-Weg), mit kleinem Zwischenspeicher. Aufruf mit Lock."""
        j = self.jpegs.get(idx)
        if j is not None:
            return j
        fr = self.frames.get(idx)
        if fr is None:
            return None
        import cv2
        q = int(self.cfg.get("jpeg_quality", 80) or 80)
        ok, enc = cv2.imencode(".jpg", fr[1][:, :, ::-1], [int(cv2.IMWRITE_JPEG_QUALITY), q])
        if not ok:
            return None
        j = enc.tobytes()
        self.jpegs[idx] = j
        while len(self.jpegs) > 4:
            self.jpegs.popitem(last=False)
        return j

    def latest_raw(self):
        with self.lock:
            return self.raw

    def jpeg_for(self, idx):
        with self.lock:
            if self.mode in ("nvdec", "gst"):
                j = self._encode(idx)
                if j is None and self.frames:
                    j = self._encode(next(reversed(self.frames)))
                return j
            j = self.jpegs.get(idx)
            if j is None and self.jpeg:
                j = self.jpeg[2]
            return j

    def latest_jpeg(self):
        with self.lock:
            if self.mode in ("nvdec", "gst"):
                if not self.frames:
                    return None
                idx = next(reversed(self.frames))
                return (idx, self.frames[idx][0], self._encode(idx))
            return self.jpeg

    def wait_jpeg(self, last_idx, timeout=5.0):
        with self.cond:
            if self.mode in ("nvdec", "gst"):
                if not self.frames or next(reversed(self.frames)) == last_idx:
                    self.cond.wait(timeout)
                if not self.frames:
                    return None
                idx = next(reversed(self.frames))
                return (idx, self.frames[idx][0], self._encode(idx))
            if self.jpeg is None or self.jpeg[0] == last_idx:
                self.cond.wait(timeout)
            return self.jpeg

    def info(self):
        dec = {"nvdec": "NVDEC", "gst": "GStreamer", "ffmpeg": "ffmpeg"}.get(self.mode or "", "")
        rtsp = self.cfg.get("url", "").lower().startswith("rtsp")
        return dict(id=self.id, name=self.cfg.get("name", self.id), status=self.status, error=self.error,
                    fps=round(self.fps, 1), size=self.size, restarts=self.restarts, decoder=dec,
                    transport=(self.proto.upper() + (" (auto)" if self.auto_tr else "")) if rtsp else "",
                    online=self.status == "live" and time.time() - self.last_ts < 15)
