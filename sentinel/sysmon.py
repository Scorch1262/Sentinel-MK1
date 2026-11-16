# -*- coding: utf-8 -*-
"""Einbindung von System-Monitor-MQTT-MK1 (https://github.com/Scorch1262/System-Monitor-MQTT-MK1).

Der System-Monitor laeuft als eigener Dienst (systemd "system-monitor-mqtt", Ordner
/opt/System-Monitor-MQTT-MK1, Webseite Port 8095) und sendet CPU/GPU/RAM/Temperaturen/Luefter des
Jetson per MQTT. Sentinel zeigt seinen Zustand, laedt ihn direkt aus GitHub (dessen install.sh,
unveraendert), startet ihn neu und kann die MQTT-Zugangsdaten von Sentinel uebernehmen.

Der System-Monitor braucht Python 3.7 (http.server.ThreadingHTTPServer, subprocess.run mit
capture_output/text), JetPack 4 hat Python 3.6. Statt seinen Code zu aendern, legt Sentinel eine kleine
Anpassung (sitecustomize.py in <Ordner>/py36) daneben und bindet sie per systemd-Drop-in
(PYTHONPATH) nur in diesen Dienst ein. Sie ueberlebt Updates des System-Monitors (install.sh und
dessen eigener Updater ersetzen nur app/, web/, assets/, main.py ...).
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
DROPIN_DIR = "/etc/systemd/system/%s.service.d" % SERVICE
DROPIN = DROPIN_DIR + "/sentinel-py36.conf"

PY36_SHIM = '''# -*- coding: utf-8 -*-
# Von Sentinel-MK1 angelegt: Python-3.6-Anpassung fuer System-Monitor-MQTT-MK1.
# Wird nur fuer den Dienst system-monitor-mqtt geladen (PYTHONPATH im systemd-Drop-in
# /etc/systemd/system/system-monitor-mqtt.service.d/sentinel-py36.conf).
import sys

if sys.version_info < (3, 7):
    import http.server
    import socketserver
    import subprocess

    if not hasattr(http.server, "ThreadingHTTPServer"):
        class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
        http.server.ThreadingHTTPServer = ThreadingHTTPServer  # novermin

    _run = subprocess.run

    def run(*args, **kw):
        if kw.pop("capture_output", False):
            kw["stdout"] = subprocess.PIPE
            kw["stderr"] = subprocess.PIPE
        if "text" in kw:
            kw["universal_newlines"] = bool(kw.pop("text"))
        return _run(*args, **kw)

    subprocess.run = run
'''


class SysMon(object):
    def __init__(self, log):
        self.log = log
        self.job = None              # laufende Installation: dict(state, log, started, finished)
        self.lock = threading.Lock()
        self.remote = None           # Version auf GitHub (main)
        self.remote_ts = 0.0

    def remote_version(self):
        """Version auf GitHub (app/version.py im main-Zweig), hoechstens alle 10 min im Hintergrund geholt."""
        now = time.time()
        if now - self.remote_ts > 600:
            self.remote_ts = now

            def work():
                try:
                    url = "https://raw.githubusercontent.com/%s/main/app/version.py" % REPO
                    txt = urlopen(Request(url, headers={"User-Agent": "Sentinel-MK1"}), timeout=20).read()
                    m = re.search(r'VERSION\s*=\s*"([^"]+)"', txt.decode("utf-8", "replace"))
                    if m:
                        self.remote = m.group(1)
                except Exception:
                    self.remote_ts = time.time() - 540      # in 1 min erneut versuchen
            threading.Thread(target=work, daemon=True).start()
        return self.remote

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
    def needs_shim():
        """True, wenn das System-Python zu alt fuer den System-Monitor ist (JetPack 4: 3.6)."""
        try:
            p = subprocess.run(["python3", "-c", "import http.server as h;print(hasattr(h,'ThreadingHTTPServer'))"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
            return p.stdout.decode().strip() == "False"
        except Exception:
            return False

    def shim_active(self):
        d = self.directory()
        return bool(d) and os.path.isfile(os.path.join(d, "py36", "sitecustomize.py")) and os.path.isfile(DROPIN)

    def ensure_compat(self, restart=False):
        """Python-3.6-Anpassung anlegen/aktualisieren. Gibt True zurueck, wenn etwas geaendert wurde."""
        d = self.directory()
        if not d or not self.needs_shim():
            return False
        changed = False
        shim_dir = os.path.join(d, "py36")
        shim = os.path.join(shim_dir, "sitecustomize.py")
        try:
            old = open(shim, encoding="utf-8").read()
        except Exception:
            old = None
        if old != PY36_SHIM:
            os.makedirs(shim_dir, exist_ok=True)
            with open(shim, "w", encoding="utf-8") as f:
                f.write(PY36_SHIM)
            changed = True
        conf = "[Service]\n# von Sentinel-MK1: Python-3.6-Anpassung (siehe %s)\nEnvironment=PYTHONPATH=%s\n" % (shim, shim_dir)
        try:
            old = open(DROPIN, encoding="utf-8").read()
        except Exception:
            old = None
        if old != conf:
            os.makedirs(DROPIN_DIR, exist_ok=True)
            with open(DROPIN, "w", encoding="utf-8") as f:
                f.write(conf)
            try:
                subprocess.run(["systemctl", "daemon-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=60)
            except Exception as e:
                self.log("System-Monitor: systemctl daemon-reload fehlgeschlagen: %s" % e)
            changed = True
        if changed:
            self.log("System-Monitor: Python-3.6-Anpassung eingerichtet")
            if restart:
                try:
                    self.restart()
                except Exception as e:
                    self.log("System-Monitor: Neustart fehlgeschlagen: %s" % e)
        return changed

    def startup_check(self):
        """Beim Start von Sentinel: Anpassung nachruesten (z. B. nach Installation per install-jetson.sh)."""
        def work():
            try:
                self.ensure_compat(restart=True)
            except Exception as e:
                self.log("System-Monitor: Anpassung fehlgeschlagen: %s" % e)
        threading.Thread(target=work, daemon=True).start()

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
                  web=False, mqtt_host="", mqtt_connected=None, base_topic="", py36=False,
                  job=self.job_info(), repo="https://github.com/" + REPO, remote=self.remote_version())
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
        st["py36"] = self.shim_active()
        return st

    def job_info(self):
        j = self.job
        if not j:
            return None
        return dict(state=j["state"], log=j["log"][-6000:], started=j["started"], finished=j.get("finished"))

    # ------------------------------------------------------------------ Aktionen
    def install(self):
        """System-Monitor direkt aus GitHub laden: dessen install.sh holen und ausfuehren (installiert bzw.
        aktualisiert auf den aktuellen Stand des Repos), danach ggf. Python-3.6-Anpassung."""
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
            if self.needs_shim():
                out("Python %s: richte Python-3.6-Anpassung für den Dienst ein ..." % self._pyver())
                self.ensure_compat()
                self.restart()
            v = self.version()
            ok = False
            for _ in range(15):
                time.sleep(1)
                ok, _ = self.reachable()
                if ok:
                    break
            if not ok:
                raise RuntimeError("installiert (v%s), Webseite antwortet aber nicht – "
                                   "journalctl -u %s zeigt den Grund" % (v, SERVICE))
            j["state"] = "fertig"
            out("Fertig: System-Monitor v%s läuft auf Port %d" % (v, self.port()))
            self.log("System-Monitor v%s installiert/aktualisiert" % v)
            self.remote_ts = 0.0
        except Exception as e:
            j["state"] = "Fehler"
            out("FEHLER: %s" % e)
            self.log("System-Monitor: %s" % e)
        finally:
            j["finished"] = time.time()

    @staticmethod
    def _pyver():
        try:
            p = subprocess.run(["python3", "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
            return p.stdout.decode().split()[-1]
        except Exception:
            return "?"

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
