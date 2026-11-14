# -*- coding: utf-8 -*-
"""Stream-Diagnose: prueft eine RTSP-Kamera Schritt fuer Schritt direkt vom Geraet aus.

Ergebnis ist eine Liste von Schritten {name, ok, detail}; ok ist True, False oder None (nur Info).
Geprueft wird: Netzwerkverbindung, RTSP OPTIONS/DESCRIBE (Status und SDP), Datenempfang ueber
TCP (interleaved) und UDP (wie VLC) sowie ffprobe mit beiden Transportwegen.
"""

import base64
import hashlib
import json
import re
import socket
import subprocess
import time

try:
    from urllib.parse import urlsplit, unquote
except ImportError:  # pragma: no cover
    from urlparse import urlsplit, unquote

from .streams import find_tool, mask_url

UA = "Sentinel-MK1"

STATUS_HINT = {
    401: "Benutzername/Passwort fehlen oder sind falsch",
    403: "Zugriff verweigert",
    404: "Pfad nicht gefunden - stimmt der Teil nach dem Port (z. B. /stream)?",
    453: "Kamera hat keine freien Verbindungen (zu viele Clients) - VLC/andere Viewer schließen",
    454: "Sitzung unbekannt",
    461: "Transportart wird nicht unterstützt",
    503: "Kamera ist noch nicht bereit oder ausgelastet (z. B. Encoder liefert noch keine Parameter)",
}


class Rtsp(object):
    def __init__(self, url, timeout=6.0):
        u = urlsplit(url)
        self.host = u.hostname
        self.port = u.port or 554
        self.user = unquote(u.username) if u.username else None
        self.pw = unquote(u.password) if u.password else ""
        net = self.host + ("" if u.port is None else ":%d" % u.port)
        self.url = "rtsp://%s%s%s" % (net, u.path or "/", ("?" + u.query) if u.query else "")
        self.timeout = timeout
        self.sock = None
        self.cseq = 0
        self.auth = None      # (art, params)
        self.session = None
        self.buf = b""

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def _auth_header(self, method, uri):
        if not self.auth or not self.user:
            return ""
        kind, prm = self.auth
        if kind == "basic":
            tok = base64.b64encode(("%s:%s" % (self.user, self.pw)).encode()).decode()
            return "Authorization: Basic %s\r\n" % tok
        realm, nonce = prm.get("realm", ""), prm.get("nonce", "")
        ha1 = hashlib.md5(("%s:%s:%s" % (self.user, realm, self.pw)).encode()).hexdigest()
        ha2 = hashlib.md5(("%s:%s" % (method, uri)).encode()).hexdigest()
        resp = hashlib.md5(("%s:%s:%s" % (ha1, nonce, ha2)).encode()).hexdigest()
        return ('Authorization: Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s"\r\n'
                % (self.user, realm, nonce, uri, resp))

    def _read_response(self):
        """Antwort lesen; dazwischen liegende interleaved-Pakete ('$') werden uebersprungen."""
        deadline = time.time() + self.timeout
        while True:
            while self.buf[:1] == b"$":
                if len(self.buf) < 4:
                    break
                ln = (self.buf[2] << 8) | self.buf[3]
                if len(self.buf) < 4 + ln:
                    break
                self.buf = self.buf[4 + ln:]
            i = self.buf.find(b"\r\n\r\n")
            if i >= 0 and self.buf[:1] != b"$":
                head = self.buf[:i].decode("utf-8", "replace")
                m = re.search(r"(?im)^content-length:\s*(\d+)", head)
                cl = int(m.group(1)) if m else 0
                if len(self.buf) >= i + 4 + cl:
                    body = self.buf[i + 4:i + 4 + cl].decode("utf-8", "replace")
                    self.buf = self.buf[i + 4 + cl:]
                    lines = head.split("\r\n")
                    st = re.match(r"RTSP/\d\.\d\s+(\d+)\s*(.*)", lines[0] or "")
                    hdr = {}
                    for l in lines[1:]:
                        if ":" in l:
                            k, v = l.split(":", 1)
                            hdr[k.strip().lower()] = v.strip()
                    return (int(st.group(1)) if st else 0), (st.group(2) if st else lines[0]), hdr, body
            if time.time() > deadline:
                raise socket.timeout("keine Antwort")
            self.sock.settimeout(max(0.2, deadline - time.time()))
            d = self.sock.recv(65536)
            if not d:
                raise IOError("Verbindung von der Kamera geschlossen")
            self.buf += d

    def request(self, method, uri=None, extra=""):
        uri = uri or self.url
        for _ in range(2):
            self.cseq += 1
            msg = "%s %s RTSP/1.0\r\nCSeq: %d\r\nUser-Agent: %s\r\n%s%s%s\r\n" % (
                method, uri, self.cseq, UA, self._auth_header(method, uri),
                ("Session: %s\r\n" % self.session) if self.session else "", extra)
            self.sock.sendall(msg.encode())
            code, reason, hdr, body = self._read_response()
            if code == 401 and self.user and not self.auth:
                wa = hdr.get("www-authenticate", "")
                if wa.lower().startswith("digest"):
                    self.auth = ("digest", dict(re.findall(r'(\w+)="?([^",]*)"?', wa)))
                else:
                    self.auth = ("basic", {})
                continue
            return code, reason, hdr, body
        return code, reason, hdr, body


