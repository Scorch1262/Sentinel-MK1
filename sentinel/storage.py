# -*- coding: utf-8 -*-
"""Speicherort fuer Bilder und Videoschnipsel.

Arten (config storage.target):
  local  - Datenordner von Sentinel (Standard)
  path   - beliebiger Ordner, z. B. eine bereits eingebundene Festplatte (/media/...)
  disk   - USB-Festplatte/-Stick per UUID; Sentinel bindet sie selbst unter /mnt/sentinel-disk ein
  nas    - Netzwerkspeicher per SMB (Windows-Freigabe, Fritzbox, Synology ...) oder NFS;
           Sentinel bindet ihn unter /mnt/sentinel-nas ein

Ist das Ziel nicht erreichbar, wird automatisch in den Datenordner geschrieben (nichts geht verloren)
und regelmaessig erneut versucht. Gelesen wird immer aus allen bekannten Orten, damit aeltere
Bilder nach einem Wechsel sichtbar bleiben.
"""

import json
import os
import shutil
import subprocess
import threading
import time

DISK_MNT = "/mnt/sentinel-disk"
NAS_MNT = "/mnt/sentinel-nas"
SUBDIR = "Sentinel-MK1"


def _run(cmd, timeout=30, inp=None):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, input=inp)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "", "Zeitueberschreitung"
    except Exception as e:
        return 1, "", str(e)


def mounted_at(path):
    """Geraet/Quelle, die an path eingebunden ist, sonst None."""
    path = os.path.realpath(path)
    try:
        for line in open("/proc/mounts"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].replace("\\040", " ") == path:
                return parts[0]
    except OSError:
        pass
    return None


