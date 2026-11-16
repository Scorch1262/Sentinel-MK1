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

import glob
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


def mount_point_of(path):
    """Einhaengepunkt des Dateisystems, auf dem path liegt."""
    p = os.path.realpath(path)
    while not os.path.ismount(p) and p != "/":
        p = os.path.dirname(p)
    return p


def blkid(dev_or_uuid, tag):
    """Wert aus blkid (UUID, TYPE, LABEL) fuer ein Geraet (/dev/..) oder eine UUID."""
    dev = dev_or_uuid
    if not dev.startswith("/dev/"):
        dev = dev_by_uuid(dev_or_uuid) or ""
    rc, out, _ = _run(["blkid", "-s", tag, "-o", "value", dev], 15)
    return out.strip() if rc == 0 else ""


def dev_by_uuid(uuid):
    """Geraetedatei zu einer UUID (ueber /dev/disk/by-uuid, sonst blkid -U)."""
    node = os.path.join("/dev/disk/by-uuid", uuid)
    if os.path.exists(node):
        return os.path.realpath(node)
    rc, out, _ = _run(["blkid", "-U", uuid], 15)
    out = out.strip()
    return out if rc == 0 and out.startswith("/dev/") and os.path.exists(out) else None


def uptime():
    try:
        return float(open("/proc/uptime").read().split()[0])
    except Exception:
        return 1e9


def usb_rescan(log=None):
    """USB neu erkennen - wie Abziehen und wieder Anstecken.
    Manche USB-3-Festplatten werden am Jetson Nano nach dem Einschalten nicht erkannt (Strom beim
    Start, Firmware des USB-3-Controllers). Ein kurzes Ab- und Wiederanmelden der USB-Root-Hubs
    zaehlt alle Geraete neu auf. Kurz betroffen sind alle USB-Geraete (z. B. ein WLAN-Stick)."""
    hubs = sorted(glob.glob("/sys/bus/usb/devices/usb*/authorized"))
    if not hubs:
        return False
    for h in hubs:
        try:
            with open(h, "w") as f:
                f.write("0")
        except OSError:
            pass
    time.sleep(2)
    for h in hubs:
        try:
            with open(h, "w") as f:
                f.write("1")
        except OSError:
            pass
    _run(["udevadm", "settle", "-t", "20"], 30)
    if log:
        log("USB neu erkannt (%d Hubs)" % len(hubs))
    return True


def protect_mountpoint(path):
    """Leeren Einhaengepunkt schreibschuetzen (chattr +i): Ist das Laufwerk nicht eingebunden,
    schlaegt Schreiben fehl, statt unbemerkt auf der SD-Karte/eMMC zu landen."""
    try:
        os.makedirs(path, exist_ok=True)
        if not os.path.ismount(path) and not os.listdir(path) and shutil.which("chattr"):
            _run(["chattr", "+i", path], 10)
    except OSError:
        pass


def system_disks():
    """Geraete, die das System selbst nutzt (/, /boot, Swap, Datenordner) - nie formatieren."""
    used = set()
    try:
        for line in open("/proc/mounts"):
            dev = line.split()[0]
            mp = line.split()[1]
            if dev.startswith("/dev/") and (mp in ("/", "/boot", "/boot/efi") or mp.startswith(("/usr", "/var", "/opt", "/home"))):
                used.add(os.path.realpath(dev))
        for line in open("/proc/swaps").readlines()[1:]:
            used.add(os.path.realpath(line.split()[0]))
    except OSError:
        pass
    return used


