"""Konfiguration (config.json neben app.py, gut lesbares JSON)."""
import copy
import json
import os
import threading

DEFAULTS = {
    "config_version": 2,
    "server": {"host": "0.0.0.0", "port": 8096},
    "streams": [],
    "detection": {
        "enabled": True,
        "model": "auto",            # auto | nanodet | yolox_tiny | yolox_s  (auto: GPU -> yolox_s, sonst yolox_tiny)
        "model_size": 416,          # nur NanoDet auf der CPU: 320 = schneller, 416 = genauer
        "threshold": 0.5,           # Grundschwelle
        "group_thresholds": {"person": 0.55},   # strengere Schwellen je Gruppe
        "person_min_aspect": 0.9,   # Person muss mindestens 0,9x so hoch wie breit sein (gegen Schatten)
        "verify": True,             # Zweitpruefung neuer Objekte im vergroesserten Ausschnitt
        "verify_margin": 0.05,
        "verify_retry_s": 20,
        "groups": ["person", "animal", "vehicle", "bike"],
        "extra_classes": [],
        "min_hits": 3,              # so oft hintereinander erkannt, bis eine Sichtung zaehlt
        "interval_s": 0.3,          # Mindestabstand zwischen zwei Analysen je Stream
        "motion_gate": True,        # ohne Bewegung keine (teure) Analyse
        "motion_level": 2.5,
        "track_timeout_s": 6.0,
        "resnap_s": 120,            # bei laengerer Anwesenheit alle x s ein weiteres Bild
        "sighting_gap_s": 60,       # erst nach so langer Abwesenheit im selben Stream zaehlt eine neue Sichtung
        "min_box_px": 24,
        "use_opencv": True,         # OpenCV-DNN nutzen, falls installiert (nur ohne GPU)
        "accel": "auto",            # auto | gpu | cpu  (gpu = TensorRT auf dem NVIDIA Jetson)
    },
    "identity": {
        "faces": True,
        "face_threshold": 0.40,     # SFace-Kosinus (0.363 = Empfehlung des Modells)
        "face_min_px": 28,
        "appearance_threshold": 0.90,
        "appearance_window_h": 12,  # Personen ohne Gesicht nur innerhalb dieser Zeit ueber Kleidung zuordnen
        "appearance_window_other_h": 72,
        "max_samples": 40,
    },
    "storage": {"max_days": 30, "max_mb": 8000},
    "mqtt": {"enabled": False, "host": "127.0.0.1", "port": 1883, "user": "", "password": "",
             "prefix": "sentinel", "publish_detections": False},
    "ui": {"sound": True, "repeat_list_min": 2, "live_mode": "live"},
    "update": {"repo": "Scorch1262/Sentinel-MK1", "branch": "main", "auto_check": True},
    # Luefter (nur Jetson): auto = Sentinel-Kurve, kernel = Jetson-Standard, fixed = fest, max = voll
    "fan": {"mode": "auto", "off_temp": 32, "min_temp": 38, "max_temp": 62, "min_pwm": 80, "max_pwm": 255,
            "fixed_pct": 60, "emergency_temp": 75, "interval_s": 2.0, "kickstart": True},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


CONFIG_VERSION = 2


def migrate(data):
    """Aeltere config.json anheben (v1.4.0: strengere Erkennung). Gibt True zurueck, wenn geaendert."""
    v = int(data.get("config_version", 1) or 1)
    if v >= CONFIG_VERSION:
        return False
    d = data.setdefault("detection", {})
    if v < 2:
        d["threshold"] = max(float(d.get("threshold", 0.5) or 0.5), 0.5)
        d.setdefault("group_thresholds", {"person": 0.55})
        d["min_hits"] = max(int(d.get("min_hits", 3) or 3), 3)
        d.setdefault("verify", True)
        d.setdefault("person_min_aspect", 0.9)
        d.setdefault("model", "auto")
    data["config_version"] = CONFIG_VERSION
    return True


class Config:
    def __init__(self, path, first_run=None):
        self.path = path
        self.lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load(first_run)

    def load(self, first_run=None):
        if not os.path.exists(self.path) and first_run:
            # Startwerte passend zum Geraet (z. B. Jetson: 416 px, kuerzere Analyse-Abstaende)
            self.data = _merge(DEFAULTS, first_run)
        if os.path.exists(self.path):
            try:
                raw = open(self.path, "rb").read().decode("utf-8-sig")
                loaded = json.loads(raw)
                if migrate(loaded):
                    print("config.json auf Version %d angehoben (strengere Objekterkennung)" % CONFIG_VERSION)
                self.data = _merge(DEFAULTS, loaded)
            except Exception as e:
                bad = self.path + ".defekt"
                try:
                    os.replace(self.path, bad)
                except Exception:
                    pass
                print("config.json unlesbar (%s) - Standardwerte, alte Datei: %s" % (e, bad))
                self.data = copy.deepcopy(DEFAULTS)
        self.save()

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    def get(self, *keys):
        with self.lock:
            d = self.data
            for k in keys:
                d = d[k]
            return copy.deepcopy(d)

    def update(self, section, values):
        with self.lock:
            if isinstance(self.data.get(section), dict) and isinstance(values, dict):
                self.data[section] = _merge(self.data[section], values)
            else:
                self.data[section] = values
            self.save()
