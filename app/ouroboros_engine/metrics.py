"""16-metric scientific circular loop evaluation suite, including native-resolution post-DDS validation."""

from __future__ import annotations

import math
import time
from typing import Sequence

import cv2
import numpy as np
from skimage.metrics import structural_similarity as skimage_ssim

from .. import dds, dxt1
from ..config import ANIMATED_SIZES, MAX_FRAME_COUNT, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from . import OUROBOROS_VERSION as DELUXE_VERSION
from .flow import compute_bidirectional_flow, flow_kinematics, to_gray_u8


def _as_rgb_stack(frames: Sequence[np.ndarray] | np.ndarray) -> np.ndarray:
    arr = np.asarray(frames)
    if arr.ndim != 4 or arr.shape[0] < 4:
        raise ValueError("A avaliação circular exige ao menos 4 quadros RGB/RGBA.")
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    if arr.shape[-1] != 3:
        raise ValueError("Quadros devem ter 3 ou 4 canais.")
    return arr.astype(np.uint8, copy=False)


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    diff = a.astype(np.float32) - b.astype(np.float32)
    mse = float(np.mean(diff * diff))
    if mse <= 1e-9:
        return 99.0
    return float(10.0 * math.log10((255.0 * 255.0) / mse))


def _ssim_gray(a: np.ndarray, b: np.ndarray) -> float:
    """Exact uniform-window SSIM matching skimage (ddof=1) using OpenCV float32 boxFilter (~3.5x faster)."""
    h, w = a.shape[:2]
    win = min(7, h if h % 2 == 1 else h - 1, w if w % 2 == 1 else w - 1)
    if win < 3:
        return 1.0 - float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean()) / 255.0
    af = a.astype(np.float32, copy=False)
    bf = b.astype(np.float32, copy=False)
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    k = (win, win)
    n_el = float(win * win)
    cov_norm = n_el / (n_el - 1.0)
    ux = cv2.boxFilter(af, -1, k, borderType=cv2.BORDER_REFLECT)
    uy = cv2.boxFilter(bf, -1, k, borderType=cv2.BORDER_REFLECT)
    uxx = cv2.boxFilter(af * af, -1, k, borderType=cv2.BORDER_REFLECT)
    uyy = cv2.boxFilter(bf * bf, -1, k, borderType=cv2.BORDER_REFLECT)
    uxy = cv2.boxFilter(af * bf, -1, k, borderType=cv2.BORDER_REFLECT)
    vx = np.maximum(0.0, (uxx - ux * ux) * cov_norm)
    vy = np.maximum(0.0, (uyy - uy * uy) * cov_norm)
    vxy = (uxy - ux * uy) * cov_norm
    smap = ((2.0 * ux * uy + c1) * (2.0 * vxy + c2)) / ((ux * ux + uy * uy + c1) * (vx + vy + c2))
    pad = win // 2
    if pad > 0 and smap.shape[0] > 2 * pad and smap.shape[1] > 2 * pad:
        return float(smap[pad:-pad, pad:-pad].mean())
    return float(smap.mean())