def free_space(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except OSError:
        return None, None


def writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        t = os.path.join(path, ".sentinel_test_%d" % os.getpid())
        with open(t, "wb") as f:
            f.write(b"ok")
        os.remove(t)
        return True
    except Exception:
        return False


def list_disks():
    """Externe Laufwerke/Partitionen (lsblk). Systemspeicher (eMMC/SD mit /) wird ausgelassen."""
    rc, out, _ = _run(["lsblk", "-J", "-b", "-o", "NAME,UUID,FSTYPE,SIZE,LABEL,MOUNTPOINT,TRAN,TYPE,MODEL"], 15)
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    res = []

    def walk(devs, parent):
        for d in devs or []:
            kids = d.get("children") or []
            tran = d.get("tran") or (parent or {}).get("tran")
            model = (d.get("model") or (parent or {}).get("model") or "").strip()
            if kids:
                walk(kids, d)
                continue
            name = d.get("name") or ""
            mp = d.get("mountpoint")
            if name.startswith(("mmcblk", "zram", "loop", "ram", "mtdblock")) or d.get("type") not in ("part", "disk"):
                continue
            if mp in ("/", "/boot", "[SWAP]") or (d.get("fstype") or "") in ("swap", ""):
                continue
            if not d.get("uuid"):
                continue
            size = int(d.get("size") or 0)
            res.append(dict(dev="/dev/" + name, uuid=d["uuid"], fstype=d.get("fstype"), size=size,
                            label=d.get("label") or "", mount=mp or "", tran=tran or "", model=model))
    walk(data.get("blockdevices"), None)
    return res


class MediaStore(object):
    def __init__(self, cfg, data_dir, log):
        self.cfg = cfg
        self.data_dir = data_dir
        self.log = log
        self.local_root = data_dir
        self.root = data_dir             # aktiver Wurzelordner (enthaelt snaps/ und clips/)
        self.state = "lokal"
        self.error = ""
        self.lock = threading.RLock()
        self.last_try = 0
        self.migrating = None
        self.apply(initial=True)

    # -------------------------------------------------------------- Pfade
    @property
    def snap_dir(self):
        return os.path.join(self.root, "snaps")

    @property
    def clip_dir(self):
        return os.path.join(self.root, "clips")

    def roots(self):
        """Alle Orte, in denen Dateien liegen koennen (aktiver zuerst)."""
        out = [self.root]
        if self.local_root not in out:
            out.append(self.local_root)
        for extra in self.cfg.get("storage").get("old_roots") or []:
            if extra not in out:
                out.append(extra)
        return out

    def find(self, kind, rel):
        """Vorhandene Datei (kind = 'snaps' | 'clips') in irgendeinem Ort, sonst Pfad im aktiven Ort."""
        rel = rel.lstrip("/")
        for r in self.roots():
            base = os.path.join(r, kind)
            p = os.path.normpath(os.path.join(base, rel))
            if not p.startswith(base + os.sep):
                return None
            if os.path.exists(p):
                return p
        return os.path.join(self.root, kind, rel)

    def remove(self, kind, rel):
        for r in self.roots():
            p = os.path.normpath(os.path.join(r, kind, rel))
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    # -------------------------------------------------------------- Einbinden
    def _target(self):
        return dict(self.cfg.get("storage").get("target") or {"type": "local"})

    def _mount_disk(self, t):
        uuid = (t.get("uuid") or "").strip()
        if not uuid:
            raise RuntimeError("keine Festplatte ausgewählt")
        dev = mounted_at(DISK_MNT)
        if dev:
            return DISK_MNT
        # schon woanders eingebunden (z. B. Desktop-Automount)? Dann dort nutzen.
        for d in list_disks():
            if d["uuid"] == uuid and d["mount"]:
                return d["mount"]
        os.makedirs(DISK_MNT, exist_ok=True)
        fstype = (t.get("fstype") or "").lower()
        opts = "noatime"
        if fstype in ("vfat", "exfat", "ntfs"):
            opts += ",uid=0,gid=0,umask=000"
        cmd = ["mount", "-o", opts, "UUID=" + uuid, DISK_MNT]
        if fstype == "ntfs" and shutil.which("ntfs-3g"):
            cmd = ["mount", "-t", "ntfs-3g", "-o", opts, "UUID=" + uuid, DISK_MNT]
        rc, _, err = _run(cmd, 60)
        if rc != 0:
            hint = ""
            if "unknown filesystem type" in err:
                hint = " - Dateisystem %s nicht unterstützt (Paket exfat-fuse/ntfs-3g fehlt?)" % fstype
            raise RuntimeError("Festplatte nicht einbindbar: %s%s" % ((err.strip().splitlines() or [""])[0][:200] or rc, hint))
        return DISK_MNT

    def _mount_nas(self, t):
        if mounted_at(NAS_MNT):
            return NAS_MNT
        os.makedirs(NAS_MNT, exist_ok=True)
        proto = (t.get("proto") or "smb").lower()
        server = (t.get("server") or "").strip().strip("/\\")
        share = (t.get("share") or "").strip().strip("/\\").replace("\\", "/")
        if not server or not share:
            raise RuntimeError("Server und Freigabe angeben")
        if proto == "nfs":
            rc, _, err = _run(["mount", "-t", "nfs", "-o", "soft,timeo=100,retrans=2,noatime",
                               "%s:/%s" % (server, share), NAS_MNT], 60)
        else:
            cred = os.path.join(self.data_dir, ".nas_credentials")
            with open(cred, "w") as f:
                f.write("username=%s\npassword=%s\n" % (t.get("user") or "guest", t.get("password") or ""))
                if t.get("domain"):
                    f.write("domain=%s\n" % t["domain"])
            os.chmod(cred, 0o600)
            err = ""
            rc = 1
            # Versionen nacheinander probieren (Fritzbox/aeltere NAS koennen nur 2.0/1.0)
            for ver in ([t["smb_version"]] if t.get("smb_version") else ["3.0", "2.1", "2.0", "1.0"]):
                opts = "credentials=%s,vers=%s,uid=0,gid=0,file_mode=0666,dir_mode=0777,soft,noserverino" % (cred, ver)
                if not t.get("user"):
                    opts += ",guest"
                rc, _, err = _run(["mount", "-t", "cifs", "//%s/%s" % (server, share), NAS_MNT, "-o", opts], 60)
                if rc == 0:
                    break
                if "Permission denied" in err or "error(13)" in err:
                    break
        if rc != 0:
            e = (err.strip().splitlines() or [""])[0]
            hint = ""
            if "wrong fs type" in e or "unknown filesystem" in e:
                hint = " - Paket %s fehlt (install-jetson.sh erneut ausführen)" % (
                    "nfs-common" if proto == "nfs" else "cifs-utils")
            elif "Permission denied" in e or "error(13)" in e:
                hint = " - Benutzer/Passwort prüfen"
            elif "No such file" in e or "error(2)" in e:
                hint = " - Freigabename prüfen"
            elif "Host is down" in e or "error(112)" in e or "timed out" in e:
                hint = " - Server nicht erreichbar"
            raise RuntimeError("Netzwerkspeicher nicht einbindbar: %s%s" % (e[:200] or rc, hint))
        return NAS_MNT

    def _resolve(self, t):
        typ = t.get("type", "local")
        if typ == "local":
            return self.local_root
        if typ == "path":
            p = (t.get("path") or "").strip()
            if not p.startswith("/"):
                raise RuntimeError("Pfad muss mit / beginnen")
            if not os.path.isdir(p):
                raise RuntimeError("Ordner %s existiert nicht (Laufwerk eingebunden?)" % p)
            return os.path.join(p, SUBDIR)
        if typ == "disk":
            return os.path.join(self._mount_disk(t), SUBDIR)
        if typ == "nas":
            sub = (t.get("subdir") or SUBDIR).strip("/") or SUBDIR
            return os.path.join(self._mount_nas(t), sub)
        raise RuntimeError("unbekannte Speicherart %s" % typ)

    def apply(self, initial=False):
        """Ziel aus der Konfiguration einbinden/aktivieren. -> (ok, meldung)"""
        with self.lock:
            t = self._target()
            self.last_try = time.time()
            try:
                root = self._resolve(t)
                if not writable(os.path.join(root, "snaps")) or not writable(os.path.join(root, "clips")):
                    raise RuntimeError("%s ist nicht beschreibbar" % root)
                changed = root != self.root
                self.root = root
                self.state = "lokal" if t.get("type", "local") == "local" else "ok"
                self.error = ""
                if changed or initial:
                    if self.state != "lokal":
                        self.log("Speicherort: %s (%s frei)" % (root, _fmt(free_space(root)[0])))
                return True, root
            except Exception as e:
                self.root = self.local_root
                self.state = "ersatz" if t.get("type", "local") != "local" else "lokal"
                if str(e) != self.error:
                    self.log("Speicherort nicht verfügbar: %s - speichere vorerst im Datenordner" % e)
                self.error = str(e)
                for k in ("snaps", "clips"):
                    os.makedirs(os.path.join(self.local_root, k), exist_ok=True)
                return False, str(e)

    def check(self):
        """Regelmaessig: Ziel noch da? Sonst auf lokal wechseln bzw. erneut einbinden."""
        t = self._target()
        if t.get("type", "local") == "local":
            return
        if self.state == "ok":
            if not writable(os.path.join(self.root, "snaps")):
                self.log("Speicherort %s nicht mehr erreichbar - speichere im Datenordner" % self.root)
                self.root = self.local_root
                self.state = "ersatz"
                self.error = "nicht erreichbar"
        elif time.time() - self.last_try > 120:
            self.apply()

    def test(self, t):
        """Ziel testen, ohne es zu uebernehmen. -> dict"""
        try:
            root = self._resolve(dict(t))
            ok = writable(os.path.join(root, "snaps"))
            free, total = free_space(root)
            return dict(ok=ok, root=root, free=free, total=total,
                        msg=("beschreibbar, %s frei von %s" % (_fmt(free), _fmt(total))) if ok
                        else "%s ist nicht beschreibbar" % root)
        except Exception as e:
            return dict(ok=False, msg=str(e))

    def unmount_unused(self):
        """Nicht mehr benoetigte Einbindungen loesen (nach Wechsel des Ziels)."""
        typ = self._target().get("type", "local")
        for mnt, need in ((DISK_MNT, "disk"), (NAS_MNT, "nas")):
            if typ != need and mounted_at(mnt) and not self.root.startswith(mnt):
                _run(["umount", "-l", mnt], 30)

    def info(self):
        free, total = free_space(self.root)
        t = self._target()
        t.pop("password", None)
        st = self.cfg.get("storage")
        return dict(root=self.root, state=self.state, error=self.error, free=free, total=total, target=t,
                    migrating=self.migrating, loop=bool(st.get("loop")),
                    loop_reserve_gb=st.get("loop_reserve_gb", 2))

    # -------------------------------------------------------------- Umzug
    def migrate_from(self, old_root, done=None):
        """Vorhandene Bilder/Clips aus old_root in den aktiven Ort verschieben (Hintergrund)."""
        if self.migrating or old_root == self.root:
            return False

        def run():
            moved = 0
            try:
                for kind in ("snaps", "clips"):
                    src = os.path.join(old_root, kind)
                    for dp, _, files in os.walk(src):
                        for fn in files:
                            s = os.path.join(dp, fn)
                            d = os.path.join(self.root, kind, os.path.relpath(s, src))
                            try:
                                os.makedirs(os.path.dirname(d), exist_ok=True)
                                shutil.move(s, d)
                                moved += 1
                                if moved % 50 == 0:
                                    self.migrating = "%d Dateien verschoben" % moved
                            except Exception:
                                pass
                self.log("Umzug abgeschlossen: %d Dateien nach %s" % (moved, self.root))
            finally:
                self.migrating = None
                if done:
                    done()
        self.migrating = "startet"
        threading.Thread(target=run, daemon=True).start()
        return True

    def usage(self):
        """Belegter Platz (Bytes) von Bildern und Clips in allen Orten."""
        total = 0
        for r in self.roots():
            for kind in ("snaps", "clips"):
                for dp, _, files in os.walk(os.path.join(r, kind)):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(dp, f))
                        except OSError:
                            pass
        return total


def _fmt(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024.0
