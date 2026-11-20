"""Gifsicle-backed GIF temporal inspection, unoptimization, and Mode A / Mode B cadence reconstruction.

Solves the historic variable-delay GIF problem (e.g. frames alternating between 50 ms and 150 ms):
- Uses `gifsicle --info` to read true per-frame delays, disposal methods, transparency,
  and partial bounding boxes (optimized GIFs).
- Uses `gifsicle --unoptimize` to expand frames faithfully before analysis (never using
  Gifsicle as a motion synthesizer).
- Evaluates:
    MODE A — PRESERVE ORIGINAL TIMING (feasible only when delays are uniform within the
             canonical PS4 RAF uniform `<case wait>` schema; otherwise explicitly reports
             the limitation without faking smoothness via frame duplication).
    MODE B — RECONSTRUCT CADENCE FOR CONTINUOUS MOTION (resamples along the true cumulative
             GIF timeline onto canonical PS4 RAF intervals using flow-consistency-gated
             OpenCV warping only when bidirectional flow confidence is high).
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
from PIL import Image

from ..config import MAX_WAIT_SECONDS, MIN_WAIT_SECONDS, PROJECT_ROOT, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from .flow import compute_flow_capped, to_gray_u8, warp_frame

WAIT_GRID_MS = (25.0, 33.333, 40.0, 50.0, 66.667, 75.0, 100.0, 125.0, 150.0)


def find_gifsicle() -> Path | None:
    """Locate bundled user-space tools/bin/gifsicle or system gifsicle."""
    local = PROJECT_ROOT / "tools" / "bin" / "gifsicle"
    if local.is_file() and os.access(local, os.X_OK):
        return local
    found = shutil.which("gifsicle")
    return Path(found) if found else None


def is_gif_file(path: Path) -> bool:
    """Detect real GIF container via binary magic header (GIF87a / GIF89a)."""
    try:
        with path.open("rb") as fh:
            hdr = fh.read(6)
        return hdr in (b"GIF87a", b"GIF89a")
    except OSError:
        return False


def _parse_gifsicle_info(text: str) -> dict[str, Any]:
    """Parse `gifsicle --info` output into per-frame structured metadata."""
    screen_w, screen_h = 0, 0
    m_scr = re.search(r"logical screen\s+(\d+)x(\d+)", text)
    if m_scr:
        screen_w, screen_h = int(m_scr.group(1)), int(m_scr.group(2))

    loop_mode = "none"
    loop_count: int | None = None
    if re.search(r"loop forever", text):
        loop_mode = "forever"
        loop_count = 0
    else:
        m_loop = re.search(r"loop count\s+(\d+)", text)
        if m_loop:
            loop_mode = "finite"
            loop_count = int(m_loop.group(1))

    frames: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        m_img = re.match(
            r"^\+\s+image\s+#(\d+)\s+(\d+)x(\d+)(?:\s+at\s+(\d+),(\d+))?(.*)$",
            line,
        )
        if m_img:
            if cur is not None:
                frames.append(cur)
            idx = int(m_img.group(1))
            w = int(m_img.group(2))
            h = int(m_img.group(3))
            ox = int(m_img.group(4)) if m_img.group(4) is not None else 0
            oy = int(m_img.group(5)) if m_img.group(5) is not None else 0
            rest = m_img.group(6) or ""
            m_tr = re.search(r"transparent\s+(\d+)", rest)
            is_partial = bool((screen_w and w < screen_w) or (screen_h and h < screen_h) or ox != 0 or oy != 0)
            cur = {
                "index": idx,
                "width": w,
                "height": h,
                "offsetX": ox,
                "offsetY": oy,
                "isPartial": is_partial,
                "transparentIndex": int(m_tr.group(1)) if m_tr else None,
                "hasTransparency": m_tr is not None,
                "disposal": "none",
                "delaySec": 0.1,
                "delayMs": 100.0,
            }
            continue
        if cur is not None:
            m_disp = re.search(r"disposal\s+(\w+)", line)
            if m_disp:
                cur["disposal"] = m_disp.group(1).lower()
            m_del = re.search(r"delay\s+([0-9]+(?:\.[0-9]+)?)s", line)
            if m_del:
                d_sec = max(0.01, float(m_del.group(1)))
                cur["delaySec"] = round(d_sec, 4)
                cur["delayMs"] = round(d_sec * 1000.0, 2)
    if cur is not None:
        frames.append(cur)

    return {
        "screenWidth": screen_w,
        "screenHeight": screen_h,
        "loopMode": loop_mode,
        "loopCount": loop_count,
        "frames": frames,
    }


def _scale_crop_rgb(rgb: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Aspect-preserving fill + center crop to exact PS4 16:9 target size (matches FFmpeg scale_crop)."""
    tw, th = size
    sh, sw = rgb.shape[:2]
    if sw == tw and sh == th:
        return rgb
    scale = max(tw / float(sw), th / float(sh))
    nw = max(tw, int(round(sw * scale)))
    nh = max(th, int(round(sh * scale)))
    im = Image.fromarray(rgb, "RGB").resize((nw, nh), Image.Resampling.LANCZOS)
    x0 = (nw - tw) // 2
    y0 = (nh - th) // 2
    return np.asarray(im.crop((x0, y0, x0 + tw, y0 + th)), dtype=np.uint8)


