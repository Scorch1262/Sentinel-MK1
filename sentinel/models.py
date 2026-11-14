"""KI-Modelle (ONNX) laden und pruefen (OpenCV Model Zoo, YOLOX-Releases auf GitHub).

Es werden nur die Modelle geladen, die gebraucht werden: das gewaehlte Erkennungsmodell
plus Gesichtssuche und Gesichtsmerkmale."""
import hashlib
import os
import shutil
import subprocess
import threading
import urllib.request

ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
ZOO_ALT = "https://github.com/opencv/opencv_zoo/raw/main/models/"

YOLOX_REL = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/"

# Erkennungsmodelle: Schluessel = Wert von detection.model in config.json
DETECTORS = {
    "nanodet": dict(key="detector", title="NanoDet-Plus", input=416, kind="nanodet",
                    note="am schnellsten, aber am ungenauesten"),
    "yolox_tiny": dict(key="yolox_tiny", title="YOLOX-Tiny", input=416, kind="yolox",
                       note="genauer, ca. 4× Rechenaufwand von NanoDet"),
    "yolox_s": dict(key="yolox_s", title="YOLOX-S", input=640, kind="yolox",
                    note="deutlich genauer – empfohlen für Jetson-GPU"),
    "yolox_m": dict(key="yolox_m", title="YOLOX-M", input=640, kind="yolox",
                    note="am genauesten, ca. 3× Rechenaufwand von YOLOX-S – für 1–2 Kameras"),
}

