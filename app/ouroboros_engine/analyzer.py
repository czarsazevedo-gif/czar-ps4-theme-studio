"""Sentinela Supreme — 5-level progressive motion intelligence (Levels A–E)."""

from __future__ import annotations

import json
import math
import time
from fractions import Fraction
from pathlib import Path
from typing import Callable, Sequence

import cv2
import numpy as np

from .. import media
from . import media_io as io_media
from ..config import ANIMATED_SIZES, dds_bytes_for, max_frames_for
from . import OUROBOROS_VERSION as DELUXE_VERSION
from .flow import (
    compute_bidirectional_flow,
    estimate_camera_motion,
    estimate_subpixel_shift,
    flow_kinematics,
    to_gray_u8,
)

SURVEY_SIZE = (160, 90)
REGION_GRID = (9, 16)  # 10x10 px blocks on 160x90

# Level E canonical classifications
CLASS_NATURAL = "NATURALMENTE CÍCLICO"
CLASS_CORRECTABLE = "CORRIGÍVEL"
CLASS_SYNTHESIS = "ADEQUADO À SÍNTESE"
CLASS_REGIONAL = "ADEQUADO À RECONSTRUÇÃO REGIONAL"
CLASS_UNSUITABLE = "INADEQUADO"


def _probe_pts_window(source: Path, interval: str, check: Callable[[], None]) -> tuple[dict, list[float]]:
    raw = io_media.command(
        [
            media.FFPROBE,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-select_streams",
            "v:0",
            "-read_intervals",
            interval,
            "-show_frames",
            "-show_entries",
            "stream=width,height,duration,avg_frame_rate,r_frame_rate,codec_name:frame=best_effort_timestamp_time,pkt_duration_time",
            "-of",
            "json",
            str(source),
        ],
        check,
    )
    data = json.loads(raw)
    stream = data["streams"][0]
    pts = [
        float(f["best_effort_timestamp_time"])
        for f in data.get("frames", [])
        if "best_effort_timestamp_time" in f
    ]
    return stream, pts


def probe_source_extended(source: Path, duration: float, check: Callable[[], None] = lambda: None) -> dict:
    """Read real timestamps distributed across the full video duration for reliable early & late VFR detection."""
    stream, pts0 = _probe_pts_window(source, "%+#240", check)
    stream_dur = (
        min(duration, float(stream["duration"]))
        if stream.get("duration") not in {None, "N/A", "0"}
        else duration
    )

    windows: list[list[float]] = [pts0] if pts0 else []
    first_end = pts0[-1] if pts0 else 0.0

    # If the initial 240-frame prefix does not cover the video (~92%), sample distributed windows across the full span!
    if stream_dur > 0.5 and first_end < stream_dur * 0.92:
        # 4 additional windows across remaining span including near-end so late VFR transitions are never missed.
        fracs = (0.35, 0.60, 0.80, 0.92)
        for frac in fracs:
            start_t = max(first_end + 0.05, stream_dur * frac)
            if start_t >= stream_dur - 0.05:
                continue
            try:
                _st, w_pts = _probe_pts_window(source, f"{start_t:.3f}%+#120", check)
            except Exception:
                continue
            if len(w_pts) >= 2:
                windows.append(w_pts)

    # Compute deltas strictly WITHIN each contiguous window (never across seek gaps!)
    intra_dts_list: list[np.ndarray] = []
    fps_by_window: list[float] = []
    all_pts: list[float] = []
    covered_span = 0.0
    for w_pts in windows:
        all_pts.extend(w_pts)
        if len(w_pts) >= 2:
            w_arr = np.diff(np.asarray(w_pts, dtype=np.float64))
            w_arr = w_arr[w_arr > 1e-6]
            if len(w_arr):
                intra_dts_list.append(w_arr)
                fps_by_window.append(round(float(1.0 / np.median(w_arr)), 4))
                covered_span += float(w_pts[-1] - w_pts[0])

    dts = np.concatenate(intra_dts_list) if intra_dts_list else np.array([], dtype=np.float64)

    try:
        declared = float(Fraction(stream.get("avg_frame_rate", "0")))
    except (ZeroDivisionError, ValueError):
        declared = 0.0
    try:
        r_fps = float(Fraction(stream.get("r_frame_rate", "0")))
    except (ZeroDivisionError, ValueError):
        r_fps = 0.0

    med_dt = float(np.median(dts)) if len(dts) else 0.0
    measured = float(1.0 / med_dt) if med_dt > 0 else None
    vfr_cv = float(np.std(dts) / med_dt) if len(dts) >= 3 and med_dt > 0 else 0.0
    vfr_max_dev = float(np.max(np.abs(dts / med_dt - 1.0))) if len(dts) >= 2 and med_dt > 0 else 0.0
    dup_ts = int(np.sum(dts < med_dt * 0.45)) if len(dts) >= 2 and med_dt > 0 else 0
    drop_ts = int(np.sum(dts > med_dt * 1.75)) if len(dts) >= 2 and med_dt > 0 else 0
    window_fps_shift = (
        (max(fps_by_window) / max(min(fps_by_window), 1e-6) - 1.0)
        if len(fps_by_window) >= 2
        else 0.0
    )
    is_vfr = bool(
        (len(dts) >= 3 and (vfr_max_dev > 0.12 or vfr_cv > 0.05))
        or window_fps_shift > 0.12
    )
    coverage_ratio = (
        round(min(1.0, (max(all_pts) - min(all_pts)) / max(stream_dur, 1e-6)), 3)
        if len(all_pts) >= 2
        else 0.0
    )
    return {
        "duration": stream_dur,
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "codec": str(stream.get("codec_name", "?")),
        "fps": declared or measured or r_fps or 10.0,
        "rFrameRate": r_fps,
        "sampleFps": measured,
        "timestamps": all_pts[:240],
        "timestampCount": len(all_pts),
        "windowsSampled": len(windows),
        "fpsByWindow": fps_by_window,
        "coverageRatio": coverage_ratio,
        "isVFR": is_vfr,
        "vfrMaxDeviation": round(max(vfr_max_dev, window_fps_shift), 4),
        "vfrCV": round(vfr_cv, 4),
        "duplicateTimestamps": dup_ts,
        "droppedTimestamps": drop_ts,
    }


