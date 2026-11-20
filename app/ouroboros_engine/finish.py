"""Ouroboros Deluxe Finish — cinematic post-processing with mandatory post-DDS DXT1a verification gate."""

from __future__ import annotations

import time
from typing import Sequence

import cv2
import numpy as np

from . import OUROBOROS_VERSION as DELUXE_VERSION
from .synthesis import _align_protected_dxt1
from .metrics import compare_before_after, evaluate_loop, evaluate_post_dds
from .reconstruction import level2_photometric_harmonize

# 4x4 Bayer ordered dither matrix normalized to [-0.5, +0.5] (temporally static -> ZERO flicker!)
_BAYER_4X4 = (
    np.array(
        [
            [0, 8, 2, 10],
            [12, 4, 14, 6],
            [3, 11, 1, 9],
            [15, 7, 13, 5],
        ],
        dtype=np.float32,
    )
    / 16.0
    - 0.5
)


def _srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    x = rgb.astype(np.float32) / 255.0
    return np.where(x <= 0.04045, x / 12.92, np.power(np.maximum(x + 0.055, 0.0) / 1.055, 2.4))


def _linear_to_srgb(lin: np.ndarray) -> np.ndarray:
    x = np.clip(lin, 0.0, 1.0)
    srgb = np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(np.maximum(x, 1e-9), 1.0 / 2.4) - 0.055)
    return np.clip(np.rint(srgb * 255.0), 0, 255).astype(np.uint8)


def suppress_artificial_flicker(
    frames: np.ndarray,
    *,
    protected_mask: np.ndarray | None = None,
    strength: float = 0.65,
) -> np.ndarray:
    """Selectively damp high-frequency frame-to-frame luminance/exposure oscillation in linear light & Lab."""
    n, h, w, _ = frames.shape
    labs = np.stack([cv2.cvtColor(f, cv2.COLOR_RGB2LAB).astype(np.float32) for f in frames])
    L_means = labs[..., 0].mean(axis=(1, 2))  # (N,)
    # Circular 3-tap binomial smooth [0.25, 0.5, 0.25] removes alternating Nyquist flicker cleanly!
    smoothed = 0.25 * np.roll(L_means, 1) + 0.50 * L_means + 0.25 * np.roll(L_means, -1)
    # Apply second gentle pass for strong alternating flicker
    smoothed = 0.25 * np.roll(smoothed, 1) + 0.50 * smoothed + 0.25 * np.roll(smoothed, -1)
    delta_L = (smoothed - L_means) * float(np.clip(strength, 0.0, 1.0))

    # Also damp regional 4x4 coarse flicker
    small_L = np.stack([cv2.resize(labs[i, ..., 0], (4, 4), interpolation=cv2.INTER_AREA) for i in range(n)])
    small_sm = 0.25 * np.roll(small_L, 1, axis=0) + 0.50 * small_L + 0.25 * np.roll(small_L, -1, axis=0)
    small_delta = (small_sm - small_L) - (smoothed - L_means)[:, None, None]

    prot = None if protected_mask is None else _align_protected_dxt1(protected_mask)
    out = np.empty_like(frames)
    for i in range(n):
        c = labs[i].copy()
        reg_corr = cv2.resize(small_delta[i], (w, h), interpolation=cv2.INTER_LINEAR) * (0.45 * strength)
        c[..., 0] = np.clip(c[..., 0] + delta_L[i] + reg_corr, 0.0, 255.0)
        rgb_i = cv2.cvtColor(c.astype(np.uint8), cv2.COLOR_LAB2RGB)
        if prot is not None:
            rgb_i[prot] = frames[0][prot]
        out[i] = rgb_i
    return out


