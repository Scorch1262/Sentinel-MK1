"""TensorRT-Ausfuehrung auf der GPU des NVIDIA Jetson (JetPack 4.6: TensorRT 8.2, CUDA 10.2, Python 3.6).

Ohne pycuda: der Speicher wird ueber die CUDA-Laufzeit (libcudart) per ctypes verwaltet.
Die Engines werden einmalig auf dem Geraet mit trtexec aus den ONNX-Modellen gebaut (FP16).
"""
import ctypes
import glob
import os
import shutil
import subprocess
import time
import threading

import numpy as np

_trt = None
_cuda = None
_lock = threading.Lock()


def trt_module():
    global _trt
    if _trt is None:
        try:
            import tensorrt  # noqa: F401  (JetPack: python3-libnvinfer)
            _trt = tensorrt
        except Exception:
            _trt = False
    return _trt or None


def trt_version():
    t = trt_module()
    return getattr(t, "__version__", "") if t else ""


def trtexec_path():
    for p in ("/usr/src/tensorrt/bin/trtexec", shutil.which("trtexec") or "", "/usr/local/bin/trtexec"):
        if p and os.path.exists(p):
            return p
    return None


def available():
    """TensorRT nutzbar? (Python-Modul + CUDA-Laufzeit)"""
    if not trt_module():
        return False
    try:
        cuda()
        return True
    except Exception:
        return False


class _Cuda:
    H2D, D2H = 1, 2

    def __init__(self):
        names = ["libcudart.so", "libcudart.so.10.2", "libcudart.so.11.0", "libcudart.so.12"]
        names += sorted(glob.glob("/usr/local/cuda*/lib64/libcudart.so*"), reverse=True)
        lib = None
        err = None
        for n in names:
            try:
                lib = ctypes.CDLL(n)
                break
            except OSError as e:
                err = e
        if lib is None:
            raise OSError("libcudart nicht gefunden (%s)" % err)
        lib.cudaMalloc.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
        lib.cudaMalloc.restype = ctypes.c_int
        lib.cudaFree.argtypes = [ctypes.c_void_p]
        lib.cudaFree.restype = ctypes.c_int
        lib.cudaMemcpy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        lib.cudaMemcpy.restype = ctypes.c_int
        lib.cudaGetErrorString.argtypes = [ctypes.c_int]
        lib.cudaGetErrorString.restype = ctypes.c_char_p
        lib.cudaMemGetInfo.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
        lib.cudaMemGetInfo.restype = ctypes.c_int
        self.lib = lib

    def _check(self, rc, what):
        if rc != 0:
            raise RuntimeError("CUDA %s: %s" % (what, self.lib.cudaGetErrorString(rc).decode()))

    def malloc(self, n):
        p = ctypes.c_void_p()
        self._check(self.lib.cudaMalloc(ctypes.byref(p), max(1, int(n))), "cudaMalloc")
        return p.value

    def free(self, p):
        if p:
            self.lib.cudaFree(p)

    def h2d(self, dptr, arr):
        self._check(self.lib.cudaMemcpy(dptr, arr.ctypes.data, arr.nbytes, self.H2D), "cudaMemcpy H2D")

    def d2h(self, arr, dptr):
        self._check(self.lib.cudaMemcpy(arr.ctypes.data, dptr, arr.nbytes, self.D2H), "cudaMemcpy D2H")


def cuda():
    global _cuda
    with _lock:
        if _cuda is None:
            _cuda = _Cuda()
        return _cuda