def _normalize_frames(frames: Sequence[np.ndarray] | np.ndarray) -> np.ndarray:
    arr = np.asarray(frames)
    if arr.ndim != 4 or arr.shape[0] < 4:
        raise ValueError("Sentinela Supreme requer ao menos 4 quadros RGB.")
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    h, w = arr.shape[1:3]
    if (w, h) != SURVEY_SIZE:
        arr = np.stack([cv2.resize(f, SURVEY_SIZE, interpolation=cv2.INTER_AREA) for f in arr])
    return arr.astype(np.uint8, copy=False)


def _detect_spatial_repetition(rgb: np.ndarray, flows_fwd: np.ndarray) -> dict:
    """Detect horizontally or vertically repeating translating bands (Gênesis-ready evidence)."""
    n, h, w, _ = rgb.shape
    diff_rows = np.abs(np.diff(rgb.astype(np.float32), axis=0)).mean(axis=(0, 2, 3))
    diff_cols = np.abs(np.diff(rgb.astype(np.float32), axis=0)).mean(axis=(0, 1, 3))

    best = {"detected": False, "axis": None, "periodPx": None, "bands": 0, "error": 999.0}
    for axis, active_profile, oriented in [
        ("horizontal", diff_rows > 1.2, rgb),
        ("vertical", diff_cols > 1.2, rgb.transpose(0, 2, 1, 3)),
    ]:
        edges = np.flatnonzero(np.diff(np.r_[False, active_profile, False]))
        valid_bands = 0
        periods = []
        errs = []
        for y0, y1 in edges.reshape(-1, 2):
            if y1 - y0 < 4:
                continue
            first = oriented[0, y0:y1].astype(np.float32)
            max_p = first.shape[1] // 2
            best_p, best_e = None, 999.0
            for p in range(8, max_p + 1):
                e = float(np.abs(first[:, p:] - first[:, :-p]).mean())
                if e < best_e:
                    best_e, best_p = e, p
                if e < 2.2:
                    break
            if best_p is not None and best_e < 4.5:
                valid_bands += 1
                periods.append(best_p)
                errs.append(best_e)
        if valid_bands > 0 and float(np.mean(errs)) < best["error"]:
            best = {
                "detected": True,
                "axis": axis,
                "periodPx": int(round(float(np.median(periods)))),
                "bands": valid_bands,
                "error": round(float(np.mean(errs)), 3),
            }
    return best


