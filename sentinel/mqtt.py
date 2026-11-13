"""Minimaler MQTT-3.1.1-Sender (nur PUBLISH QoS 0) ohne Zusatzpakete."""
import os
import socket
import struct
import threading


def _enc_len(n):
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


class MqttSender:
    def __init__(self):
        self.lock = threading.Lock()
        self.sock = None
        self.key = None
        self.last_error = ""

    def _connect(self, cfg):
        s = socket.create_connection((cfg.get("host") or "127.0.0.1", int(cfg.get("port") or 1883)), timeout=5)
        flags = 0x02
        payload = _str("sentinel-mk1-%s" % os.urandom(3).hex())
        if cfg.get("user"):
            flags |= 0x80
            payload += _str(cfg["user"])
            if cfg.get("password"):
                flags |= 0x40
                payload += _str(cfg["password"])
        var = _str("MQTT") + bytes([4, flags]) + struct.pack(">H", 60)
        body = var + payload
        s.sendall(bytes([0x10]) + _enc_len(len(body)) + body)
        ack = s.recv(4)
        if len(ack) < 4 or ack[0] != 0x20 or ack[3] != 0:
            s.close()
            raise OSError("Broker lehnt Verbindung ab (Code %s)" % (ack[3] if len(ack) >= 4 else "?"))
        s.settimeout(5)
        return s

    def publish(self, cfg, topic, payload, retain=False):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        key = (cfg.get("host"), cfg.get("port"), cfg.get("user"), cfg.get("password"))
        pkt_body = _str(topic) + payload
        pkt = bytes([0x30 | (1 if retain else 0)]) + _enc_len(len(pkt_body)) + pkt_body
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.sock is None or self.key != key:
                        self.close()
                        self.sock = self._connect(cfg)
                        self.key = key
                    self.sock.sendall(pkt)
                    self.last_error = ""
                    return True
                except Exception as e:
                    self.last_error = str(e)
                    self.close()
            return False

    def ping(self):
        with self.lock:
            if self.sock is None:
                return
            try:
                self.sock.sendall(b"\xc0\x00")
                self.sock.settimeout(0.5)
                try:
                    self.sock.recv(16)
                except socket.timeout:
                    pass
                self.sock.settimeout(5)
            except Exception:
                self.close()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.sendall(b"\xe0\x00")
                self.sock.close()
            except Exception:
                pass
        self.sock = None
