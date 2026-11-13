#!/usr/bin/env python3
"""
Sentinel MK1 - Objekterkennung in mehreren RTSP-Streams
=======================================================

Erkennt Personen, Tiere, Fahrzeuge usw. in RTSP-Streams, markiert sie farbig,
speichert Bilder in einer Galerie, erkennt Personen wieder (Gesicht + Aussehen),
lernt aus Korrekturen und loest Alarme aus.

Start:   python3 app.py [--port 8096] [--host 0.0.0.0]
Daten:   config.json und Ordner data/ neben dieser Datei
Geraete: GL.iNet Brume 2 (install.sh), NVIDIA Jetson Nano (install-jetson.sh), PC
"""
import argparse
import os
import signal
import sys
import threading
import time

VERSION = "1.1.0"

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
# numpy/OpenBLAS: hoechstens zwei Kerne (Brume 2 hat zwei)
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")
# Jetson (Cortex-A57): manche OpenBLAS-Builds brechen sonst mit "Illegal instruction" ab
if os.path.exists("/etc/nv_tegra_release"):
    os.environ.setdefault("OPENBLAS_CORETYPE", "ARMV8")
    os.environ["OPENBLAS_NUM_THREADS"] = os.environ.get("SENTINEL_CPU_THREADS", "4")
    os.environ["OMP_NUM_THREADS"] = os.environ.get("SENTINEL_CPU_THREADS", "4")

try:
    import numpy  # noqa: F401
except ImportError:
    print("FEHLER: numpy fehlt.  Brume 2:  sh install.sh   |   PC:  pip install numpy")
    sys.exit(1)

from sentinel.config import Config  # noqa: E402
from sentinel.engine import Engine  # noqa: E402
from sentinel.web import Api, Server, make_handler  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Sentinel MK1")
    ap.add_argument("--port", type=int, default=None, help="Web-Port (Standard aus config.json, sonst 8096)")
    ap.add_argument("--host", default=None, help="Adresse (Standard 0.0.0.0 = alle)")
    ap.add_argument("--data", default=None, help="anderer Ordner fuer config.json und data/")
    args = ap.parse_args()

    base = os.path.abspath(args.data) if args.data else BASE
    os.makedirs(base, exist_ok=True)
    from sentinel import sysinfo
    first = None
    if sysinfo.is_jetson():
        first = {"detection": {"model_size": 416, "interval_s": 0.3}, "storage": {"max_mb": 8000}}
    cfg = Config(os.path.join(base, "config.json"), first_run=first)
    srv_cfg = cfg.get("server")
    env_port = os.environ.get("SENTINEL_PORT")
    port = args.port or (int(env_port) if env_port else None) or int(srv_cfg.get("port", 8096))
    host = args.host or srv_cfg.get("host", "0.0.0.0")

    engine = Engine(base, cfg, VERSION)

    def restart():
        time.sleep(0.8)
        engine.log("Neustart angefordert")
        engine.shutdown()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    ctx = dict(port=port, port_locked=bool(args.port or env_port), restart=restart)
    api = Api(engine, ctx)
    try:
        httpd = Server((host, port), make_handler(engine, api))
    except OSError as e:
        print("FEHLER: Port %d ist belegt oder nicht nutzbar (%s)" % (port, e))
        sys.exit(2)
    engine.start()
    engine.log("Weboberflaeche: http://%s:%d/" % ("<router-ip>" if host == "0.0.0.0" else host, port))

    def stop(*_):
        engine.log("Beende ...")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        engine.shutdown()


if __name__ == "__main__":
    main()