def analyze_levels(
    frames: Sequence[np.ndarray] | np.ndarray,
    *,
    dt: float = 0.1,
    source_info: dict | None = None,
    check: Callable[[], None] = lambda: None,
) -> dict:
    """Run Levels A, B, C, D, and E on a chronological frame stack covering the source."""
    t0 = time.perf_counter()
    rgb = _normalize_frames(frames)
    n, h, w, _ = rgb.shape
    grays = np.stack([to_gray_u8(f) for f in rgb])
    labs = np.stack([cv2.cvtColor(f, cv2.COLOR_RGB2LAB).astype(np.float32) for f in rgb])
    L_chan = labs[..., 0] * (100.0 / 255.0)
    ab_chan = labs[..., 1:3] - 128.0

    # --- Compute dense bidirectional flow & camera motion across all steps ---
    flows_fwd = np.empty((n - 1, h, w, 2), dtype=np.float32)
    confs = np.empty((n - 1, h, w), dtype=np.float32)
    occs = np.empty((n - 1, h, w), dtype=bool)
    cam_steps = []
    subpixel_shifts = []

    for i in range(n - 1):
        check()
        fr = compute_bidirectional_flow(rgb[i], rgb[i + 1])
        flows_fwd[i] = fr.fwd
        confs[i] = fr.confidence
        occs[i] = fr.occluded
        if i % max(1, (n - 1) // 24) == 0 or n <= 40:
            cam_steps.append(estimate_camera_motion(rgb[i], rgb[i + 1], fr.fwd, fr.confidence))
        dx_p, dy_p, resp = estimate_subpixel_shift(grays[i], grays[i + 1])
        subpixel_shifts.append((dx_p, dy_p, resp))

    # ==================== LEVEL A — GLOBAL EXPLORATION ====================
    diff_gray = np.abs(np.diff(grays.astype(np.float32), axis=0))
    diff_rgb = np.abs(np.diff(rgb.astype(np.float32), axis=0))
    frame_mae = diff_rgb.mean(axis=(1, 2, 3))
    med_mae = float(np.median(frame_mae))
    mad_mae = float(np.median(np.abs(frame_mae - med_mae)))

    # Color histogram distance & normalized correlation drop for robust hard-cut detection
    hist_dists = np.empty(n - 1, dtype=np.float32)
    corr_scores = np.empty(n - 1, dtype=np.float32)
    hists = [cv2.calcHist([f], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256]).ravel() for f in rgb]
    hists = [hh / max(float(hh.sum()), 1.0) for hh in hists]
    for i in range(n - 1):
        hist_dists[i] = float(0.5 * np.abs(hists[i] - hists[i + 1]).sum())
        ga_f = grays[i].astype(np.float32)
        gb_f = grays[i + 1].astype(np.float32)
        ga_c = ga_f - ga_f.mean()
        gb_c = gb_f - gb_f.mean()
        denom = float(np.linalg.norm(ga_c) * np.linalg.norm(gb_c))
        corr_scores[i] = float((ga_c * gb_c).sum() / denom) if denom > 1e-5 else 1.0
    med_hist = float(np.median(hist_dists))
    med_corr = float(np.median(corr_scores))

    cuts = [
        int(i)
        for i in range(n - 1)
        if (
            float(frame_mae[i]) > max(16.0, med_mae + max(10.0, 4.5 * 1.4826 * mad_mae))
            and (
                float(hist_dists[i]) > max(0.16, med_hist * 2.2 + 0.06)
                or float(corr_scores[i]) < min(0.55, med_corr - 0.25)
            )
        )
        or (float(frame_mae[i]) > max(24.0, 3.0 * max(med_mae, 1.0) + 8.0) and float(corr_scores[i]) < 0.35)
    ]
    continuous_mask = np.ones(n - 1, dtype=bool)
    for c in cuts:
        continuous_mask[c] = False

    freezes = [
        int(i)
        for i in range(n - 1)
        if float(frame_mae.max()) > 1.2 and float(frame_mae[i]) < 0.22
    ]

    lum_curve = L_chan.mean(axis=(1, 2))
    lum_diff = np.diff(lum_curve)
    chroma_mean = ab_chan.mean(axis=(1, 2))
    chroma_step = np.hypot(np.diff(chroma_mean[:, 0]), np.diff(chroma_mean[:, 1]))

    flow_mag = np.hypot(flows_fwd[..., 0], flows_fwd[..., 1])
    step_speeds = np.array(
        [
            float(flow_mag[i][confs[i] > 0.25].mean())
            if (confs[i] > 0.25).any()
            else float(flow_mag[i].mean())
            for i in range(n - 1)
        ],
        dtype=np.float32,
    )
    cont_speeds = step_speeds[continuous_mask] if continuous_mask.any() else step_speeds
    step_accels = np.abs(np.diff(cont_speeds)) if len(cont_speeds) >= 2 else np.array([0.0], dtype=np.float32)

    # Subpixel global shift evidence (handles Scenario O where flow might be tiny 0.2-0.5 px)
    sp_arr = np.asarray(subpixel_shifts, dtype=np.float32)
    valid_sp = sp_arr[continuous_mask] if continuous_mask.any() else sp_arr
    sp_mags = np.hypot(valid_sp[:, 0], valid_sp[:, 1])
    sp_med = float(np.median(sp_mags)) if len(sp_mags) else 0.0
    sp_resp = float(np.median(valid_sp[:, 2])) if len(valid_sp) else 0.0
    sp_vec = valid_sp[:, :2].sum(axis=0)
    sp_coh = float(np.hypot(sp_vec[0], sp_vec[1]) / max(float(sp_mags.sum()), 1e-6))
    subpixel_motion = bool(0.08 <= sp_med <= 1.2 and sp_resp > 0.30 and sp_coh > 0.65)

    # ==================== LEVEL B — REGION ANALYSIS ====================
    gh, gw = REGION_GRID
    bh, bw = h // gh, w // gw
    # Reshape flow, diff, lum into (T, gh, gw, ...)
    valid_t = np.flatnonzero(continuous_mask)
    if len(valid_t) == 0:
        valid_t = np.arange(n - 1)
    f_blk = flows_fwd[valid_t, : gh * bh, : gw * bw].reshape(len(valid_t), gh, bh, gw, bw, 2).mean(axis=(2, 4))
    c_blk = confs[valid_t, : gh * bh, : gw * bw].reshape(len(valid_t), gh, bh, gw, bw).mean(axis=(2, 4))
    d_blk = diff_gray[valid_t, : gh * bh, : gw * bw].reshape(len(valid_t), gh, bh, gw, bw).mean(axis=(2, 4))
    L_blk = np.abs(np.diff(L_chan[:, : gh * bh, : gw * bw].reshape(n, gh, bh, gw, bw).mean(axis=(2, 4)), axis=0))[valid_t]

    mag_blk = np.hypot(f_blk[..., 0], f_blk[..., 1])
    mean_mag_blk = mag_blk.mean(axis=0)
    mean_vec_blk = f_blk.mean(axis=0)
    mean_diff_blk = d_blk.mean(axis=0)
    max_diff_blk = d_blk.max(axis=0)
    active_frac_blk = (d_blk > 1.8).mean(axis=0)
    mean_lum_blk = L_blk.mean(axis=0)

    sum_u_blk = f_blk[..., 0].sum(axis=0)
    sum_v_blk = f_blk[..., 1].sum(axis=0)
    coh_blk = np.hypot(sum_u_blk, sum_v_blk) / np.maximum(mag_blk.sum(axis=0), 1e-5)

    # Classify each cell
    cell_class = np.full((gh, gw), "static", dtype=object)
    for r in range(gh):
        for c in range(gw):
            mm = float(mean_mag_blk[r, c])
            md = float(mean_diff_blk[r, c])
            mx = float(max_diff_blk[r, c])
            af = float(active_frac_blk[r, c])
            ch = float(coh_blk[r, c])
            ml = float(mean_lum_blk[r, c])
            if md < 0.75 and mm < 0.22 and ml < 0.4:
                cell_class[r, c] = "static"
            elif af < 0.38 and mx > max(5.5, md * 3.2):
                cell_class[r, c] = "transient"
            elif mm >= 0.22 and ch >= 0.55:
                cell_class[r, c] = "progressive_flow"
            elif mm >= 0.22 and ch < 0.55:
                cell_class[r, c] = "oscillatory"
            elif md >= 0.75 or ml >= 0.4:
                cell_class[r, c] = "photometric"
            else:
                cell_class[r, c] = "static"

    static_frac = float((cell_class == "static").mean())
    cell_std = grays[0, : gh * bh, : gw * bw].reshape(gh, bh, gw, bw).std(axis=(1, 3))
    textured_static_frac = float(((cell_class == "static") & (cell_std >= 5.0)).mean())
    transient_frac = float((cell_class == "transient").mean())
    progressive_frac = float((cell_class == "progressive_flow").mean())
    oscillatory_frac = float((cell_class == "oscillatory").mean())
    photometric_frac = float((cell_class == "photometric").mean())

    # Camera motion aggregation: distinguish fixed camera vs jitter vs smooth pan/travelling vs zoom/rotation
    cam_txs = [c["tx"] for c in cam_steps]
    cam_tys = [c["ty"] for c in cam_steps]
    cam_rots = [c["rotationDeg"] for c in cam_steps]
    cam_scales = [c["scale"] for c in cam_steps]
    cam_disps = [math.hypot(tx, ty) for tx, ty in zip(cam_txs, cam_tys)]
    cam_moving_frac = float(np.mean([c["cameraMoving"] for c in cam_steps])) if cam_steps else 0.0
    cam_inliers = float(np.mean([c["inlierRatio"] for c in cam_steps])) if cam_steps else 0.0
    cam_quads = float(np.mean([c.get("inlierQuadrants", 0) for c in cam_steps])) if cam_steps else 0.0
    cam_med_disp = float(np.median(cam_disps)) if cam_disps else 0.0
    cam_jitter = float(np.std(cam_disps)) if len(cam_disps) >= 2 else 0.0
    cam_zoom_dev = float(max((abs(s - 1.0) for s in cam_scales), default=0.0))
    cam_max_rot = float(max((abs(r) for r in cam_rots), default=0.0))

    camera_moving = bool(
        static_frac < 0.18
        and textured_static_frac < 0.08
        and cam_moving_frac >= 0.50
        and (
            (cam_inliers >= 0.65 and cam_med_disp > 0.45)
            or (cam_inliers >= 0.42 and cam_quads >= 2.8 and cam_med_disp > 0.70)
        )
    )
    if not camera_moving:
        camera_class = "tremor_leve" if (cam_jitter > 0.45 and cam_med_disp > 0.20) else "estavel"
    elif cam_zoom_dev > 0.012 or cam_max_rot > 0.6:
        camera_class = "zoom_ou_rotacao"
    elif cam_jitter > max(0.85, cam_med_disp * 0.75):
        camera_class = "instavel_tremor"
    else:
        camera_class = "panoramica_ou_travelling"

    max_lum_step = float(np.max(np.abs(lum_diff))) if len(lum_diff) else 0.0
    lum_range = float(np.ptp(lum_curve))

    level_a = {
        "samples": n,
        "stepSec": round(dt, 6),
        "durationSec": round(n * dt, 4),
        "cuts": cuts,
        "cutSeconds": [round((c + 1) * dt, 4) for c in cuts],
        "freezes": freezes,
        "medianMae": round(med_mae, 4),
        "meanSpeedPx": round(float(np.mean(cont_speeds)), 4),
        "maxAccelPx": round(float(np.max(step_accels)), 4),
        "luminanceRangeL": round(lum_range, 4),
        "maxLumStep": round(max_lum_step, 4),
        "maxChromaStep": round(float(np.max(chroma_step)) if len(chroma_step) else 0.0, 4),
        "cameraMoving": camera_moving,
        "cameraClass": camera_class,
        "cameraMedianDispPx": round(cam_med_disp, 4),
        "cameraJitterPx": round(cam_jitter, 4),
        "cameraZoomDev": round(cam_zoom_dev, 5),
        "cameraInlierRatio": round(cam_inliers, 3),
        "subpixelMotionDetected": subpixel_motion,
        "subpixelMedianShiftPx": round(sp_med, 4),
    }

    # Segment semantic regions: velocity-aware BFS for progressive_flow so adjacent
    # layers with distinct directions or speeds (e.g., slow clouds + fast river) stay separate!
    regions = []
    region_id = 0

    def _emit_region(comp: np.ndarray, beh: str) -> None:
        nonlocal region_id
        area_cells = int(comp.sum())
        if area_cells < 2 and beh != "transient":
            return
        ys, xs = np.where(comp)
        u_m = float(f_blk[:, comp, 0].mean())
        v_m = float(f_blk[:, comp, 1].mean())
        sp_m = float(mag_blk[:, comp].mean())
        coh_m = float(
            np.hypot(f_blk[:, comp, 0].sum(), f_blk[:, comp, 1].sum())
            / max(float(mag_blk[:, comp].sum()), 1e-5)
        )
        regions.append(
            {
                "id": f"r{region_id}",
                "behavior": beh,
                "bboxNorm": [
                    round(float(xs.min()) / gw, 4),
                    round(float(ys.min()) / gh, 4),
                    round(float(xs.max() + 1) / gw, 4),
                    round(float(ys.max() + 1) / gh, 4),
                ],
                "areaFraction": round(area_cells / float(gh * gw), 4),
                "meanSpeedPx": round(sp_m, 4),
                "meanVec": [round(u_m, 4), round(v_m, 4)],
                "coherence": round(coh_m, 4),
                "activityMean": round(float(mean_diff_blk[comp].mean()), 3),
            }
        )
        region_id += 1

    prog_mask = cell_class == "progressive_flow"
    visited = np.zeros((gh, gw), dtype=bool)
    for r0 in range(gh):
        for c0 in range(gw):
            if not prog_mask[r0, c0] or visited[r0, c0]:
                continue
            comp = np.zeros((gh, gw), dtype=bool)
            queue = [(r0, c0)]
            visited[r0, c0] = True
            comp[r0, c0] = True
            seed_vec = mean_vec_blk[r0, c0].copy()
            seed_mag = float(np.hypot(seed_vec[0], seed_vec[1]))
            while queue:
                cr, cc = queue.pop(0)
                for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nr, nc = cr + dr, cc + dc
                    if 0 <= nr < gh and 0 <= nc < gw and prog_mask[nr, nc] and not visited[nr, nc]:
                        nv = mean_vec_blk[nr, nc]
                        nmag = float(np.hypot(nv[0], nv[1]))
                        cos = float((seed_vec[0] * nv[0] + seed_vec[1] * nv[1]) / max(seed_mag * nmag, 1e-5))
                        ratio = max(seed_mag, nmag) / max(min(seed_mag, nmag), 0.15)
                        if cos >= 0.72 and ratio <= 1.65:
                            visited[nr, nc] = True
                            comp[nr, nc] = True
                            queue.append((nr, nc))
            _emit_region(comp, "progressive_flow")

    for beh in ("static", "oscillatory", "photometric", "transient"):
        mask_b = (cell_class == beh).astype(np.uint8)
        if not mask_b.any():
            continue
        num_labels, labels_img = cv2.connectedComponents(mask_b, connectivity=4)
        for lbl in range(1, num_labels):
            _emit_region(labels_img == lbl, beh)

    # Detect multi-speed or multi-direction progressive regions (e.g. slow clouds + fast river)
    prog_regions = [r for r in regions if r["behavior"] == "progressive_flow"]
    distinct_speeds = False
    if len(prog_regions) >= 2:
        sp_vals = sorted(r["meanSpeedPx"] for r in prog_regions)
        has_speed_diff = (sp_vals[-1] / max(sp_vals[0], 0.1)) >= 1.45
        vecs = [r["meanVec"] for r in prog_regions]
        has_dir_diff = any(
            (v1[0] * v2[0] + v1[1] * v2[1]) / max(math.hypot(*v1) * math.hypot(*v2), 1e-5) < 0.5
            for idx_a, v1 in enumerate(vecs)
            for v2 in vecs[idx_a + 1 :]
        )
        distinct_speeds = bool(has_speed_diff or has_dir_diff)

    level_b = {
        "gridSize": [gw, gh],
        "staticFraction": round(static_frac, 4),
        "progressiveFraction": round(progressive_frac, 4),
        "oscillatoryFraction": round(oscillatory_frac, 4),
        "photometricFraction": round(photometric_frac, 4),
        "transientFraction": round(transient_frac, 4),
        "hasMixedStaticDynamic": bool(static_frac >= 0.12 and (progressive_frac + oscillatory_frac + photometric_frac) >= 0.10),
        "hasIndependentSpeeds": distinct_speeds,
        "regions": regions,
        "regionGrid": [[str(cell_class[r, c])[0] for c in range(gw)] for r in range(gh)],
    }

    # ==================== LEVEL C — DIRECTIONAL & KINEMATIC ====================
    valid_flows = flows_fwd[valid_t]
    valid_confs = confs[valid_t]
    valid_occs = occs[valid_t]
    mean_flow = valid_flows.mean(axis=0)
    kin_global = flow_kinematics(mean_flow, valid_confs.mean(axis=0) > 0.25)

    # Temporal flow consistency: cosine similarity of consecutive flow fields
    temp_cos = []
    for k in range(len(valid_flows) - 1):
        fa, fb = valid_flows[k], valid_flows[k + 1]
        ma = np.hypot(fa[..., 0], fa[..., 1])
        mb = np.hypot(fb[..., 0], fb[..., 1])
        m = (valid_confs[k] > 0.25) & (valid_confs[k + 1] > 0.25) & (ma > 0.25) & (mb > 0.25)
        if int(m.sum()) >= 16:
            c_val = (fa[..., 0][m] * fb[..., 0][m] + fa[..., 1][m] * fb[..., 1][m]) / (ma[m] * mb[m] + 1e-6)
            temp_cos.append(float(np.clip(c_val, -1.0, 1.0).mean()))
    temporal_consistency = float(np.mean(temp_cos)) if temp_cos else (1.0 if subpixel_motion else 0.0)
    mean_occ_frac = float(valid_occs.mean())
    max_occ_frac = float(valid_occs.mean(axis=(1, 2)).max())
    mean_flow_conf = float(valid_confs.mean())

    level_c = {
        "dominantVec": kin_global["meanVec"],
        "dominantAngleDeg": kin_global["angleDeg"],
        "meanSpeedPx": round(float(cont_speeds.mean()), 4),
        "p90SpeedPx": round(float(np.percentile(flow_mag[valid_t], 90)), 4),
        "meanAccelPx": round(float(step_accels.mean()), 4),
        "spatialCoherence": kin_global["coherence"],
        "temporalConsistency": round(temporal_consistency, 4),
        "meanDivergence": kin_global["meanDivergence"],
        "meanCurl": kin_global["meanCurl"],
        "occlusionMeanFraction": round(mean_occ_frac, 4),
        "occlusionMaxFraction": round(max_occ_frac, 4),
        "meanFlowConfidence": round(mean_flow_conf, 4),
        "cameraMotion": {
            "moving": camera_moving,
            "medianTx": round(float(np.median(cam_txs)) if cam_txs else 0.0, 4),
            "medianTy": round(float(np.median(cam_tys)) if cam_tys else 0.0, 4),
            "medianRotationDeg": round(float(np.median([c["rotationDeg"] for c in cam_steps])) if cam_steps else 0.0, 4),
            "inlierRatio": round(cam_inliers, 3),
        },
        "subpixel": {
            "detected": subpixel_motion,
            "medianShiftPx": round(sp_med, 4),
            "coherence": round(sp_coh, 4),
            "response": round(sp_resp, 4),
        },
    }

    # ==================== LEVEL D — PERIODICITY ANALYSIS ====================
    # Check internal turnaround / ping-pong symmetry
    turn_events = []
    for pivot in range(2, n - 2):
        for hold in (0, 1):
            span = min(5, pivot, n - 1 - pivot - hold)
            if span < 2:
                continue
            opp_scores = []
            mir_ratios = []
            for k in range(span):
                fp = flows_fwd[pivot - 1 - k]
                ff = flows_fwd[pivot + hold + k]
                mp = np.hypot(fp[..., 0], fp[..., 1])
                mf = np.hypot(ff[..., 0], ff[..., 1])
                mv = (confs[pivot - 1 - k] > 0.25) & (confs[pivot + hold + k] > 0.25) & (mp > 0.3) & (mf > 0.3)
                if int(mv.sum()) < 24:
                    continue
                cos = (fp[..., 0][mv] * ff[..., 0][mv] + fp[..., 1][mv] * ff[..., 1][mv]) / (mp[mv] * mf[mv] + 1e-6)
                opp_scores.append(float((cos < -0.35).mean()))
                diff = grays[pivot - 1 - k].astype(np.float32) - grays[pivot + hold + 1 + k].astype(np.float32)
                mir_err = float(np.abs(diff - diff.mean()).mean())
                mir_ratios.append(mir_err / max(1.0, float(frame_mae[pivot - 1 - k])))
            if len(opp_scores) >= 2 and float(np.mean(opp_scores)) >= 0.60:
                m_ratio = float(np.mean(mir_ratios))
                turn_events.append(
                    {
                        "index": pivot,
                        "second": round(pivot * dt, 4),
                        "opposition": round(float(np.mean(opp_scores)), 3),
                        "mirrorRatio": round(m_ratio, 3),
                        "mirrored": bool(m_ratio < 0.72),
                    }
                )
                break

    ping_pong = any(e["mirrored"] for e in turn_events)

    # Multi-lag appearance + flow recurrence
    g_small = grays[:, ::2, ::2].astype(np.float32)
    f_small = flows_fwd[:, ::4, ::4]
    typical_step = max(0.5, med_mae)
    best_lag = None
    best_lag_ratio = 99.0
    best_lag_flow_cos = 0.0
    has_motion = (med_mae > 0.35 or float(cont_speeds.mean()) > 0.18 or float(np.ptp(lum_curve)) > 1.8 or subpixel_motion)

    if has_motion and n >= 8:
        for k in range(4, n // 2 + 1):
            app_err = float(np.abs(g_small[k:] - g_small[:-k]).mean())
            ratio = app_err / typical_step
            if ratio < best_lag_ratio:
                # Verify flow direction agrees at lag k (not reversed!)
                f_cos = 1.0
                if len(f_small) > k:
                    fa, fb = f_small[:-k], f_small[k:]
                    ma = np.hypot(fa[..., 0], fa[..., 1])
                    mb = np.hypot(fb[..., 0], fb[..., 1])
                    mv = (ma > 0.25) & (mb > 0.25)
                    if int(mv.sum()) >= 20:
                        f_cos = float(((fa[..., 0][mv] * fb[..., 0][mv] + fa[..., 1][mv] * fb[..., 1][mv]) / (ma[mv] * mb[mv] + 1e-6)).mean())
                if ratio < 0.72 and f_cos > 0.35:
                    best_lag = k
                    best_lag_ratio = ratio
                    best_lag_flow_cos = f_cos
                    break
                if ratio < best_lag_ratio:
                    best_lag_ratio = ratio
                    best_lag_flow_cos = f_cos

    # Luminous pulsation: periodic brightness wave without structural trajectory reversal
    lum_pulsation = bool(
        not cuts
        and not ping_pong
        and float(np.ptp(lum_curve)) > 2.2
        and float(cont_speeds.mean()) < 0.28
        and (best_lag is not None or (float(np.min(lum_diff)) < -0.15 and float(np.max(lum_diff)) > 0.15))
    )

    # Artificial flicker vs natural illumination
    dL_int = np.diff(lum_curve)
    alt_rate = float(((dL_int[1:] * dL_int[:-1]) < -0.04).mean()) if len(dL_int) >= 3 else 0.0
    d2L = np.abs(np.diff(dL_int)) if len(dL_int) >= 2 else np.array([0.0])
    artificial_flicker = bool(alt_rate > 0.55 and float(np.median(d2L)) > 1.8)

    if artificial_flicker:
        lighting_stability = "flicker_artificial"
    elif max_lum_step > 6.5:
        lighting_stability = "mudanca_brusca"
    elif lum_range > 8.0:
        lighting_stability = "variacao_suave"
    else:
        lighting_stability = "consistente"
    level_a["lightingStability"] = lighting_stability
    level_c["cameraMotion"]["cameraClass"] = camera_class
    level_c["cameraMotion"]["jitterPx"] = round(cam_jitter, 4)
    level_c["cameraMotion"]["zoomDeviation"] = round(cam_zoom_dev, 5)

    # ==================== VISUAL & TECHNICAL QUALITY AUDIT ====================
    sample_idx = np.linspace(0, n - 1, min(n, 8), dtype=int)
    sharp_vals = [float(np.var(cv2.Laplacian(grays[i], cv2.CV_32F, ksize=3))) for i in sample_idx]
    med_sharp = float(np.median(sharp_vals)) if sharp_vals else 0.0
    # High-frequency noise estimate (median absolute difference against 3x3 median filter)
    noise_vals = [
        float(np.median(np.abs(grays[i].astype(np.float32) - cv2.medianBlur(grays[i], 3).astype(np.float32))))
        for i in sample_idx
    ]
    med_noise = float(np.median(noise_vals)) if noise_vals else 0.0
    # 8x8 macroblock boundary ratio on luminance (compression blocking cue)
    block_ratios = []
    for i in sample_idx:
        gf = grays[i].astype(np.float32)
        dx_g = np.abs(np.diff(gf, axis=1))
        dy_g = np.abs(np.diff(gf, axis=0))
        if dx_g.shape[1] >= 16 and dy_g.shape[0] >= 16:
            bx = float(dx_g[:, 7::8].mean())
            ix = float(dx_g[:, 3::8].mean())
            by = float(dy_g[7::8, :].mean())
            iy = float(dy_g[3::8, :].mean())
            block_ratios.append(0.5 * ((bx / max(ix, 0.5)) + (by / max(iy, 0.5))))
    blocking_ratio = float(np.median(block_ratios)) if block_ratios else 1.0

    src_w = int((source_info or {}).get("width") or w)
    src_h = int((source_info or {}).get("height") or h)
    aspect_ratio = src_w / max(src_h, 1)
    aspect_diff = abs(aspect_ratio - (16.0 / 9.0))
    crop_needed = bool(aspect_diff > 0.08)
    portrait_source = bool(src_h > src_w)
    sharp_label = "alta" if med_sharp >= 260.0 else ("adequada" if med_sharp >= 75.0 else ("suave" if med_sharp >= 25.0 else "borrada"))
    compression_label = "alta_compressao" if blocking_ratio > 1.38 else ("moderada" if blocking_ratio > 1.18 else "limpa")
    noise_label = "alto" if med_noise > 3.5 else ("moderado" if med_noise > 1.6 else "baixo")
    motion_blur_risk = bool(med_sharp < 45.0 and float(cont_speeds.mean()) > 1.2)

    level_quality = {
        "sharpnessVar": round(med_sharp, 2),
        "sharpnessLabel": sharp_label,
        "noiseMad": round(med_noise, 3),
        "noiseLabel": noise_label,
        "blockingRatio": round(blocking_ratio, 3),
        "compressionLabel": compression_label,
        "motionBlurRisk": motion_blur_risk,
        "sourceWidth": src_w,
        "sourceHeight": src_h,
        "aspectRatio": round(aspect_ratio, 4),
        "matchesPs416x9": not crop_needed,
        "cropNeeded": crop_needed,
        "portraitSource": portrait_source,
        "resolutionTier": (
            "1080p_ou_superior" if src_w >= 1900 and src_h >= 1060
            else ("720p_hd" if src_w >= 1260 and src_h >= 700 else "inferior_a_720p")
        ),
    }

    spatial_rep = _detect_spatial_repetition(rgb, flows_fwd)

    true_recurrence = bool(best_lag is not None and not ping_pong and not cuts and best_lag_flow_cos > 0.35)
    artificial_return = bool(ping_pong or (len(freezes) >= max(2, n // 6) and has_motion))

    level_d = {
        "trueRecurrence": true_recurrence,
        "periodSamples": best_lag,
        "periodSeconds": round(best_lag * dt, 4) if best_lag else None,
        "recurrenceRatio": round(best_lag_ratio, 4),
        "recurrenceFlowCosine": round(best_lag_flow_cos, 4),
        "trajectorySymmetry": ping_pong,
        "reverseMotion": ping_pong,
        "turnEvents": turn_events,
        "luminousPulsation": lum_pulsation,
        "artificialFlicker": artificial_flicker,
        "spatialRepetition": spatial_rep,
        "temporalRepetition": bool(best_lag is not None and best_lag_ratio < 0.45),
        "artificialReturn": artificial_return,
    }

    # ==================== LEVEL E — RESOLUTION-AWARE DIAGNOSIS ====================
    is_static = bool(
        not has_motion
        or (static_frac > 0.94 and not subpixel_motion and float(np.ptp(lum_curve)) < 1.5 and med_mae < 0.35)
    )

    per_resolution = []
    for sz in ANIMATED_SIZES:
        cap = max_frames_for(sz)
        fb = dds_bytes_for(*sz)
        reasons = []
        if is_static:
            cls = CLASS_UNSUITABLE
            conf = 0.92
            reasons.append("Cena estática ou com movimento desprezível; ausência de movimento não constitui loop.")
        elif true_recurrence and best_lag is not None and best_lag <= cap and not artificial_flicker:
            cls = CLASS_NATURAL
            conf = round(float(np.clip(0.65 + 0.35 * (1.0 - min(best_lag_ratio, 1.0)), 0.0, 0.98)), 3)
            reasons.append(
                f"Recorrência verdadeira detectada ({best_lag} amostras ≈ {best_lag * dt:.2f} s), compatível com o teto de {cap} quadros em {sz[0]}×{sz[1]}."
            )
        elif spatial_rep["detected"] and not ping_pong and not cuts:
            cls = CLASS_SYNTHESIS
            conf = round(float(np.clip(0.85 - 0.08 * spatial_rep["error"], 0.45, 0.95)), 3)
            reasons.append(
                f"Repetição espacial coerente no eixo {spatial_rep['axis']} ({spatial_rep['bands']} faixa(s)); adequado à síntese periódica."
            )
        elif level_b["hasMixedStaticDynamic"] and (progressive_frac >= 0.08 or transient_frac >= 0.03 or level_b["hasIndependentSpeeds"]):
            cls = CLASS_REGIONAL
            conf = round(float(np.clip(0.55 + 0.3 * mean_flow_conf, 0.35, 0.9)), 3)
            reasons.append("Regiões estáticas e dinâmicas distintas coexistem; reconstrução regional recomendada.")
            if level_b["hasIndependentSpeeds"]:
                reasons.append("Múltiplas velocidades independentes identificadas.")
            if transient_frac >= 0.03:
                reasons.append("Objeto transitório localizado exige proteção ou remoção espaço-temporal.")
        elif ping_pong:
            cls = CLASS_CORRECTABLE if (progressive_frac > 0.15 and len(turn_events) == 1 and turn_events[0]["index"] >= 6) else CLASS_UNSUITABLE
            conf = 0.85
            reasons.append("Reversão / ping-pong detectada; trecho não pode ser aprovado como loop progressivo natural.")
        elif cuts and len(cuts) >= max(2, n // 12):
            cls = CLASS_UNSUITABLE
            conf = 0.82
            reasons.append("Múltiplos cortes abruptos comprometem continuidade temporal.")
        elif camera_moving and not true_recurrence:
            cls = CLASS_CORRECTABLE if cam_med_disp < 4.0 else CLASS_UNSUITABLE
            conf = 0.72
            reasons.append("Deslocamento global de câmera detectado; exige estabilização e reconstrução.")
        elif true_recurrence and best_lag is not None and best_lag > cap:
            cls = CLASS_CORRECTABLE
            conf = 0.74
            reasons.append(
                f"Período natural ({best_lag} amostras) excede o teto de {cap} quadros em {sz[0]}×{sz[1]}; requer reamostragem temporal ou síntese."
            )
        elif progressive_frac >= 0.12 or subpixel_motion or lum_pulsation:
            cls = CLASS_SYNTHESIS if (progressive_frac >= 0.25 and kin_global["coherence"] >= 0.6) else CLASS_CORRECTABLE
            conf = round(float(np.clip(0.45 + 0.35 * mean_flow_conf, 0.35, 0.85)), 3)
            reasons.append("Movimento contínuo sem fechamento natural exato; candidato à reconstrução ou síntese direcional.")
        elif (
            oscillatory_frac >= 0.22
            and transient_frac < 0.08
            and not cuts
            and not ping_pong
            and not artificial_flicker
            and float(cont_speeds.mean()) >= 0.30
        ):
            # Continuous stochastic / turbulent dynamic textures (e.g., campfire flames, rain, turbulent ripples)
            cls = CLASS_CORRECTABLE
            conf = round(float(np.clip(0.42 + 0.25 * mean_flow_conf, 0.35, 0.72)), 3)
            reasons.append(
                "Textura dinâmica turbulenta/oscilatória contínua (ex.: fogo, chuva ou espuma); requer ponte de transição ou seleção cuidadosa de costura."
            )
        else:
            cls = CLASS_UNSUITABLE
            conf = 0.65
            reasons.append("Trajetória não periódica ou com evidência insuficiente para fechamento convincente.")

        per_resolution.append(
            {
                "size": list(sz),
                "maxFrames": cap,
                "frameBytes": fb,
                "classification": cls,
                "confidence": conf,
                "reasons": reasons,
            }
        )

    # ==================== SOURCE MOTION PROFILE (TEMPORAL FIDELITY BASELINE) ====================
    rgb_f32 = rgb.astype(np.float32)
    t_std_map = rgb_f32.std(axis=0).mean(axis=-1)  # (h, w)
    cy0, cy1 = int(h * 0.15), max(int(h * 0.15) + 1, int(h * 0.80))
    cx0, cx1 = int(w * 0.20), max(int(w * 0.20) + 1, int(w * 0.80))
    focal_std_map = t_std_map[cy0:cy1, cx0:cx1]
    p75_std = float(np.percentile(t_std_map, 75))
    subj_mask = t_std_map >= max(2.5, p75_std)
    active_subj_std = float(t_std_map[subj_mask].mean()) if subj_mask.any() else float(t_std_map.mean())

    # Sliding ~2.0s local window temporal std so sub-clips are fairly benchmarked against typical local activity
    win_n = min(n, max(4, int(round(2.0 / max(dt, 1e-4)))))
    win_stride = max(1, win_n // 3)
    win_stds: list[float] = []
    win_focal_stds: list[float] = []
    win_active_fracs: list[float] = []
    if n >= win_n:
        for i0 in range(0, n - win_n + 1, win_stride):
            seg_f = rgb_f32[i0 : i0 + win_n]
            wmap = seg_f.std(axis=0).mean(axis=-1)
            win_stds.append(float(wmap.mean()))
            win_focal_stds.append(float(wmap[cy0:cy1, cx0:cx1].mean()))
            win_active_fracs.append(float((wmap > 2.5).mean()))
    if not win_stds:
        win_stds = [float(t_std_map.mean())]
        win_focal_stds = [float(focal_std_map.mean())]
        win_active_fracs = [float((t_std_map > 2.5).mean())]

    scale_to_180 = 180.0 / float(max(h, 1))
    valid_fm = flow_mag[valid_t]
    flow_p95_180 = float(np.percentile(valid_fm, 95) * scale_to_180) if len(valid_fm) else 0.0
    focal_fm = valid_fm[:, cy0:cy1, cx0:cx1] if len(valid_fm) else valid_fm
    focal_flow_p95_180 = float(np.percentile(focal_fm, 95) * scale_to_180) if len(focal_fm) else 0.0
    flow_active_frac = float(((valid_fm * scale_to_180) > 0.30).mean()) if len(valid_fm) else 0.0

    wrap_mae_full = float(np.abs(rgb_f32[-1] - rgb_f32[0]).mean())
    full_wrap_ratio = float(wrap_mae_full / max(med_mae, 0.5))
    source_motion_profile = {
        "temporalStdMean": round(float(t_std_map.mean()), 4),
        "temporalStdP90": round(float(np.percentile(t_std_map, 90)), 4),
        "focalTemporalStdMean": round(float(focal_std_map.mean()), 4),
        "activeSubjectStdMean": round(active_subj_std, 4),
        "localWindowSec": round(win_n * dt, 3),
        "localWindowStdMedian": round(float(np.median(win_stds)), 4),
        "localWindowStdP85": round(float(np.percentile(win_stds, 85)), 4),
        "localWindowStdMin": round(float(np.min(win_stds)), 4),
        "localFocalStdMedian": round(float(np.median(win_focal_stds)), 4),
        "localFocalStdP85": round(float(np.percentile(win_focal_stds, 85)), 4),
        "localActiveAreaMedian": round(float(np.median(win_active_fracs)), 4),
        "medianStepMae": round(med_mae, 4),
        "p90StepMae": round(float(np.percentile(frame_mae, 90)), 4),
        "flowP95Norm180": round(flow_p95_180, 4),
        "focalFlowP95Norm180": round(focal_flow_p95_180, 4),
        "activeAreaFraction": round(float((t_std_map > 2.5).mean()), 4),
        "focalActiveAreaFraction": round(float((focal_std_map > 2.5).mean()), 4),
        "flowActiveAreaFraction": round(flow_active_frac, 4),
        "fullVideoWrapMae": round(wrap_mae_full, 4),
        "fullVideoWrapRatio": round(full_wrap_ratio, 4),
        "isSeamlessFullSource": bool(full_wrap_ratio <= 1.35 and not cuts and not ping_pong and not is_static),
        "transientPlusPhotometricFraction": round(float(transient_frac + photometric_frac), 4),
    }

    # Overall classification prefers highest-capability resolution verdict (640x360 has largest budget)
    priority = [CLASS_NATURAL, CLASS_SYNTHESIS, CLASS_REGIONAL, CLASS_CORRECTABLE, CLASS_UNSUITABLE]
    best_res = min(per_resolution, key=lambda r: priority.index(r["classification"]))

    return {
        "version": DELUXE_VERSION,
        "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
        "classification": best_res["classification"],
        "confidence": best_res["confidence"],
        "confidenceLabel": "forte" if best_res["confidence"] >= 0.72 else ("moderada" if best_res["confidence"] >= 0.45 else "limitada"),
        "reasons": best_res["reasons"],
        "source": source_info,
        "sourceMotionProfile": source_motion_profile,
        "levelA": level_a,
        "levelB": level_b,
        "levelC": level_c,
        "levelD": level_d,
        "levelE": per_resolution,
        "levelQuality": level_quality,
    }


def survey_file(
    source: Path,
    duration: float,
    folder: Path,
    progress: Callable[[str, int], None] = lambda *_: None,
    check: Callable[[], None] = lambda: None,
) -> tuple[dict, np.ndarray]:
    """Full-duration progressive Sentinela Supreme survey directly from a video file."""
    info = probe_source_extended(source, duration, check)
    dur = info["duration"]
    fps = info["sampleFps"] or info["fps"] or 10.0
    n = min(240, max(4, int(round(dur * min(12.0, fps)))))
    progress("Sentinela Supreme · Nível A (exploração global)", 6)
    frames = io_media.extract(source, 0.0, dur, n, SURVEY_SIZE, folder, check)
    progress("Sentinela Supreme · Níveis B–E (regiões, fluxo, periodicidade e orçamento)", 18)
    dt = dur / float(n)
    report = analyze_levels(frames, dt=dt, source_info=info, check=check)
    report["coverage"] = [0.0, dur]
    return report, frames
