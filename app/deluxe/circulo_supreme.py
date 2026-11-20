"""Círculo Supreme — full-duration multicriteria take discovery with calibrated costs and 5 categories."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .. import ouroboros_media as io_media
from ..config import ANIMATED_SIZES, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from . import DELUXE_VERSION
from .metrics import evaluate_loop
from .sentinela_supreme import (
    CLASS_CORRECTABLE,
    CLASS_NATURAL,
    CLASS_REGIONAL,
    CLASS_SYNTHESIS,
    CLASS_UNSUITABLE,
    SURVEY_SIZE,
    survey_file,
)

# Canonical wait grid in seconds (25ms .. 150ms)
CANONICAL_WAITS = (0.025, 0.033333, 0.04, 0.05, 0.066667, 0.075, 0.1, 0.125, 0.15)


def duration_penalty(duration_sec: float, source_duration: float) -> float:
    """Penalize implausibly short loops so a 0.4s micro-clip cannot beat a clean 2.4s loop."""
    if source_duration <= 0.8:
        # Short source cannot produce long loops; only penalize < 50% of source
        return max(0.0, (0.5 * source_duration - duration_sec) * 2.5)
    if duration_sec >= 2.0:
        return 0.0
    if duration_sec >= 1.2:
        return round(0.18 * (2.0 - duration_sec) / 0.8, 4)
    if duration_sec >= 0.7:
        return round(0.18 + 0.85 * (1.2 - duration_sec) / 0.5, 4)
    return round(1.03 + 3.0 * (0.7 - duration_sec), 4)


def compute_multicriteria_cost(
    eval_report: dict,
    *,
    duration_sec: float,
    source_duration: float,
    source_fps: float,
    wait: float,
    source_risks: list[str] | tuple[str, ...] = (),
) -> dict:
    """Compute the 9 calibrated multicriteria costs from the 16-metric scientific evaluation."""
    m = eval_report["metrics"]
    vs = m["visualSimilarity"]
    fc = m["flowContinuity"]
    vc = m["velocityContinuity"]
    ac = m["accelerationContinuity"]
    lc = m["luminanceContinuity"]
    cc = m["chromaticContinuity"]
    ec = m["edgeConsistency"]
    fl = m["flicker"]
    gh = m["ghosting"]
    ra = m["reconstructionArtifacts"]
    rv = m["reversals"]
    fz = m["freezes"]
    cd = m["cadenceRegularity"]
    ct = m["cuts"]

    # 1. Appearance error (calibrated against internal transition distribution + local burst)
    appearance = (
        max(0.0, vs["seamRatio"] - 1.0) * 0.85
        + vs["ssimDrop"] * 4.5
        + max(0.0, vs["seamZScore"] - 1.5) * 0.12
        + max(0.0, vs.get("localSeamExcess", 0.0) - 0.9) * 0.65
    )

    # 2. Motion / optical flow error
    if fc["directionMeasured"]:
        motion = (
            max(0.0, fc["flowErrorRatio"] - 1.0) * 0.65
            + (1.0 - max(-1.0, min(1.0, fc["seamDirectionCosine"]))) * 1.4
            + fc["seamOppositeFraction"] * 2.5
        )
    else:
        motion = max(0.0, fc["flowErrorRatio"] - 1.2) * 0.35

    # 3. Velocity error
    velocity = max(0.0, vc["relativeJump"] - 0.15) * 1.4

    # 4. Acceleration error
    acceleration = max(0.0, ac["accelRatio"] - 1.3) * 0.45

    # 5. Photometric error (luminance + chroma)
    photometric = (
        max(0.0, lc["lumRatio"] - 1.3) * 0.35
        + max(0.0, abs(lc["seamDeltaL"]) - 1.2) * 0.12
        + max(0.0, cc["chromaRatio"] - 1.3) * 0.30
        + max(0.0, cc["seamDeltaChroma"] - 1.5) * 0.10
    )

    # 6. Structural error (edge discontinuity + cuts + transient object pop-in/out)
    structural = (
        max(0.0, ec["edgeRatio"] - 1.15) * 0.8
        + max(0.0, ec["edgeZScore"] - 1.8) * 0.12
        + max(0.0, ec.get("localEdgeExcess", 0.0) - 1.1) * 0.55
        + (4.0 if ct["detected"] else 0.0)
    )

    # 7. Cadence error (speed irregularity + excessive deviation from source natural rate)
    cand_fps = 1.0 / max(wait, 1e-6)
    rate_ratio = cand_fps / max(source_fps, 1.0)
    speed_mismatch = 0.0
    if rate_ratio < 0.45:
        speed_mismatch = (0.45 - rate_ratio) * 1.5
    elif rate_ratio > 1.65:
        speed_mismatch = (rate_ratio - 1.65) * 1.2
    cadence = cd["speedCV"] * 0.75 + cd["timingJitter"] * 1.2 + speed_mismatch + (0.45 if cand_fps < 7.5 else 0.0)

    # 8. Duration penalty
    dur_pen = duration_penalty(duration_sec, source_duration)

    # 9. Artifact penalty (reversal, freeze, ghosting, flicker, empty borders, static scene)
    is_static = vs["internalMedianMae"] < 0.25 and vc["internalMedianSpeedPx"] < 0.12 and lc["rangeL"] < 1.2
    artifact = (
        (6.0 if is_static else 0.0)
        + (5.0 if rv["detected"] else 0.0)
        + (3.5 if fz["detected"] else 0.0)
        + (2.5 if any("Retorno" in r or "Corte" in r for r in source_risks) else 0.0)
        + gh["score"] * 1.8
        + (fl["score"] * 0.6 if fl["artificialFlicker"] else fl["score"] * 0.1)
        + ra["emptyBorderFraction"] * 8.0
        + ra["maxFlowFoldover"] * 5.0
    )

    total = (
        1.25 * appearance
        + 1.35 * motion
        + 0.85 * velocity
        + 0.55 * acceleration
        + 0.70 * photometric
        + 1.05 * structural
        + 0.60 * cadence
        + 1.10 * dur_pen
        + 1.50 * artifact
    )

    # Classification for this specific take (strict directional gate prevents opposite-flow takes from CLASS_NATURAL!)
    dir_ok_natural = (not fc["directionMeasured"]) or (
        fc["seamDirectionCosine"] >= 0.45 and fc["seamOppositeFraction"] <= 0.25
    )
    has_hard_defect = bool(
        is_static
        or rv["detected"]
        or ct["detected"]
        or fz["seamFrozen"]
        or any("Retorno" in r or "Corte" in r for r in source_risks)
    )
    if is_static:
        take_class = CLASS_UNSUITABLE
    elif (
        not has_hard_defect
        and dir_ok_natural
        and appearance < 0.55
        and motion < 0.65
        and structural < 0.65
        and photometric < 0.85
        and not fl["artificialFlicker"]
        and not gh["detected"]
    ):
        take_class = CLASS_NATURAL
    elif not is_static and not ct["detected"] and not rv["detected"] and total < 5.2:
        take_class = CLASS_CORRECTABLE
    elif not is_static and not ct["detected"] and not rv["detected"]:
        take_class = CLASS_SYNTHESIS
    else:
        take_class = CLASS_UNSUITABLE

    # Perceptible repetition risk for short loops (< 1.2s repeats > 50 times/minute on PS4 home screen)
    repetition_risk = "alta" if duration_sec < 0.9 else ("moderada" if duration_sec < 1.6 else "baixa")

    return {
        "appearanceError": round(appearance, 4),
        "motionError": round(motion, 4),
        "velocityError": round(velocity, 4),
        "accelerationError": round(acceleration, 4),
        "photometricError": round(photometric, 4),
        "structuralError": round(structural, 4),
        "cadenceError": round(cadence, 4),
        "durationPenalty": round(dur_pen, 4),
        "artifactPenalty": round(artifact, 4),
        "totalCost": round(total, 4),
        "classification": take_class,
        "repetitionRisk": repetition_risk,
        "isStatic": is_static,
    }


def _generate_seeds(
    frames: np.ndarray,
    offset: float,
    step: float,
    size: tuple[int, int],
    source_fps: float,
    source_duration: float,
) -> list[tuple[float, float, float, int, float]]:
    """Generate diverse candidate (rough_score, start, end, frame_count, wait) tuples across full span.

    Uses Schödl et al. (2000) Video Textures multi-frame temporal neighborhood filtering combined
    with signed temporal-derivative (motion-phase) matching so opposite-phase seams (e.g. swaying
    foliage moving left at start and right at end) are rejected before shortlisting.
    """
    cap = max_frames_for(size)
    # 40x22 descriptor for fast vectorized lag bank
    desc = frames[:, ::4, ::4].astype(np.float32)
    n_desc = len(desc)
    if n_desc < 4:
        return []

    # Signed temporal derivative (motion phase descriptor): captures direction of motion at every instant!
    vel = np.diff(desc, axis=0)  # (n_desc - 1, h', w', 3)

    # Build cadence candidates from canonical waits + source-native wait
    waits = sorted(
        {
            w
            for w in CANONICAL_WAITS
            if (1.0 / w) <= max(source_fps * 1.15, 16.0)
        }
        | {round(1.0 / max(min(source_fps, 24.0), 6.0), 6)}
    )
    frame_counts = sorted({c for c in (8, 10, 12, 13, 16, 20, 24, 30, 36, 40, 48, cap) if 4 <= c <= cap})

    lags_to_check: dict[int, list[tuple[int, float]]] = {}
    for w in waits:
        for cnt in frame_counts:
            dur = cnt * w
            lag = int(round(dur / step))
            if 3 <= lag <= n_desc - 1:
                lags_to_check.setdefault(lag, []).append((cnt, w))

    # Cumulative motion activity to reject dead static windows
    step_act = np.abs(vel).mean(axis=(1, 2, 3))
    cum_act = np.r_[0.0, np.cumsum(step_act)]

    # 4-tap Video Textures binomial temporal kernel [0.15, 0.35, 0.35, 0.15]
    vt_kernel = np.array([0.15, 0.35, 0.35, 0.15], dtype=np.float32)

    options: list[tuple[float, float, float, int, float]] = []
    for lag, configs in sorted(lags_to_check.items()):
        dist = np.abs(desc[:-lag] - desc[lag:]).mean(axis=(1, 2, 3))
        if len(dist) == 0:
            continue
        # Multi-frame temporal neighborhood filter (Schödl et al. Video Textures criterion)
        if len(dist) >= 4:
            padded = np.pad(dist, (1, 2), mode="edge")
            app_cost = np.convolve(padded, vt_kernel, mode="valid")[: len(dist)]
        elif len(dist) >= 2:
            nbr = 0.5 * (dist[:-1] + dist[1:])
            app_cost = np.r_[nbr, dist[-1]]
        else:
            app_cost = dist

        # Signed temporal-derivative (motion phase) cost: penalizes opposite direction at start vs end!
        if lag < len(vel):
            vel_dist = np.abs(vel[:-lag] - vel[lag:]).mean(axis=(1, 2, 3))
            if len(vel_dist) >= 2:
                vel_smooth = 0.5 * (vel_dist[:-1] + vel_dist[1:])
                vel_cost = np.r_[vel_smooth, vel_dist[-1]]
            else:
                vel_cost = vel_dist
            # Pad to match len(dist) (vel has length n_desc - 1 -> len(dist) - 1)
            if len(vel_cost) < len(app_cost):
                vel_cost = np.pad(vel_cost, (0, len(app_cost) - len(vel_cost)), mode="edge")
            else:
                vel_cost = vel_cost[: len(app_cost)]
            seam_cost = 0.62 * app_cost + 0.55 * vel_cost
        else:
            seam_cost = app_cost

        act = (cum_act[lag : lag + len(seam_cost)] - cum_act[: len(seam_cost)]) / max(lag, 1)
        # Penalize static windows heavily
        adj_cost = seam_cost + np.where(act < 0.35, 90.0, 0.0)
        seg_count = min(6, len(adj_cost))
        for seg in np.array_split(np.arange(len(adj_cost)), seg_count):
            if not len(seg):
                continue
            rem = seg
            for _ in range(2):
                if not len(rem):
                    break
                idx = int(rem[adj_cost[rem].argmin()])
                start = round(offset + idx * step, 6)
                end = round(min(source_duration, offset + (idx + lag) * step), 6)
                actual_dur = end - start
                if actual_dur <= 0.15:
                    break
                # Pick best matching (count, wait) for this duration
                best_cfg = min(
                    configs,
                    key=lambda cw: abs(cw[0] * cw[1] - actual_dur) + 0.15 * abs(1.0 / cw[1] - min(source_fps, 15.0)) / 10.0,
                )
                cnt = min(cap, max(4, best_cfg[0]))
                wait = float(f"{actual_dur / cnt:.6g}")
                if 0.02 <= wait <= 0.5:
                    # Rough score already incorporates duration plausibility so micro-clips don't crowd out shortlist!
                    rough = float(adj_cost[idx]) + 8.0 * duration_penalty(actual_dur, source_duration)
                    options.append((rough, start, end, cnt, wait))
                rem = rem[np.abs(rem - idx) * step >= max(0.25, actual_dur * 0.25)]

    if not options:
        dur = n_desc * step
        cnt = min(cap, max(4, int(round(dur * min(source_fps, 15.0)))))
        wait = float(f"{dur / cnt:.6g}")
        if 0.02 <= wait <= 5.0:
            options.append((0.0, round(offset, 6), round(offset + dur, 6), cnt, wait))
    return options


def _select_diverse(
    options: list[tuple[float, float, float, int, float]],
    limit: int,
) -> list[tuple[float, float, float, int, float]]:
    chosen: list[tuple[float, float, float, int, float]] = []
    for opt in sorted(options, key=lambda x: x[0]):
        _, a, b, cnt, _ = opt
        if all(
            max(abs(a - x), abs(b - y)) >= max(0.22, min(b - a, y - x) * 0.20) or abs(cnt - oc) >= 6
            for _, x, y, oc, _ in chosen
        ):
            chosen.append(opt)
            if len(chosen) == limit:
                break
    return chosen


def _pick_categories(
    all_candidates: list[dict],
    sentinela_report: dict,
) -> dict:
    """Populate the 5 semantic categories with real, non-fictitious candidates only."""
    valid = [c for c in all_candidates if c["costs"]["classification"] != CLASS_UNSUITABLE]
    naturals = [c for c in valid if c["costs"]["classification"] == CLASS_NATURAL]
    correctables = [c for c in valid if c["costs"]["classification"] == CLASS_CORRECTABLE]

    categories: dict[str, dict | None] = {
        "best_natural": None,
        "best_correctable": None,
        "best_synthesized": None,
        "best_convincing_duration": None,
        "best_balance": None,
    }

    if naturals:
        categories["best_natural"] = min(naturals, key=lambda c: c["costs"]["totalCost"])

    if correctables:
        categories["best_correctable"] = min(correctables, key=lambda c: c["costs"]["totalCost"])
    elif naturals and len(naturals) > 1:
        # Only populate best_correctable when there is an actually correctable candidate;
        # do NOT fabricate a fake one if none exists.
        pass

    # Synthesis candidate: populated if Sentinela indicates synthesis/regional suitability
    # OR if a non-static progressive candidate exists for synthesis escalation.
    synth_eligible = sentinela_report["classification"] in {CLASS_SYNTHESIS, CLASS_REGIONAL} or (
        sentinela_report["levelB"]["progressiveFraction"] >= 0.15
        and not sentinela_report["levelD"]["reverseMotion"]
    )
    if synth_eligible:
        pool_s = [c for c in all_candidates if not c["costs"]["isStatic"]]
        if pool_s:
            best_s = min(
                pool_s,
                key=lambda c: (
                    c["costs"]["artifactPenalty"],
                    c["costs"]["cadenceError"],
                    -c["duration"],
                ),
            )
            synth_copy = dict(best_s)
            synth_copy["recommendedStrategy"] = (
                "genesis_periodic"
                if sentinela_report["levelD"]["spatialRepetition"]["detected"]
                else ("regional_reconstruction" if sentinela_report["levelB"]["hasMixedStaticDynamic"] else "eulerian_synthesis")
            )
            categories["best_synthesized"] = synth_copy

    # Best convincing duration: prefer >= 1.2s among valid takes, balancing duration and cost
    if valid:
        long_pool = [c for c in valid if c["duration"] >= 1.2] or valid
        categories["best_convincing_duration"] = min(
            long_pool,
            key=lambda c: (
                -min(c["duration"], 4.0) * 0.9 + c["costs"]["totalCost"] * 0.45,
                c["costs"]["totalCost"],
            ),
        )

    # Best balance between quality (resolution + low cost) and fluidity (fps + duration)
    if valid:
        def balance_score(c: dict) -> float:
            fps_bonus = min(c["fps"], 24.0) / 24.0
            res_bonus = (c["size"][0] / 1280.0) * 0.55
            dur_bonus = min(c["duration"], 3.0) / 3.0 * 0.65
            return c["costs"]["totalCost"] - (fps_bonus + res_bonus + dur_bonus)

        categories["best_balance"] = min(valid, key=balance_score)

    return categories


def search_supreme(
    path: Path,
    duration: float,
    folder: Path,
    *,
    source_sha256: str = "",
    progress: Callable[[str, int], None] = lambda *_: None,
    check: Callable[[], None] = lambda: None,
    shortlist_per_res: int = 12,
) -> dict:
    """Full-duration multicriteria Círculo Supreme search across all 3 canonical PS4 resolutions."""
    t0 = time.perf_counter()
    sentinela_report, overview = survey_file(path, duration, folder, progress, check)
    info = sentinela_report["source"]
    dur = info["duration"]
    fps = info["sampleFps"] or info["fps"] or 10.0
    step = sentinela_report["levelA"]["stepSec"]

    # Build progressive inspection windows covering the entire duration
    windows = [(overview, 0.0, step)]
    if step > 0.14 and dur > 4.0:
        span = min(6.0, dur)
        change = np.abs(np.diff(overview[:, ::4, ::4].astype(np.float32), axis=0)).mean(axis=(1, 2, 3))
        peak = float(np.clip(int(change.argmax()) * step - span / 2.0, 0.0, max(0.0, dur - span)))
        starts = sorted({0.0, peak, max(0.0, dur - span), *np.linspace(0.0, max(0.0, dur - span), 6).tolist()})
        for st in starts:
            check()
            wn = max(6, min(72, int(round(span * min(fps, 12.0)))))
            w_frames = io_media.extract(path, st, st + span, wn, SURVEY_SIZE, folder, check)
            windows.append((w_frames, st, span / wn))

    turn_secs = [e["second"] for e in sentinela_report["levelD"]["turnEvents"]]
    cut_secs = sentinela_report["levelA"]["cutSeconds"]

    groups = []
    all_candidates: list[dict] = []
    eval_cache: dict[tuple[float, float, int], dict | None] = {}

    for si, size in enumerate(ANIMATED_SIZES):
        options = []
        for w_frames, off, w_step in windows:
            check()
            options.extend(_generate_seeds(w_frames, off, w_step, size, fps, dur))
        shortlist = _select_diverse(options, shortlist_per_res)

        group_candidates = []
        for idx, (_, a, b, cnt, wait) in enumerate(shortlist):
            check()
            progress(
                f"Círculo Supreme · avaliando {size[0]}×{size[1]} ({idx + 1}/{len(shortlist)})",
                25 + si * 23 + int(22 * idx / max(1, len(shortlist))),
            )
            recipe_key = (a, b, cnt)
            if recipe_key not in eval_cache:
                try:
                    sampled = io_media.extract(path, a, b, cnt, SURVEY_SIZE, folder, check)
                except io_media.MotionError:
                    eval_cache[recipe_key] = None
                else:
                    risks = []
                    if any(a < t <= b - wait for t in turn_secs):
                        risks.append("Retorno estrutural observado na fonte.")
                    if any(a < t <= b - wait for t in cut_secs):
                        risks.append("Corte abrupto observado na fonte.")
                    ev = evaluate_loop(sampled, wait, flow_max_width=160, evaluate_dds=False)
                    costs = compute_multicriteria_cost(
                        ev,
                        duration_sec=cnt * wait,
                        source_duration=dur,
                        source_fps=fps,
                        wait=wait,
                        source_risks=risks,
                    )
                    eval_cache[recipe_key] = {"eval": ev, "costs": costs, "risks": risks}

            cached = eval_cache[recipe_key]
            if cached is None:
                continue

            est_bytes = cnt * dds_bytes_for(*size)
            if est_bytes > RAF_SIZE_LIMIT or cnt > max_frames_for(size):
                continue

            candidate = {
                "start": a,
                "end": b,
                "frames": cnt,
                "size": list(size),
                "wait": wait,
                "fps": round(1.0 / wait, 4),
                "duration": round(cnt * wait, 4),
                "estimatedBytes": est_bytes,
                "costs": cached["costs"],
                "evaluation": cached["eval"],
                "sourceRisks": cached["risks"],
                "recipe": {
                    "algorithmVersion": DELUXE_VERSION,
                    "sourceSha256": source_sha256,
                    "start": a,
                    "end": b,
                    "frames": cnt,
                    "wait": wait,
                    "size": list(size),
                    "strategy": "natural_cut" if cached["costs"]["classification"] == CLASS_NATURAL else "needs_refinement",
                    "estimatedBytes": est_bytes,
                },
            }
            group_candidates.append(candidate)

        order_map = {
            CLASS_NATURAL: 0,
            CLASS_CORRECTABLE: 1,
            CLASS_SYNTHESIS: 2,
            CLASS_REGIONAL: 2,
            CLASS_UNSUITABLE: 3,
        }
        group_candidates.sort(
            key=lambda c: (
                order_map.get(c["costs"]["classification"], 3),
                c["costs"]["totalCost"],
                -c["duration"],
            )
        )
        top = group_candidates[:5]
        all_candidates.extend(top)
        groups.append(
            {
                "size": list(size),
                "maxFrames": max_frames_for(size),
                "frameBytes": dds_bytes_for(*size),
                "evaluated": len(group_candidates),
                "unreadable": len(shortlist) - len(group_candidates),
                "classification": top[0]["costs"]["classification"] if top else CLASS_UNSUITABLE,
                "candidates": top,
            }
        )

    categories = _pick_categories(all_candidates, sentinela_report)

    return {
        "version": DELUXE_VERSION,
        "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
        "sentinela": sentinela_report,
        "groups": groups,
        "categories": categories,
        "windows": [[round(st, 4), round(st + len(wf) * wdt, 4)] for wf, st, wdt in windows],
    }
