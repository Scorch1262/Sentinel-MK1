"""
onnxlite - minimale ONNX-Laufzeit nur mit numpy (Sentinel MK1)

Auf dem GL.iNet Brume 2 (OpenWrt, musl, ARM) gibt es weder OpenCV noch
onnxruntime als fertiges Paket - numpy dagegen schon. Dieses Modul liest
ONNX-Dateien mit einem eigenen, kleinen Protobuf-Decoder und fuehrt die
Operatoren aus, die die verwendeten Modelle (NanoDet-Plus, YuNet, SFace)
brauchen. BatchNorm wird beim Laden in die vorangehende Faltung eingerechnet.
"""
import struct

import numpy as np

# ---------------------------------------------------------------------------
# Protobuf-Decoder (nur das, was ONNX braucht)
# ---------------------------------------------------------------------------


def _varint(buf, pos):
    res = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        res |= (b & 0x7F) << shift
        if not b & 0x80:
            return res, pos
        shift += 7


def _fields(buf):
    """Liefert (Feldnummer, Wiretyp, Wert) fuer jede Nachricht im Puffer."""
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = _varint(buf, pos)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, pos = _varint(buf, pos)
        elif wt == 1:
            v = buf[pos:pos + 8]
            pos += 8
        elif wt == 2:
            ln, pos = _varint(buf, pos)
            v = buf[pos:pos + ln]
            pos += ln
        elif wt == 5:
            v = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError("Protobuf-Wiretyp %d nicht unterstuetzt" % wt)
        yield fn, wt, v


def _signed(v):
    return v - (1 << 64) if v >= (1 << 63) else v


def _packed_varints(v):
    out = []
    pos = 0
    while pos < len(v):
        x, pos = _varint(v, pos)
        out.append(_signed(x))
    return out


_DTYPES = {1: np.float32, 2: np.uint8, 3: np.int8, 5: np.int16, 6: np.int32, 7: np.int64,
           9: np.bool_, 10: np.float16, 11: np.float64}