def list_block_devices():
    """Ganze Laufwerke (USB/SATA/NVMe) zum Formatieren; Systemspeicher ausgenommen."""
    rc, out, _ = _run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,TRAN,MODEL,VENDOR,MOUNTPOINT,FSTYPE,LABEL"], 15)
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    sysd = system_disks()
    mounts = {}
    try:
        for line in open("/proc/mounts"):
            dv, mp = line.split()[:2]
            if dv.startswith("/dev/"):
                mounts.setdefault(os.path.realpath(dv), []).append(mp.replace("\\040", " "))
    except OSError:
        pass
    res = []
    for d in data.get("blockdevices") or []:
        name = d.get("name") or ""
        skip = ("mmcblk", "zram", "ram", "mtdblock") + (() if os.environ.get("SENTINEL_TEST_LOOP") else ("loop",))
        if d.get("type") not in ("disk", "loop") or name.startswith(skip) or \
                (d.get("type") == "loop" and not os.environ.get("SENTINEL_TEST_LOOP")):
            continue
        parts = d.get("children") or []
        devs = ["/dev/" + name] + ["/dev/" + (c.get("name") or "") for c in parts]
        if any(os.path.realpath(x) in sysd for x in devs):
            continue
        # irgendwo anders als unter /media, /run/media oder Sentinels eigenem Einhaengepunkt eingebunden?
        # Dann gehoert es zum System bzw. wird anderweitig benutzt -> nicht anbieten
        if any(not mp.startswith(("/media/", "/run/media/", DISK_MNT, NAS_MNT))
               for x in devs for mp in mounts.get(os.path.realpath(x), [])):
            continue
        res.append(dict(dev="/dev/" + name, size=int(d.get("size") or 0), tran=d.get("tran") or "",
                        model=" ".join(x for x in ((d.get("vendor") or "").strip(), (d.get("model") or "").strip()) if x),
                        parts=[dict(dev="/dev/" + (c.get("name") or ""), fstype=c.get("fstype") or "",
                                    label=c.get("label") or "", size=int(c.get("size") or 0),
                                    mount=c.get("mountpoint") or "") for c in parts]))
    return res


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
            skip = ("mmcblk", "zram", "ram", "mtdblock") + (() if os.environ.get("SENTINEL_TEST_LOOP") else ("loop",))
            if name.startswith(skip) or d.get("type") not in ("part", "disk", "loop"):
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
        self.migrating = None       # Fortschritt eines Umzugs (dict) oder None
        self.mig_cancel = False
        self.formatting = None      # Fortschritt einer Formatierung (dict) oder None
        self.mnt = None             # Einhaengepunkt, der fuer den aktiven Ort eingebunden sein muss
        self.started = time.time()
        self.usb_resets = 0
        self.on_ready = None        # Rueckruf, wenn der Speicherort (wieder) verfuegbar ist
        protect_mountpoint(DISK_MNT)
        protect_mountpoint(NAS_MNT)
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

    def _mount_dev(self, uuid, mnt, fstype_hint=""):
        """Partition (UUID) an mnt einbinden - mit Dateisystem-Erkennung und Reparaturversuch."""
        dev = dev_by_uuid(uuid)
        if not dev:
            raise RuntimeError("Festplatte nicht gefunden (nicht angeschlossen oder noch nicht bereit)")
        if mounted_at(mnt):
            if os.path.realpath(mounted_at(mnt)) == dev:
                return mnt
            _run(["umount", "-l", mnt], 30)           # fremdes Laufwerk am Einhaengepunkt
        fstype = (blkid(dev, "TYPE") or fstype_hint or "").lower()
        os.makedirs(mnt, exist_ok=True)

        def attempt():
            opts = "noatime"
            if fstype in ("vfat", "exfat", "ntfs"):
                opts += ",uid=0,gid=0,umask=000"
            cmd = ["mount", "-o", opts]
            if fstype == "ntfs" and shutil.which("ntfs-3g"):
                cmd += ["-t", "ntfs-3g"]
            elif fstype:
                cmd += ["-t", fstype]
            return _run(cmd + [dev, mnt], 90)

        rc, _, err = attempt()
        if rc != 0 and "unknown filesystem type" not in err:
            # Nicht sauber getrennt (Strom weg)? Dateisystem pruefen/reparieren und erneut versuchen
            fix = {"ext2": ["e2fsck", "-p", dev], "ext3": ["e2fsck", "-p", dev], "ext4": ["e2fsck", "-p", dev],
                   "ntfs": ["ntfsfix", "-d", dev], "vfat": ["fsck.vfat", "-a", dev],
                   "exfat": ["exfatfsck", "-y", dev] if shutil.which("exfatfsck") else ["fsck.exfat", dev]}.get(fstype)
            if fix and shutil.which(fix[0]):
                self.log("Laufwerk %s ließ sich nicht einbinden - prüfe Dateisystem (%s) ..." % (dev, fstype))
                frc, fout, ferr = _run(fix, 1800)
                if fstype.startswith("ext") and frc >= 4:
                    _run(["e2fsck", "-y", dev], 1800)
                rc, _, err = attempt()
                if rc == 0:
                    self.log("Dateisystem auf %s repariert und eingebunden" % dev)
        if rc != 0:
            hint = ""
            if "unknown filesystem type" in err:
                hint = " - Dateisystem %s nicht unterstützt (Paket exfat-fuse/ntfs-3g fehlt?)" % fstype
            raise RuntimeError("Festplatte nicht einbindbar: %s%s"
                               % ((err.strip().splitlines() or [""])[0][:200] or rc, hint))
        return mnt

    def _mount_disk(self, t):
        uuid = (t.get("uuid") or "").strip()
        if not uuid:
            raise RuntimeError("keine Festplatte ausgewählt")
        # schon woanders eingebunden (z. B. Desktop-Automount)? Dann dort nutzen.
        dev = dev_by_uuid(uuid)
        if dev:
            try:
                for line in open("/proc/mounts"):
                    parts = line.split()
                    if os.path.realpath(parts[0]) == dev and parts[1] != DISK_MNT:
                        return parts[1].replace("\\040", " ")
            except OSError:
                pass
        return self._mount_dev(uuid, DISK_MNT, t.get("fstype") or "")

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
            mp = t.get("mount") or ""
            if t.get("uuid") and mp and not os.path.ismount(mp):
                # Ordner liegt auf einem Laufwerk, das (noch) nicht eingebunden ist -> selbst einbinden
                self._mount_dev(t["uuid"], mp, t.get("fstype") or "")
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
                typ = t.get("type", "local")
                mnt = None
                if typ in ("disk", "nas") or (typ == "path" and t.get("uuid")):
                    mnt = mount_point_of(root if os.path.exists(root) else os.path.dirname(root))
                    if mnt == "/":
                        raise RuntimeError("Laufwerk nicht eingebunden - schreibe nicht auf die Systemkarte")
                if not writable(os.path.join(root, "snaps")) or not writable(os.path.join(root, "clips")):
                    raise RuntimeError("%s ist nicht beschreibbar" % root)
                changed = root != self.root
                was_fallback = self.state == "ersatz"
                self.root = root
                self.mnt = mnt
                self.state = "lokal" if typ == "local" else "ok"
                self.error = ""
                if changed or initial:
                    if self.state != "lokal":
                        self.log("Speicherort: %s (%s frei)" % (root, _fmt(free_space(root)[0])))
                since = self.cfg.get("storage").get("fallback_since")
                if self.state == "ok" and since:
                    # waehrend des Ausfalls im Datenordner gespeicherte Dateien nachtraeglich verschieben
                    if was_fallback or initial:
                        self.log("Speicherort wieder da - verschiebe die zwischenzeitlich gespeicherten Dateien")
                    self.migrate_from([self.local_root], since=float(since) - 5,
                                      done=lambda: self.cfg.update("storage", dict(fallback_since=None)))
                elif self.state == "lokal" and since:
                    self.cfg.update("storage", dict(fallback_since=None))
                if (was_fallback or initial or changed) and self.on_ready:
                    threading.Thread(target=self.on_ready, daemon=True).start()
                return True, root
            except Exception as e:
                self.root = self.local_root
                self.mnt = None
                self.state = "ersatz" if t.get("type", "local") != "local" else "lokal"
                if self.state == "ersatz" and not self.cfg.get("storage").get("fallback_since"):
                    self.cfg.update("storage", dict(fallback_since=time.time()))
                if str(e) != self.error:
                    self.log("Speicherort nicht verfügbar: %s - speichere vorerst im Datenordner" % e)
                    if t.get("type") == "disk":
                        try:
                            seen = ", ".join("%s %s %s" % (d["dev"], d["fstype"], d["label"] or d["uuid"][:8])
                                             for d in list_disks()) or "keine"
                        except Exception:
                            seen = "?"
                        self.log("Gesucht: UUID %s - erkannte Laufwerke: %s" % (t.get("uuid"), seen))
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
            gone = (self.mnt and not os.path.ismount(self.mnt)) or not os.path.isdir(self.root)
            if gone or not writable(os.path.join(self.root, "snaps")):
                self.log("Speicherort %s nicht mehr erreichbar - speichere im Datenordner" % self.root)
                lost = self.mnt
                self.root = self.local_root
                self.mnt = None
                self.state = "ersatz"
                self.error = "nicht erreichbar"
                if lost in (DISK_MNT, NAS_MNT):
                    protect_mountpoint(lost)
                if not self.cfg.get("storage").get("fallback_since"):
                    self.cfg.update("storage", dict(fallback_since=time.time()))
        else:
            # nach dem Einschalten melden sich USB-Laufwerke oft erst spaet: anfangs alle 10 s versuchen
            wait = 10 if time.time() - self.started < 900 else 60
            if time.time() - self.last_try > wait:
                self.apply()
            # Festplatte nach dem Einschalten nicht aufgetaucht? USB einmal neu erkennen lassen
            t = self._target()
            el = time.time() - self.started
            if self.state == "ersatz" and t.get("type") == "disk" and t.get("uuid") and \
                    not dev_by_uuid(t["uuid"]) and uptime() < 1200 and \
                    ((self.usb_resets == 0 and el > 40) or (self.usb_resets == 1 and el > 180)):
                self.usb_resets += 1
                self.log("Festplatte nach dem Start nicht gefunden - lasse USB neu erkennen (Versuch %d/2)"
                         % self.usb_resets)
                if usb_rescan(self.log):
                    time.sleep(5)
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
        """Nicht mehr benoetigte Einbindungen loesen (nach einem Umzug weg vom Laufwerk)."""
        typ = self._target().get("type", "local")
        for mnt, need in ((DISK_MNT, "disk"), (NAS_MNT, "nas")):
            if typ != need and mounted_at(mnt) and not self.root.startswith(mnt):
                _run(["umount", "-l", mnt], 30)
                protect_mountpoint(mnt)
                olds = [r for r in (self.cfg.get("storage").get("old_roots") or []) if not r.startswith(mnt)]
                self.cfg.update("storage", dict(old_roots=olds))

    def info(self):
        free, total = free_space(self.root)
        t = self._target()
        t.pop("password", None)
        st = self.cfg.get("storage")
        return dict(root=self.root, state=self.state, error=self.error, free=free, total=total, target=t,
                    migrating=self.migrating, last_migration=getattr(self, "last_migration", None),
                    formatting=self.formatting, loop=bool(st.get("loop")),
                    loop_reserve_gb=st.get("loop_reserve_gb", 2))

    # -------------------------------------------------------------- Umzug
    def migrate_from(self, sources, done=None, since=None):
        """Bilder/Videos aus anderen Orten in den aktiven Ort verschieben (Hintergrund, mit Fortschritt).
        sources: Liste von Wurzelordnern; since: nur Dateien, die seitdem geschrieben wurden."""
        if isinstance(sources, str):
            sources = [sources]
        sources = [x for x in sources if x and os.path.realpath(x) != os.path.realpath(self.root)]
        if self.migrating or not sources:
            return False
        dest_root = self.root
        self.mig_cancel = False
        self.migrating = dict(phase="zählt", files_total=0, files_done=0, bytes_total=0, bytes_done=0,
                              started=time.time(), current="", errors=0, dest=dest_root)

        def run():
            m = self.migrating
            jobs = []
            try:
                for src_root in sources:
                    for kind in ("snaps", "clips"):
                        src = os.path.join(src_root, kind)
                        for dp, _, files in os.walk(src):
                            for fn in files:
                                if fn.endswith(".part"):
                                    continue
                                sp = os.path.join(dp, fn)
                                try:
                                    st = os.stat(sp)
                                except OSError:
                                    continue
                                if since and st.st_mtime < since:
                                    continue
                                jobs.append((sp, os.path.join(dest_root, kind, os.path.relpath(sp, src)), st.st_size))
                m["files_total"] = len(jobs)
                m["bytes_total"] = sum(j[2] for j in jobs)
                m["phase"] = "kopiert"
                buf = bytearray(1 << 20)
                for sp, dp, size in jobs:
                    if self.mig_cancel:
                        m["phase"] = "abgebrochen"
                        break
                    if self.root != dest_root:
                        m["phase"] = "Ziel nicht mehr erreichbar"
                        break
                    m["current"] = os.path.basename(sp)
                    try:
                        os.makedirs(os.path.dirname(dp), exist_ok=True)
                        if os.path.exists(dp) and os.path.getsize(dp) == size:
                            os.remove(sp)
                        else:
                            try:
                                os.rename(sp, dp)                  # gleiches Dateisystem: sofort
                            except OSError:
                                tmp = dp + ".part"
                                with open(sp, "rb") as fi, open(tmp, "wb") as fo:
                                    mv = memoryview(buf)
                                    while True:
                                        n = fi.readinto(buf)
                                        if not n:
                                            break
                                        fo.write(mv[:n])
                                        m["bytes_done"] += n
                                        if self.mig_cancel:
                                            break
                                if self.mig_cancel:
                                    os.remove(tmp)
                                    m["phase"] = "abgebrochen"
                                    break
                                shutil.copystat(sp, tmp)
                                os.rename(tmp, dp)
                                os.remove(sp)
                                m["files_done"] += 1
                                continue
                        m["bytes_done"] += size
                        m["files_done"] += 1
                    except Exception:
                        m["errors"] += 1
                if m["phase"] == "kopiert":
                    m["phase"] = "fertig"
                self.log("Umzug %s: %d von %d Dateien (%s) nach %s%s" % (
                    m["phase"], m["files_done"], m["files_total"], _fmt(m["bytes_done"]), dest_root,
                    (", %d Fehler" % m["errors"]) if m["errors"] else ""))
            except Exception as e:
                m["phase"] = "Fehler: %s" % e
                self.log("Umzug: %s" % e)
            finally:
                m["finished"] = time.time()
                self.last_migration = dict(m)
                self.migrating = None
                if done and m["phase"] == "fertig":
                    try:
                        done()
                    except Exception:
                        pass
        threading.Thread(target=run, daemon=True).start()
        return True

    def cancel_migration(self):
        self.mig_cancel = True

    def pending_elsewhere(self):
        """Bilder/Videos, die noch in anderen Orten liegen: (anzahl, bytes) - fuer den Knopf 'hierher holen'."""
        n = b = 0
        for r in self.roots()[1:]:
            if os.path.realpath(r) == os.path.realpath(self.root):
                continue
            for kind in ("snaps", "clips"):
                for dp, _, files in os.walk(os.path.join(r, kind)):
                    for f in files:
                        if f.endswith(".part"):
                            continue
                        try:
                            b += os.path.getsize(os.path.join(dp, f))
                            n += 1
                        except OSError:
                            pass
        return n, b

    # -------------------------------------------------------------- Formatieren
    def format_disk(self, dev, fs="ext4", label="SENTINEL", use_after=True):
        """Ganzes Laufwerk neu einteilen (GPT, eine Partition) und formatieren - im Hintergrund.
        ALLE Daten auf dem Laufwerk gehen verloren. Systemspeicher wird abgelehnt."""
        if self.formatting and not self.formatting.get("finished"):
            raise RuntimeError("Es läuft bereits eine Formatierung")
        devs = {d["dev"]: d for d in list_block_devices()}
        if dev not in devs:
            raise RuntimeError("Laufwerk %s nicht gefunden oder Systemspeicher" % dev)
        fs = fs if fs in ("ext4", "exfat") else "ext4"
        mk = {"ext4": "mkfs.ext4", "exfat": "mkfs.exfat"}[fs]
        if not shutil.which(mk):
            raise RuntimeError("%s fehlt (Paket %s)" % (mk, "e2fsprogs" if fs == "ext4" else "exfat-utils"))
        if not shutil.which("sfdisk"):
            raise RuntimeError("sfdisk fehlt (Paket util-linux)")
        info = devs[dev]
        st = self.formatting = dict(dev=dev, fs=fs, step="vorbereiten", pct=0, error="", finished=None,
                                    model=info.get("model"), size=info.get("size"))

        def step(name, pct):
            st["step"], st["pct"] = name, pct

        def run():
            try:
                # 1. Wird das Laufwerk gerade benutzt? Dann auf den Datenordner ausweichen und loesen
                step("Laufwerk lösen", 5)
                if self.mnt and any(os.path.realpath(p["dev"]) == os.path.realpath(mounted_at(self.mnt) or "")
                                    for p in info["parts"]):
                    with self.lock:
                        self.root, self.mnt, self.state = self.local_root, None, "ersatz"
                        self.error = "wird formatiert"
                for p in info["parts"]:
                    if p["mount"]:
                        _run(["umount", "-l", p["mount"]], 60)
                cur = mounted_at(DISK_MNT)
                if cur and any(os.path.realpath(cur) == os.path.realpath(p["dev"]) for p in info["parts"]):
                    _run(["umount", "-l", DISK_MNT], 30)
                # 2. Alte Signaturen loeschen, neue Partitionstabelle (GPT) mit einer Partition
                step("Partitionstabelle anlegen", 15)
                _run(["wipefs", "-a", dev], 60)
                rc, _, err = _run(["sfdisk", "--wipe", "always", dev], 120, inp=b"label: gpt\n,,L\n") \
                    if self._sfdisk_wipe() else _run(["sfdisk", dev], 120, inp=b"label: gpt\n,,L\n")
                if rc != 0:
                    raise RuntimeError("Partitionieren fehlgeschlagen: %s" % (err.strip().splitlines() or [""])[-1])
                _run(["partx", "-u", dev], 30)
                _run(["udevadm", "settle"], 60)
                part = dev + ("p1" if dev[-1].isdigit() else "1")
                for _ in range(40):
                    if os.path.exists(part):
                        break
                    time.sleep(0.5)
                if not os.path.exists(part):
                    raise RuntimeError("Partition %s erscheint nicht" % part)
                _run(["wipefs", "-a", part], 60)
                # 3. Dateisystem anlegen
                step("Dateisystem %s anlegen (kann einige Minuten dauern)" % fs, 35)
                if fs == "ext4":
                    cmd = ["mkfs.ext4", "-F", "-L", label[:16], "-m", "0",
                           "-E", "lazy_itable_init=1,lazy_journal_init=1", part]
                else:
                    cmd = ["mkfs.exfat", "-n", label[:11], part]
                rc, out, err = _run(cmd, 3600)
                if rc != 0:
                    raise RuntimeError("Formatieren fehlgeschlagen: %s" % ((err or out).strip().splitlines() or [""])[-1])
                _run(["udevadm", "settle"], 60)
                step("prüfen", 85)
                uuid = ""
                for _ in range(20):
                    uuid = blkid(part, "UUID")
                    if uuid and dev_by_uuid(uuid):
                        break
                    time.sleep(0.5)
                if not uuid:
                    raise RuntimeError("Neue Partition hat keine UUID")
                st["uuid"] = uuid
                st["part"] = part
                # 4. Als Speicherort verwenden
                if use_after:
                    step("als Speicherort einbinden", 92)
                    self.cfg.update("storage", dict(target=None))
                    self.cfg.update("storage", dict(target=dict(type="disk", uuid=uuid, fstype=fs)))
                    ok, msg = self.apply()
                    if not ok:
                        raise RuntimeError("formatiert, aber nicht einbindbar: %s" % msg)
                step("fertig", 100)
                self.log("Laufwerk %s formatiert (%s, %s)%s" % (dev, fs, uuid, " und als Speicherort aktiv" if use_after else ""))
            except Exception as e:
                st["error"] = str(e)
                st["step"] = "Fehler"
                self.log("Formatieren von %s: %s" % (dev, e))
                self.apply()
            finally:
                st["finished"] = time.time()
        threading.Thread(target=run, daemon=True).start()
        return st

    def _sfdisk_wipe(self):
        """sfdisk kennt --wipe erst ab util-linux 2.28"""
        rc, out, _ = _run(["sfdisk", "--version"], 10)
        try:
            v = [int(x) for x in out.strip().split()[-1].split(".")[:2]]
            return v >= [2, 28]
        except Exception:
            return False

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


