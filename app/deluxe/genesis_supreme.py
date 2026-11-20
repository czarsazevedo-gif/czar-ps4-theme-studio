"""Gênesis Supreme — freeform-mask 2D progressive periodic synthesis (strictly unidirectional, zero ping-pong)."""

from __future__ import annotations

import math
import time
from typing import Sequence

import cv2
import numpy as np

from . import DELUXE_VERSION
from .flow import compute_bidirectional_flow, to_gray_u8
from .metrics import evaluate_loop, evaluate_post_dds


class SynthesisRejected(ValueError):
    """Raised honestly when a source cannot produce a convincing unidirectional periodic synthesis."""


def _feather_mask(mask: np.ndarray, feather_px: int = 6, protected_mask: np.ndarray | None = None) -> np.ndarray:
    """Create a smooth [0, 1] float32 alpha mask while keeping protected pixels strictly 0."""
    m = mask.astype(np.float32)
    if feather_px > 0:
        k = int(feather_px) * 2 + 1
        m = cv2.GaussianBlur(m, (k, k), sigmaX=max(1.0, feather_px * 0.45))
        m = np.clip(m, 0.0, 1.0)
    if protected_mask is not None:
        m = np.where(protected_mask.astype(bool), 0.0, m)
    return m.astype(np.float32)


def _align_protected_dxt1(protected_mask: np.ndarray) -> np.ndarray:
    """Expand protected mask to full 4x4 DXT1 blocks so moving neighbors cannot perturb protected block palettes."""
    h, w = protected_mask.shape[:2]
    bh, bw = (h + 3) // 4, (w + 3) // 4
    padded = np.zeros((bh * 4, bw * 4), dtype=bool)
    padded[:h, :w] = protected_mask.astype(bool)
    blk = padded.reshape(bh, 4, bw, 4).any(axis=(1, 3))
    aligned = np.repeat(np.repeat(blk, 4, axis=0), 4, axis=1)[:h, :w]
    return aligned


