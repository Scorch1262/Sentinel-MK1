"""Kern von Sentinel MK1: Streams, Erkennung, Verfolgung, Wiedererkennung, Galerie, Alarme."""
import collections
import json
import os
import shutil
import subprocess
import threading
import time
import traceback
import urllib.request

import numpy as np

from . import imaging, sysinfo, trt
from .detector import CLASS_GROUP, COCO, DE, GROUPS, SAME_TRACK, BodyReID, FaceEngine, NanoDet, YoloX, detect_tiled
from .models import DETECTORS, ModelManager
from .mqtt import MqttSender
from .store import Store, blob_vec, vec_blob
from .recorder import Recorder
from .storage import MediaStore, SpaceMonitor, topic_name
from .streams import StreamWorker, decoder_info, find_ffmpeg, mask_url


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def track_key(cls):
    return SAME_TRACK.get(cls, cls)


def fmt_dur(minutes):
    m = float(minutes)
    if m >= 60 and m % 60 == 0:
        return "%d h" % (m // 60)
    return "%g min" % m


class Track:
    __slots__ = ("tid", "cls", "key", "box", "score", "hits", "first_ts", "last_ts", "confirmed", "obj",
                 "last_save", "face_tries", "face_last", "has_face", "dets", "match", "named", "rejected",
                 "retry_at", "scores", "why", "moved", "c0", "body", "body_tries", "body_last", "body0", "switched")

    def __init__(self, tid, cls, box, score, ts):
        self.body, self.body_tries, self.body_last = None, 9, 0.0
        self.body0, self.switched = None, False
        self.tid, self.cls, self.key = tid, cls, track_key(cls)
        self.box, self.score = box, score
        self.hits, self.first_ts, self.last_ts = 1, ts, ts
        self.confirmed = False
        self.obj = None
        self.last_save = 0.0
        self.face_tries = 0
        self.face_last = 0.0
        self.has_face = False
        self.dets = []
        self.match = ""
        self.named = False
        self.rejected = False      # von Fehlerkennungs-Filter oder Zweitpruefung verworfen
        self.retry_at = 0.0
        self.scores = [score]
        self.why = ""
        self.moved = False         # hat sich im Bereich des Objekts etwas bewegt?
        self.c0 = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


class IdentityIndex:
    """Lernbeispiele (positiv/negativ) aller Objekte im Speicher, fuer schnelle Zuordnung.

    Arten: face (Gesicht, SFace), body (ganzer Koerper, YoutuReID - nur Personen), app (Farbhistogramm,
    Rueckfall fuer Tiere/Fahrzeuge und Personen ohne Koerpermerkmal). Von Hand zugewiesene Beispiele
    sind 'fest' (pinned): sie verfallen nicht und zaehlen ohne Zeitfenster."""

    KINDS = ("app", "face", "body")

    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.objs = {}      # oid -> dict(cls, last_seen, <kind>=[vec], <kind>_neg=[vec], <kind>_meta=[(pinned, ts)])
        self.load()

    @classmethod
    def _empty(cls, ocls, last_seen):
        o = dict(cls=ocls, last_seen=last_seen or 0)
        for k in cls.KINDS:
            o[k], o[k + "_neg"], o[k + "_meta"] = [], [], []
        return o

    def _put(self, o, kind, neg, v, pinned, ts):
        if kind not in self.KINDS:
            return
        if neg:
            o[kind + "_neg"].append(v)
        else:
            o[kind].append(v)
            o[kind + "_meta"].append((bool(pinned), ts or 0))

    def load(self):
        with self.lock:
            self.objs = {}
            for o in self.store.q("SELECT id, cls, last_seen FROM objects"):
                self.objs[o["id"]] = self._empty(o["cls"], o["last_seen"])
            for s in self.store.q("SELECT object_id, kind, neg, vec, pinned, ts FROM samples ORDER BY id"):
                o = self.objs.get(s["object_id"])
                v = blob_vec(s["vec"])
                if o is None or v is None:
                    continue
                self._put(o, s["kind"], s["neg"], v, s["pinned"], s["ts"])

    def ensure(self, oid, cls, last_seen=0):
        with self.lock:
            if oid not in self.objs:
                self.objs[oid] = self._empty(cls, last_seen)
            return self.objs[oid]

    def drop(self, oid):
        with self.lock:
            self.objs.pop(oid, None)

    @staticmethod
    def _best(vecs, v):
        if not vecs or v is None:
            return -1.0
        return float(np.max(np.stack(vecs) @ v))

    @staticmethod
    def topk(vecs, v, k=3):
        """Mittel der k aehnlichsten Beispiele - robuster als ein einzelner Bestwert."""
        if not vecs or v is None:
            return -1.0
        sims = np.stack(vecs) @ v
        k = min(k, len(sims))
        s = float(np.sort(sims)[-k:].mean())
        return s - (0.03 if k == 1 else 0.0)        # nur ein Beispiel: etwas vorsichtiger

    def body_score(self, o, body, now, win):
        """Koerper-Aehnlichkeit zu einem Objekt (feste Beispiele immer, gelernte im Zeitfenster)."""
        vecs = [v for v, (pin, ts) in zip(o["body"], o["body_meta"]) if pin or now - ts <= win]
        if not vecs:
            return None
        s = self.topk(vecs, body, 3)
        if o["body_neg"] and self._best(o["body_neg"], body) >= s:
            return None                                 # sieht einem Gegenbeispiel aehnlicher
        return s

    def match(self, cls, app, face, now, cfg, body=None, exclude=None):
        """Rueckgabe (oid, art, aehnlichkeit) oder (None, '', bester Wert)."""
        key = track_key(cls)
        face_thr = float(cfg.get("face_threshold", 0.40))
        app_thr = float(cfg.get("appearance_threshold", 0.90))
        win = float(cfg.get("appearance_window_h" if cls == "person" else "appearance_window_other_h", 12)) * 3600
        best = (None, "", -1.0)
        with self.lock:
            items = [(oid, o) for oid, o in self.objs.items() if track_key(o["cls"]) == key and oid != exclude]
            if face is not None:
                for oid, o in items:
                    s = self._best(o["face"], face)
                    if s < face_thr:
                        continue
                    if self._best(o["face_neg"], face) >= s - 0.02:
                        continue
                    if s > best[2] or best[1] != "Gesicht":
                        best = (oid, "Gesicht", s)
                if best[0] is not None:
                    return best
            best = (None, "", -1.0)
            # Personen: Koerpermerkmal (ReID) mit Abstand zum Zweitbesten
            if body is not None and key == track_key("person"):
                thr = float(cfg.get("body_threshold", 0.60))
                margin = float(cfg.get("body_margin", 0.05))
                bwin = float(cfg.get("body_window_h", 72)) * 3600
                scores = []
                for oid, o in items:
                    if face is not None and o["face"] and self._best(o["face"], face) < face_thr * 0.6:
                        continue                        # Gesicht bekannt und eindeutig anders
                    sc = self.body_score(o, body, now, bwin)
                    if sc is not None:
                        scores.append((sc, oid))
                if scores:
                    scores.sort(reverse=True)
                    s1, oid = scores[0]
                    s2 = scores[1][0] if len(scores) > 1 else -1.0
                    if s1 >= thr and s1 - s2 >= margin:
                        return (oid, "Körper", s1)
                    best = (None, "", s1)
            if key == track_key("person") and cfg.get("body", True) and cfg.get("_body_active"):
                # Koerpermodell aktiv: Personen nie ueber das schwache Farbhistogramm zuordnen -
                # das war die Hauptursache fuer Verwechslungen (Kleidungsfarben aehneln sich oft)
                return best
            if key == track_key("person"):
                items = [(oid, o) for oid, o in items if not o["body"]]
            if app is None:
                return best
            for oid, o in items:
                if not o["app"]:
                    continue
                if now - (o["last_seen"] or 0) > win:
                    continue
                if face is not None and o["face"] and self._best(o["face"], face) < face_thr * 0.6:
                    continue
                s = self._best(o["app"], app)
                if s < app_thr or s <= best[2]:
                    continue
                if self._best(o["app_neg"], app) >= s:
                    continue
                best = (oid, "Aussehen", s)
            return best

    def add(self, oid, cls, kind, vec, det_id, now, max_samples, pinned=False, neg=False):
        if vec is None:
            return
        with self.lock:
            o = self.ensure(oid, cls)
            lst = o[kind + ("_neg" if neg else "")]
            if not pinned and not neg and lst and self._best(lst, vec) > 0.985:
                return  # fast identisch - kein neues Beispiel noetig
            self._put(o, kind, neg, np.asarray(vec, np.float32), pinned, now)
            self.store.x("INSERT INTO samples(object_id, kind, neg, vec, det_id, pinned, ts) VALUES(?,?,?,?,?,?,?)",
                         (oid, kind, 1 if neg else 0, vec_blob(vec), det_id, 1 if pinned else 0, now))
            # automatisch gelernte Beispiele begrenzen; feste (von Hand) bis zu einer hohen Obergrenze
            limit = max_samples if not pinned else max(max_samples, 400)
            if not neg and len(lst) > limit:
                rows = self.store.q("SELECT id FROM samples WHERE object_id=? AND kind=? AND neg=0 AND pinned=? "
                                    "ORDER BY id LIMIT ?", (oid, kind, 1 if pinned else 0, len(lst) - limit))
                if rows:
                    self.store.x("DELETE FROM samples WHERE id IN (%s)" % ",".join(str(r["id"]) for r in rows))
                    self.reload_obj(oid)

    def reload_obj(self, oid):
        with self.lock:
            o = self.objs.get(oid)
            if o is None:
                return
            fresh = self._empty(o["cls"], o["last_seen"])
            for s in self.store.q("SELECT kind, neg, vec, pinned, ts FROM samples WHERE object_id=? ORDER BY id", (oid,)):
                v = blob_vec(s["vec"])
                if v is not None:
                    self._put(fresh, s["kind"], s["neg"], v, s["pinned"], s["ts"])
            o.update({k: v for k, v in fresh.items() if k not in ("cls", "last_seen")})

    def touch(self, oid, ts):
        with self.lock:
            if oid in self.objs:
                self.objs[oid]["last_seen"] = ts

    def set_cls(self, oid, cls):
        with self.lock:
            if oid in self.objs:
                self.objs[oid]["cls"] = cls


def _clip_duration(path):
    try:
        from .recorder import _duration
        return _duration(path) if path and os.path.exists(path) else None
    except Exception:
        return None


def stream_mode(sc):
    """Analyse eines Streams: objects (KI-Objekterkennung) | motion (nur Bewegung) | off"""
    m = sc.get("analysis")
    if m in ("objects", "motion", "off"):
        return m
    return "objects" if sc.get("detect", True) else "off"


class Engine:
    @property
    def snap_dir(self):
        """aktiver Ordner fuer neue Bilder (Datenordner, Festplatte oder Netzwerkspeicher)"""
        return self.media.snap_dir

    def snap_path(self, rel):
        """vorhandenes Bild in einem der Speicherorte finden"""
        return self.media.find("snaps", rel)

    def __init__(self, base_dir, config, version):
        self.base = base_dir
        self.cfg = config
        self.version = version
        self.data_dir = os.path.join(base_dir, "data")
        self.avatar_dir = os.path.join(self.data_dir, "avatars")
        for d in (self.data_dir, os.path.join(self.data_dir, "snaps"), self.avatar_dir):
            os.makedirs(d, exist_ok=True)
        self.logbuf = collections.deque(maxlen=300)
        self.media = MediaStore(config, self.data_dir, self.log)
        self.space = SpaceMonitor(self.media, self.log)
        self.mq_sent = {}           # Topic -> zuletzt gesendeter Wert (Stream-Status)
        self.mq_full = 0.0
        self.last_motion_ts = {}
        self.recorder = Recorder(self.log, self.data_dir)
        self.mot = {}               # stream_id -> Zustand der Bewegungserkennung
        self.store = Store(os.path.join(self.data_dir, "sentinel.db"))
        # Aufnahmen, die beim letzten Beenden noch liefen, sind unvollstaendig -> Kennzeichen zuruecksetzen
        self.store.x("UPDATE events SET rec=0 WHERE rec=1")
        self.media.on_ready = self.rescan_media
        if self.media.state != "ersatz":
            threading.Thread(target=self.rescan_media, daemon=True).start()
        self.index = IdentityIndex(self.store)
        self.models = ModelManager(os.path.join(self.data_dir, "models"), self.log, wanted=self.wanted_models)
        self.fp_cache = None        # gelernte Fehlerkennungen (Liste), None = neu laden
        self.stats = dict(rejected_fp=0, rejected_verify=0, rejected_shape=0, rejected_static=0)
        self.bg = {}                # stream_id -> Hintergrund (Graustufen, klein, float32)
        self.motion = {}            # stream_id -> Bewegungsmaske des zuletzt analysierten Bildes
        self.mqtt = MqttSender()
        self.workers = {}
        self.tracks = {}            # stream_id -> [Track]
        self.live = {}              # stream_id -> Overlay
        self.analyzed = {}          # stream_id -> (ts, jpeg) des zuletzt analysierten Bildes
        self.last_proc = {}         # stream_id -> (idx, ts)
        self.last_gray = {}
        self.last_full = {}
        self.tid = 0
        self.det = None
        self.faces = None
        self.det_lock = threading.Lock()
        self.clean_lock = threading.Lock()
        self.build_pause = False
        self.det_ms = collections.deque(maxlen=30)
        self.body_ms = collections.deque(maxlen=30)
        self.body = None            # Personen-Wiedererkennung (Koerper)
        self.det_passes = collections.deque(maxlen=30)
        self.face_ms = collections.deque(maxlen=30)
        self.engine_error = ""
        self.cooldown = {}
        self.seen = {}              # (stream_id, object_id) -> zuletzt gesehen
        self.stop_ev = threading.Event()
        self.alarm_seq = 0
        self.lock = threading.RLock()
        self.started = time.time()
        self.updater = None          # wird von app.py gesetzt
        self.last_move_skipped = 0
        self.fan = None              # Luefterregelung (nur Jetson)

    # ------------------------------------------------------------------
    def log(self, msg):
        line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        self.logbuf.append(line)
        print(line, flush=True)

    def start(self):
        self.log("Sentinel MK1 v%s startet (Daten: %s)" % (self.version, self.data_dir))
        if sysinfo.is_jetson():
            self.log("Speicher: %s" % sysinfo.memory_text())
            try:
                swap = [l for l in open("/proc/meminfo") if l.startswith("SwapTotal")][0].split()[1]
                if int(swap) < 2 * 1048576:
                    self.log("WARNUNG: weniger als 2 GB Auslagerung - für 2 GB RAM zu wenig (Abstürze, Engine-Bau "
                             "scheitert). Abhilfe: sudo bash install-jetson.sh erneut ausführen (legt 4 GB an).")
            except Exception:
                pass
        if not find_ffmpeg():
            self.log("WARNUNG: ffmpeg fehlt - Jetson: sudo apt-get install ffmpeg (bzw. install-jetson.sh erneut ausführen)")
        if sysinfo.is_jetson() or os.environ.get("SENTINEL_FAN_SYSFS"):
            from .fan import FanController
            self.fan = FanController(self.cfg, self.log)
            if self.fan.available:
                self.fan.start()
            else:
                self.log("Kein regelbarer Lüfter gefunden (/sys/devices/pwm-fan)")
        self.sync_streams()
        if self.models.all_ok():
            self.load_models()
        else:
            self.models.ensure(on_done=self.load_models)
        threading.Thread(target=self._detect_loop, daemon=True).start()
        threading.Thread(target=self._housekeeping, daemon=True).start()
        threading.Thread(target=self._status_loop, daemon=True).start()

    def detector_choice(self):
        """Gewaehltes Erkennungsmodell; 'auto' = YOLOX-S auf der GPU, ohne GPU YOLOX-Tiny."""
        m = self.cfg.get("detection").get("model", "auto")
        if m not in DETECTORS:
            m = "yolox_s" if self.use_gpu() else "yolox_tiny"
        return m

    def wanted_models(self):
        w = [DETECTORS[self.detector_choice()]["key"], "face_det", "face_rec"]
        if self.cfg.get("identity").get("body", True):
            w.append("body_reid")
        return w

    def use_gpu(self):
        """GPU (TensorRT) verwenden? Einstellung detection.accel: auto | gpu | cpu"""
        acc = self.cfg.get("detection").get("accel", "auto")
        return acc != "cpu" and sysinfo.is_jetson() and trt.available()

    def load_models(self):
        with self.det_lock:
            try:
                d = self.cfg.get("detection")
                use_cv2 = bool(d.get("use_opencv", True))
                gpu = self.use_gpu()
                if self.build_pause:
                    return
                if gpu and self.models.all_ok() and self.models.engines_missing():
                    # Engines fehlen: einmalig bauen. Auf dem Jetson Nano 2GB reicht der Speicher nur,
                    # wenn Streams und KI-Modelle so lange angehalten werden.
                    self.build_pause = True
                    self.models.build_engines(on_done=self._resume_after_build, before=self._pause_for_build)
                    return
                choice = self.detector_choice()
                spec = DETECTORS[choice]
                key = spec["key"]
                if self.models.state.get(key) == "ok":
                    t = time.time()
                    det = None
                    eng = self.models.engine_path(key) if gpu and self.models.engine_ok(key) else None
                    if gpu and not eng and key == "yolox_m" and self.models.engine_ok("yolox_s") \
                            and self.models.ok("yolox_s", quick=True):
                        try:
                            det = YoloX(self.models.path("yolox_s"), 640, engine=self.models.engine_path("yolox_s"),
                                        title="YOLOX-S")
                            self.log("Übergangsweise YOLOX-S (GPU), bis die YOLOX-M-Engine fertig ist")
                        except Exception:
                            det = None
                    if det is None and gpu and not eng and key != "detector" and self.models.engine_ok("detector") \
                            and self.models.ok("detector", quick=True):
                        # Engine des gewaehlten Modells wird noch gebaut: so lange NanoDet auf der GPU
                        try:
                            det = NanoDet(self.models.path("detector"), 416, False,
                                          engine=self.models.engine_path("detector"))
                            self.log("Übergangsweise NanoDet (GPU), bis die %s-Engine fertig ist" % spec["title"])
                        except Exception:
                            det = None
                    if det is None:
                        try:
                            if spec["kind"] == "yolox":
                                det = YoloX(self.models.path(key), spec["input"], engine=eng, title=spec["title"])
                            else:
                                det = NanoDet(self.models.path(key), int(d.get("model_size", 320)), use_cv2, engine=eng)
                        except Exception as e:
                            if not eng:
                                raise
                            self.log("TensorRT-Erkennung nicht ladbar (%s) - nutze CPU" % e)
                            det = (YoloX(self.models.path(key), spec["input"], title=spec["title"])
                                   if spec["kind"] == "yolox" else
                                   NanoDet(self.models.path(key), int(d.get("model_size", 320)), use_cv2))
                    det.model_title = getattr(det, "title", None) or "NanoDet-Plus"
                    if isinstance(det, NanoDet):
                        det.model_title = "NanoDet-Plus"
                    self.det = det
                    self.log("Objekterkennung bereit: %s (%s, %d px, %.1f s)" % (det.model_title, det.backend,
                                                                                det.size, time.time() - t))
                elif not self.models.busy:
                    self.models.ensure(on_done=self.load_models)
                if self.models.state["face_det"] == "ok":
                    rec = self.models.path("face_rec") if self.models.state["face_rec"] == "ok" else None
                    eng = self.models.engine_path("face_rec") if rec and gpu and self.models.engine_ok("face_rec") else None
                    try:
                        faces = FaceEngine(self.models.path("face_det"), rec, sface_engine=eng)
                    except Exception as e:
                        if not eng:
                            raise
                        self.log("TensorRT-Gesichtsmerkmale nicht ladbar (%s) - nutze CPU" % e)
                        faces = FaceEngine(self.models.path("face_det"), rec)
                    self.faces = faces
                    self.log("Gesichtserkennung bereit%s" % ("" if rec else " (ohne Wiedererkennung)")
                             + (" (Merkmale auf GPU)" if faces.gpu else ""))
                if self.models.state.get("body_reid") == "ok" and self.cfg.get("identity").get("body", True):
                    beng = self.models.engine_path("body_reid") if gpu and self.models.engine_ok("body_reid") else None
                    t0 = time.time()
                    try:
                        body = BodyReID(self.models.path("body_reid"), engine=beng)
                    except Exception as e:
                        if not beng:
                            raise
                        self.log("TensorRT-Körpermerkmale nicht ladbar (%s) - nutze CPU" % e)
                        body = BodyReID(self.models.path("body_reid"))
                    self.body = body
                    self.log("Personen-Wiedererkennung (Körper) bereit: %s, %.1f s" % (body.backend, time.time() - t0))
                    if not getattr(self, "_backfill_started", False):
                        self._backfill_started = True
                        threading.Thread(target=self._body_backfill, daemon=True).start()
                elif self.cfg.get("identity").get("body", True) and self.models.state.get("body_reid") != "ok" \
                        and not self.models.busy:
                    self.models.ensure(on_done=self.load_models)
                self.engine_error = "" if self.det else "Modelle fehlen"
            except Exception as e:
                self.engine_error = "Modelle konnten nicht geladen werden: %s" % e
                self.log(self.engine_error)
                traceback.print_exc()

    def _pause_for_build(self):
        names = ", ".join(self.models.ENGINES[k] for k in self.models.engines_missing())
        self.log("GPU-Engines fehlen (%s): Streams und Erkennung werden für den einmaligen Bau angehalten "
                 "(ca. 5–20 Minuten) – %s" % (names, sysinfo.memory_text()))
        with self.lock:
            for w in self.workers.values():
                w.stop()
            self.workers.clear()
        for sid in self.recorder.ring_ids():
            self.recorder.stop_ring(sid)
        with self.lock:
            self.tracks.clear()
            self.live.clear()
        with self.det_lock:
            for obj in (self.det, self.faces):
                for attr in ("net", "det", "rec"):
                    n = getattr(obj, attr, None)
                    if n is not None and hasattr(n, "close"):
                        try:
                            n.close()
                        except Exception:
                            pass
            if self.body is not None:
                try:
                    self.body.close()
                except Exception:
                    pass
            self.det = None
            self.faces = None
            self.body = None
        import gc
        gc.collect()
        time.sleep(3)    # ffmpeg/GStreamer-Prozesse beenden lassen
        self.log("Speicher vor dem Bau: %s" % sysinfo.memory_text())

    def _resume_after_build(self):
        self.build_pause = False
        self.log("Engine-Bau beendet - Streams und Erkennung laufen wieder")
        self.sync_streams()
        self.load_models()

    def reload_detector(self):
        self.load_models()

    # ------------------------------------------------------------------
    # Streams
    # ------------------------------------------------------------------
    def sync_streams(self):
        if self.build_pause:
            return
        want = {s["id"]: s for s in self.cfg.get("streams") if s.get("enabled", True)}
        with self.lock:
            for sid in list(self.workers):
                w = self.workers[sid]
                if sid not in want or self._stream_sig(w.cfg) != self._stream_sig(want[sid]):
                    w.stop()
                    del self.workers[sid]
                    self.tracks.pop(sid, None)
                    self.live.pop(sid, None)
                elif w.cfg.get("name") != want[sid].get("name"):
                    w.cfg["name"] = want[sid].get("name")
            for sid, s in want.items():
                if sid not in self.workers:
                    w = StreamWorker(s, self.log)
                    self.workers[sid] = w
                    w.start()

    @staticmethod
    def _stream_sig(s):
        return json.dumps({k: s.get(k) for k in ("url", "fps", "width", "keyframes_only", "transport", "jpeg_q",
                                                 "decoder")},
                          sort_keys=True)

    def stream_cfg(self, sid):
        for s in self.cfg.get("streams"):
            if s["id"] == sid:
                return s
        return {}

    # ------------------------------------------------------------------
    # Erkennungsschleife
    # ------------------------------------------------------------------
    def _allowed(self):
        d = self.cfg.get("detection")
        cls = set(d.get("extra_classes") or [])
        for g in GROUPS:
            if g["id"] in (d.get("groups") or []):
                cls.update(g["classes"])
        return [COCO.index(c) for c in cls if c in COCO]

    def _detect_loop(self):
        rr = 0
        while not self.stop_ev.is_set():
            try:
                d = self.cfg.get("detection")
                obj_ok = d.get("enabled", True) and self.det is not None
                with self.lock:
                    ws = list(self.workers.values())
                if not ws:
                    time.sleep(0.5)
                    continue
                did = False
                for k in range(len(ws)):
                    w = ws[(rr + k) % len(ws)]
                    sc = self.stream_cfg(w.id)
                    mode = stream_mode(sc)
                    if mode == "off" or (mode == "objects" and not obj_ok):
                        continue
                    raw = w.latest_raw()
                    if raw is None or time.time() - raw[1] > 10:
                        continue
                    lp = self.last_proc.get(w.id)
                    iv = float(d.get("interval_s", 1.0)) if mode == "objects" else float(sc.get("motion_interval_s", 0.5))
                    if lp and (lp[0] == raw[0] or time.time() - lp[1] < iv):
                        continue
                    rr = (rr + k + 1) % len(ws)
                    self.last_proc[w.id] = (raw[0], time.time())
                    if mode == "motion":
                        self._process_motion(w, raw, d, sc)
                    else:
                        self._process(w, raw, d)
                    did = True
                    break
                if not did:
                    time.sleep(0.05)
            except Exception as e:
                self.engine_error = "Analysefehler: %s" % e
                self.log(self.engine_error)
                traceback.print_exc()
                time.sleep(2)

    def _process(self, w, raw, d):
        idx, ts_frame, frame = raw
        now = time.time()
        H, W = frame.shape[:2]
        tracks = self.tracks.setdefault(w.id, [])
        self._update_background(w.id, frame, d)
        if (self.stream_cfg(w.id).get("record") or "off") == "motion":
            self._motion_step(w, idx, W, H, now, self.stream_cfg(w.id))
        # Bewegungsfilter: ruhige Szene -> keine Analyse (vorhandene Objekte bleiben bestehen).
        # Massgeblich ist die bewegte Flaeche gegenueber dem Hintergrundmodell - auch eine kleine,
        # weit entfernte Person loest die Analyse aus (frueher: mittlerer Bildunterschied).
        if d.get("motion_gate", True):
            full_age = now - self.last_full.get(w.id, 0)
            pending = any(not t.confirmed for t in tracks)
            m = self.motion.get(w.id)
            if m is not None and full_age < 20 and not pending:
                frac = float(m.mean()) * 100.0
                if frac < float(d.get("motion_area_pct", 0.08)):
                    for t in tracks:
                        t.last_ts = now
                    self._publish_live(w.id, W, H, tracks, now, idle=True)
                    return
        self.last_full[w.id] = now
        t0 = time.time()
        thr = self._thresholds(d)
        with self.det_lock:
            if self.det:
                dets, passes = detect_tiled(self.det, frame, min(thr.values()), allowed=self._allowed(),
                                            mode=self._tile_mode(d), max_tiles=int(d.get("max_tiles", 4)))
            else:
                dets, passes = [], 0
        self.det_ms.append((time.time() - t0) * 1000)
        self.det_passes.append(passes)
        dets = self._plausible(dets, thr, d, W, H)
        self._update_tracks(w, tracks, dets, frame, idx, now, d)
        jpg = w.jpeg_for(idx)
        if jpg:
            self.analyzed[w.id] = (now, jpg)
        self._publish_live(w.id, W, H, tracks, now)

    def _tile_mode(self, d):
        """Kachel-Analyse: on | off | auto (auto = nur mit GPU, auf der CPU zu langsam)."""
        m = d.get("tiles", "auto")
        if m == "auto":
            return "on" if (self.det is not None and getattr(self.det, "gpu", False)) else "off"
        return "on" if m == "on" else "off"

    def _thresholds(self, d):
        """Mindest-Sicherheit je Gruppe (person/animal/vehicle/...); Grundwert fuer alle anderen."""
        base = float(d.get("threshold", 0.5))
        out = {g["id"]: base for g in GROUPS}
        for k, v in (d.get("group_thresholds") or {}).items():
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
        return out

    def _plausible(self, dets, thr, d, W, H):
        """Unplausible Treffer verwerfen: zu klein, zu unsicher fuer die Gruppe, falsche Form."""
        minpx = float(d.get("min_box_px", 24))
        asp = float(d.get("person_min_aspect", 0.9))
        out = []
        for x in dets:
            bw, bh = x[2] - x[0], x[3] - x[1]
            cls = COCO[x[5]]
            g = CLASS_GROUP.get(cls, {"id": "other"})["id"]
            if x[4] < thr.get(g, thr.get("other", 0.5)):
                continue
            if bh < minpx or bw < minpx * 0.4:
                continue
            if cls == "person" and asp > 0 and bh < bw * asp and x[4] < 0.8:
                # Schatten, Spiegelungen, liegende Formen: breiter als hoch -> nur sehr sichere Treffer
                self.stats["rejected_shape"] += 1
                continue
            if bw * bh > 0.85 * W * H:
                continue                           # ganzes Bild als Objekt: fast immer Fehltreffer
            out.append(x)
        return out

    # ------------------------------------------------------------------
    # Gelernte Fehlerkennungen + Zweitpruefung
    # ------------------------------------------------------------------
    def false_positives(self):
        if self.fp_cache is None:
            rows = self.store.q("SELECT id, stream_id, ckey, x1, y1, x2, y2, app FROM false_pos")
            for r in rows:
                r["app"] = blob_vec(r["app"])
            self.fp_cache = rows
        return self.fp_cache

    def _is_known_false(self, sid, t, frame):
        W, H = frame.shape[1], frame.shape[0]
        box = (t.box[0] / W, t.box[1] / H, t.box[2] / W, t.box[3] / H)
        app = None
        for fp in self.false_positives():
            if fp["stream_id"] != sid or fp["ckey"] != t.key:
                continue
            v = iou(box, (fp["x1"], fp["y1"], fp["x2"], fp["y2"]))
            if v >= 0.5:
                return True
            if v >= 0.2 and fp["app"] is not None:
                if app is None:
                    crop, _ = imaging.crop(frame, t.box)
                    app = imaging.appearance(crop) if crop is not None else None
                if app is not None and float(np.dot(app, fp["app"])) >= 0.88:
                    return True
        return False

    def _verify(self, frame, t, d):
        """Zweitpruefung: Ausschnitt um das Objekt vergroessert erneut erkennen lassen.
        Schatten, Muster und Spiegelungen fallen dabei meist durch."""
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = t.box
        bw, bh = x2 - x1, y2 - y1
        side = max(bw, bh) * 2.2
        side = max(side, min(W, H) * 0.35)
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        rx1, ry1 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        rx2, ry2 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
        if rx2 - rx1 < 16 or ry2 - ry1 < 16:
            return True
        # Ausschnitt deckt fast das ganze Bild ab: bringt nichts Neues
        if (rx2 - rx1) * (ry2 - ry1) > 0.8 * W * H:
            return True
        sub = frame[ry1:ry2, rx1:rx2]
        g = CLASS_GROUP.get(t.cls, {"id": "other"})["id"]
        need = self._thresholds(d).get(g, 0.5) + float(d.get("verify_margin", 0.05))
        with self.det_lock:
            res = self.det.detect(np.ascontiguousarray(sub), max(0.2, need - 0.2), allowed=self._allowed())
        lb = (x1 - rx1, y1 - ry1, x2 - rx1, y2 - ry1)
        for r in res:
            if track_key(COCO[r[5]]) == t.key and r[4] >= need and iou(lb, r[:4]) >= 0.35:
                return True
        return False

    # ------------------------------------------------------------------
    # Bewegungspflicht: neue Personen/Tiere nur, wenn sich dort etwas bewegt
    # ------------------------------------------------------------------
    BG_W = 256

    def _update_background(self, sid, frame, d):
        H, W = frame.shape[:2]
        h = max(9, int(round(self.BG_W * H / float(W))))
        g = imaging.gray_small(frame, self.BG_W, h)
        bg = self.bg.get(sid)
        if bg is None or bg.shape != g.shape:
            self.bg[sid] = g.copy()
            self.motion[sid] = np.zeros(g.shape, bool)
            return
        diff = np.abs(g - bg)
        # Helligkeitsaenderung des ganzen Bildes (Wolke, IR-Umschaltung) herausrechnen
        diff = np.abs(diff - np.median(diff))
        self.motion[sid] = diff > float(d.get("motion_pixel_level", 18))
        # langsam anpassen; bewegte Stellen noch langsamer (stehenbleibende Person verschwindet nicht sofort)
        a = np.where(self.motion[sid], 0.01, 0.05).astype(np.float32)
        self.bg[sid] = bg * (1 - a) + g * a

    def _check_moved(self, sid, t, frame):
        """Bewegung im Bereich der Spur oder deutliche Ortsveraenderung der Spur."""
        if t.moved:
            return True
        m = self.motion.get(sid)
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = t.box
        if m is not None:
            mh, mw = m.shape
            a, b = int(x1 * mw / W), int(y1 * mh / H)
            c, e = max(a + 1, int(np.ceil(x2 * mw / W))), max(b + 1, int(np.ceil(y2 * mh / H)))
            sub = m[b:e, a:c]
            if sub.size and float(sub.mean()) >= 0.06:
                t.moved = True
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        if ((cx - t.c0[0]) ** 2 + (cy - t.c0[1]) ** 2) ** 0.5 > 0.25 * max(1.0, y2 - y1):
            t.moved = True
        return t.moved

    def _accept(self, w, t, frame, now, d):
        """Darf eine Spur zur Sichtung werden? (Fehlerkennungs-Filter, Zweitpruefung)"""
        if self._is_known_false(w.id, t, frame):
            t.rejected, t.why = True, "gelernte Fehlerkennung"
            self.stats["rejected_fp"] += 1
            return False
        mean = sum(t.scores[-5:]) / float(len(t.scores[-5:]))
        g = CLASS_GROUP.get(t.cls, {"id": "other"})["id"]
        if mean < self._thresholds(d).get(g, 0.5):
            return False
        if g in (d.get("require_motion") or []) and not self._check_moved(w.id, t, frame):
            # steht still seit dem ersten Auftauchen (Schatten, Statue, Plakat ...): noch nicht melden
            if not t.why:
                t.why = "ohne Bewegung"
                self.stats["rejected_static"] += 1
            return False
        if d.get("verify", True) and self.det is not None:
            if not self._verify(frame, t, d):
                t.rejected, t.why = True, "Zweitprüfung"
                t.retry_at = now + float(d.get("verify_retry_s", 20))
                self.stats["rejected_verify"] += 1
                return False
        return True

    def _update_tracks(self, w, tracks, dets, frame, idx, now, d):
        timeout = max(float(d.get("track_timeout_s", 6.0)), 3 * float(d.get("interval_s", 1.0)))
        pairs = []
        tracks[:] = [t for t in tracks if not t.switched]      # vertauschte Spuren beenden
        for ti, t in enumerate(tracks):
            for di, x in enumerate(dets):
                cls = COCO[x[5]]
                if track_key(cls) != t.key:
                    continue
                v = iou(t.box, x[:4])
                if v < 0.2:
                    # schnelle Bewegung: Mittelpunktabstand relativ zur Groesse
                    cx1, cy1 = (t.box[0] + t.box[2]) / 2, (t.box[1] + t.box[3]) / 2
                    cx2, cy2 = (x[0] + x[2]) / 2, (x[1] + x[3]) / 2
                    sz = max(t.box[2] - t.box[0], t.box[3] - t.box[1], 1)
                    dist = ((cx1 - cx2) ** 2 + (cy1 - cy2) ** 2) ** 0.5 / sz
                    if dist > 0.8:
                        continue
                    v = 0.19 - dist * 0.1
                pairs.append((v, ti, di))
        pairs.sort(reverse=True)
        used_t, used_d = set(), set()
        for v, ti, di in pairs:
            if ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            t, x = tracks[ti], dets[di]
            t.box, t.score = tuple(x[:4]), x[4]
            t.scores.append(x[4])
            if len(t.scores) > 10:
                del t.scores[0]
            t.hits += 1
            t.last_ts = now
        for di, x in enumerate(dets):
            if di in used_d:
                continue
            self.tid += 1
            tracks.append(Track("%s-%d" % (w.id, self.tid), COCO[x[5]], tuple(x[:4]), x[4], now))
        # beendete Spuren entfernen
        tracks[:] = [t for t in tracks if now - t.last_ts <= timeout]
        min_hits = max(1, int(d.get("min_hits", 2)))
        for ti, t in enumerate(tracks):
            if t.last_ts != now:
                continue
            if t.confirmed and t.obj:
                self.seen[(w.id, t.obj)] = now
            elif not t.confirmed:
                self._check_moved(w.id, t, frame)
            try:
                if not t.confirmed and t.hits >= min_hits:
                    if t.rejected:
                        if t.why == "Zweitprüfung" and now >= t.retry_at:
                            t.rejected, t.why = False, ""       # erneut pruefen (z. B. Person kommt naeher)
                        else:
                            continue
                    if not self._accept(w, t, frame, now, d):
                        continue
                    t.confirmed = True
                    self._new_sighting(w, t, frame, idx, now)
                elif t.confirmed:
                    self._follow_up(w, t, frame, idx, now, d)
            except Exception as e:
                self.log("Fehler bei Sichtung: %s" % e)
                traceback.print_exc()

    # ------------------------------------------------------------------
    # Merkmale
    # ------------------------------------------------------------------
    def _features(self, frame, t, want_face=True):
        icfg = self.cfg.get("identity")
        crop, (ox, oy) = imaging.crop(frame, t.box, pad=0.0)
        if crop is None:
            return None, None, None
        app = imaging.appearance(crop)
        body = None
        if t.cls == "person" and self.body is not None and icfg.get("body", True) and not self._occluded(t) \
                and crop.shape[0] >= float(icfg.get("body_min_px", 80)):
            # zu kleine Personen (weit weg) liefern unzuverlaessige Koerpermerkmale -> lieber neu anlegen
            t0 = time.time()
            try:
                body = self.body.embed(crop)
            except Exception as e:
                if time.time() - getattr(self, "_body_err", 0) > 600:
                    self._body_err = time.time()
                    self.log("Körpermerkmal: %s" % e)
            self.body_ms.append((time.time() - t0) * 1000)
        if body is not None:
            t.body = body
            if t.body0 is None:
                t.body0 = body
            elif float(body @ t.body0) < float(icfg.get("body_switch", 0.35)):
                # Spur ist auf eine andere Person gesprungen (Personen haben sich gekreuzt)
                t.switched = True
        face = None
        lm_abs = None
        if want_face and t.cls == "person" and icfg.get("faces", True) and self.faces is not None:
            h = crop.shape[0]
            head = crop[:max(8, int(h * 0.6))]
            t0 = time.time()
            f = self.faces.find(head, thr=0.7)
            if f is not None:
                fw = f[1][2] - f[1][0]
                if fw >= float(icfg.get("face_min_px", 28)) and self.faces.rec is not None:
                    face = self.faces.embed(head, f[2])
                    lm_abs = f[2] + np.array([ox, oy], np.float32)
            self.face_ms.append((time.time() - t0) * 1000)
        return app, face, lm_abs, body

    def _occluded(self, t, limit=0.2):
        """Ueberlappt eine andere Person diesen Ausschnitt deutlich? Dann vermischt das Koerpermerkmal
        zwei Menschen - fuer Zuordnung und Lernen ungeeignet."""
        x1, y1, x2, y2 = t.box
        area = max(1.0, (x2 - x1) * (y2 - y1))
        for tl in self.tracks.values():
            if t not in tl:
                continue
            for o in tl:
                if o is t or o.cls != "person" or o.last_ts < t.last_ts - 0.01:
                    continue
                ix = max(0.0, min(x2, o.box[2]) - max(x1, o.box[0]))
                iy = max(0.0, min(y2, o.box[3]) - max(y1, o.box[1]))
                if ix * iy / area > limit:
                    return True
            break
        return False

    def _learn_body(self, oid, cls, body, det_id, now, strong=True):
        """Automatisch gelerntes Koerperbeispiel nur, wenn es zu den vorhandenen passt - sonst wuerde
        eine vertauschte Spur (Personen kreuzen sich) oder eine falsche Zuordnung das Objekt
        'verwaessern' und spaeter Fremde anziehen."""
        if body is None or not oid or not strong:
            return False
        icfg = self.cfg.get("identity")
        o = self.index.objs.get(oid)
        if o and o["body"]:
            if self.index.topk(o["body"], body, 3) < float(icfg.get("body_learn_min", 0.6)):
                return False
        self.index.add(oid, cls, "body", body, det_id, now, int(icfg.get("max_samples", 40)))
        return True

    # ------------------------------------------------------------------
    # Sichtungen
    # ------------------------------------------------------------------
    def _new_sighting(self, w, t, frame, idx, now):
        icfg = self.cfg.get("identity")
        app, face, _, body = self._features(frame, t)
        t.face_tries = 1
        t.face_last = now
        t.body_tries = 0
        t.body_last = now
        icfg = dict(icfg, _body_active=self.body is not None)
        oid, how, sim = self.index.match(t.cls, app, face, now, icfg, body=body)
        if oid is not None and not self.store.q("SELECT id FROM objects WHERE id=?", (oid,), one=True):
            self.index.drop(oid)
            oid = None
        if oid is None:
            oid = self._create_object(t.cls, now)
            how, sim = "neu", 0.0
        t.obj = oid
        t.match = how
        t.has_face = face is not None
        # Dasselbe Objekt war in diesem Stream eben noch da (Spur kurz verloren, Verdeckung,
        # Neustart) -> keine neue Sichtung, nur Fortsetzung
        d = self.cfg.get("detection")
        gap = float(d.get("sighting_gap_s", 60))
        last = self.seen.get((w.id, oid))
        if last is None and how != "neu":
            r = self.store.q("SELECT MAX(ts) m FROM detections WHERE object_id=? AND stream_id=?", (oid, w.id), one=True)
            last = r["m"] if r else None
        if how != "neu" and last and now - last < gap:
            old = next((o for o in self.tracks.get(w.id, []) if o is not t and o.obj == oid and o.confirmed), None)
            if old is not None:
                t.dets, t.last_save = old.dets, old.last_save
                t.has_face = t.has_face or old.has_face
                old.last_ts = 0  # alte Spur verwerfen
            self.seen[(w.id, oid)] = now
            if now - t.last_save < float(d.get("resnap_s", 120) or 1e9):
                return
            det_id = self._save_detection(w, t, frame, idx, now, app, face, sighting=0, match=how, sim=sim, body=body)
            self._after_detection(oid, det_id, now, sighting=False, has_face=face is not None)
            return
        self.seen[(w.id, oid)] = now
        det_id = self._save_detection(w, t, frame, idx, now, app, face, sighting=1, match=how, sim=sim, body=body)
        ms = int(icfg.get("max_samples", 40))
        self.index.add(oid, t.cls, "app", app, det_id, now, ms)
        if face is not None:
            self.index.add(oid, t.cls, "face", face, det_id, now, ms)
        # nur bei sicherer Zuordnung (bzw. neuer Person) lernen
        self._learn_body(oid, t.cls, body, det_id, now,
                         strong=how in ("neu", "Gesicht") or (how == "Körper" and sim >= float(icfg.get("body_threshold", 0.60)) + 0.1))
        self._after_detection(oid, det_id, now, sighting=True, has_face=face is not None)
        self._object_event(w, t, det_id, now)

    def _follow_up(self, w, t, frame, idx, now, d):
        icfg = self.cfg.get("identity")
        if t.switched:
            # vertauschte Spur beenden - die Person wird im naechsten Bild als neue Spur sauber erfasst
            t.last_ts = 0
            return
        # Neu angelegte Person: das erste Bild ist oft ungeeignet (halb im Bild, verdeckt). Mit
        # weiteren Bildern derselben Spur nachpruefen, ob es doch eine bekannte Person ist.
        if t.cls == "person" and self.body is not None and t.match == "neu" and t.body_tries < 6 \
                and now - t.body_last >= 1.5 and icfg.get("body", True):
            t.body_tries += 1
            t.body_last = now
            app, face, _, body = self._features(frame, t, want_face=False)
            if body is not None:
                oid, how, sim = self.index.match(t.cls, None, None, now, dict(icfg, _body_active=True),
                                                 body=body, exclude=t.obj)
                if t.switched:
                    t.last_ts = 0
                    return
                if oid is not None and oid != t.obj and t.dets:
                    # Passen auch die schon gespeicherten Bilder dieser Spur zur erkannten Person?
                    # (sonst zeigt das Bild jemand anderen - z. B. Spur hat die Person gewechselt)
                    tgt = self.index.objs.get(oid)
                    for r in self.store.q("SELECT bvec FROM detections WHERE id IN (%s) AND bvec IS NOT NULL"
                                          % ",".join(str(i) for i in t.dets)):
                        v = blob_vec(r["bvec"])
                        if tgt and tgt["body"] and v is not None and self.index.topk(tgt["body"], v, 3) < \
                                float(icfg.get("body_threshold", 0.60)) - 0.15:
                            oid = None
                            break
                if oid is not None and oid != t.obj:
                    self.log("Sichtung %s: am Körper erkannt -> Objekt %s (vorher neu angelegt %s)" % (t.tid, oid, t.obj))
                    old = t.obj
                    self._reassign_track(t, oid, move_samples=False)
                    t.match = "Körper"
                    self._cleanup_object(old)
                    self._learn_body(oid, t.cls, body, t.dets[-1] if t.dets else None, now,
                                     strong=sim >= float(icfg.get("body_threshold", 0.60)) + 0.1)
                    if t.dets:
                        self.store.x("UPDATE detections SET match=?, sim=? WHERE id IN (%s)"
                                     % ",".join(str(i) for i in t.dets), ("Körper", float(sim)))
                    return
                elif oid is None and t.obj:
                    # gleiche (neue) Person: weiteres Beispiel lernen -> spaeter besser wiedererkennbar
                    self._learn_body(t.obj, t.cls, body, t.dets[-1] if t.dets else None, now)
        # Personen ohne Gesicht: weiter nach einem Gesicht suchen (max. 8 Versuche)
        if t.cls == "person" and not t.has_face and t.face_tries < 8 and now - t.face_last >= 1.5 \
                and self.faces is not None and icfg.get("faces", True):
            t.face_tries += 1
            t.face_last = now
            app, face, _, body = self._features(frame, t)
            if face is not None:
                t.has_face = True
                oid, how, sim = self.index.match(t.cls, None, face, now, icfg)
                if oid is not None and oid != t.obj and t.match != "Gesicht":
                    # Gesicht gehoert zu einer bekannten Person -> Sichtung dorthin umbuchen
                    self.log("Sichtung %s: Gesicht erkannt -> Objekt %s (vorher %s)" % (t.tid, oid, t.obj))
                    old = t.obj
                    self._reassign_track(t, oid)
                    t.match = "Gesicht"
                    self._cleanup_object(old)
                det_id = self._save_detection(w, t, frame, idx, now, app, face, sighting=0, match="Gesicht",
                                              sim=sim if oid == t.obj else 0, body=body)
                ms = int(icfg.get("max_samples", 40))
                self.index.add(t.obj, t.cls, "face", face, det_id, now, ms)
                self.index.add(t.obj, t.cls, "app", app, det_id, now, ms)
                self._after_detection(t.obj, det_id, now, sighting=False, has_face=True)
                return
        if now - t.last_save >= float(d.get("resnap_s", 120)) > 0:
            app, face, _, body = self._features(frame, t, want_face=False)
            det_id = self._save_detection(w, t, frame, idx, now, app, None, sighting=0, match=t.match, sim=0, body=body)
            self._learn_body(t.obj, t.cls, body, det_id, now)
            self._after_detection(t.obj, det_id, now, sighting=False, has_face=False)

    def _create_object(self, cls, now):
        n = self.store.next_seq(cls)
        oid = self.store.x("INSERT INTO objects(cls, name, created, first_seen, last_seen, seq) VALUES(?,?,?,?,?,?)",
                           (cls, "", now, now, now, n))
        self.index.ensure(oid, cls, now)
        return oid

    def _save_detection(self, w, t, frame, idx, now, app, face, sighting, match, sim, body=None):
        H, W = frame.shape[:2]
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        folder = os.path.join(self.snap_dir, day)
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%H%M%S", time.localtime(now)) + "_%03d" % int((now % 1) * 1000)
        # Spur-Nummer im Namen: mehrere Objekte im selben Bild duerfen sich nicht ueberschreiben
        base = "%s_%s_%s_%s" % (stamp, w.id, t.cls.replace(" ", "-"), t.tid.rsplit("-", 1)[-1])
        img_rel = crop_rel = None
        jpg = w.jpeg_for(idx)
        if jpg:
            img_rel = "%s/%s.jpg" % (day, base)
            with open(os.path.join(self.snap_dir, img_rel), "wb") as f:
                f.write(jpg)
        crop, _ = imaging.crop(frame, t.box, pad=0.08)
        if crop is not None:
            crop_rel = "%s/%s_c.png" % (day, base)
            with open(os.path.join(self.snap_dir, crop_rel), "wb") as f:
                f.write(imaging.png_encode(imaging.thumb(crop, 220)))
        x1, y1, x2, y2 = t.box
        det_id = self.store.x(
            "INSERT INTO detections(ts, stream_id, stream_name, cls, score, x1, y1, x2, y2, object_id, track, "
            "sighting, face, match, sim, img, crop, app, fvec, bvec) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, w.id, w.cfg.get("name", w.id), t.cls, t.score, x1 / W, y1 / H, x2 / W, y2 / H, t.obj, t.tid,
             sighting, 1 if face is not None else 0, match, float(sim), img_rel, crop_rel, vec_blob(app),
             vec_blob(face), vec_blob(body)))
        t.dets.append(det_id)
        t.last_save = now
        return det_id

    def _after_detection(self, oid, det_id, now, sighting, has_face):
        o = self.store.q("SELECT * FROM objects WHERE id=?", (oid,), one=True)
        if not o:
            return
        upd_avatar = o["avatar_det"] is None or (has_face and not o["has_face"])
        self.store.x("UPDATE objects SET last_seen=?, sightings=sightings+?, detections=detections+1, "
                     "first_seen=COALESCE(first_seen, ?), has_face=MAX(has_face, ?) WHERE id=?",
                     (now, 1 if sighting else 0, now, 1 if has_face else 0, oid))
        self.index.touch(oid, now)
        if upd_avatar:
            self._set_avatar(oid, det_id)
        det = self.store.q("SELECT id, ts, stream_id, stream_name, cls, score, object_id, match, sim, face, img, crop "
                           "FROM detections WHERE id=?", (det_id,), one=True)
        m = self.cfg.get("mqtt")
        if m.get("enabled") and m.get("publish_detections") and det:
            self._mqtt_async("detection", dict(det, object_name=self.object_label(oid)))
        if sighting:
            self._check_alarms(oid, det_id, now)

    def _set_avatar(self, oid, det_id):
        r = self.store.q("SELECT crop FROM detections WHERE id=?", (det_id,), one=True)
        if r and r["crop"]:
            src = self.snap_path(r["crop"])
            if src and os.path.exists(src):
                shutil.copyfile(src, os.path.join(self.avatar_dir, "%d.png" % oid))
                self.store.x("UPDATE objects SET avatar_det=? WHERE id=?", (det_id, oid))

    # ------------------------------------------------------------------
    # Koerpermerkmale: nachberechnen, Vorschlaege, Ablehnungen
    # ------------------------------------------------------------------
    def _read_rgb(self, rel):
        p = self.snap_path(rel) if rel else None
        if not p or not os.path.exists(p):
            return None
        try:
            import cv2
            img = cv2.imread(p, cv2.IMREAD_COLOR)
            return None if img is None else img[:, :, ::-1].copy()
        except Exception:
            return None

    def _body_for_row(self, r, save=True):
        """Koerpermerkmal einer gespeicherten Erkennung aus ihrem Bild berechnen (und merken)."""
        if self.body is None or r.get("cls") != "person":
            return None
        crop = None
        if r.get("img") and r.get("match") != "import" and r.get("x2"):
            full = self._read_rgb(r["img"])
            if full is not None:
                H, W = full.shape[:2]
                crop, _ = imaging.crop(full, (r["x1"] * W, r["y1"] * H, r["x2"] * W, r["y2"] * H))
        if crop is None and r.get("crop"):
            crop = self._read_rgb(r["crop"])
        if crop is None:
            return None
        try:
            b = self.body
            v = b.embed(crop) if b is not None else None
        except Exception:
            return None
        if v is not None and save:
            self.store.x("UPDATE detections SET bvec=? WHERE id=?", (vec_blob(v), r["id"]))
        return v

    def _body_backfill(self):
        """Einmalig im Hintergrund: Koerpermerkmale fuer vorhandene Personenbilder berechnen und daraus
        Lernbeispiele bilden - damit bisherige Zuweisungen sofort zur Wiedererkennung beitragen."""
        try:
            time.sleep(20)
            todo = self.store.q("SELECT COUNT(*) n FROM detections WHERE cls='person' AND bvec IS NULL", one=True)["n"]
            done = 0
            if todo:
                self.log("Körpermerkmale: berechne %d vorhandene Personenbilder nach (Hintergrund) ..." % todo)
            while not self.stop_ev.is_set():
                if self.build_pause or self.body is None:
                    time.sleep(5)
                    continue
                rows = self.store.q("SELECT id, cls, img, crop, x1, y1, x2, y2, match FROM detections "
                                    "WHERE cls='person' AND bvec IS NULL AND id NOT IN (%s) "
                                    "ORDER BY (object_id IS NULL), manual DESC, ts DESC LIMIT 40"
                                    % (",".join(str(i) for i in getattr(self, "_bf_skip", set())) or "0"))
                if not rows:
                    break
                for r in rows:
                    if self._body_for_row(r) is None:
                        self.__dict__.setdefault("_bf_skip", set()).add(r["id"])   # Bild fehlt
                    else:
                        done += 1
                    time.sleep(0.02)
            self._body_samples_from_history()
            if todo:
                self.log("Körpermerkmale: %d Bilder nachberechnet, Lernbeispiele aus bisherigen Zuweisungen gebildet" % done)
        except Exception as e:
            self.log("Körpermerkmale nachberechnen: %s" % e)
            traceback.print_exc()

    def _body_samples_from_history(self):
        """Fuer Personen ohne Koerperbeispiele: aus ihren (zugewiesenen) Bildern Beispiele bilden;
        Gegenbeispiele aus frueheren Korrekturen uebernehmen."""
        ms = int(self.cfg.get("identity").get("max_samples", 40))
        have = set(r["object_id"] for r in self.store.q("SELECT DISTINCT object_id FROM samples WHERE kind='body' AND neg=0"))
        for o in self.store.q("SELECT id, cls FROM objects WHERE cls='person'"):
            if o["id"] in have:
                continue
            rows = self.store.q("SELECT id, ts, manual, bvec FROM detections WHERE object_id=? AND bvec IS NOT NULL "
                                "ORDER BY manual DESC, ts DESC LIMIT 200", (o["id"],))
            auto = 0
            for r in rows:
                v = blob_vec(r["bvec"])
                if r["manual"]:
                    self.index.add(o["id"], o["cls"], "body", v, r["id"], r["ts"], ms, pinned=True)
                elif auto < ms:
                    self.index.add(o["id"], o["cls"], "body", v, r["id"], r["ts"], ms)
                    auto += 1
        # Gegenbeispiele: wo frueher ein Aussehen-Gegenbeispiel gelernt wurde, auch den Koerper
        for s in self.store.q("SELECT DISTINCT s.object_id, s.det_id, d.bvec, d.ts FROM samples s JOIN detections d "
                              "ON d.id=s.det_id WHERE s.neg=1 AND s.kind='app' AND d.cls='person' AND d.bvec IS NOT NULL "
                              "AND NOT EXISTS (SELECT 1 FROM samples b WHERE b.object_id=s.object_id AND b.det_id=s.det_id "
                              "AND b.kind='body' AND b.neg=1)"):
            self.index.add(s["object_id"], "person", "body", blob_vec(s["bvec"]), s["det_id"], s["ts"], ms,
                           pinned=True, neg=True)

    def suggest(self, oid, limit=60, days=30):
        """Personenbilder anderer/unbenannter Objekte, die dieser Person aehneln (Koerpermerkmal)."""
        o = self.index.objs.get(int(oid))
        if not o or not o["body"]:
            return [], (0 if not o else len(o["body"]))
        thr = float(self.cfg.get("identity").get("suggest_threshold", 0.45))
        rows = self.store.q(
            "SELECT d.* FROM detections d LEFT JOIN objects b ON b.id=d.object_id WHERE d.cls='person' "
            "AND d.bvec IS NOT NULL AND (d.object_id IS NULL OR (d.object_id<>? AND IFNULL(b.name,'')='' AND d.manual=0)) "
            "AND d.ts>? AND d.id NOT IN (SELECT det_id FROM samples WHERE object_id=? AND kind='body' AND neg=1 "
            "AND det_id IS NOT NULL) ORDER BY d.ts DESC LIMIT 3000",
            (int(oid), time.time() - days * 86400, int(oid)))
        if not rows:
            return [], len(o["body"])
        V = np.stack([blob_vec(r["bvec"]) for r in rows])
        P = np.stack(o["body"])
        sims = V @ P.T
        k = min(3, P.shape[0])
        score = np.sort(sims, axis=1)[:, -k:].mean(axis=1)
        if o["body_neg"]:
            negb = (V @ np.stack(o["body_neg"]).T).max(axis=1)
            score = np.where(negb >= score, -1.0, score)
        order = np.argsort(-score)
        out = []
        for i in order[:limit]:
            if score[i] < thr:
                break
            r = rows[int(i)]
            r["score"] = float(score[i])
            out.append(r)
        return out, len(o["body"])

    def reject_suggestions(self, oid, det_ids):
        """'Ist nicht diese Person' -> Gegenbeispiele, damit diese Bilder nicht mehr vorgeschlagen werden
        und aehnlich aussehende Fremde nicht zugeordnet werden."""
        ms = int(self.cfg.get("identity").get("max_samples", 40))
        o = self.store.q("SELECT cls FROM objects WHERE id=?", (int(oid),), one=True)
        if not o:
            return 0
        n = 0
        now = time.time()
        for r in self.store.q("SELECT id, bvec FROM detections WHERE id IN (%s)"
                              % ",".join(str(int(i)) for i in det_ids or [0])):
            v = blob_vec(r["bvec"])
            if v is not None:
                self.index.add(int(oid), o["cls"], "body", v, r["id"], now, ms, pinned=True, neg=True)
                n += 1
        return n

    def _reassign_track(self, t, new_oid, move_samples=True):
        old = t.obj
        if t.dets:
            ids = ",".join(str(i) for i in t.dets)
            rows = self.store.q("SELECT id, sighting FROM detections WHERE id IN (%s)" % ids)
            self.store.x("UPDATE detections SET object_id=? WHERE id IN (%s)" % ids, (new_oid,))
            if move_samples:
                self.store.x("UPDATE samples SET object_id=? WHERE det_id IN (%s) AND object_id=? AND neg=0" % ids,
                             (new_oid, old))
            else:
                # Beispiele nicht blind uebernehmen (koennten eine vertauschte Spur enthalten) -
                # das Ziel lernt nur ueber die Pruefung in _learn_body
                self.store.x("DELETE FROM samples WHERE det_id IN (%s) AND object_id=?" % ids, (old,))
            n_s = sum(r["sighting"] for r in rows)
            self.store.xmany([
                ("UPDATE objects SET sightings=MAX(0, sightings-?), detections=MAX(0, detections-?) WHERE id=?",
                 (n_s, len(rows), old)),
                ("UPDATE objects SET sightings=sightings+?, detections=detections+?, last_seen=? WHERE id=?",
                 (n_s, len(rows), time.time(), new_oid))])
            self.index.reload_obj(old)
            self.index.reload_obj(new_oid)
        t.obj = new_oid

    def _cleanup_object(self, oid):
        """Automatisch angelegtes, leeres Objekt ohne Namen entfernen."""
        o = self.store.q("SELECT * FROM objects WHERE id=?", (oid,), one=True)
        if not o:
            return
        left = self.store.q("SELECT COUNT(*) n FROM detections WHERE object_id=?", (oid,), one=True)["n"]
        if left == 0 and not o["name"] and not o["category_id"]:
            self.delete_object(oid, delete_detections=False)

    def object_label(self, oid):
        o = self.store.q("SELECT cls, name, seq FROM objects WHERE id=?", (oid,), one=True)
        if not o:
            return "?"
        return o["name"] or "%s %d" % (DE.get(o["cls"], o["cls"]), o["seq"] or oid)

    # ------------------------------------------------------------------
    # Live-Anzeige
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Bewegungserkennung, Ereignisse und Videoaufnahmen
    # ------------------------------------------------------------------
    def _process_motion(self, w, raw, d, sc):
        """Stream im Modus 'nur Bewegung': keine KI, nur Hintergrundvergleich."""
        idx, ts_frame, frame = raw
        H, W = frame.shape[:2]
        now = time.time()
        self._update_background(w.id, frame, d)
        st = self._motion_step(w, idx, W, H, now, sc)
        boxes = []
        if st and st.get("active") and st.get("box"):
            b = st["box"]
            boxes.append(dict(x1=b[0], y1=b[1], x2=b[2], y2=b[3], cls="motion", label="Bewegung %.1f %%" % st["area"],
                              color="#3ecf8e", score=1.0, confirmed=True, obj=None, named=False, cat=None,
                              face=False, age=0))
        self.live[w.id] = dict(ts=now, w=W, h=H, boxes=boxes, idle=not boxes, motion=True)

    def _motion_region(self, m):
        """Bewegte Flaeche in % und umschliessender Rahmen (relativ) der groessten Bewegung."""
        if m is None or not m.any():
            return 0.0, None
        area = float(m.mean()) * 100.0
        mm = m
        try:
            import cv2
            k = np.ones((3, 3), np.uint8)
            mm = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_OPEN, k).astype(bool)   # Rauschen weg
            if not mm.any():
                return area, None
        except Exception:
            pass
        ys, xs = np.nonzero(mm)
        mh, mw = m.shape
        x1, x2 = np.percentile(xs, [2, 98])
        y1, y2 = np.percentile(ys, [2, 98])
        return area, (float(x1) / mw, float(y1) / mh, float(x2 + 1) / mw, float(y2 + 1) / mh)

    def _motion_step(self, w, idx, W, H, now, sc):
        """Bewegung auswerten: Ereignis anlegen (mit Bild), Aufnahme starten/verlaengern."""
        st = self.mot.setdefault(w.id, dict(active=False, hits=0, last=0.0, event=None, area=0.0, box=None))
        area, box = self._motion_region(self.motion.get(w.id))
        thr = float(sc.get("motion_pct", 0.5) or 0.5)
        moving = area >= thr and box is not None
        st["area"], st["box"] = area, box
        if moving:
            st["hits"] += 1
            st["last"] = now
            self.recorder.touch(w.id)
        else:
            st["hits"] = 0
        need = max(1, int(sc.get("motion_frames", 2) or 2))
        if not st["active"] and st["hits"] >= need and now - st.get("ended", 0) >= float(sc.get("motion_cooldown_s", 10)):
            st["active"] = True
            st["event"] = self._new_event(w, idx, now, "motion", "Bewegung", area, box, sc)
        elif st["active"]:
            if moving and st["event"]:
                self.store.x("UPDATE events SET ts_end=?, area=MAX(area, ?) WHERE id=?", (now, area, st["event"]))
                # Dauerbewegung: Aufnahme hat ihre Hoechstlaenge erreicht -> naechstes Ereignis mit neuem Video
                if (sc.get("record") or "off") == "motion" and not self.recorder.busy(w.id) and \
                        self.store.q("SELECT 1 FROM events WHERE id=? AND rec=0 AND clip IS NOT NULL",
                                     (st["event"],), one=True):
                    st["event"] = self._new_event(w, idx, now, "motion", "Bewegung", area, box, sc)
            if now - st["last"] > float(sc.get("motion_quiet_s", 8)):
                st["active"] = False
                st["ended"] = now
                st["event"] = None
        return st

    def _object_event(self, w, t, det_id, now):
        """Neue Sichtung in einem Stream mit 'Aufnahme bei Objekt'."""
        sc = self.stream_cfg(w.id)
        if (sc.get("record") or "off") != "objects":
            return
        r = self.store.q("SELECT img, x1, y1, x2, y2 FROM detections WHERE id=?", (det_id,), one=True)
        label = DE.get(t.cls, t.cls)
        if t.obj:
            label = self.object_label(t.obj) or label
        if self.recorder.busy(w.id):
            self.recorder.touch(w.id)
            return
        eid = self.store.x(
            "INSERT INTO events(ts, ts_end, stream_id, stream_name, kind, label, area, x1, y1, x2, y2, img, det_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, now, w.id, w.cfg.get("name", w.id), "object", label, 0, r["x1"] if r else None,
             r["y1"] if r else None, r["x2"] if r else None, r["y2"] if r else None, r["img"] if r else None, det_id))
        self._start_recording(w, eid, now, sc)

    def _new_event(self, w, idx, now, kind, label, area, box, sc):
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        stamp = time.strftime("%H%M%S", time.localtime(now)) + "_%03d" % int((now % 1) * 1000)
        img_rel = None
        jpg = w.jpeg_for(idx) or w.latest_jpeg()
        if isinstance(jpg, tuple):
            jpg = jpg[-1]
        if jpg:
            try:
                img_rel = "%s/%s_%s_bewegung.jpg" % (day, stamp, w.id)
                p = os.path.join(self.snap_dir, img_rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(jpg)
            except Exception as e:
                self.log("Ereignisbild: %s" % e)
                img_rel = None
        b = box or (None, None, None, None)
        eid = self.store.x(
            "INSERT INTO events(ts, ts_end, stream_id, stream_name, kind, label, area, x1, y1, x2, y2, img) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, now, w.id, w.cfg.get("name", w.id), kind, label, area, b[0], b[1], b[2], b[3], img_rel))
        if (sc.get("record") or "off") == "motion":
            self._start_recording(w, eid, now, sc)
        if self.cfg.get("mqtt").get("enabled"):
            self._mqtt_async("motion", dict(event=eid, stream=w.id, stream_name=w.cfg.get("name", w.id),
                                            area=round(area, 2), ts=now))
        return eid

    def _start_recording(self, w, eid, now, sc):
        if self.recorder.busy(w.id):
            return False
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        rel = "%s/%s_%s.mp4" % (day, time.strftime("%H%M%S", time.localtime(now)), w.id)
        codec = (w.probed[0] if getattr(w, "probed", None) else None) or ""
        if not self._space_ok():
            return False
        ok = self.recorder.start(sc, codec, getattr(w, "proto", "tcp"), os.path.join(self.media.clip_dir, rel), rel,
                                 eid, self._recording_done, t_event=now)
        if ok:
            self.store.x("UPDATE events SET rec=1 WHERE id=?", (eid,))
        return ok

    def _sync_rings(self):
        """Vorlauf-Ringpuffer fuer Streams mit Aufnahme + Vorlauf starten bzw. beenden."""
        want = set()
        if not self.build_pause:
            with self.lock:
                ws = list(self.workers.values())
            for w in ws:
                sc = self.stream_cfg(w.id)
                if not sc or (sc.get("record") or "off") == "off" or float(sc.get("record_pre_s", 0) or 0) <= 0:
                    continue
                if stream_mode(sc) == "off":
                    continue                    # ohne Analyse gibt es keine Ausloeser
                want.add(w.id)
                if w.status != "live":
                    continue                    # Ring erst starten, wenn die Kamera Bilder liefert
                codec = (w.probed[0] if getattr(w, "probed", None) else None) or ""
                try:
                    self.recorder.ensure_ring(sc, codec, getattr(w, "proto", "tcp"))
                except Exception as e:
                    self.log("Vorlauf-Puffer: %s" % e)
        for sid in self.recorder.ring_ids():
            if sid not in want:
                self.recorder.stop_ring(sid)

    def _space_ok(self):
        """Vor einer Aufnahme: genug Platz? Im Loop-Betrieb vorher Platz schaffen."""
        st = self.cfg.get("storage")
        free, _ = self._free()
        if free is None:
            return True
        if st.get("loop"):
            if free < self._reserve():
                self.loop_cleanup()
            return True
        if free < 200 * 1048576:
            if time.time() - getattr(self, "_full_warn", 0) > 600:
                self._full_warn = time.time()
                self.log("Speicher voll (%d MB frei) - keine Aufnahme. Tipp: Loop-Betrieb einschalten "
                         "oder alte Bilder/Videos löschen." % (free // 1048576))
            return False
        return True

    def _free(self):
        try:
            v = os.statvfs(self.media.root)
            return v.f_bavail * v.f_frsize, v.f_blocks * v.f_frsize
        except OSError:
            return None, None

    def _reserve(self):
        """Im Loop-Betrieb freizuhaltender Platz (Bytes)."""
        st = self.cfg.get("storage")
        free, total = self._free()
        gb = float(st.get("loop_reserve_gb", 2) or 0)
        pct = float(st.get("loop_reserve_pct", 5) or 0)
        return max(gb * 1073741824, (total or 0) * pct / 100.0, 300 * 1048576)

    def loop_cleanup(self):
        """Loop-Betrieb: Speicher bis auf die Reserve fuellen, dann die aeltesten Bilder/Videos
        ueberschreiben (loeschen). Gelernte Personen/Objekte bleiben erhalten."""
        st = self.cfg.get("storage")
        if not st.get("loop"):
            return 0
        if self.media.state == "ersatz":
            return 0        # Ziel fehlt: NIE den Bestand (Datenbank) anfassen - die Dateien liegen auf dem Ziel
        root = os.path.realpath(self.media.root) + os.sep

        def here(*rels, kind="snaps"):
            for r in rels:
                if r:
                    p = self.media.find(kind, r)
                    if p and os.path.exists(p) and os.path.realpath(p).startswith(root):
                        return True
            return False

        with self.clean_lock:
            removed = 0
            reserve = self._reserve()
            d_after = e_after = -1.0
            for _ in range(500):
                free, _t = self._free()
                if free is None or free >= reserve:
                    break
                # nur Eintraege, deren Dateien wirklich auf dem vollen Speicherort liegen
                d1, e1 = [], []
                while not d1:
                    rows = self.store.q("SELECT id, ts, img, crop FROM detections WHERE ts>? ORDER BY ts LIMIT 200",
                                        (d_after,))
                    if not rows:
                        break
                    d_after = rows[-1]["ts"]
                    d1 = [r for r in rows if here(r["img"], r["crop"])][:50]
                while not e1:
                    rows = self.store.q("SELECT id, ts, img, clip FROM events WHERE rec=0 AND ts>? ORDER BY ts LIMIT 100",
                                        (e_after,))
                    if not rows:
                        break
                    e_after = rows[-1]["ts"]
                    e1 = [r for r in rows if here(r["clip"], kind="clips") or here(r["img"])][:10]
                if d1:
                    d_after = d1[-1]["ts"]
                if e1:
                    e_after = e1[-1]["ts"]
                if not d1 and not e1:
                    if time.time() - getattr(self, "_loop_warn", 0) > 3600:
                        self._loop_warn = time.time()
                        self.log("Loop: Speicher unter der Reserve, aber keine eigenen Dateien mehr zum "
                                 "Löschen - der Platz wird von anderen Daten belegt")
                    break
                if e1 and (not d1 or e1[0]["ts"] <= d1[-1]["ts"]):
                    self.delete_events([r["id"] for r in e1])
                    removed += len(e1)
                else:
                    for r in d1:
                        self.delete_detection(r["id"], recount=False)
                    removed += len(d1)
            if removed:
                self.loop_removed = getattr(self, "loop_removed", 0) + removed
                self.store.x("UPDATE objects SET avatar_det=NULL WHERE avatar_det NOT IN (SELECT id FROM detections)")
                if time.time() - getattr(self, "_loop_log", 0) > 600:
                    self._loop_log = time.time()
                    self.log("Loop: %d älteste Bilder/Videos überschrieben (Reserve %.1f GB)"
                             % (removed, reserve / 1073741824.0))
            return removed

    def rescan_media(self, force=False):
        """Bilder/Videos auf den Speicherorten, die (z. B. nach einem Ausfall) nicht mehr in der
        Datenbank stehen, wieder aufnehmen - damit sie in Galerie und Ereignissen erscheinen."""
        if getattr(self, "_rescanning", False):
            return 0
        self._rescanning = True
        try:
            time.sleep(1.0)
            known = set()
            for r in self.store.q("SELECT img, crop FROM detections"):
                known.update(x for x in (r["img"], r["crop"]) if x)
            for r in self.store.q("SELECT img, clip FROM events"):
                known.update(x for x in (r["img"], r["clip"]) if x)
            names = {sc["id"]: sc.get("name", sc["id"]) for sc in self.cfg.get("streams")}
            found = {}                       # rel -> (kind, root)
            for root in self.media.roots():
                for kind in ("snaps", "clips"):
                    base = os.path.join(root, kind)
                    for dp, _, files in os.walk(base):
                        for fn in files:
                            if fn.endswith(".part"):
                                continue
                            rel = os.path.relpath(os.path.join(dp, fn), base).replace(os.sep, "/")
                            if rel not in known and rel not in found:
                                found[rel] = kind
            if not found:
                return 0

            def ts_of(day, stamp):
                try:
                    hms, ms = (stamp.split("_") + ["0"])[:2]
                    return time.mktime(time.strptime(day + " " + hms, "%Y-%m-%d %H%M%S")) + int(ms) / 1000.0
                except Exception:
                    return None
            dets = {}
            n_det = n_evt = 0
            for rel, kind in sorted(found.items()):
                if "/" not in rel:
                    continue
                day, fn = rel.split("/", 1)
                stem, ext = os.path.splitext(fn)
                if kind == "clips" and ext == ".mp4":
                    parts = stem.split("_", 1)
                    ts = ts_of(day, parts[0])
                    if ts is None or len(parts) < 2:
                        continue
                    sid = parts[1]
                    p = self.media.find("clips", rel)
                    dur = _clip_duration(p)
                    self.store.x("INSERT INTO events(ts, ts_end, stream_id, stream_name, kind, label, area, clip, "
                                 "clip_dur, clip_size, rec) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                                 (ts, ts + (dur or 0), sid, names.get(sid, sid), "motion", "Video (wiederhergestellt)", 0,
                                  rel, dur, os.path.getsize(p) if p and os.path.exists(p) else None))
                    n_evt += 1
                    continue
                if kind != "snaps":
                    continue
                if stem.endswith("_bewegung"):
                    bits = stem.split("_")
                    ts = ts_of(day, "_".join(bits[:2]))
                    if ts is None or len(bits) < 4:
                        continue
                    sid = "_".join(bits[2:-1])
                    self.store.x("INSERT INTO events(ts, ts_end, stream_id, stream_name, kind, label, area, img, rec) "
                                 "VALUES(?,?,?,?,?,?,?,?,0)", (ts, ts, sid, names.get(sid, sid), "motion", "Bewegung", 0, rel))
                    n_evt += 1
                    continue
                key = stem[:-2] if stem.endswith("_c") else stem
                d = dets.setdefault(day + "/" + key, {})
                d["crop" if stem.endswith("_c") else "img"] = rel
            for k, d in dets.items():
                day, base = k.split("/", 1)
                bits = base.split("_")
                if len(bits) < 5:
                    continue
                ts = ts_of(day, "_".join(bits[:2]))
                if ts is None:
                    continue
                cls = bits[-2].replace("-", " ")
                sid = "_".join(bits[2:-2])
                self.store.x(
                    "INSERT INTO detections(ts, stream_id, stream_name, cls, score, x1, y1, x2, y2, object_id, track, "
                    "sighting, face, match, sim, img, crop) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, sid, names.get(sid, sid), cls, 0.0, 0.0, 0.0, 1.0, 1.0, None, "import-" + bits[-1],
                     1, 0, "import", 0.0, d.get("img"), d.get("crop") or d.get("img")))
                n_det += 1
            if n_det or n_evt:
                self.log("Bestand eingelesen: %d Erkennungen und %d Ereignisse/Videos wieder in Galerie und "
                         "Ereignissen (Zuordnung zu Personen ggf. neu vornehmen)" % (n_det, n_evt))
            return n_det + n_evt
        except Exception as e:
            self.log("Bestand einlesen: %s" % e)
            return 0
        finally:
            self._rescanning = False

    # ------------------------------------------------------------------
    # Status je Stream und Speicherfuellstand (UI + MQTT)
    # ------------------------------------------------------------------
    def stream_states(self):
        """Aktueller Zustand jedes konfigurierten Streams (fuer MQTT-Subtopics)."""
        now = time.time()
        out = []
        for sc in self.cfg.get("streams"):
            sid = sc["id"]
            w = self.workers.get(sid)
            mode = stream_mode(sc) if sc.get("enabled", True) else "aus"
            online = bool(w and w.status == "live" and w.last_ts and now - w.last_ts < 10)
            m = self.motion.get(sid)
            lp = self.last_proc.get(sid)
            area = None
            if m is not None and lp and now - lp[1] < 5 and online and mode in ("objects", "motion"):
                area = round(float(m.mean()) * 100.0, 1)
            moving = area is not None and area >= float(sc.get("motion_pct", 0.5) or 0.5)
            if moving:
                self.last_motion_ts[sid] = now
            tr = [t for t in self.tracks.get(sid, []) if t.confirmed and not t.rejected and now - t.last_ts < 2.5]
            ring = self.recorder.ring_info(sid)
            lm = self.last_motion_ts.get(sid)
            out.append(dict(
                id=sid, name=sc.get("name", sid), key=topic_name(sc.get("name", sid)),
                erreichbar=1 if online else 0,
                status=(w.status if w else ("deaktiviert" if not sc.get("enabled", True) else "pausiert")),
                bilder_s=round(w.fps, 1) if w and online else 0,
                analyse={"objects": "objekte", "motion": "bewegung", "off": "aus"}.get(mode, mode),
                bewegung=1 if moving else 0,
                bewegung_flaeche=area if area is not None else 0,
                aufnahme=1 if self.recorder.busy(sid) else 0,
                aufnahme_modus={"off": "aus", "motion": "bewegung", "objects": "objekte"}.get(sc.get("record") or "off"),
                vorlauf_puffer=(1 if ring and ring.get("ok") else 0) if ring is not None else 0,
                personen=sum(1 for t in tr if t.cls == "person"),
                objekte=len(tr),
                letzte_bewegung=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(lm)) if lm else "",
                fehler=mask_url((w.error if w and not online else "") or "")))
        return out

    def _status_loop(self):
        last_space = 0.0
        backoff = 0.0
        while not self.stop_ev.wait(1.0):
            try:
                m = self.cfg.get("mqtt")
                if not m.get("enabled") or time.time() < backoff:
                    if not m.get("enabled"):
                        self.mq_sent = {}
                    continue
                fail = []

                def pub(topic, val, retain=False):
                    # Werte werden NICHT beim Broker gespeichert (kein retain)
                    if fail:
                        return False
                    if self.mqtt.publish(m, topic, val, retain=retain):
                        return True
                    fail.append(topic)
                    return False
                pre = (m.get("prefix") or "sentinel").strip("/")
                now = time.time()
                full = now - self.mq_full > 60          # alle 60 s alles erneut senden
                if m.get("publish_streams", True):
                    states = self.stream_states()
                    self._mq_clear_retained(pub, pre, "stream", [x["key"] for x in states],
                                            [k for k in states[0] if k not in ("id", "key")] if states else [])
                    for st in states:
                        base = "%s/stream/%s" % (pre, st["key"])
                        vals = {k: v for k, v in st.items() if k not in ("id", "key")}
                        changed = False
                        for k, v in vals.items():
                            topic = base + "/" + k
                            sv = str(v)
                            if full or self.mq_sent.get(topic) != sv:
                                if pub(topic, sv):
                                    self.mq_sent[topic] = sv
                                    changed = True
                        if changed:
                            pub(base + "/json", json.dumps(vals, ensure_ascii=False))
                if m.get("publish_storage", True) and now - last_space >= float(m.get("storage_interval_s", 1) or 1) - 0.05:
                    last_space = now
                    locs, _ = self.space.get()
                    self._mq_clear_retained(pub, pre, "speicher", [x["key"] for x in locs],
                                            ["name", "erreichbar", "aktiv", "frei_gb", "belegt_gb", "gesamt_gb",
                                             "belegt_prozent", "frei_prozent", "pfad"])
                    for l in locs:
                        base = "%s/speicher/%s" % (pre, l["key"])
                        gb = lambda v: round(v / 1073741824.0, 2) if v is not None else ""
                        vals = dict(name=l["name"], erreichbar=1 if l["reachable"] else 0, aktiv=1 if l["active"] else 0,
                                    frei_gb=gb(l["free"]), belegt_gb=gb(l["used"]), gesamt_gb=gb(l["total"]),
                                    belegt_prozent=l["pct"] if l["pct"] is not None else "",
                                    frei_prozent=round(100 - l["pct"], 1) if l["pct"] is not None else "",
                                    pfad=l["path"])
                        for k, v in vals.items():
                            pub(base + "/" + k, str(v))
                        pub(base + "/json", json.dumps(vals, ensure_ascii=False))
                if fail:
                    self.mq_sent = {}               # nach Wiederverbindung alles neu senden
                    backoff = time.time() + 15
                elif full:
                    self.mq_full = now
            except Exception as e:
                if time.time() - getattr(self, "_mq_err", 0) > 300:
                    self._mq_err = time.time()
                    self.log("MQTT-Status: %s" % e)

    def _mq_clear_retained(self, pub, pre, kind, keys, fields):
        """v2.6.0 hat die Werte beim Broker gespeichert (retained). Einmalig je Topic eine leere
        retained-Nachricht senden - damit loescht der Broker den gespeicherten Wert."""
        done = self.__dict__.get("mq_cleared")
        if done is None:
            r = self.store.q("SELECT v FROM meta WHERE k='mqtt_cleared'", one=True)
            done = self.mq_cleared = set(json.loads(r["v"])) if r and r["v"] else set()
        new = False
        for k in keys:
            base = "%s/%s/%s" % (pre, kind, k)
            if base in done:
                continue
            ok = all(pub("%s/%s" % (base, f), "", retain=True) for f in list(fields) + ["json"])
            if ok:
                done.add(base)
                new = True
        if new:
            self.store.x("INSERT OR REPLACE INTO meta(k, v) VALUES('mqtt_cleared', ?)",
                         (json.dumps(sorted(done)),))

    def _recording_done(self, eid, rel, dur, size):
        self.store.x("UPDATE events SET rec=0, clip=?, clip_dur=?, clip_size=? WHERE id=?",
                     (rel, round(dur, 1) if rel else None, size or None, eid))
        if rel and self.cfg.get("mqtt").get("enabled"):
            self._mqtt_async("clip", dict(event=eid, clip="/clips/" + rel, duration=round(dur, 1)))

    def _img_shared(self, rel, det_id=None, event_id=None):
        """Wird das Bild noch von einer anderen Erkennung bzw. einem Ereignis benutzt?"""
        if self.store.q("SELECT 1 FROM detections WHERE (img=? OR crop=?) AND id<>? LIMIT 1",
                        (rel, rel, det_id or -1), one=True):
            return True
        return bool(self.store.q("SELECT 1 FROM events WHERE img=? AND id<>? LIMIT 1", (rel, event_id or -1), one=True))

    def delete_events(self, ids):
        """Ereignisse samt Bild und Video loeschen (ids=None: alle)."""
        if ids is None:
            rows = self.store.q("SELECT * FROM events")
        else:
            ids = [int(i) for i in ids]
            if not ids:
                return 0
            rows = self.store.q("SELECT * FROM events WHERE id IN (%s)" % ",".join(str(i) for i in ids))
        for r in rows:
            if r["rec"]:
                continue                      # laeuft noch
            if r["clip"]:
                self.media.remove("clips", r["clip"])
            if r["img"] and not self._img_shared(r["img"], event_id=r["id"]):
                self.media.remove("snaps", r["img"])
            self.store.x("DELETE FROM events WHERE id=?", (r["id"],))
        return len(rows)

    def events(self, q):
        sql = "SELECT * FROM events WHERE 1=1"
        args = []
        if q.get("stream"):
            sql += " AND stream_id=?"
            args.append(q["stream"])
        if q.get("kind"):
            sql += " AND kind=?"
            args.append(q["kind"])
        if q.get("clip") == "1":
            sql += " AND (clip IS NOT NULL OR rec=1)"
        if q.get("before"):
            sql += " AND ts<?"
            args.append(float(q["before"]))
        if q.get("from"):
            sql += " AND ts>=?"
            args.append(float(q["from"]))
        if q.get("to"):
            sql += " AND ts<=?"
            args.append(float(q["to"]))
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(min(500, int(q.get("limit") or 120)))
        return self.store.q(sql, tuple(args))

    def _publish_live(self, sid, W, H, tracks, now, idle=False):
        names = {}
        oids = [t.obj for t in tracks if t.obj]
        if oids:
            for o in self.store.q("SELECT o.id, o.cls, o.name, o.seq, c.name AS cat, c.color AS ccol FROM objects o "
                                  "LEFT JOIN categories c ON c.id=o.category_id WHERE o.id IN (%s)"
                                  % ",".join(str(i) for i in set(oids))):
                names[o["id"]] = o
        boxes = []
        for t in tracks:
            if t.rejected or not t.confirmed:
                continue                      # unbestaetigte/verworfene Treffer nicht anzeigen
            g = CLASS_GROUP.get(t.cls)
            o = names.get(t.obj)
            label = DE.get(t.cls, t.cls)
            if o:
                label = o["name"] or "%s %d" % (DE.get(o["cls"], o["cls"]), o["seq"] or 0)
            boxes.append(dict(x1=t.box[0] / W, y1=t.box[1] / H, x2=t.box[2] / W, y2=t.box[3] / H, cls=t.cls,
                              label=label, color=g["color"] if g else "#9aa4b2", score=round(t.score, 2),
                              confirmed=t.confirmed, obj=t.obj, named=bool(o and o["name"]),
                              cat=o["cat"] if o else None, face=t.has_face, age=round(now - t.last_ts, 1)))
        self.live[sid] = dict(ts=now, w=W, h=H, boxes=boxes, idle=idle)

    # ------------------------------------------------------------------
    # Korrekturen (Lernen)
    # ------------------------------------------------------------------
    def move_detections(self, det_ids, target):
        """Bilder einem anderen (oder neuen) Objekt zuordnen. Das System lernt daraus:
        die Merkmale werden beim alten Objekt als Gegenbeispiel und beim neuen als feste Beispiele gespeichert."""
        rows = self.store.q("SELECT * FROM detections WHERE id IN (%s)" % ",".join(str(int(i)) for i in det_ids))
        if not rows:
            return None
        now = time.time()
        cls = rows[0]["cls"]
        if target in (None, "", "new"):
            target = self._create_object(cls, now)
        target = int(target)
        tgt = self.store.q("SELECT * FROM objects WHERE id=?", (target,), one=True)
        if not tgt:
            raise ValueError("Zielobjekt existiert nicht")
        ms = int(self.cfg.get("identity").get("max_samples", 40))
        olds = set()
        self.last_move_skipped = 0
        for r in rows:
            old = r["object_id"]
            if old == target:
                continue
            if track_key(r["cls"]) != track_key(tgt["cls"]):
                self.last_move_skipped += 1      # z. B. Auto-Bild in eine Person: nicht zulassen
                continue
            olds.add(old)
            app, face = blob_vec(r["app"]), blob_vec(r["fvec"])
            body = blob_vec(r.get("bvec")) if r.get("bvec") is not None else self._body_for_row(r)
            self.store.x("DELETE FROM samples WHERE det_id=? AND neg=0", (r["id"],))
            if old:
                self.index.add(old, r["cls"], "app", app, r["id"], now, ms, pinned=True, neg=True)
                self.index.add(old, r["cls"], "face", face, r["id"], now, ms, pinned=True, neg=True)
                self.index.add(old, r["cls"], "body", body, r["id"], now, ms, pinned=True, neg=True)
            self.index.add(target, tgt["cls"], "app", app, r["id"], now, ms, pinned=True)
            self.index.add(target, tgt["cls"], "face", face, r["id"], now, ms, pinned=True)
            self.index.add(target, tgt["cls"], "body", body, r["id"], now, ms, pinned=True)
            self.store.x("UPDATE detections SET object_id=?, manual=1 WHERE id=?", (target, r["id"]))
            # laufende Spuren mitnehmen
            for tl in self.tracks.values():
                for t in tl:
                    if r["id"] in t.dets:
                        t.obj = target
                        t.match = "manuell"
        for oid in olds | {target}:
            if oid:
                self.recount(oid)
                self.index.reload_obj(oid)
        for oid in olds:
            if oid:
                self._cleanup_object(oid)
        if self.store.q("SELECT avatar_det FROM objects WHERE id=?", (target,), one=True)["avatar_det"] is None:
            self._set_avatar(target, rows[0]["id"])
        return target

    def purge(self, objects=False, falsepos=False, alarms=False, events=True):
        """Alle Erkennungen und Bilder loeschen. objects: auch Personen/Objekte (Namen, Lernbeispiele);
        sonst bleiben benannte bzw. einsortierte Objekte mit ihrem Gelernten erhalten."""
        n = self.store.q("SELECT COUNT(*) n FROM detections", one=True)["n"]
        for tl in self.tracks.values():
            del tl[:]
        self.live = {}
        keep = set(r["img"] for r in self.store.q("SELECT img FROM events WHERE img IS NOT NULL"))
        for r in self.store.q("SELECT img, crop FROM detections"):
            for k in ("img", "crop"):
                if r[k] and r[k] not in keep:
                    self.media.remove("snaps", r[k])
        if events:
            self.delete_events(None)
        self.store.xmany([("DELETE FROM detections", ()),
                          ("DELETE FROM samples WHERE det_id IS NOT NULL AND pinned=0 AND neg=0 AND object_id IN "
                           "(SELECT id FROM objects WHERE name='' AND category_id IS NULL)", ())])
        if objects:
            self.store.xmany([("DELETE FROM samples", ()), ("DELETE FROM objects", ())])
            shutil.rmtree(self.avatar_dir, ignore_errors=True)
            os.makedirs(self.avatar_dir, exist_ok=True)
            self.store.x("DELETE FROM meta WHERE k LIKE 'seq_%'")
        else:
            for o in self.store.q("SELECT id FROM objects WHERE name='' AND category_id IS NULL"):
                self.delete_object(o["id"], delete_detections=False)
            self.store.x("UPDATE objects SET sightings=0, detections=0, avatar_det=NULL")
        if falsepos:
            self.store.x("DELETE FROM false_pos")
            shutil.rmtree(os.path.join(self.data_dir, "falsepos"), ignore_errors=True)
            self.fp_cache = None
        if alarms:
            self.store.x("DELETE FROM alarm_events")
        self.seen = {}
        self.cooldown = {}
        self.index.load()
        try:
            self.store.x("VACUUM")
        except Exception:
            pass
        self.log("Alle Erkennungen und Bilder gelöscht (%d Einträge)%s" % (n, ", inkl. Objekte" if objects else ""))
        return n

    def create_object(self, cls, name="", category_id=None, notes=""):
        """Objekt/Person von Hand anlegen (danach Erkennungen zuweisen)."""
        if cls not in COCO:
            raise ValueError("Art unbekannt")
        oid = self._create_object(cls, time.time())
        self.store.x("UPDATE objects SET name=?, category_id=?, notes=?, first_seen=NULL, last_seen=NULL WHERE id=?",
                     ((name or "").strip()[:80], category_id, (notes or "")[:500], oid))
        return oid

    def mark_false(self, det_ids):
        """Bilder als Fehlerkennung lernen: Ort + Aussehen merken (je Stream), Bilder loeschen.
        Kuenftige Treffer an derselben Stelle mit aehnlichem Aussehen werden nicht mehr gemeldet."""
        rows = self.store.q("SELECT * FROM detections WHERE id IN (%s)" % ",".join(str(int(i)) for i in det_ids))
        fdir = os.path.join(self.data_dir, "falsepos")
        os.makedirs(fdir, exist_ok=True)
        objs = set()
        now = time.time()
        for r in rows:
            crop_rel = None
            src = self.snap_path(r["crop"]) if r["crop"] else None
            if src and os.path.exists(src):
                crop_rel = "fp_%d.png" % r["id"]
                shutil.copyfile(src, os.path.join(fdir, crop_rel))
            self.store.x("INSERT INTO false_pos(ts, stream_id, stream_name, cls, ckey, x1, y1, x2, y2, app, crop) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (now, r["stream_id"], r["stream_name"], r["cls"], track_key(r["cls"]), r["x1"], r["y1"],
                          r["x2"], r["y2"], r["app"], crop_rel))
            if r["object_id"]:
                objs.add(r["object_id"])
            # laufende Spur sofort ausblenden
            for t in self.tracks.get(r["stream_id"], []):
                if r["id"] in t.dets:
                    t.rejected, t.why, t.confirmed = True, "gelernte Fehlerkennung", False
            self.delete_detection(r["id"], recount=False)
        self.fp_cache = None
        for oid in objs:
            left = self.store.q("SELECT COUNT(*) n FROM detections WHERE object_id=?", (oid,), one=True)["n"]
            o = self.store.q("SELECT name, category_id FROM objects WHERE id=?", (oid,), one=True)
            if left == 0 and o and not o["name"] and not o["category_id"]:
                self.delete_object(oid, delete_detections=False)
            elif o:
                self.recount(oid)
                self.index.reload_obj(oid)
        self.log("%d Bild(er) als Fehlerkennung gelernt" % len(rows))
        return len(rows)

    def merge_objects(self, src, dst):
        src, dst = int(src), int(dst)
        if src == dst:
            return
        self.store.xmany([
            ("UPDATE detections SET object_id=? WHERE object_id=?", (dst, src)),
            ("UPDATE samples SET object_id=? WHERE object_id=? AND neg=0", (dst, src)),
            ("DELETE FROM samples WHERE object_id=?", (src,)),
        ])
        s = self.store.q("SELECT * FROM objects WHERE id=?", (src,), one=True)
        d = self.store.q("SELECT * FROM objects WHERE id=?", (dst,), one=True)
        if s and d:
            self.store.x("UPDATE objects SET sightings=sightings+?, detections=detections+?, "
                         "first_seen=MIN(COALESCE(first_seen,?),?), last_seen=MAX(COALESCE(last_seen,0),?), "
                         "has_face=MAX(has_face,?), name=CASE WHEN name='' THEN ? ELSE name END, "
                         "category_id=COALESCE(category_id, ?) WHERE id=?",
                         (s["sightings"], s["detections"], s["first_seen"] or 0, s["first_seen"] or 0,
                          s["last_seen"] or 0, s["has_face"], s["name"] or "", s["category_id"], dst))
        for tl in self.tracks.values():
            for t in tl:
                if t.obj == src:
                    t.obj = dst
        self.store.x("DELETE FROM objects WHERE id=?", (src,))
        self.index.drop(src)
        self.index.reload_obj(dst)
        try:
            os.remove(os.path.join(self.avatar_dir, "%d.png" % src))
        except OSError:
            pass

    def recount(self, oid):
        r = self.store.q("SELECT COUNT(*) n, SUM(sighting) s, MIN(ts) f, MAX(ts) l, MAX(face) fc "
                         "FROM detections WHERE object_id=?", (oid,), one=True)
        if r and r["n"]:
            self.store.x("UPDATE objects SET detections=?, sightings=MAX(?,1), first_seen=?, last_seen=?, "
                         "has_face=? WHERE id=?", (r["n"], r["s"] or 0, r["f"], r["l"], r["fc"] or 0, oid))
            o = self.store.q("SELECT avatar_det FROM objects WHERE id=?", (oid,), one=True)
            if o and o["avatar_det"]:
                still = self.store.q("SELECT object_id FROM detections WHERE id=?", (o["avatar_det"],), one=True)
                if not still or still["object_id"] != oid:
                    best = self.store.q("SELECT id FROM detections WHERE object_id=? ORDER BY face DESC, ts DESC "
                                        "LIMIT 1", (oid,), one=True)
                    if best:
                        self._set_avatar(oid, best["id"])
        else:
            self.store.x("UPDATE objects SET detections=0, sightings=0 WHERE id=?", (oid,))

    def delete_object(self, oid, delete_detections=True):
        oid = int(oid)
        if delete_detections:
            for r in self.store.q("SELECT id FROM detections WHERE object_id=?", (oid,)):
                self.delete_detection(r["id"], recount=False)
        else:
            self.store.x("UPDATE detections SET object_id=NULL WHERE object_id=?", (oid,))
        self.store.xmany([("DELETE FROM samples WHERE object_id=?", (oid,)),
                          ("DELETE FROM objects WHERE id=?", (oid,))])
        self.index.drop(oid)
        try:
            os.remove(os.path.join(self.avatar_dir, "%d.png" % oid))
        except OSError:
            pass

    def delete_detection(self, det_id, recount=True):
        r = self.store.q("SELECT * FROM detections WHERE id=?", (det_id,), one=True)
        if not r:
            return
        for k in ("img", "crop"):
            if r[k] and not self._img_shared(r[k], det_id):
                self.media.remove("snaps", r[k])
        self.store.x("DELETE FROM detections WHERE id=?", (det_id,))
        self.store.x("DELETE FROM samples WHERE det_id=? AND neg=0 AND pinned=0", (det_id,))
        if recount and r["object_id"]:
            self.recount(r["object_id"])
            self.index.reload_obj(r["object_id"])

    # ------------------------------------------------------------------
    # Alarme
    # ------------------------------------------------------------------
    def _rule_matches(self, rule, o):
        tt, tg = rule["target_type"], str(rule["target"] or "")
        if tt == "object" and str(o["id"]) != tg:
            return False
        if tt == "category" and str(o["category_id"] or "") != tg:
            return False
        if tt == "class":
            if tg.startswith("grp:"):
                g = CLASS_GROUP.get(o["cls"])
                if not g or g["id"] != tg[4:]:
                    return False
            elif o["cls"] != tg:
                return False
        on = int(rule.get("only_named") or 0)
        if on == 1 and not o["name"]:
            return False
        if on == 2 and o["name"]:
            return False
        return True

    def _scope_sql(self, rule):
        tt, tg = rule["target_type"], str(rule["target"] or "")
        where, args = [], []
        if tt == "object":
            where.append("o.id=?")
            args.append(int(tg))
        elif tt == "category":
            where.append("o.category_id=?")
            args.append(int(tg))
        elif tt == "class":
            if tg.startswith("grp:"):
                cl = next((g["classes"] for g in GROUPS if g["id"] == tg[4:]), [])
                where.append("o.cls IN (%s)" % ",".join("?" * len(cl)) if cl else "0")
                args += cl
            else:
                where.append("o.cls=?")
                args.append(tg)
        on = int(rule.get("only_named") or 0)
        if on == 1:
            where.append("o.name<>''")
        elif on == 2:
            where.append("o.name=''")
        return (" AND ".join(where) or "1"), args

    def _check_alarms(self, oid, det_id, now):
        o = self.store.q("SELECT * FROM objects WHERE id=?", (oid,), one=True)
        if not o:
            return
        for rule in self.store.q("SELECT * FROM alarms WHERE enabled=1"):
            try:
                if not self._rule_matches(rule, o):
                    continue
                since = now - float(rule["window_min"] or 30) * 60
                if rule["count_mode"] == "total" and rule["target_type"] != "object":
                    w, a = self._scope_sql(rule)
                    rows = self.store.q("SELECT d.stream_name, d.object_id FROM detections d JOIN objects o "
                                        "ON o.id=d.object_id WHERE d.sighting=1 AND d.ts>=? AND " + w,
                                        [since] + a)
                    ckey = (rule["id"], "total")
                else:
                    rows = self.store.q("SELECT stream_name, object_id FROM detections WHERE object_id=? AND "
                                        "sighting=1 AND ts>=?", (oid, since))
                    ckey = (rule["id"], oid)
                cnt = len(rows)
                streams = sorted({r["stream_name"] for r in rows if r["stream_name"]})
                if cnt < int(rule["min_count"] or 1) or len(streams) < int(rule["min_streams"] or 1):
                    continue
                last = self.cooldown.get(ckey)
                if last is None:
                    ev = self.store.q("SELECT ts FROM alarm_events WHERE alarm_id=? %s ORDER BY ts DESC LIMIT 1"
                                      % ("AND object_id=?" if ckey[1] != "total" else ""),
                                      (rule["id"], oid) if ckey[1] != "total" else (rule["id"],), one=True)
                    last = ev["ts"] if ev else 0
                if now - last < float(rule["cooldown_min"] or 0) * 60:
                    continue
                self.cooldown[ckey] = now
                self._fire(rule, o, cnt, streams, det_id, now)
            except Exception as e:
                self.log("Alarmregel %s: %s" % (rule.get("name"), e))

    def _fire(self, rule, o, cnt, streams, det_id, now):
        label = self.object_label(o["id"])
        if rule["count_mode"] == "total" and rule["target_type"] != "object":
            text = "%s: %d Sichtungen in %s (zuletzt %s)" % (rule["name"], cnt, fmt_dur(rule["window_min"]), label)
        else:
            text = "%s %d× in %s erkannt" % (label, cnt, fmt_dur(rule["window_min"]))
        if streams:
            text += " – " + ", ".join(streams)
        eid = self.store.x("INSERT INTO alarm_events(ts, alarm_id, alarm_name, object_id, object_name, cls, count, "
                           "streams, det_id, text) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (now, rule["id"], rule["name"], o["id"], label, o["cls"], cnt, ", ".join(streams),
                            det_id, text))
        self.alarm_seq += 1
        self.log("ALARM: " + text)
        payload = dict(id=eid, ts=now, time=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                       alarm=rule["name"], object_id=o["id"], object=label, cls=o["cls"], count=cnt,
                       streams=streams, text=text, detection=det_id)
        if rule.get("mqtt") and self.cfg.get("mqtt").get("enabled"):
            self._mqtt_async("alarm", payload)
        hook = (rule.get("webhook") or "").strip()
        if hook:
            threading.Thread(target=self._webhook, args=(hook, payload), daemon=True).start()

    def _webhook(self, url, payload):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            self.log("Webhook fehlgeschlagen: %s" % e)

    def _mqtt_async(self, sub, payload):
        m = self.cfg.get("mqtt")
        topic = "%s/%s" % ((m.get("prefix") or "sentinel").strip("/"), sub)

        def go():
            if not self.mqtt.publish(m, topic, json.dumps(payload, ensure_ascii=False)):
                self.log("MQTT: %s" % self.mqtt.last_error)

        threading.Thread(target=go, daemon=True).start()

    def test_alarm(self, rule_id):
        rule = self.store.q("SELECT * FROM alarms WHERE id=?", (rule_id,), one=True)
        if not rule:
            return False
        payload = dict(test=True, alarm=rule["name"], text="Testalarm '%s'" % rule["name"],
                       time=time.strftime("%Y-%m-%d %H:%M:%S"))
        if rule.get("mqtt") and self.cfg.get("mqtt").get("enabled"):
            self._mqtt_async("alarm", payload)
        if (rule.get("webhook") or "").strip():
            threading.Thread(target=self._webhook, args=(rule["webhook"].strip(), payload), daemon=True).start()
        self.store.x("INSERT INTO alarm_events(ts, alarm_id, alarm_name, object_name, count, streams, text) "
                     "VALUES(?,?,?,?,?,?,?)", (time.time(), rule["id"], rule["name"], "Test", 0, "",
                                               "Testalarm '%s'" % rule["name"]))
        self.alarm_seq += 1
        return True

    # ------------------------------------------------------------------
    # Haushalt: Aufbewahrung, Speicherplatz, Stream-Waechter
    # ------------------------------------------------------------------
    def _housekeeping(self):
        last_clean = 0
        last_loop = 0
        last_upd = time.time() - 6 * 3600 + 120   # erste Update-Pruefung 2 min nach dem Start
        confirmed = False
        while not self.stop_ev.wait(10):
            try:
                now = time.time()
                if self.updater is not None:
                    if not confirmed and now - self.started > 60:
                        confirmed = True
                        self.updater.confirm_running()
                    if self.cfg.get("update").get("auto_check", True) and now - last_upd > 6 * 3600:
                        last_upd = now
                        threading.Thread(target=self.updater.check, daemon=True).start()
                with self.lock:
                    ws = list(self.workers.values())
                for w in ws:
                    if w.status == "live" and w.last_ts and now - w.last_ts > 30:
                        self.log("Stream %s liefert keine Bilder mehr - Neustart" % w.cfg.get("name"))
                        w._kill()
                for sid in list(self.live):
                    if sid not in self.workers:
                        self.live.pop(sid, None)
                self.media.check()
                self._sync_rings()
                if now - last_loop > 30:
                    last_loop = now
                    self.loop_cleanup()
                if self.cfg.get("mqtt").get("enabled"):
                    self.mqtt.ping()
                if now - last_clean > 3600:
                    last_clean = now
                    self.cleanup()
            except Exception as e:
                self.log("Wartung: %s" % e)

    def disk_usage(self):
        return self.media.usage()

    def cleanup(self):
        st = self.cfg.get("storage")
        if self.media.state == "ersatz":
            return 0        # Speicherort fehlt gerade: Bestand nicht anfassen
        days = float(st.get("max_days", 30) or 0)
        removed = 0
        if days > 0:
            cut = time.time() - days * 86400
            for r in self.store.q("SELECT id FROM detections WHERE ts<? ", (cut,)):
                self.delete_detection(r["id"], recount=False)
                removed += 1
        if days > 0:
            old = [r["id"] for r in self.store.q("SELECT id FROM events WHERE ts<?", (cut,))]
            if old:
                self.delete_events(old)
                removed += len(old)
        cap = float(st.get("max_mb", 2000) or 0) * 1024 * 1024
        if st.get("loop"):
            cap = 0                     # Loop-Betrieb: Grenze ist der freie Platz (loop_cleanup)
            self.loop_cleanup()
        if cap > 0:
            used = self.disk_usage()
            while used > cap:
                # aelteste Eintraege zuerst - Erkennungen und Ereignisse (Videos) gemeinsam
                d1 = self.store.q("SELECT id, ts FROM detections ORDER BY ts LIMIT 100")
                e1 = self.store.q("SELECT id, ts FROM events ORDER BY ts LIMIT 20")
                if not d1 and not e1:
                    break
                if e1 and (not d1 or e1[0]["ts"] <= d1[-1]["ts"]):
                    self.delete_events([r["id"] for r in e1])
                    removed += len(e1)
                else:
                    for r in d1:
                        self.delete_detection(r["id"], recount=False)
                        removed += 1
                used = self.disk_usage()
        # leere Tagesordner entfernen
        for root in self.media.roots():
            for kind in ("snaps", "clips"):
                base = os.path.join(root, kind)
                for d in (os.listdir(base) if os.path.isdir(base) else []):
                    p = os.path.join(base, d)
                    if os.path.isdir(p) and not os.listdir(p) and d != time.strftime("%Y-%m-%d"):
                        try:
                            os.rmdir(p)
                        except OSError:
                            pass
        cut_ev = time.time() - 90 * 86400
        self.store.x("DELETE FROM alarm_events WHERE ts<?", (cut_ev,))
        if removed:
            self.log("Aufräumen: %d alte Bilder/Ereignisse entfernt" % removed)
            self.store.x("UPDATE objects SET avatar_det=NULL WHERE avatar_det NOT IN (SELECT id FROM detections)")

    # ------------------------------------------------------------------
    def status(self):
        with self.lock:
            ws = [w.info() for w in self.workers.values()]
        if self.build_pause:
            ws = [dict(id=s["id"], name=s.get("name", s["id"]), status="pausiert", fps=0, size=None, restarts=0,
                       decoder="", transport="", online=False,
                       error="angehalten, solange die GPU-Engine gebaut wird (einmalig, einige Minuten)")
                  for s in self.cfg.get("streams") if s.get("enabled", True)]
        for x in ws:
            x["recording"] = self.recorder.busy(x["id"])
            x["prebuf"] = self.recorder.ring_info(x["id"])
            x["analysis"] = stream_mode(self.stream_cfg(x["id"]))
        dm = list(self.det_ms)
        fm = list(self.face_ms)
        load = None
        try:
            load = open("/proc/loadavg").read().split()[:3]
        except Exception:
            pass
        unack = self.store.q("SELECT COUNT(*) n FROM alarm_events WHERE ack=0", one=True)["n"]
        jet = sysinfo.is_jetson()
        return dict(version=self.version, streams=ws, models=self.models.info(), models_ready=self.det is not None,
                    platform=sysinfo.platform_name(), jetson=jet, l4t=sysinfo.l4t_version() if jet else "",
                    gpu_available=bool(jet and trt.available()), trt_version=trt.trt_version() if jet else "",
                    engines=self.models.engines_info() if jet else [], building=self.models.building,
                    decoder=decoder_info(), sys=sysinfo.system_stats(),
                    update=self.updater.public() if self.updater else None,
                    fan=self.fan.status() if self.fan else dict(available=False), face_gpu=bool(self.faces and self.faces.gpu),
                    backend=self.det.backend if self.det else "", model_size=self.det.size if self.det else 0,
                    model_title=getattr(self.det, "model_title", "") if self.det else "",
                    model_choice=self.detector_choice(), filter_stats=dict(self.stats),
                    false_count=len(self.false_positives()),
                    det_ms=round(sum(dm) / len(dm)) if dm else None,
                    det_passes=round(sum(self.det_passes) / float(len(self.det_passes)), 1) if self.det_passes else None,
                    tile_mode=self._tile_mode(self.cfg.get("detection")),
                    face_ms=round(sum(fm) / len(fm)) if fm else None, ffmpeg=bool(find_ffmpeg()),
                    error=self.engine_error, load=load, unack=unack, alarm_seq=self.alarm_seq,
                    uptime=int(time.time() - self.started), time=time.time(),
                    time_text=time.strftime("%d.%m.%Y %H:%M:%S"))

    def power(self, action):
        """Geraet herunterfahren bzw. neu starten - vorher Aufnahmen sauber abschliessen."""
        time.sleep(1.0)                       # Antwort an den Browser zuerst rausgeben
        try:
            self.recorder.stop_all()
        except Exception:
            pass
        try:
            self.store.x("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        subprocess.call(["sync"])
        cmds = ([["systemctl", "poweroff"], ["shutdown", "-h", "now"], ["poweroff"]] if action == "shutdown"
                else [["systemctl", "reboot"], ["shutdown", "-r", "now"], ["reboot"]])
        for c in cmds:
            try:
                if subprocess.call(c) == 0:
                    return
            except OSError:
                continue
        self.log("%s nicht möglich (fehlende Rechte?)" % ("Herunterfahren" if action == "shutdown" else "Neustart"))

    def shutdown(self):
        self.stop_ev.set()
        try:
            self.recorder.stop_all()     # laufende Aufnahmen sauber abschliessen
        except Exception:
            pass
        if self.fan is not None:
            self.fan.stop()          # Regelung an den Jetson zurueckgeben
        for w in list(self.workers.values()):
            w.stop()
        self.store.close()
