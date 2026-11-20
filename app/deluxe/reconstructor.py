"""Ouroboros Supreme — 6-level escalating loop reconstruction engine with content-piece transition bridging."""

from __future__ import annotations

import math
import time
from typing import Sequence

import cv2
import numpy as np

from . import DELUXE_VERSION
from .flow import compute_bidirectional_flow, estimate_camera_motion, to_gray_u8, warp_frame
from .genesis_supreme import SynthesisRejected, _align_protected_dxt1, estimate_2d_flow_field, synthesize_periodic_2d
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


def _optimal_seam_mask(img_a: np.ndarray, img_b: np.ndarray, blend_pos: float, vertical: bool = True) -> np.ndarray:
    """Compute a dynamic-programming minimum-cost spatial seam mask between two content pieces (Graphcut-inspired)."""
    h, w = img_a.shape[:2]
    diff = np.abs(img_a.astype(np.float32) - img_b.astype(np.float32)).mean(axis=-1)
    # Also include gradient mismatch so seams avoid cutting across sharp object edges
    ga = cv2.Sobel(to_gray_u8(img_a), cv2.CV_32F, 1, 0, ksize=3) ** 2 + cv2.Sobel(to_gray_u8(img_a), cv2.CV_32F, 0, 1, ksize=3) ** 2
    gb = cv2.Sobel(to_gray_u8(img_b), cv2.CV_32F, 1, 0, ksize=3) ** 2 + cv2.Sobel(to_gray_u8(img_b), cv2.CV_32F, 0, 1, ksize=3) ** 2
    cost = diff + 0.02 * np.abs(np.sqrt(ga) - np.sqrt(gb))

    if not vertical:
        cost = cost.T
    ch, cw = cost.shape

    # Constrain seam near blend_pos with a gentle quadratic guide so the seam sweeps smoothly across time
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
    mask = (grid_x < seam_x[:, None]).astype(np.float32)
    if not vertical:
        mask = mask.T
    return mask


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

    # Expected natural step between adjacent frames vs observed wrap jump
    # Remove the secular linear trend from frame 0 to frame N-1 so frame N-1 aligns seamlessly to frame 0
    end_to_start_diff = means[-1] - means[0]
    std_ratio_end = np.clip(stds[0] / stds[-1], 0.85, 1.18)

    out = np.empty_like(rgb)
    # Also compute coarse spatial luminance drift map (4x4) for localized lighting gradients
    gh, gw = spatial_grid
    small_0 = cv2.resize(labs[0, ..., 0], (gw, gh), interpolation=cv2.INTER_AREA)
    small_end = cv2.resize(labs[-1, ..., 0], (gw, gh), interpolation=cv2.INTER_AREA)
    spatial_drift = cv2.resize(small_end - small_0, (w, h), interpolation=cv2.INTER_LINEAR)
    # Remove global mean already handled globally
    spatial_drift -= float(spatial_drift.mean())
    # Clamp localized correction so we never over-distort legitimate spatial motion
    spatial_drift = np.clip(spatial_drift, -14.0, 14.0)

    prot = None if protected_mask is None else _align_protected_dxt1(protected_mask)

    for i in range(n):
        # Centered ramp: -0.5 at i=0 -> +0.5 at i=N-1 (spreads correction symmetrically, minimizing max distortion!)
        # Actually pinning frame 0 keeps exact compatibility with reference frame 0 while distributing over 0..N-1:
        # Ramp s in [0, 1] with smoothstep-free linear progression so derivative dL/dt stays constant across seam!
        s = i / float(n)
        corrected = labs[i].copy()
        # Global mean alignment (subtracts secular linear drift)
        corrected -= (s * end_to_start_diff)[None, None, :]
        # Gentle contrast/std alignment on L* channel only
        c_scale = 1.0 + s * (float(std_ratio_end[0]) - 1.0)
        l_mean = float(corrected[..., 0].mean())
        corrected[..., 0] = (corrected[..., 0] - l_mean) * c_scale + l_mean
        # Gentle localized lighting ramp correction (capped)
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
    """Align boundary motion across the seam using confidence- & occlusion-gated bidirectional optical flow."""
    rgb = np.asarray(frames, dtype=np.uint8)
    n, h, w, _ = rgb.shape
    if n < 6:
        return rgb.copy(), {"applied": False, "reason": "Sequência curta demais para compensação de fluxo."}

    # First apply gentle photometric harmonization so optical flow isn't fooled by brightness drift
    harm = level2_photometric_harmonize(rgb, protected_mask=protected_mask)
    fr = compute_bidirectional_flow(harm[-1], harm[0])

    # Check if correspondence is trustworthy enough to warp!
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

    # Gate & smooth the boundary residual flow so low-confidence / occluded zones are NEVER distorted
    gate = (fr.confidence * (~fr.occluded).astype(np.float32))
    gate = np.where(gate >= min_confidence, gate, 0.0).astype(np.float32)
    if prot is not None:
        gate[prot] = 0.0
    gate = cv2.GaussianBlur(gate, (11, 11), 3.0)
    if prot is not None:
        gate[prot] = 0.0

    # Clamp maximum warp magnitude to avoid extreme geometric stretching
    max_warp = max(6.0, min(h, w) * 0.08)
    dx = np.clip(fr.fwd[..., 0] * gate, -max_warp, max_warp)
    dy = np.clip(fr.fwd[..., 1] * gate, -max_warp, max_warp)
    # Subtract the expected single-step forward displacement (so we align F[N-1] to ONE step before F[0],
    # NOT identical to F[0] — avoiding a freeze at the seam!)
    fr_step = compute_bidirectional_flow(harm[-2], harm[-1])
    step_dx = fr_step.fwd[..., 0] * gate
    step_dy = fr_step.fwd[..., 1] * gate
    # Residual alignment needed so F[N-1] sits one natural step behind F[0]
    res_flow = np.stack([dx - step_dx, dy - step_dy], axis=-1).astype(np.float32)
    res_flow[..., 0] = cv2.GaussianBlur(res_flow[..., 0], (9, 9), 2.0)
    res_flow[..., 1] = cv2.GaussianBlur(res_flow[..., 1], (9, 9), 2.0)
    if prot is not None:
        res_flow[prot] = 0.0

    win = window if window is not None else min(5, max(2, n // 3))
    out = harm.copy()
    for k in range(win):
        idx = n - win + k
        t = (k + 1) / float(win)
        # Smooth cubic ease-in across the tail window
        weight = t * t * (3.0 - 2.0 * t)
        warped = warp_frame(harm[idx], res_flow * weight, border_mode=cv2.BORDER_REFLECT_101)
        # Blend warped pixels only where gate is active; keep original elsewhere
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

    # Stabilize towards reference frame 0 while keeping border fill clean (reflect + subtle zoom if small)
    out = np.empty_like(rgb)
    out[0] = rgb[0]
    cx, cy = w * 0.5, h * 0.5
    for i in range(1, n):
        m = cv2.getRotationMatrix2D((cx, cy), -cum_rot[i], 1.0)
        m[0, 2] -= cum_tx[i]
        m[1, 2] -= cum_ty[i]
        # BORDER_REFLECT_101 prevents any black empty borders
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

    # Temporal median across all frames cleanly recovers background wherever object was transient
    median_plate = np.median(rgb, axis=0).astype(np.uint8)
    # Also compute an inpainted fallback for pixels where object lingered >50% of frames
    mask_u8 = (m_bool.astype(np.uint8) * 255)
    dilated = cv2.dilate(mask_u8, np.ones((5, 5), np.uint8), iterations=1)
    inpainted_plate = cv2.inpaint(median_plate, dilated, inpaintRadius=4, flags=cv2.INPAINT_TELEA)

    # Decide per-pixel whether median plate differs strongly from surrounding border (object lingered)
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


def level4_content_bridge_reconstruct(
    frames: Sequence[np.ndarray] | np.ndarray,
    *,
    lead_in: Sequence[np.ndarray] | np.ndarray | None = None,
    lead_out: Sequence[np.ndarray] | np.ndarray | None = None,
    protected_mask: np.ndarray | None = None,
    stabilize_camera: bool = False,
    transient_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, dict]:
    """Reconstruct a seamless unidirectional loop using content-piece transition bridging + multiband/seam stitching.

    When `lead_in` (frames immediately BEFORE frame 0) or `lead_out` (frames immediately AFTER frame N-1)
    are available from the source recording, builds real key transition bridge pieces directly from the
    source's own content, flow-warped along the forward direction and stitched via optimal DP seam +
    Laplacian multiband blending (preventing crossfade ghosting and preserving one-way flow).
    """
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

    # Determine content bridge source pieces:
    # Ideal case: `lead_in` contains K real source frames immediately preceding work[0]
    # (so lead_in[-1] naturally flows forward into work[0]!).
    # Alternate case: `lead_out` contains K real source frames immediately following work[-1]
    # (so work[-1] naturally flows forward into lead_out[0]!).
    # Fallback when neither extra buffer is supplied: use the tail/head overlap inside `work` itself
    # paired with forward flow-aligned warping.
    bridge_k = min(6, max(2, n // 3))
    out = work.copy()

    if lead_in is not None and len(lead_in) >= 2:
        li = np.asarray(lead_in, dtype=np.uint8)
        if li.shape[-1] == 4:
            li = li[..., :3]
        k_use = min(bridge_k, len(li), n - 2)
        li_tail = li[-k_use:]
        # Estimate dominant horizontal vs vertical flow direction to orient the optimal spatial seam
        fr_dom = compute_bidirectional_flow(work[0], work[1])
        vertical_seam = abs(float(fr_dom.fwd[..., 0].mean())) >= abs(float(fr_dom.fwd[..., 1].mean()))

        for j in range(k_use):
            idx = n - k_use + j
            alpha_t = (j + 1) / float(k_use + 1)  # strictly progressive 0 -> 1 toward head piece!
            piece_tail = work[idx]
            piece_head = li_tail[j]
            # Bidirectional flow align piece_tail <-> piece_head to eliminate double-edge ghosting
            fr = compute_bidirectional_flow(piece_tail, piece_head)
            valid = (fr.confidence > 0.30) & (~fr.occluded)
            w_tail = warp_frame(piece_tail, fr.fwd * (alpha_t * 0.75 * valid[..., None]))
            w_head = warp_frame(piece_head, fr.bwd * ((1.0 - alpha_t) * 0.75 * valid[..., None]))
            # Optimal DP spatial seam sweeping progressively across the frame + multiband pyramid stitch
            seam_mask = _optimal_seam_mask(w_tail, w_head, blend_pos=1.0 - alpha_t, vertical=vertical_seam)
            # Combine spatial seam with gentle global temporal weighting so transition is imperceptible
            combined_mask = np.clip(0.65 * seam_mask + 0.35 * (1.0 - alpha_t), 0.0, 1.0)
            stitched = multiband_blend(w_tail, w_head, combined_mask, levels=4)
            if prot is not None:
                stitched[prot] = work[0][prot]
            out[idx] = stitched
        meta["applied"] = True
        meta["bridgeType"] = "lead_in_content_pieces"
        meta["bridgeFrames"] = k_use
        return out, meta

    if lead_out is not None and len(lead_out) >= 2:
        lo = np.asarray(lead_out, dtype=np.uint8)
        if lo.shape[-1] == 4:
            lo = lo[..., :3]
        k_use = min(bridge_k, len(lo), n - 2)
        lo_head = lo[:k_use]
        fr_dom = compute_bidirectional_flow(work[0], work[1])
        vertical_seam = abs(float(fr_dom.fwd[..., 0].mean())) >= abs(float(fr_dom.fwd[..., 1].mean()))

        for j in range(k_use):
            # Blend outgoing continuation piece into the opening k_use frames so work[-1] -> out[0] is seamless!
            alpha_t = (j + 1) / float(k_use + 1)  # 0 (mostly lo_head) -> 1 (mostly work[j])
            piece_out = lo_head[j]
            piece_in = work[j]
            fr = compute_bidirectional_flow(piece_out, piece_in)
            valid = (fr.confidence > 0.30) & (~fr.occluded)
            w_out = warp_frame(piece_out, fr.fwd * (alpha_t * 0.75 * valid[..., None]))
            w_in = warp_frame(piece_in, fr.bwd * ((1.0 - alpha_t) * 0.75 * valid[..., None]))
            seam_mask = _optimal_seam_mask(w_out, w_in, blend_pos=1.0 - alpha_t, vertical=vertical_seam)
            combined_mask = np.clip(0.65 * seam_mask + 0.35 * (1.0 - alpha_t), 0.0, 1.0)
            stitched = multiband_blend(w_out, w_in, combined_mask, levels=4)
            if prot is not None:
                stitched[prot] = work[0][prot]
            out[j] = stitched
        meta["applied"] = True
        meta["bridgeType"] = "lead_out_content_pieces"
        meta["bridgeFrames"] = k_use
        return out, meta

    # Self-contained bridge when no extra lead_in/lead_out exists: flow-guided seam compensation + multiband
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

    Each item in `region_specs` may specify:
      - `id`: str
      - `mask`: bool ndarray (H, W)
      - `strategy`: "protect" | "animate" | "remove_transient" | "fix_seam" | "adjust_motion"
      - `speedScale`: float (default 1.0)
      - `directionXY`: optional [dx, dy] override
      - `featherPx`: int (default 6)
    """
    rgb = np.asarray(frames, dtype=np.uint8)
    if rgb.shape[-1] == 4:
        rgb = rgb[..., :3]
    n, h, w, _ = rgb.shape

    specs = list(region_specs or [])
    if not specs and sentinela_report is not None:
        # Auto-build regional plan from Sentinela Level B
        for reg in sentinela_report.get("levelB", {}).get("regions", []):
            beh = reg["behavior"]
            strat = {
                "static": "protect",
                "progressive_flow": "animate",
                "transient": "remove_transient",
                "oscillatory": "fix_seam",
                "photometric": "fix_seam",
            }.get(beh, "fix_seam")
            specs.append(
                {
                    "id": reg["id"],
                    "mask": _region_mask_from_sentinela(sentinela_report, reg, (h, w)),
                    "strategy": strat,
                    "speedScale": 1.0,
                    "featherPx": 6,
                }
            )

    if not specs:
        return rgb.copy(), {"applied": False, "regionsProcessed": 0}

    # Combine all protected regions into a master static protection mask (aligned to 4x4 DXT1 blocks!)
    protected_master = np.zeros((h, w), dtype=bool)
    for sp in specs:
        if sp.get("strategy") == "protect":
            m = np.asarray(sp["mask"], dtype=bool)
            if m.shape != (h, w):
                m = cv2.resize(m.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
            protected_master |= m
    protected_aligned = _align_protected_dxt1(protected_master) if protected_master.any() else None

    # Base canvas starts with photometrically harmonized frames (and clean protected frame 0)
    canvas = level2_photometric_harmonize(rgb, protected_mask=protected_aligned)
    base_plate = rgb[0].copy()

    # Estimate global 2D flow field once if any region uses "animate" or "adjust_motion"
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
            # Applied strictly at the end so nothing can overwrite protected pixels
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
            elif global_flow is not None:
                flow_reg = global_flow.copy()
            else:
                continue
            # Ensure motion stays strictly inside this region
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

    # Final hard lock of protected static regions
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

    # Level 3: Bidirectional optical flow seam compensation
    l3, l3_meta = level3_motion_compensated_seam(src, protected_mask=protected_mask)
    if l3_meta.get("applied"):
        candidates.append(("level3_motion_compensated_seam", l3, l3_meta))

    # Level 4: Content-piece transition bridge / camera stab / transient removal
    if lead_in is not None or lead_out is not None or stabilize_camera or transient_mask is not None:
        l4, l4_meta = level4_content_bridge_reconstruct(
            src,
            lead_in=lead_in,
            lead_out=lead_out,
            protected_mask=protected_mask,
            stabilize_camera=stabilize_camera,
            transient_mask=transient_mask,
        )
        if l4_meta.get("applied") or l4_meta.get("stabilized") or l4_meta.get("transientRemoved"):
            candidates.append(("level4_content_bridge", l4, l4_meta))

    # Level 5: Dynamic texture synthesis (dual-phase Eulerian advection)
    try:
        flow_f, f_stats = estimate_2d_flow_field(src, protected_mask=protected_mask)
        if f_stats["p90SpeedPx"] >= 0.30 and f_stats["temporalCoherence"] >= 0.45:
            mean_sp = max(f_stats["meanSpeedPx"], 0.35)
            cyc = float(np.clip((max(1.0, min(mean_sp * 1.15, 6.0)) * n * 0.55) / mean_sp, 3.0, 14.0))
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
