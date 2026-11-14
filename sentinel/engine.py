"""Kern von Sentinel MK1: Streams, Erkennung, Verfolgung, Wiedererkennung, Galerie, Alarme."""
import collections
import json
import os
import shutil
import threading
import time
import traceback
import urllib.request

import numpy as np

from . import imaging, sysinfo, trt
from .detector import CLASS_GROUP, COCO, DE, GROUPS, SAME_TRACK, FaceEngine, NanoDet, YoloX
from .models import DETECTORS, ModelManager
from .mqtt import MqttSender
from .store import Store, blob_vec, vec_blob
from .streams import StreamWorker, decoder_info, find_ffmpeg


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
                 "retry_at", "scores", "why")

    def __init__(self, tid, cls, box, score, ts):
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


class IdentityIndex:
    """Lernbeispiele (positiv/negativ) aller Objekte im Speicher, fuer schnelle Zuordnung."""

    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.objs = {}      # oid -> dict(cls, last_seen, app=[], face=[], app_neg=[], face_neg=[])
        self.load()

    def load(self):
        with self.lock:
            self.objs = {}
            for o in self.store.q("SELECT id, cls, last_seen FROM objects"):
                self.objs[o["id"]] = dict(cls=o["cls"], last_seen=o["last_seen"] or 0,
                                          app=[], face=[], app_neg=[], face_neg=[])
            for s in self.store.q("SELECT object_id, kind, neg, vec FROM samples ORDER BY id"):
                o = self.objs.get(s["object_id"])
                v = blob_vec(s["vec"])
                if o is None or v is None:
                    continue
                o[s["kind"] + ("_neg" if s["neg"] else "")].append(v)

    def ensure(self, oid, cls, last_seen=0):
        with self.lock:
            if oid not in self.objs:
                self.objs[oid] = dict(cls=cls, last_seen=last_seen, app=[], face=[], app_neg=[], face_neg=[])
            return self.objs[oid]

    def drop(self, oid):
        with self.lock:
            self.objs.pop(oid, None)

    @staticmethod
    def _best(vecs, v):
        if not vecs or v is None:
            return -1.0
        return float(np.max(np.stack(vecs) @ v))

    def match(self, cls, app, face, now, cfg):
        """Rueckgabe (oid, art, aehnlichkeit) oder (None, '', bester Wert)."""
        key = track_key(cls)
        face_thr = float(cfg.get("face_threshold", 0.40))
        app_thr = float(cfg.get("appearance_threshold", 0.90))
        win = float(cfg.get("appearance_window_h" if cls == "person" else "appearance_window_other_h", 12)) * 3600
        best = (None, "", -1.0)
        with self.lock:
            items = [(oid, o) for oid, o in self.objs.items() if track_key(o["cls"]) == key]
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
            if app is None:
                return best
            for oid, o in items:
                if not o["app"]:
                    continue
                if now - (o["last_seen"] or 0) > win:
                    continue
                # Gesicht bekannt und eindeutig anders -> nicht ueber Kleidung zuordnen
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
            lst.append(np.asarray(vec, np.float32))
            self.store.x("INSERT INTO samples(object_id, kind, neg, vec, det_id, pinned, ts) VALUES(?,?,?,?,?,?,?)",
                         (oid, kind, 1 if neg else 0, vec_blob(vec), det_id, 1 if pinned else 0, now))
            if not neg and len(lst) > max_samples:
                rows = self.store.q("SELECT id FROM samples WHERE object_id=? AND kind=? AND neg=0 AND pinned=0 "
                                    "ORDER BY id LIMIT ?", (oid, kind, len(lst) - max_samples))
                if rows:
                    self.store.x("DELETE FROM samples WHERE id IN (%s)" % ",".join(str(r["id"]) for r in rows))
                    self.reload_obj(oid)

    def reload_obj(self, oid):
        with self.lock:
            o = self.objs.get(oid)
            if o is None:
                return
            for k in ("app", "face", "app_neg", "face_neg"):
                o[k] = []
            for s in self.store.q("SELECT kind, neg, vec FROM samples WHERE object_id=? ORDER BY id", (oid,)):
                v = blob_vec(s["vec"])
                if v is not None:
                    o[s["kind"] + ("_neg" if s["neg"] else "")].append(v)

    def touch(self, oid, ts):
        with self.lock:
            if oid in self.objs:
                self.objs[oid]["last_seen"] = ts

    def set_cls(self, oid, cls):
        with self.lock:
            if oid in self.objs:
                self.objs[oid]["cls"] = cls