def unoptimize_gif_frames(
    source_path: Path,
    work_dir: Path | None = None,
    *,
    bg_rgb: tuple[int, int, int] = (0, 0, 0),
    check: Callable[[], None] = lambda: None,
) -> tuple[list[np.ndarray], list[float], bool]:
    """Expand an optimized/partial/disposal GIF via `gifsicle --unoptimize` into full opaque RGB frames."""
    gs = find_gifsicle()
    if work_dir is not None:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
    tmp_ctx = tempfile.TemporaryDirectory(prefix="czar-gif-unopt-", dir=work_dir)
    try:
        td = Path(tmp_ctx.name)
        read_path = source_path
        used_gifsicle = False
        if gs is not None:
            out_gif = td / "unoptimized.gif"
            proc = subprocess.run(
                [str(gs), "--unoptimize", str(source_path), "-o", str(out_gif)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            if proc.returncode == 0 and out_gif.is_file() and out_gif.stat().st_size > 0:
                read_path = out_gif
                used_gifsicle = True

        frames_rgb: list[np.ndarray] = []
        delays_sec: list[float] = []
        with Image.open(read_path) as im:
            n_frames = getattr(im, "n_frames", 1)
            bg = Image.new("RGBA", im.size, (*bg_rgb, 255))
            for i in range(n_frames):
                check()
                im.seek(i)
                d_ms = float(im.info.get("duration", 100) or 100)
                delays_sec.append(max(0.01, d_ms / 1000.0))
                rgba = im.convert("RGBA")
                # Premultiplied clean alpha composite over solid background (no black fringe)
                comp = Image.alpha_composite(bg, rgba).convert("RGB")
                frames_rgb.append(np.asarray(comp, dtype=np.uint8))
        return frames_rgb, delays_sec, used_gifsicle
    finally:
        tmp_ctx.cleanup()


def inspect_gif(
    source_path: Path,
    *,
    work_dir: Path | None = None,
    check: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """Complete Gifsicle-backed GIF inspection + Mode A (Preserve) vs Mode B (Reconstruct) evaluation."""
    if not is_gif_file(source_path):
        return {"isGif": False}

    gs = find_gifsicle()
    parsed: dict[str, Any]
    if gs is not None:
        proc = subprocess.run(
            [str(gs), "--info", str(source_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if proc.returncode == 0:
            parsed = _parse_gifsicle_info(proc.stdout.decode("utf-8", "replace"))
        else:
            parsed = {"screenWidth": 0, "screenHeight": 0, "loopMode": "unknown", "loopCount": None, "frames": []}
    else:
        parsed = {"screenWidth": 0, "screenHeight": 0, "loopMode": "unknown", "loopCount": None, "frames": []}

    frames_rgb, unopt_delays, used_unopt = unoptimize_gif_frames(source_path, work_dir=work_dir, check=check)
    n_frames = len(frames_rgb)
    frame_meta = parsed["frames"]

    # Prefer gifsicle delays when frame counts match, otherwise Pillow unoptimized delays
    if len(frame_meta) == n_frames and n_frames > 0:
        delays_ms = [float(f["delayMs"]) for f in frame_meta]
    else:
        delays_ms = [round(d * 1000.0, 2) for d in unopt_delays]

    delays_sec = [max(0.01, d / 1000.0) for d in delays_ms]
    total_dur = float(sum(delays_sec))
    cum_times = [0.0]
    for d in delays_sec[:-1]:
        cum_times.append(round(cum_times[-1] + d, 5))

    # Detect consecutive repeated frames (pixel MAE < 0.5)
    repeated_indices: list[int] = []
    for i in range(1, n_frames):
        mae = float(np.mean(np.abs(frames_rgb[i].astype(np.int16) - frames_rgb[i - 1].astype(np.int16))))
        if mae < 0.5:
            repeated_indices.append(i)
    distinct_count = max(1, n_frames - len(repeated_indices))

    arr_d = np.asarray(delays_ms, dtype=np.float64) if delays_ms else np.array([100.0])
    min_ms = float(np.min(arr_d))
    max_ms = float(np.max(arr_d))
    med_ms = float(np.median(arr_d))
    mean_ms = float(np.mean(arr_d))
    delay_cv = float(np.std(arr_d) / max(mean_ms, 1e-6))
    unique_ms = sorted({round(x, 1) for x in delays_ms})
    has_variable_delays = bool(len(unique_ms) > 1 and (delay_cv > 0.08 or (max_ms - min_ms) > max(10.0, 0.12 * med_ms)))

    partial_count = sum(1 for f in frame_meta if f.get("isPartial"))
    transparent_count = sum(1 for f in frame_meta if f.get("hasTransparency"))
    disposals = sorted({str(f.get("disposal", "none")) for f in frame_meta}) or ["none"]

    # MODE A vs MODE B evaluation against canonical PS4 RAF uniform `<case wait>` semantics
    mode_a_feasible = bool(
        not has_variable_delays
        and (MIN_WAIT_SECONDS * 1000.0 <= med_ms <= MAX_WAIT_SECONDS * 1000.0)
        and distinct_count >= 2
    )
    if mode_a_feasible:
        mode_a_reason = (
            f"Atrasos uniformes ({med_ms:.1f} ms): o RAF canônico do PS4 reproduz fielmente "
            "a temporização original sem reconstrução."
        )
    elif has_variable_delays:
        mode_a_reason = (
            f"Atrasos variáveis detectados ({min_ms:.0f} ms a {max_ms:.0f} ms; valores: {unique_ms} ms). "
            "O XML/RAF canônico do PS4 usa <case wait> uniforme por transição; portanto o Modo A não "
            "reproduz atrasos heterogêneos quadro a quadro sem quebrar o contrato canônico, e duplicação "
            "artificial de quadros não é usada como solução de suavidade. Recomenda-se o Modo B."
        )
    else:
        mode_a_reason = "Temporização original fora dos limites canônicos do PS4 (20 ms a 5000 ms)."

    # Choose best canonical uniform PS4 interval for MODE B matching total_dur & avoiding frame duplication
    mode_b_candidates: list[dict[str, Any]] = []
    for w_ms in WAIT_GRID_MS:
        w_sec = w_ms / 1000.0
        target_n = int(round(total_dur / w_sec)) if total_dur > 0 else n_frames
        target_n = max(4, min(48, target_n))
        # Penalize if target_n would exceed distinct source frames excessively without true motion
        playback_dur = target_n * w_sec
        speed_ratio = total_dur / max(playback_dur, 1e-6) if total_dur > 0 else 1.0
        speed_err = abs(math.log2(max(speed_ratio, 0.1)))
        # Measure alignment error between uniform sample timestamps and original cumulative timestamps
        uniform_ts = np.linspace(0.0, max(total_dur - 1e-4, 0.01), target_n, endpoint=False)
        src_ts = np.asarray(cum_times, dtype=np.float64)
        phase_err = float(np.mean(np.min(np.abs(uniform_ts[:, None] - src_ts[None, :]), axis=1))) if src_ts.size else 0.0
        dup_penalty = max(0.0, (target_n - distinct_count) / max(distinct_count, 1)) * 0.65
        cost = speed_err * 1.4 + (phase_err / max(w_sec, 0.01)) * 0.55 + dup_penalty
        mode_b_candidates.append(
            {
                "waitMs": w_ms,
                "waitSec": round(w_sec, 6),
                "frames": target_n,
                "durationSec": round(playback_dur, 4),
                "speedRatio": round(speed_ratio, 4),
                "phaseErrorMs": round(phase_err * 1000.0, 2),
                "cost": round(cost, 4),
            }
        )
    mode_b_candidates.sort(key=lambda c: c["cost"])
    best_b = mode_b_candidates[0]

    recommended_mode = "mode_a_preserve" if mode_a_feasible else "mode_b_reconstruct"

    return {
        "isGif": True,
        "tool": "gifsicle" if gs is not None else "pillow-fallback",
        "usedUnoptimize": used_unopt,
        "screenSize": [parsed["screenWidth"] or (frames_rgb[0].shape[1] if n_frames else 0),
                       parsed["screenHeight"] or (frames_rgb[0].shape[0] if n_frames else 0)],
        "loopMode": parsed["loopMode"],
        "loopCount": parsed["loopCount"],
        "frameCount": n_frames,
        "distinctFrameCount": distinct_count,
        "repeatedFrameCount": len(repeated_indices),
        "repeatedIndices": repeated_indices,
        "partialFrameCount": partial_count,
        "isOptimized": bool(partial_count > 0),
        "hasTransparency": bool(transparent_count > 0),
        "transparentFrameCount": transparent_count,
        "disposals": disposals,
        "delaysMs": [round(x, 2) for x in delays_ms],
        "uniqueDelaysMs": unique_ms,
        "minDelayMs": round(min_ms, 2),
        "maxDelayMs": round(max_ms, 2),
        "medianDelayMs": round(med_ms, 2),
        "meanDelayMs": round(mean_ms, 2),
        "delayCV": round(delay_cv, 4),
        "hasVariableDelays": has_variable_delays,
        "totalDurationSec": round(total_dur, 4),
        "cumulativeTimesSec": cum_times,
        "rafTimingSupport": {
            "canonicalUniformWaitSupported": True,
            "perFrameVariableWaitInCanonicalXml": False,
            "note": "app/raf.py gera <case wait> uniforme em segundos; compatibilidade física de intervalos específicos no PS4 Slim 12.52 permanece pendente de ensaio físico.",
        },
        "modeA_preserve": {
            "id": "mode_a_preserve",
            "label": "MODO A — PRESERVAR TEMPORIZAÇÃO ORIGINAL",
            "feasible": mode_a_feasible,
            "exactMatch": mode_a_feasible,
            "recommendedWaitMs": round(med_ms, 2) if mode_a_feasible else None,
            "reason": mode_a_reason,
        },
        "modeB_reconstruct": {
            "id": "mode_b_reconstruct",
            "label": "MODO B — RECONSTRUIR CADÊNCIA PARA MOVIMENTO CONTÍNUO",
            "feasible": distinct_count >= 2,
            "recommended": best_b,
            "candidates": mode_b_candidates[:5],
            "reason": (
                f"Reamostragem contínua ao longo da linha do tempo cumulativa real ({total_dur:.2f}s) "
                f"para cadência uniforme PS4 de {best_b['waitMs']} ms ({best_b['frames']} quadros, sem duplicação enganosa)."
            ),
        },
        "recommendedMode": recommended_mode,
        "frames": frame_meta,
    }


def _interpolate_pair_flow_safe(
    img_a: np.ndarray,
    img_b: np.ndarray,
    alpha: float,
) -> np.ndarray:
    """Bidirectional flow-guided temporal interpolation with consistency gating (avoids ghosting on cuts)."""
    if alpha <= 0.12:
        return img_a
    if alpha >= 0.88:
        return img_b

    ga = to_gray_u8(img_a)
    gb = to_gray_u8(img_b)
    # Check if pair is almost identical or a hard cut
    mae = float(np.mean(np.abs(ga.astype(np.float32) - gb.astype(np.float32))))
    if mae < 0.8:
        return img_a
    if mae > 58.0:
        return img_a if alpha < 0.5 else img_b

    fl = compute_flow_capped(ga, gb, max_width=320)
    fwd = fl.fwd
    bwd = fl.bwd
    mag = np.hypot(fwd[..., 0], fwd[..., 1])
    med_mag = float(np.median(mag))
    med_err = float(np.median(fl.fb_error))
    # If flow is inconsistent or near-zero, snap cleanly without creating ghosting
    if med_mag < 0.25 or med_err > max(2.2, med_mag * 0.45):
        return img_a if alpha < 0.5 else img_b

    warped_a = warp_frame(img_a, alpha * fwd)
    warped_b = warp_frame(img_b, (1.0 - alpha) * bwd)
    blended = (1.0 - alpha) * warped_a.astype(np.float32) + alpha * warped_b.astype(np.float32)
    return np.clip(np.rint(blended), 0, 255).astype(np.uint8)


def extract_gif_continuous_cadence(
    source_path: Path,
    start: float,
    end: float,
    count: int,
    size: tuple[int, int],
    work_dir: Path,
    *,
    mode: str = "auto",
    check: Callable[[], None] = lambda: None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Extract `count` frames at `size` from a GIF using Gifsicle unoptimization + Mode A/B timeline resampling."""
    gif_info = inspect_gif(source_path, work_dir=work_dir, check=check)
    frames_rgb, delays_sec, _ = unoptimize_gif_frames(source_path, work_dir=work_dir, check=check)
    if not frames_rgb:
        raise ValueError("GIF não contém quadros decodificáveis.")

    # Collapse strictly identical consecutive frames while accumulating their true hold duration
    distinct_frames: list[np.ndarray] = [frames_rgb[0]]
    distinct_durs: list[float] = [delays_sec[0]]
    for i in range(1, len(frames_rgb)):
        mae = float(np.mean(np.abs(frames_rgb[i].astype(np.int16) - distinct_frames[-1].astype(np.int16))))
        if mae < 0.5:
            distinct_durs[-1] += delays_sec[i]
        else:
            distinct_frames.append(frames_rgb[i])
            distinct_durs.append(delays_sec[i])

    # Scale once to target PS4 16:9 resolution
    scaled = [_scale_crop_rgb(f, size) for f in distinct_frames]
    total_dur = max(0.05, float(sum(distinct_durs)))
    starts = [0.0]
    for d in distinct_durs[:-1]:
        starts.append(starts[-1] + d)
    starts_arr = np.asarray(starts, dtype=np.float64)

    eff_mode = mode
    if eff_mode == "auto":
        eff_mode = gif_info["recommendedMode"]

    s0 = max(0.0, min(float(start), max(0.0, total_dur - 0.02)))
    s1 = min(total_dur, max(s0 + 0.02, float(end)))
    sample_ts = np.linspace(s0, s1, count, endpoint=False)

    out_frames: list[np.ndarray] = []
    interpolated_count = 0
    for ts in sample_ts:
        check()
        t_wrap = float(ts % total_dur)
        idx = int(np.searchsorted(starts_arr, t_wrap, side="right")) - 1
        idx = max(0, min(len(scaled) - 1, idx))
        nxt = (idx + 1) % len(scaled)
        dur_k = max(distinct_durs[idx], 1e-4)
        alpha = float(np.clip((t_wrap - starts_arr[idx]) / dur_k, 0.0, 1.0))

        if eff_mode == "mode_b_reconstruct" and len(scaled) >= 2 and 0.15 < alpha < 0.85:
            interp = _interpolate_pair_flow_safe(scaled[idx], scaled[nxt], alpha)
            if interp is not scaled[idx] and interp is not scaled[nxt]:
                interpolated_count += 1
            out_frames.append(interp)
        else:
            out_frames.append(scaled[idx] if alpha < 0.5 else scaled[nxt])

    meta = {
        "appliedMode": eff_mode,
        "hasVariableDelays": gif_info["hasVariableDelays"],
        "distinctSourceFrames": len(scaled),
        "interpolatedFrames": interpolated_count,
        "totalDurationSec": round(total_dur, 4),
    }
    return np.stack(out_frames, axis=0), meta