def topic_name(name):
    """Name fuer ein MQTT-Topic: / + # und Leerzeichen ersetzen"""
    out = "".join("_" if c in "/+#" or c.isspace() else c for c in (name or "").strip())
    return out.strip("_") or "unbenannt"


class SpaceMonitor(object):
    """Fuellstand aller verfuegbaren Speicherorte, jede Sekunde im Hintergrund gemessen.

    Orte: Datenordner, aktiver Speicherort, eingebundene externe Laufwerke (USB/SATA/NVMe),
    eingebundener Netzwerkspeicher. Gleiche Dateisysteme werden zusammengefasst."""

    def __init__(self, media, log, interval=1.0):
        self.media = media
        self.log = log
        self.interval = interval
        self.data = []
        self.ts = 0.0
        self._disks = ([], 0.0)
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _candidates(self):
        m = self.media
        t = m._target()
        typ = t.get("type", "local")
        out = [dict(name="Datenordner", kind="data", path=m.local_root)]
        if typ != "local":
            nm = {"disk": "USB-Festplatte", "nas": "Netzwerkspeicher", "path": "Eigener Ordner"}.get(typ, typ)
            if typ == "disk":
                for d in self._disk_list():
                    if d["uuid"] == t.get("uuid") and d["label"]:
                        nm = d["label"]
            elif typ == "nas":
                nm = "NAS %s" % (t.get("share") or "").strip("/").split("/")[-1] if t.get("share") else nm
            elif typ == "path":
                nm = os.path.basename((t.get("path") or "").rstrip("/")) or nm
            path = m.root if m.state == "ok" else {"disk": DISK_MNT, "nas": NAS_MNT}.get(typ, t.get("path") or "")
            out.append(dict(name=nm, kind=typ, path=path, target=True))
        for d in self._disk_list():
            if d["mount"]:
                out.append(dict(name=d["label"] or d["model"] or os.path.basename(d["dev"]), kind="usb" if d["tran"] == "usb" else "disk",
                                path=d["mount"]))
        if mounted_at(NAS_MNT) and typ != "nas":
            out.append(dict(name="Netzwerkspeicher", kind="nas", path=NAS_MNT))
        return out

    def _disk_list(self):
        lst, ts = self._disks
        if time.time() - ts > 30:
            try:
                lst = list_disks()
            except Exception:
                lst = []
            self._disks = (lst, time.time())
        return lst

    def sample(self):
        res, seen = [], {}
        active_root = self.media.root
        for c in self._candidates():
            p = c["path"]
            item = dict(name=c["name"], key=topic_name(c["name"]), kind=c["kind"], path=p,
                        active=False, reachable=False, total=None, free=None, used=None, pct=None)
            try:
                st = os.statvfs(p)
                dev = os.stat(p).st_dev
                total = st.f_blocks * st.f_frsize
                free = st.f_bavail * st.f_frsize
                used = total - st.f_bfree * st.f_frsize
                item.update(reachable=total > 0, total=total, free=free, used=used,
                            pct=round(100.0 * used / max(1, used + free), 1) if total else None)
            except OSError:
                dev = None
            if c.get("target") and self.media.state == "ok":
                item["active"] = True
            if c["kind"] == "data" and active_root == self.media.local_root:
                item["active"] = True
            if dev is not None and dev in seen:
                prev = seen[dev]                      # dasselbe Dateisystem: zusammenfassen
                if item["active"]:
                    prev["active"] = True
                prev["name"] += " · " + item["name"]       # Topic-Name (key) bleibt der erste
                continue
            if dev is not None:
                seen[dev] = item
            res.append(item)
        return res

    def _loop(self):
        while True:
            t0 = time.time()
            try:
                d = self.sample()
                with self.lock:
                    self.data, self.ts = d, time.time()
            except Exception as e:
                self.log("Speichermessung: %s" % e)
            time.sleep(max(0.2, self.interval - (time.time() - t0)))

    def get(self):
        with self.lock:
            return [dict(x) for x in self.data], self.ts


def _fmt(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024.0
