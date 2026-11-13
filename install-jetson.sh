#!/bin/bash
# ============================================================================
#  Sentinel MK1 – Installer fuer NVIDIA Jetson Nano (2GB / 4GB), JetPack 4.6 (L4T R32.7)
#
#  Aufruf (im entpackten Ordner):
#     sudo bash install-jetson.sh                 installieren bzw. aktualisieren
#     sudo bash install-jetson.sh --port 8096     anderen Web-Port verwenden
#     sudo bash install-jetson.sh --ohne-desktop  grafische Oberflaeche abschalten (+ ca. 0,5-0,8 GB RAM)
#     sudo bash install-jetson.sh --mit-desktop   grafische Oberflaeche wieder einschalten
#     sudo bash install-jetson.sh --maxtakt       beim Start jetson_clocks (hoechste Taktraten)
#     sudo bash install-jetson.sh --ohne-swap     keine Auslagerungsdatei anlegen
#     sudo bash install-jetson.sh uninstall       Dienst entfernen (Daten bleiben erhalten)
#
#  Ohne entpackten Ordner (laedt alles von GitHub):
#     wget -O /tmp/install-jetson.sh https://raw.githubusercontent.com/Scorch1262/Sentinel-MK1/main/install-jetson.sh
#     sudo bash /tmp/install-jetson.sh
#
#  Nutzt die Hardware des Jetson:
#    * Video-Dekodierung + Skalierung in Hardware (GStreamer nvv4l2decoder / nvvidconv)
#    * Objekterkennung und Gesichtsmerkmale auf der GPU (TensorRT, FP16)
#    * 4 CPU-Kerne fuer Gesichtssuche, Verfolgung und Weboberflaeche
#
#  Ziel:   /opt/Sentinel-MK1 (Programm, config.json, data/)   Dienst: sentinel (systemd)
# ============================================================================
VERSION=1.2.0
REPO=Scorch1262/Sentinel-MK1
DIR=${SENTINEL_DIR:-/opt/Sentinel-MK1}
PORT=""
DESKTOP=ask
MAXCLOCK=0
SWAP=1
ACTION=install

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift ;;
        --port=*) PORT="${1#--port=}" ;;
        --ohne-desktop|--headless) DESKTOP=off ;;
        --mit-desktop) DESKTOP=on ;;
        --desktop-lassen) DESKTOP=keep ;;
        --maxtakt|--jetson-clocks) MAXCLOCK=1 ;;
        --ohne-swap) SWAP=0 ;;
        uninstall|--uninstall|deinstallieren) ACTION=uninstall ;;
        -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    esac
    shift
done

log()  { echo -e "\e[33m[Sentinel]\e[0m $*"; }
ok()   { echo -e "\e[32m[Sentinel]\e[0m $*"; }
fail() { echo -e "\e[31m[Sentinel] FEHLER:\e[0m $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || fail "bitte mit sudo ausfuehren:  sudo bash $0"
export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
if [ "$ACTION" = "uninstall" ]; then
    systemctl stop sentinel 2>/dev/null
    systemctl disable sentinel 2>/dev/null
    rm -f /etc/systemd/system/sentinel.service
    systemctl daemon-reload
    ok "Dienst entfernt. Programm und Daten liegen weiter in $DIR (zum Loeschen: sudo rm -rf $DIR)"
    exit 0
fi

if [ ! -f /etc/nv_tegra_release ]; then
    fail "kein NVIDIA Jetson erkannt (/etc/nv_tegra_release fehlt). Fuer den GL.iNet Brume 2: sh install.sh"
fi
L4T=$(head -n1 /etc/nv_tegra_release | sed -n 's/^# R\([0-9]*\) (release), REVISION: \([0-9.]*\).*/R\1.\2/p')
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)
log "Installation v$VERSION auf ${MODEL:-Jetson} (L4T ${L4T:-?}) nach $DIR"
case "$L4T" in R32*) ;; *) log "HINWEIS: getestet ist JetPack 4.6 (L4T R32.7) - andere Versionen sollten funktionieren" ;; esac

