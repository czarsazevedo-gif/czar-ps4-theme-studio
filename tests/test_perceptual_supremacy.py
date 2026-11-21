"""Dedicated unit tests for Missão Czar — Ouroboros Perceptual Supremacy (Interventions 01..21)."""

from __future__ import annotations

import numpy as np

from app.ouroboros_engine import PROPOSAL_PROFILES
from app.ouroboros_engine.metrics import evaluate_loop
from app.ouroboros_engine.proposals import _score_candidate_for_profile, _select_profile_plan
from app.ouroboros_engine.search import (
    _pareto_rank_candidates,
    _slice_from_buffer,
    compute_multicriteria_cost,
    detect_scipy_periodicity,
)


def _make_cyclic_wave(n: int = 16, h: int = 48, w: int = 64, *, add_seam_jump: bool = False) -> np.ndarray:
    """Synthetic periodic horizontal wave moving smoothly to the right."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    frames = []
    for i in range(n):
        phase = 2.0 * np.pi * (i / float(n))
        if add_seam_jump and i == n - 1:
            phase += 1.6  # abrupt jump right before wrap
        wave = 128.0 + 70.0 * np.sin(2.0 * np.pi * xx / 16.0 - phase) * np.cos(np.pi * yy / h)
        r = np.clip(wave, 0, 255).astype(np.uint8)
        g = np.clip(wave * 0.85 + 20.0, 0, 255).astype(np.uint8)
        b = np.clip(255.0 - wave * 0.6, 0, 255).astype(np.uint8)
        frames.append(np.stack([r, g, b], axis=-1))
    return np.stack(frames, axis=0)


# ==================== CHECKPOINT B: SELECTION & SEARCH (01, 02, 03, 10, 11, 12, 13, 14) ====================


def test_i01_micro_loop_penalized_vs_viable_cycle():
    """Intervention 01: Sub-1.1s micro-loops must lose to viable >= 1.5s cycles with similar seam quality."""
    micro = {
        "start": 1.0,
        "end": 1.4,
        "duration": 0.40,
        "frames": 6,
        "fps": 15.0,
        "costs": {
            "totalCost": 0.65,
            "seamRatio": 0.95,
            "seamSsim": 0.94,
            "seamDirectionCosine": 0.85,
            "velocityJump": 0.12,
            "ghostingScore": 0.0,
            "hasHardDefect": False,
            "isStatic": False,
        },
        "paretoFrontier": True,
    }
    viable = {
        **micro,
        "start": 2.0,
        "end": 3.8,
        "duration": 1.80,
        "frames": 18,
        "fps": 10.0,
        "costs": {**micro["costs"], "totalCost": 0.72},
    }
    bal = PROPOSAL_PROFILES[1]
    s_micro = _score_candidate_for_profile(micro, bal, source_duration=10.0)
    s_viable = _score_candidate_for_profile(viable, bal, source_duration=10.0)
    # Higher score is better in _score_candidate_for_profile
    assert s_viable > s_micro, f"Viable loop ({s_viable}) must beat micro-loop ({s_micro})"


def test_i03_cross_profile_diversity_and_effective_motion_fps():
    """Intervention 03 & 14: Flexibility expands span capped by anime eff_motion_fps and avoids cloning quality."""
    cand_a = {
        "start": 1.0,
        "end": 2.6,
        "duration": 1.6,
        "frames": 13,
        "fps": 8.125,
        "costs": {
            "totalCost": 0.65,
            "seamRatio": 1.02,
            "seamSsim": 0.93,
            "seamDirectionCosine": 0.88,
            "velocityJump": 0.10,
            "ghostingScore": 0.0,
            "hasHardDefect": False,
            "isStatic": False,
        },
        "paretoFrontier": True,
        "phaseLocked": True,
    }
    cand_b = {
        **cand_a,
        "start": 6.0,
        "end": 9.2,
        "duration": 3.2,
        "frames": 38,
        "fps": 11.875,
        "costs": {**cand_a["costs"], "totalCost": 0.78},
    }
    analysis = {
        "gifInfo": {"isGif": False},
        "sentinela": {"source": {"fps": 60.0}, "levelA": {"effectiveMotionFps": 12.0}},
        "groups": [
            {"size": [1280, 720], "shortlist": [cand_a]},
            {"size": [960, 540], "shortlist": [cand_a, cand_b]},
            {"size": [640, 360], "shortlist": [cand_a, cand_b]},
        ],
    }
    used = []
    plans = {}
    for prof in PROPOSAL_PROFILES:
        p = _select_profile_plan(analysis, prof, source_duration=20.0, source_fps=60.0, used_intervals=used)
        plans[prof["id"]] = p
        used.append((p["start"], p["end"], p["frames"]))

    # Flexibility must cap FPS close to anime effectiveMotionFps (~12 FPS) so it doesn't sample duplicate frames!
    flex_fps = 1.0 / plans["flexibility"]["wait"]
    assert flex_fps <= 15.0, f"Flexibility FPS ({flex_fps:.1f}) must respect anime effectiveMotionFps"
    assert (plans["quality"]["start"], plans["quality"]["frames"]) != (
        plans["flexibility"]["start"],
        plans["flexibility"]["frames"],
    )


def test_i10_single_buffer_slicing_matches_expected_frame_count():
    """Intervention 10: Dense 160x90 single-pass buffer slicing yields exact requested frame count & order."""
    buf = np.zeros((100, 18, 32, 3), dtype=np.uint8)
    for i in range(100):
        buf[i] = i
    sliced = _slice_from_buffer(buf, buf_dur=10.0, start=2.0, end=4.0, count=12)
    assert sliced is not None
    assert sliced.shape == (12, 18, 32, 3)
    vals = [int(sliced[k, 0, 0, 0]) for k in range(12)]
    assert vals[0] == 20
    assert all(vals[k] <= vals[k + 1] for k in range(11))


def test_i11_pareto_ranking_vetoes_hard_defects_and_marks_frontier():
    """Intervention 11: Multi-objective Pareto ranking separates clean frontier from dominated and hard-defect loops."""
    cands = [
        {
            "id": "a_best_seam",
            "costs": {
                "seamRatio": 0.90,
                "seamSsim": 0.96,
                "seamDirectionCosine": 0.95,
                "velocityJump": 0.05,
                "accelRatio": 1.0,
                "cadenceError": 0.1,
                "durationPenalty": 0.2,
                "artifactPenalty": 0.0,
                "hasHardDefect": False,
            },
        },
        {
            "id": "b_longer",
            "costs": {
                "seamRatio": 1.05,
                "seamSsim": 0.94,
                "seamDirectionCosine": 0.92,
                "velocityJump": 0.08,
                "accelRatio": 1.1,
                "cadenceError": 0.1,
                "durationPenalty": 0.0,
                "artifactPenalty": 0.0,
                "hasHardDefect": False,
            },
        },
        {
            "id": "c_dominated",
            "costs": {
                "seamRatio": 1.40,
                "seamSsim": 0.85,
                "seamDirectionCosine": 0.70,
                "velocityJump": 0.30,
                "accelRatio": 2.0,
                "cadenceError": 0.5,
                "durationPenalty": 0.4,
                "artifactPenalty": 0.2,
                "hasHardDefect": False,
            },
        },
        {
            "id": "d_hard_defect",
            "costs": {
                "seamRatio": 0.50,
                "seamSsim": 0.99,
                "seamDirectionCosine": 0.99,
                "velocityJump": 0.01,
                "accelRatio": 0.5,
                "cadenceError": 0.0,
                "durationPenalty": 0.0,
                "artifactPenalty": 0.0,
                "hasHardDefect": True,
            },
        },
    ]
    ranked = _pareto_rank_candidates(cands)
    by_id = {c["id"]: c for c in ranked}
    assert by_id["a_best_seam"]["paretoFrontier"] is True
    assert by_id["b_longer"]["paretoFrontier"] is True
    assert by_id["c_dominated"]["paretoFrontier"] is False
    assert by_id["c_dominated"]["paretoDominatedCount"] >= 1
    assert by_id["d_hard_defect"]["paretoFrontier"] is False


def test_i12_and_i14_multicriteria_cost_detects_seam_jump_and_slideshow():
    """Interventions 12 & 14: Multicriteria cost penalizes abrupt seam jumps and extreme low-FPS slideshows."""
    clean = _make_cyclic_wave(16, add_seam_jump=False)
    broken = _make_cyclic_wave(16, add_seam_jump=True)

    ev_clean = evaluate_loop(clean, wait=0.10)
    ev_broken = evaluate_loop(broken, wait=0.10)
    c_clean = compute_multicriteria_cost(ev_clean, duration_sec=1.6, source_duration=10.0, source_fps=30.0, wait=0.10)
    c_broken = compute_multicriteria_cost(ev_broken, duration_sec=1.6, source_duration=10.0, source_fps=30.0, wait=0.10)
    assert c_clean["totalCost"] < c_broken["totalCost"]
    assert "accelRatio" in c_clean and "hasHardDefect" in c_clean

    # Stretching 12 frames over 5.0 seconds (2.4 FPS slideshow) must carry higher cost than natural 1.4s (~8.6 FPS)
    ev_12 = evaluate_loop(clean[:12], wait=1.4 / 12.0)
    c_natural = compute_multicriteria_cost(ev_12, duration_sec=1.4, source_duration=10.0, source_fps=30.0, wait=1.4 / 12.0)
    c_slideshow = compute_multicriteria_cost(ev_12, duration_sec=5.0, source_duration=10.0, source_fps=30.0, wait=5.0 / 12.0)
    assert c_natural["totalCost"] < c_slideshow["totalCost"]


def test_i13_regional_periodicity_detects_local_periodic_motion():
    """Intervention 13: Regional FFT+autocorrelation detects periodicity confined to one quadrant."""
    n = 48
    h, w = 48, 64
    frames = np.full((n, h, w, 3), 40, dtype=np.uint8)
    # Periodic blinking/oscillating texture only in top-left quadrant (period = 12 frames = 1.5s at step=0.125s)
    for i in range(n):
        val = int(120 + 90 * np.sin(2.0 * np.pi * i / 12.0))
        frames[i, :20, :28, :] = val
    res = detect_scipy_periodicity(frames, step=0.125)
    assert res["detected"] is True
    assert any(abs(p - 1.5) < 0.35 for p in res["periodsSec"])
    assert len(res["regionalPeriods"]) >= 1


# ==================== CHECKPOINT C: FLOW, RECONSTRUCTION & METRICS (04, 05, 06, 15, 16, 17, 18) ====================


def test_i15_multifactor_flow_confidence_and_lru_cache():
    """Intervention 15: Multi-factor confidence penalizes occluded/untextured areas and caches repeated pairs."""
    from app.ouroboros_engine.flow import clear_flow_cache, compute_bidirectional_flow

    clear_flow_cache()
    seq = _make_cyclic_wave(8, h=64, w=96)
    fr1 = compute_bidirectional_flow(seq[0], seq[1], multi_factor=True)
    fr2 = compute_bidirectional_flow(seq[0], seq[1], multi_factor=True)
    assert fr1 is fr2, "Second identical call must hit bounded LRU flow cache"
    assert 0.0 <= float(fr1.confidence.min()) <= float(fr1.confidence.max()) <= 1.0

    # Flat untextured pair must yield low confidence compared to textured wave
    flat = np.full((64, 96, 3), 120, dtype=np.uint8)
    fr_flat = compute_bidirectional_flow(flat, flat, multi_factor=True)
    assert float(fr_flat.confidence.mean()) < float(fr1.confidence.mean())


def test_i04_and_i16_hf_multiband_and_protected_seam_routing():
    """Interventions 04 & 16: HF-preserving multiband keeps sharpness; optimal seam routes around protected regions."""
    from app.ouroboros_engine.metrics import _laplacian_sharpness
    from app.ouroboros_engine.reconstruction import _optimal_seam_mask, multiband_blend

    seq = _make_cyclic_wave(8, h=64, w=96)
    mask = np.tile(np.linspace(0.0, 1.0, 96, dtype=np.float32)[None, :], (64, 1))
    b_std = multiband_blend(seq[0], seq[3], mask, levels=3, preserve_hf=False)
    b_hf = multiband_blend(seq[0], seq[3], mask, levels=3, preserve_hf=True)
    assert _laplacian_sharpness(b_hf[..., 0]) >= _laplacian_sharpness(b_std[..., 0])

    # Protected mask right across center column x=48 -> seam must bend away from protected band
    prot = np.zeros((64, 96), dtype=bool)
    prot[:, 42:54] = True
    seam = _optimal_seam_mask(seq[0], seq[3], blend_pos=0.5, vertical=True, protected_mask=prot)
    # Inside protected band, seam values should be nearly uniform (not cutting through center)
    cut_inside = float(((seam[:, 42:54] > 0.25) & (seam[:, 42:54] < 0.75)).mean())
    assert cut_inside < 0.20, f"Seam cut through protected region: {cut_inside:.3f}"


def test_i05_level6_keeps_animated_regions_independent_and_never_freezes_subtle_motion():
    """Intervention 05 (CZAR-ABS-AUD-008) & Full Moon Clouds fix: independent progressive regions + no total freeze."""
    from app.ouroboros_engine.reconstruction import level6_multi_region_reconstruct

    # Build a subtle moving night-sky scene (temporal std ~3.5) with 95% marked "static" by Sentinela
    n, h, w = 8, 64, 96
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    clouds = np.stack(
        [
            np.stack(
                [
                    np.clip(25 + 14 * np.sin(xx * 0.12 - i * 0.6), 0, 255).astype(np.uint8),
                    np.clip(28 + 14 * np.sin(xx * 0.12 - i * 0.6), 0, 255).astype(np.uint8),
                    np.clip(45 + 18 * np.sin(xx * 0.12 - i * 0.6), 0, 255).astype(np.uint8),
                ],
                axis=-1,
            )
            for i in range(n)
        ],
        axis=0,
    )
    fake_sentinela = {
        "levelB": {
            "hasMixedStaticDynamic": True,
            "regions": [
                {"bboxNorm": [0.0, 0.0, 0.5, 0.5], "behavior": "progressive_flow"},
                {"bboxNorm": [0.5, 0.5, 1.0, 1.0], "behavior": "progressive_flow"},
                {"bboxNorm": [0.0, 0.5, 0.5, 1.0], "behavior": "static"},
            ],
        }
    }
    out, meta = level6_multi_region_reconstruct(clouds, 0.1, sentinela_report=fake_sentinela)
    anim_ids = [d["id"] for d in meta.get("details", []) if d["strategy"] == "animate"]
    # Both progressive_flow regions must stay independent (auto_animate_0 and auto_animate_1)!
    assert len(anim_ids) == 2 and len(set(anim_ids)) == 2, f"Expected 2 independent animate regions, got {anim_ids}"
    # Because clouds have temporal_std > 1.5 everywhere, auto-protect must NOT freeze moving clouds!
    assert meta.get("protectedFraction", 0.0) < 0.30
    assert float(np.abs(out[0].astype(np.float32) - out[4].astype(np.float32)).mean()) > 1.0


def test_i06_i17_i18_ghosting_spatial_audit_provenance_and_static_rejection():
    """Interventions 06, 17, 18 & CZAR-ABS-AUD-011: no false ghosting on clean wave, spatial audit, provenance, static REPROVADO."""
    from app.ouroboros_engine.metrics import (
        classify_artifact_provenance,
        classify_loop_verdict,
        evaluate_loop,
        evaluate_ten_cycles,
    )

    clean = _make_cyclic_wave(12, h=64, w=96, add_seam_jump=False)
    ev = evaluate_loop(clean, wait=1.0 / 12.0)
    assert not ev["metrics"]["ghosting"]["detected"], f"False ghosting on clean wave: {ev['metrics']['ghosting']}"
    assert "spatialRegions" in ev["metrics"]
    ten = evaluate_ten_cycles(ev)
    assert "spatialAudit" in ten and "seamHotspot" in ten["spatialAudit"]

    # Differential border check (CZAR-ABS-AUD-011): dark letterbox present in BOTH source and output must not fire
    dark_src = clean.copy()
    dark_src[:, :8, :, :] = 0
    dark_src[:, -8:, :, :] = 0
    ev_dark = evaluate_loop(dark_src, wait=1.0 / 12.0, reference_frames=dark_src)
    assert not any("Bordas vazias" in iss for iss in ev_dark["issues"])

    # Static scene MUST be classified REPROVADO (never CONDICIONAL!)
    static_seq = np.repeat(clean[:1], 8, axis=0)
    ev_stat = evaluate_loop(static_seq, wait=0.1)
    verdict_stat = classify_loop_verdict(ev_stat, evaluate_ten_cycles(ev_stat))
    assert verdict_stat["verdict"] == "REPROVADO"

    # Provenance separation (Intervention 18)
    prov = classify_artifact_provenance(
        ["Risco de banding/posterização após codificação DXT1a.", "Corte abrupto", "Salto de luminância na junção."],
        raw_issues={"Corte abrupto"},
        strategy_used="level4_content_bridge",
    )
    assert prov["originalDefects"] == ["Corte abrupto"]
    assert prov["compressionDefects"] == ["Risco de banding/posterização após codificação DXT1a."]
    assert prov["reconstructionDefects"] == ["Salto de luminância na junção."]
