"""CZAR Temporal Calibrator Supreme — cadence & resolution optimization across canonical PS4 wait intervals."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Callable

import numpy as np

from scipy import optimize as sp_optimize

from ..config import ANIMATED_SIZES, MAX_WAIT_SECONDS, MIN_WAIT_SECONDS, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from . import OUROBOROS_VERSION as DELUXE_VERSION
from . import media_io as io_media
from .gif_inspector import extract_gif_continuous_cadence, inspect_gif, is_gif_file
from .metrics import evaluate_loop, evaluate_post_dds

# Canonical wait intervals in milliseconds (Section 21)
WAIT_GRID_MS = (25.0, 33.333, 40.0, 50.0, 66.667, 75.0, 100.0, 125.0, 150.0)
WAIT_GRID_SEC = tuple(round(ms / 1000.0, 6) for ms in WAIT_GRID_MS)


def optimize_wait_scipy(
    source_span: float,
    frames: int,
    *,
    grid_sec: tuple[float, ...] = WAIT_GRID_SEC,
) -> dict:
    """Use SciPy bounded scalar optimization to find continuous ideal wait & snap to canonical PS4 grid."""
    ideal_lo = max(MIN_WAIT_SECONDS, min(grid_sec))
    ideal_hi = min(MAX_WAIT_SECONDS, max(grid_sec))

    def _objective(w: float) -> float:
        dur = frames * w
        speed_err = abs(math.log2(max(source_span / max(dur, 1e-6), 0.1)))
        fps = 1.0 / max(w, 1e-6)
        judder_pen = max(0.0, (10.0 - fps) / 10.0) * 0.12
        return speed_err * speed_err + judder_pen

    res = sp_optimize.minimize_scalar(_objective, bounds=(ideal_lo, ideal_hi), method="bounded")
    cont_wait = float(res.x)
    snapped = min(grid_sec, key=lambda g: abs(g - cont_wait))
    return {
        "continuousOptimalSec": round(cont_wait, 6),
        "snappedWaitSec": snapped,
        "snappedWaitMs": round(snapped * 1000.0, 3),
        "speedRatioAtSnapped": round(source_span / max(frames * snapped, 1e-6), 4),
    }


def _score_configuration(
    ev: dict,
    *,
    size: tuple[int, int],
    frames: int,
    wait: float,
    source_span: float,
    source_fps: float,
) -> dict:
    """Compute physical motion, judder, fluidity, duration, and budget indicators for a rendered config."""
    m = ev["metrics"]
    vs = m["visualSimilarity"]
    fc = m["flowContinuity"]
    vc = m["velocityContinuity"]
    ac = m["accelerationContinuity"]
    gh = m["ghosting"]
    cd = m["cadenceRegularity"]

    fps = 1.0 / max(wait, 1e-6)
    duration = frames * wait
    total_bytes = frames * dds_bytes_for(*size)
    max_f = max_frames_for(size)
    speed_px = vc["internalMedianSpeedPx"]

    # Speed fidelity: natural playback speed is duration ≈ source_span (speedRatio ≈ 1.0)
    speed_ratio = source_span / max(duration, 1e-6)
    speed_distortion = abs(math.log2(max(speed_ratio, 0.1)))

    # Judder risk: large per-frame pixel jumps at low FPS or erratic cadence
    judder_risk = float(
        np.clip(
            max(0.0, (speed_px - 6.0) / 14.0) * (15.0 / max(fps, 5.0))
            + max(0.0, (10.0 - fps) / 12.0) * 0.45
            + cd["speedCV"] * 0.6,
            0.0,
            2.0,
        )
    )

    # Perceived fluidity (higher is better): rewards smooth fps & low judder without warping natural speed
    fluidity_score = float(
        min(fps, 30.0) / 30.0 * 1.4
        - judder_risk * 0.85
        - gh["score"] * 0.9
        - speed_distortion * 0.7
    )

    # Continuity score (higher is better): rewards clean seam & motion flow consistency
    continuity_score = float(
        vs["seamSsim"] * 2.0
        - max(0.0, vs["seamRatio"] - 1.0) * 0.9
        - max(0.0, fc["flowErrorRatio"] - 1.0) * 0.55
        - vc["relativeJump"] * 0.8
        - (2.5 if m["reversals"]["detected"] else 0.0)
        - (1.5 if m["freezes"]["detected"] else 0.0)
    )

    # Definition score (higher is better): resolution clarity weighted by visual fidelity & non-severe judder
    res_factor = {1280: 1.0, 960: 0.78, 640: 0.55}.get(size[0], size[0] / 1280.0)
    definition_score = float(
        res_factor * 1.5
        + vs["seamSsim"] * 0.8
        - judder_risk * 0.45
        - speed_distortion * 0.4
    )

    # Convincing duration score (higher is better): rewards >= 1.2s without sacrificing continuity or natural speed
    duration_score = float(
        min(duration, 4.0) / 4.0 * 1.5
        + continuity_score * 0.35
        - judder_risk * 0.35
        - speed_distortion * 0.8
    )

    # Overall balanced score
    balance_score = float(
        0.30 * fluidity_score
        + 0.25 * definition_score
        + 0.25 * continuity_score
        + 0.20 * duration_score
        - len(ev["issues"]) * 0.25
    )

    return {
        "size": list(size),
        "frames": frames,
        "maxFrames": max_f,
        "wait": wait,
        "waitMs": round(wait * 1000.0, 3),
        "fps": round(fps, 3),
        "duration": round(duration, 4),
        "sourceSpan": round(source_span, 4),
        "speedRatio": round(speed_ratio, 4),
        "ddsBytes": total_bytes,
        "budgetPercent": round(total_bytes / float(RAF_SIZE_LIMIT) * 100.0, 2),
        "displacementPx": round(speed_px, 4),
        "velocityJump": vc["relativeJump"],
        "accelRatio": ac["accelRatio"],
        "judderRisk": round(judder_risk, 4),
        "ghostingRisk": gh["score"],
        "seamSsim": vs["seamSsim"],
        "seamRatio": vs["seamRatio"],
        "issues": ev["issues"],
        "scores": {
            "fluidity": round(fluidity_score, 4),
            "definition": round(definition_score, 4),
            "continuity": round(continuity_score, 4),
            "duration": round(duration_score, 4),
            "balance": round(balance_score, 4),
        },
    }


def calibrate_interval(
    source_path: Path,
    start: float,
    end: float,
    source_fps: float,
    folder: Path,
    *,
    sizes: tuple[tuple[int, int], ...] = tuple(ANIMATED_SIZES),
    waits_sec: tuple[float, ...] = WAIT_GRID_SEC,
    verify_dds: bool = False,
    check: Callable[[], None] = lambda: None,
) -> dict:
    """Evaluate real rendered configurations across resolutions & canonical waits for `[start, end]`."""
    t0 = time.perf_counter()
    span = float(end - start)
    if span <= 0.05:
        raise ValueError("Intervalo temporal inválido para calibração.")

    gif_info = inspect_gif(source_path, work_dir=folder, check=check) if is_gif_file(source_path) else None
    distinct_gif_frames = int(gif_info["distinctFrameCount"]) if gif_info and gif_info.get("isGif") else None

    configs: list[dict] = []
    eval_fn = evaluate_post_dds if verify_dds else evaluate_loop
    max_cache_bytes = 64 * 1024 * 1024
    extract_cache: dict[tuple[int, tuple[int, int]], np.ndarray] = {}
    cache_order: list[tuple[int, tuple[int, int]]] = []
    cache_bytes = 0

    def _get_frames(n_frames: int, render_sz: tuple[int, int]) -> np.ndarray:
        nonlocal cache_bytes
        ckey = (n_frames, render_sz)
        cached = extract_cache.get(ckey)
        if cached is not None:
            return cached
        if gif_info and gif_info.get("isGif"):
            arr, _ = extract_gif_continuous_cadence(
                source_path, start, end, n_frames, render_sz, folder, mode="auto", check=check
            )
        else:
            arr = np.array(io_media.extract(source_path, start, end, n_frames, render_sz, folder, check))
        arr_bytes = int(arr.nbytes)
        if arr_bytes <= max_cache_bytes:
            while cache_bytes + arr_bytes > max_cache_bytes and cache_order:
                evict_key = cache_order.pop(0)
                evicted = extract_cache.pop(evict_key, None)
                if evicted is not None:
                    cache_bytes -= int(evicted.nbytes)
            extract_cache[ckey] = arr
            cache_order.append(ckey)
            cache_bytes += arr_bytes
        return arr

    try:
        for size in sizes:
            cap = max_frames_for(size)
            seen_counts: set[int] = set()
            for w_sec in waits_sec:
                if not (MIN_WAIT_SECONDS <= w_sec <= MAX_WAIT_SECONDS):
                    continue
                nat_n = int(round(span / w_sec))
                # Never inflate a low-frame GIF by duplicating frames to simulate smoothness
                if distinct_gif_frames is not None and not gif_info.get("hasVariableDelays"):
                    nat_n = min(nat_n, distinct_gif_frames)
                n = max(4, min(cap, nat_n))
                if n * dds_bytes_for(*size) > RAF_SIZE_LIMIT:
                    continue
                actual_wait = float(f"{w_sec:.6g}")
                playback_dur = n * actual_wait
                speed_ratio = span / max(playback_dur, 1e-6)
                if not (0.65 <= speed_ratio <= 1.45):
                    continue
                if n in seen_counts and abs(speed_ratio - 1.0) > 0.15:
                    continue
                seen_counts.add(n)

                check()
                try:
                    render_sz = size if verify_dds else (min(size[0], 320), min(size[1], 180))
                    rendered = _get_frames(n, render_sz)
                except (io_media.MotionError, ValueError):
                    continue

                ev = eval_fn(rendered, actual_wait)
                scored = _score_configuration(
                    ev,
                    size=size,
                    frames=n,
                    wait=actual_wait,
                    source_span=span,
                    source_fps=source_fps,
                )
                scored["scipyOptimization"] = optimize_wait_scipy(span, n, grid_sec=waits_sec)
                configs.append(scored)

        if not configs:
            for size in sizes:
                cap = max_frames_for(size)
                n = max(4, min(cap, int(round(span * min(source_fps, 15.0)))))
                opt = optimize_wait_scipy(span, n, grid_sec=waits_sec)
                w_sec = opt["snappedWaitSec"]
                render_sz = size if verify_dds else (min(size[0], 320), min(size[1], 180))
                rendered = _get_frames(n, render_sz)
                ev = eval_fn(rendered, w_sec)
                scored = _score_configuration(ev, size=size, frames=n, wait=w_sec, source_span=span, source_fps=source_fps)
                scored["scipyOptimization"] = opt
                configs.append(scored)
    finally:
        extract_cache.clear()
        cache_order.clear()

    recommendations = {
        "best_fluidity": max(configs, key=lambda c: c["scores"]["fluidity"]),
        "best_definition": max(configs, key=lambda c: c["scores"]["definition"]),
        "best_continuity": max(configs, key=lambda c: c["scores"]["continuity"]),
        "best_duration": max(configs, key=lambda c: c["scores"]["duration"]),
        "best_balance": max(configs, key=lambda c: c["scores"]["balance"]),
    }

    return {
        "version": DELUXE_VERSION,
        "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
        "start": round(start, 6),
        "end": round(end, 6),
        "sourceSpan": round(span, 4),
        "sourceFps": round(source_fps, 3),
        "candidateWaitsMs": list(WAIT_GRID_MS),
        "rafXmlTimingSemantics": {
            "uniformWaitPerCase": True,
            "physicalPs4Verified": False,
            "note": "Intervalos são validados contra app/raf.py (wait uniforme em segundos); suporte físico de cada intervalo no PS4 Slim 12.52 requer teste no console.",
        },
        "gifTiming": gif_info,
        "evaluatedCount": len(configs),
        "configurations": sorted(configs, key=lambda c: c["scores"]["balance"], reverse=True),
        "recommendations": recommendations,
    }
