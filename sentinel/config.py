"""Konfiguration (config.json neben app.py, gut lesbares JSON)."""
import copy
import json
import os
import threading

DEFAULTS = {
    "server": {"host": "0.0.0.0", "port": 8096},
    "streams": [],
    "detection": {
        "enabled": True,
        "model_size": 320,          # 320 = schnell (Brume 2), 416 = genauer
        "threshold": 0.45,
        "groups": ["person", "animal", "vehicle", "bike"],
        "extra_classes": [],
        "min_hits": 2,              # so oft hintereinander erkannt, bis eine Sichtung zaehlt
        "interval_s": 1.0,          # Mindestabstand zwischen zwei Analysen je Stream
        "motion_gate": True,        # ohne Bewegung keine (teure) Analyse
        "motion_level": 2.5,
        "track_timeout_s": 6.0,
        "resnap_s": 120,            # bei laengerer Anwesenheit alle x s ein weiteres Bild
        "sighting_gap_s": 60,       # erst nach so langer Abwesenheit im selben Stream zaehlt eine neue Sichtung
        "min_box_px": 24,
        "use_opencv": True,         # OpenCV nutzen, falls installiert (PC); auf dem Brume immer numpy
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
    "storage": {"max_days": 30, "max_mb": 2000},
    "mqtt": {"enabled": False, "host": "127.0.0.1", "port": 1883, "user": "", "password": "",
             "prefix": "sentinel", "publish_detections": False},
    "ui": {"sound": True, "repeat_list_min": 2, "live_mode": "live"},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                raw = open(self.path, "rb").read().decode("utf-8-sig")
                self.data = _merge(DEFAULTS, json.loads(raw))
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