def _tensor(buf):
    dims, dtype, name, raw = [], 1, "", None
    floats, ints32, ints64, doubles = [], [], [], []
    for fn, wt, v in _fields(buf):
        if fn == 1:
            dims.extend(_packed_varints(v) if wt == 2 else [_signed(v)])
        elif fn == 2:
            dtype = v
        elif fn == 4:
            floats.extend(struct.unpack("<%df" % (len(v) // 4), v) if wt == 2 else struct.unpack("<f", v))
        elif fn == 5:
            ints32.extend(_packed_varints(v) if wt == 2 else [_signed(v)])
        elif fn == 7:
            ints64.extend(_packed_varints(v) if wt == 2 else [_signed(v)])
        elif fn == 8:
            name = bytes(v).decode()
        elif fn == 9:
            raw = bytes(v)
        elif fn == 10:
            doubles.extend(struct.unpack("<%dd" % (len(v) // 8), v) if wt == 2 else struct.unpack("<d", v))
    dt = _DTYPES.get(dtype, np.float32)
    if raw is not None:
        arr = np.frombuffer(raw, dtype=dt).copy()
    elif floats:
        arr = np.array(floats, dtype=np.float32)
    elif ints64:
        arr = np.array(ints64, dtype=np.int64)
    elif ints32:
        arr = np.array(ints32, dtype=dt)
    elif doubles:
        arr = np.array(doubles, dtype=np.float64)
    else:
        arr = np.zeros(0, dtype=dt)
    return name, arr.reshape(dims) if dims else (arr.reshape(()) if arr.size == 1 else arr)


def _attribute(buf):
    name, val = "", None
    floats, ints, strings = [], [], []
    atype = 0
    for fn, wt, v in _fields(buf):
        if fn == 1:
            name = bytes(v).decode()
        elif fn == 2:
            val = struct.unpack("<f", v)[0]
        elif fn == 3:
            val = _signed(v)
        elif fn == 4:
            val = bytes(v).decode("utf-8", "replace")
        elif fn == 5:
            val = _tensor(v)[1]
        elif fn == 7:
            floats.extend(struct.unpack("<%df" % (len(v) // 4), v) if wt == 2 else struct.unpack("<f", v))
        elif fn == 8:
            ints.extend(_packed_varints(v) if wt == 2 else [_signed(v)])
        elif fn == 9:
            strings.append(bytes(v).decode("utf-8", "replace"))
        elif fn == 20:
            atype = v
    if atype == 6 or (val is None and floats):
        val = floats
    elif atype == 7 or (val is None and ints):
        val = ints
    elif atype == 8 or (val is None and strings):
        val = strings
    return name, val


def _node(buf):
    ins, outs, op, attrs = [], [], "", {}
    for fn, wt, v in _fields(buf):
        if fn == 1:
            ins.append(bytes(v).decode())
        elif fn == 2:
            outs.append(bytes(v).decode())
        elif fn == 4:
            op = bytes(v).decode()
        elif fn == 5:
            k, a = _attribute(v)
            attrs[k] = a
    return dict(op=op, inputs=ins, outputs=outs, attrs=attrs)


def _value_info_name(buf):
    for fn, wt, v in _fields(buf):
        if fn == 1:
            return bytes(v).decode()
    return ""


def load_onnx(path):
    data = memoryview(open(path, "rb").read())
    graph = None
    for fn, wt, v in _fields(data):
        if fn == 7:
            graph = v
    if graph is None:
        raise ValueError("keine ONNX-Datei: %s" % path)
    nodes, inits, inputs, outputs = [], {}, [], []
    for fn, wt, v in _fields(graph):
        if fn == 1:
            nodes.append(_node(v))
        elif fn == 5:
            k, arr = _tensor(v)
            inits[k] = arr
        elif fn == 11:
            inputs.append(_value_info_name(v))
        elif fn == 12:
            outputs.append(_value_info_name(v))
    inputs = [i for i in inputs if i not in inits]
    return nodes, inits, inputs, outputs


# ---------------------------------------------------------------------------
# Operatoren (Batchgroesse 1, Layout NCHW)
# ---------------------------------------------------------------------------


def _pad_hw(x, p):
    t, l, b, r = p
    if t == l == b == r == 0:
        return x
    return np.pad(x, ((0, 0), (t, b), (l, r)), mode="constant")


def conv2d(x, w, b, stride, pads, group, dil=(1, 1)):
    """x: (C,H,W) float32, w: (O, C/g, kh, kw)."""
    C, H, W = x.shape
    O, cg, kh, kw = w.shape
    sh, sw = stride
    dh, dw = dil
    xp = _pad_hw(x, pads)
    Hp, Wp = xp.shape[1:]
    Ho = (Hp - (dh * (kh - 1) + 1)) // sh + 1
    Wo = (Wp - (dw * (kw - 1) + 1)) // sw + 1
    if kh == 1 and kw == 1 and group == 1:
        xs = xp[:, ::sh, ::sw][:, :Ho, :Wo]
        y = w.reshape(O, C) @ xs.reshape(C, -1)
        y = y.reshape(O, Ho, Wo)
    elif group == C and cg == 1:
        # Depthwise: Summe ueber verschobene Ausschnitte
        mult = O // C
        if mult != 1:
            xp = np.repeat(xp, mult, axis=0)
        wk = w.reshape(O, kh, kw)
        y = None
        for i in range(kh):
            for j in range(kw):
                sl = xp[:, i * dh:i * dh + sh * (Ho - 1) + 1:sh, j * dw:j * dw + sw * (Wo - 1) + 1:sw]
                t = sl * wk[:, i, j][:, None, None]
                y = t if y is None else y + t
    else:
        og = O // group
        ys = []
        for g in range(group):
            xg = xp[g * cg:(g + 1) * cg]
            cols = np.empty((cg, kh, kw, Ho, Wo), dtype=np.float32)
            for i in range(kh):
                for j in range(kw):
                    cols[:, i, j] = xg[:, i * dh:i * dh + sh * (Ho - 1) + 1:sh, j * dw:j * dw + sw * (Wo - 1) + 1:sw]
            wg = w[g * og:(g + 1) * og].reshape(og, -1)
            ys.append((wg @ cols.reshape(cg * kh * kw, -1)).reshape(og, Ho, Wo))
        y = ys[0] if group == 1 else np.concatenate(ys, axis=0)
    if b is not None:
        y = y + b[:, None, None]
    return y.astype(np.float32, copy=False)


def maxpool(x, k, s, pads):
    C, H, W = x.shape
    xp = x
    if any(pads):
        t, l, b, r = pads
        xp = np.pad(x, ((0, 0), (t, b), (l, r)), mode="constant", constant_values=-np.inf)
    Hp, Wp = xp.shape[1:]
    Ho = (Hp - k[0]) // s[0] + 1
    Wo = (Wp - k[1]) // s[1] + 1
    y = None
    for i in range(k[0]):
        for j in range(k[1]):
            sl = xp[:, i:i + s[0] * (Ho - 1) + 1:s[0], j:j + s[1] * (Wo - 1) + 1:s[1]]
            y = sl if y is None else np.maximum(y, sl)
    return y


def _lin_idx(n_in, n_out, mode):
    scale = n_in / n_out
    o = np.arange(n_out, dtype=np.float32)
    if mode == "align_corners":
        src = o * ((n_in - 1) / (n_out - 1)) if n_out > 1 else np.zeros_like(o)
    elif mode == "asymmetric":
        src = o * scale
    else:  # half_pixel / pytorch_half_pixel
        src = (o + 0.5) * scale - 0.5
        if mode == "pytorch_half_pixel" and n_out == 1:
            src = np.zeros_like(o)
    src = np.clip(src, 0, n_in - 1)
    i0 = np.floor(src).astype(np.int64)
    i1 = np.minimum(i0 + 1, n_in - 1)
    f = (src - i0).astype(np.float32)
    return i0, i1, f


def resize(x, Ho, Wo, mode, ctm, nearest_mode):
    C, H, W = x.shape
    if mode == "nearest":
        if ctm == "asymmetric":
            ys = np.floor(np.arange(Ho) * (H / Ho)).astype(np.int64)
            xs = np.floor(np.arange(Wo) * (W / Wo)).astype(np.int64)
        else:
            ys = np.clip(np.floor((np.arange(Ho) + 0.5) * (H / Ho) - 0.5 + 0.5), 0, H - 1).astype(np.int64)
            xs = np.clip(np.floor((np.arange(Wo) + 0.5) * (W / Wo) - 0.5 + 0.5), 0, W - 1).astype(np.int64)
        return x[:, ys][:, :, xs]
    y0, y1, fy = _lin_idx(H, Ho, ctm)
    x0, x1, fx = _lin_idx(W, Wo, ctm)
    top = x[:, y0] * (1 - fy)[None, :, None] + x[:, y1] * fy[None, :, None]
    return (top[:, :, x0] * (1 - fx) + top[:, :, x1] * fx).astype(np.float32)


def _unbatch(x, z):
    """Konstanten mit Batch-Achse (1,C,1,1) an Tensoren ohne Batch-Achse anpassen."""
    if isinstance(z, np.ndarray) and z.ndim == x.ndim + 1 and z.shape[0] == 1:
        z = z[0]
    if isinstance(x, np.ndarray) and isinstance(z, np.ndarray) and x.ndim == z.ndim + 1 and x.shape[0] == 1 and False:
        pass
    return x, z


class OnnxModel:
    """Fuehrt ein ONNX-Modell mit numpy aus (Batch 1)."""

    def __init__(self, path, spatial_map=None):
        nodes, inits, inputs, outputs = load_onnx(path)
        self.inits = {k: (v.astype(np.float32) if v.dtype == np.float64 else v) for k, v in inits.items()}
        self.input_names = inputs
        self.output_names = outputs
        self.spatial_map = spatial_map or {}
        self.nodes = self._fold_bn(nodes)

    def _fold_bn(self, nodes):
        consumers = {}
        for n in nodes:
            for i in n["inputs"]:
                consumers[i] = consumers.get(i, 0) + 1
        prod = {}
        for n in nodes:
            for o in n["outputs"]:
                prod[o] = n
        out = []
        dropped = set()
        for n in nodes:
            if n["op"] == "BatchNormalization":
                src = prod.get(n["inputs"][0])
                if src is not None and src["op"] == "Conv" and consumers.get(n["inputs"][0], 0) == 1 \
                        and n["inputs"][0] not in self.output_names:
                    sc, bi, mu, va = [self.inits[k] for k in n["inputs"][1:5]]
                    eps = n["attrs"].get("epsilon", 1e-5)
                    f = sc / np.sqrt(va + eps)
                    w = self.inits[src["inputs"][1]] * f[:, None, None, None]
                    b0 = self.inits[src["inputs"][2]] if len(src["inputs"]) > 2 else np.zeros_like(mu)
                    b = (b0 - mu) * f + bi
                    wn, bn = src["inputs"][1] + "_bnf", src["outputs"][0] + "_bnb"
                    self.inits[wn] = w.astype(np.float32)
                    self.inits[bn] = b.astype(np.float32)
                    src["inputs"] = [src["inputs"][0], wn, bn]
                    src["outputs"] = list(n["outputs"])
                    continue
            if n["op"] == "Dropout":
                n = dict(n, op="Identity", outputs=n["outputs"][:1])
            out.append(n)
        # Konstanten-Knoten vorab auswerten
        res = []
        for n in out:
            if n["op"] == "Constant":
                self.inits[n["outputs"][0]] = n["attrs"].get("value")
                continue
            res.append(n)
        return [n for n in res if id(n) not in dropped]

    def _sp(self, v):
        return self.spatial_map.get(int(v), int(v))

    def run(self, feed):
        """feed: dict name -> Array (N=1). Rueckgabe: Liste der Ausgaben (mit N-Achse)."""
        env = dict(self.inits)
        for k, v in feed.items():
            env[k] = v[0].astype(np.float32)  # intern ohne Batch-Achse
        for n in self.nodes:
            op, a = n["op"], n["attrs"]
            ins = [env[i] if i else None for i in n["inputs"]]
            x = ins[0]
            if op == "Conv":
                w = ins[1]
                kh, kw = w.shape[2:]
                pads = a.get("pads", [0, 0, 0, 0])
                if a.get("auto_pad", "NOTSET") in ("SAME_UPPER", "SAME_LOWER"):
                    ph, pw = kh // 2, kw // 2
                    pads = [ph, pw, kh - 1 - ph, kw - 1 - pw]
                y = conv2d(x, w, ins[2] if len(ins) > 2 else None, a.get("strides", [1, 1]),
                           (pads[0], pads[1], pads[2], pads[3]), a.get("group", 1), a.get("dilations", [1, 1]))
            elif op == "Relu":
                y = np.maximum(x, 0)
            elif op == "LeakyRelu":
                al = a.get("alpha", 0.01)
                y = np.where(x > 0, x, x * np.float32(al))
            elif op == "PRelu":
                s = ins[1]
                if s.ndim == 3:
                    s = s.reshape(-1)
                if s.ndim == 1 and x.ndim == 3:
                    s = s[:, None, None]
                y = np.where(x > 0, x, x * s)
            elif op == "Sigmoid":
                y = 1.0 / (1.0 + np.exp(-x))
            elif op in ("Add", "Sub", "Mul", "Div"):
                x, z = _unbatch(x, ins[1])
                y = x + z if op == "Add" else x - z if op == "Sub" else x * z if op == "Mul" else x / z
            elif op == "Identity":
                y = x
            elif op == "MaxPool":
                y = maxpool(x, a["kernel_shape"], a.get("strides", a["kernel_shape"]), a.get("pads", [0, 0, 0, 0]))
            elif op == "Concat":
                ax = a.get("axis", 1)
                y = np.concatenate([t for t in ins if t is not None], axis=ax - 1 if ax > 0 else ax)
            elif op == "Split":
                ax = a.get("axis", 0)
                ax = ax - 1 if ax > 0 else ax
                sp = a.get("split")
                if sp is None and len(ins) > 1 and ins[1] is not None:
                    sp = list(ins[1])
                if sp is None:
                    k = len(n["outputs"])
                    sp = [x.shape[ax] // k] * k
                idx = np.cumsum(sp)[:-1]
                parts = np.split(x, idx, axis=ax)
                for o, p in zip(n["outputs"], parts):
                    env[o] = p
                continue
            elif op == "Reshape":
                shp = [int(s) for s in ins[1]]
                full = (1,) + x.shape
                if len(shp) >= 4:
                    shp = shp[:-2] + [self._sp(shp[-2]), self._sp(shp[-1])]
                shp = [full[i] if s == 0 else s for i, s in enumerate(shp)]
                y = x.reshape(full).reshape(shp)[0]
            elif op == "Slice":
                full = x[None]
                starts = [int(v) for v in np.ravel(ins[1])]
                ends = [int(v) for v in np.ravel(ins[2])]
                axes = [int(v) for v in np.ravel(ins[3])] if len(ins) > 3 and ins[3] is not None else list(range(len(starts)))
                steps = [int(v) for v in np.ravel(ins[4])] if len(ins) > 4 and ins[4] is not None else [1] * len(starts)
                sl = [slice(None)] * full.ndim
                for st, en, ax, sp in zip(starts, ends, axes, steps):
                    ax = ax % full.ndim
                    dim = full.shape[ax]
                    en = max(min(en, dim), -dim - 1) if en < 0 or en > dim else en
                    sl[ax] = slice(st, en, sp)
                y = full[tuple(sl)][0]
            elif op == "Flatten":
                y = x.reshape(-1)
            elif op == "Transpose":
                perm = a.get("perm")
                y = np.transpose(x[None], perm)[0]
            elif op == "Resize":
                mode = a.get("mode", "nearest")
                ctm = a.get("coordinate_transformation_mode", "half_pixel")
                sizes = ins[3] if len(ins) > 3 and ins[3] is not None and ins[3].size else None
                scales = ins[2] if len(ins) > 2 and ins[2] is not None and ins[2].size else None
                if sizes is not None:
                    Ho, Wo = self._sp(sizes[-2]), self._sp(sizes[-1])
                else:
                    Ho, Wo = int(x.shape[-2] * scales[-2]), int(x.shape[-1] * scales[-1])
                y = resize(x, Ho, Wo, mode, ctm, a.get("nearest_mode", "round_prefer_floor"))
            elif op == "Gemm":
                A = x.reshape(1, -1)
                B = ins[1]
                if a.get("transA", 0):
                    A = A.T
                if a.get("transB", 0):
                    B = B.T
                y = a.get("alpha", 1.0) * (A @ B)
                if len(ins) > 2 and ins[2] is not None:
                    y = y + a.get("beta", 1.0) * ins[2]
                y = y.reshape(-1)
            elif op == "Softmax":
                ax = a.get("axis", -1)
                ax = ax - 1 if ax > 0 else ax
                e = np.exp(x - x.max(axis=ax, keepdims=True))
                y = e / e.sum(axis=ax, keepdims=True)
            elif op == "BatchNormalization":
                sc, bi, mu, va = ins[1:5]
                eps = a.get("epsilon", 1e-5)
                sh = (-1,) + (1,) * (x.ndim - 1)
                y = (x - mu.reshape(sh)) / np.sqrt(va.reshape(sh) + eps) * sc.reshape(sh) + bi.reshape(sh)
            else:
                raise NotImplementedError("ONNX-Operator %s fehlt in onnxlite" % op)
            env[n["outputs"][0]] = y.astype(np.float32, copy=False) if y.dtype == np.float64 else y
        return [env[o][None] for o in self.output_names]
