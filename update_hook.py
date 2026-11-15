# -*- coding: utf-8 -*-
"""Wird nach einem Update ueber die Weboberflaeche ausgefuehrt (alte_version neue_version).

v2.4.0: Pakete zum Einbinden von Netzwerkspeichern (SMB/NFS) und USB-Laufwerken (exFAT/NTFS)
nachinstallieren. Fehlende Pakete sind kein Fehler - das Update laeuft trotzdem weiter.
"""
import os
import shutil
import subprocess
import sys

PKGS = ["cifs-utils", "nfs-common", "ntfs-3g", "exfat-fuse", "exfat-utils"]
NEED = {"cifs-utils": "/sbin/mount.cifs", "nfs-common": "/sbin/mount.nfs", "ntfs-3g": "/bin/ntfs-3g",
        "exfat-fuse": "/sbin/mount.exfat-fuse"}


def main():
    if os.geteuid() != 0 or not shutil.which("apt-get"):
        print("Paketinstallation übersprungen (kein root/apt)")
        return
    missing = [p for p in PKGS if p not in NEED or not os.path.exists(NEED[p])]
    missing = [p for p in missing if subprocess.run(["dpkg", "-s", p], stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL).returncode != 0]
    if not missing:
        print("Speicher-Pakete vorhanden")
        return
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    for p in missing:
        try:
            r = subprocess.run(["apt-get", "install", "-y", "-q", "--no-install-recommends", p], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
            print("%s: %s" % (p, "installiert" if r.returncode == 0 else "nicht installierbar"))
        except Exception as e:
            print("%s: %s" % (p, e))


if __name__ == "__main__":
    print("update_hook %s" % " -> ".join(sys.argv[1:3]))
    main()
