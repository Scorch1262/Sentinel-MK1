"""Luefterregelung fuer den NVIDIA Jetson (Nano 2GB/4GB: 4-Pin-PWM-Luefteranschluss).

Sentinel regelt den Luefter selbst nach einer Temperaturkurve (CPU/GPU), weil die Standardregelung
des Jetson Nano den Luefter erst spaet und zoegerlich startet - bei Dauerlast durch die
Objekterkennung wird das Modul sonst heiss und taktet herunter.

Sysfs (L4T R32 / JetPack 4.6):
  /sys/devices/pwm-fan/target_pwm     Sollwert 0..255 (schreiben)
  /sys/devices/pwm-fan/cur_pwm        aktueller Wert (lesen)
  /sys/devices/pwm-fan/temp_control   1 = Kernel regelt, 0 = Regelung von aussen
  /sys/devices/pwm-fan/rpm_measured   Drehzahl (nur Luefter mit Tachosignal)
Neuere L4T-Versionen (R35+): /sys/devices/platform/pwm-fan/hwmon/hwmon*/pwm1

Betriebsarten:
  auto    Sentinel-Kurve (Standard)
  kernel  Jetson-Standardregelung
  fixed   feste Drehzahl
  max     immer volle Drehzahl
"""
import glob
import os
import subprocess
import threading
import time

ROOT = os.environ.get("SENTINEL_FAN_SYSFS", "")   # nur fuer Tests: Pfad-Praefix fuer ein nachgebildetes sysfs

DEFAULTS = dict(mode="auto", off_temp=32, min_temp=38, max_temp=62, min_pwm=80, max_pwm=255, fixed_pct=60,
                emergency_temp=75, interval_s=2.0, kickstart=True)


