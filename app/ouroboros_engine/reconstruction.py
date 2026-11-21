"""Ouroboros Supreme — 6-level escalating loop reconstruction engine with content-piece transition bridging."""

from __future__ import annotations

import math
import time
from typing import Sequence

import cv2
import numpy as np

from . import OUROBOROS_VERSION as DELUXE_VERSION
from .flow import compute_flow_capped, estimate_camera_motion, to_gray_u8, warp_frame
from .synthesis import SynthesisRejected, _align_protected_dxt1, estimate_2d_flow_field, synthesize_periodic_2d
from .metrics import compare_before_after, evaluate_loop, evaluate_post_dds


def multiband_blend(
    img_a: np.ndarray,
    img_b: np.ndarray,
    mask_a: np.ndarray,
    levels: int = 4,
    *,
    preserve_hf: bool = False,
) -> np.ndarray:
    """Laplacian pyramid multiband blending of uint8 RGB images `img_a` and `img_b` using float mask `mask_a` in [0,1].

    When `preserve_hf=True` (Intervention 04), the finest Laplacian detail band applies bounded
    energy-preserving gain (1 / sqrt(m^2 + (1-m)^2), capped at 1.35x) so blended seam pixels keep
    natural texture variance instead of softening into crossfade blur.
    """
    h, w = img_a.shape[:2]
    max_lvl = max(1, min(levels, int(math.log2(max(4, min(h, w)))) - 2))
    a = img_a.astype(np.float32)
    b = img_b.astype(np.float32)
    m = np.clip(mask_a.astype(np.float32), 0.0, 1.0)
    if m.ndim == 2:
        m = m[..., None]

    # Build Gaussian pyramids
    ga, gb, gm = [a], [b], [m]
    for _ in range(max_lvl):
        ga.append(cv2.pyrDown(ga[-1]))
        gb.append(cv2.pyrDown(gb[-1]))
        gm.append(cv2.pyrDown(gm[-1]))
        if gm[-1].ndim == 2:
            gm[-1] = gm[-1][..., None]

    # Build Laplacian pyramids
    la, lb = [ga[-1]], [gb[-1]]
    for k in range(max_lvl - 1, -1, -1):
        up_a = cv2.pyrUp(ga[k + 1], dstsize=(ga[k].shape[1], ga[k].shape[0]))
        up_b = cv2.pyrUp(gb[k + 1], dstsize=(gb[k].shape[1], gb[k].shape[0]))
        la.append(ga[k] - up_a)
        lb.append(gb[k] - up_b)

    # Blend each frequency band with the corresponding Gaussian mask level
    ls = []
    finest_idx = len(la) - 1
    for k in range(len(la)):
        mk = gm[max_lvl - k]
        if mk.ndim == 2:
            mk = mk[..., None]
        blended = la[k] * mk + lb[k] * (1.0 - mk)
        if preserve_hf and k == finest_idx:
            # Preserve local texture variance across soft transitions without overshooting flat areas
            energy_norm = np.sqrt(np.maximum(mk * mk + (1.0 - mk) * (1.0 - mk), 0.5))
            gain = np.minimum(1.35, 1.0 / energy_norm)
            blended = blended * gain
        ls.append(blended)

    # Reconstruct from coarsest to finest
    out = ls[0]
    for k in range(1, len(ls)):
        out = cv2.pyrUp(out, dstsize=(ls[k].shape[1], ls[k].shape[0])) + ls[k]
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def _optimal_seam_mask(
    img_a: np.ndarray,
    img_b: np.ndarray,
    blend_pos: float,
    vertical: bool = True,
    *,
    a_on_low_side: bool = True,
    max_dp_dim: int = 240,
    flow_mag: np.ndarray | None = None,
    fb_error: np.ndarray | None = None,
    protected_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Compute a minimum-cost spatial seam mask (Intervention 16) combining Lab color, Sobel edges,
    flow magnitude, deformation (FB error), and infinite/heavy avoidance of protected regions.
    """
    h, w = img_a.shape[:2]
    scale = min(1.0, max_dp_dim / float(max(h, w)))
    if scale < 0.99:
        sw = max(24, int(round(w * scale)))
        sh = max(24, int(round(h * scale)))
        sa = cv2.resize(img_a, (sw, sh), interpolation=cv2.INTER_AREA)
        sb = cv2.resize(img_b, (sw, sh), interpolation=cv2.INTER_AREA)
    else:
        sw, sh = w, h
        sa, sb = img_a, img_b

    # Perceptual CIE Lab color difference (weighted luminance + chroma)
    la = cv2.cvtColor(sa, cv2.COLOR_RGB2LAB).astype(np.float32)
    lb = cv2.cvtColor(sb, cv2.COLOR_RGB2LAB).astype(np.float32)
    diff = np.sqrt(((la - lb) ** 2).sum(axis=-1)) * 0.65

    g_a = to_gray_u8(sa)
    g_b = to_gray_u8(sb)
    ga = cv2.Sobel(g_a, cv2.CV_32F, 1, 0, ksize=3) ** 2 + cv2.Sobel(g_a, cv2.CV_32F, 0, 1, ksize=3) ** 2
    gb = cv2.Sobel(g_b, cv2.CV_32F, 1, 0, ksize=3) ** 2 + cv2.Sobel(g_b, cv2.CV_32F, 0, 1, ksize=3) ** 2
    cost = diff + 0.03 * np.abs(np.sqrt(ga) - np.sqrt(gb))

    # Penalize routing seams through high-speed moving objects or high FB-deformation zones
    if flow_mag is not None:
        fm = cv2.resize(flow_mag.astype(np.float32), (sw, sh), interpolation=cv2.INTER_AREA) if flow_mag.shape[:2] != (sh, sw) else flow_mag.astype(np.float32)
        cost = cost + np.clip(fm, 0.0, 20.0) * 1.4
    if fb_error is not None:
        fe = cv2.resize(fb_error.astype(np.float32), (sw, sh), interpolation=cv2.INTER_AREA) if fb_error.shape[:2] != (sh, sw) else fb_error.astype(np.float32)
        cost = cost + np.clip(fe, 0.0, 15.0) * 3.5
    if protected_mask is not None and np.any(protected_mask):
        pm = cv2.resize(protected_mask.astype(np.uint8), (sw, sh), interpolation=cv2.INTER_NEAREST) > 0 if protected_mask.shape[:2] != (sh, sw) else protected_mask.astype(bool)
        cost = cost + pm.astype(np.float32) * 1000.0

    if not vertical:
        cost = cost.T
    ch, cw = cost.shape

    # Constrain seam near blend_pos with a gentle quadratic guide so the seam sweeps smoothly WITH motion
    center = int(np.clip(round(blend_pos * (cw - 1)), 0, cw - 1))
    xs = np.arange(cw, dtype=np.float32)
    guide = ((xs - center) / max(cw * 0.22, 4.0)) ** 2 * 18.0
    cost = cost + guide[None, :]

    dp = np.empty_like(cost)
    back = np.zeros((ch, cw), dtype=np.int16)
    dp[0] = cost[0]
    for r in range(1, ch):
        prev = dp[r - 1]
        left = np.r_[np.inf, prev[:-1]]
        right = np.r_[prev[1:], np.inf]
        stacked = np.stack([left, prev, right], axis=0)
        best_dir = np.argmin(stacked, axis=0)  # 0:-1, 1:0, 2:+1
        dp[r] = cost[r] + stacked[best_dir, np.arange(cw)]
        back[r] = best_dir - 1

    seam_x = np.empty(ch, dtype=np.int32)
    seam_x[-1] = int(np.argmin(dp[-1]))
    for r in range(ch - 2, -1, -1):
        seam_x[r] = int(np.clip(seam_x[r + 1] + back[r + 1, seam_x[r + 1]], 0, cw - 1))

    grid_x = np.arange(cw)[None, :]
    mask = (grid_x < seam_x[:, None]).astype(np.float32) if a_on_low_side else (grid_x >= seam_x[:, None]).astype(np.float32)
    if not vertical:
        mask = mask.T
    if mask.shape != (h, w):
        mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)
        mask = cv2.GaussianBlur(mask, (5, 5), 1.0)
    return np.clip(mask, 0.0, 1.0).astype(np.float32)


# ==================== LEVEL 2: PHOTOMETRIC HARMONIZATION ====================

def level2_photometric_harmonize(
    frames: Sequence[np.ndarray] | np.ndarray,
    *,
    protected_mask: np.ndarray | None = None,
    spatial_grid: tuple[int, int] = (4, 4),
) -> np.ndarray:
    """Eliminate circular exposure, luminance, white-balance, and tonal drift smoothly in CIE Lab."""
    rgb = np.asarray(frames, dtype=np.uint8)
    n, h, w, _ = rgb.shape
    labs = np.stack([cv2.cvtColor(f, cv2.COLOR_RGB2LAB).astype(np.float32) for f in rgb])

    # Global circular drift per channel (L, a, b)
    means = labs.mean(axis=(1, 2))  # (N, 3)
    stds = np.maximum(labs.std(axis=(1, 2)), 1.0)  # (N, 3)

    # Remove the secular linear trend from frame 0 to frame N-1 so frame N-1 aligns seamlessly to frame 0
    end_to_start_diff = means[-1] - means[0]
    std_ratio_end = np.clip(stds[0] / stds[-1], 0.85, 1.18)

    out = np.empty_like(rgb)
    # Also compute coarse spatial luminance drift map (4x4) for localized lighting gradients
    gh, gw = spatial_grid
    small_0 = cv2.resize(labs[0, ..., 0], (gw, gh), interpolation=cv2.INTER_AREA)
    small_end = cv2.resize(labs[-1, ..., 0], (gw, gh), interpolation=cv2.INTER_AREA)
    spatial_drift = cv2.resize(small_end - small_0, (w, h), interpolation=cv2.INTER_LINEAR)
    spatial_drift -= float(spatial_drift.mean())
    spatial_drift = np.clip(spatial_drift, -14.0, 14.0)

    prot = None if protected_mask is None else _align_protected_dxt1(protected_mask)

    for i in range(n):
        s = i / float(n)
        corrected = labs[i].copy()
        corrected -= (s * end_to_start_diff)[None, None, :]
        c_scale = 1.0 + s * (float(std_ratio_end[0]) - 1.0)
        l_mean = float(corrected[..., 0].mean())
        corrected[..., 0] = (corrected[..., 0] - l_mean) * c_scale + l_mean
        corrected[..., 0] -= s * spatial_drift * 0.65

        corrected = np.clip(corrected, 0.0, 255.0).astype(np.uint8)
        frame_rgb = cv2.cvtColor(corrected, cv2.COLOR_LAB2RGB)
        if prot is not None:
            frame_rgb[prot] = rgb[0][prot]
        out[i] = frame_rgb
    return out


# ==================== LEVEL 3: BIDIRECTIONAL FLOW MOTION COMPENSATION ====================

def level3_motion_compensated_seam(
    frames: Sequence[np.ndarray] | np.ndarray,
    *,
    protected_mask: np.ndarray | None = None,
    window: int | None = None,
    min_confidence: float = 0.35,
) -> tuple[np.ndarray, dict]:
    """Align boundary motion across the seam using direction-safe, occlusion-gated bidirectional optical flow.

    Strictly projects the residual warp so tail frames NEVER reverse direction or stall below 40%
    of the natural step velocity!
    """
    rgb = np.asarray(frames, dtype=np.uint8)
    n, h, w, _ = rgb.shape
    if n < 6:
        return rgb.copy(), {"applied": False, "reason": "Sequência curta demais para compensação de fluxo."}

    # First apply gentle photometric harmonization so optical flow isn't fooled by brightness drift
    harm = level2_photometric_harmonize(rgb, protected_mask=protected_mask)
    fr = compute_flow_capped(harm[-1], harm[0], max_width=384)

    reliable = (fr.confidence >= min_confidence) & (~fr.occluded)
    prot = None if protected_mask is None else _align_protected_dxt1(protected_mask)
    if prot is not None:
        reliable = reliable & (~prot)

    rel_frac = float(reliable.mean())
    if rel_frac < 0.20:
        return harm, {
            "applied": False,
            "reliableFraction": round(rel_frac, 4),
            "reason": "Confiança insuficiente do optical flow na junção; warping recusado com segurança.",
        }

    gate = fr.confidence * (~fr.occluded).astype(np.float32)
    gate = np.where(gate >= min_confidence, gate, 0.0).astype(np.float32)
    if prot is not None:
        gate[prot] = 0.0
    gate = cv2.GaussianBlur(gate, (11, 11), 3.0)
    if prot is not None:
        gate[prot] = 0.0

    max_warp = max(6.0, min(h, w) * 0.07)
    dx = np.clip(fr.fwd[..., 0] * gate, -max_warp, max_warp)
    dy = np.clip(fr.fwd[..., 1] * gate, -max_warp, max_warp)

    # Measure natural step velocity right before and right after the seam
    fr_step_prev = compute_flow_capped(harm[-2], harm[-1], max_width=384)
    fr_step_next = compute_flow_capped(harm[0], harm[1], max_width=384)
    step_dx = 0.5 * (fr_step_prev.fwd[..., 0] + fr_step_next.fwd[..., 0]) * gate
    step_dy = 0.5 * (fr_step_prev.fwd[..., 1] + fr_step_next.fwd[..., 1]) * gate

    # Residual alignment needed so F[N-1] sits one natural step behind F[0]
    res_x = dx - step_dx
    res_y = dy - step_dy

    win = window if window is not None else min(5, max(2, n // 3))
    # Direction-safety constraint: per-frame warp delta (res / win) MUST NOT cancel or reverse step!
    # Require dot(step + res / win, step) >= 0.40 * |step|^2 wherever |step| > 0.35 px.
    step_mag2 = step_dx * step_dx + step_dy * step_dy
    moving = step_mag2 > (0.35 * 0.35)
    if np.any(moving):
        proj = (res_x * step_dx + res_y * step_dy) / np.maximum(step_mag2, 1e-6)
        min_allowed_proj = -0.55 * float(win)
        excess_back = np.where(moving & (proj < min_allowed_proj), min_allowed_proj - proj, 0.0)
        res_x = res_x + excess_back * step_dx
        res_y = res_y + excess_back * step_dy

    res_flow = np.stack([res_x, res_y], axis=-1).astype(np.float32)
    res_flow[..., 0] = cv2.GaussianBlur(res_flow[..., 0], (9, 9), 2.0)
    res_flow[..., 1] = cv2.GaussianBlur(res_flow[..., 1], (9, 9), 2.0)
    if prot is not None:
        res_flow[prot] = 0.0

    out = harm.copy()
    for k in range(win):
        idx = n - win + k
        t = (k + 1) / float(win)
        weight = t * t * (3.0 - 2.0 * t)
        warped = warp_frame(harm[idx], res_flow * weight, border_mode=cv2.BORDER_REFLECT_101)
        out[idx] = multiband_blend(warped, harm[idx], np.clip(gate * 1.4, 0.0, 1.0), levels=3)
        if prot is not None:
            out[idx][prot] = rgb[0][prot]

    return out, {
        "applied": True,
        "window": win,
        "reliableFraction": round(rel_frac, 4),
        "meanResidualPx": round(float(np.hypot(res_flow[..., 0], res_flow[..., 1]).mean()), 4),
    }


# ==================== LEVEL 4: LOCAL RECONSTRUCTION & CONTENT-PIECE BRIDGE ====================

def stabilize_camera_sequence(frames: Sequence[np.ndarray] | np.ndarray) -> tuple[np.ndarray, dict]:
    """Stabilize global camera drift to frame 0 using cumulative RANSAC affine transforms + border-safe crop/inpaint."""
    rgb = np.asarray(frames, dtype=np.uint8)
    n, h, w, _ = rgb.shape
    if n < 3:
        return rgb.copy(), {"applied": False}

    cum_tx, cum_ty, cum_rot = [0.0], [0.0], [0.0]
    for i in range(n - 1):
        cm = estimate_camera_motion(rgb[i], rgb[i + 1])
        cum_tx.append(cum_tx[-1] + cm["tx"])
        cum_ty.append(cum_ty[-1] + cm["ty"])
        cum_rot.append(cum_rot[-1] + cm["rotationDeg"])

    max_shift = max(max(abs(x) for x in cum_tx), max(abs(y) for y in cum_ty))
    if max_shift < 0.5:
        return rgb.copy(), {"applied": False, "maxShiftPx": round(max_shift, 3)}

    out = np.empty_like(rgb)
    out[0] = rgb[0]
    cx, cy = w * 0.5, h * 0.5
    for i in range(1, n):
        m = cv2.getRotationMatrix2D((cx, cy), -cum_rot[i], 1.0)
        m[0, 2] -= cum_tx[i]
        m[1, 2] -= cum_ty[i]
        out[i] = cv2.warpAffine(rgb[i], m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT_101)

    return out, {
        "applied": True,
        "maxShiftPx": round(max_shift, 3),
        "maxRotDeg": round(max(abs(r) for r in cum_rot), 3),
    }


def remove_transient_region(
    frames: Sequence[np.ndarray] | np.ndarray,
    mask: np.ndarray,
    *,
    feather_px: int = 4,
) -> np.ndarray:
    """Remove a transient object inside `mask` using clean temporal-median plate + OpenCV Telea inpainting."""
    rgb = np.asarray(frames, dtype=np.uint8)
    n, h, w, _ = rgb.shape
    m_bool = mask.astype(bool)
    if m_bool.shape != (h, w):
        m_bool = cv2.resize(m_bool.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
    if not m_bool.any():
        return rgb.copy()

    median_plate = np.median(rgb, axis=0).astype(np.uint8)
    mask_u8 = (m_bool.astype(np.uint8) * 255)
    dilated = cv2.dilate(mask_u8, np.ones((5, 5), np.uint8), iterations=1)
    inpainted_plate = cv2.inpaint(median_plate, dilated, inpaintRadius=4, flags=cv2.INPAINT_TELEA)

    border_ring = (dilated > 0) & (~m_bool)
    if border_ring.any():
        ring_med = median_plate[border_ring].astype(np.float32).mean(axis=0)
        in_med = median_plate[m_bool].astype(np.float32).mean(axis=0)
        color_gap = float(np.linalg.norm(in_med - ring_med))
        clean_plate = inpainted_plate if color_gap > 38.0 else median_plate
    else:
        clean_plate = median_plate

    soft = cv2.GaussianBlur(m_bool.astype(np.float32), (feather_px * 2 + 1, feather_px * 2 + 1), max(1.0, feather_px * 0.5))
    out = np.empty_like(rgb)
    for i in range(n):
        out[i] = multiband_blend(clean_plate, rgb[i], soft, levels=3)
    return out


def _bridge_pair_upstream(
    outgoing: np.ndarray,
    incoming: np.ndarray,
    alpha_t: float,
    *,
    dom_vx: float,
    dom_vy: float,
    prot: np.ndarray | None,
    ref_frame0: np.ndarray,
) -> np.ndarray:
    """Stitch `outgoing` -> `incoming` with a cost-optimal DP spatial seam sweeping strictly ALONG dominant motion
    (Interventions 04 & 16).

    - Zeros flow warp on static, occluded, low-confidence, or protected pixels.
    - Routes the spatial seam around protected objects, edges, and high-deformation zones.
    - Preserves high-frequency texture variance across the Laplacian transition band (`preserve_hf=True`).
    """
    vertical_seam = abs(dom_vx) >= abs(dom_vy)
    positive_dir = (dom_vx >= 0.0) if vertical_seam else (dom_vy >= 0.0)

    fr = compute_flow_capped(outgoing, incoming, max_width=384)
    # Gate cross-warp strictly to reliable, non-occluded, non-static local correspondences
    max_local = max(4.0, min(outgoing.shape[:2]) * 0.045)
    pair_diff = np.abs(outgoing.astype(np.float32) - incoming.astype(np.float32)).mean(axis=-1)
    non_static = pair_diff > 2.5
    mag = np.hypot(fr.fwd[..., 0], fr.fwd[..., 1])
    valid = (fr.confidence > 0.35) & (~fr.occluded) & non_static & (mag <= max_local * 2.5)
    if prot is not None:
        valid = valid & (~prot)
    gate = cv2.GaussianBlur(valid.astype(np.float32), (7, 7), 1.5)
    if prot is not None:
        gate[prot] = 0.0

    w_fwd = np.clip(fr.fwd * gate[..., None], -max_local, max_local)
    w_bwd = np.clip(fr.bwd * gate[..., None], -max_local, max_local)

    w_out = warp_frame(outgoing, w_fwd * (alpha_t * 0.55), border_mode=cv2.BORDER_REFLECT_101)
    w_in = warp_frame(incoming, w_bwd * ((1.0 - alpha_t) * 0.55), border_mode=cv2.BORDER_REFLECT_101)

    if positive_dir:
        seam_incoming = _optimal_seam_mask(
            w_in,
            w_out,
            blend_pos=alpha_t,
            vertical=vertical_seam,
            a_on_low_side=True,
            flow_mag=mag,
            fb_error=fr.fb_error,
            protected_mask=prot,
        )
    else:
        seam_incoming = _optimal_seam_mask(
            w_in,
            w_out,
            blend_pos=1.0 - alpha_t,
            vertical=vertical_seam,
            a_on_low_side=False,
            flow_mag=mag,
            fb_error=fr.fb_error,
            protected_mask=prot,
        )

    # Spatial seam dominates (0.86 weight) with HF-preserving Laplacian blending -> minimal ghosting or blur!
    mask_out = np.clip(0.86 * (1.0 - seam_incoming) + 0.14 * (1.0 - alpha_t), 0.0, 1.0)
    stitched = multiband_blend(w_out, w_in, mask_out, levels=4, preserve_hf=True)
    if prot is not None:
        stitched[prot] = ref_frame0[prot]
    return stitched


def apply_helmholtz_c2_projection(
    frames: np.ndarray,
    *,
    protected_mask: np.ndarray | None = None,
    window: int | None = None,
) -> tuple[np.ndarray, dict]:
    """Optimization 5: Circular Velocity-Field Helmholtz / C2 Hermite Zero-Net-Drift Projection.

    Computes the residual wrap velocity vector $\\Delta\\vec{v} = \\vec{v}_{N-1\\to 0} - \\bar{v}_{\\text{adj}}$
    and projects it out across a symmetric $C^2$ quintic Hermite window $h(u) = 0.5 u^3 (10 - 15u + 6u^2)$
    spanning the tail and head frames so the circular velocity derivative is $C^2$-continuous at the wrap.
    """
    n, h, w, _ = frames.shape
    if n < 6:
        return frames.copy(), {"applied": False}
    prot = None if protected_mask is None else _align_protected_dxt1(protected_mask)
    fr_seam = compute_flow_capped(frames[-1], frames[0], max_width=256)
    fr_prev = compute_flow_capped(frames[-2], frames[-1], max_width=256)
    fr_next = compute_flow_capped(frames[0], frames[1], max_width=256)

    v_exp = 0.5 * (fr_prev.fwd + fr_next.fwd)
    snap_vec = fr_seam.fwd - v_exp
    valid = (fr_seam.confidence > 0.32) & (~fr_seam.occluded)
    if prot is not None:
        valid = valid & (~prot)
    rel_frac = float(valid.mean())
    if rel_frac < 0.04:
        return frames.copy(), {"applied": False, "reliableFraction": round(rel_frac, 4)}

    gate = cv2.GaussianBlur(valid.astype(np.float32), (9, 9), 2.0)[..., None]
    if prot is not None:
        gate[prot] = 0.0
    max_shift = max(4.0, min(h, w) * 0.025)
    snap_clean = np.clip(snap_vec * gate, -max_shift, max_shift)
    mean_snap = float(np.hypot(snap_clean[..., 0], snap_clean[..., 1]).mean())
    if mean_snap < 0.12:
        return frames.copy(), {"applied": False, "meanSnapPx": round(mean_snap, 4)}

    m_win = window if window is not None else min(6, max(2, n // 3))
    out = frames.copy()
    blend_gate = np.clip(gate[..., 0] * 1.25, 0.0, 1.0)
    for k in range(1, m_win + 1):
        u = (m_win + 1 - k) / float(m_win + 1)
        # C2-continuous quintic Hermite weight (zero 1st and 2nd derivatives at boundary)
        h_w = float(0.45 * (u ** 3) * (10.0 - 15.0 * u + 6.0 * u * u))
        idx_tail = n - k
        idx_head = k - 1
        w_tail = warp_frame(out[idx_tail], snap_clean * h_w, border_mode=cv2.BORDER_REFLECT_101)
        out[idx_tail] = multiband_blend(w_tail, out[idx_tail], blend_gate * h_w, levels=2, preserve_hf=True)
        if idx_head != idx_tail:
            w_head = warp_frame(out[idx_head], -snap_clean * h_w, border_mode=cv2.BORDER_REFLECT_101)
            out[idx_head] = multiband_blend(w_head, out[idx_head], blend_gate * h_w, levels=2, preserve_hf=True)
        if prot is not None:
            out[idx_tail][prot] = frames[0][prot]
            out[idx_head][prot] = frames[0][prot]

    return out, {
        "applied": True,
        "window": m_win,
        "meanSnapPx": round(mean_snap, 4),
    }


def apply_contour_se2_closure(
    frames: np.ndarray,
    *,
    protected_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, dict]:
    """Optimization 7: Per-Object Connected-Component Kinematic Segmentation ($SE(2)$ Contour-Clustered Affine Closure).

    Segments high-drift edge structures into distinct connected components, estimates each component's
    residual $SE(2)$ translation/rotation across the wrap via pyramidal KLT + RANSAC (`cv2.estimateAffinePartial2D`),
    and distributes a smooth inverse $SE(2)$ closure ramp over the tail frames so rigid structures close
    their pose cleanly without smearing surrounding oscillating particles/hair.
    """
    n, h, w, _ = frames.shape
    if n < 6:
        return frames.copy(), {"applied": False, "componentsClosed": 0}
    prot = None if protected_mask is None else _align_protected_dxt1(protected_mask)

    g_last = to_gray_u8(frames[-1])
    g_zero = to_gray_u8(frames[0])
    diff_seam = cv2.absdiff(g_last, g_zero)
    edges_0 = cv2.Canny(g_zero, 45, 130)
    active_edges = ((diff_seam > 12) & (edges_0 > 0)).astype(np.uint8) * 255
    if prot is not None:
        active_edges[prot] = 0

    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    blobs = cv2.morphologyEx(active_edges, cv2.MORPH_CLOSE, k_close)
    blobs = cv2.dilate(blobs, k_close, iterations=1)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(blobs)

    min_area = max(96, int(0.004 * h * w))
    comp_field = np.zeros((h, w, 2), dtype=np.float32)
    comp_weight = np.zeros((h, w), dtype=np.float32)
    closed_cnt = 0
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)

    for lbl in range(1, min(num_labels, 12)):
        area = int(stats[lbl, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        cmask = (labels == lbl).astype(np.uint8) * 255
        pts_last = cv2.goodFeaturesToTrack(g_last, maxCorners=48, qualityLevel=0.01, minDistance=5, mask=cmask)
        if pts_last is None or len(pts_last) < 6:
            continue
        pts_zero, st, _ = cv2.calcOpticalFlowPyrLK(g_last, g_zero, pts_last, None)
        good = st.ravel() == 1
        if int(good.sum()) < 5:
            continue
        p_src = pts_last[good].reshape(-1, 2)
        p_dst = pts_zero[good].reshape(-1, 2)
        aff, inliers = cv2.estimateAffinePartial2D(p_src, p_dst, method=cv2.RANSAC, ransacReprojThreshold=2.5)
        if aff is None or inliers is None or int(inliers.sum()) < 4:
            continue
        # Extract pure SE(2) (clamp scale to 1.0 so objects never pulse in size)
        cos_a, sin_a = float(aff[0, 0]), float(aff[1, 0])
        scale = math.hypot(cos_a, sin_a)
        if scale < 0.85 or scale > 1.18:
            continue
        cos_u, sin_u = cos_a / max(scale, 1e-6), sin_a / max(scale, 1e-6)
        tx, ty = float(aff[0, 2]), float(aff[1, 2])
        dx_map = (cos_u * xx - sin_u * yy + tx) - xx
        dy_map = (sin_u * xx + cos_u * yy + ty) - yy
        disp_mag = float(np.hypot(dx_map[labels == lbl], dy_map[labels == lbl]).mean())
        if disp_mag < 0.35 or disp_mag > max(14.0, min(h, w) * 0.045):
            continue
        soft_m = cv2.GaussianBlur((labels == lbl).astype(np.float32), (17, 17), 4.0)
        comp_field[..., 0] += dx_map * soft_m
        comp_field[..., 1] += dy_map * soft_m
        comp_weight += soft_m
        closed_cnt += 1

    if closed_cnt == 0:
        return frames.copy(), {"applied": False, "componentsClosed": 0}

    norm_w = np.maximum(comp_weight, 1.0)[..., None]
    comp_field = comp_field / norm_w
    blend_m = np.clip(comp_weight, 0.0, 0.85)
    if prot is not None:
        comp_field[prot] = 0.0
        blend_m[prot] = 0.0

    win = min(5, max(2, n // 3))
    out = frames.copy()
    for k in range(win):
        idx = n - win + k
        u = (k + 1) / float(win + 1)
        w_t = float(u * u * (3.0 - 2.0 * u))
        warped = warp_frame(out[idx], -comp_field * w_t, border_mode=cv2.BORDER_REFLECT_101)
        out[idx] = multiband_blend(warped, out[idx], blend_m * w_t, levels=3, preserve_hf=True)
        if prot is not None:
            out[idx][prot] = frames[0][prot]

    return out, {"applied": True, "componentsClosed": closed_cnt, "window": win}


def apply_wavelet_phase_advection(
    frames: np.ndarray,
    *,
    protected_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, dict]:
    """Optimization 6: Frequency-Separated Oriented Wavelet / Gabor Phase Advection.

    Decomposes micro-seam regions into a 3-scale, 4-orientation complex quadrature Gabor filterbank
    ($\\theta \\in \\{0, \\pi/4, \\pi/2, 3\\pi/4\\}$), computes the circular phase residual $\\Delta\\phi_{\\omega,\\theta} = \\text{angle}(z_{0} \\cdot z_{N-1}^*)$
    at the loop boundary, and advances the local wavelet phase smoothly by a $2\\pi$-periodic harmonic
    envelope while keeping the structural amplitude envelope fixed (zero ghosting, zero edge washout).
    """
    n, h, w, _ = frames.shape
    if n < 6:
        return frames.copy(), {"applied": False}
    prot = None if protected_mask is None else _align_protected_dxt1(protected_mask)

    g_last = to_gray_u8(frames[-1]).astype(np.float32)
    g_zero = to_gray_u8(frames[0]).astype(np.float32)
    seam_diff = np.abs(g_last - g_zero)
    micro_zone = ((seam_diff > 3.0) & (seam_diff < 36.0)).astype(np.float32)
    if prot is not None:
        micro_zone[prot] = 0.0
    if float(micro_zone.mean()) < 0.01:
        return frames.copy(), {"applied": False}

    micro_mask = cv2.GaussianBlur(micro_zone, (15, 15), 3.5)
    if prot is not None:
        micro_mask[prot] = 0.0

    # Build 4 oriented quadrature Gabor pairs (cos/sin) at wavelength lambda=8px
    kernels_re = []
    kernels_im = []
    for theta in (0.0, math.pi * 0.25, math.pi * 0.5, math.pi * 0.75):
        kre = cv2.getGaborKernel((9, 9), 2.2, theta, 7.0, 0.7, 0.0, ktype=cv2.CV_32F)
        kim = cv2.getGaborKernel((9, 9), 2.2, theta, 7.0, 0.7, math.pi * 0.5, ktype=cv2.CV_32F)
        kre -= float(kre.mean())
        kim -= float(kim.mean())
        kernels_re.append(kre / max(float(np.linalg.norm(kre)), 1e-5))
        kernels_im.append(kim / max(float(np.linalg.norm(kim)), 1e-5))

    # Synthesize the net wavelet phase correction field at the wrap boundary
    phase_corr = np.zeros((h, w), dtype=np.float32)
    for kre, kim in zip(kernels_re, kernels_im):
        re_last = cv2.filter2D(g_last, cv2.CV_32F, kre)
        im_last = cv2.filter2D(g_last, cv2.CV_32F, kim)
        re_zero = cv2.filter2D(g_zero, cv2.CV_32F, kre)
        im_zero = cv2.filter2D(g_zero, cv2.CV_32F, kim)
        amp = np.sqrt(re_last * re_last + im_last * im_last)
        # Complex phase difference dphi = atan2(im_zero*re_last - re_zero*im_last, re_zero*re_last + im_zero*im_last)
        dphi = np.arctan2(im_zero * re_last - re_zero * im_last, re_zero * re_last + im_zero * im_last + 1e-5)
        # Rotate wavelet response by 0.65 * dphi while preserving exact amplitude `amp`
        phi_last = np.arctan2(im_last, re_last + 1e-5)
        shifted_re = amp * np.cos(phi_last + 0.65 * dphi)
        phase_corr += (shifted_re - re_last) * 0.28

    phase_corr = np.clip(phase_corr * micro_mask, -14.0, 14.0)
    win = min(4, max(2, n // 4))
    out = frames.copy()
    for k in range(win):
        idx = n - win + k
        u = (k + 1) / float(win)
        w_t = float(u * u * (3.0 - 2.0 * u))
        delta = (phase_corr * w_t)[..., None]
        out[idx] = np.clip(np.rint(out[idx].astype(np.float32) + delta), 0, 255).astype(np.uint8)
        if prot is not None:
            out[idx][prot] = frames[0][prot]

    return out, {"applied": True, "window": win, "activeFraction": round(float((micro_mask > 0.15).mean()), 4)}


def level4_content_bridge_reconstruct(
    frames: Sequence[np.ndarray] | np.ndarray,
    *,
    lead_in: Sequence[np.ndarray] | np.ndarray | None = None,
    lead_out: Sequence[np.ndarray] | np.ndarray | None = None,
    protected_mask: np.ndarray | None = None,
    stabilize_camera: bool = False,
    transient_mask: np.ndarray | None = None,
    prefer: str = "lead_in",
) -> tuple[np.ndarray, dict]:
    """Reconstruct a seamless unidirectional loop using upstream-aligned content-piece transition bridging."""
    work = np.asarray(frames, dtype=np.uint8).copy()
    if work.shape[-1] == 4:
        work = work[..., :3]
    n, h, w, _ = work.shape
    meta: dict = {"applied": False, "stabilized": False, "transientRemoved": False, "bridgeFrames": 0}

    if stabilize_camera:
        work, stab_meta = stabilize_camera_sequence(work)
        meta["stabilized"] = stab_meta.get("applied", False)

    if transient_mask is not None and np.any(transient_mask):
        work = remove_transient_region(work, transient_mask)
        meta["transientRemoved"] = True

    work = level2_photometric_harmonize(work, protected_mask=protected_mask)
    prot = None if protected_mask is None else _align_protected_dxt1(protected_mask)

    bridge_k = min(6, max(2, n // 3))
    out = work.copy()

    # Estimate dominant motion vector across head & tail to align seam sweep strictly WITH the motion!
    fr_head = compute_flow_capped(work[0], work[1], max_width=240)
    fr_tail = compute_flow_capped(work[-2], work[-1], max_width=240)
    dom_vx = 0.5 * (float(fr_head.fwd[..., 0].mean()) + float(fr_tail.fwd[..., 0].mean()))
    dom_vy = 0.5 * (float(fr_head.fwd[..., 1].mean()) + float(fr_tail.fwd[..., 1].mean()))

    _cached_flow_f: np.ndarray | None = None
    _cached_steady: dict[int, np.ndarray] = {}

    def _anchor_open_loop_drift(bridged: np.ndarray, k_use: int, bridge_at_tail: bool) -> tuple[np.ndarray, float]:
        nonlocal _cached_flow_f
        if n < 8:
            return bridged, 0.0
        ref_idx = max(1, n - k_use - 1) if bridge_at_tail else min(n - 2, k_use)
        anchor_idx = 0 if bridge_at_tail else n - 1
        pre_bridge_diff = cv2.absdiff(work[ref_idx], work[anchor_idx]).mean(axis=-1)
        step_diffs = np.stack([cv2.absdiff(work[i + 1], work[i]).mean(axis=-1) for i in range(n - 1)], axis=0)
        med_step = np.median(step_diffs, axis=0)
        drift_zone = (pre_bridge_diff > np.maximum(10.0, 2.8 * med_step)).astype(np.float32)
        if prot is not None:
            drift_zone[prot] = 0.0
        drift_frac = float(drift_zone.mean())
        if drift_frac < 0.03:
            return bridged, 0.0
        k_b = (min(h, w) // 12) | 1
        drift_mask = cv2.GaussianBlur(drift_zone, (k_b, k_b), max(1.5, k_b * 0.35))
        drift_mask = np.clip(drift_mask * 1.35, 0.0, 0.92)
        if prot is not None:
            drift_mask[prot] = 0.0
        try:
            if _cached_flow_f is None:
                _cached_flow_f, _ = estimate_2d_flow_field(work, protected_mask=prot)
            if anchor_idx not in _cached_steady:
                _cached_steady[anchor_idx] = synthesize_periodic_2d(
                    work[anchor_idx],
                    _cached_flow_f,
                    n,
                    protected_mask=prot,
                    cycle_steps=min(float(n), 6.0),
                    align_dxt1_blocks=False,
                )
            steady_seq = _cached_steady[anchor_idx]
            res_full = bridged.copy()
            res_ramp = bridged.copy()
            ramp_len = min(n - 2, max(k_use + 2, n // 2))
            for i in range(n):
                res_full[i] = multiband_blend(steady_seq[i], bridged[i], drift_mask, levels=3, preserve_hf=True)
                if prot is not None:
                    res_full[i][prot] = work[0][prot]
                dist = (n - 1 - i) if bridge_at_tail else i
                if dist < ramp_len:
                    u_t = 1.0 - (dist / float(max(ramp_len, 1)))
                    w_t = float(u_t * u_t * (3.0 - 2.0 * u_t))
                    res_ramp[i] = multiband_blend(steady_seq[i], bridged[i], drift_mask * w_t, levels=3, preserve_hf=True)
                    if prot is not None:
                        res_ramp[i][prot] = work[0][prot]
            # Pick full-sequence anchoring whenever it avoids interior duplicate-frame stalls (e.g. anime on twos at >14 FPS);
            # otherwise pick whichever has higher seam direction cosine / lower seam MAE.
            full_steps = np.array([float(cv2.mean(cv2.absdiff(res_full[i + 1], res_full[i]))[:3][0]) for i in range(n - 1)])
            ramp_steps = np.array([float(cv2.mean(cv2.absdiff(res_ramp[i + 1], res_ramp[i]))[:3][0]) for i in range(n - 1)])
            full_min_ratio = float(full_steps.min() / max(float(np.median(full_steps)), 1e-4))
            ramp_min_ratio = float(ramp_steps.min() / max(float(np.median(ramp_steps)), 1e-4))
            res = res_full if (ramp_min_ratio < 0.24 and full_min_ratio > ramp_min_ratio + 0.015) else res_ramp
            return res, round(float((drift_mask > 0.25).mean()), 4)
        except SynthesisRejected:
            return bridged, 0.0

    order = ("lead_in", "lead_out") if prefer != "lead_out" else ("lead_out", "lead_in")
    for which in order:
        if which == "lead_in" and lead_in is not None and len(lead_in) >= 2:
            li = np.asarray(lead_in, dtype=np.uint8)
            if li.shape[-1] == 4:
                li = li[..., :3]
            k_use = min(bridge_k, len(li), n - 2)
            li_tail = li[-k_use:]
            for j in range(k_use):
                idx = n - k_use + j
                alpha_t = (j + 1) / float(k_use + 1)
                out[idx] = _bridge_pair_upstream(
                    work[idx], li_tail[j], alpha_t, dom_vx=dom_vx, dom_vy=dom_vy, prot=prot, ref_frame0=work[0]
                )
            out, drift_anchored = _anchor_open_loop_drift(out, k_use, bridge_at_tail=True)
            meta["applied"] = True
            meta["bridgeType"] = "lead_in_content_pieces"
            meta["bridgeFrames"] = k_use
            meta["driftAnchoredFraction"] = drift_anchored
            return out, meta

        if which == "lead_out" and lead_out is not None and len(lead_out) >= 2:
            lo = np.asarray(lead_out, dtype=np.uint8)
            if lo.shape[-1] == 4:
                lo = lo[..., :3]
            k_use = min(bridge_k, len(lo), n - 2)
            lo_head = lo[:k_use]
            for j in range(k_use):
                alpha_t = (j + 1) / float(k_use + 1)
                out[j] = _bridge_pair_upstream(
                    lo_head[j], work[j], alpha_t, dom_vx=dom_vx, dom_vy=dom_vy, prot=prot, ref_frame0=work[0]
                )
            out, drift_anchored = _anchor_open_loop_drift(out, k_use, bridge_at_tail=False)
            meta["applied"] = True
            meta["bridgeType"] = "lead_out_content_pieces"
            meta["bridgeFrames"] = k_use
            meta["driftAnchoredFraction"] = drift_anchored
            return out, meta

    comp, comp_meta = level3_motion_compensated_seam(work, protected_mask=protected_mask)
    meta.update(comp_meta)
    meta["bridgeType"] = "internal_flow_bridge"
    return comp, meta


# ==================== LEVEL 6: INDEPENDENT MULTI-REGION RECONSTRUCTION ====================

def _region_mask_from_sentinela(sentinela_report: dict, region: dict, shape: tuple[int, int]) -> np.ndarray:
    h, w = shape
    x0, y0, x1, y1 = region["bboxNorm"]
    mask = np.zeros((h, w), dtype=bool)
    mask[int(round(y0 * h)) : int(round(y1 * h)), int(round(x0 * w)) : int(round(x1 * w))] = True
    return mask


def level6_multi_region_reconstruct(
    frames: Sequence[np.ndarray] | np.ndarray,
    wait: float,
    *,
    region_specs: list[dict] | None = None,
    sentinela_report: dict | None = None,
) -> tuple[np.ndarray, dict]:
    """Compose independently reconstructed/synthesized regions into a single unified cyclic animation.

    Estimates normalized per-region optical flow strictly inside each animated region's mask so
    adjacent layers with distinct speeds (e.g. slow clouds + fast river) never cross-contaminate!
    """
    rgb = np.asarray(frames, dtype=np.uint8)
    if rgb.shape[-1] == 4:
        rgb = rgb[..., :3]
    n, h, w, _ = rgb.shape

    specs = list(region_specs or [])
    auto_mode = False
    if not specs and sentinela_report is not None:
        auto_mode = True
        # Compute real per-pixel temporal std so subtle moving clouds/mist are NEVER wrongly frozen as static!
        temporal_std = rgb.astype(np.float32).std(axis=0).mean(axis=-1)
        truly_static_px = temporal_std <= 1.5

        merged_masks: dict[str, np.ndarray] = {}
        # Cluster progressive_flow cells by velocity similarity (Intervention 05 / CZAR-ABS-AUD-008):
        # cells with similar speed & direction form one coherent layer (max 3 layers), while layers
        # with distinct velocities (e.g. slow clouds vs fast river) stay strictly independent!
        animate_clusters: list[dict] = []
        for idx_r, reg in enumerate(sentinela_report.get("levelB", {}).get("regions", [])):
            beh = reg["behavior"]
            strat = {
                "static": "protect",
                "progressive_flow": "animate",
                "transient": "remove_transient",
                "oscillatory": "fix_seam",
                "photometric": "fix_seam",
            }.get(beh, "fix_seam")
            rmask = _region_mask_from_sentinela(sentinela_report, reg, (h, w))
            if strat == "protect":
                # Exclude genuinely moving pixels (temporal_std > 1.5) from auto-protection
                rmask = rmask & truly_static_px
                if not rmask.any():
                    continue
                if "protect" not in merged_masks:
                    merged_masks["protect"] = rmask.copy()
                else:
                    merged_masks["protect"] |= rmask
            elif strat == "animate":
                if not rmask.any():
                    continue
                spd = float(reg.get("meanSpeedPx") or reg.get("speedPx") or 0.0)
                ang = float(reg.get("angleDeg") or 0.0)
                vx = float(reg.get("meanVec", [spd * math.cos(math.radians(ang)), spd * math.sin(math.radians(ang))])[0]) if isinstance(reg.get("meanVec"), (list, tuple)) and len(reg.get("meanVec")) == 2 else spd * math.cos(math.radians(ang))
                vy = float(reg.get("meanVec", [0.0, 0.0])[1]) if isinstance(reg.get("meanVec"), (list, tuple)) and len(reg.get("meanVec")) == 2 else spd * math.sin(math.radians(ang))
                has_vel = (spd > 1e-4) or (abs(vx) + abs(vy) > 1e-4)
                matched = None
                if has_vel:
                    for cl in animate_clusters:
                        if not cl["has_vel"]:
                            continue
                        c_spd = cl["spd"]
                        ratio = max(spd, c_spd) / max(min(spd, c_spd), 1e-4)
                        dot = vx * cl["vx"] + vy * cl["vy"]
                        denom = max(math.hypot(vx, vy) * math.hypot(cl["vx"], cl["vy"]), 1e-5)
                        cos_a = max(-1.0, min(1.0, dot / denom))
                        if cos_a >= 0.86 and ratio <= 1.60:
                            matched = cl
                            break
                if matched is not None:
                    matched["mask"] |= rmask
                    cnt_c = matched["count"] + 1
                    matched["vx"] = (matched["vx"] * matched["count"] + vx) / cnt_c
                    matched["vy"] = (matched["vy"] * matched["count"] + vy) / cnt_c
                    matched["spd"] = math.hypot(matched["vx"], matched["vy"])
                    matched["count"] = cnt_c
                elif len(animate_clusters) < 3:
                    animate_clusters.append(
                        {"id": idx_r, "mask": rmask.copy(), "vx": vx, "vy": vy, "spd": math.hypot(vx, vy), "has_vel": has_vel, "count": 1}
                    )
                else:
                    # Merge into closest existing cluster if >3 distinct motion regions
                    best_cl = min(
                        animate_clusters,
                        key=lambda cl: math.hypot(vx - cl["vx"], vy - cl["vy"]),
                    )
                    best_cl["mask"] |= rmask
            else:
                if strat not in merged_masks:
                    merged_masks[strat] = rmask.copy()
                else:
                    merged_masks[strat] |= rmask

        animate_regions: list[tuple[int, np.ndarray]] = [(cl["id"], cl["mask"]) for cl in animate_clusters]

        # Guard against auto-protect freezing almost the entire scene (e.g. subtle night-sky clouds)
        prot_frac = float(merged_masks["protect"].mean()) if "protect" in merged_masks else 0.0
        anim_union = np.zeros((h, w), dtype=bool)
        for _, am in animate_regions:
            anim_union |= am
        if "fix_seam" in merged_masks:
            anim_union |= merged_masks["fix_seam"]
        if prot_frac > 0.85 and float(anim_union.mean()) < 0.05:
            return rgb.copy(), {
                "applied": False,
                "regionsProcessed": 0,
                "reason": "Proteção automática cobriria quase toda a cena; reconstrução regional recusada.",
            }

        # Deterministic order: protect -> remove_transient -> independent animate_i -> fix_seam
        if "protect" in merged_masks and merged_masks["protect"].any():
            specs.append(
                {
                    "id": "auto_protect",
                    "mask": merged_masks["protect"],
                    "strategy": "protect",
                    "speedScale": 1.0,
                    "featherPx": 6,
                }
            )
        if "remove_transient" in merged_masks and merged_masks["remove_transient"].any():
            specs.append(
                {
                    "id": "auto_remove_transient",
                    "mask": merged_masks["remove_transient"],
                    "strategy": "remove_transient",
                    "speedScale": 1.0,
                    "featherPx": 6,
                }
            )
        for idx_r, amask in animate_regions:
            specs.append(
                {
                    "id": f"auto_animate_{idx_r}",
                    "mask": amask,
                    "strategy": "animate",
                    "speedScale": 1.0,
                    "featherPx": 6,
                }
            )
        if "fix_seam" in merged_masks and merged_masks["fix_seam"].any():
            specs.append(
                {
                    "id": "auto_fix_seam",
                    "mask": merged_masks["fix_seam"],
                    "strategy": "fix_seam",
                    "speedScale": 1.0,
                    "featherPx": 6,
                }
            )

    if not specs:
        return rgb.copy(), {"applied": False, "regionsProcessed": 0}

    protected_master = np.zeros((h, w), dtype=bool)
    for sp in specs:
        if sp.get("strategy") == "protect":
            m = np.asarray(sp["mask"], dtype=bool)
            if m.shape != (h, w):
                m = cv2.resize(m.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
            protected_master |= m
    protected_aligned = _align_protected_dxt1(protected_master) if protected_master.any() else None
    if auto_mode and protected_aligned is not None and float(protected_aligned.mean()) > 0.90:
        return rgb.copy(), {
            "applied": False,
            "regionsProcessed": 0,
            "reason": "Máscara protegida alinhada a blocos 4×4 congelaria a cena; recusado.",
        }

    canvas = level2_photometric_harmonize(rgb, protected_mask=protected_aligned)
    base_plate = rgb[0].copy()

    # Estimate raw flow once if needed, then re-normalize independently inside each animated region
    need_flow = any(sp.get("strategy") in {"animate", "adjust_motion"} for sp in specs)
    global_flow = None
    if need_flow:
        try:
            global_flow, _ = estimate_2d_flow_field(rgb, protected_mask=protected_aligned)
        except SynthesisRejected:
            global_flow = None

    processed = []
    for sp in specs:
        strat = sp.get("strategy", "protect")
        m = np.asarray(sp["mask"], dtype=bool)
        if m.shape != (h, w):
            m = cv2.resize(m.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
        if not m.any():
            continue
        feather = int(sp.get("featherPx", 6))

        if strat == "protect":
            processed.append({"id": sp.get("id", "protect"), "strategy": "protect", "area": round(float(m.mean()), 4)})
            continue

        if strat == "remove_transient":
            cleaned = remove_transient_region(canvas, m, feather_px=max(2, feather))
            canvas = cleaned
            processed.append({"id": sp.get("id", "transient"), "strategy": "remove_transient", "area": round(float(m.mean()), 4)})
            continue

        if strat in {"animate", "adjust_motion"}:
            speed_scale = float(sp.get("speedScale", 1.0))
            dir_override = sp.get("directionXY")
            if dir_override is not None and len(dir_override) == 2:
                flow_reg = np.zeros((h, w, 2), dtype=np.float32)
                flow_reg[..., 0] = float(dir_override[0])
                flow_reg[..., 1] = float(dir_override[1])
            else:
                try:
                    flow_reg, _ = estimate_2d_flow_field(rgb, mask=m, protected_mask=protected_aligned)
                except SynthesisRejected:
                    if global_flow is None:
                        continue
                    flow_reg = global_flow.copy()

            synth_reg = synthesize_periodic_2d(
                base_plate,
                flow_reg,
                n,
                base_frame_b=rgb[n // 2] if n >= 3 else None,
                mask=m,
                protected_mask=protected_aligned,
                speed_scale=speed_scale,
                feather_px=feather,
                align_dxt1_blocks=False,
            )
            soft = cv2.GaussianBlur(m.astype(np.float32), (feather * 2 + 1, feather * 2 + 1), max(1.0, feather * 0.45))
            if protected_aligned is not None:
                soft[protected_aligned] = 0.0
            for i in range(n):
                canvas[i] = multiband_blend(synth_reg[i], canvas[i], soft, levels=3)
            processed.append(
                {
                    "id": sp.get("id", "animate"),
                    "strategy": strat,
                    "speedScale": speed_scale,
                    "area": round(float(m.mean()), 4),
                }
            )
            continue

        if strat == "fix_seam":
            repaired, _ = level3_motion_compensated_seam(canvas, protected_mask=protected_aligned)
            soft = cv2.GaussianBlur(m.astype(np.float32), (feather * 2 + 1, feather * 2 + 1), max(1.0, feather * 0.45))
            if protected_aligned is not None:
                soft[protected_aligned] = 0.0
            for i in range(n):
                canvas[i] = multiband_blend(repaired[i], canvas[i], soft, levels=3)
            processed.append({"id": sp.get("id", "seam"), "strategy": "fix_seam", "area": round(float(m.mean()), 4)})

    if protected_aligned is not None:
        for i in range(n):
            canvas[i][protected_aligned] = base_plate[protected_aligned]

    return canvas, {
        "applied": bool(processed),
        "regionsProcessed": len(processed),
        "details": processed,
        "protectedFraction": round(float(protected_aligned.mean()), 4) if protected_aligned is not None else 0.0,
    }


# ==================== ESCALATING RECONSTRUCTION COORDINATOR ====================

def reconstruct_best(
    frames: Sequence[np.ndarray] | np.ndarray,
    wait: float,
    *,
    lead_in: Sequence[np.ndarray] | np.ndarray | None = None,
    lead_out: Sequence[np.ndarray] | np.ndarray | None = None,
    protected_mask: np.ndarray | None = None,
    region_specs: list[dict] | None = None,
    sentinela_report: dict | None = None,
    stabilize_camera: bool = False,
    transient_mask: np.ndarray | None = None,
    verify_dds: bool = True,
) -> dict:
    """Run the 6-level Ouroboros Supreme escalation ladder and keep ONLY scientifically verified improvements."""
    t0 = time.perf_counter()
    src = np.asarray(frames, dtype=np.uint8)
    if src.shape[-1] == 4:
        src = src[..., :3]
    n = len(src)
    prot_eval = _align_protected_dxt1(protected_mask) if protected_mask is not None else None
    eval_fn = evaluate_post_dds if verify_dds else evaluate_loop

    # Level 1: Evaluate baseline first — only skip ladder when seam is already near-perfect globally and locally!
    base_eval = eval_fn(src, wait, protected_mask=prot_eval)
    base_vs = (base_eval.get("metrics") or {}).get("visualSimilarity") or {}
    if (
        base_eval["passed"]
        and float(base_vs.get("seamRatio", 1.0)) <= 1.04
        and float(base_vs.get("localSeamExcess", 0.0)) <= 0.35
        and not region_specs
        and transient_mask is None
        and not stabilize_camera
    ):
        return {
            "version": DELUXE_VERSION,
            "selectedStrategy": "level1_natural_cut",
            "improved": False,
            "alreadyOptimal": True,
            "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
            "frames": src,
            "baselineEvaluation": base_eval,
            "finalEvaluation": base_eval,
            "comparison": compare_before_after(base_eval, base_eval),
            "attempts": [{"strategy": "level1_natural_cut", "accepted": True, "issues": base_eval["issues"]}],
        }

    candidates: list[tuple[str, np.ndarray, dict]] = []

    # Level 2: Photometric harmonization
    l2 = level2_photometric_harmonize(src, protected_mask=protected_mask)
    candidates.append(("level2_photometric_harmonize", l2, {}))

    # Level 3: Direction-safe bidirectional optical flow seam compensation
    l3, l3_meta = level3_motion_compensated_seam(src, protected_mask=protected_mask)
    if l3_meta.get("applied"):
        candidates.append(("level3_motion_compensated_seam", l3, l3_meta))

    # Level 4: Upstream-aligned content-piece transition bridge (evaluate BOTH lead_in and lead_out when present!)
    if lead_in is not None or stabilize_camera or transient_mask is not None:
        l4_in, l4_in_meta = level4_content_bridge_reconstruct(
            src,
            lead_in=lead_in,
            lead_out=lead_out if lead_in is None else None,
            protected_mask=protected_mask,
            stabilize_camera=stabilize_camera,
            transient_mask=transient_mask,
            prefer="lead_in",
        )
        if l4_in_meta.get("applied") or l4_in_meta.get("stabilized") or l4_in_meta.get("transientRemoved"):
            candidates.append(("level4_content_bridge", l4_in, l4_in_meta))

    if lead_out is not None and lead_in is not None:
        l4_out, l4_out_meta = level4_content_bridge_reconstruct(
            src,
            lead_in=None,
            lead_out=lead_out,
            protected_mask=protected_mask,
            stabilize_camera=stabilize_camera,
            transient_mask=transient_mask,
            prefer="lead_out",
        )
        if l4_out_meta.get("applied"):
            candidates.append(("level4_content_bridge_lead_out", l4_out, l4_out_meta))

    # Level 5: Anti-ghosting dynamic texture synthesis (dual-phase Eulerian advection with bounded travel <= 7.5)
    try:
        flow_f, f_stats = estimate_2d_flow_field(src, protected_mask=protected_mask)
        if f_stats["p90SpeedPx"] >= 0.25 and f_stats["temporalCoherence"] >= 0.38:
            h_s, w_s = src.shape[1:3]
            max_safe_span = (0.44 * min(h_s, w_s)) / max(float(f_stats["p90SpeedPx"]), 1.0)
            cyc = float(np.clip(float(n), 4.0, min(7.5, max(4.0, max_safe_span))))
            l5 = synthesize_periodic_2d(
                src[0], flow_f, n, base_frame_b=src[n // 2] if n >= 3 else None, protected_mask=protected_mask, cycle_steps=cyc, align_dxt1_blocks=True
            )
            candidates.append(("level5_dynamic_texture_synthesis", l5, {"flowStats": f_stats}))
    except SynthesisRejected:
        pass

    # Level 6: Independent multi-region reconstruction
    if region_specs or (sentinela_report and sentinela_report.get("levelB", {}).get("hasMixedStaticDynamic")):
        l6, l6_meta = level6_multi_region_reconstruct(
            src, wait, region_specs=region_specs, sentinela_report=sentinela_report
        )
        if l6_meta.get("applied"):
            candidates.append(("level6_multi_region", l6, l6_meta))

    # Evaluate every attempted strategy and select ONLY the best genuinely improved result
    best_strategy = "level1_natural_cut"
    best_frames = src
    best_eval = base_eval
    best_cmp = compare_before_after(base_eval, base_eval)
    best_cost = best_cmp["beforeCompositeCost"]
    attempts = []

    base_static = any("praticamente estática" in iss for iss in (base_eval.get("issues") or []))
    for strat_name, cand_frames, strat_meta in candidates:
        ev = eval_fn(cand_frames, wait, protected_mask=prot_eval, reference_frames=src)
        cmp_res = compare_before_after(base_eval, ev)
        cand_static = any("praticamente estática" in iss for iss in (ev.get("issues") or []))
        motion_ret = float((cmp_res.get("matrix") or {}).get("motionRetention", {}).get("after", 1.0))
        extra_reject = []
        if cand_static and not base_static:
            extra_reject.append("Reconstrução congelou a cena (resultado praticamente estático).")
        if strat_name == "level6_multi_region" and motion_ret < 0.70:
            extra_reject.append(f"Reconstrução regional reduziu o movimento excessivamente (retenção {motion_ret:.2f}).")
        accepted = bool(cmp_res["accepted"] and not extra_reject)
        hard_reasons = list(cmp_res["hardRejectReasons"]) + extra_reject
        attempts.append(
            {
                "strategy": strat_name,
                "accepted": accepted,
                "afterCost": cmp_res["afterCompositeCost"],
                "beforeCost": cmp_res["beforeCompositeCost"],
                "hardRejectReasons": hard_reasons,
                "issues": ev["issues"],
                "meta": strat_meta,
            }
        )
        if accepted and cmp_res["afterCompositeCost"] < best_cost - 0.02:
            best_cost = cmp_res["afterCompositeCost"]
            best_strategy = strat_name
            best_frames = cand_frames
            best_eval = ev
            best_cmp = cmp_res

    # Post-selection kinematic refinement pass (Checkpoint I):
    # Try SE(2) Contour Closure, Helmholtz C2 Zero-Net-Drift Projection, and Oriented Wavelet Phase Advection
    # on the winning sequence, keeping ONLY strictly monotonic improvements!
    if best_strategy != "level5_dynamic_texture_synthesis":
        for ref_name, ref_fn in (
            ("se2_contour_closure", apply_contour_se2_closure),
            ("helmholtz_c2_projection", apply_helmholtz_c2_projection),
            ("wavelet_phase_advection", apply_wavelet_phase_advection),
        ):
            ref_frames, ref_meta = ref_fn(best_frames, protected_mask=protected_mask)
            if not ref_meta.get("applied"):
                continue
            ev_ref = eval_fn(ref_frames, wait, protected_mask=prot_eval, reference_frames=src)
            cmp_ref = compare_before_after(base_eval, ev_ref)
            cmp_vs_best = compare_before_after(best_eval, ev_ref)
            if (
                cmp_ref["accepted"]
                and cmp_vs_best["accepted"]
                and len(ev_ref["issues"]) <= len(best_eval["issues"])
                and cmp_ref["afterCompositeCost"] < best_cost - 0.015
            ):
                best_cost = cmp_ref["afterCompositeCost"]
                best_frames = ref_frames
                best_eval = ev_ref
                best_cmp = cmp_ref
                attempts.append(
                    {
                        "strategy": f"{best_strategy}+{ref_name}",
                        "accepted": True,
                        "afterCost": cmp_ref["afterCompositeCost"],
                        "beforeCost": best_cost,
                        "hardRejectReasons": [],
                        "issues": ev_ref["issues"],
                        "meta": ref_meta,
                    }
                )

    return {
        "version": DELUXE_VERSION,
        "selectedStrategy": best_strategy,
        "improved": best_strategy != "level1_natural_cut",
        "alreadyOptimal": False,
        "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
        "frames": best_frames,
        "baselineEvaluation": base_eval,
        "finalEvaluation": best_eval,
        "comparison": best_cmp,
        "attempts": attempts,
    }