MODELS = {
    "detector": dict(file="object_detection_nanodet_2022nov.onnx", path="object_detection_nanodet/",
                     size=3800954, sha256="4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186",
                     title="NanoDet-Plus (Objekte, 80 Klassen)", license="Apache-2.0"),
    "yolox_tiny": dict(file="yolox_tiny.onnx", urls=[YOLOX_REL + "yolox_tiny.onnx"],
                       size=20219662, sha256="427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7",
                       title="YOLOX-Tiny (Objekte, 80 Klassen)", license="Apache-2.0"),
    "yolox_s": dict(file="object_detection_yolox_2022nov.onnx", path="object_detection_yolox/",
                    size=35858002, sha256="c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
                    title="YOLOX-S (Objekte, 80 Klassen)", license="Apache-2.0"),
    "yolox_m": dict(file="yolox_m.onnx", urls=[YOLOX_REL + "yolox_m.onnx"],
                    size=101259744, sha256="21ff6cfdeb53b013bac2249599e55f00bff3cfdfdab37ed7a4620818c1d15b3f",
                    title="YOLOX-M (Objekte, 80 Klassen)", license="Apache-2.0"),
    "face_det": dict(file="face_detection_yunet_2023mar.onnx", path="face_detection_yunet/",
                     size=232589, sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
                     title="YuNet (Gesichter finden)", license="MIT"),
    "face_rec": dict(file="face_recognition_sface_2021dec.onnx", path="face_recognition_sface/",
                     size=38696353, sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
                     title="SFace (Personen wiedererkennen)", license="Apache-2.0"),
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class ModelManager:
    def __init__(self, folder, log, wanted=None):
        self.folder = folder
        self.log = log
        self.wanted = wanted or (lambda: list(MODELS))   # benoetigte Modell-Schluessel
        self.state = {k: "fehlt" for k in MODELS}
        self.progress = {}
        self.lock = threading.Lock()
        self.busy = False
        self.building = False
        self.engine_state = {}
        os.makedirs(folder, exist_ok=True)
        for k in MODELS:
            if self.ok(k, quick=True):
                self.state[k] = "ok"

    def path(self, key):
        return os.path.join(self.folder, MODELS[key]["file"])

    def ok(self, key, quick=False):
        p = self.path(key)
        m = MODELS[key]
        if not os.path.exists(p) or os.path.getsize(p) != m["size"]:
            return False
        if quick:
            return True
        return sha256(p) == m["sha256"]

    def all_ok(self):
        return all(self.state[k] == "ok" for k in self.wanted())

    def info(self):
        w = set(self.wanted())
        return [dict(key=k, title=m["title"], file=m["file"], size=m["size"], license=m["license"],
                     state=self.state[k], progress=self.progress.get(k, 0)) for k, m in MODELS.items()
                if k in w or self.state[k] == "ok"]

    # ------------------------------------------------------------------
    # TensorRT-Engines (nur Jetson): einmalig aus den ONNX-Modellen bauen
    # ------------------------------------------------------------------
    ENGINES = {"detector": "nanodet_416", "yolox_tiny": "yolox_tiny_416", "yolox_s": "yolox_s_640", "yolox_m": "yolox_m_640",
               "face_rec": "sface_112"}

    def engine_path(self, key):
        from . import trt
        ver = (trt.trt_version() or "x").replace(".", "_")
        return os.path.join(self.folder, "%s_fp16_trt%s.engine" % (self.ENGINES[key], ver))

    def engine_ok(self, key):
        p = self.engine_path(key)
        return os.path.exists(p) and os.path.getsize(p) > 1000

    def engines_info(self):
        w = set(self.wanted())
        return [dict(key=k, file=os.path.basename(self.engine_path(k)),
                     state="ok" if self.engine_ok(k) else self.engine_state.get(k, "fehlt"))
                for k in self.ENGINES if k in w]

    def build_engines(self, on_done=None):
        """Fehlende Engines im Hintergrund bauen (nacheinander, braucht viel Speicher)."""
        from . import trt
        with self.lock:
            if self.building:
                return
            self.building = True

        def run():
            try:
                for k in self.ENGINES:
                    if k not in self.wanted():
                        continue
                    if self.engine_ok(k) or self.state.get(k) != "ok" or self.engine_state.get(k) == "fehler":
                        continue
                    self.engine_state[k] = "baut"
                    ok = trt.build_engine(self.path(k), self.engine_path(k), self.log)
                    self.engine_state[k] = "ok" if ok else "fehler"
            finally:
                self.building = False
                if on_done:
                    on_done()

        threading.Thread(target=run, daemon=True).start()

    def ensure(self, on_done=None):
        with self.lock:
            if self.busy:
                return
            self.busy = True

        def run():
            try:
                for k in self.wanted():
                    if self.ok(k, quick=True):
                        self.state[k] = "ok"
                        continue
                    self._download(k)
            finally:
                self.busy = False
                if on_done:
                    on_done()

        threading.Thread(target=run, daemon=True).start()

    def _download(self, key):
        m = MODELS[key]
        dest = self.path(key)
        tmp = dest + ".part"
        urls = m.get("urls") or [base + m["path"] + m["file"] for base in (ZOO, ZOO_ALT)]
        for url in urls:
            self.state[key] = "lädt"
            self.progress[key] = 0
            try:
                self.log("Lade Modell %s ..." % m["file"])
                req = urllib.request.Request(url, headers={"User-Agent": "Sentinel-MK1"})
                with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                    got = 0
                    while True:
                        b = r.read(1 << 16)
                        if not b:
                            break
                        f.write(b)
                        got += len(b)
                        self.progress[key] = int(got * 100 / m["size"])
            except Exception as e:
                self.log("Python-Download fehlgeschlagen (%s) - versuche curl/wget" % e)
                self._shell_download(url, tmp)
            if os.path.exists(tmp) and os.path.getsize(tmp) == m["size"] and sha256(tmp) == m["sha256"]:
                os.replace(tmp, dest)
                self.state[key] = "ok"
                self.progress[key] = 100
                self.log("Modell %s bereit" % m["file"])
                return True
            try:
                os.remove(tmp)
            except OSError:
                pass
        self.state[key] = "fehler"
        self.log("Modell %s konnte nicht geladen werden - Datei manuell nach %s kopieren" % (m["file"], self.folder))
        return False

    @staticmethod
    def _shell_download(url, dest):
        if shutil.which("curl"):
            subprocess.call(["curl", "-fsSL", "--retry", "3", "-o", dest, url])
        elif shutil.which("wget"):
            subprocess.call(["wget", "-q", "-O", dest, url])