def _p(path):
    return ROOT + path if ROOT else path


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _write(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except Exception:
        return False


def find_fan():
    """Pfade der Luefterregelung oder None."""
    base = _p("/sys/devices/pwm-fan")
    if os.path.exists(os.path.join(base, "target_pwm")):
        return dict(kind="l4t", target=os.path.join(base, "target_pwm"), cur=os.path.join(base, "cur_pwm"),
                    control=os.path.join(base, "temp_control"), rpm=os.path.join(base, "rpm_measured"))
    for h in sorted(glob.glob(_p("/sys/devices/platform/pwm-fan/hwmon/hwmon*"))):
        if os.path.exists(os.path.join(h, "pwm1")):
            return dict(kind="hwmon", target=os.path.join(h, "pwm1"), cur=os.path.join(h, "pwm1"),
                        control=os.path.join(h, "pwm1_enable"), rpm=os.path.join(h, "fan1_input"))
    return None


def jetson_temps():
    """Temperaturen der Thermal-Zonen {name: Grad}; ohne PMIC-Dummy und ohne Schaetzwerte."""
    out = {}
    for z in glob.glob(_p("/sys/class/thermal/thermal_zone*")):
        typ = (_read(os.path.join(z, "type")) or "").strip()
        low = typ.lower()
        if not typ or "pmic" in low:
            continue
        t = _read(os.path.join(z, "temp"))
        try:
            v = int(t) / 1000.0
        except (TypeError, ValueError):
            continue
        if 0 < v < 125:
            out[typ] = round(v, 1)
    return out


def curve(temp, c):
    """Sollwert 0..255 fuer eine Temperatur nach der Kurve (ohne Hysterese/Rampe)."""
    if temp is None:
        return int(c["max_pwm"])
    if temp >= c["emergency_temp"]:
        return 255
    if temp <= c["min_temp"]:
        return int(c["min_pwm"])
    if temp >= c["max_temp"]:
        return int(c["max_pwm"])
    f = (temp - c["min_temp"]) / float(c["max_temp"] - c["min_temp"])
    return int(round(c["min_pwm"] + f * (c["max_pwm"] - c["min_pwm"])))


class FanController(threading.Thread):
    def __init__(self, cfg, log):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.log = log
        self.stop_ev = threading.Event()
        self.fan = find_fan()
        self.available = self.fan is not None
        self.target = None          # zuletzt geschriebener Sollwert 0..255
        self.temp = None
        self.temps = {}
        self.running = False        # Luefter laeuft (Sollwert > 0)
        self.took_over = False
        self.error = ""
        self.mode = ""
        self.conflict = self._conflict()

    @staticmethod
    def _conflict():
        """Laeuft NVIDIAs eigener Luefterdienst (ab JetPack 5)? Dann wuerden sich beide Regelungen streiten."""
        try:
            r = subprocess.run(["systemctl", "is-active", "nvfancontrol"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=5)
            return r.stdout.decode().strip() == "active"
        except Exception:
            return False

    def settings(self):
        c = dict(DEFAULTS)
        c.update(self.cfg.get("fan") or {})
        for k in ("off_temp", "min_temp", "max_temp", "emergency_temp", "interval_s"):
            c[k] = float(c[k])
        for k in ("min_pwm", "max_pwm", "fixed_pct"):
            c[k] = int(c[k])
        if c["max_temp"] <= c["min_temp"]:
            c["max_temp"] = c["min_temp"] + 1
        c["off_temp"] = min(c["off_temp"], c["min_temp"])
        return c

    # ------------------------------------------------------------------
    def _take_over(self):
        if self.took_over:
            return
        if self.fan["kind"] == "l4t":
            _write(self.fan["control"], 0)
        else:
            _write(self.fan["control"], 1)   # hwmon: 1 = manuelle PWM
        self.took_over = True

    def _release(self):
        """Regelung an den Jetson zurueckgeben."""
        if not self.took_over:
            return
        if self.fan["kind"] == "l4t":
            if not _write(self.fan["control"], 1):
                _write(self.fan["target"], 128)  # ohne Kernelregelung: sicherer Mittelwert
        else:
            _write(self.fan["control"], 2)
        self.took_over = False
        self.target = None

    def _set(self, pwm, c):
        pwm = max(0, min(255, int(pwm)))
        if pwm == self.target:
            return
        if pwm > 0 and not self.running and c.get("kickstart", True) and pwm < 160:
            # Anlauf: viele 5-V-Luefter starten bei kleinem Tastgrad nicht -> kurz voll anlaufen lassen
            _write(self.fan["target"], 255)
            time.sleep(1.0)
        if not _write(self.fan["target"], pwm):
            self.error = "Lüfter nicht beschreibbar (Rechte? Dienst läuft als root)"
            return
        self.error = ""
        self.target = pwm
        self.running = pwm > 0

    def step(self):
        """Ein Regelschritt (auch fuer Tests direkt aufrufbar)."""
        c = self.settings()
        mode = c["mode"]
        self.temps = jetson_temps()
        self.temp = max(self.temps.values()) if self.temps else None
        if mode != self.mode:
            self.log("Lüfter: Betriebsart %s" % {"auto": "Sentinel-Kurve", "kernel": "Jetson-Standard",
                                                 "fixed": "fest %d %%" % c["fixed_pct"],
                                                 "max": "volle Drehzahl"}.get(mode, mode))
            self.mode = mode
        if mode == "kernel":
            self._release()
            return
        self._take_over()
        if mode == "max":
            self._set(255, c)
            return
        if mode == "fixed":
            self._set(round(c["fixed_pct"] * 2.55), c)
            return
        # auto: Kurve mit Hysterese (Aus/Ein) und langsamem Herunterregeln
        t = self.temp
        if t is not None and t >= c["emergency_temp"]:
            self._set(255, c)
            return
        if t is not None and t <= c["off_temp"]:
            want = 0
        elif t is not None and not self.running and t < c["min_temp"]:
            want = 0                     # bleibt aus, bis min_temp erreicht ist
        else:
            want = curve(t, c)
        if self.target is not None and want < self.target and want > 0:
            want = max(want, self.target - 12)   # leiser werden in kleinen Schritten (kein Pumpen)
        self._set(want, c)

    def run(self):
        if not self.available:
            return
        self.log("Lüfter gefunden (%s) - Regelung durch Sentinel aktiv" % self.fan["target"])
        if self.conflict:
            self.log("Lüfter: nvfancontrol läuft - Betriebsart 'Jetson-Standard' empfohlen")
        while not self.stop_ev.is_set():
            try:
                self.step()
            except Exception as e:
                self.error = str(e)
            self.stop_ev.wait(max(0.5, self.settings()["interval_s"]))
        self._release()

    def stop(self):
        self.stop_ev.set()
        if self.available:
            self._release()

    def status(self):
        if not self.available:
            return dict(available=False)
        cur = _read(self.fan["cur"])
        rpm = _read(self.fan["rpm"])
        try:
            cur = int(cur)
        except (TypeError, ValueError):
            cur = self.target
        try:
            rpm = int(rpm)
        except (TypeError, ValueError):
            rpm = None
        return dict(available=True, mode=self.mode, pwm=cur, pct=round((cur or 0) / 2.55) if cur is not None else None,
                    rpm=rpm, temp=self.temp, temps=self.temps, error=self.error, conflict=self.conflict,
                    managed=self.took_over)
