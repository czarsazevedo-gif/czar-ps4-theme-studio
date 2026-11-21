"""Círculo Supreme — full-duration multicriteria take discovery with calibrated costs and 5 categories."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Callable

import numpy as np

from . import media_io as io_media
from ..config import ANIMATED_SIZES, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from . import OUROBOROS_VERSION as DELUXE_VERSION
from .metrics import evaluate_loop
from .analyzer import (
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

    # 4. Acceleration & jerk continuity (Intervention 12: position -> velocity -> acceleration)
    accel_pen = max(0.0, ac["accelRatio"] - 1.25) * 0.65
    if ac.get("seamAccelPx", 0.0) > 1.2 and ac["accelRatio"] > 2.0:
        accel_pen += (ac["seamAccelPx"] - 1.2) * 0.25
    acceleration = accel_pen

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

    # 7. Cadence & RAF-budget-aware smoothness (Intervention 14: prevent slideshows & excessive per-frame jump!)
    cand_fps = 1.0 / max(wait, 1e-6)
    rate_ratio = cand_fps / max(source_fps, 1.0)
    speed_mismatch = 0.0
    if rate_ratio < 0.45:
        speed_mismatch = (0.45 - rate_ratio) * 1.5
    elif rate_ratio > 1.65:
        speed_mismatch = (rate_ratio - 1.65) * 1.2
    slideshow_pen = 0.0
    if cand_fps < 6.0:
        slideshow_pen = (6.0 - cand_fps) * 0.55
    elif cand_fps < 7.5:
        slideshow_pen = (7.5 - cand_fps) * 0.22
    # Penalize excessive per-frame displacement or low inter-frame SSIM caused by stretching few frames over long span
    med_ssim = float(vs.get("internalMedianSsim", 0.95))
    fragmentation_pen = max(0.0, 0.84 - med_ssim) * 4.5 + max(0.0, float(vc.get("internalMedianSpeedPx", 0.0)) - 7.5) * 0.18
    cadence = cd["speedCV"] * 0.75 + cd["timingJitter"] * 1.2 + speed_mismatch + slideshow_pen + fragmentation_pen

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
        + 0.70 * acceleration
        + 0.70 * photometric
        + 1.05 * structural
        + 0.80 * cadence
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
        "seamRatio": round(float(vs["seamRatio"]), 4),
        "seamSsim": round(float(vs["seamSsim"]), 4),
        "internalMedianSsim": round(med_ssim, 4),
        "seamDirectionCosine": round(float(fc["seamDirectionCosine"]), 4),
        "velocityJump": round(float(vc["relativeJump"]), 4),
        "accelRatio": round(float(ac["accelRatio"]), 4),
        "ghostingScore": round(float(gh["score"]), 4),
        "flowConfidence": round(float(fc.get("meanFlowConfidence", 0.5)), 4),
        "hasHardDefect": has_hard_defect or bool(fz["detected"]),
    }


def detect_scipy_periodicity(frames: np.ndarray, step: float) -> dict:
    """Detect dominant global & regional temporal periodicities using SciPy FFT + autocorrelation (Intervention 13)."""
    from scipy import fft as sp_fft
    from scipy import signal as sp_signal

    n = int(frames.shape[0])
    if n < 8:
        return {"detected": False, "periodsSec": [], "peakLags": [], "strength": 0.0, "regionalPeriods": []}
    # Compact luminance + spatial-quadrant + texture-energy temporal signals
    small = frames[:, ::4, ::4].mean(axis=3).astype(np.float64)
    h, w = small.shape[1], small.shape[2]
    # Also compute per-quadrant local gradient (texture periodicity) and temporal delta (flow periodicity)
    gx = np.abs(np.diff(small, axis=2, prepend=small[:, :, :1]))
    gy = np.abs(np.diff(small, axis=1, prepend=small[:, :1, :]))
    tex = gx + gy
    regions_2d = [
        ("global_lum", small.mean(axis=(1, 2))),
        ("top_left", small[:, : max(1, h // 2), : max(1, w // 2)].mean(axis=(1, 2))),
        ("top_right", small[:, : max(1, h // 2), max(0, w // 2) :].mean(axis=(1, 2))),
        ("bot_left", small[:, max(0, h // 2) :, : max(1, w // 2)].mean(axis=(1, 2))),
        ("bot_right", small[:, max(0, h // 2) :, max(0, w // 2) :].mean(axis=(1, 2))),
        ("global_tex", tex.mean(axis=(1, 2))),
        ("flow_mag", np.r_[0.0, np.abs(np.diff(small, axis=0)).mean(axis=(1, 2))]),
    ]
    min_lag = max(3, int(round(0.45 / max(step, 1e-3))))
    max_lag = min(n - 2, int(n * 0.85))
    if max_lag <= min_lag + 1:
        return {"detected": False, "periodsSec": [], "peakLags": [], "strength": 0.0, "regionalPeriods": []}

    ac_sum = np.zeros(n, dtype=np.float64)
    valid = 0
    regional_periods: list[dict] = []
    fft_lags: list[int] = []
    hann = np.hanning(n)
    freqs = sp_fft.rfftfreq(n, d=max(step, 1e-4))

    for rname, sig in regions_2d:
        s = sig - np.mean(sig)
        energy = float(np.dot(s, s))
        if energy < 1e-5:
            continue
        corr = sp_signal.correlate(s, s, mode="full")[n - 1 :] / energy
        ac_sum += corr
        valid += 1
        # Regional FFT peak period
        spec = np.abs(sp_fft.rfft(s * hann))
        valid_f = (freqs >= 1.0 / max(max_lag * step, 0.5)) & (freqs <= 1.0 / max(min_lag * step, 0.25))
        if valid_f.any():
            sub_idx = np.where(valid_f)[0]
            best_f = float(freqs[sub_idx[int(np.argmax(spec[sub_idx]))]])
            if best_f > 1e-4:
                p_sec = 1.0 / best_f
                p_lag = int(round(p_sec / max(step, 1e-4)))
                if min_lag <= p_lag <= max_lag and corr[p_lag] > 0.22:
                    fft_lags.append(p_lag)
                    regional_periods.append({"region": rname, "periodSec": round(p_sec, 4), "lag": p_lag, "ac": round(float(corr[p_lag]), 3)})

    if valid == 0:
        return {"detected": False, "periodsSec": [], "peakLags": [], "strength": 0.0, "regionalPeriods": []}
    ac = ac_sum / float(valid)
    peaks, props = sp_signal.find_peaks(ac[min_lag:max_lag], height=0.28, prominence=0.10, distance=max(2, min_lag // 2))
    lags: list[int] = []
    strength = 0.0
    if len(peaks) > 0:
        order = np.argsort(props["peak_heights"])[::-1][:5]
        lags = [int(peaks[i] + min_lag) for i in order]
        strength = float(props["peak_heights"][order[0]])
    # Harmonize with regional FFT lags that aren't already represented
    for fl in fft_lags:
        if all(abs(fl - l) >= max(2, int(round(0.25 / max(step, 1e-3)))) for l in lags):
            lags.append(fl)
            strength = max(strength, float(ac[fl]))
    lags = lags[:6]
    if not lags:
        return {"detected": False, "periodsSec": [], "peakLags": [], "strength": 0.0, "regionalPeriods": regional_periods}
    return {
        "detected": True,
        "peakLags": lags,
        "periodsSec": [round(l * step, 4) for l in lags],
        "strength": round(strength, 4),
        "regionalPeriods": regional_periods,
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
    with signed temporal-derivative (motion-phase) matching and SciPy periodicity peaks so
    opposite-phase seams (e.g. swaying foliage moving left at start and right at end) are rejected.
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

    # Inject SciPy-detected periodicity peaks directly into the lag bank
    sci_per = detect_scipy_periodicity(frames, step)
    for p_lag in sci_per["peakLags"]:
        if 3 <= p_lag <= n_desc - 1:
            p_dur = p_lag * step
            best_cnt = max(4, min(cap, int(round(p_dur * min(source_fps, 15.0)))))
            best_w = min(CANONICAL_WAITS, key=lambda w: abs(best_cnt * w - p_dur))
            lags_to_check.setdefault(p_lag, []).append((best_cnt, best_w))

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


def _slice_from_buffer(
    buf: np.ndarray,
    buf_dur: float,
    start: float,
    end: float,
    count: int,
) -> np.ndarray | None:
    """Slice `count` evenly-spaced frames from a single dense global 160x90 buffer (Intervention 10)."""
    nb = int(buf.shape[0])
    if nb < 4 or count < 4 or end <= start + 0.05 or buf_dur <= 0.05:
        return None
    step_t = (end - start) / float(count)
    # Use open-ended phase convention t_i = start + i * step_t so F_{N-1} advances into F_0 without duplicating F_0!
    times = start + np.arange(count, dtype=np.float64) * step_t
    idx = np.clip(np.rint(times / buf_dur * nb).astype(np.int64), 0, nb - 1)
    # Require distinct buffer samples so coarse evaluation does not hallucinate freezes on dense short windows
    if len(np.unique(idx)) < max(3, int(count * 0.72)):
        return None
    return np.asarray(buf[idx], dtype=np.uint8)


def _pareto_rank_candidates(candidates: list[dict]) -> list[dict]:
    """Compute Pareto frontier rank across multi-objective loop criteria with hard-defect veto (Intervention 11)."""
    n = len(candidates)
    if n == 0:
        return candidates
    # Lower is better for every objective vector entry
    objs = []
    for c in candidates:
        co = c["costs"]
        objs.append(
            (
                float(co.get("seamRatio", 2.0)),
                1.0 - float(co.get("seamSsim", 0.5)),
                1.0 - max(-1.0, min(1.0, float(co.get("seamDirectionCosine", 0.5)))),
                float(co.get("velocityJump", 0.5)),
                float(co.get("accelRatio", 1.5)),
                float(co.get("cadenceError", 1.0)),
                float(co.get("durationPenalty", 0.5)),
                float(co.get("artifactPenalty", 0.0)),
            )
        )
    dominated_by = [0] * n
    for i in range(n):
        oi = objs[i]
        i_defect = bool(candidates[i]["costs"].get("hasHardDefect", False))
        for j in range(n):
            if i == j:
                continue
            # Hard-defect candidates (ping-pong, cut, freeze, static) must NEVER dominate clean loops!
            if (not i_defect) and bool(candidates[j]["costs"].get("hasHardDefect", False)):
                continue
            oj = objs[j]
            if all(oj[k] <= oi[k] + 1e-6 for k in range(len(oi))) and any(oj[k] < oi[k] - 1e-6 for k in range(len(oi))):
                dominated_by[i] += 1
    for i, c in enumerate(candidates):
        c["paretoDominatedCount"] = dominated_by[i]
        c["paretoFrontier"] = dominated_by[i] == 0 and not c["costs"].get("hasHardDefect", False)
    return candidates


def _refine_phase_locked_endpoint(
    path: Path,
    source_dur: float,
    folder: Path,
    start: float,
    end: float,
    cnt: int,
    source_fps: float,
    check: Callable[[], None],
) -> tuple[float, float, np.ndarray | None]:
    """Phase-Locked Loop Endpoint refinement: sweep ±3 native frames around `end` in a single compact decode (Intervention 02)."""
    native_dt = 1.0 / max(min(source_fps, 60.0), 12.0)
    span = end - start
    if span <= 0.25 or cnt < 4:
        return end, float(f"{span / max(cnt, 1):.6g}"), None
    max_shift = min(3 * native_dt, span * 0.12)
    win_end = min(source_dur, end + max_shift + native_dt)
    win_span = win_end - start
    if win_span <= 0.2:
        return end, float(f"{span / cnt:.6g}"), None
    # Single compact decode at <=30fps in SURVEY_SIZE so ±3 shifts cost only 1 FFmpeg call!
    probe_fps = min(max(source_fps, 15.0), 30.0)
    probe_n = max(cnt + 6, min(140, int(round(win_span * probe_fps))))
    try:
        dense = np.asarray(io_media.extract(path, start, win_end, probe_n, SURVEY_SIZE, folder, check), dtype=np.uint8)
    except io_media.MotionError:
        return end, float(f"{span / cnt:.6g}"), None
    if len(dense) < cnt:
        return end, float(f"{span / cnt:.6g}"), None

    best_end = end
    best_sampled = None
    best_loss = float("inf")
    f32 = dense.astype(np.float32)

    for shift_k in range(-3, 4):
        cand_end = min(win_end, max(start + 0.20, end + shift_k * native_dt))
        cand_span = cand_end - start
        cand_wait = cand_span / float(cnt)
        if not (0.02 <= cand_wait <= 0.5):
            continue
        times = np.arange(cnt, dtype=np.float64) * cand_wait
        idx = np.clip(np.rint(times / win_span * len(dense)).astype(np.int64), 0, len(dense) - 1)
        if len(np.unique(idx)) < max(3, int(cnt * 0.8)):
            continue
        seq = f32[idx]
        int_mads = np.abs(seq[:-1] - seq[1:]).mean(axis=(1, 2, 3))
        med_mad = float(np.median(int_mads))
        seam_mad = float(np.abs(seq[-1] - seq[0]).mean())
        # Penalize both jump (seam_mad >> med_mad) AND artificial duplicate freeze (seam_mad << 0.25*med_mad)
        ratio_err = abs(seam_mad - med_mad) / max(med_mad, 0.5)
        dup_pen = 1.5 if (med_mad > 0.6 and seam_mad < 0.22 * med_mad) else 0.0
        # Directional phase check: velocity before end vs velocity after start
        v_end = seq[-1] - seq[-2]
        v_start = seq[1] - seq[0]
        v_seam = seq[0] - seq[-1]
        n_end = float(np.linalg.norm(v_end)) + 1e-5
        n_st = float(np.linalg.norm(v_start)) + 1e-5
        n_sm = float(np.linalg.norm(v_seam)) + 1e-5
        cos_phase = 0.5 * (
            float(np.sum(v_seam * v_end) / (n_sm * n_end))
            + float(np.sum(v_seam * v_start) / (n_sm * n_st))
        )
        loss = ratio_err + dup_pen + 0.55 * (1.0 - max(-1.0, min(1.0, cos_phase)))
        if loss < best_loss:
            best_loss = loss
            best_end = round(cand_end, 6)
            best_sampled = dense[idx]

    best_wait = float(f"{(best_end - start) / cnt:.6g}")
    return best_end, best_wait, best_sampled


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
    """Full-duration multicriteria Círculo Supreme search across all 3 canonical PS4 resolutions.

    Uses a single dense global 160x90 survey buffer (Intervention 10) to avoid ~30+ redundant
    FFmpeg decodes on 4K/1080p media, applies SciPy regional FFT/autocorrelation periodicity
    (Intervention 13), Phase-Locked Loop endpoint alignment (Intervention 02), and Pareto
    multi-objective ranking with hard-defect veto (Intervention 11).
    """
    t0 = time.perf_counter()
    folder.mkdir(parents=True, exist_ok=True)
    sentinela_report, overview = survey_file(path, duration, folder, progress, check)
    info = sentinela_report["source"]
    dur = info["duration"]
    fps = info["sampleFps"] or info["fps"] or 10.0
    step = sentinela_report["levelA"]["stepSec"]

    # Build a single in-memory dense global 160x90 buffer (<=360 frames, ~15 MB) to replace dozens of FFmpeg seeks!
    dense_buf = overview
    dense_dur = dur
    if dur > 3.0 and len(overview) < 320 and fps >= 14.0:
        dense_n = min(360, max(len(overview), int(round(dur * min(fps, 16.0)))))
        if dense_n > len(overview) + 24:
            try:
                dense_buf = np.asarray(io_media.extract(path, 0.0, dur, dense_n, SURVEY_SIZE, folder, check), dtype=np.uint8)
            except io_media.MotionError:
                dense_buf = overview
    dense_step = dense_dur / float(max(len(dense_buf), 1))

    # Build inspection windows directly in memory from dense_buf (zero extra FFmpeg subprocesses!)
    windows = [(dense_buf, 0.0, dense_step)]
    if dur > 6.0 and len(dense_buf) >= 48:
        span_idx = max(24, int(round(min(6.5, dur * 0.5) / max(dense_step, 1e-4))))
        stride_idx = max(12, (len(dense_buf) - span_idx) // 4)
        for s_i in range(0, max(1, len(dense_buf) - span_idx + 1), stride_idx):
            e_i = min(len(dense_buf), s_i + span_idx)
            if e_i - s_i >= 16:
                windows.append((dense_buf[s_i:e_i], s_i * dense_step, dense_step))

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
                sampled = _slice_from_buffer(dense_buf, dense_dur, a, b, cnt)
                if sampled is None:
                    try:
                        sampled = io_media.extract(path, a, b, cnt, SURVEY_SIZE, folder, check)
                    except io_media.MotionError:
                        eval_cache[recipe_key] = None
                        sampled = None
                if sampled is not None:
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

        _pareto_rank_candidates(group_candidates)
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
                0 if c.get("paretoFrontier") else 1,
                c["costs"]["totalCost"],
                -c["duration"],
            )
        )

        # Phase-Locked Loop Endpoint refinement (Intervention 02) on top 2 candidates of each resolution
        refined_top = []
        for cand in group_candidates[:5]:
            if len(refined_top) < 2 and dur >= 1.0 and not cand["costs"].get("isStatic"):
                check()
                new_b, new_w, ref_sampled = _refine_phase_locked_endpoint(
                    path, dur, folder, cand["start"], cand["end"], cand["frames"], fps, check
                )
                if ref_sampled is not None and abs(new_b - cand["end"]) > 1e-4:
                    r_risks = []
                    if any(cand["start"] < t <= new_b - new_w for t in turn_secs):
                        r_risks.append("Retorno estrutural observado na fonte.")
                    if any(cand["start"] < t <= new_b - new_w for t in cut_secs):
                        r_risks.append("Corte abrupto observado na fonte.")
                    r_ev = evaluate_loop(ref_sampled, new_w, flow_max_width=160, evaluate_dds=False)
                    r_costs = compute_multicriteria_cost(
                        r_ev,
                        duration_sec=cand["frames"] * new_w,
                        source_duration=dur,
                        source_fps=fps,
                        wait=new_w,
                        source_risks=r_risks,
                    )
                    # Accept phase-locked endpoint ONLY if it strictly improves or matches total cost without adding defects!
                    if r_costs["totalCost"] <= cand["costs"]["totalCost"] + 0.05 and (
                        not r_costs.get("hasHardDefect") or cand["costs"].get("hasHardDefect")
                    ):
                        cand = dict(cand)
                        cand["end"] = new_b
                        cand["wait"] = new_w
                        cand["fps"] = round(1.0 / new_w, 4)
                        cand["duration"] = round(cand["frames"] * new_w, 4)
                        cand["costs"] = r_costs
                        cand["evaluation"] = r_ev
                        cand["sourceRisks"] = r_risks
                        cand["phaseLocked"] = True
                        cand["recipe"] = dict(cand["recipe"], end=new_b, wait=new_w)
            refined_top.append(cand)

        _pareto_rank_candidates(refined_top)
        refined_top.sort(
            key=lambda c: (
                order_map.get(c["costs"]["classification"], 3),
                0 if c.get("paretoFrontier") else 1,
                c["costs"]["totalCost"],
                -c["duration"],
            )
        )
        all_candidates.extend(refined_top)
        groups.append(
            {
                "size": list(size),
                "maxFrames": max_frames_for(size),
                "frameBytes": dds_bytes_for(*size),
                "evaluated": len(group_candidates),
                "unreadable": len(shortlist) - len(group_candidates),
                "classification": refined_top[0]["costs"]["classification"] if refined_top else CLASS_UNSUITABLE,
                "candidates": refined_top,
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
