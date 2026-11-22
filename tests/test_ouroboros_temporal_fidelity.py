"""Permanent regression tests for Ouroboros Temporal & Perceptual Motion Fidelity.

Guards against optimizing visual/seam continuity at the expense of original motion preservation:
  1. Motion-starved or near-static sequences NEVER get APROVADO or inflated definitiveLoopScore.
  2. Dead-zone intervals vs active sourceMotionProfile are penalized and flagged.
  3. Reconstruction/synthesis that collapses temporal excursion, focal motion, or active area is vetoed.
  4. Multi-cycle synthesize_periodic_2d preserves 1.0x speed at high frame counts (33..48f) with zero freezes.
  5. Search seeds & proposal scoring favor genuinely active intervals over low-motion dead zones.
  6. All 7 independent evaluation axes are emitted explicitly and enforced.
  7. Dark-vignette anime scenes do not trigger false empty-border alarms after photometric harmonization.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from app.ouroboros_engine import OUROBOROS_VERSION
from app.ouroboros_engine.analyzer import analyze_levels
from app.ouroboros_engine.metrics import (
    check_prohibited_artifacts,
    classify_loop_verdict,
    compare_before_after,
    evaluate_loop,
    evaluate_post_dds,
    evaluate_ten_cycles,
)
from app.ouroboros_engine.proposals import _score_candidate_for_profile
from app.ouroboros_engine.reconstruction import (
    level2_photometric_harmonize,
    level4_content_bridge_reconstruct,
    reconstruct_best,
)
from app.ouroboros_engine.search import _generate_seeds, compute_multicriteria_cost
from app.ouroboros_engine.synthesis import estimate_2d_flow_field, synthesize_periodic_2d


def _make_character_with_lightning_clip(n: int = 16, h: int = 144, w: int = 256) -> np.ndarray:
    """Synthetic anime portrait silhouette with swaying hair/breathing + branching lightning arcs."""
    rng = np.random.default_rng(2026)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    # Dark vignette background (like Zenitsu White)
    cx, cy = w * 0.5, h * 0.5
    dist = np.hypot((xx - cx) / (w * 0.55), (yy - cy) / (h * 0.55))
    moon = np.clip(110.0 * np.exp(-dist * dist * 2.0), 4.0, 120.0)
    frames = []
    for i in range(n):
        phase = 2.0 * math.pi * i / float(n)
        img = np.stack([moon, moon, moon * 1.04], axis=-1).astype(np.float32)
        # Subtle swaying character head + waving hair spikes in focal center
        hx = int(round(cx + 3.0 * math.sin(phase)))
        hy = int(round(cy * 0.92 + 2.5 * math.cos(phase)))
        cv2.circle(img, (hx, hy), 28, (95, 90, 95), -1)
        for k in range(-4, 5):
            ang = -math.pi * 0.5 + k * 0.25 + 0.14 * math.sin(phase + k * 0.7)
            x1 = int(round(hx + 24 * math.cos(ang)))
            y1 = int(round(hy + 24 * math.sin(ang)))
            x2 = int(round(hx + 48 * math.cos(ang)))
            y2 = int(round(hy + 48 * math.sin(ang)))
            cv2.line(img, (x1, y1), (x2, y2), (185, 185, 195), 3)
        # Dynamic crackling lightning bolts shifting & branching across frames
        for bolt in range(4):
            bx = int(round((w * 0.22) + ((i * 19 + bolt * 53) % int(w * 0.58))))
            by0 = h - 10
            for seg in range(5):
                nx = int(np.clip(bx + int(rng.integers(-16, 17)), 10, w - 10))
                ny = max(8, by0 - 24)
                cv2.line(img, (bx, by0), (nx, ny), (250, 250, 255), 2)
                bx, by0 = nx, ny
        frames.append(np.clip(img, 0, 255).astype(np.uint8))
    return np.stack(frames, axis=0)


def _make_drifting_clouds_clip(n: int = 36, h: int = 144, w: int = 256, vx: float = 2.4) -> np.ndarray:
    """Clean unidirectional drifting clouds with rich spatial texture (valid target for Level 5 advection)."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    frames = []
    for i in range(n):
        sx = xx - i * vx
        cloud = (
            75.0
            + 55.0 * np.sin(sx * 0.09) * np.cos(yy * 0.05)
            + 30.0 * np.sin(sx * 0.19 + yy * 0.08)
            + 20.0 * np.cos(sx * 0.31 - yy * 0.12)
        )
        r = np.clip(cloud * 0.9, 10, 245).astype(np.uint8)
        g = np.clip(cloud * 0.95, 10, 245).astype(np.uint8)
        b = np.clip(cloud * 1.12, 18, 255).astype(np.uint8)
        frames.append(np.stack([r, g, b], axis=-1))
    return np.stack(frames, axis=0)


