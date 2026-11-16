# -*- coding: utf-8 -*-
"""Einbindung von System-Monitor-MQTT-MK1 (https://github.com/Scorch1262/System-Monitor-MQTT-MK1).

Der System-Monitor laeuft als eigener Dienst (systemd "system-monitor-mqtt", Ordner
/opt/System-Monitor-MQTT-MK1, Webseite Port 8095) und sendet CPU/GPU/RAM/Temperaturen/Luefter des
Jetson per MQTT. Sentinel zeigt seinen Zustand, installiert/aktualisiert ihn ueber dessen
install.sh, startet ihn neu und kann die MQTT-Zugangsdaten von Sentinel uebernehmen.
"""

import json
import os
import re
import subprocess
import threading
import time

try:
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover
    from urllib2 import Request, urlopen

NAME = "System-Monitor-MQTT-MK1"
SERVICE = "system-monitor-mqtt"
REPO = "Scorch1262/System-Monitor-MQTT-MK1"
DIRS = ("/opt/" + NAME, "/root/" + NAME)
MIN_VERSION = (1, 4, 0)          # ab 1.4.0 laeuft er mit Python 3.6 (JetPack 4) und kennt Jetson-Sensoren


def _vtuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])
    except ValueError:
        return ()


class SysMon(object):
    def __init__(self, log):
        self.log = log
        self.job = None              # laufende Installation: dict(state, log, started, finished)
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ Zustand
    def directory(self):
        for d in DIRS:
            if os.path.isfile(os.path.join(d, "main.py")):
                return d
        return None

    def config_path(self):
        d = self.directory()
        return os.path.join(d, NAME + ".json") if d else None

    def read_config(self):
        p = self.config_path()
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def version(self):
        d = self.directory()
        if not d:
            return None
        try:
            txt = open(os.path.join(d, "app", "version.py"), encoding="utf-8").read()
            m = re.search(r'VERSION\s*=\s*"([^"]+)"', txt)
            return m.group(1) if m else None
        except Exception:
            return None

    def port(self):
        try:
            return int(self.read_config().get("web", {}).get("port") or 8095)
        except (TypeError, ValueError):
            return 8095

    @staticmethod
    def service_state():
        try:
            p = subprocess.run(["systemctl", "is-active", SERVICE], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=10)
            return p.stdout.decode().strip() or "unbekannt"
        except Exception:
            if os.path.exists("/etc/init.d/" + SERVICE):
                return "unbekannt"
            return "fehlt"

    def reachable(self, port=None):
        port = port or self.port()
        try:
            r = urlopen(Request("http://127.0.0.1:%d/api/state" % port), timeout=2.5)
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
            return True, data
        except Exception:
            return False, None

    def status(self):
        d = self.directory()
        st = dict(installed=bool(d), dir=d, version=None, port=8095, service="fehlt", running=False,
                  web=False, mqtt_host="", mqtt_connected=None, base_topic="", too_old=False,
                  job=self.job_info(), repo="https://github.com/" + REPO)
        if not d:
            return st
        cfg = self.read_config()
        st["version"] = self.version()
        st["port"] = self.port()
        st["service"] = self.service_state()
        st["running"] = st["service"] == "active"
        st["mqtt_host"] = (cfg.get("mqtt") or {}).get("host") or ""
        st["base_topic"] = (cfg.get("publish") or {}).get("base_topic") or ""
        ok, data = self.reachable(st["port"])
        st["web"] = ok
        if ok and isinstance(data, dict):
            mq = data.get("mqtt")
            if isinstance(mq, dict):
                st["mqtt_connected"] = bool(mq.get("connected"))
                st["mqtt_state"] = mq.get("state") or ""
                st["mqtt_error"] = mq.get("last_error") or ""
                st["base_topic"] = mq.get("base_topic") or st["base_topic"]
                st["sent"] = mq.get("sent_total")
        v = _vtuple(st["version"])
        st["too_old"] = bool(v) and v < MIN_VERSION
        return st

    def job_info(self):
        j = self.job
        if not j:
            return None
        return dict(state=j["state"], log=j["log"][-6000:], started=j["started"], finished=j.get("finished"))

    # ------------------------------------------------------------------ Aktionen
    def install(self):
        """install.sh des System-Monitors aus GitHub holen und ausfuehren (installiert bzw. aktualisiert)."""
        with self.lock:
            if self.job and not self.job.get("finished"):
                raise RuntimeError("Installation läuft bereits")
            self.job = dict(state="läuft", log="", started=time.time())
        threading.Thread(target=self._install, daemon=True).start()
        return True

    def _install(self):
        j = self.job

        def out(line):
            j["log"] += line.rstrip("\n") + "\n"

        try:
            url = "https://raw.githubusercontent.com/%s/main/install.sh" % REPO
            out("Lade %s ..." % url)
            data = urlopen(Request(url, headers={"User-Agent": "Sentinel-MK1"}), timeout=60).read()
            path = "/tmp/%s-install.sh" % NAME
            with open(path, "wb") as f:
                f.write(data)
            env = dict(os.environ, REPO=REPO)
            # per Pipe-Name "sh" starten -> install.sh laedt den Code selbst aus GitHub
            p = subprocess.Popen(["sh", "-c", 'sh < "%s"' % path], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, env=env)
            for line in iter(p.stdout.readline, b""):
                out(line.decode("utf-8", "replace"))
            rc = p.wait()
            if rc != 0:
                raise RuntimeError("install.sh beendet mit Code %d" % rc)
            time.sleep(3)
            ok, _ = self.reachable()
            v = self.version()
            if not ok:
                too_old = bool(_vtuple(v)) and _vtuple(v) < MIN_VERSION
                raise RuntimeError("installiert (v%s), Webseite antwortet aber nicht%s" % (
                    v, " - auf dem Jetson ist v1.4.0 oder neuer nötig (Python 3.6)" if too_old else ""))
            j["state"] = "fertig"
            out("Fertig: System-Monitor v%s läuft auf Port %d" % (v, self.port()))
            self.log("System-Monitor v%s installiert/aktualisiert" % v)
        except Exception as e:
            j["state"] = "Fehler"
            out("FEHLER: %s" % e)
            self.log("System-Monitor: %s" % e)
        finally:
            j["finished"] = time.time()

    def restart(self):
        p = subprocess.run(["systemctl", "restart", SERVICE], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=60)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or "Neustart fehlgeschlagen")
        return True

    def adopt_mqtt(self, mcfg):
        """MQTT-Broker und Zugangsdaten von Sentinel in den System-Monitor uebernehmen."""
        p = self.config_path()
        if not p:
            raise RuntimeError("System-Monitor ist nicht installiert")
        cfg = self.read_config()
        mq = cfg.setdefault("mqtt", {})
        mq["host"] = mcfg.get("host") or ""
        mq["port"] = int(mcfg.get("port") or 1883)
        mq["username"] = mcfg.get("user") or ""
        mq["password"] = mcfg.get("password") or ""
        pub = cfg.setdefault("publish", {})
        pub.setdefault("base_topic", "jetson/{hostname}")
        pub["enabled"] = True
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
        self.restart()
        return True