def _sdp_info(sdp, base):
    """Codec, Payload-Typ und Steuer-URL der ersten Videospur."""
    codec, pt, ctrl, inv = None, None, None, False
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            inv = line.startswith("m=video")
            if inv:
                parts = line.split()
                pt = parts[3] if len(parts) > 3 else None
                if pt == "26":
                    codec = "JPEG"
        elif inv and line.startswith("a=rtpmap:"):
            m = re.match(r"a=rtpmap:(\d+)\s+([^/]+)", line)
            if m:
                codec = m.group(2)
        elif inv and line.startswith("a=control:") and ctrl is None:
            ctrl = line[10:].strip()
    if not ctrl or ctrl == "*":
        url = base
    elif "://" in ctrl:
        url = ctrl
    else:
        url = base.rstrip("/") + "/" + ctrl
    return codec, pt, url


def _setup_play(url, desc_base, transport, seconds=4.0):
    """SETUP + PLAY mit gewuenschtem Transport; zaehlt eingehende RTP-Pakete. -> (ok, text)"""
    r = Rtsp(url)
    us = []
    try:
        r.connect()
        code, reason, hdr, sdp = r.request("DESCRIBE", extra="Accept: application/sdp\r\n")
        if code != 200:
            return False, "DESCRIBE %d %s" % (code, reason)
        base = hdr.get("content-base") or desc_base
        codec, pt, track = _sdp_info(sdp, base)
        if transport == "tcp":
            tr = "RTP/AVP/TCP;unicast;interleaved=0-1"
        else:
            for _ in range(10):
                a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                a.bind(("", 0))
                p = a.getsockname()[1]
                if p % 2:
                    a.close()
                    continue
                b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    b.bind(("", p + 1))
                except Exception:
                    a.close()
                    b.close()
                    continue
                us = [a, b]
                break
            if not us:
                return False, "keine freien UDP-Ports"
            tr = "RTP/AVP;unicast;client_port=%d-%d" % (us[0].getsockname()[1], us[0].getsockname()[1] + 1)
        code, reason, hdr, _ = r.request("SETUP", track, "Transport: %s\r\n" % tr)
        if code != 200:
            return False, "SETUP %d %s%s" % (code, reason, (" - " + STATUS_HINT[code]) if code in STATUS_HINT else "")
        r.session = (hdr.get("session") or "").split(";")[0] or None
        code, reason, hdr, _ = r.request("PLAY", base, "Range: npt=0.000-\r\n")
        if code != 200:
            return False, "PLAY %d %s" % (code, reason)
        n, nbytes, t0 = 0, 0, time.time()
        while time.time() - t0 < seconds:
            if transport == "tcp":
                r.sock.settimeout(0.5)
                try:
                    d = r.sock.recv(65536)
                except socket.timeout:
                    continue
                if not d:
                    break
                r.buf += d
                while len(r.buf) >= 4 and r.buf[:1] == b"$":
                    ln = (r.buf[2] << 8) | r.buf[3]
                    if len(r.buf) < 4 + ln:
                        break
                    if r.buf[1] == 0:
                        n += 1
                        nbytes += ln
                    r.buf = r.buf[4 + ln:]
                if r.buf and r.buf[:1] != b"$":
                    # Text (z. B. Antwort) ueberspringen
                    j = r.buf.find(b"$")
                    r.buf = r.buf[j:] if j >= 0 else b""
            else:
                us[0].settimeout(0.5)
                try:
                    d = us[0].recv(65536)
                except socket.timeout:
                    continue
                n += 1
                nbytes += len(d)
        try:
            r.request("TEARDOWN", base)
        except Exception:
            pass
        dt = max(0.1, time.time() - t0)
        if n == 0:
            return False, "SETUP/PLAY ok, aber in %.0f s keine Videodaten empfangen" % dt
        return True, "%d RTP-Pakete, %.0f kbit/s (%s, PT %s)" % (n, nbytes * 8 / dt / 1000.0, codec or "?", pt or "?")
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    finally:
        r.close()
        for u in us:
            u.close()