def test_motion_starved_sequence_never_gets_approved_or_max_score():
    """High SSIM / zero seam diff achieved through near-static or starved motion MUST NOT get APROVADO or high score."""
    h, w, n = 90, 160, 12
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = np.stack(
        [
            np.clip(60 + 0.5 * xx, 0, 255).astype(np.uint8),
            np.clip(70 + 0.6 * yy, 0, 255).astype(np.uint8),
            np.clip(90 + 0.4 * xx, 0, 255).astype(np.uint8),
        ],
        axis=-1,
    )
    # Tiny imperceptible shimmer on a 4x4 patch (escapes strict zero-diff, seamSsim > 0.999)
    starved = np.repeat(base[None, ...], n, axis=0)
    for i in range(n):
        delta = int(round(6.0 * math.sin(2.0 * math.pi * i / n)))
        starved[i, 40:44, 76:80] = np.clip(starved[i, 40:44, 76:80].astype(np.int16) + delta, 0, 255).astype(np.uint8)

    # 1. Even WITHOUT source_motion_profile, intrinsic starvation blocks APROVADO and caps score!
    ev_intr = evaluate_loop(starved, 0.10)
    ten_intr = evaluate_ten_cycles(ev_intr)
    ver_intr = classify_loop_verdict(ev_intr, ten_intr)
    assert ver_intr["verdict"] != "APROVADO", ver_intr
    assert ev_intr["metrics"]["temporalFidelity"]["motionStarved"] is True
    assert ev_intr["metrics"]["trajectoryKinematics"]["definitiveLoopScore"] <= 5.8

    # 2. Against a genuinely active source_motion_profile (like Zenitsu: localWindowStdMedian=11.0),
    # a quiet loop with t_std=3.8 (35% of source) MUST be flagged sourceMotionStarved and NEVER get APROVADO!
    quiet = np.repeat(base[None, ...], n, axis=0)
    for i in range(n):
        wave = (5.0 * np.sin(xx * 0.10 - 2.0 * math.pi * i / n)).astype(np.float32)
        quiet[i] = np.clip(quiet[i].astype(np.float32) + wave[..., None], 0, 255).astype(np.uint8)

    active_src_profile = {
        "temporalStdMean": 24.0,
        "localWindowStdMedian": 11.5,
        "localFocalStdMedian": 12.5,
        "localActiveAreaMedian": 0.65,
        "flowP95Norm180": 3.2,
    }
    ev_dead = evaluate_post_dds(quiet, 0.10, source_motion_profile=active_src_profile)
    ten_dead = evaluate_ten_cycles(ev_dead)
    ver_dead = classify_loop_verdict(ev_dead, ten_dead)
    tf_dead = ev_dead["metrics"]["temporalFidelity"]
    assert tf_dead["sourceMotionStarved"] is True, tf_dead
    assert tf_dead["motionStarved"] is True, tf_dead
    assert ver_dead["verdict"] != "APROVADO", ver_dead
    assert ev_dead["metrics"]["trajectoryKinematics"]["definitiveLoopScore"] <= 5.8
    assert ten_dead["motionPreservationOk"] is False
    assert ten_dead["continuousTenCyclePass"] is False


def test_seven_independent_axes_emitted_and_enforced():
    """All 7 evaluation dimensions must be exposed independently inside metrics['independentAxes']."""
    clouds = _make_drifting_clouds_clip(n=12, h=90, w=160, vx=2.0)
    ev = evaluate_post_dds(clouds, 0.08)
    ten = evaluate_ten_cycles(ev)
    axes = ev["metrics"]["independentAxes"]
    expected = {
        "visualContinuity",
        "kinematicContinuity",
        "motionPreservation",
        "freezeIntegrity",
        "jumpIntegrity",
        "cadenceIntegrity",
        "tenCycleIntegrity",
    }
    assert expected.issubset(set(axes.keys())), axes.keys()
    for name in expected:
        assert "passed" in axes[name], (name, axes[name])
    assert axes["motionPreservation"]["passed"] is True
    assert axes["freezeIntegrity"]["passed"] is True
    assert ten["motionPreservationOk"] is True