class TrtModel:
    """Gleiche Schnittstelle wie onnxlite.OnnxModel: input_names, output_names, run(feed) -> Liste."""

    backend = "TensorRT (GPU)"

    def __init__(self, engine_path):
        trt = trt_module()
        if not trt:
            raise RuntimeError("TensorRT nicht verfuegbar")
        self.cu = cuda()
        self.logger = trt.Logger(trt.Logger.WARNING)
        try:
            trt.init_libnvinfer_plugins(self.logger, "")
        except Exception:
            pass
        self.runtime = trt.Runtime(self.logger)
        with open(engine_path, "rb") as f:
            self.engine = self.runtime.deserialize_cuda_engine(f.read())
        if self.engine is None:
            raise RuntimeError("Engine nicht ladbar (anderes TensorRT? Datei loeschen, sie wird neu gebaut)")
        self.ctx = self.engine.create_execution_context()
        self.bind = []
        self.input_names, self.output_names = [], []
        self.host, self.dev, self.ptrs = {}, {}, []
        for i in range(self.engine.num_bindings):
            name = self.engine.get_binding_name(i)
            shape = tuple(int(d) for d in self.engine.get_binding_shape(i))
            dtype = np.dtype(trt.nptype(self.engine.get_binding_dtype(i)))
            is_in = self.engine.binding_is_input(i)
            self.host[name] = np.empty(shape, dtype=dtype)
            self.dev[name] = self.cu.malloc(self.host[name].nbytes)
            self.ptrs.append(int(self.dev[name]))
            (self.input_names if is_in else self.output_names).append(name)
        self.lock = threading.Lock()

    def run(self, feed):
        with self.lock:
            for name, arr in feed.items():
                h = self.host[name]
                h[...] = arr.reshape(h.shape)
                self.cu.h2d(self.dev[name], h)
            if not self.ctx.execute_v2(self.ptrs):
                raise RuntimeError("TensorRT-Ausfuehrung fehlgeschlagen")
            out = []
            for name in self.output_names:
                h = self.host[name]
                self.cu.d2h(h, self.dev[name])
                out.append(h.astype(np.float32, copy=True))
            return out

    def close(self):
        for p in self.dev.values():
            self.cu.free(p)
        self.dev = {}


def build_engine(onnx_path, engine_path, log, workspace_mb=256, fp16=True, timeout=3600):
    """ONNX -> TensorRT-Engine mit trtexec (dauert auf dem Jetson Nano einige Minuten)."""
    exe = trtexec_path()
    if not exe:
        log("trtexec nicht gefunden (/usr/src/tensorrt/bin/trtexec) - GPU-Beschleunigung nicht moeglich")
        return False
    tmp = engine_path + ".part"
    major = 0
    try:
        major = int((trt_version() or "0").split(".")[0])
    except ValueError:
        pass
    for attempt, ws in enumerate((workspace_mb, 64)):
        cmd = [exe, "--onnx=%s" % onnx_path, "--saveEngine=%s" % tmp, "--workspace=%d" % ws]
        if fp16:
            cmd.append("--fp16")
        if major >= 8:
            # cuBLAS/cuDNN-Taktiken weglassen: spart beim Bauen mehrere hundert MB (Jetson Nano 2GB)
            cmd.append("--tacticSources=-cublasLt,-cublas,-cudnn")
        log("Baue TensorRT-Engine %s (einmalig, einige Minuten)%s ..."
            % (os.path.basename(engine_path), " - zweiter Versuch mit weniger Speicher" if attempt else ""))
        t0 = time.time()
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            txt = p.stdout.decode("utf-8", "replace")
        except Exception as e:
            log("trtexec: %s" % e)
            return False
        if p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) >= 1000:
            break
        try:
            os.remove(tmp)
        except OSError:
            pass
        if p.returncode in (-9, 137):
            log("Engine-Bau abgebrochen: vom System beendet, Arbeitsspeicher reicht nicht (RAM + Auslagerung). "
                "Tipp: Auslagerungsdatei pruefen (free -h), Desktop abschalten.")
            continue
        tail = " | ".join([l for l in txt.splitlines() if "ERROR" in l or "error" in l][-3:])
        log("Engine-Bau fehlgeschlagen: %s" % (tail or "Code %s" % p.returncode))
        return False
    else:
        return False
    log("(Bauzeit %d s)" % (time.time() - t0))
    os.replace(tmp, engine_path)
    log("TensorRT-Engine fertig: %s" % os.path.basename(engine_path))
    return True
