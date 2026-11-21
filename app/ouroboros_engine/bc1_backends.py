"""Versioned BC1/DXT1a compression backends with temporal block optimization (Interventions 09 & 19).

Preserves canonical `app/dxt1.py` untouched as the guaranteed fallback. Every backend produces
strictly valid, alpha-safe, non-mipmapped PS4 DXT1a `.dds` blobs (128-byte header + 8 B / 4×4 block).
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image

from .. import dds, dxt1

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_CZAR_BC1 = REPO_ROOT / "tools" / "lib" / "libczar_bc1.so"
COMPRESSONATOR_CLI = (
    REPO_ROOT / "tools" / "bin" / "compressonator" / "compressonatorcli-4.5.52-Linux" / "compressonatorcli"
)

DEFAULT_OUROBOROS_BC1_BACKEND = "rgbcx_temporal"

import threading

_LIB_HANDLE: ctypes.CDLL | None = None
_LIB_LOAD_ATTEMPTED = False
_LIB_LOCK = threading.Lock()


def _load_bc1_lib() -> ctypes.CDLL | None:
    global _LIB_HANDLE, _LIB_LOAD_ATTEMPTED
    if _LIB_LOAD_ATTEMPTED:
        return _LIB_HANDLE
    with _LIB_LOCK:
        if _LIB_LOAD_ATTEMPTED:
            return _LIB_HANDLE
        _LIB_LOAD_ATTEMPTED = True
        if not LIB_CZAR_BC1.is_file():
            return None
        try:
            lib = ctypes.CDLL(str(LIB_CZAR_BC1))
            lib.encode_frame_bc1_ex.argtypes = [
                ctypes.c_void_p,  # rgba
                ctypes.c_int,     # width
                ctypes.c_int,     # height
                ctypes.c_int,     # backend (0=rgbcx, 1=icbc)
                ctypes.c_int,     # quality
                ctypes.c_int,     # perceptual_weights
                ctypes.c_void_p,  # prev_rgba
                ctypes.c_void_p,  # prev_blocks
                ctypes.c_int,     # temporal_mode (0=off, 1=ModeA, 2=ModeA+B)
                ctypes.c_float,   # static_mae_thresh
                ctypes.c_float,   # beta_flicker
                ctypes.c_void_p,  # out_blocks
                ctypes.c_void_p,  # reused_blocks_out
            ]
            lib.encode_frame_bc1_ex.restype = None
            # Single-threaded warmup so static RGBCX/ICBC lookup tables are initialized before concurrent calls
            dummy_rgba = np.full((4, 4, 4), 255, dtype=np.uint8)
            dummy_out = (ctypes.c_uint8 * 8)()
            reused = ctypes.c_int(0)
            lib.encode_frame_bc1_ex(
                dummy_rgba.ctypes.data, 4, 4, 0, 1, 1, 0, 0, 0,
                ctypes.c_float(0.0), ctypes.c_float(0.0), ctypes.addressof(dummy_out), ctypes.byref(reused),
            )
            _LIB_HANDLE = lib
        except OSError:
            _LIB_HANDLE = None
        return _LIB_HANDLE


def available_ouroboros_bc1_backends() -> list[str]:
    """List currently operational BC1/DXT1a backends in priority order."""
    out: list[str] = []
    if _load_bc1_lib() is not None:
        out.extend(["rgbcx_temporal", "rgbcx", "icbc"])
    if COMPRESSONATOR_CLI.is_file():
        out.append("compressonator")
    out.append("native")
    return out


def _as_rgba_padded(frame: np.ndarray) -> tuple[np.ndarray, int, int, int, int]:
    """Convert RGB or RGBA uint8 frame to contiguous opaque RGBA padded to 4x4 multiple."""
    arr = np.asarray(frame, dtype=np.uint8)
    h, w = arr.shape[:2]
    if arr.shape[-1] == 3:
        rgba = np.concatenate([arr, np.full((h, w, 1), 255, dtype=np.uint8)], axis=-1)
    else:
        rgba = arr.copy()
        rgba[..., 3] = 255
    pad_h = (-h) % 4
    pad_w = (-w) % 4
    if pad_h or pad_w:
        rgba = np.pad(rgba, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
    ph, pw = rgba.shape[:2]
    return np.ascontiguousarray(rgba, dtype=np.uint8), w, h, pw, ph


def _stabilize_wrap_blocks(
    rgba_last: np.ndarray,
    rgba_first: np.ndarray,
    blocks_last: bytearray,
    blocks_first: bytes,
    pw: int,
    ph: int,
    *,
    static_mae_thresh: float = 1.2,
) -> int:
    """Circular wrap consistency (Intervention 19): align static 4×4 blocks on frame N-1 to frame 0."""
    bh, bw = ph // 4, pw // 4
    a = rgba_last[..., :3].reshape(bh, 4, bw, 4, 3).astype(np.int16)
    b = rgba_first[..., :3].reshape(bh, 4, bw, 4, 3).astype(np.int16)
    diff = np.abs(a - b)
    mae = diff.mean(axis=(1, 3, 4))      # (bh, bw)
    max_d = diff.max(axis=(1, 3, 4))     # (bh, bw)
    static_wrap = (mae <= static_mae_thresh) & (max_d <= 3)
    if not np.any(static_wrap):
        return 0

    arr_last = np.frombuffer(blocks_last, dtype=np.uint8).reshape(bh, bw, 8)
    arr_first = np.frombuffer(blocks_first, dtype=np.uint8).reshape(bh, bw, 8)
    arr_last[static_wrap] = arr_first[static_wrap]
    return int(static_wrap.sum())


def _encode_sequence_compressonator(frames: Sequence[np.ndarray] | np.ndarray) -> list[bytes]:
    """Encode sequence via pinned AMD Compressonator CLI 4.5.52 (invoked with bash)."""
    blobs: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="czar-cmp-") as td:
        tdir = Path(td)
        for i, f in enumerate(frames):
            rgba, w, h, _, _ = _as_rgba_padded(f)
            in_png = tdir / f"in_{i:02d}.png"
            out_dds = tdir / f"out_{i:02d}.dds"
            Image.fromarray(rgba[..., :3], "RGB").save(in_png, "PNG")
            proc = subprocess.run(
                [
                    "bash",
                    str(COMPRESSONATOR_CLI),
                    "-fd",
                    "BC1",
                    "-miplevels",
                    "1",
                    "-silent",
                    str(in_png),
                    str(out_dds),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            if proc.returncode != 0 or not out_dds.is_file():
                raise RuntimeError(f"Compressonator failed on frame {i}: {proc.stderr.decode('utf-8', 'replace')[:200]}")
            raw = out_dds.read_bytes()
            # Re-wrap payload with canonical 128-byte DXT1 header so PS4 flags/FourCC are exact
            payload = raw[128 : 128 + ((h + 3) // 4) * ((w + 3) // 4) * 8]
            blobs.append(dxt1.dds_header(w, h, len(payload)) + payload)
    return blobs


def encode_sequence_bc1(
    frames: Sequence[np.ndarray] | np.ndarray,
    *,
    backend: str = DEFAULT_OUROBOROS_BC1_BACKEND,
    quality: int | None = None,
    static_mae_thresh: float = 1.2,
    beta_flicker: float = 0.40,
) -> tuple[list[bytes], dict]:
    """Encode an RGB/RGBA sequence to canonical PS4 DXT1a `.dds` blobs using the requested BC1 backend.

    Backends:
      - `rgbcx_temporal`: RGBCX L10 + Temporal Mode A/B + circular wrap consistency (default Ouroboros)
      - `rgbcx`: RGBCX L10 per-frame
      - `icbc`: ICBC quality 5 perceptual per-frame
      - `compressonator`: AMD Compressonator CLI 4.5.52
      - `native` / `canonical`: canonical `app/dxt1.py` (always available fallback)
    """
    t0 = time.perf_counter()
    seq = list(frames)
    n = len(seq)
    if n == 0:
        return [], {"requestedBackend": backend, "actualBackend": backend, "reusedBlocks": 0, "totalBlocks": 0}

    req = (backend or DEFAULT_OUROBOROS_BC1_BACKEND).strip().lower()
    if req in {"canonical", "native"}:
        rgba_iter = (_as_rgba_padded(f)[0][: f.shape[0], : f.shape[1]] for f in seq)
        blobs = [b""] * n
        for idx, blob in dds.encode_sequence(rgba_iter, backend="native"):
            blobs[idx] = blob
        h, w = seq[0].shape[:2]
        tot_b = n * ((h + 3) // 4) * ((w + 3) // 4)
        return blobs, {
            "requestedBackend": req,
            "actualBackend": "native",
            "reusedBlocks": 0,
            "wrapStabilizedBlocks": 0,
            "totalBlocks": tot_b,
            "reuseRatio": 0.0,
            "encodeMs": round((time.perf_counter() - t0) * 1000.0, 1),
        }

    if req == "compressonator" and COMPRESSONATOR_CLI.is_file():
        try:
            blobs = _encode_sequence_compressonator(seq)
            h, w = seq[0].shape[:2]
            tot_b = n * ((h + 3) // 4) * ((w + 3) // 4)
            return blobs, {
                "requestedBackend": req,
                "actualBackend": "compressonator",
                "reusedBlocks": 0,
                "wrapStabilizedBlocks": 0,
                "totalBlocks": tot_b,
                "reuseRatio": 0.0,
                "encodeMs": round((time.perf_counter() - t0) * 1000.0, 1),
            }
        except Exception:
            req = "rgbcx_temporal"

    lib = _load_bc1_lib()
    if lib is None or req not in {"rgbcx_temporal", "rgbcx", "icbc"}:
        # Automatic safe fallback to canonical app/dxt1.py
        blobs, meta = encode_sequence_bc1(seq, backend="native")
        meta["requestedBackend"] = backend
        meta["fallbackUsed"] = True
        return blobs, meta

    backend_id = 1 if req == "icbc" else 0
    q_val = quality if quality is not None else (5 if req == "icbc" else 10)
    temporal_mode = 2 if req == "rgbcx_temporal" else 0

    raw_blocks_list: list[bytearray] = []
    rgba_list: list[np.ndarray] = []
    reused_total = 0
    w0 = h0 = pw0 = ph0 = 0

    for i, f in enumerate(seq):
        rgba, w, h, pw, ph = _as_rgba_padded(f)
        if i == 0:
            w0, h0, pw0, ph0 = w, h, pw, ph
        rgba_list.append(rgba)
        n_blocks = (pw // 4) * (ph // 4)
        out_buf = (ctypes.c_uint8 * (n_blocks * 8))()
        reused_c = ctypes.c_int(0)
        prev_rgba_ptr = rgba_list[i - 1].ctypes.data if (i > 0 and temporal_mode > 0) else 0
        prev_blk_arr = (ctypes.c_uint8 * len(raw_blocks_list[i - 1])).from_buffer(raw_blocks_list[i - 1]) if (i > 0 and temporal_mode > 0) else None
        prev_blk_ptr = ctypes.addressof(prev_blk_arr) if prev_blk_arr is not None else 0

        lib.encode_frame_bc1_ex(
            rgba.ctypes.data,
            pw,
            ph,
            backend_id,
            int(q_val),
            1,  # perceptual weights
            prev_rgba_ptr,
            prev_blk_ptr,
            temporal_mode,
            ctypes.c_float(static_mae_thresh),
            ctypes.c_float(beta_flicker),
            ctypes.addressof(out_buf),
            ctypes.byref(reused_c),
        )
        reused_total += int(reused_c.value)
        raw_blocks_list.append(bytearray(out_buf))

    wrap_stabilized = 0
    if temporal_mode > 0 and n >= 3:
        wrap_stabilized = _stabilize_wrap_blocks(
            rgba_list[-1],
            rgba_list[0],
            raw_blocks_list[-1],
            bytes(raw_blocks_list[0]),
            pw0,
            ph0,
            static_mae_thresh=static_mae_thresh,
        )

    hdr = dxt1.dds_header(w0, h0, (pw0 // 4) * (ph0 // 4) * 8)
    blobs = [hdr + bytes(b) for b in raw_blocks_list]
    total_blocks = n * (pw0 // 4) * (ph0 // 4)
    return blobs, {
        "requestedBackend": backend,
        "actualBackend": req,
        "fallbackUsed": False,
        "quality": q_val,
        "temporalMode": temporal_mode,
        "reusedBlocks": reused_total,
        "wrapStabilizedBlocks": wrap_stabilized,
        "totalBlocks": total_blocks,
        "reuseRatio": round(reused_total / float(max(total_blocks, 1)), 4),
        "encodeMs": round((time.perf_counter() - t0) * 1000.0, 1),
    }


def extract_bc1_endpoints(dds_blob: bytes) -> np.ndarray:
    """Extract (N_blocks, 2) uint16 RGB565 endpoints (c0, c1) from a DXT1 DDS blob."""
    raw = np.frombuffer(dds_blob, dtype=np.uint8, offset=128)
    blocks = raw.reshape(-1, 8)
    c0 = blocks[:, 0].astype(np.uint16) | (blocks[:, 1].astype(np.uint16) << 8)
    c1 = blocks[:, 2].astype(np.uint16) | (blocks[:, 3].astype(np.uint16) << 8)
    return np.stack([c0, c1], axis=-1)