def test_compare_before_after_and_reconstruct_best_veto_motion_collapse():
    """Any reconstruction that collapses temporal excursion or active subject motion must be rejected."""
    clip = _make_character_with_lightning_clip(n=14, h=144, w=256)
    # Build a severely damped fake candidate (blending every frame 75% toward frame 0 -> kills motion!)
    damped = np.clip(
        np.rint(0.75 * clip[0:1].astype(np.float32) + 0.25 * clip.astype(np.float32)),
        0,
        255,
    ).astype(np.uint8)

    ev_base = evaluate_loop(clip, 0.10)
    ev_damped = evaluate_loop(damped, 0.10, reference_frames=clip)
    cmp = compare_before_after(ev_base, ev_damped)
    assert cmp["accepted"] is False, cmp
    assert any("excursão" in r.lower() or "movimento" in r.lower() for r in cmp["hardRejectReasons"]), cmp

    proh = check_prohibited_artifacts(ev_damped, strategy_used="level5_dynamic_texture_synthesis")
    assert proh["clean"] is False, proh

    # Run reconstruct_best on the non-advectable character+lightning sequence:
    # it must NOT pick level5 and must retain >= 75% of original temporal std!
    sent = analyze_levels(clip, dt=0.10)
    rec = reconstruct_best(clip, 0.10, sentinela_report=sent, source_motion_profile=sent["sourceMotionProfile"], verify_dds=False)
    assert rec["selectedStrategy"] != "level5_dynamic_texture_synthesis", rec["attempts"]
    raw_std = float(clip.astype(np.float32).std(axis=0).mean())
    out_std = float(rec["frames"].astype(np.float32).std(axis=0).mean())
    assert out_std >= raw_std * 0.75, f"Motion collapsed: raw_std={raw_std:.2f} -> out_std={out_std:.2f}"


def test_synthesize_periodic_2d_preserves_speed_at_high_frame_counts_without_freezes():
    """Velocity-preserving multi-cycle phase handoff must keep ~1.0x speed and zero freezes at n=36 frames."""
    clouds = _make_drifting_clouds_clip(n=36, h=144, w=256, vx=2.6)
    flow_f, f_stats = estimate_2d_flow_field(clouds)
    assert f_stats["advectionExplainability"] >= 0.25, f_stats

    synth = synthesize_periodic_2d(
        clouds[0],
        flow_f,
        n_frames=36,
        base_frame_b=clouds[18],
        cycle_steps=36.0,
        align_dxt1_blocks=True,
    )
    ev = evaluate_loop(synth, 0.0667, reference_frames=clouds)
    tf = ev["metrics"]["temporalFidelity"]
    assert ev["metrics"]["freezes"]["detected"] is False, ev["metrics"]["freezes"]
    assert tf["nearDuplicatePairs"] == 0, tf
    assert tf["rawToOutputExcursionRetention"] >= 0.75, tf
    assert tf["rawToOutputActiveAreaRetention"] >= 0.80, tf
    assert ev["metrics"]["reversals"]["detected"] is False