def preserve_detail_unsharp(
    frames: np.ndarray,
    *,
    amount: float = 0.28,
    sigma: float = 1.0,
    protected_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Gentle edge-aware detail restoration (unsharp mask on Lab L* only — avoids color halos!)."""
    prot = None if protected_mask is None else _align_protected_dxt1(protected_mask)
    out = np.empty_like(frames)
    for i, f in enumerate(frames):
        lab = cv2.cvtColor(f, cv2.COLOR_RGB2LAB).astype(np.float32)
        L = lab[..., 0]
        blur = cv2.GaussianBlur(L, (0, 0), sigmaX=sigma)
        detail = L - blur
        # Soft-clamp detail impulse to prevent haloing on high-contrast edges
        detail = np.clip(detail, -18.0, 18.0)
        lab[..., 0] = np.clip(L + detail * float(amount), 0.0, 255.0)
        rgb_i = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2RGB)
        if prot is not None:
            rgb_i[prot] = frames[0][prot]
        out[i] = rgb_i
    return out


def apply_stable_gradient_dither(
    frames: np.ndarray,
    *,
    amplitude: float = 4.5,
    protected_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Apply temporally-static 4x4 Bayer ordered dither ONLY in smooth gradient zones to break DXT1a banding.

    Because the Bayer pattern is identical across every frame (spatially locked), it introduces
    ZERO temporal shimmer/flicker while helping DXT1a RGB565 endpoints dither across smooth sky ramps.
    """
    n, h, w, _ = frames.shape
    reps_y = (h + 3) // 4
    reps_x = (w + 3) // 4
    bayer = np.tile(_BAYER_4X4, (reps_y, reps_x))[:h, :w]
    prot = None if protected_mask is None else _align_protected_dxt1(protected_mask)

    out = np.empty_like(frames)
    for i, f in enumerate(frames):
        gray = cv2.cvtColor(f, cv2.COLOR_RGB2GRAY).astype(np.float32)
        smooth = cv2.GaussianBlur(gray, (9, 9), 2.0)
        gx = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3) / 8.0
        gy = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3) / 8.0
        grad = np.hypot(gx, gy)
        # Target smooth gradient zones only (avoid flat black/white and sharp textures)
        zone = ((grad >= 0.12) & (grad <= 4.8)).astype(np.float32)
        zone = cv2.GaussianBlur(zone, (7, 7), 1.5)
        if prot is not None:
            zone[prot] = 0.0
        dither_offset = (bayer * amplitude * zone)[..., None]
        # Slightly stronger on R/B channels (5-bit in RGB565) than G (6-bit)
        chan_scale = np.array([1.15, 0.75, 1.15], dtype=np.float32)[None, None, :]
        res = np.clip(np.rint(f.astype(np.float32) + dither_offset * chan_scale), 0, 255).astype(np.uint8)
        if prot is not None:
            res[prot] = frames[0][prot]
        out[i] = res
    return out