class Engine:
    def __init__(self, base_dir, config, version):
        self.base = base_dir
        self.cfg = config
        self.version = version
        self.data_dir = os.path.join(base_dir, "data")
        self.snap_dir = os.path.join(self.data_dir, "snaps")
        self.avatar_dir = os.path.join(self.data_dir, "avatars")
        for d in (self.data_dir, self.snap_dir, self.avatar_dir):
            os.makedirs(d, exist_ok=True)
        self.logbuf = collections.deque(maxlen=300)
        self.store = Store(os.path.join(self.data_dir, "sentinel.db"))
        self.index = IdentityIndex(self.store)
        self.models = ModelManager(os.path.join(self.data_dir, "models"), self.log, wanted=self.wanted_models)
        self.fp_cache = None        # gelernte Fehlerkennungen (Liste), None = neu laden
        self.stats = dict(rejected_fp=0, rejected_verify=0, rejected_shape=0)
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
        self.det_ms = collections.deque(maxlen=30)
        self.face_ms = collections.deque(maxlen=30)
        self.engine_error = ""
        self.cooldown = {}
        self.seen = {}              # (stream_id, object_id) -> zuletzt gesehen
        self.stop_ev = threading.Event()
        self.alarm_seq = 0
        self.lock = threading.RLock()
        self.started = time.time()
        self.updater = None          # wird von app.py gesetzt
        self.fan = None              # Luefterregelung (nur Jetson)

    # ------------------------------------------------------------------
    def log(self, msg):
        line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        self.logbuf.append(line)
        print(line, flush=True)

    def start(self):
        self.log("Sentinel MK1 v%s startet (Daten: %s)" % (self.version, self.data_dir))
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

    def detector_choice(self):
        """Gewaehltes Erkennungsmodell; 'auto' = YOLOX-S auf der GPU, ohne GPU YOLOX-Tiny."""
        m = self.cfg.get("detection").get("model", "auto")
        if m not in DETECTORS:
            m = "yolox_s" if self.use_gpu() else "yolox_tiny"
        return m

    def wanted_models(self):
        return [DETECTORS[self.detector_choice()]["key"], "face_det", "face_rec"]

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
                if gpu and self.models.all_ok() and any(not self.models.engine_ok(k) and
                                                        self.models.engine_state.get(k) != "fehler"
                                                        for k in self.models.ENGINES if k in self.wanted_models()):
                    # Engines fehlen: einmalig bauen, bis dahin auf der CPU rechnen
                    self.models.build_engines(on_done=self.load_models)
                choice = self.detector_choice()
                spec = DETECTORS[choice]
                key = spec["key"]
                if self.models.state.get(key) == "ok":
                    t = time.time()
                    det = None
                    eng = self.models.engine_path(key) if gpu and self.models.engine_ok(key) else None
                    if gpu and not eng and key != "detector" and self.models.engine_ok("detector") \
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
                self.engine_error = "" if self.det else "Modelle fehlen"
            except Exception as e:
                self.engine_error = "Modelle konnten nicht geladen werden: %s" % e
                self.log(self.engine_error)
                traceback.print_exc()

    def reload_detector(self):
        self.load_models()

    # ------------------------------------------------------------------
    # Streams
    # ------------------------------------------------------------------
    def sync_streams(self):
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
                if not d.get("enabled", True) or self.det is None:
                    time.sleep(1)
                    continue
                with self.lock:
                    ws = list(self.workers.values())
                if not ws:
                    time.sleep(0.5)
                    continue
                did = False
                for k in range(len(ws)):
                    w = ws[(rr + k) % len(ws)]
                    if not self.stream_cfg(w.id).get("detect", True):
                        continue
                    raw = w.latest_raw()
                    if raw is None or time.time() - raw[1] > 10:
                        continue
                    lp = self.last_proc.get(w.id)
                    if lp and (lp[0] == raw[0] or time.time() - lp[1] < float(d.get("interval_s", 1.0))):
                        continue
                    rr = (rr + k + 1) % len(ws)
                    self.last_proc[w.id] = (raw[0], time.time())
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
        # Bewegungsfilter: ruhige Szene -> keine Analyse (vorhandene Objekte bleiben bestehen)
        gray = imaging.gray_small(frame)
        if d.get("motion_gate", True):
            prev = self.last_gray.get(w.id)
            full_age = now - self.last_full.get(w.id, 0)
            pending = any(not t.confirmed for t in tracks)
            if prev is not None and full_age < 30 and not pending:
                diff = float(np.abs(gray - prev).mean())
                if diff < float(d.get("motion_level", 2.5)):
                    for t in tracks:
                        t.last_ts = now
                    self._publish_live(w.id, W, H, tracks, now, idle=True)
                    return
        self.last_gray[w.id] = gray
        self.last_full[w.id] = now
        t0 = time.time()
        thr = self._thresholds(d)
        with self.det_lock:
            dets = self.det.detect(frame, min(thr.values()), allowed=self._allowed()) if self.det else []
        self.det_ms.append((time.time() - t0) * 1000)
        dets = self._plausible(dets, thr, d, W, H)
        self._update_tracks(w, tracks, dets, frame, idx, now, d)
        jpg = w.jpeg_for(idx)
        if jpg:
            self.analyzed[w.id] = (now, jpg)
        self._publish_live(w.id, W, H, tracks, now)

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
        return app, face, lm_abs

    # ------------------------------------------------------------------
    # Sichtungen
    # ------------------------------------------------------------------
    def _new_sighting(self, w, t, frame, idx, now):
        icfg = self.cfg.get("identity")
        app, face, _ = self._features(frame, t)
        t.face_tries = 1
        t.face_last = now
        oid, how, sim = self.index.match(t.cls, app, face, now, icfg)
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
            det_id = self._save_detection(w, t, frame, idx, now, app, face, sighting=0, match=how, sim=sim)
            self._after_detection(oid, det_id, now, sighting=False, has_face=face is not None)
            return
        self.seen[(w.id, oid)] = now
        det_id = self._save_detection(w, t, frame, idx, now, app, face, sighting=1, match=how, sim=sim)
        ms = int(icfg.get("max_samples", 40))
        self.index.add(oid, t.cls, "app", app, det_id, now, ms)
        if face is not None:
            self.index.add(oid, t.cls, "face", face, det_id, now, ms)
        self._after_detection(oid, det_id, now, sighting=True, has_face=face is not None)

    def _follow_up(self, w, t, frame, idx, now, d):
        icfg = self.cfg.get("identity")
        # Personen ohne Gesicht: weiter nach einem Gesicht suchen (max. 8 Versuche)
        if t.cls == "person" and not t.has_face and t.face_tries < 8 and now - t.face_last >= 1.5 \
                and self.faces is not None and icfg.get("faces", True):
            t.face_tries += 1
            t.face_last = now
            app, face, _ = self._features(frame, t)
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
                                              sim=sim if oid == t.obj else 0)
                ms = int(icfg.get("max_samples", 40))
                self.index.add(t.obj, t.cls, "face", face, det_id, now, ms)
                self.index.add(t.obj, t.cls, "app", app, det_id, now, ms)
                self._after_detection(t.obj, det_id, now, sighting=False, has_face=True)
                return
        if now - t.last_save >= float(d.get("resnap_s", 120)) > 0:
            app, face, _ = self._features(frame, t, want_face=False)
            det_id = self._save_detection(w, t, frame, idx, now, app, None, sighting=0, match=t.match, sim=0)
            self._after_detection(t.obj, det_id, now, sighting=False, has_face=False)

    def _create_object(self, cls, now):
        n = self.store.next_seq(cls)
        oid = self.store.x("INSERT INTO objects(cls, name, created, first_seen, last_seen, seq) VALUES(?,?,?,?,?,?)",
                           (cls, "", now, now, now, n))
        self.index.ensure(oid, cls, now)
        return oid

    def _save_detection(self, w, t, frame, idx, now, app, face, sighting, match, sim):
        H, W = frame.shape[:2]
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        folder = os.path.join(self.snap_dir, day)
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%H%M%S", time.localtime(now)) + "_%03d" % int((now % 1) * 1000)
        base = "%s_%s_%s" % (stamp, w.id, t.cls.replace(" ", "-"))
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
            "sighting, face, match, sim, img, crop, app, fvec) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, w.id, w.cfg.get("name", w.id), t.cls, t.score, x1 / W, y1 / H, x2 / W, y2 / H, t.obj, t.tid,
             sighting, 1 if face is not None else 0, match, float(sim), img_rel, crop_rel, vec_blob(app),
             vec_blob(face)))
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
            src = os.path.join(self.snap_dir, r["crop"])
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(self.avatar_dir, "%d.png" % oid))
                self.store.x("UPDATE objects SET avatar_det=? WHERE id=?", (det_id, oid))

    def _reassign_track(self, t, new_oid):
        old = t.obj
        if t.dets:
            ids = ",".join(str(i) for i in t.dets)
            rows = self.store.q("SELECT id, sighting FROM detections WHERE id IN (%s)" % ids)
            self.store.x("UPDATE detections SET object_id=? WHERE id IN (%s)" % ids, (new_oid,))
            self.store.x("UPDATE samples SET object_id=? WHERE det_id IN (%s) AND object_id=? AND neg=0" % ids,
                         (new_oid, old))
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
        for r in rows:
            old = r["object_id"]
            if old == target:
                continue
            olds.add(old)
            app, face = blob_vec(r["app"]), blob_vec(r["fvec"])
            self.store.x("DELETE FROM samples WHERE det_id=? AND neg=0", (r["id"],))
            if old:
                self.index.add(old, r["cls"], "app", app, r["id"], now, ms, pinned=True, neg=True)
                self.index.add(old, r["cls"], "face", face, r["id"], now, ms, pinned=True, neg=True)
            self.index.add(target, tgt["cls"], "app", app, r["id"], now, ms, pinned=True)
            self.index.add(target, tgt["cls"], "face", face, r["id"], now, ms, pinned=True)
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
            if r["crop"] and os.path.exists(os.path.join(self.snap_dir, r["crop"])):
                crop_rel = "fp_%d.png" % r["id"]
                shutil.copyfile(os.path.join(self.snap_dir, r["crop"]), os.path.join(fdir, crop_rel))
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
            if r[k]:
                try:
                    os.remove(os.path.join(self.snap_dir, r[k]))
                except OSError:
                    pass
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
                if self.cfg.get("mqtt").get("enabled"):
                    self.mqtt.ping()
                if now - last_clean > 3600:
                    last_clean = now
                    self.cleanup()
            except Exception as e:
                self.log("Wartung: %s" % e)

    def disk_usage(self):
        total = 0
        for root, _, files in os.walk(self.snap_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    def cleanup(self):
        st = self.cfg.get("storage")
        days = float(st.get("max_days", 30) or 0)
        removed = 0
        if days > 0:
            cut = time.time() - days * 86400
            for r in self.store.q("SELECT id FROM detections WHERE ts<? ", (cut,)):
                self.delete_detection(r["id"], recount=False)
                removed += 1
        cap = float(st.get("max_mb", 2000) or 0) * 1024 * 1024
        if cap > 0:
            used = self.disk_usage()
            while used > cap:
                rows = self.store.q("SELECT id FROM detections ORDER BY ts LIMIT 200")
                if not rows:
                    break
                for r in rows:
                    self.delete_detection(r["id"], recount=False)
                    removed += 1
                used = self.disk_usage()
        # leere Tagesordner entfernen
        for d in os.listdir(self.snap_dir):
            p = os.path.join(self.snap_dir, d)
            if os.path.isdir(p) and not os.listdir(p):
                try:
                    os.rmdir(p)
                except OSError:
                    pass
        cut_ev = time.time() - 90 * 86400
        self.store.x("DELETE FROM alarm_events WHERE ts<?", (cut_ev,))
        if removed:
            self.log("Aufräumen: %d alte Bilder entfernt" % removed)
            self.store.x("UPDATE objects SET avatar_det=NULL WHERE avatar_det NOT IN (SELECT id FROM detections)")

    # ------------------------------------------------------------------
    def status(self):
        with self.lock:
            ws = [w.info() for w in self.workers.values()]
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
                    face_ms=round(sum(fm) / len(fm)) if fm else None, ffmpeg=bool(find_ffmpeg()),
                    error=self.engine_error, load=load, unack=unack, alarm_seq=self.alarm_seq,
                    uptime=int(time.time() - self.started), time=time.time(),
                    time_text=time.strftime("%d.%m.%Y %H:%M:%S"))

    def shutdown(self):
        self.stop_ev.set()
        if self.fan is not None:
            self.fan.stop()          # Regelung an den Jetson zurueckgeben
        for w in list(self.workers.values()):
            w.stop()
        self.store.close()
