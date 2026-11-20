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


def multiband_blend(img_a: np.ndarray, img_b: np.ndarray, mask_a: np.ndarray, levels: int = 4) -> np.ndarray:
    """Laplacian pyramid multiband blending of uint8 RGB images `img_a` and `img_b` using float mask `mask_a` in [0,1]."""
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
    for k in range(len(la)):
        mk = gm[max_lvl - k]
        if mk.ndim == 2:
            mk = mk[..., None]
        ls.append(la[k] * mk + lb[k] * (1.0 - mk))

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
) -> np.ndarray:
    """Compute a fast minimum-cost spatial seam mask (Graphcut-inspired DP) at <= 240px and smoothly upsample."""
    h, w = img_a.shape[:2]
    scale = min(1.0, max_dp_dim / float(max(h, w)))
    if scale < 0.99:
        sw = max(24, int(round(w * scale)))
        sh = max(24, int(round(h * scale)))
        sa = cv2.resize(img_a, (sw, sh), interpolation=cv2.INTER_AREA)
        sb = cv2.resize(img_b, (sw, sh), interpolation=cv2.INTER_AREA)
    else:
        sa, sb = img_a, img_b

    diff = np.abs(sa.astype(np.float32) - sb.astype(np.float32)).mean(axis=-1)
    g_a = to_gray_u8(sa)
    g_b = to_gray_u8(sb)
    ga = cv2.Sobel(g_a, cv2.CV_32F, 1, 0, ksize=3) ** 2 + cv2.Sobel(g_a, cv2.CV_32F, 0, 1, ksize=3) ** 2
    gb = cv2.Sobel(g_b, cv2.CV_32F, 1, 0, ksize=3) ** 2 + cv2.Sobel(g_b, cv2.CV_32F, 0, 1, ksize=3) ** 2
    cost = diff + 0.02 * np.abs(np.sqrt(ga) - np.sqrt(gb))

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
    """Stitch `outgoing` -> `incoming` with a DP spatial seam sweeping strictly ALONG dominant motion.

    When flow moves +x (left->right), `incoming` enters from x=0 and sweeps rightward (`blend_pos=alpha_t`)
    so the transition rides WITH the current rather than against it! Same for -x, +y (waterfall), and -y (smoke).
    """
    vertical_seam = abs(dom_vx) >= abs(dom_vy)
    positive_dir = (dom_vx >= 0.0) if vertical_seam else (dom_vy >= 0.0)

    fr = compute_flow_capped(outgoing, incoming, max_width=384)
    # Gate cross-warp strictly to small local correspondences that don't oppose dominant flow
    max_local = max(4.0, min(outgoing.shape[:2]) * 0.045)
    valid = (fr.confidence > 0.35) & (~fr.occluded)
    mag = np.hypot(fr.fwd[..., 0], fr.fwd[..., 1])
    valid = valid & (mag <= max_local * 2.5)
    w_fwd = np.clip(fr.fwd * valid[..., None], -max_local, max_local)
    w_bwd = np.clip(fr.bwd * valid[..., None], -max_local, max_local)

    w_out = warp_frame(outgoing, w_fwd * (alpha_t * 0.55))
    w_in = warp_frame(incoming, w_bwd * ((1.0 - alpha_t) * 0.55))

    if positive_dir:
        # Incoming piece occupies low side (x < seam_x or y < seam_y) and sweeps forward 0 -> 1
        seam_incoming = _optimal_seam_mask(w_in, w_out, blend_pos=alpha_t, vertical=vertical_seam, a_on_low_side=True)
    else:
        # Flow moves negative: incoming piece enters from high side (right/bottom) and sweeps 1 -> 0
        seam_incoming = _optimal_seam_mask(w_in, w_out, blend_pos=1.0 - alpha_t, vertical=vertical_seam, a_on_low_side=False)

    # Outgoing weight mask = 1 - incoming weight (spatial seam dominates -> minimal double exposure!)
    mask_out = np.clip(0.78 * (1.0 - seam_incoming) + 0.22 * (1.0 - alpha_t), 0.0, 1.0)
    stitched = multiband_blend(w_out, w_in, mask_out, levels=4)
    if prot is not None:
        stitched[prot] = ref_frame0[prot]
    return stitched


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
            meta["applied"] = True
            meta["bridgeType"] = "lead_in_content_pieces"
            meta["bridgeFrames"] = k_use
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
            meta["applied"] = True
            meta["bridgeType"] = "lead_out_content_pieces"
            meta["bridgeFrames"] = k_use
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
    if not specs and sentinela_report is not None:
        merged_masks: dict[str, np.ndarray] = {}
        for reg in sentinela_report.get("levelB", {}).get("regions", []):
            beh = reg["behavior"]
            strat = {
                "static": "protect",
                "progressive_flow": "animate",
                "transient": "remove_transient",
                "oscillatory": "fix_seam",
                "photometric": "fix_seam",
            }.get(beh, "fix_seam")
            rmask = _region_mask_from_sentinela(sentinela_report, reg, (h, w))
            if strat not in merged_masks:
                merged_masks[strat] = rmask.copy()
            else:
                merged_masks[strat] |= rmask
        # Process in deterministic order: protect -> remove_transient -> animate -> fix_seam
        for strat in ("protect", "remove_transient", "animate", "fix_seam"):
            if strat in merged_masks and merged_masks[strat].any():
                specs.append(
                    {
                        "id": f"auto_{strat}",
                        "mask": merged_masks[strat],
                        "strategy": strat,
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

    # Level 1: Evaluate baseline first — never unnecessarily reconstruct a clean natural loop!
    base_eval = eval_fn(src, wait, protected_mask=prot_eval)
    if base_eval["passed"] and not region_specs and transient_mask is None and not stabilize_camera:
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

    # Level 5: Anti-ghosting dynamic texture synthesis (dual-phase Eulerian advection at true 1.0x velocity)
    try:
        flow_f, f_stats = estimate_2d_flow_field(src, protected_mask=protected_mask)
        if f_stats["p90SpeedPx"] >= 0.30 and f_stats["temporalCoherence"] >= 0.45:
            h_s, w_s = src.shape[1:3]
            max_safe_span = (0.44 * min(h_s, w_s)) / max(float(f_stats["p90SpeedPx"]), 1.0)
            cyc = float(np.clip(float(n), 4.0, min(28.0, max(4.0, max_safe_span))))
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

    for strat_name, cand_frames, strat_meta in candidates:
        ev = eval_fn(cand_frames, wait, protected_mask=prot_eval, reference_frames=src)
        cmp_res = compare_before_after(base_eval, ev)
        attempts.append(
            {
                "strategy": strat_name,
                "accepted": cmp_res["accepted"],
                "afterCost": cmp_res["afterCompositeCost"],
                "beforeCost": cmp_res["beforeCompositeCost"],
                "hardRejectReasons": cmp_res["hardRejectReasons"],
                "issues": ev["issues"],
                "meta": strat_meta,
            }
        )
        if cmp_res["accepted"] and cmp_res["afterCompositeCost"] < best_cost - 0.02:
            best_cost = cmp_res["afterCompositeCost"]
            best_strategy = strat_name
            best_frames = cand_frames
            best_eval = ev
            best_cmp = cmp_res

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