def apply_deluxe_finish(
    frames: Sequence[np.ndarray] | np.ndarray,
    wait: float,
    *,
    protected_mask: np.ndarray | None = None,
    reference_frames: Sequence[np.ndarray] | np.ndarray | None = None,
    backend: str = "native",
    enable_dither: bool = True,
    enable_flicker_fix: bool = True,
    enable_photometric_fix: bool = True,
    enable_sharpness_guard: bool = True,
    precomputed_baseline: dict | None = None,
) -> dict:
    """Execute the full Ouroboros Deluxe Finish pipeline with mandatory post-DDS validation & regression rejection.

    Pipeline:
      1. Baseline post-DDS encode -> decode -> evaluate (ground truth before finishing!)
      2. Selective anti-flicker (ONLY if artificial flicker is detected!)
      3. Photometric Lab circular harmonization (ONLY if luminance/chroma seam step warrants it!)
      4. Sharpness/texture guard (ONLY if reference sharpness dropped below 0.90!)
      5. Temporally-static Bayer dither in smooth gradients (ONLY if post-DDS bandingRisk is detected!)
      6. Mandatory post-DDS round-trip after every candidate stage — reject any stage that degrades decoded DDS!
    """
    t0 = time.perf_counter()
    src = np.asarray(frames, dtype=np.uint8)
    if src.shape[-1] == 4:
        src = src[..., :3]
    prot_eval = _align_protected_dxt1(protected_mask) if protected_mask is not None else None
    ref = np.asarray(reference_frames, dtype=np.uint8)[..., :3] if reference_frames is not None else src

    # 1. Mandatory baseline post-DDS measurement on the actual sequence (reuse if identical sequence already measured)
    base_res = (
        precomputed_baseline
        if precomputed_baseline is not None
        else evaluate_post_dds(src, wait, protected_mask=prot_eval, reference_frames=ref, backend=backend, return_blobs=True)
    )
    base_eval = base_res["report"]
    best_frames = src
    best_eval = base_eval
    best_blobs = base_res["blobs"]
    best_decoded = base_res["decoded"]

    applied_steps: list[str] = []
    rejected_steps: list[dict] = []

    def _try_stage(stage_name: str, candidate_frames: np.ndarray, *, prefer_banding: bool = False) -> None:
        nonlocal best_frames, best_eval, best_blobs, best_decoded
        res = evaluate_post_dds(
            candidate_frames, wait, protected_mask=prot_eval, reference_frames=ref, backend=backend, return_blobs=True
        )
        cand_eval = res["report"]
        cmp = compare_before_after(best_eval, cand_eval)

        if prefer_banding:
            # Accept gradient dither if it lowers post-DDS banding without introducing flicker/ghosting/reversals
            b_old = best_eval["metrics"]["ddsDelta"]["bandingPostDds"]
            b_new = cand_eval["metrics"]["ddsDelta"]["bandingPostDds"]
            flk_ok = not cand_eval["metrics"]["flicker"]["artificialFlicker"]
            psnr_ok = cand_eval["metrics"]["ddsDelta"]["meanPsnr"] >= best_eval["metrics"]["ddsDelta"]["meanPsnr"] - 1.5
            improved = (b_new < b_old - 0.015) and flk_ok and psnr_ok and not cmp["hardRejectReasons"]
            if improved:
                best_frames = candidate_frames
                best_eval = cand_eval
                best_blobs = res["blobs"]
                best_decoded = res["decoded"]
                applied_steps.append(stage_name)
            else:
                rejected_steps.append(
                    {
                        "stage": stage_name,
                        "reason": "Dither não reduziu banding pós-DDS sem custo colateral.",
                        "bandingBefore": b_old,
                        "bandingAfter": b_new,
                    }
                )
            return

        improved = cmp["accepted"] and (
            cmp["afterCompositeCost"] < cmp["beforeCompositeCost"] - 0.02
            or len(cand_eval["issues"]) < len(best_eval["issues"])
        )
        if improved:
            best_frames = candidate_frames
            best_eval = cand_eval
            best_blobs = res["blobs"]
            best_decoded = res["decoded"]
            applied_steps.append(stage_name)
        else:
            rejected_steps.append(
                {
                    "stage": stage_name,
                    "reason": "; ".join(cmp["hardRejectReasons"]) or "Não melhorou o resultado após DDS.",
                    "beforeCost": cmp["beforeCompositeCost"],
                    "afterCost": cmp["afterCompositeCost"],
                }
            )

    # 2. Selective anti-flicker (ONLY when artificial flicker is detected — preserves natural lighting!)
    if enable_flicker_fix and best_eval["metrics"]["flicker"]["artificialFlicker"]:
        deflickered = suppress_artificial_flicker(best_frames, protected_mask=protected_mask, strength=0.80)
        _try_stage("selective_anti_flicker", deflickered)

    # 3. Circular photometric consistency (ONLY when seam luminance or chroma jump exists)
    if enable_photometric_fix:
        lc = best_eval["metrics"]["luminanceContinuity"]
        cc = best_eval["metrics"]["chromaticContinuity"]
        if abs(lc["seamDeltaL"]) > 1.2 or cc["seamDeltaChroma"] > 1.8:
            harm = level2_photometric_harmonize(best_frames, protected_mask=protected_mask)
            _try_stage("photometric_lab_harmonize", harm)

    # 4. Texture & sharpness preservation guard (ONLY if sharpness dropped relative to reference)
    if enable_sharpness_guard:
        if best_eval["metrics"]["texturePreservation"]["sharpnessRetention"] < 0.88:
            sharpened = preserve_detail_unsharp(best_frames, amount=0.30, protected_mask=protected_mask)
            _try_stage("detail_unsharp_guard", sharpened)

    # 5. DXT1a-aware temporally-static gradient dither (ONLY if post-DDS banding is detected)
    if enable_dither and best_eval["metrics"]["ddsDelta"]["bandingRisk"]:
        dithered = apply_stable_gradient_dither(best_frames, amplitude=4.5, protected_mask=protected_mask)
        _try_stage("stable_bayer_gradient_dither", dithered, prefer_banding=True)

    overall_cmp = compare_before_after(base_eval, best_eval)

    return {
        "version": DELUXE_VERSION,
        "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
        "frames": best_frames,
        "decodedDdsFrames": best_decoded,
        "ddsBlobs": best_blobs,
        "ddsBytes": sum(len(b) for b in best_blobs),
        "appliedSteps": applied_steps,
        "rejectedSteps": rejected_steps,
        "baselinePostDds": base_eval,
        "finalPostDds": best_eval,
        "comparison": overall_cmp,
    }