def estimate_2d_flow_field(
    frames: Sequence[np.ndarray] | np.ndarray,
    *,
    mask: np.ndarray | None = None,
    protected_mask: np.ndarray | None = None,
    flow_max_width: int = 320,
) -> tuple[np.ndarray, dict]:
    """Estimate a steady, confidence-weighted 2D Eulerian velocity field (H, W, 2) from source frames."""
    arr = np.asarray(frames, dtype=np.uint8)
    if arr.ndim != 4 or arr.shape[0] < 2:
        raise SynthesisRejected("Gênesis Supreme requer ao menos 2 quadros para estimar fluxo 2D.")
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    n, h, w, _ = arr.shape

    scale = 1.0
    work = arr
    if w > flow_max_width:
        scale = flow_max_width / float(w)
        nw = max(32, int(round(w * scale / 2)) * 2)
        nh = max(32, int(round(h * scale / 2)) * 2)
        work = np.stack([cv2.resize(f, (nw, nh), interpolation=cv2.INTER_AREA) for f in arr])
        scale = nw / float(w)

    # Sample up to 14 consecutive pairs evenly across the input for speed & robustness
    pair_indices = np.linspace(0, n - 2, min(n - 1, 14), dtype=int)
    flows = []
    confs = []
    for idx in pair_indices:
        fr = compute_bidirectional_flow(work[idx], work[idx + 1])
        flows.append(fr.fwd / max(scale, 1e-6))
        confs.append(fr.confidence * (~fr.occluded).astype(np.float32))

    flow_stack = np.stack(flows, axis=0)  # (P, hs, ws, 2)
    conf_stack = np.stack(confs, axis=0)  # (P, hs, ws)

    # Confidence-weighted robust temporal aggregation (trimmed/weighted mean + median stabilization)
    w_sum = np.maximum(conf_stack.sum(axis=0, keepdims=True), 1e-5)
    w_mean = (flow_stack * conf_stack[..., None]).sum(axis=0) / w_sum[0, ..., None]
    med_flow = np.median(flow_stack, axis=0)
    steady = (0.55 * med_flow + 0.45 * w_mean).astype(np.float32)

    if steady.shape[:2] != (h, w):
        u_up = cv2.resize(steady[..., 0], (w, h), interpolation=cv2.INTER_LINEAR)
        v_up = cv2.resize(steady[..., 1], (w, h), interpolation=cv2.INTER_LINEAR)
        steady = np.stack([u_up, v_up], axis=-1)

    # Smooth spatial flow field for coherent Eulerian advection
    ksize = max(5, (min(h, w) // 16) | 1)
    steady[..., 0] = cv2.GaussianBlur(steady[..., 0], (ksize, ksize), 0)
    steady[..., 1] = cv2.GaussianBlur(steady[..., 1], (ksize, ksize), 0)

    # Apply mask & smoothly taper near protected boundaries so static elements never tear
    active_mask = np.ones((h, w), dtype=np.float32) if mask is None else mask.astype(np.float32)
    if protected_mask is not None:
        active_mask = np.where(protected_mask.astype(bool), 0.0, active_mask)
    soft_taper = cv2.GaussianBlur(active_mask, (9, 9), 2.5)
    if protected_mask is not None:
        soft_taper = np.where(protected_mask.astype(bool), 0.0, soft_taper)
    steady *= soft_taper[..., None]

    # Measure directional coherence and speed inside active region
    sel = active_mask > 0.5
    if not sel.any():
        raise SynthesisRejected("A máscara de síntese não contém pixels ativos.")
    u_s = steady[..., 0][sel]
    v_s = steady[..., 1][sel]
    mags = np.hypot(u_s, v_s)
    mean_speed = float(mags.mean())
    p90_speed = float(np.percentile(mags, 90))
    coherence = float(np.hypot(u_s.sum(), v_s.sum()) / max(float(mags.sum()), 1e-6))

    # Check temporal consistency across pairs (reject ping-pong or chaotic jumps as steady flow!)
    pair_vecs = np.array(
        [
            [float(f[..., 0].mean()), float(f[..., 1].mean())]
            for f in flow_stack
        ],
        dtype=np.float32,
    )
    pv_mags = np.hypot(pair_vecs[:, 0], pair_vecs[:, 1])
    temp_coh = float(np.hypot(pair_vecs[:, 0].sum(), pair_vecs[:, 1].sum()) / max(float(pv_mags.sum()), 1e-6))

    stats = {
        "meanSpeedPx": round(mean_speed, 4),
        "p90SpeedPx": round(p90_speed, 4),
        "spatialCoherence": round(coherence, 4),
        "temporalCoherence": round(temp_coh, 4),
        "meanVec": [round(float(u_s.mean()), 4), round(float(v_s.mean()), 4)],
        "activeFraction": round(float(sel.mean()), 4),
    }
    return steady, stats


def _semi_lagrangian_advect(
    image: np.ndarray,
    flow_field: np.ndarray,
    steps: float,
    *,
    substeps: int = 4,
    wrap: bool = False,
) -> np.ndarray:
    """Integrate backward semi-Lagrangian particle paths along `flow_field` for `steps` forward steps.

    Backward tracing (x_{k-1} = x_k - dt * V(x_k)) transports texture strictly FORWARD along +V!
    """
    h, w = image.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    if abs(steps) < 1e-6:
        return image.copy()

    n_sub = max(1, int(math.ceil(abs(steps) * 0.5)), substeps)
    dt = float(steps) / float(n_sub)
    px = xx.copy()
    py = yy.copy()
    u_field = flow_field[..., 0].astype(np.float32, copy=False)
    v_field = flow_field[..., 1].astype(np.float32, copy=False)

    for _ in range(n_sub):
        sx = np.mod(px, w) if wrap else np.clip(px, 0.0, w - 1.0)
        sy = np.mod(py, h) if wrap else np.clip(py, 0.0, h - 1.0)
        vx = cv2.remap(u_field, sx, sy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        vy = cv2.remap(v_field, sx, sy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        px -= dt * vx
        py -= dt * vy

    if wrap:
        px = np.mod(px, w).astype(np.float32)
        py = np.mod(py, h).astype(np.float32)
        border = cv2.BORDER_WRAP
    else:
        px = np.clip(px, 0.0, w - 1.0).astype(np.float32)
        py = np.clip(py, 0.0, h - 1.0).astype(np.float32)
        border = cv2.BORDER_REFLECT_101

    return cv2.remap(image, px, py, interpolation=cv2.INTER_CUBIC, borderMode=border)


def _contrast_preserving_dual_blend(img_a: np.ndarray, img_b: np.ndarray, w_a: float) -> np.ndarray:
    """Blend two advected texture layers without mid-transition contrast washout (anti-ghosting).

    Uses a 2-band decomposition:
    - Low-frequency color/lighting blends linearly via (w_a, w_b).
    - High-frequency detail is variance-normalized by 1/sqrt(w_a^2 + w_b^2) clamped gently
      to preserve crisp ripples/clouds without amplifying noise!
    """
    w_b = 1.0 - w_a
    if w_a <= 1e-4:
        return img_b.copy()
    if w_b <= 1e-4:
        return img_a.copy()

    fa = img_a.astype(np.float32)
    fb = img_b.astype(np.float32)
    low_a = cv2.GaussianBlur(fa, (9, 9), 2.2)
    low_b = cv2.GaussianBlur(fb, (9, 9), 2.2)
    high_a = fa - low_a
    high_b = fb - low_b

    low_blend = w_a * low_a + w_b * low_b
    # Variance-preserving detail gain (capped at 1.32 to avoid overshoot/ringing before DXT1a)
    norm_gain = min(1.32, 1.0 / math.sqrt(w_a * w_a + w_b * w_b))
    high_blend = (w_a * high_a + w_b * high_b) * norm_gain
    return np.clip(np.rint(low_blend + high_blend), 0, 255).astype(np.uint8)


def _detect_mask_spatial_period(base_frame: np.ndarray, flow_field: np.ndarray, mask: np.ndarray) -> tuple[int, int] | None:
    """Check if the masked region has clean 1D/2D spatial periodicity along its dominant motion direction."""
    ys, xs = np.where(mask)
    if len(ys) < 64:
        return None
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    roi = base_frame[y0:y1, x0:x1].astype(np.float32)
    u_mean = float(flow_field[..., 0][mask].mean())
    v_mean = float(flow_field[..., 1][mask].mean())
    rh, rw = roi.shape[:2]

    # Horizontal dominant check
    if abs(u_mean) >= abs(v_mean) * 2.0 and rw >= 24:
        for p in range(12, rw // 2 + 1):
            err = float(np.abs(roi[:, p:] - roi[:, :-p]).mean())
            if err < 2.2:
                return (p, 0)
    # Vertical dominant check
    if abs(v_mean) >= abs(u_mean) * 2.0 and rh >= 24:
        for p in range(12, rh // 2 + 1):
            err = float(np.abs(roi[p:, :] - roi[:-p, :]).mean())
            if err < 2.2:
                return (0, p)
    return None


def synthesize_periodic_2d(
    base_frame: np.ndarray,
    flow_field: np.ndarray,
    n_frames: int,
    *,
    base_frame_b: np.ndarray | None = None,
    mask: np.ndarray | None = None,
    protected_mask: np.ndarray | None = None,
    speed_scale: float = 1.0,
    cycle_steps: float | None = None,
    feather_px: int = 6,
    align_dxt1_blocks: bool = True,
) -> np.ndarray:
    """Synthesize an N-frame strictly unidirectional infinite loop using dual-phase Eulerian advection.

    Uses two distinct phase textures (`base_frame` and `base_frame_b`) so the full N-frame cycle
    produces N distinct DDS frames (never collapsing to an N/2 half-cycle duplicate!).
    """
    base = np.asarray(base_frame, dtype=np.uint8)
    if base.shape[-1] == 4:
        base = base[..., :3]
    h, w, _ = base.shape

    active = np.ones((h, w), dtype=bool) if mask is None else mask.astype(bool)
    prot = None
    if protected_mask is not None:
        prot = _align_protected_dxt1(protected_mask) if align_dxt1_blocks else protected_mask.astype(bool)
        active = active & (~prot)
    elif mask is not None:
        prot = ~active
        if align_dxt1_blocks:
            prot = _align_protected_dxt1(prot)
            active = ~prot

    alpha = _feather_mask(active, feather_px=feather_px, protected_mask=prot)
    alpha_3 = alpha[..., None]

    # Cycle advection span: scales naturally with N and speed_scale, bounded to avoid excessive stretch
    span = float(cycle_steps) if cycle_steps is not None else max(4.0, min(float(n_frames) * 0.65, 16.0)) * float(speed_scale)
    scaled_flow = flow_field.astype(np.float32) * float(speed_scale)

    # Second phase reference texture: use real distinct source frame if provided, else forward-advected offset
    if base_frame_b is not None:
        base_b = np.asarray(base_frame_b, dtype=np.uint8)
        if base_b.shape[-1] == 4:
            base_b = base_b[..., :3]
    else:
        base_b = _semi_lagrangian_advect(base, scaled_flow, 0.37 * span, wrap=False)

    # Check if exact spatial periodicity exists inside mask for crystal-sharp zero-blend periodic wrap
    period_xy = _detect_mask_spatial_period(base, scaled_flow, active)
    use_exact_wrap = False
    if period_xy is not None:
        px, py = period_xy
        u_std = float(scaled_flow[..., 0][active].std()) if active.any() else 99.0
        v_std = float(scaled_flow[..., 1][active].std()) if active.any() else 99.0
        if px > 0 and u_std < 0.35 and abs(float(scaled_flow[..., 1][active].mean())) < 0.2:
            use_exact_wrap = True
            u_sign = 1.0 if float(scaled_flow[..., 0][active].mean()) >= 0 else -1.0
            nat_travel = abs(float(scaled_flow[..., 0][active].mean())) * n_frames
            k_cycles = max(1, int(round(nat_travel / px)))
            wrap_step = (u_sign * k_cycles * px) / float(n_frames)
        elif py > 0 and v_std < 0.35 and abs(float(scaled_flow[..., 0][active].mean())) < 0.2:
            use_exact_wrap = True
            v_sign = 1.0 if float(scaled_flow[..., 1][active].mean()) >= 0 else -1.0
            nat_travel = abs(float(scaled_flow[..., 1][active].mean())) * n_frames
            k_cycles = max(1, int(round(nat_travel / py)))
            wrap_step = (v_sign * k_cycles * py) / float(n_frames)

    out_frames = []
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    for i in range(n_frames):
        if use_exact_wrap:
            if period_xy[0] > 0:
                mx = np.mod(xx - i * wrap_step, w).astype(np.float32)
                my = yy
            else:
                mx = xx
                my = np.mod(yy - i * wrap_step, h).astype(np.float32)
            dyn = cv2.remap(base, mx, my, interpolation=cv2.INTER_CUBIC, borderMode=cv2.BORDER_WRAP)
        else:
            # Dual-phase Eulerian advection with two distinct source layers (base_a, base_b):
            # Both advance STRICTLY FORWARD along +flow_field and produce N distinct frames!
            phi_a = i / float(n_frames)
            phi_b = (phi_a + 0.5) % 1.0
            t_a = (phi_a - 0.5) * span
            t_b = (phi_b - 0.5) * span
            w_a = 0.5 * (1.0 - math.cos(2.0 * math.pi * phi_a))
            adv_a = _semi_lagrangian_advect(base, scaled_flow, t_a, wrap=False)
            adv_b = _semi_lagrangian_advect(base_b, scaled_flow, t_b, wrap=False)
            dyn = _contrast_preserving_dual_blend(adv_a, adv_b, w_a)

        comp = np.clip(np.rint(dyn.astype(np.float32) * alpha_3 + base.astype(np.float32) * (1.0 - alpha_3)), 0, 255).astype(np.uint8)
        if prot is not None:
            comp[prot] = base[prot]
        out_frames.append(comp)

    return np.stack(out_frames)


def synthesize_genesis_supreme(
    frames: Sequence[np.ndarray] | np.ndarray,
    n_frames: int,
    wait: float,
    *,
    mask: np.ndarray | None = None,
    protected_mask: np.ndarray | None = None,
    flow_override: np.ndarray | None = None,
    speed_scale: float = 1.0,
    feather_px: int = 6,
    verify_dds: bool = True,
) -> dict:
    """Full Gênesis Supreme synthesis pipeline with automatic flow estimation & post-DDS verification."""
    t0 = time.perf_counter()
    arr = np.asarray(frames, dtype=np.uint8)
    if arr.ndim != 4 or arr.shape[0] < 2:
        raise SynthesisRejected("São necessários ao menos 2 quadros de origem.")
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    base = arr[0]
    base_b = arr[len(arr) // 2] if len(arr) >= 3 else None

    if flow_override is not None:
        flow_field = flow_override.astype(np.float32)
        sel = np.ones(base.shape[:2], dtype=bool) if mask is None else mask.astype(bool)
        if protected_mask is not None:
            sel = sel & (~protected_mask.astype(bool))
        mags = np.hypot(flow_field[..., 0][sel], flow_field[..., 1][sel]) if sel.any() else np.array([0.0])
        flow_stats = {
            "meanSpeedPx": round(float(mags.mean()), 4),
            "p90SpeedPx": round(float(np.percentile(mags, 90)), 4),
            "spatialCoherence": 1.0,
            "temporalCoherence": 1.0,
            "meanVec": [
                round(float(flow_field[..., 0][sel].mean()) if sel.any() else 0.0, 4),
                round(float(flow_field[..., 1][sel].mean()) if sel.any() else 0.0, 4),
            ],
            "activeFraction": round(float(sel.mean()), 4),
        }
    else:
        flow_field, flow_stats = estimate_2d_flow_field(arr, mask=mask, protected_mask=protected_mask)
        if flow_stats["p90SpeedPx"] < 0.25:
            raise SynthesisRejected("Movimento insuficiente na região selecionada para síntese progressiva.")
        if flow_stats["temporalCoherence"] < 0.35:
            raise SynthesisRejected("A fonte apresenta reversão ou movimento incoerente para síntese unidirecional.")

    # Pick a clean cycle_steps that produces smooth, non-duplicate steps without excessive warping
    mean_sp = max(flow_stats["meanSpeedPx"], 0.35)
    target_step_px = max(1.0, min(mean_sp * 1.15, 6.0))
    cycle_steps = float(np.clip((target_step_px * n_frames * 0.55) / max(mean_sp, 0.25), 3.0, 14.0))

    synthesized = synthesize_periodic_2d(
        base,
        flow_field,
        n_frames,
        base_frame_b=base_b,
        mask=mask,
        protected_mask=protected_mask,
        speed_scale=speed_scale,
        cycle_steps=cycle_steps,
        feather_px=feather_px,
        align_dxt1_blocks=True,
    )

    prot_eval = _align_protected_dxt1(protected_mask) if protected_mask is not None else None
    if verify_dds:
        evaluation = evaluate_post_dds(synthesized, wait, protected_mask=prot_eval, reference_frames=arr[: min(len(arr), n_frames)])
    else:
        evaluation = evaluate_loop(synthesized, wait, protected_mask=prot_eval, reference_frames=arr[: min(len(arr), n_frames)])

    # Strict self-verification: reject if reversals or duplicates occurred
    if evaluation["metrics"]["reversals"]["detected"]:
        raise SynthesisRejected("A síntese falhou na verificação de progressão unidirecional (reversão detectada).")
    if evaluation["metrics"]["freezes"]["exactDuplicates"]:
        raise SynthesisRejected("A síntese produziu quadros duplicados.")

    return {
        "version": DELUXE_VERSION,
        "strategy": "genesis_supreme_2d",
        "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
        "frames": synthesized,
        "frameCount": int(n_frames),
        "wait": float(wait),
        "flowStats": flow_stats,
        "evaluation": evaluation,
    }
