"""Updates direkt aus dem GitHub-Repo (Standard: Scorch1262/Sentinel-MK1, Zweig main).

Ablauf beim Update:
  1. Archiv des Zweigs von codeload.github.com laden und pruefen (app.py, sentinel/, static/, VERSION)
  2. aktuelle Programmdateien nach data/backup/<version>/ sichern
  3. neue Programmdateien kopieren - config.json und data/ bleiben unberuehrt
  4. optional update_hook.py der neuen Version ausfuehren (z. B. fuer neue Pakete)
  5. Programm neu starten
Startet die neue Version zweimal nicht (Absturz direkt nach dem Start), stellt app.py
(update_guard) automatisch die Sicherung wieder her.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request

PROGRAM_FILES = ["app.py", "VERSION", "requirements.txt", "install-jetson.sh", "README.md",
                 "LICENSE", "CHANGELOG.md", "MANIFEST", "update_hook.py", ".gitignore"]
PROGRAM_DIRS = ["sentinel", "static", "docs"]
OBSOLETE_FILES = ["install.sh"]   # gehoerte zu frueheren Versionen (GL.iNet Brume 2) - wird beim Update entfernt


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:4])


def read_version(base):
    try:
        return open(os.path.join(base, "VERSION")).read().strip()
    except Exception:
        return ""


class Updater:
    def __init__(self, base, data_dir, cfg, version, log, restart):
        self.base = base                    # Programmordner (app.py)
        self.data_dir = data_dir
        self.cfg = cfg
        self.version = version
        self.log = log
        self.restart = restart
        self.state = dict(remote="", remote_sha="", checked=0, available=False, busy=False, step="",
                          error="", changes="", last_result="")
        self.lock = threading.Lock()
        self.state_file = os.path.join(data_dir, "update_state.json")
        self._load_result()

    # ------------------------------------------------------------------
    def _ucfg(self):
        u = self.cfg.get("update")
        repo = (u.get("repo") or "Scorch1262/Sentinel-MK1").strip().strip("/")
        repo = re.sub(r"^https?://github\.com/", "", repo).replace(".git", "")
        return repo, (u.get("branch") or "main").strip(), u

    def _token(self):
        for p in (os.path.join(self.data_dir, "github_token"), os.path.join(self.base, "github_token")):
            try:
                t = open(p).read().strip()
                if t:
                    return t
            except Exception:
                pass
        return ""

    def _get(self, url, timeout=30, accept=None):
        h = {"User-Agent": "Sentinel-MK1/%s" % self.version}
        if accept:
            h["Accept"] = accept
        tok = self._token()
        if tok and ("api.github.com" in url or "codeload" in url or "raw.githubusercontent" in url):
            h["Authorization"] = "token " + tok
        req = urllib.request.Request(url, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            # Rueckfall: curl (z. B. aeltere SSL-Bibliotheken)
            if shutil.which("curl"):
                cmd = ["curl", "-fsSL", "--retry", "2", "--max-time", str(timeout * 4)]
                for k, v in h.items():
                    cmd += ["-H", "%s: %s" % (k, v)]
                p = subprocess.run(cmd + [url], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if p.returncode == 0:
                    return p.stdout
            raise e

    # ------------------------------------------------------------------
    def check(self):
        """Neueste Version auf GitHub ermitteln."""
        repo, br, _ = self._ucfg()
        try:
            raw = self._get("https://raw.githubusercontent.com/%s/%s/VERSION?t=%d" % (repo, br, int(time.time())),
                            timeout=20)
            remote = raw.decode("utf-8", "replace").strip()
            if not re.match(r"^\d+(\.\d+)*$", remote):
                raise ValueError("VERSION im Repo ungueltig: %r" % remote[:40])
            sha = ""
            try:
                sha = self._get("https://api.github.com/repos/%s/commits/%s" % (repo, br), timeout=15,
                                accept="application/vnd.github.sha").decode()[:40]
            except Exception:
                pass
            changes = ""
            try:
                cl = self._get("https://raw.githubusercontent.com/%s/%s/CHANGELOG.md" % (repo, br),
                               timeout=15).decode("utf-8", "replace")
                changes = self._changes_since(cl, self.version)
            except Exception:
                pass
            self.state.update(remote=remote, remote_sha=sha, checked=time.time(), error="", changes=changes,
                              available=vtuple(remote) > vtuple(self.version))
            if self.state["available"]:
                self.log("Update verfuegbar: v%s -> v%s (GitHub %s)" % (self.version, remote, repo))
        except Exception as e:
            self.state.update(checked=time.time(), error="Prüfung fehlgeschlagen: %s" % e)
        return self.public()

    @staticmethod
    def _changes_since(text, current):
        """Abschnitte '## x.y.z' der CHANGELOG.md, die neuer als die laufende Version sind."""
        out, keep = [], False
        for line in text.splitlines():
            m = re.match(r"^##\s*v?(\d+(?:\.\d+)*)", line)
            if m:
                keep = vtuple(m.group(1)) > vtuple(current)
            if keep:
                out.append(line)
        return "\n".join(out).strip()[:6000]

    def public(self):
        repo, br, u = self._ucfg()
        d = dict(self.state)
        d.update(version=self.version, repo=repo, branch=br, auto_check=bool(u.get("auto_check", True)))
        return d

    # ------------------------------------------------------------------
    def start_update(self, force=False):
        with self.lock:
            if self.state["busy"]:
                return False
            self.state.update(busy=True, error="", step="startet")
        threading.Thread(target=self._run, args=(force,), daemon=True).start()
        return True

    def _step(self, s):
        self.state["step"] = s
        self.log("Update: " + s)

    def _run(self, force):
        repo, br, _ = self._ucfg()
        tmp = os.path.join(self.data_dir, "update_tmp")
        try:
            self._step("lade Version von GitHub (%s, Zweig %s)" % (repo, br))
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp)
            src = self._fetch_archive(repo, br, tmp) or self._fetch_files(repo, br, tmp)
            for need in ("app.py", "VERSION", "sentinel", "static"):
                if not os.path.exists(os.path.join(src, need)):
                    raise ValueError("%s fehlt im Repo" % need)
            new_ver = read_version(src)
            if vtuple(new_ver) < vtuple(self.version) and not os.environ.get("SENTINEL_ALLOW_DOWNGRADE"):
                raise ValueError("GitHub hat eine ältere Version (v%s) - kein Downgrade" % new_ver)
            if not force and vtuple(new_ver) <= vtuple(self.version):
                raise ValueError("GitHub hat keine neuere Version (v%s)" % new_ver)
            # Syntaxpruefung der neuen Python-Dateien mit dem laufenden Interpreter
            self._step("prüfe neue Version v%s" % new_ver)
            files = [os.path.join(src, "app.py")] + [os.path.join(src, "sentinel", f)
                                                     for f in os.listdir(os.path.join(src, "sentinel"))
                                                     if f.endswith(".py")]
            for f in files:
                compile(open(f, "rb").read(), f, "exec")
            # Sicherung
            self._step("sichere v%s" % self.version)
            bdir = os.path.join(self.data_dir, "backup", "v%s" % self.version)
            shutil.rmtree(bdir, ignore_errors=True)
            os.makedirs(bdir)
            for f in PROGRAM_FILES + OBSOLETE_FILES:
                if os.path.isfile(os.path.join(self.base, f)):
                    shutil.copy2(os.path.join(self.base, f), os.path.join(bdir, f))
            for d in PROGRAM_DIRS:
                if os.path.isdir(os.path.join(self.base, d)):
                    shutil.copytree(os.path.join(self.base, d), os.path.join(bdir, d),
                                    ignore=shutil.ignore_patterns("__pycache__"))
            self._prune_backups()
            # Installieren
            self._step("installiere v%s" % new_ver)
            install_tree(src, self.base)
            for sh in ("install-jetson.sh",):
                p = os.path.join(self.base, sh)
                if os.path.exists(p):
                    os.chmod(p, 0o755)
            hook = os.path.join(self.base, "update_hook.py")
            if os.path.exists(hook):
                self._step("führe update_hook.py aus")
                r = subprocess.run([sys.executable, hook, self.version, new_ver], cwd=self.base,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800)
                for line in r.stdout.decode("utf-8", "replace").splitlines()[-20:]:
                    self.log("  hook: " + line)
            with open(self.state_file, "w") as f:
                json.dump(dict(status="pending", from_version=self.version, to_version=new_ver, backup=bdir,
                               attempts=0, ts=time.time()), f)
            self._step("fertig - starte neu mit v%s" % new_ver)
            shutil.rmtree(tmp, ignore_errors=True)
            time.sleep(1)
            self.restart()
        except Exception as e:
            self.state.update(error="Update fehlgeschlagen: %s" % e, busy=False, step="")
            self.log("Update fehlgeschlagen: %s" % e)
            shutil.rmtree(tmp, ignore_errors=True)

    def _fetch_archive(self, repo, br, tmp):
        """Ganzes Repo als tar.gz (codeload / github.com / eigener Spiegel)."""
        _, _, u = self._ucfg()
        urls = []
        if (u.get("archive_url") or "").strip():
            urls.append(u["archive_url"].strip())
        urls += ["https://codeload.github.com/%s/tar.gz/refs/heads/%s" % (repo, br),
                 "https://github.com/%s/archive/refs/heads/%s.tar.gz" % (repo, br)]
        for url in urls:
            try:
                data = self._get(url, timeout=60)
                if len(data) < 2000:
                    continue
                self._step("entpacke Archiv")
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                    for m in tf.getmembers():
                        n = os.path.normpath(m.name)
                        if n.startswith("..") or os.path.isabs(n) or m.issym() or m.islnk():
                            continue
                        tf.extract(m, tmp)
                tops = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
                if len(tops) == 1:
                    return os.path.join(tmp, tops[0])
            except Exception as e:
                self.log("Update: Archiv %s nicht ladbar (%s)" % (url.split("/")[2], e))
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp)
        return None

    def _raw_base(self, repo, br):
        return os.environ.get("SENTINEL_RAW_BASE") or "https://raw.githubusercontent.com/%s/%s/" % (repo, br)

    def _fetch_files(self, repo, br, tmp):
        """Rueckfall ohne Archiv: Dateiliste (GitHub-API, sonst MANIFEST im Repo) und Einzeldateien laden."""
        names = []
        try:
            tree = json.loads(self._get("https://api.github.com/repos/%s/git/trees/%s?recursive=1" % (repo, br),
                                        timeout=30).decode("utf-8"))
            names = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"]
        except Exception:
            pass
        if not names:
            raw = self._get(self._raw_base(repo, br) + "MANIFEST", timeout=30).decode("utf-8", "replace")
            names = [l.strip() for l in raw.splitlines() if l.strip() and not l.startswith("#")]
        names = [n for n in names if n in PROGRAM_FILES or n.split("/")[0] in PROGRAM_DIRS or n == "MANIFEST"]
        if "app.py" not in names:
            raise ValueError("Dateiliste ohne app.py")
        dst = os.path.join(tmp, "repo")
        self._step("lade %d Dateien einzeln" % len(names))
        for n in names:
            if ".." in n.split("/"):
                continue
            data = self._get(self._raw_base(repo, br) + n, timeout=60)
            p = os.path.join(dst, *n.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
        return dst

    def _prune_backups(self, keep=3):
        root = os.path.join(self.data_dir, "backup")
        try:
            dirs = sorted(os.listdir(root), key=lambda d: os.path.getmtime(os.path.join(root, d)), reverse=True)
            for d in dirs[keep:]:
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _load_result(self):
        try:
            st = json.load(open(self.state_file))
        except Exception:
            return
        if st.get("status") == "ok":
            self.state["last_result"] = "Update auf v%s erfolgreich (%s)" % (
                st.get("to_version"), time.strftime("%d.%m.%Y %H:%M", time.localtime(st.get("ts", 0))))
        elif st.get("status") == "rolled_back":
            self.state["last_result"] = "Update auf v%s ist beim Start gescheitert - v%s wurde wiederhergestellt" % (
                st.get("to_version"), st.get("from_version"))

    def confirm_running(self):
        """Nach erfolgreichem Start der neuen Version aufrufen (Rueckfall-Zaehler loeschen)."""
        try:
            st = json.load(open(self.state_file))
        except Exception:
            return
        if st.get("status") == "pending" and st.get("to_version") == self.version:
            st.update(status="ok", ts=time.time())
            json.dump(st, open(self.state_file, "w"))
            self.log("Update auf v%s erfolgreich" % self.version)
            self._load_result()


def install_tree(src, dst):
    """Programmdateien aus src nach dst kopieren (config.json, data/, .pylibs bleiben)."""
    for d in PROGRAM_DIRS:
        s = os.path.join(src, d)
        t = os.path.join(dst, d)
        if os.path.isdir(s):
            new = t + ".neu"
            shutil.rmtree(new, ignore_errors=True)
            shutil.copytree(s, new, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
            old = t + ".alt"
            shutil.rmtree(old, ignore_errors=True)
            if os.path.isdir(t):
                os.rename(t, old)
            os.rename(new, t)
            shutil.rmtree(old, ignore_errors=True)
    for f in PROGRAM_FILES:
        s = os.path.join(src, f)
        if os.path.isfile(s):
            tmp = os.path.join(dst, f + ".neu")
            shutil.copy2(s, tmp)
            os.replace(tmp, os.path.join(dst, f))
    for f in OBSOLETE_FILES:
        if not os.path.exists(os.path.join(src, f)) and os.path.isfile(os.path.join(dst, f)):
            try:
                os.remove(os.path.join(dst, f))
            except OSError:
                pass