# ---------------------------------------------------------------------------
# 1) Quelle: entpackter Ordner neben diesem Skript oder GitHub
# ---------------------------------------------------------------------------
SRC=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
TMP=""
if [ ! -f "$SRC/app.py" ] || [ ! -d "$SRC/sentinel" ]; then
    command -v curl >/dev/null 2>&1 || apt-get install -y -q curl >/dev/null
    TMP=$(mktemp -d /tmp/sentinel.XXXXXX)
    log "Lade Programm von GitHub ($REPO) ..."
    got=0
    for br in main master; do
        if curl -fsSL --retry 3 -o "$TMP/src.tgz" "https://codeload.github.com/$REPO/tar.gz/refs/heads/$br"; then
            mkdir -p "$TMP/x" && tar -xzf "$TMP/src.tgz" -C "$TMP/x" && got=1 && break
        fi
    done
    [ $got = 1 ] || fail "Download von GitHub fehlgeschlagen"
    SRC=$(ls -d "$TMP"/x/*/ | head -n1); SRC=${SRC%/}
fi

# ---------------------------------------------------------------------------
# 2) Pakete (apt)
# ---------------------------------------------------------------------------
log "Installiere Pakete (apt) ..."
apt-get update -q >/dev/null 2>&1 || log "WARNUNG: apt-get update fehlgeschlagen (Internet?)"
PKGS="python3 python3-numpy ffmpeg curl libopenblas-base gstreamer1.0-tools gstreamer1.0-plugins-base
      gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav"
# shellcheck disable=SC2086
apt-get install -y -q --no-install-recommends $PKGS >/dev/null 2>&1 || {
    log "WARNUNG: nicht alle Pakete installierbar - einzeln:"
    for p in $PKGS; do apt-get install -y -q --no-install-recommends "$p" >/dev/null 2>&1 || log "  fehlt: $p"; done
}
python3 -c "import cv2" 2>/dev/null || {
    log "OpenCV fuer Python fehlt - installiere python3-opencv"
    apt-get install -y -q python3-opencv >/dev/null 2>&1 || log "WARNUNG: OpenCV nicht installierbar (Hardware-Dekoder dann nicht nutzbar)"
}
python3 -c "import tensorrt" 2>/dev/null || {
    log "TensorRT-Python fehlt - installiere python3-libnvinfer (JetPack-Paketquelle)"
    apt-get install -y -q python3-libnvinfer >/dev/null 2>&1 || log "WARNUNG: TensorRT nicht installierbar - Erkennung laeuft dann auf der CPU"
}
python3 -c "import numpy, sqlite3" 2>/dev/null || fail "python3-numpy bzw. sqlite3 fehlt"
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
TRTV=$(python3 -c "import tensorrt; print(tensorrt.__version__)" 2>/dev/null)
CVV=$(python3 -c "import cv2; print(cv2.__version__)" 2>/dev/null)
ok "Python $PYV · numpy $(python3 -c 'import numpy; print(numpy.__version__)') · OpenCV ${CVV:-fehlt} · TensorRT ${TRTV:-fehlt}"
gst-inspect-1.0 nvv4l2decoder >/dev/null 2>&1 && ok "Hardware-Dekoder nvv4l2decoder vorhanden" \
    || log "WARNUNG: nvv4l2decoder fehlt - Videos werden per ffmpeg auf der CPU dekodiert"

# ---------------------------------------------------------------------------
# 3) Auslagerungsspeicher (der Engine-Bau braucht beim 2-GB-Modell mehr als den RAM)
# ---------------------------------------------------------------------------
SWAPMB=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$SWAP" = "1" ] && [ "${SWAPMB:-0}" -lt 3000 ] && [ ! -f /var/sentinel.swap ]; then
    FREEMB=$(df -Pm /var | awk 'NR==2 {print $4}')
    if [ "${FREEMB:-0}" -gt 8000 ]; then
        log "Lege 4 GB Auslagerungsdatei an (/var/sentinel.swap) ..."
        fallocate -l 4G /var/sentinel.swap 2>/dev/null || dd if=/dev/zero of=/var/sentinel.swap bs=1M count=4096 status=none
        chmod 600 /var/sentinel.swap && mkswap /var/sentinel.swap >/dev/null && swapon /var/sentinel.swap \
            && { grep -q sentinel.swap /etc/fstab || echo "/var/sentinel.swap none swap sw 0 0" >> /etc/fstab; } \
            && ok "Auslagerungsdatei aktiv" || log "WARNUNG: Auslagerungsdatei konnte nicht angelegt werden"
    else
        log "WARNUNG: zu wenig freier Speicher fuer eine Auslagerungsdatei (${FREEMB} MB frei)"
    fi
fi

# ---------------------------------------------------------------------------
# 4) Programmdateien (config.json und data/ bleiben erhalten)
# ---------------------------------------------------------------------------
systemctl stop sentinel >/dev/null 2>&1
mkdir -p "$DIR"
if [ "$SRC" != "$DIR" ]; then
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
# 5) KI-Modelle (OpenCV Model Zoo)
# ---------------------------------------------------------------------------
ZOO=https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models
getmodel() {   # pfad datei groesse sha256
    local f="$DIR/data/models/$2"
    if [ -f "$f" ] && [ "$(stat -c %s "$f")" = "$3" ]; then return 0; fi
    log "Lade Modell $2 ..."
    curl -fsSL --retry 3 -o "$f.part" "$ZOO/$1/$2" || { rm -f "$f.part"; log "  WARNUNG: Download fehlgeschlagen (wird beim Start erneut versucht)"; return 1; }
    if [ "$(sha256sum "$f.part" | cut -d' ' -f1)" != "$4" ]; then rm -f "$f.part"; log "  WARNUNG: Pruefsumme falsch"; return 1; fi
    mv -f "$f.part" "$f"
}
getmodel object_detection_nanodet object_detection_nanodet_2022nov.onnx 3800954 4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186
getmodel face_detection_yunet face_detection_yunet_2023mar.onnx 232589 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
getmodel face_recognition_sface face_recognition_sface_2021dec.onnx 38696353 0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79

# ---------------------------------------------------------------------------
# 6) TensorRT-Engines bauen (GPU, FP16) - einmalig, je nach Modell 3-15 Minuten
# ---------------------------------------------------------------------------
TRTEXEC=/usr/src/tensorrt/bin/trtexec
if [ -n "$TRTV" ] && [ -x "$TRTEXEC" ]; then
    TV=$(echo "$TRTV" | tr '.' '_')
    build() {   # onnx engine-name
        local e="$DIR/data/models/$2_fp16_trt$TV.engine"
        [ -s "$e" ] && { ok "GPU-Engine $2 vorhanden"; return 0; }
        [ -f "$DIR/data/models/$1" ] || return 1
        log "Baue GPU-Engine $2 mit TensorRT $TRTV (bitte warten, dauert einige Minuten) ..."
        local t0=$(date +%s)
        if "$TRTEXEC" --onnx="$DIR/data/models/$1" --saveEngine="$e.part" --fp16 --workspace=256 \
                > "$DIR/data/models/$2.trtexec.log" 2>&1 && [ -s "$e.part" ]; then
            mv -f "$e.part" "$e"
            ok "GPU-Engine $2 fertig ($(( $(date +%s) - t0 )) s)"
            grep -E "mean = .* ms|GPU Compute Time" "$DIR/data/models/$2.trtexec.log" | tail -n1 | sed 's/^/           /'
        else
            rm -f "$e.part"
            log "WARNUNG: Engine $2 fehlgeschlagen - Protokoll: $DIR/data/models/$2.trtexec.log (Erkennung dann auf der CPU)"
        fi
    }
    build object_detection_nanodet_2022nov.onnx nanodet_416
    build face_recognition_sface_2021dec.onnx sface_112
else
    log "WARNUNG: TensorRT/trtexec fehlt - Objekterkennung laeuft auf der CPU (deutlich langsamer)"
fi

# ---------------------------------------------------------------------------
# 7) Port
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
import json
try: print(int(json.load(open('$DIR/config.json')).get('server',{}).get('port',8096)))
except Exception: print(8096)" 2>/dev/null)
[ -n "$PORT" ] || PORT=8096

# ---------------------------------------------------------------------------
# 8) Leistungsmodus: MAXN (10 W) und optional hoechste Taktraten
# ---------------------------------------------------------------------------
if command -v nvpmodel >/dev/null 2>&1; then
    nvpmodel -m 0 >/dev/null 2>&1 && ok "Leistungsmodus MAXN (nvpmodel 0)"
fi
PRE=""
if [ "$MAXCLOCK" = "1" ] && [ -x /usr/bin/jetson_clocks ]; then
    PRE="ExecStartPre=-/usr/bin/jetson_clocks"
    log "jetson_clocks wird bei jedem Start gesetzt (Luefter/Kuehlung beachten)"
fi

# ---------------------------------------------------------------------------
# 9) Grafische Oberflaeche: auf dem 2-GB-Modell besser aus (mehr RAM fuer Sentinel)
# ---------------------------------------------------------------------------
CUR=$(systemctl get-default 2>/dev/null)
MEMMB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$DESKTOP" = "ask" ] && [ "$CUR" = "graphical.target" ] && [ "${MEMMB:-4000}" -lt 3000 ] && [ -t 0 ]; then
    echo
    read -r -p "[Sentinel] Grafische Oberflaeche abschalten? Spart ca. 0,5-0,8 GB RAM (nach Neustart) [J/n] " a
    case "$a" in n|N|nein) DESKTOP=keep ;; *) DESKTOP=off ;; esac
fi
if [ "$DESKTOP" = "off" ] && [ "$CUR" != "multi-user.target" ]; then
    systemctl set-default multi-user.target >/dev/null && ok "Grafische Oberflaeche wird ab dem naechsten Neustart nicht mehr gestartet (zurueck: --mit-desktop)"
elif [ "$DESKTOP" = "on" ]; then
    systemctl set-default graphical.target >/dev/null && ok "Grafische Oberflaeche ab dem naechsten Neustart wieder aktiv"
fi

# ---------------------------------------------------------------------------
# 10) Dienst (systemd)
# ---------------------------------------------------------------------------
cat > /etc/systemd/system/sentinel.service <<UNIT_EOF
[Unit]
Description=Sentinel MK1 - Objekterkennung in RTSP-Streams
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1 OPENBLAS_CORETYPE=ARMV8
$PRE
ExecStart=/usr/bin/python3 $DIR/app.py
Restart=always
RestartSec=5
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload
systemctl enable sentinel >/dev/null 2>&1
systemctl restart sentinel

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$PORT/tcp" >/dev/null && ok "Firewall (ufw): Port $PORT freigegeben"
fi
[ -n "$TMP" ] && rm -rf "$TMP"

# ---------------------------------------------------------------------------
# 11) Pruefen
# ---------------------------------------------------------------------------
for i in $(seq 1 40); do
    ss -ltn 2>/dev/null | grep -q ":$PORT " && break
    sleep 1
done
echo
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    ok "Sentinel MK1 v$VERSION laeuft auf Port $PORT"
    for ip in $(hostname -I); do ok "   http://$ip:$PORT/"; done
else
    log "WARNUNG: Port $PORT noch nicht erreichbar. Protokoll: journalctl -u sentinel -n 50"
fi
log "Kameras unter Einstellungen -> Streams eintragen. Protokoll: journalctl -u sentinel -f"
[ "$DESKTOP" = "off" ] && [ "$CUR" = "graphical.target" ] && log "Fuer mehr freien RAM jetzt neu starten: sudo reboot"
exit 0
