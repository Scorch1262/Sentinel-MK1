#!/bin/sh
# ============================================================================
#  Sentinel MK1 – Installer fuer GL.iNet Brume 2 (GL-MT2500 / OpenWrt)
#
#  Aufruf (per SSH als root, im entpackten Ordner):
#     sh install.sh                 installieren bzw. aktualisieren
#     sh install.sh --port 8096     anderen Web-Port verwenden
#     sh install.sh --ohne-firewall keine Firewall-Regel fuer den WAN-Port
#     sh install.sh uninstall       Dienst entfernen (Daten bleiben erhalten)
#
#  Ohne entpackten Ordner (laedt alles von GitHub):
#     wget -O /tmp/install.sh https://raw.githubusercontent.com/Scorch1262/Sentinel-MK1/main/install.sh
#     sh /tmp/install.sh
#
#  Ziel:      /root/Sentinel-MK1  (alle Programmdateien, config.json, data/)
#  Dienst:    /etc/init.d/sentinel  (procd, Autostart, Neustart bei Absturz)
#  Kompatibel mit dem Netzwerk-Core MK1 (Startdatei "@install.sh", ID "sentinel").
#  config.json und data/ (Datenbank, Bilder, Modelle) bleiben bei Updates erhalten.
# ============================================================================
VERSION=1.3.0
REPO=Scorch1262/Sentinel-MK1
DIR=${SENTINEL_DIR:-/root/Sentinel-MK1}
PORT=""
FIREWALL=1
ACTION=install

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift ;;
        --port=*) PORT="${1#--port=}" ;;
        --ohne-firewall) FIREWALL=0 ;;
        uninstall|--uninstall|deinstallieren) ACTION=uninstall ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    esac
    shift
done

# NVIDIA Jetson? -> eigener Installer (apt, GPU/TensorRT, Hardware-Dekoder, systemd)
if [ -f /etc/nv_tegra_release ]; then
    D=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
    if [ -f "$D/install-jetson.sh" ]; then
        exec bash "$D/install-jetson.sh" "$@"
    fi
    { wget -q -O /tmp/install-jetson.sh "https://raw.githubusercontent.com/$REPO/main/install-jetson.sh" 2>/dev/null \
      || python3 -c "import urllib.request as u; open('/tmp/install-jetson.sh','wb').write(u.urlopen('https://raw.githubusercontent.com/$REPO/main/install-jetson.sh', timeout=60).read())"; } \
        && exec bash /tmp/install-jetson.sh "$@"
    echo "[Sentinel] FEHLER: install-jetson.sh nicht gefunden"; exit 1
fi