def _ffprobe(url, transport, timeout=15):
    fp = find_tool("ffprobe")
    if not fp:
        return None, "ffprobe nicht installiert"
    cmd = [fp, "-v", "error", "-rtsp_transport", transport, "-select_streams", "v:0",
           "-show_entries", "stream=codec_name,width,height", "-of", "json", url]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        st = json.loads(p.stdout.decode("utf-8", "replace") or "{}").get("streams") or []
        if st and st[0].get("width"):
            return True, "%s %sx%s" % (st[0].get("codec_name"), st[0].get("width"), st[0].get("height"))
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        return False, (err[-1] if err else "keine Videospur gefunden")[:200]
    except subprocess.TimeoutExpired:
        return False, "keine Antwort nach %d s" % timeout
    except Exception as e:
        return False, str(e)


def run_test(url):
    url = (url or "").strip()
    steps = []

    def add(name, ok, detail):
        steps.append(dict(name=name, ok=ok, detail=mask_url(detail)))

    result = dict(url=mask_url(url), steps=steps, recommend=None)
    if not url.lower().startswith("rtsp://"):
        add("Adresse", None, "Kein RTSP-Link - nur ffmpeg-Test")
        ok, d = _ffprobe(url, "tcp") if "://" in url else (None, "lokale Datei")
        add("ffprobe", ok, d)
        return result
    try:
        r = Rtsp(url)
    except Exception as e:
        add("Adresse", False, "ungültig: %s" % e)
        return result
    add("Adresse", True, "%s Port %d%s" % (r.host, r.port, " · mit Anmeldung" if r.user else ""))

    t0 = time.time()
    try:
        r.connect()
        add("Netzwerk", True, "Port %d erreichbar (%.0f ms)" % (r.port, (time.time() - t0) * 1000))
    except Exception as e:
        add("Netzwerk", False, "Port %d nicht erreichbar: %s - IP/Port/Firewall prüfen" % (r.port, e))
        return result

    base = r.url
    try:
        code, reason, hdr, _ = r.request("OPTIONS")
        add("RTSP OPTIONS", code == 200, "%d %s%s" % (code, reason,
            (" · " + hdr["public"]) if hdr.get("public") else ""))
        code, reason, hdr, sdp = r.request("DESCRIBE", extra="Accept: application/sdp\r\n")
        if code == 200:
            base = hdr.get("content-base") or r.url
            codec, pt, track = _sdp_info(sdp, base)
            add("RTSP DESCRIBE", True, "200 OK · Video %s (PT %s) · Spur %s" % (codec or "?", pt or "?", track))
            result["sdp"] = mask_url(sdp)[:3000]
            result["codec"] = codec
        else:
            add("RTSP DESCRIBE", False, "%d %s%s" % (code, reason,
                (" - " + STATUS_HINT[code]) if code in STATUS_HINT else ""))
            return result
    except Exception as e:
        add("RTSP", False, "%s: %s" % (type(e).__name__, e))
        return result
    finally:
        r.close()

    ok_t, d = _setup_play(url, base, "tcp")
    add("Empfang über TCP", ok_t, d)
    ok_u, d = _setup_play(url, base, "udp")
    add("Empfang über UDP (wie VLC)", ok_u, d)

    fp_t, d = _ffprobe(url, "tcp")
    add("ffprobe TCP", fp_t, d)
    fp_u, d = _ffprobe(url, "udp")
    add("ffprobe UDP", fp_u, d)

    if (ok_t and fp_t is not False) or fp_t:
        result["recommend"] = "tcp"
    elif ok_u or fp_u:
        result["recommend"] = "udp"
    return result
