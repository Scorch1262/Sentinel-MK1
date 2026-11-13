"""Bildfunktionen nur mit numpy + Standardbibliothek (kein OpenCV/Pillow noetig)."""
import struct
import zlib

import numpy as np


def jpeg_size(data):
    """Breite/Hoehe aus dem SOF-Marker einer JPEG-Datei lesen."""
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        ln = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + ln
    return None


def resize_nn(img, w, h):
    """Naechster-Nachbar-Skalierung (schnell, fuer die Erkennung ausreichend)."""
    H, W = img.shape[:2]
    ys = (np.arange(h) * (H / h)).astype(np.int32)
    xs = (np.arange(w) * (W / w)).astype(np.int32)
    return img[ys][:, xs]


def resize_area(img, w, h):
    """Glatte Verkleinerung (fuer Vorschaubilder): Mittelwert ueber Bloecke + Bilinear."""
    H, W = img.shape[:2]
    if w >= W and h >= H:
        return resize_bilinear(img, w, h)
    fy, fx = max(1, H // h), max(1, W // w)
    if fy > 1 or fx > 1:
        Hc, Wc = (H // fy) * fy, (W // fx) * fx
        img = img[:Hc, :Wc].reshape(Hc // fy, fy, Wc // fx, fx, -1).mean(axis=(1, 3))
    return resize_bilinear(img, w, h)


def resize_bilinear(img, w, h):
    H, W = img.shape[:2]
    src = img.astype(np.float32)
    ys = (np.arange(h, dtype=np.float32) + 0.5) * (H / h) - 0.5
    xs = (np.arange(w, dtype=np.float32) + 0.5) * (W / w) - 0.5
    ys = np.clip(ys, 0, H - 1)
    xs = np.clip(xs, 0, W - 1)
    y0 = np.floor(ys).astype(np.int32)
    x0 = np.floor(xs).astype(np.int32)
    y1 = np.minimum(y0 + 1, H - 1)
    x1 = np.minimum(x0 + 1, W - 1)
    fy = (ys - y0)[:, None, None]
    fx = (xs - x0)[None, :, None]
    top = src[y0][:, x0] * (1 - fx) + src[y0][:, x1] * fx
    bot = src[y1][:, x0] * (1 - fx) + src[y1][:, x1] * fx
    out = top * (1 - fy) + bot * fy
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def png_encode(rgb):
    """RGB-Array (H,W,3 uint8) als PNG-Datei kodieren."""
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w = rgb.shape[:2]
    raw = np.empty((h, w * 3 + 1), dtype=np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = rgb.reshape(h, w * 3)

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b""))


def crop(img, box, pad=0.0):
    """box in Pixeln (x1,y1,x2,y2); pad als Anteil der Boxgroesse."""
    H, W = img.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    x1 = int(max(0, x1 - bw * pad))
    y1 = int(max(0, y1 - bh * pad))
    x2 = int(min(W, x2 + bw * pad))
    y2 = int(min(H, y2 + bh * pad))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None, (x1, y1)
    return img[y1:y2, x1:x2], (x1, y1)


def thumb(img, max_side=200):
    h, w = img.shape[:2]
    s = min(1.0, max_side / float(max(h, w)))
    if s >= 1.0:
        return img
    return resize_area(img, max(1, int(w * s)), max(1, int(h * s)))


def rgb_to_hsv(rgb):
    x = rgb.astype(np.float32) / 255.0
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    mx = x.max(axis=-1)
    mn = x.min(axis=-1)
    d = mx - mn
    h = np.zeros_like(mx)
    m = d > 1e-6
    rm = m & (mx == r)
    gm = m & (mx == g) & ~rm
    bm = m & ~rm & ~gm
    h[rm] = ((g - b)[rm] / d[rm]) % 6
    h[gm] = (b - r)[gm] / d[gm] + 2
    h[bm] = (r - g)[bm] / d[bm] + 4
    h = h / 6.0
    s = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0)
    return h, s, mx


APP_DIM = 3 * (12 * 3 + 4)


def appearance(rgb):
    """Farb-Merkmal eines Ausschnitts: drei Baender (oben/mitte/unten),
    je Band Farbton x Saettigung (12x3, gewichtet) + Helligkeit (4). L2-normiert."""
    h, w = rgb.shape[:2]
    if h < 6 or w < 4:
        return None
    # Raender abschneiden (Hintergrund), auf handliche Groesse bringen
    small = resize_nn(rgb[int(h * 0.05):max(int(h * 0.95), 1), int(w * 0.15):max(int(w * 0.85), 1)], 24, 48)
    hh, ss, vv = rgb_to_hsv(small)
    feats = []
    for y0, y1 in ((0, 14), (14, 30), (30, 48)):
        hb, sb, vb = hh[y0:y1].ravel(), ss[y0:y1].ravel(), vv[y0:y1].ravel()
        colorful = (sb > 0.18) & (vb > 0.15)
        hi = np.minimum((hb * 12).astype(np.int32), 11)
        si = np.minimum(((sb - 0.18) / 0.82 * 3).astype(np.int32), 2)
        hist = np.zeros(12 * 3, np.float32)
        np.add.at(hist, (hi * 3 + np.maximum(si, 0))[colorful], 1.0)
        vh = np.zeros(4, np.float32)
        vi = np.minimum((vb * 4).astype(np.int32), 3)
        np.add.at(vh, vi[~colorful], 1.0)
        f = np.concatenate([hist, vh])
        f = f / max(f.sum(), 1.0)
        feats.append(np.sqrt(f))
    v = np.concatenate(feats).astype(np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 0 else None


def similarity_transform(src, dst):
    """Umeyama: 2x3-Matrix, die src-Punkte auf dst-Punkte abbildet (Drehung, Skalierung, Verschiebung)."""
    src = np.asarray(src, np.float64)
    dst = np.asarray(dst, np.float64)
    n = src.shape[0]
    ms, md = src.mean(0), dst.mean(0)
    sc, dc = src - ms, dst - md
    cov = dc.T @ sc / n
    U, S, Vt = np.linalg.svd(cov)
    D = np.eye(2)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        D[1, 1] = -1
    R = U @ D @ Vt
    var = (sc ** 2).sum() / n
    s = np.trace(np.diag(S) @ D) / var if var > 0 else 1.0
    t = md - s * R @ ms
    M = np.zeros((2, 3))
    M[:, :2] = s * R
    M[:, 2] = t
    return M


def warp_affine(img, M, w, h):
    """Bilineares Warp (M bildet Quelle -> Ziel ab)."""
    A = np.vstack([M, [0, 0, 1]])
    Ai = np.linalg.inv(A)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    sx = Ai[0, 0] * xx + Ai[0, 1] * yy + Ai[0, 2]
    sy = Ai[1, 0] * xx + Ai[1, 1] * yy + Ai[1, 2]
    H, W = img.shape[:2]
    x0 = np.floor(sx).astype(np.int32)
    y0 = np.floor(sy).astype(np.int32)
    fx = (sx - x0)[..., None]
    fy = (sy - y0)[..., None]
    src = img.astype(np.float32)

    def px(yi, xi):
        ok = (xi >= 0) & (xi < W) & (yi >= 0) & (yi < H)
        v = src[np.clip(yi, 0, H - 1), np.clip(xi, 0, W - 1)]
        return v * ok[..., None]

    out = (px(y0, x0) * (1 - fx) * (1 - fy) + px(y0, x0 + 1) * fx * (1 - fy)
           + px(y0 + 1, x0) * (1 - fx) * fy + px(y0 + 1, x0 + 1) * fx * fy)
    return np.clip(out, 0, 255).astype(np.uint8)


def gray_small(rgb, w=64, h=36):
    s = resize_nn(rgb, w, h).astype(np.float32)
    return s @ np.array([0.299, 0.587, 0.114], np.float32)
