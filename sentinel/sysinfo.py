"""Erkennung der Plattform (GL.iNet Brume 2 / NVIDIA Jetson / PC) und Systemwerte fuer die Kopfzeile."""
import glob
import os
import re

_CACHE = {}


def is_jetson():
    if "jetson" not in _CACHE:
        _CACHE["jetson"] = os.path.exists("/etc/nv_tegra_release") or os.path.exists("/sys/devices/gpu.0/load")
    return _CACHE["jetson"]


def is_openwrt():
    return os.path.exists("/etc/openwrt_release")


def model_name():
    for p in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            return open(p, "rb").read().decode("utf-8", "replace").strip("\x00 \n")
        except Exception:
            pass
    if is_openwrt():
        try:
            m = re.search(r"DISTRIB_DESCRIPTION='([^']*)'", open("/etc/openwrt_release").read())
            return m.group(1) if m else "OpenWrt"
        except Exception:
            return "OpenWrt"
    return ""


def l4t_version():
    try:
        t = open("/etc/nv_tegra_release").read()
        m = re.search(r"R(\d+).*?REVISION:\s*([\d.]+)", t)
        return "L4T R%s.%s" % (m.group(1), m.group(2)) if m else ""
    except Exception:
        return ""


def platform_name():
    if is_jetson():
        return model_name() or "NVIDIA Jetson"
    if is_openwrt():
        return model_name() or "OpenWrt"
    return model_name() or "PC"


def gpu_load():
    """Jetson-GPU-Auslastung in Prozent (Datei liefert 0..1000)."""
    for p in ("/sys/devices/gpu.0/load", "/sys/devices/platform/gpu.0/load"):
        try:
            return round(int(open(p).read().strip()) / 10.0)
        except Exception:
            pass
    return None


def temperature():
    """Hoechste Temperatur der Thermal-Zonen in Grad Celsius (ohne 'PMIC'-Dummy mit 100 Grad)."""
    best = None
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        try:
            typ = open(os.path.join(z, "type")).read().strip().lower()
            if "pmic" in typ:
                continue
            t = int(open(os.path.join(z, "temp")).read().strip()) / 1000.0
            if 0 < t < 120 and (best is None or t > best):
                best = t
        except Exception:
            pass
    return round(best) if best is not None else None


def memory():
    """(belegt MB, gesamt MB, Swap belegt MB)"""
    try:
        info = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            info[k] = int(v.strip().split()[0])
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        swap = info.get("SwapTotal", 0) - info.get("SwapFree", 0)
        return round((total - avail) / 1024.0), round(total / 1024.0), round(swap / 1024.0)
    except Exception:
        return None


def system_stats():
    m = memory()
    return dict(gpu=gpu_load(), temp=temperature(), mem_used=m[0] if m else None, mem_total=m[1] if m else None,
                swap_used=m[2] if m else None)