log()  { echo "[Sentinel] $*"; }
fail() { echo "[Sentinel] FEHLER: $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || fail "bitte als root ausfuehren"

# Download mit dem, was da ist: curl -> wget -> Python (auf frischen Systemen fehlt curl oft)
fetch() {   # url ziel
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 --connect-timeout 20 -o "$2" "$1" && return 0
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -q -T 30 -O "$2" "$1" && [ -s "$2" ] && return 0
    fi
    python3 - "$1" "$2" <<'PYEOF'
import sys, urllib.request
req = urllib.request.Request(sys.argv[1], headers={"User-Agent": "Sentinel-MK1-Installer"})
with urllib.request.urlopen(req, timeout=60) as r, open(sys.argv[2], "wb") as f:
    while True:
        b = r.read(1 << 16)
        if not b:
            break
        f.write(b)
PYEOF
}


# ---------------------------------------------------------------------------
if [ "$ACTION" = "uninstall" ]; then
    /etc/init.d/sentinel stop 2>/dev/null
    /etc/init.d/sentinel disable 2>/dev/null
    rm -f /etc/init.d/sentinel
    if command -v uci >/dev/null 2>&1 && uci -q get firewall.sentinel_mk1 >/dev/null; then
        uci -q delete firewall.sentinel_mk1; uci commit firewall; /etc/init.d/firewall reload >/dev/null 2>&1
    fi
    log "Dienst entfernt. Programm und Daten liegen weiter in $DIR (zum Loeschen: rm -rf $DIR)"
    exit 0
fi

log "Installation v$VERSION nach $DIR"

# ---------------------------------------------------------------------------
# 1) Quelle: entpackter Ordner neben diesem Skript oder GitHub
# ---------------------------------------------------------------------------
SRC=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
TMP=""
if [ ! -f "$SRC/app.py" ] || [ ! -d "$SRC/sentinel" ]; then
    command -v curl >/dev/null 2>&1 || { opkg update >/dev/null 2>&1; opkg install curl ca-bundle >/dev/null 2>&1; }
    TMP=$(mktemp -d /tmp/sentinel.XXXXXX)
    log "Lade Programm von GitHub ($REPO) ..."
    ok=0
    for br in main master; do
        if fetch "https://codeload.github.com/$REPO/tar.gz/refs/heads/$br" "$TMP/src.tgz"; then
            mkdir -p "$TMP/x" && tar -xzf "$TMP/src.tgz" -C "$TMP/x" && ok=1 && break
        fi
    done
    [ $ok = 1 ] || fail "Download von GitHub fehlgeschlagen"
    SRC=$(ls -d "$TMP"/x/*/ | head -n1); SRC=${SRC%/}
fi

# ---------------------------------------------------------------------------
# 2) Pakete (opkg): Python, pip, SQLite, ffmpeg
# ---------------------------------------------------------------------------
if command -v opkg >/dev/null 2>&1; then
    log "Pruefe Pakete (opkg) ..."
    need=""
    for p in python3 python3-pip python3-sqlite3 python3-openssl ffmpeg curl ca-bundle libstdcpp; do
        opkg list-installed 2>/dev/null | grep -q "^$p " || need="$need $p"
    done
    if [ -n "$need" ]; then
        n=0
        until opkg update >/dev/null 2>&1; do
            n=$((n+1)); [ $n -ge 4 ] && { log "WARNUNG: opkg update fehlgeschlagen (Internet?)"; break; }
            sleep 8
        done
        for p in $need; do
            log "  installiere $p"
            opkg install "$p" >/dev/null 2>&1 || log "  WARNUNG: $p nicht installierbar"
        done
    fi
else
    log "Kein opkg gefunden - es wird angenommen, dass python3, pip und ffmpeg vorhanden sind"
fi
command -v python3 >/dev/null 2>&1 || fail "python3 fehlt"
command -v ffmpeg >/dev/null 2>&1 || log "WARNUNG: ffmpeg fehlt - ohne ffmpeg koennen keine Streams empfangen werden"
python3 -c "import sqlite3" 2>/dev/null || fail "Python-Modul sqlite3 fehlt (opkg install python3-sqlite3)"

# ---------------------------------------------------------------------------
# 3) Programmdateien kopieren (config.json und data/ bleiben erhalten)
# ---------------------------------------------------------------------------
mkdir -p "$DIR"
if [ "$SRC" != "$DIR" ]; then
    /etc/init.d/sentinel stop >/dev/null 2>&1
    rm -rf "$DIR/sentinel" "$DIR/static"
    for f in app.py VERSION requirements.txt install.sh install-jetson.sh README.md LICENSE CHANGELOG.md; do
        [ -f "$SRC/$f" ] && cp -f "$SRC/$f" "$DIR/$f"
    done
    cp -r "$SRC/sentinel" "$DIR/sentinel"
    cp -r "$SRC/static" "$DIR/static"
    [ -d "$SRC/docs" ] && { rm -rf "$DIR/docs"; cp -r "$SRC/docs" "$DIR/docs"; }
fi
mkdir -p "$DIR/data/models"
find "$DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null

# ---------------------------------------------------------------------------
# 4) numpy (fertiges Paket fuer musl/aarch64) in $DIR/.pylibs
# ---------------------------------------------------------------------------
if PYTHONPATH="$DIR/.pylibs" python3 -c "import numpy" 2>/dev/null; then
    log "numpy vorhanden: $(PYTHONPATH="$DIR/.pylibs" python3 -c 'import numpy; print(numpy.__version__)')"
else
    log "Installiere numpy (pip, fertiges Paket) ..."
    # aeltere pip-Versionen (OpenWrt 21.02) kennen die musl-Pakete noch nicht
    python3 -m pip install --no-cache-dir --disable-pip-version-check --upgrade pip >/dev/null 2>&1
    if ! python3 -m pip install --no-cache-dir --disable-pip-version-check --only-binary=:all: \
            --target "$DIR/.pylibs" numpy; then
        log "pip fehlgeschlagen - versuche opkg python3-numpy"
        opkg install python3-numpy >/dev/null 2>&1
    fi
    PYTHONPATH="$DIR/.pylibs" python3 -c "import numpy" 2>/dev/null || fail "numpy konnte nicht installiert werden"
fi

# ---------------------------------------------------------------------------
# 5) KI-Modelle (OpenCV Model Zoo) - laedt das Programm sonst beim ersten Start
# ---------------------------------------------------------------------------
ZOO=https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models
getmodel() {   # pfad datei groesse sha256
    f="$DIR/data/models/$2"
    if [ -f "$f" ] && [ "$(wc -c < "$f" | tr -d ' ')" = "$3" ]; then return 0; fi
    log "Lade Modell $2 ..."
    fetch "$ZOO/$1/$2" "$f.part" || { rm -f "$f.part"; log "  WARNUNG: Download fehlgeschlagen (wird beim Start erneut versucht)"; return 1; }
    if command -v sha256sum >/dev/null 2>&1 && [ "$(sha256sum "$f.part" | cut -d' ' -f1)" != "$4" ]; then
        rm -f "$f.part"; log "  WARNUNG: Pruefsumme falsch"; return 1
    fi
    mv -f "$f.part" "$f"
}
getmodel object_detection_nanodet object_detection_nanodet_2022nov.onnx 3800954 4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186
getmodel face_detection_yunet face_detection_yunet_2023mar.onnx 232589 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
getmodel face_recognition_sface face_recognition_sface_2021dec.onnx 38696353 0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79

# ---------------------------------------------------------------------------
# 6) Port: Parameter > config.json > 8096
# ---------------------------------------------------------------------------
if [ -n "$PORT" ]; then
    python3 - "$DIR/config.json" "$PORT" <<'PYEOF'
import json, os, sys
f, p = sys.argv[1], int(sys.argv[2])
d = {}
if os.path.exists(f):
    try:
        d = json.loads(open(f, "rb").read().decode("utf-8-sig"))
    except Exception:
        d = {}
d.setdefault("server", {})["port"] = p
open(f, "w", encoding="utf-8").write(json.dumps(d, indent=2, ensure_ascii=False))
PYEOF
fi
PORT=$(python3 -c "
import json,sys
try: print(int(json.load(open('$DIR/config.json')).get('server',{}).get('port',8096)))
except Exception: print(8096)" 2>/dev/null)
[ -n "$PORT" ] || PORT=8096

# ---------------------------------------------------------------------------
# 7) Autostart (procd)
# ---------------------------------------------------------------------------
if [ -d /etc/init.d ] && [ -f /etc/rc.common ]; then
    cat > /etc/init.d/sentinel <<INIT_EOF
#!/bin/sh /etc/rc.common
# Sentinel MK1 - Objekterkennung in RTSP-Streams
START=96
STOP=10
USE_PROCD=1
start_service() {
    procd_open_instance sentinel
    procd_set_param command /bin/sh -c '[ -r /etc/TZ ] && export TZ="\$(cat /etc/TZ)"; cd "$DIR" && exec /usr/bin/python3 app.py'
    procd_set_param env PYTHONPATH="$DIR/.pylibs" PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2
    procd_set_param respawn 3600 5 0
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}
INIT_EOF
    chmod +x /etc/init.d/sentinel
    /etc/init.d/sentinel enable
    /etc/init.d/sentinel stop >/dev/null 2>&1
    sleep 1
    /etc/init.d/sentinel start
else
    log "Kein procd gefunden - Start von Hand: cd $DIR && PYTHONPATH=.pylibs python3 app.py"
fi

# ---------------------------------------------------------------------------
# 8) Firewall: Webseite auch ueber den WAN-Port erreichbar
# ---------------------------------------------------------------------------
if [ "$FIREWALL" = "1" ] && command -v uci >/dev/null 2>&1; then
    uci -q delete firewall.sentinel_mk1
    uci set firewall.sentinel_mk1=rule
    uci set firewall.sentinel_mk1.name='Allow-Sentinel-MK1'
    uci set firewall.sentinel_mk1.src='wan'
    uci set firewall.sentinel_mk1.proto='tcp'
    uci set firewall.sentinel_mk1.dest_port="$PORT"
    uci set firewall.sentinel_mk1.target='ACCEPT'
    uci commit firewall
    /etc/init.d/firewall reload >/dev/null 2>&1
fi

[ -n "$TMP" ] && rm -rf "$TMP"

# ---------------------------------------------------------------------------
# 9) Pruefen
# ---------------------------------------------------------------------------
i=0
while [ $i -lt 20 ]; do
    netstat -lnt 2>/dev/null | grep -q ":$PORT " && break
    i=$((i+1)); sleep 1
done
IPS=$(ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -v '^127\.' | tr '\n' ' ')
echo
if netstat -lnt 2>/dev/null | grep -q ":$PORT "; then
    log "OK - Sentinel MK1 v$VERSION laeuft auf Port $PORT"
    for ip in $IPS; do log "   http://$ip:$PORT/"; done
else
    log "WARNUNG: Port $PORT noch nicht erreichbar. Log: logread -e sentinel | tail -n 30"
fi
log "Kameras unter Einstellungen -> Streams eintragen (RTSP-Substream empfohlen)."
exit 0
