"""Objekterkennung (NanoDet-Plus, COCO), Gesichtssuche (YuNet) und Gesichtsmerkmale (SFace).

Alle Netze laufen mit onnxlite (reines numpy). Ist OpenCV installiert (z. B. beim
Testen am PC), kann die Objekterkennung optional ueber cv2.dnn laufen.
"""
import math

import numpy as np

from . import imaging
from .onnxlite import OnnxModel

# COCO-Klassen (Reihenfolge des Modells) mit deutschen Namen
COCO = ["person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
        "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
        "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
        "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
        "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
        "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
        "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant", "bed",
        "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
        "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
        "hair drier", "toothbrush"]

DE = {"person": "Person", "bicycle": "Fahrrad", "car": "Auto", "motorcycle": "Motorrad",
      "airplane": "Flugzeug", "bus": "Bus", "train": "Zug", "truck": "LKW", "boat": "Boot",
      "traffic light": "Ampel", "fire hydrant": "Hydrant", "stop sign": "Stoppschild",
      "parking meter": "Parkuhr", "bench": "Bank", "bird": "Vogel", "cat": "Katze", "dog": "Hund",
      "horse": "Pferd", "sheep": "Schaf", "cow": "Kuh", "elephant": "Elefant", "bear": "Bär",
      "zebra": "Zebra", "giraffe": "Giraffe", "backpack": "Rucksack", "umbrella": "Schirm",
      "handbag": "Handtasche", "tie": "Krawatte", "suitcase": "Koffer", "frisbee": "Frisbee",
      "skis": "Ski", "snowboard": "Snowboard", "sports ball": "Ball", "kite": "Drachen",
      "baseball bat": "Baseballschläger", "baseball glove": "Baseballhandschuh",
      "skateboard": "Skateboard", "surfboard": "Surfbrett", "tennis racket": "Tennisschläger",
      "bottle": "Flasche", "wine glass": "Weinglas", "cup": "Tasse", "fork": "Gabel", "knife": "Messer",
      "spoon": "Löffel", "bowl": "Schüssel", "banana": "Banane", "apple": "Apfel",
      "sandwich": "Sandwich", "orange": "Orange", "broccoli": "Brokkoli", "carrot": "Karotte",
      "hot dog": "Hotdog", "pizza": "Pizza", "donut": "Donut", "cake": "Kuchen", "chair": "Stuhl",
      "couch": "Sofa", "potted plant": "Topfpflanze", "bed": "Bett", "dining table": "Tisch",
      "toilet": "Toilette", "tv": "Fernseher", "laptop": "Laptop", "mouse": "Maus",
      "remote": "Fernbedienung", "keyboard": "Tastatur", "cell phone": "Handy",
      "microwave": "Mikrowelle", "oven": "Ofen", "toaster": "Toaster", "sink": "Spüle",
      "refrigerator": "Kühlschrank", "book": "Buch", "clock": "Uhr", "vase": "Vase",
      "scissors": "Schere", "teddy bear": "Teddybär", "hair drier": "Föhn", "toothbrush": "Zahnbürste"}

# Gruppen mit Rahmenfarbe (Rot = Menschen, Blau = Tiere, Gelb = Fahrzeuge, ...)
GROUPS = [
    dict(id="person", name="Personen", color="#ff4d4d", classes=["person"], default=True),
    dict(id="animal", name="Tiere", color="#4aa3ff",
         classes=["bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"],
         default=True),
    dict(id="vehicle", name="Fahrzeuge", color="#ffd23f", classes=["car", "truck", "bus", "motorcycle"],
         default=True),
    dict(id="bike", name="Fahrräder", color="#3ddc97", classes=["bicycle"], default=True),
    dict(id="transport", name="Weitere Verkehrsmittel", color="#ff9142",
         classes=["airplane", "train", "boat"], default=False),
    dict(id="luggage", name="Taschen & Gepäck", color="#c084fc",
         classes=["backpack", "handbag", "suitcase", "umbrella"], default=False),
]
_grouped = {c for g in GROUPS for c in g["classes"]}
GROUPS.append(dict(id="other", name="Sonstige Gegenstände", color="#9aa4b2",
                   classes=[c for c in COCO if c not in _grouped], default=False))