def _ssim_and_ms_ssim_gray(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Compute full-res SSIM and 3-scale MS-SSIM in a single pass (eliminates duplicate scale-0 work)."""
    weights = (0.4, 0.35, 0.25)
    scores = []
    ca, cb = a, b
    s0 = 1.0
    for idx, w_scale in enumerate(weights):
        s = _ssim_gray(ca, cb)
        if idx == 0:
            s0 = s
        scores.append((w_scale, s))
        if min(ca.shape[:2]) < 24:
            break
        ca = cv2.pyrDown(ca)
        cb = cv2.pyrDown(cb)
    total_w = sum(w_s for w_s, _ in scores)
    return s0, float(sum(w_s * s for w_s, s in scores) / max(total_w, 1e-6))


def _ms_ssim_gray(a: np.ndarray, b: np.ndarray) -> float:
    """Fast 3-scale multi-scale SSIM approximation on grayscale frames."""
    return _ssim_and_ms_ssim_gray(a, b)[1]


def _robust_z(value: float, samples: np.ndarray, floor: float = 0.25) -> float:
    if len(samples) == 0:
        return 0.0
    med = float(np.median(samples))
    mad = float(np.median(np.abs(samples - med)))
    scale = max(floor, 1.4826 * mad)
    return float((value - med) / scale)


def _edge_map(gray: np.ndarray) -> np.ndarray:
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return np.hypot(gx, gy)


def _laplacian_sharpness(gray: np.ndarray) -> float:
    lap = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
    return float(np.var(lap))


def _banding_index(rgb: np.ndarray) -> float:
    """Estimate false-contour banding in smooth gradient areas.

    In smooth regions (low coarse gradient), true smooth gradients have tiny uniform
    pixel steps while quantized/DXT1a-banded regions exhibit flat 4x4 plateaus broken
    by sudden micro-steps (2–14 gray levels) along block or contour edges.
    """
    gray = to_gray_u8(rgb).astype(np.float32)
    smooth = cv2.GaussianBlur(gray, (9, 9), 2.0)
    gx_s = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3) / 8.0
    gy_s = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3) / 8.0
    coarse_grad = np.hypot(gx_s, gy_s)

    # Smooth-gradient mask: gentle tone ramp (not flat solid color, not busy texture)
    gradient_zone = (coarse_grad >= 0.18) & (coarse_grad <= 4.5)
    if int(gradient_zone.sum()) < 64:
        return 0.0

    # Local pixel differences inside smooth gradient zones
    dx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    dy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
    step = np.maximum(dx, dy)

    z_steps = step[gradient_zone]
    z_coarse = coarse_grad[gradient_zone]
    # Plateau fraction (zero local step despite non-zero coarse gradient)
    plateau_frac = float((z_steps < 0.5).mean())
    # Excess jump ratio: where step jumps > 2.2x the underlying smooth slope (false contour edge)
    false_contour_frac = float((z_steps > np.maximum(2.0, 2.2 * z_coarse)).mean())
    # Block-boundary energy ratio (4x4 DXT1 grid emphasis)
    bx = dx[:, 4::4]
    by = dy[4::4, :]
    in_x = dx[:, 2::4]
    in_y = dy[2::4, :]
    m = min(bx.shape[1], in_x.shape[1])
    n = min(by.shape[0], in_y.shape[0])
    block_ratio = 0.0
    if m > 0 and n > 0:
        b_edge = 0.5 * (float(bx[:, :m].mean()) + float(by[:n, :].mean()))
        b_inner = 0.5 * (float(in_x[:, :m].mean()) + float(in_y[:n, :].mean()))
        block_ratio = max(0.0, (b_edge - b_inner) / max(b_inner + 0.5, 0.5))

    # Scale into an interpretable [0, 1+] banding score
    score = plateau_frac * false_contour_frac * 6.0 + 0.35 * block_ratio
    return float(max(0.0, score))


def _border_empty_fraction(rgb: np.ndarray) -> float:
    """Detect pure-black/empty border strips (e.g. from uncompensated warping)."""
    gray = to_gray_u8(rgb)
    h, w = gray.shape
    band = max(2, min(8, min(h, w) // 20))
    interior_mean = float(gray[band:-band, band:-band].mean()) if h > 2 * band and w > 2 * band else float(gray.mean())
    if interior_mean < 12.0:
        return 0.0
    borders = np.concatenate([
        gray[:band, :].ravel(),
        gray[-band:, :].ravel(),
        gray[:, :band].ravel(),
        gray[:, -band:].ravel(),
    ])
    return float((borders < 3).mean())


def _resize_for_flow(frames: np.ndarray, max_width: int) -> tuple[np.ndarray, float]:
    h, w = frames.shape[1:3]
    if w <= max_width:
        return frames, 1.0
    scale = max_width / float(w)
    nh = max(32, int(round(h * scale / 2)) * 2)
    nw = max(32, int(round(w * scale / 2)) * 2)
    resized = np.stack([cv2.resize(f, (nw, nh), interpolation=cv2.INTER_AREA) for f in frames])
    return resized, nw / float(w)


def _detect_reversals(
    grays: np.ndarray,
    flows_fwd: list[np.ndarray],
    confs: list[np.ndarray],
    internal_mae: np.ndarray,
) -> dict:
    """Detect internal turnaround reversals (ping-pong) vs legitimate luminous pulsation."""
    n = len(grays)
    events = []
    # For each candidate pivot, check if past flow opposes future reversed flow AND frames mirror
    for pivot in range(2, n - 2):
        for hold in (0, 1):
            span = min(4, pivot, n - 1 - pivot - hold)
            if span < 2:
                continue
            opp_scores = []
            mirror_ratios = []
            for k in range(span):
                f_past = flows_fwd[pivot - 1 - k]
                f_fut = flows_fwd[pivot + hold + k]
                c_valid = (confs[pivot - 1 - k] > 0.3) & (confs[pivot + hold + k] > 0.3)
                mag_p = np.hypot(f_past[..., 0], f_past[..., 1])
                mag_f = np.hypot(f_fut[..., 0], f_fut[..., 1])
                moving = c_valid & (mag_p > 0.35) & (mag_f > 0.35)
                if int(moving.sum()) < max(16, int(0.015 * grays.shape[1] * grays.shape[2])):
                    continue
                cos = (
                    f_past[..., 0][moving] * f_fut[..., 0][moving]
                    + f_past[..., 1][moving] * f_fut[..., 1][moving]
                ) / (mag_p[moving] * mag_f[moving] + 1e-6)
                opp_scores.append(float((cos < -0.35).mean()))

                # Compare mirrored frame similarity (zero-mean local contrast cancels light drift)
                a = grays[pivot - 1 - k].astype(np.float32)
                b = grays[pivot + hold + 1 + k].astype(np.float32)
                diff = a - b
                mir_err = float(np.abs(diff - diff.mean()).mean())
                step_err = max(1.0, float(internal_mae[pivot - 1 - k]))
                mirror_ratios.append(mir_err / step_err)

            if len(opp_scores) >= 2 and float(np.mean(opp_scores)) >= 0.60:
                m_ratio = float(np.mean(mirror_ratios)) if mirror_ratios else 9.9
                events.append(
                    {
                        "pivot": pivot,
                        "hold": hold,
                        "opposition": round(float(np.mean(opp_scores)), 3),
                        "mirrorRatio": round(m_ratio, 3),
                        "mirrored": bool(m_ratio < 0.72),
                    }
                )
                break
    return {
        "detected": bool(any(e["mirrored"] for e in events)),
        "turnEvents": events,
    }


def evaluate_loop(
    frames: Sequence[np.ndarray] | np.ndarray,
    wait: float,
    *,
    protected_mask: np.ndarray | None = None,
    reference_frames: Sequence[np.ndarray] | np.ndarray | None = None,
    timestamps: Sequence[float] | None = None,
    flow_max_width: int = 240,
    evaluate_dds: bool = False,
    backend: str = "native",
) -> dict:
    """Compute the full 16-metric scientific circular evaluation on a frame sequence."""
    t_start = time.perf_counter()
    rgb = _as_rgb_stack(frames)
    n, h, w, _ = rgb.shape
    grays = np.stack([to_gray_u8(f) for f in rgb])
    labs = np.stack([cv2.cvtColor(f, cv2.COLOR_RGB2LAB).astype(np.float32) for f in rgb])
    # OpenCV uint8 Lab maps L* [0..100] -> [0..255], a*/b* -> +128
    L_chan = labs[..., 0] * (100.0 / 255.0)
    ab_chan = labs[..., 1:3] - 128.0

    # --- 1. Visual similarity (internal vs circular seam) ---
    mae_all = np.empty(n, dtype=np.float32)
    psnr_all = np.empty(n, dtype=np.float32)
    ssim_all = np.empty(n, dtype=np.float32)
    ms_ssim_all = np.empty(n, dtype=np.float32)
    edge_diff_all = np.empty(n, dtype=np.float32)
    lum_step_all = np.empty(n, dtype=np.float32)
    chroma_step_all = np.empty(n, dtype=np.float32)

    edges = np.stack([_edge_map(g) for g in grays])
    sharpness_all = np.array([_laplacian_sharpness(g) for g in grays], dtype=np.float32)

    for i in range(n):
        j = (i + 1) % n
        mae_all[i] = float(np.abs(rgb[i].astype(np.float32) - rgb[j].astype(np.float32)).mean())
        psnr_all[i] = _psnr(rgb[i], rgb[j])
        ssim_all[i], ms_ssim_all[i] = _ssim_and_ms_ssim_gray(grays[i], grays[j])
        edge_diff_all[i] = float(np.abs(edges[i] - edges[j]).mean())
        lum_step_all[i] = float(L_chan[j].mean() - L_chan[i].mean())
        ab_i = ab_chan[i].mean(axis=(0, 1))
        ab_j = ab_chan[j].mean(axis=(0, 1))
        chroma_step_all[i] = float(np.hypot(ab_j[0] - ab_i[0], ab_j[1] - ab_i[1]))

    int_mae = mae_all[:-1]
    seam_mae = float(mae_all[-1])
    int_ssim = ssim_all[:-1]
    seam_ssim = float(ssim_all[-1])
    int_ms_ssim = ms_ssim_all[:-1]
    seam_ms_ssim = float(ms_ssim_all[-1])
    int_psnr = psnr_all[:-1]
    seam_psnr = float(psnr_all[-1])
    med_mae = float(np.median(int_mae))
    med_ssim = float(np.median(int_ssim))

    visual_seam_ratio = float(seam_mae / max(med_mae, 0.5))
    visual_z = _robust_z(seam_mae, int_mae, floor=0.4)
    ssim_drop = float(max(0.0, med_ssim - seam_ssim))

    # --- 2, 3, 4. Dense bidirectional optical flow, velocity, acceleration ---
    small_rgb, flow_scale = _resize_for_flow(rgb, flow_max_width)
    small_grays = np.stack([to_gray_u8(f) for f in small_rgb])
    flows_fwd: list[np.ndarray] = []
    confs: list[np.ndarray] = []
    occ_fracs: list[float] = []
    foldovers: list[float] = []
    speeds = np.empty(n, dtype=np.float32)
    mean_vecs = np.empty((n, 2), dtype=np.float32)

    for i in range(n):
        j = (i + 1) % n
        fr = compute_bidirectional_flow(small_rgb[i], small_rgb[j])
        # Rescale flow magnitudes back to native pixel units
        scaled_fwd = fr.fwd / max(flow_scale, 1e-6)
        flows_fwd.append(scaled_fwd)
        confs.append(fr.confidence)
        occ_fracs.append(float(fr.occluded.mean()))
        kin = flow_kinematics(scaled_fwd, fr.confidence > 0.25)
        foldovers.append(kin["foldoverFraction"])
        speeds[i] = kin["meanSpeed"]
        mean_vecs[i] = kin["meanVec"]

    # Flow continuity at seam: compare F[N-1]->F[0] against adjacent F[N-2]->F[N-1] and F[0]->F[1]
    f_before = flows_fwd[n - 2]
    f_seam = flows_fwd[n - 1]
    f_after = flows_fwd[0]
    c_seam = confs[n - 1]
    c_adj = np.minimum(confs[n - 2], confs[0])

    # Internal flow-to-flow acceleration norms
    int_acc = []
    for i in range(n - 2):
        w_mask = (confs[i] > 0.25) & (confs[i + 1] > 0.25)
        diff_v = flows_fwd[i + 1] - flows_fwd[i]
        dv = np.hypot(diff_v[..., 0], diff_v[..., 1])
        int_acc.append(float(dv[w_mask].mean()) if w_mask.any() else float(dv.mean()))
    int_acc_arr = np.asarray(int_acc, dtype=np.float32)

    valid_seam = (c_seam > 0.25) & (c_adj > 0.25)
    f_expected = 0.5 * (f_before + f_after)
    f_err_map = np.hypot(f_seam[..., 0] - f_expected[..., 0], f_seam[..., 1] - f_expected[..., 1])
    seam_flow_err = float(f_err_map[valid_seam].mean()) if valid_seam.any() else float(f_err_map.mean())
    med_int_acc = float(np.median(int_acc_arr)) if len(int_acc_arr) else 0.0
    flow_err_ratio = float(seam_flow_err / max(med_int_acc, 0.25))

    # Direction & reversal at seam (require genuine structural motion, not DXT1a/photometric noise)
    min_moving_px = max(36, int(0.015 * valid_seam.size))
    opp_fracs = []
    cos_means = []
    for adj in (f_before, f_after):
        ma = np.hypot(adj[..., 0], adj[..., 1])
        ms = np.hypot(f_seam[..., 0], f_seam[..., 1])
        moving = valid_seam & (ma > 0.45) & (ms > 0.45)
        if int(moving.sum()) >= min_moving_px:
            cos = (adj[..., 0][moving] * f_seam[..., 0][moving] + adj[..., 1][moving] * f_seam[..., 1][moving]) / (
                ma[moving] * ms[moving] + 1e-6
            )
            opp_fracs.append(float((cos < -0.25).mean()))
            cos_means.append(float(np.clip(cos, -1.0, 1.0).mean()))
    seam_opp_frac = max(opp_fracs, default=0.0)
    seam_dir_cosine = min(cos_means, default=1.0)
    direction_measured = bool(opp_fracs)

    # Velocity & acceleration continuity
    int_speeds = speeds[:-1]
    med_speed = float(np.median(int_speeds))
    seam_speed = float(speeds[-1])
    adj_speed = 0.5 * (float(speeds[n - 2]) + float(speeds[0]))
    vel_jump_rel = float(abs(seam_speed - adj_speed) / max(adj_speed, med_speed, 0.35))

    acc_before_seam = float(np.hypot(*(mean_vecs[n - 1] - mean_vecs[n - 2])))
    acc_seam_after = float(np.hypot(*(mean_vecs[0] - mean_vecs[n - 1])))
    seam_acc = max(acc_before_seam, acc_seam_after)
    int_vec_acc = np.hypot(
        np.diff(mean_vecs[:-1, 0]), np.diff(mean_vecs[:-1, 1])
    ) if n > 3 else np.array([0.0], dtype=np.float32)
    acc_ratio = float(seam_acc / max(float(np.median(int_vec_acc)), 0.15))

    # --- 5, 6. Luminance & Chromatic continuity ---
    int_lum_abs = np.abs(lum_step_all[:-1])
    seam_lum_abs = float(abs(lum_step_all[-1]))
    lum_ratio = float(seam_lum_abs / max(float(np.median(int_lum_abs)), 0.4))
    # Derivative continuity for smooth photometric pulsation
    lum_slope_err = float(abs(lum_step_all[-1] - 0.5 * (lum_step_all[-2] + lum_step_all[0])))

    int_chroma = chroma_step_all[:-1]
    seam_chroma = float(chroma_step_all[-1])
    chroma_ratio = float(seam_chroma / max(float(np.median(int_chroma)), 0.4))

    # --- 7. Edge consistency & localized 8x8 seam burst (catches transient objects) ---
    int_edge = edge_diff_all[:-1]
    seam_edge = float(edge_diff_all[-1])
    edge_ratio = float(seam_edge / max(float(np.median(int_edge)), 0.8))
    edge_z = _robust_z(seam_edge, int_edge, floor=0.8)

    gh_b, gw_b = min(8, max(2, h // 8)), min(8, max(2, w // 8))
    bh_b, bw_b = h // gh_b, w // gw_b
    rgb_crop = rgb[:, : gh_b * bh_b, : gw_b * bw_b].astype(np.float32)
    edge_crop = edges[:, : gh_b * bh_b, : gw_b * bw_b]
    blk_mae = np.empty((n, gh_b, gw_b), dtype=np.float32)
    blk_edg = np.empty((n, gh_b, gw_b), dtype=np.float32)
    for i in range(n):
        j = (i + 1) % n
        dm = np.abs(rgb_crop[j] - rgb_crop[i]).mean(axis=-1)
        de = np.abs(edge_crop[j] - edge_crop[i])
        blk_mae[i] = dm.reshape(gh_b, bh_b, gw_b, bw_b).mean(axis=(1, 3))
        blk_edg[i] = de.reshape(gh_b, bh_b, gw_b, bw_b).mean(axis=(1, 3))
    base_blk_mae = np.quantile(blk_mae[:-1], 0.85, axis=0)
    base_blk_edg = np.quantile(blk_edg[:-1], 0.85, axis=0)
    local_seam_excess = float(np.max((blk_mae[-1] - base_blk_mae) / (base_blk_mae + 4.5)))
    local_edge_excess = float(np.max((blk_edg[-1] - base_blk_edg) / (base_blk_edg + 8.0)))

    # --- 8. Flicker (distinguishes smooth natural lighting waves from high-freq artificial flicker) ---
    L_means = L_chan.mean(axis=(1, 2))
    dL_circ = np.array([L_means[(i + 1) % n] - L_means[i] for i in range(n)], dtype=np.float32)
    d2L_circ = np.array([dL_circ[(i + 1) % n] - dL_circ[i] for i in range(n)], dtype=np.float32)
    sign_flips = np.array(
        [(dL_circ[i] * dL_circ[(i - 1) % n] < -0.04) for i in range(n)], dtype=bool
    )
    alternation_rate = float(sign_flips.mean())
    hf_lum_energy = float(np.abs(d2L_circ[sign_flips]).mean()) if sign_flips.any() else 0.0
    flicker_score = float(alternation_rate * hf_lum_energy)
    gL = L_chan.reshape(n, 4, h // 4 if h % 4 == 0 else h // 4, -1) if (h % 4 == 0 and w % 4 == 0) else None
    if gL is not None:
        reg_L = L_chan.reshape(n, 4, h // 4, 4, w // 4).mean(axis=(2, 4))
        reg_dL = np.roll(reg_L, -1, axis=0) - reg_L
        reg_d2L = np.roll(reg_dL, -1, axis=0) - reg_dL
        reg_flip = (reg_dL * np.roll(reg_dL, 1, axis=0)) < -0.09
        reg_flicker = float(np.abs(reg_d2L[reg_flip]).mean() * reg_flip.mean()) if reg_flip.any() else 0.0
        flicker_score = max(flicker_score, reg_flicker)

    # --- 9. Ghosting detector ---
    sharp_ref = max(float(np.percentile(sharpness_all, 85)), 1.0)
    sharp_min_ratio = float(sharpness_all.min() / sharp_ref)
    seam_sharp_ratio = float(min(sharpness_all[0], sharpness_all[-1]) / sharp_ref)
    blend_scores = []
    for i in range(n):
        prev_f = grays[(i - 1) % n].astype(np.float32)
        curr_f = grays[i].astype(np.float32)
        next_f = grays[(i + 1) % n].astype(np.float32)
        span_diff = float(np.abs(next_f - prev_f).mean())
        if span_diff < 2.0:
            blend_scores.append(0.0)
            continue
        nbr_sharp = max(0.5 * (float(sharpness_all[(i - 1) % n]) + float(sharpness_all[(i + 1) % n])), 1.0)
        local_sharp_dip = max(0.0, 1.0 - float(sharpness_all[i]) / nbr_sharp)
        mid_avg = 0.5 * (prev_f + next_f)
        moving_px = np.abs(next_f - prev_f) > max(10.0, span_diff * 1.5)
        if int(moving_px.sum()) >= 32 and (local_sharp_dip > 0.06 or sharp_min_ratio < 0.88):
            rel_mid = float(
                np.abs(curr_f[moving_px] - mid_avg[moving_px]).mean()
                / (np.abs(next_f[moving_px] - prev_f[moving_px]).mean() + 1e-5)
            )
            blend_scores.append(
                max(0.0, (0.28 - rel_mid) / 0.28)
                * min(1.0, (local_sharp_dip + max(0.0, 0.90 - sharp_min_ratio)) * 5.0)
            )
        else:
            blend_scores.append(0.0)
    ghosting_blend_peak = float(max(blend_scores)) if blend_scores else 0.0
    ghosting_score = float(max(0.0, max(0.0, 0.92 - sharp_min_ratio) * 0.75 + ghosting_blend_peak * 0.85))

    # --- 10. Texture preservation / sharpness ---
    med_sharp = float(np.median(sharpness_all))
    sharp_retention = 1.0
    if reference_frames is not None:
        ref_arr = _as_rgb_stack(reference_frames)
        ref_sharp = float(np.median([_laplacian_sharpness(to_gray_u8(f)) for f in ref_arr]))
        sharp_retention = float(med_sharp / max(ref_sharp, 1.0))

    # --- 11. Static / protected region integrity ---
    static_max_diff = 0.0
    static_mean_diff = 0.0
    static_changed_frac = 0.0
    if protected_mask is not None:
        pmask = np.asarray(protected_mask, dtype=bool)
        if pmask.shape != (h, w):
            pmask = cv2.resize(pmask.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
        if pmask.any():
            base_px = rgb[0][pmask].astype(np.int16)
            diffs = np.abs(rgb[:, pmask].astype(np.int16) - base_px[None, :, :])
            static_max_diff = float(diffs.max())
            static_mean_diff = float(diffs.mean())
            static_changed_frac = float((diffs.max(axis=-1) > 0).mean())

    # --- 12. Reconstruction artifacts ---
    border_empty = float(max(_border_empty_fraction(f) for f in rgb))
    max_foldover = float(max(foldovers)) if foldovers else 0.0
    banding_pre = float(np.mean([_banding_index(rgb[i]) for i in np.linspace(0, n - 1, min(n, 6), dtype=int)]))

    # --- 13. Reversals (Ping-Pong) ---
    rev_info = _detect_reversals(small_grays, flows_fwd, confs, int_mae)
    has_reversal = bool(rev_info["detected"] or (direction_measured and seam_opp_frac > 0.45))

    # --- 14. Freezes / duplicate frames ---
    structural_motion = bool(med_speed > 0.25 and med_mae > 0.6)
    exact_dups = [i for i in range(n) if np.array_equal(rgb[i], rgb[(i + 1) % n])]
    near_stalls = [
        i for i in range(n)
        if structural_motion and float(mae_all[i]) < max(0.15, 0.14 * med_mae)
    ]
    freeze_indices = sorted(set(exact_dups + near_stalls))
    seam_frozen = (n - 1) in freeze_indices

    # --- 15. Cadence regularity ---
    speed_cv = float(np.std(speeds) / max(float(np.mean(speeds)), 0.15)) if structural_motion else 0.0
    timing_jitter = 0.0
    if timestamps is not None and len(timestamps) >= 3:
        dts = np.diff(np.asarray(timestamps, dtype=np.float64))
        dts = dts[dts > 0]
        if len(dts) >= 2:
            timing_jitter = float(np.max(np.abs(dts / np.median(dts) - 1.0)))

    # --- 16. Post-DDS evaluation (optional inline) ---
    dds_info = None
    if evaluate_dds:
        dds_info = _measure_dds_roundtrip(rgb, backend=backend)

    # --- Hard-cut detection inside sequence (including seam) ---
    cut_thresh = max(16.0, 3.0 * med_mae + 6.0)
    cut_indices = [
        i for i in range(n)
        if float(mae_all[i]) > cut_thresh and float(ssim_all[i]) < max(0.65, med_ssim - 0.18)
    ]

    # --- Aggregate diagnostic summary (never a blind single number!) ---
    issues: list[str] = []
    if med_mae < 0.25 and med_speed < 0.12 and float(np.ptp(L_means)) < 1.2:
        issues.append("Cena praticamente estática (ausência de movimento não indica bom loop).")
    if has_reversal:
        issues.append("Reversão estrutural / ping-pong detectada.")
    if cut_indices:
        issues.append(f"Corte abrupto detectado nos índices {cut_indices}.")
    if freeze_indices:
        issues.append(f"Congelamento / quadros duplicados nos índices {freeze_indices}.")
    if (visual_seam_ratio > 1.75 and visual_z > 3.0 and ssim_drop > 0.03) or local_seam_excess > 1.8:
        issues.append("Descontinuidade visual significativa na junção circular.")
    if direction_measured and (flow_err_ratio > 2.2 and seam_flow_err > 1.0 or seam_dir_cosine < 0.35):
        issues.append("Descontinuidade de fluxo óptico na junção.")
    if structural_motion and vel_jump_rel > 0.55:
        issues.append("Ruptura de velocidade na junção.")
    if structural_motion and acc_ratio > 3.0 and seam_acc > 1.2:
        issues.append("Salto brusco de aceleração na junção.")
    if lum_ratio > 2.8 and seam_lum_abs > 3.5:
        issues.append("Salto de luminância na junção.")
    if chroma_ratio > 2.8 and seam_chroma > 4.0:
        issues.append("Salto cromático na junção.")
    if (edge_ratio > 1.9 and edge_z > 3.2 and seam_edge > 6.0) or local_edge_excess > 2.2:
        issues.append("Inconsistência estrutural / de bordas na junção.")
    if flicker_score > 1.8 and alternation_rate > 0.45:
        issues.append("Flicker artificial de alta frequência detectado.")
    if ghosting_score > 0.45 and ghosting_blend_peak > 0.35:
        issues.append("Sinal de ghosting / mistura dupla detectado.")
    if border_empty > 0.08:
        issues.append("Bordas vazias/pretas introduzidas por reconstrução.")
    if static_max_diff > 0 and protected_mask is not None:
        issues.append(f"Região protegida alterada (diff máx {static_max_diff:.1f}).")

    # Confidence reflects measurable signal strength & FB flow reliability
    mean_conf = float(np.mean([c.mean() for c in confs]))
    mean_occ = float(np.mean(occ_fracs))
    conf_score = float(np.clip(mean_conf * (1.0 - 0.5 * mean_occ), 0.0, 1.0))

    return {
        "version": DELUXE_VERSION,
        "frames": n,
        "size": [w, h],
        "wait": float(wait),
        "duration": round(n * float(wait), 6),
        "elapsedMs": round((time.perf_counter() - t_start) * 1000.0, 1),
        "metrics": {
            "visualSimilarity": {
                "seamMae": round(seam_mae, 4),
                "internalMedianMae": round(med_mae, 4),
                "seamRatio": round(visual_seam_ratio, 4),
                "localSeamExcess": round(local_seam_excess, 4),
                "seamZScore": round(visual_z, 3),
                "seamPsnr": round(seam_psnr, 2),
                "internalMedianPsnr": round(float(np.median(int_psnr)), 2),
                "seamSsim": round(seam_ssim, 4),
                "internalMedianSsim": round(med_ssim, 4),
                "seamMsSsim": round(seam_ms_ssim, 4),
                "internalMedianMsSsim": round(float(np.median(int_ms_ssim)), 4),
                "ssimDrop": round(ssim_drop, 4),
            },
            "flowContinuity": {
                "seamFlowErrorPx": round(seam_flow_err, 4),
                "internalMedianAccPx": round(med_int_acc, 4),
                "flowErrorRatio": round(flow_err_ratio, 4),
                "seamDirectionCosine": round(seam_dir_cosine, 4),
                "seamOppositeFraction": round(seam_opp_frac, 4),
                "directionMeasured": direction_measured,
                "meanOcclusionFraction": round(mean_occ, 4),
                "seamOcclusionFraction": round(occ_fracs[-1], 4),
                "meanFlowConfidence": round(mean_conf, 4),
            },
            "velocityContinuity": {
                "seamSpeedPx": round(seam_speed, 4),
                "internalMedianSpeedPx": round(med_speed, 4),
                "adjacentSpeedPx": round(adj_speed, 4),
                "relativeJump": round(vel_jump_rel, 4),
            },
            "accelerationContinuity": {
                "seamAccelPx": round(seam_acc, 4),
                "internalMedianAccelPx": round(float(np.median(int_vec_acc)), 4),
                "accelRatio": round(acc_ratio, 4),
            },
            "luminanceContinuity": {
                "seamDeltaL": round(float(lum_step_all[-1]), 4),
                "internalMedianAbsDeltaL": round(float(np.median(int_lum_abs)), 4),
                "lumRatio": round(lum_ratio, 4),
                "slopeErrorL": round(lum_slope_err, 4),
                "rangeL": round(float(np.ptp(L_means)), 4),
            },
            "chromaticContinuity": {
                "seamDeltaChroma": round(seam_chroma, 4),
                "internalMedianChroma": round(float(np.median(int_chroma)), 4),
                "chromaRatio": round(chroma_ratio, 4),
            },
            "edgeConsistency": {
                "seamEdgeDiff": round(seam_edge, 4),
                "internalMedianEdgeDiff": round(float(np.median(int_edge)), 4),
                "edgeRatio": round(edge_ratio, 4),
                "localEdgeExcess": round(local_edge_excess, 4),
                "edgeZScore": round(edge_z, 3),
            },
            "flicker": {
                "score": round(flicker_score, 4),
                "alternationRate": round(alternation_rate, 4),
                "highFreqLumEnergy": round(hf_lum_energy, 4),
                "artificialFlicker": bool(flicker_score > 1.8 and alternation_rate > 0.45),
            },
            "ghosting": {
                "score": round(ghosting_score, 4),
                "blendPeak": round(ghosting_blend_peak, 4),
                "sharpnessMinRatio": round(sharp_min_ratio, 4),
                "seamSharpnessRatio": round(seam_sharp_ratio, 4),
                "detected": bool(ghosting_score > 0.45 and ghosting_blend_peak > 0.35),
            },
            "texturePreservation": {
                "medianSharpness": round(med_sharp, 2),
                "minSharpness": round(float(sharpness_all.min()), 2),
                "sharpnessRetention": round(sharp_retention, 4),
            },
            "staticIntegrity": {
                "checked": protected_mask is not None,
                "maxAbsDiff": round(static_max_diff, 3),
                "meanAbsDiff": round(static_mean_diff, 4),
                "changedPixelFraction": round(static_changed_frac, 6),
            },
            "reconstructionArtifacts": {
                "emptyBorderFraction": round(border_empty, 5),
                "maxFlowFoldover": round(max_foldover, 5),
                "bandingIndex": round(banding_pre, 4),
            },
            "reversals": {
                "detected": has_reversal,
                "internalMirrorDetected": rev_info["detected"],
                "seamOppositeFraction": round(seam_opp_frac, 4),
                "turnEvents": rev_info["turnEvents"],
            },
            "freezes": {
                "detected": bool(freeze_indices),
                "seamFrozen": seam_frozen,
                "indices": freeze_indices,
                "exactDuplicates": exact_dups,
            },
            "cadenceRegularity": {
                "speedCV": round(speed_cv, 4),
                "timingJitter": round(timing_jitter, 4),
                "fps": round(1.0 / max(float(wait), 1e-6), 4),
            },
            "cuts": {
                "detected": bool(cut_indices),
                "indices": cut_indices,
            },
            "ddsDelta": dds_info,
        },
        "issues": issues,
        "confidence": round(conf_score, 3),
        "confidenceLabel": "forte" if conf_score >= 0.55 else ("moderada" if conf_score >= 0.30 else "limitada"),
        "passed": len(issues) == 0,
    }


def _measure_dds_roundtrip(rgb: np.ndarray, *, backend: str = "native") -> dict:
    """Encode RGB sequence to canonical DDS DXT1a, decode it, and measure exact deltas & banding."""
    n, h, w, _ = rgb.shape
    rgba_iter = (
        np.concatenate([rgb[i], np.full((h, w, 1), 255, dtype=np.uint8)], axis=-1)
        for i in range(n)
    )
    t0 = time.perf_counter()
    blobs: list[bytes] = [b""] * n
    for idx, blob in dds.encode_sequence(rgba_iter, backend=backend):
        blobs[idx] = blob
    encode_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    decoded_rgb = np.stack([dxt1.decode_dds(b)[..., :3] for b in blobs])
    decode_ms = (time.perf_counter() - t1) * 1000.0

    total_bytes = sum(len(b) for b in blobs)
    expected_bytes = n * dds_bytes_for(w, h)
    valid_headers = all(b[:4] == b"DDS " and b[84:88] == b"DXT1" for b in blobs)
    within_budget = (
        total_bytes <= RAF_SIZE_LIMIT
        and n <= MAX_FRAME_COUNT
        and ((w, h) not in ANIMATED_SIZES or n <= max_frames_for((w, h)))
    )

    psnrs = [_psnr(rgb[i], decoded_rgb[i]) for i in range(n)]
    ssims = [_ssim_gray(to_gray_u8(rgb[i]), to_gray_u8(decoded_rgb[i])) for i in range(n)]
    maes = [float(np.abs(rgb[i].astype(np.float32) - decoded_rgb[i].astype(np.float32)).mean()) for i in range(n)]

    sample_idx = np.linspace(0, n - 1, min(n, 6), dtype=int)
    band_pre = float(np.mean([_banding_index(rgb[i]) for i in sample_idx]))
    band_post = float(np.mean([_banding_index(decoded_rgb[i]) for i in sample_idx]))

    return {
        "totalBytes": total_bytes,
        "expectedBytes": expected_bytes,
        "withinBudget": within_budget,
        "validHeaders": valid_headers,
        "encodeMs": round(encode_ms, 1),
        "decodeMs": round(decode_ms, 1),
        "meanPsnr": round(float(np.mean(psnrs)), 2),
        "minPsnr": round(float(np.min(psnrs)), 2),
        "meanSsim": round(float(np.mean(ssims)), 4),
        "meanMae": round(float(np.mean(maes)), 4),
        "bandingPreDds": round(band_pre, 4),
        "bandingPostDds": round(band_post, 4),
        "bandingIncrease": round(band_post - band_pre, 4),
        "bandingRisk": bool((band_post - band_pre) > 0.12 or band_post > 0.35),
        "distinctFrames": len(set(blobs)),
        "_decoded": decoded_rgb,
        "_blobs": blobs,
    }


def evaluate_post_dds(
    frames: Sequence[np.ndarray] | np.ndarray,
    wait: float,
    *,
    protected_mask: np.ndarray | None = None,
    reference_frames: Sequence[np.ndarray] | np.ndarray | None = None,
    timestamps: Sequence[float] | None = None,
    backend: str = "native",
    return_blobs: bool = False,
) -> dict:
    """Mandatory post-DDS evaluation: encode -> decode -> re-evaluate all 16 metrics on decoded DDS."""
    rgb = _as_rgb_stack(frames)
    dds_rt = _measure_dds_roundtrip(rgb, backend=backend)
    decoded_rgb = dds_rt.pop("_decoded")
    blobs = dds_rt.pop("_blobs")

    # Compare protected pixels against the DDS-encoded reference frame 0 (canonical DXT1a behavior)
    post_report = evaluate_loop(
        decoded_rgb,
        wait,
        protected_mask=protected_mask,
        reference_frames=reference_frames if reference_frames is not None else rgb,
        timestamps=timestamps,
        evaluate_dds=False,
    )
    post_report["metrics"]["ddsDelta"] = dds_rt
    if not dds_rt["withinBudget"]:
        post_report["issues"].append("Sequência DDS excede o orçamento RAF canônico.")
    if not dds_rt["validHeaders"]:
        post_report["issues"].append("Cabeçalho DDS DXT1 inválido.")
    if dds_rt["bandingRisk"]:
        post_report["issues"].append("Risco de banding/posterização após codificação DXT1a.")
    post_report["passed"] = len(post_report["issues"]) == 0
    post_report["evaluatedOn"] = "decoded_dds_native_resolution"
    if return_blobs:
        return {"report": post_report, "blobs": blobs, "decoded": decoded_rgb}
    return post_report


def evaluate_ten_cycles(eval_report: dict) -> dict:
    """Audit continuous 10-cycle playback (no artificial pauses) from a circular post-DDS evaluation."""
    n = int(eval_report.get("frames") or 0)
    wait = float(eval_report.get("wait") or 0.1)
    cycle_sec = round(n * wait, 4)
    total_sec = round(cycle_sec * 10.0, 3)
    m = eval_report.get("metrics") or {}
    vs = m.get("visualSimilarity") or {}
    fc = m.get("flowContinuity") or {}
    vc = m.get("velocityContinuity") or {}
    rv = m.get("reversals") or {}
    fz = m.get("freezes") or {}
    fl = m.get("flicker") or {}
    gh = m.get("ghosting") or {}

    dir_ok = (not fc.get("directionMeasured")) or (
        float(fc.get("seamDirectionCosine", 1.0)) >= 0.40 and float(fc.get("seamOppositeFraction", 0.0)) <= 0.28
    )
    seam_visible = bool(
        float(vs.get("seamRatio", 1.0)) > 1.75
        or float(vs.get("localSeamExcess", 0.0)) > 1.8
        or (fc.get("directionMeasured") and not dir_ok)
        or float(vc.get("relativeJump", 0.0)) > 0.55
    )
    rep_per_min = round(60.0 / max(cycle_sec, 0.05), 1)
    repetition_risk = "alta" if cycle_sec < 0.9 else ("moderada" if cycle_sec < 1.6 else "baixa")
    continuous_ok = bool(
        eval_report.get("passed")
        and not seam_visible
        and not rv.get("detected")
        and not fz.get("detected")
        and not fl.get("artificialFlicker")
        and not gh.get("detected")
    )
    return {
        "cycles": 10,
        "framesPerCycle": n,
        "totalFramesPlayed": n * 10,
        "wrapTransitions": 10,
        "cycleDurationSec": cycle_sec,
        "tenCycleDurationSec": total_sec,
        "repetitionsPerMinute": rep_per_min,
        "repetitionRisk": repetition_risk,
        "seamVisibleRisk": seam_visible,
        "directionalContinuityOk": dir_ok,
        "continuousTenCyclePass": continuous_ok,
    }


def compare_before_after(before: dict, after: dict) -> dict:
    """Scientific before/after comparison matrix. Rejects refinements that worsen perceptual or directional continuity."""
    bm = before["metrics"]
    am = after["metrics"]

    b_spd = float(bm["velocityContinuity"]["internalMedianSpeedPx"])
    a_spd = float(am["velocityContinuity"]["internalMedianSpeedPx"])
    motion_retention = float(a_spd / max(b_spd, 0.15)) if b_spd >= 0.25 else 1.0

    comparisons = {
        "seamMae": (bm["visualSimilarity"]["seamMae"], am["visualSimilarity"]["seamMae"], "lower"),
        "seamSsim": (bm["visualSimilarity"]["seamSsim"], am["visualSimilarity"]["seamSsim"], "higher"),
        "seamFlowErrorPx": (bm["flowContinuity"]["seamFlowErrorPx"], am["flowContinuity"]["seamFlowErrorPx"], "lower"),
        "seamDirectionCosine": (bm["flowContinuity"]["seamDirectionCosine"], am["flowContinuity"]["seamDirectionCosine"], "higher"),
        "seamOppositeFraction": (bm["flowContinuity"]["seamOppositeFraction"], am["flowContinuity"]["seamOppositeFraction"], "lower"),
        "velocityJump": (bm["velocityContinuity"]["relativeJump"], am["velocityContinuity"]["relativeJump"], "lower"),
        "edgeRatio": (bm["edgeConsistency"]["edgeRatio"], am["edgeConsistency"]["edgeRatio"], "lower"),
        "seamDeltaL": (abs(bm["luminanceContinuity"]["seamDeltaL"]), abs(am["luminanceContinuity"]["seamDeltaL"]), "lower"),
        "seamDeltaChroma": (bm["chromaticContinuity"]["seamDeltaChroma"], am["chromaticContinuity"]["seamDeltaChroma"], "lower"),
        "flickerScore": (bm["flicker"]["score"], am["flicker"]["score"], "lower"),
        "ghostingScore": (bm["ghosting"]["score"], am["ghosting"]["score"], "lower"),
        "sharpnessRetention": (bm["texturePreservation"]["sharpnessRetention"], am["texturePreservation"]["sharpnessRetention"], "higher"),
        "motionRetention": (1.0, round(min(motion_retention, 2.0), 4), "higher"),
    }

    matrix = {}
    improvements = []
    regressions = []
    for name, (b_val, a_val, better) in comparisons.items():
        delta = round(a_val - b_val, 4)
        improved = (delta < -1e-4) if better == "lower" else (delta > 1e-4)
        worsened = (delta > 1e-4) if better == "lower" else (delta < -1e-4)
        matrix[name] = {
            "before": round(b_val, 4),
            "after": round(a_val, 4),
            "delta": delta,
            "better": better,
            "status": "improved" if improved else ("regressed" if worsened else "unchanged"),
        }
        if improved:
            improvements.append(name)
        elif worsened:
            regressions.append(name)

    # Hard rejection triggers: never accept reversals, freezes, ghosting, blur, protected drift,
    # directional degradation, or artificial motion dampening!
    hard_reject = []
    if am["reversals"]["detected"] and not bm["reversals"]["detected"]:
        hard_reject.append("Introduziu reversão artificial.")
    if am["freezes"]["detected"] and not bm["freezes"]["detected"]:
        hard_reject.append("Introduziu congelamento/duplicação.")
    if am["ghosting"]["detected"] and not bm["ghosting"]["detected"]:
        hard_reject.append("Introduziu ghosting perceptível.")
    if am["texturePreservation"]["sharpnessRetention"] < 0.72 and (
        am["texturePreservation"]["sharpnessRetention"] < bm["texturePreservation"]["sharpnessRetention"] - 0.12
    ):
        hard_reject.append("Destruiu nitidez/textura para disfarçar a costura.")
    if am["staticIntegrity"]["checked"] and am["staticIntegrity"]["maxAbsDiff"] > bm["staticIntegrity"]["maxAbsDiff"] + 2.0:
        hard_reject.append("Corrompeu região estática protegida.")

    # Directional degradation gate (prevents improving static appearance while breaking motion direction!)
    if am["flowContinuity"]["directionMeasured"] and bm["flowContinuity"]["directionMeasured"]:
        b_cos = float(bm["flowContinuity"]["seamDirectionCosine"])
        a_cos = float(am["flowContinuity"]["seamDirectionCosine"])
        b_opp = float(bm["flowContinuity"]["seamOppositeFraction"])
        a_opp = float(am["flowContinuity"]["seamOppositeFraction"])
        if (a_cos < b_cos - 0.18 and a_cos < 0.50) or (a_opp > b_opp + 0.14 and a_opp > 0.25):
            hard_reject.append("Piorou a continuidade direcional do movimento na junção.")

    # Artificial motion-dampening gate (never shrink genuine movement to artificially improve seam scores!)
    if b_spd >= 0.45 and a_spd < b_spd * 0.62 and (bm["visualSimilarity"]["internalMedianMae"] >= 1.2):
        hard_reject.append("Reduziu artificialmente a amplitude de movimento para mascarar a costura.")

    def _perceptual_cost(rep: dict) -> float:
        rm = rep["metrics"]
        fc = rm["flowContinuity"]
        dir_pen = (
            (1.0 - max(-1.0, min(1.0, float(fc["seamDirectionCosine"])))) * 1.15
            + float(fc["seamOppositeFraction"]) * 2.10
            if fc["directionMeasured"]
            else 0.0
        )
        return (
            float(rm["visualSimilarity"]["seamRatio"])
            + float(fc["flowErrorRatio"]) * 0.60
            + dir_pen
            + float(rm["velocityContinuity"]["relativeJump"]) * 0.85
            + max(0.0, float(rm["edgeConsistency"]["edgeRatio"]) - 1.0) * 0.60
            + float(rm["ghosting"]["score"]) * 2.00
            + float(rm["flicker"]["score"]) * 0.50
            + len(rep["issues"]) * 1.50
        )

    b_seam_cost = _perceptual_cost(before)
    a_seam_cost = _perceptual_cost(after)

    accepted = (not hard_reject) and (a_seam_cost <= b_seam_cost + 0.05)
    return {
        "accepted": accepted,
        "hardRejectReasons": hard_reject,
        "beforeCompositeCost": round(b_seam_cost, 4),
        "afterCompositeCost": round(a_seam_cost, 4),
        "improvements": improvements,
        "regressions": regressions,
        "matrix": matrix,
    }