def test_search_and_proposal_scoring_prefer_active_segment_over_dead_zone():
    """Search seeds and proposal scoring must rank genuinely animated segments above quiet dead zones."""
    n_half = 24
    h, w = 90, 160
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)

    # First half (0..2.4s): very low-motion quiet dead zone
    quiet_frames = []
    for i in range(n_half):
        ph = 2.0 * math.pi * i / 12.0
        val = np.clip(90.0 + 3.0 * np.sin(xx * 0.10 - ph), 0, 255).astype(np.uint8)
        quiet_frames.append(np.stack([val, val, val], axis=-1))

    # Second half (2.4..4.8s): vivid periodic animation with same clean closure
    active_frames = []
    for i in range(n_half):
        ph = 2.0 * math.pi * i / 12.0
        val = np.clip(110.0 + 55.0 * np.sin(xx * 0.12 - ph) * np.cos(yy * 0.08), 0, 255).astype(np.uint8)
        active_frames.append(np.stack([val, np.clip(val * 0.9, 0, 255).astype(np.uint8), val], axis=-1))

    combined = np.stack(quiet_frames + active_frames, axis=0)
    sent = analyze_levels(combined, dt=0.10)
    seeds = _generate_seeds(
        combined,
        offset=0.0,
        step=0.10,
        size=(960, 540),
        source_fps=10.0,
        source_duration=4.8,
        source_motion_profile=sent["sourceMotionProfile"],
    )
    assert seeds
    best_seed = min(seeds, key=lambda o: o[0])
    # Best seed must come from the active second half (>= 2.2s), NOT the quiet dead zone (0..2.0s)!
    assert best_seed[1] >= 2.0, f"Selected low-motion dead zone instead of active half: {best_seed}"

    # Also verify _score_candidate_for_profile strongly prefers active over starved candidate
    dead_cand = {
        "start": 0.2,
        "end": 1.6,
        "duration": 1.4,
        "frames": 14,
        "wait": 0.10,
        "costs": {
            "totalCost": 2.2,
            "seamRatio": 1.0,
            "seamSsim": 0.97,
            "internalMedianSsim": 0.99,
            "seamDirectionCosine": 0.90,
            "velocityJump": 0.05,
            "accelRatio": 1.0,
            "motionFidelityScore": 3.5,
            "sourceExcursionRetention": 0.35,
            "sourceFocalRetention": 0.38,
            "motionStarved": True,
            "hasHardDefect": False,
        },
    }
    active_cand = {
        "start": 2.4,
        "end": 4.4,
        "duration": 2.0,
        "frames": 20,
        "wait": 0.10,
        "costs": {
            "totalCost": 2.6,
            "seamRatio": 1.15,
            "seamSsim": 0.90,
            "internalMedianSsim": 0.91,
            "seamDirectionCosine": 0.85,
            "velocityJump": 0.12,
            "accelRatio": 1.1,
            "motionFidelityScore": 9.8,
            "sourceExcursionRetention": 1.05,
            "sourceFocalRetention": 1.08,
            "motionStarved": False,
            "hasHardDefect": False,
        },
    }
    for pid in ("quality", "balance", "flexibility"):
        assert _score_candidate_for_profile(active_cand, pid) > _score_candidate_for_profile(dead_cand, pid)


def test_dark_vignette_does_not_trigger_false_empty_border_after_photometric_harmonize():
    """Dark vignette edges present in source must not trigger empty-border errors after mild Lab adjustment."""
    clip = _make_character_with_lightning_clip(n=12, h=144, w=256)
    harm = level2_photometric_harmonize(clip)
    ev_ok = evaluate_loop(harm, 0.10, reference_frames=clip)
    assert not any("Bordas vazias" in iss for iss in ev_ok["issues"]), ev_ok["issues"]

    # Contrast: genuinely introducing a black warp border on a bright scene MUST still fire!
    bright = _make_drifting_clouds_clip(n=8, h=144, w=256, vx=2.0)
    warped_bad = bright.copy()
    warped_bad[:, :6, :, :] = 0
    warped_bad[:, -6:, :, :] = 0
    warped_bad[:, :, :6, :] = 0
    warped_bad[:, :, -6:, :] = 0
    ev_bad = evaluate_loop(warped_bad, 0.10, reference_frames=bright)
    assert any("Bordas vazias" in iss for iss in ev_bad["issues"]), ev_bad["issues"]


def test_version_bump_and_open_loop_content_bridge_directional_continuity():
    """Cache version must invalidate pre-fix caches and level4_content_bridge_reconstruct must preserve forward direction."""
    assert OUROBOROS_VERSION == "ouroboros-absolute-2"

    # Open drifting cloud segment with real upstream lead_in frames
    full_clouds = _make_drifting_clouds_clip(n=26, h=144, w=256, vx=2.2)
    lead_in = full_clouds[:6]
    main_seg = full_clouds[6:24]
    bridged, meta = level4_content_bridge_reconstruct(main_seg, lead_in=lead_in, prefer="lead_in")
    assert meta["applied"] is True
    ev = evaluate_loop(bridged, 0.08, reference_frames=main_seg)
    cos = float(ev["metrics"]["independentAxes"]["kinematicContinuity"]["seamDirectionCosine"])
    assert cos >= 0.40, f"Open-loop level4_content_bridge_reconstruct suffered directional reversal: cos={cos}"
    assert ev["metrics"]["temporalFidelity"]["rawToOutputExcursionRetention"] >= 0.75