CLASS_GROUP = {c: g for g in GROUPS for c in g["classes"]}

# Klassen, die beim Verfolgen als gleich gelten (Auto/LKW/Bus springt oft hin und her)
SAME_TRACK = {"truck": "car", "bus": "car", "car": "car"}


def nms(boxes, scores, thr):
    order = scores.argsort()[::-1]
    keep = []
    x1, y1, x2, y2 = boxes.T
    area = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    while order.size:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (area[i] + area[order[1:]] - inter + 1e-6)
        order = order[1:][iou <= thr]
    return np.array(keep, dtype=np.int64)


class NanoDet:
    """NanoDet-Plus-m (COCO, 80 Klassen). size 416 = genauer, 320 = ca. 40 % schneller."""

    MEAN = np.array([103.53, 116.28, 123.675], np.float32)
    STD = np.array([57.375, 57.12, 58.395], np.float32)

    def __init__(self, path, size=416, use_cv2=False, engine=None):
        self.size = int(size)
        self.strides = (8, 16, 32)
        self.reg_max = 7
        self.cv2net = None
        self.net = None
        self.gpu = False
        if engine:
            # TensorRT (Jetson-GPU): Engine ist fest auf 416 px gebaut
            from .trt import TrtModel
            self.net = TrtModel(engine)
            self.size = 416
            self.gpu = True
        elif use_cv2 and self.size == 416:
            try:
                import cv2
                major, minor = [int(x) for x in cv2.__version__.split(".")[:2]]
                if (major, minor) >= (4, 5):  # aeltere DNN-Module koennen das Modell nicht sicher laden
                    self.cv2net = cv2.dnn.readNet(path)
            except Exception:
                self.cv2net = None
        if self.net is None and self.cv2net is None:
            smap = {416 // s: self.size // s for s in (8, 16, 32)}
            self.net = OnnxModel(path, spatial_map=smap)
        self.anchors = []
        for s in self.strides:
            n = self.size // s
            yv, xv = np.mgrid[0:n, 0:n]
            self.anchors.append(np.stack([xv.ravel() * s + 0.5 * (s - 1), yv.ravel() * s + 0.5 * (s - 1)], 1)
                                .astype(np.float32))
        self.project = np.arange(self.reg_max + 1, dtype=np.float32)

    @property
    def backend(self):
        if self.gpu:
            return "TensorRT (GPU, FP16)"
        return "OpenCV-DNN" if self.cv2net is not None else "numpy (onnxlite, CPU)"

    def detect(self, rgb, thr=0.45, iou=0.5, allowed=None):
        """rgb: (H,W,3) uint8. Rueckgabe: Liste (x1,y1,x2,y2,score,klasse) in Pixeln."""
        H, W = rgb.shape[:2]
        S = self.size
        sc = min(S / W, S / H)
        nw, nh = max(1, int(round(W * sc))), max(1, int(round(H * sc)))
        img = imaging.resize_nn(rgb, nw, nh)[:, :, ::-1].astype(np.float32)  # BGR
        canvas = np.zeros((S, S, 3), np.float32)
        canvas[:] = self.MEAN  # nach Normierung 0
        canvas[:nh, :nw] = img
        x = ((canvas - self.MEAN) / self.STD).transpose(2, 0, 1)[None].astype(np.float32)
        if self.cv2net is not None:
            self.cv2net.setInput(x)
            outs = self.cv2net.forward(self.cv2net.getUnconnectedOutLayersNames())
        else:
            outs = self.net.run({self.net.input_names[0]: x})
        # Ausgaben nach Form sortieren (Klassen: 80 Werte, Abstaende: 32 Werte; groesste Ebene zuerst)
        outs = [np.asarray(o).reshape(-1, np.asarray(o).shape[-1]) for o in outs]
        cls_l = sorted([o for o in outs if o.shape[1] == 80], key=lambda o: -o.shape[0])
        reg_l = sorted([o for o in outs if o.shape[1] == 32], key=lambda o: -o.shape[0])
        boxes, scores, labels = [], [], []
        for s, cl, rg, anc in zip(self.strides, cls_l, reg_l, self.anchors):
            cl = cl.reshape(-1, 80)
            best = cl.max(axis=1)
            m = best >= thr
            if not m.any():
                continue
            lab = cl[m].argmax(axis=1)
            if allowed is not None:
                ok = np.isin(lab, allowed)
                if not ok.any():
                    continue
                idx = np.nonzero(m)[0][ok]
                lab = lab[ok]
            else:
                idx = np.nonzero(m)[0]
            r = rg.reshape(-1, 4, self.reg_max + 1)[idx]
            e = np.exp(r - r.max(axis=2, keepdims=True))
            d = (e / e.sum(axis=2, keepdims=True)) @ self.project * s
            a = anc[idx]
            b = np.stack([a[:, 0] - d[:, 0], a[:, 1] - d[:, 1], a[:, 0] + d[:, 2], a[:, 1] + d[:, 3]], 1)
            boxes.append(b)
            scores.append(cl[idx, lab])
            labels.append(lab)
        if not boxes:
            return []
        boxes = np.concatenate(boxes)
        scores = np.concatenate(scores)
        labels = np.concatenate(labels)
        # klassenweise NMS (Versatz je Klasse)
        off = labels[:, None].astype(np.float32) * (S * 2)
        keep = nms(boxes + off, scores, iou)
        res = []
        for i in keep:
            x1, y1, x2, y2 = boxes[i] / sc
            x1, y1 = max(0.0, x1), max(0.0, y1)
            x2, y2 = min(float(W), x2), min(float(H), y2)
            if x2 - x1 < 4 or y2 - y1 < 4:
                continue
            res.append((float(x1), float(y1), float(x2), float(y2), float(scores[i]), int(labels[i])))
        return res


def _resize_fast(rgb, w, h):
    """Bilinear mit OpenCV (falls vorhanden), sonst numpy."""
    try:
        import cv2
        return cv2.resize(rgb, (w, h), interpolation=cv2.INTER_LINEAR)
    except Exception:
        return imaging.resize_bilinear(rgb, w, h)


class YoloX:
    """YOLOX-S (640) / YOLOX-Tiny (416), COCO 80 Klassen, Apache-2.0.
    Eingang: BGR 0..255 (ohne Normierung), Letterbox oben links mit Grau 114."""

    def __init__(self, path, size, engine=None, title="YOLOX"):
        self.size = int(size)
        self.title = title
        self.gpu = False
        if engine:
            from .trt import TrtModel
            self.net = TrtModel(engine)
            self.gpu = True
        else:
            self.net = OnnxModel(path)
        grids, strides = [], []
        for st in (8, 16, 32):
            n = self.size // st
            yv, xv = np.mgrid[0:n, 0:n]
            grids.append(np.stack([xv.ravel(), yv.ravel()], 1))
            strides.append(np.full((n * n, 1), st))
        self.grid = np.concatenate(grids).astype(np.float32)
        self.stride = np.concatenate(strides).astype(np.float32)

    @property
    def backend(self):
        return "TensorRT (GPU, FP16)" if self.gpu else "numpy (onnxlite, CPU)"

    def infer(self, rgb):
        """Rohvorhersage (N, 6): x1, y1, x2, y2, Wert, Klasse - in Pixeln des Eingangsbildes, ohne Schwelle."""
        H, W = rgb.shape[:2]
        S = self.size
        r = min(S / float(W), S / float(H))
        nw, nh = max(1, int(W * r)), max(1, int(H * r))
        img = _resize_fast(rgb, nw, nh)
        canvas = np.full((S, S, 3), 114, np.float32)
        canvas[:nh, :nw] = img[:, :, ::-1]        # BGR
        x = canvas.transpose(2, 0, 1)[None]
        out = self.net.run({self.net.input_names[0]: x})[0]
        o = np.asarray(out, np.float32).reshape(-1, 85)
        xy = (o[:, :2] + self.grid) * self.stride
        wh = np.exp(np.minimum(o[:, 2:4], 10.0)) * self.stride
        sc = o[:, 4:5] * o[:, 5:]
        cls = sc.argmax(1)
        best = sc[np.arange(sc.shape[0]), cls]
        boxes = np.concatenate([xy - wh / 2, xy + wh / 2], 1) / r
        return boxes, best, cls

    def detect(self, rgb, thr=0.45, iou=0.45, allowed=None):
        H, W = rgb.shape[:2]
        boxes, best, cls = self.infer(rgb)
        m = best >= thr
        if allowed is not None:
            m &= np.isin(cls, allowed)
        if not m.any():
            return []
        boxes, best, cls = boxes[m], best[m], cls[m]
        keep = nms(boxes + cls[:, None].astype(np.float32) * (max(W, H) * 2.0), best, iou)
        res = []
        for i in keep:
            x1, y1, x2, y2 = boxes[i]
            x1, y1 = max(0.0, float(x1)), max(0.0, float(y1))
            x2, y2 = min(float(W), float(x2)), min(float(H), float(y2))
            if x2 - x1 < 4 or y2 - y1 < 4:
                continue
            res.append((x1, y1, x2, y2, float(best[i]), int(cls[i])))
        return res


class FaceEngine:
    """YuNet (Gesicht + 5 Punkte) und SFace (128-dim. Merkmal, Kosinus-Aehnlichkeit)."""

    TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                         [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)

    def __init__(self, yunet_path, sface_path, sface_engine=None):
        self.det = OnnxModel(yunet_path)
        self.rec = None
        self.gpu = False
        if sface_engine:
            from .trt import TrtModel
            self.rec = TrtModel(sface_engine)
            self.gpu = True
        elif sface_path:
            self.rec = OnnxModel(sface_path)

    def find(self, rgb, thr=0.75, max_side=192, refine=True):
        """Bestes Gesicht in einem (Personen-)Ausschnitt. Rueckgabe: (score, box, landmarks) in Pixeln oder None.
        Mit refine wird das Gesicht in einem engen Ausschnitt ein zweites Mal gesucht (genauere Punkte)."""
        best = self._find(rgb, thr, max_side)
        if best is None or not refine:
            return best
        x1, y1, x2, y2 = best[1]
        sub, (ox, oy) = imaging.crop(rgb, (x1, y1, x2, y2), pad=0.6)
        if sub is None:
            return best
        r = self._find(sub, thr * 0.8, max_side=min(224, max(sub.shape[:2])))
        if r is None:
            return best
        off = np.array([ox, oy], np.float32)
        return (max(best[0], r[0]), r[1] + np.concatenate([off, off]), r[2] + off)

    def _find(self, rgb, thr, max_side):
        h, w = rgb.shape[:2]
        sc = min(1.0, max_side / float(max(h, w)))
        nw, nh = max(1, int(w * sc)), max(1, int(h * sc))
        img = imaging.resize_bilinear(rgb, nw, nh) if sc < 1.0 else rgb
        PW, PH = int(math.ceil(nw / 32.0) * 32), int(math.ceil(nh / 32.0) * 32)
        canvas = np.zeros((PH, PW, 3), np.float32)
        canvas[:nh, :nw] = img[:, :, ::-1]  # BGR
        outs = self.det.run({self.det.input_names[0]: canvas.transpose(2, 0, 1)[None]})
        names = self.det.output_names
        o = dict(zip(names, outs))
        best = None
        for s in (8, 16, 32):
            cols = PW // s
            cls = np.clip(o["cls_%d" % s].reshape(-1), 0, 1)
            obj = np.clip(o["obj_%d" % s].reshape(-1), 0, 1)
            score = np.sqrt(cls * obj)
            i = int(score.argmax())
            if score[i] < thr or (best is not None and score[i] <= best[0]):
                continue
            r, c = divmod(i, cols)
            bb = o["bbox_%d" % s].reshape(-1, 4)[i]
            kp = o["kps_%d" % s].reshape(-1, 10)[i]
            cx, cy = (c + bb[0]) * s, (r + bb[1]) * s
            bw, bh = math.exp(bb[2]) * s, math.exp(bb[3]) * s
            box = np.array([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2]) / sc
            lm = np.stack([(kp[0::2] + c) * s, (kp[1::2] + r) * s], 1) / sc
            best = (float(score[i]), box, lm)
        return best

    def embed(self, rgb, landmarks):
        if self.rec is None:
            return None
        M = imaging.similarity_transform(landmarks, self.TEMPLATE)
        face = imaging.warp_affine(rgb, M, 112, 112)  # RGB (SFace erwartet RGB)
        x = face.astype(np.float32).transpose(2, 0, 1)[None]
        f = self.rec.run({self.rec.input_names[0]: x})[0].reshape(-1)
        n = np.linalg.norm(f)
        return (f / n).astype(np.float32) if n > 0 else None


class BodyReID:
    """Personen-Wiedererkennung am ganzen Koerper (YoutuReID, 768-dim. Merkmal, Kosinus-Aehnlichkeit).
    Deutlich trennschaerfer als ein Farbhistogramm: erkennt dieselbe Person in anderer Haltung,
    aus anderem Winkel und in einer anderen Kamera."""

    MEAN = np.array([0.485, 0.456, 0.406], np.float32)
    STD = np.array([0.229, 0.224, 0.225], np.float32)
    W, H = 128, 256

    def __init__(self, onnx_path, engine=None):
        import threading
        self.lock = threading.Lock()
        self.gpu = False
        self.net = None
        self.cvnet = None
        if engine:
            from .trt import TrtModel
            self.net = TrtModel(engine)
            self.gpu = True
            self.backend = "TensorRT (GPU)"
            return
        try:
            import cv2
            self.cvnet = cv2.dnn.readNetFromONNX(onnx_path)
            self.backend = "OpenCV (CPU)"
            return
        except Exception:
            self.cvnet = None
        self.net = OnnxModel(onnx_path)
        self.backend = "CPU"

    def embed(self, rgb):
        """Merkmal fuer einen Personen-Ausschnitt (RGB, uint8) oder None (zu klein)."""
        if rgb is None or rgb.shape[0] < 40 or rgb.shape[1] < 16:
            return None
        img = imaging.resize_bilinear(rgb, self.W, self.H).astype(np.float32) / 255.0
        x = ((img - self.MEAN) / self.STD).transpose(2, 0, 1)[None].astype(np.float32)
        with self.lock:
            if self.cvnet is not None:
                self.cvnet.setInput(x)
                f = self.cvnet.forward().reshape(-1)
            else:
                f = self.net.run({self.net.input_names[0]: x})[0].reshape(-1)
        n = float(np.linalg.norm(f))
        return (f / n).astype(np.float32) if n > 0 else None

    def close(self):
        if self.net is not None and hasattr(self.net, "close"):
            self.net.close()


# ----------------------------------------------------------------------------
# Kachel-Analyse: grosse Kamerabilder zusaetzlich in ueberlappenden Ausschnitten pruefen.
# Das Modell sieht das Bild sonst verkleinert auf seine Eingangsgroesse (z. B. 640 px) -
# weit entfernte Personen sind dann nur noch wenige Pixel hoch und werden uebersehen.
# ----------------------------------------------------------------------------
def tile_layout(W, H, S, max_tiles=4):
    """Ausschnitte (x1, y1, x2, y2) mit ca. 20 % Ueberlappung; leer, wenn sich Kacheln nicht lohnen."""
    if max(W, H) < S * 1.15:
        return []
    cols = max(1, int(np.ceil(W / (S * 1.2))))
    rows = max(1, int(np.ceil(H / (S * 1.2))))
    while cols * rows > max_tiles:
        if cols >= rows:
            cols -= 1
        else:
            rows -= 1
    if cols * rows == 1:
        if W >= H:
            cols = 2
        else:
            rows = 2
    tw = min(W, int(np.ceil(W / float(cols) * 1.2))) if cols > 1 else W
    th = min(H, int(np.ceil(H / float(rows) * 1.2))) if rows > 1 else H
    if cols == 2 and rows == 1:
        tw = max(tw, min(W, H))                  # fast quadratisch -> Modell nutzt die Flaeche voll
    out = []
    for r in range(rows):
        y0 = int(round(r * (H - th) / float(rows - 1))) if rows > 1 else 0
        for c in range(cols):
            x0 = int(round(c * (W - tw) / float(cols - 1))) if cols > 1 else 0
            out.append((x0, y0, x0 + tw, y0 + th))
    return out


def _ios(a, b):
    """Anteil der Flaeche von a, der in b liegt."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    ar = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return ix * iy / ar


def detect_tiled(det, rgb, thr, allowed=None, mode="auto", max_tiles=4):
    """Ganzes Bild + Kacheln; Ergebnisse zusammenfuehren. -> (detections, anzahl_durchlaeufe)"""
    H, W = rgb.shape[:2]
    full = det.detect(rgb, thr, allowed=allowed)
    tiles = tile_layout(W, H, det.size, max_tiles) if mode != "off" else []
    if not tiles:
        return full, 1
    cand = [(d, None) for d in full]
    for (x0, y0, x1, y1) in tiles:
        sub = np.ascontiguousarray(rgb[y0:y1, x0:x1])
        for d in det.detect(sub, thr, allowed=allowed):
            b = (d[0] + x0, d[1] + y0, d[2] + x0, d[3] + y0)
            # Kante innerhalb des Bildes beruehrt -> Objekt evtl. abgeschnitten
            cut = ((d[0] <= 2 and x0 > 0) or (d[2] >= x1 - x0 - 2 and x1 < W) or
                   (d[1] <= 2 and y0 > 0) or (d[3] >= y1 - y0 - 2 and y1 < H))
            cand.append(((b[0], b[1], b[2], b[3], d[4], d[5]), cut))
    if not cand:
        return [], 1 + len(tiles)
    # Abgeschnittene Teilstuecke derselben Klasse wieder zusammensetzen
    merged = []
    used = set()
    for i, (a, ca) in enumerate(cand):
        if i in used:
            continue
        if ca:
            for j in range(i + 1, len(cand)):
                b, cb = cand[j]
                if j in used or not cb or b[5] != a[5]:
                    continue
                oy = min(a[3], b[3]) - max(a[1], b[1])
                ox = min(a[2], b[2]) - max(a[0], b[0])
                hy = min(a[3] - a[1], b[3] - b[1])
                wx = min(a[2] - a[0], b[2] - b[0])
                if (ox > 0 and oy > 0.6 * hy) or (oy > 0 and ox > 0.6 * wx):
                    a = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]), max(a[4], b[4]), a[5])
                    used.add(j)
        merged.append((a, ca))
    boxes = np.array([m[0][:4] for m in merged], np.float32)
    scores = np.array([m[0][4] for m in merged], np.float32)
    cls = np.array([m[0][5] for m in merged], np.float32)
    keep = nms(boxes + cls[:, None] * (max(W, H) * 2.0), scores, 0.5)
    kept = sorted((merged[k] for k in keep), key=lambda m: -m[0][4])
    out = []
    for a, ca in kept:
        # am Kachelrand abgeschnittenes Teilstueck, das in einem anderen Treffer steckt: weglassen
        # (vollstaendige Treffer bleiben - verdeckte Personen hintereinander duerfen ineinander liegen)
        if ca and any(o[5] == a[5] and _ios(a, o) >= 0.6 for o in out):
            continue
        out.append(a)
    return out, 1 + len(tiles)
