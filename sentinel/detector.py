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
