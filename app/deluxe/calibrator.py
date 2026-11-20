"""CZAR Temporal Calibrator Supreme — cadence & resolution optimization across canonical PS4 wait intervals."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .. import ouroboros_media as io_media
from ..config import ANIMATED_SIZES, MAX_WAIT_SECONDS, MIN_WAIT_SECONDS, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from . import DELUXE_VERSION
from .metrics import evaluate_loop, evaluate_post_dds

# Canonical wait intervals in milliseconds (Section 11)
WAIT_GRID_MS = (25.0, 33.333, 40.0, 50.0, 66.667, 75.0, 100.0, 125.0, 150.0)
WAIT_GRID_SEC = tuple(round(ms / 1000.0, 6) for ms in WAIT_GRID_MS)


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

    configs: list[dict] = []
    # Cache rendered proxy evaluations per (frames, size) so we don't re-decode redundantly
    eval_fn = evaluate_post_dds if verify_dds else evaluate_loop

    for size in sizes:
        cap = max_frames_for(size)
        seen_counts: set[int] = set()
        for w_sec in waits_sec:
            if not (MIN_WAIT_SECONDS <= w_sec <= MAX_WAIT_SECONDS):
                continue
            # Natural frame count preserving the source's true temporal speed: N ≈ span / wait
            nat_n = int(round(span / w_sec))
            # Allow clamping to cap ONLY if resulting speed change stays reasonable (within 0.65x..1.45x)
            n = max(4, min(cap, nat_n))
            if n * dds_bytes_for(*size) > RAF_SIZE_LIMIT:
                continue
            actual_wait = float(f"{w_sec:.6g}")
            playback_dur = n * actual_wait
            speed_ratio = span / max(playback_dur, 1e-6)
            # Never arbitrarily speed up slow sources or slow down fast sources!
            if not (0.65 <= speed_ratio <= 1.45):
                continue
            if n in seen_counts and abs(speed_ratio - 1.0) > 0.15:
                continue
            seen_counts.add(n)

            check()
            try:
                # Render at actual output size when verify_dds=True, or 240p-equivalent proxy scaled cleanly
                render_sz = size if verify_dds else (min(size[0], 320), min(size[1], 180))
                rendered = np.array(io_media.extract(source_path, start, end, n, render_sz, folder, check))
            except io_media.MotionError:
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
            configs.append(scored)

    if not configs:
        # Fallback: compute best-fit natural config per resolution using exact span / N
        for size in sizes:
            cap = max_frames_for(size)
            n = max(4, min(cap, int(round(span * min(source_fps, 15.0)))))
            w_sec = float(f"{np.clip(span / n, MIN_WAIT_SECONDS, MAX_WAIT_SECONDS):.6g}")
            render_sz = size if verify_dds else (min(size[0], 320), min(size[1], 180))
            rendered = np.array(io_media.extract(source_path, start, end, n, render_sz, folder, check))
            ev = eval_fn(rendered, w_sec)
            configs.append(
                _score_configuration(ev, size=size, frames=n, wait=w_sec, source_span=span, source_fps=source_fps)
            )

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
        "evaluatedCount": len(configs),
        "configurations": sorted(configs, key=lambda c: c["scores"]["balance"], reverse=True),
        "recommendations": recommendations,
    }
