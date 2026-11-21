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


# ==================== CHECKPOINT D: POST-DDS SUPREMACY (07, 08, 09, 19, 20, 21) ====================


def test_i09_i19_bc1_backends_alpha_safety_temporal_reuse_and_fallback(monkeypatch):
    """Interventions 09 & 19: RGBCX temporal beats canonical PSNR & reduces static shimmer, stays alpha-safe, falls back cleanly."""
    from app import dxt1
    from app.ouroboros_engine import bc1_backends
    from app.ouroboros_engine.metrics import evaluate_post_dds

    # Static background gradient + small moving bright object on right
    n, h, w = 8, 96, 160
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = np.stack(
        [
            np.clip(20 + 0.9 * xx, 0, 255).astype(np.uint8),
            np.clip(30 + 1.4 * yy, 0, 255).astype(np.uint8),
            np.clip(80 + 0.5 * xx + 0.4 * yy, 0, 255).astype(np.uint8),
        ],
        axis=-1,
    )
    seq = []
    for i in range(n):
        f = base.copy()
        cx = 110 + int(12 * np.sin(2.0 * np.pi * i / n))
        f[36:56, cx : cx + 16] = (230, 180, 90)
        seq.append(f)
    arr = np.stack(seq, axis=0)

    ev_nat = evaluate_post_dds(arr, 0.1, backend="native")
    ev_rgb = evaluate_post_dds(arr, 0.1, backend="rgbcx_temporal")
    ev_icbc = evaluate_post_dds(arr, 0.1, backend="icbc")

    d_nat = ev_nat["metrics"]["ddsDelta"]
    d_rgb = ev_rgb["metrics"]["ddsDelta"]
    d_icbc = ev_icbc["metrics"]["ddsDelta"]

    assert d_rgb["validHeaders"] and d_rgb["withinBudget"] and d_rgb["alphaSafe"]
    assert d_icbc["validHeaders"] and d_icbc["withinBudget"] and d_icbc["alphaSafe"]
    assert d_rgb["meanPsnr"] >= d_nat["meanPsnr"], f"RGBCX ({d_rgb['meanPsnr']}) should beat/match native ({d_nat['meanPsnr']})"
    assert d_rgb["reusedBlocks"] > 0, "Temporal Mode A/B must reuse static 4x4 blocks"
    assert d_rgb["wrapStabilizedBlocks"] > 0, "Circular wrap consistency must stabilize static N-1 -> 0 blocks"

    # Interventions 20 & 21: Perceptual Lab metrics & compressedTemporalQuality present
    for key in ("meanLabDeltaE", "p95LabDeltaE", "lumDeltaL", "chromaDeltaC", "edgePsnr", "gradientError", "highlightError"):
        assert key in d_rgb
    ctq = d_rgb["compressedTemporalQuality"]
    for key in ("staticBlockShimmerMad", "wrapStaticShimmerMad", "endpointChangeRate", "chromaOscillation", "edgeAlternationRate"):
        assert key in ctq
    assert ctq["staticBlockShimmerMad"] <= d_nat["compressedTemporalQuality"]["staticBlockShimmerMad"] + 0.05

    # Graceful fallback when libczar_bc1.so is unavailable
    monkeypatch.setattr(bc1_backends, "_load_bc1_lib", lambda: None)
    blobs_fb, meta_fb = bc1_backends.encode_sequence_bc1(arr[:2], backend="rgbcx_temporal")
    assert meta_fb["actualBackend"] == "native" and meta_fb["fallbackUsed"] is True
    assert len(blobs_fb) == 2 and dxt1.decode_dds(blobs_fb[0]).shape == (h, w, 4)


def test_i07_i08_selective_blue_noise_dither_and_strictly_monotonic_finish():
    """Interventions 07 & 08: CC0 blue-noise tile loads, leaves moving regions untouched, and finish never adds issues."""
    from app.ouroboros_engine.finish import _get_blue_noise_tile, apply_deluxe_finish, apply_selective_blue_noise_dither

    tile = _get_blue_noise_tile()
    assert tile.shape == (64, 64), f"Expected vendored 64x64 CC0 blue-noise tile, got {tile.shape}"

    # Left half: static smooth gradient; right half: moving wave
    n, h, w = 8, 96, 160
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    frames = []
    for i in range(n):
        f = np.zeros((h, w, 3), dtype=np.uint8)
        f[:, :80, 0] = np.clip(40 + xx[:, :80] * 1.1, 0, 255).astype(np.uint8)
        f[:, :80, 1] = np.clip(50 + yy[:, :80] * 0.8, 0, 255).astype(np.uint8)
        f[:, :80, 2] = np.clip(70 + xx[:, :80] * 0.9, 0, 255).astype(np.uint8)
        wave = np.clip(128 + 80 * np.sin(xx[:, 80:] * 0.25 - i * 0.9), 0, 255).astype(np.uint8)
        f[:, 80:, 0] = wave
        f[:, 80:, 1] = wave
        f[:, 80:, 2] = wave
        frames.append(f)
    arr = np.stack(frames, axis=0)

    dith = apply_selective_blue_noise_dither(arr)
    # Moving right half must be untouched (0 diff except soft feather at x=80 border)
    right_diff = float(np.abs(dith[:, :, 92:].astype(np.float32) - arr[:, :, 92:].astype(np.float32)).mean())
    left_diff = float(np.abs(dith[:, 8:-8, 8:72].astype(np.float32) - arr[:, 8:-8, 8:72].astype(np.float32)).mean())
    assert right_diff < 0.05, f"Blue-noise dither leaked into moving region: {right_diff:.4f}"
    assert left_diff > 0.20, f"Blue-noise dither did not touch static smooth gradient: {left_diff:.4f}"

    # Strictly monotonic finish gate (Intervention 07 / CZAR-ABS-AUD-004): never increases issue count!
    fin = apply_deluxe_finish(arr, 0.1)
    assert len(fin["finalPostDds"]["issues"]) <= len(fin["baselinePostDds"]["issues"])


# ==================== CHECKPOINT E: EFFICIENCY & LEAN PAYLOAD ====================


def test_checkpoint_e_lean_http_payload_vs_full_opt_in(tmp_path, monkeypatch):
    """Checkpoint E: GET /api/ouroboros/result/{key} omits heavy profileReports by default and includes them on ?full=1."""
    import json
    from fastapi.testclient import TestClient
    from app import main, projects
    from app.ouroboros_engine import OUROBOROS_VERSION

    monkeypatch.setattr(projects, "ROOT", tmp_path / "projects")
    client = TestClient(main.app)
    assert client.get("/api/projects").status_code == 200
    pid = client.post("/api/projects", json={"name": "Slim Payload"}).json()["id"]
    key = "a" * 64
    proj_dir = next((tmp_path / "projects").rglob(pid))
    res_dir = proj_dir / "ouroboros-v1" / key
    res_dir.mkdir(parents=True)
    dummy = {
        "type": "ouroboros_proposals",
        "version": OUROBOROS_VERSION,
        "key": key,
        "proposals": [{"profileId": "quality"}, {"profileId": "balance"}, {"profileId": "flexibility"}],
        "profileReports": {"quality": {"blob": "x" * 20000}, "balance": {"blob": "y" * 20000}},
    }
    (res_dir / "report.json").write_text(json.dumps(dummy), encoding="utf-8")

    r_slim = client.get(f"/api/ouroboros/result/{key}?project={pid}")
    r_full = client.get(f"/api/ouroboros/result/{key}?project={pid}&full=1")
    assert r_slim.status_code == 200 and r_full.status_code == 200
    assert "profileReports" not in r_slim.json()
    assert "profileReports" in r_full.json()
    assert len(r_slim.content) < len(r_full.content) * 0.2


# ==================== CHECKPOINT F: PILAR VII UI & PILAR VIII 7 ROBUSTNESS FIXES ====================


def test_pilar_vii_proposal_titles_and_pilar_viii_fixes_001_003_005_009_010_011(tmp_path, monkeypatch):
    """Verify Pilar VII proposal titles and Pilar VIII fixes CZAR-ABS-AUD-001, 002, 003, 005, 009, 010, 011."""
    import json
    from pathlib import Path
    import pytest
    from PIL import Image
    from fastapi.testclient import TestClient
    from app import dxt1, main, ouroboros, projects
    from app.ouroboros_engine import OUROBOROS_VERSION, PROPOSAL_PROFILES
    from app.ouroboros_engine.applier import apply_loop_to_project, assemble_canonical_raf, sha256_bytes

    # Pilar VII exact proposal titles
    titles = [p["title"] for p in PROPOSAL_PROFILES]
    assert titles == [
        "PROPOSTA 1 — FIDELIDADE MÁXIMA — 720p",
        "PROPOSTA 2 — MOVIMENTO NATURAL — 540p",
        "PROPOSTA 3 — CONTINUIDADE TEMPORAL — 360p",
    ]

    # CZAR-ABS-AUD-001: tampered 00.webp or background.raf.zip must be rejected before mutating project
    res_folder = tmp_path / "render_res"
    (res_folder / "deluxe_dds").mkdir(parents=True)
    (res_folder / "deluxe_frames").mkdir(parents=True)
    dds_hashes = {}
    file_hashes = {}
    blobs = []
    for i in range(4):
        rgb = np.full((360, 640, 3), 40 + i * 20, dtype=np.uint8)
        rgba = np.concatenate([rgb, np.full((360, 640, 1), 255, dtype=np.uint8)], axis=-1)
        blob = dxt1.encode_dds(rgba)
        blobs.append(blob)
        dname = f"{i:02d}.dds"
        wname = f"{i:02d}.webp"
        (res_folder / "deluxe_dds" / dname).write_bytes(blob)
        dds_hashes[dname] = sha256_bytes(blob)
        Image.fromarray(rgb).save(res_folder / "deluxe_frames" / f"{i:02d}.png", "PNG")
        Image.fromarray(rgb).save(res_folder / wname, "WEBP", lossless=True)
        file_hashes[wname] = sha256_bytes((res_folder / wname).read_bytes())

    raf_zip = res_folder / "background.raf.zip"
    assemble_canonical_raf(blobs, 0.1, raf_zip)
    file_hashes["background.raf.zip"] = sha256_bytes(raf_zip.read_bytes())

    rep = {
        "frames": 4,
        "size": [640, 360],
        "wait": 0.1,
        "ddsHashes": dds_hashes,
        "files": file_hashes,
        "renderKey": "b" * 64,
    }
    monkeypatch.setattr(projects, "ROOT", tmp_path / "projects")
    client = TestClient(main.app)
    assert client.get("/api/projects").status_code == 200
    pid = client.post("/api/projects", json={"name": "Reload & Cancel"}).json()["id"]
    pdir = next((tmp_path / "projects").rglob(pid))

    # Tamper 00.webp -> must raise ValueError!
    orig_webp = (res_folder / "00.webp").read_bytes()
    (res_folder / "00.webp").write_bytes(orig_webp + b"corrupt")
    with pytest.raises(ValueError, match="00.webp"):
        apply_loop_to_project(pdir, res_folder, rep, acquire_lock=False)
    (res_folder / "00.webp").write_bytes(orig_webp)

    # Tamper background.raf.zip -> must raise ValueError!
    orig_raf = raf_zip.read_bytes()
    raf_zip.write_bytes(orig_raf + b"corrupt")
    with pytest.raises(ValueError, match="background.raf.zip"):
        apply_loop_to_project(pdir, res_folder, rep, acquire_lock=False)
    raf_zip.write_bytes(orig_raf)

    # Clean apply succeeds
    ap_ok = apply_loop_to_project(pdir, res_folder, rep, acquire_lock=False)
    assert ap_ok["applied"] is True

    # CZAR-ABS-AUD-010: _sanitize_media_error strips internal paths and memory pointers
    s_err = main._sanitize_media_error(
        "/home/antigravity/.gemini/studio-projects-v1/abc/media/source.mp4: Invalid data found when processing input at 0x7ff01234abcd"
    )
    assert "/studio-projects-v1/" not in s_err
    assert "/home/" not in s_err
    assert "0x7ff" not in s_err

    # CZAR-ABS-AUD-003 & 009: /api/ouroboros/current returns cachedResultKey & appliedRenderKey
    ckey = "c" * 64
    cdir = pdir / "ouroboros-v1" / ckey
    cdir.mkdir(parents=True)
    (cdir / "report.json").write_text(
        json.dumps({"type": "ouroboros_proposals", "version": OUROBOROS_VERSION, "key": ckey, "proposals": []}),
        encoding="utf-8",
    )
    cur_r = client.get(f"/api/ouroboros/current?project={pid}")
    assert cur_r.status_code == 200
    assert cur_r.json()["cachedResultKey"] == ckey
    assert cur_r.json()["appliedRenderKey"] == "b" * 64

    # CZAR-ABS-AUD-002, 005, 011: UI static checks
    repo = Path(__file__).resolve().parents[1]
    css = (repo / "app" / "static" / "style.css").read_text(encoding="utf-8")
    html = (repo / "app" / "static" / "index.html").read_text(encoding="utf-8")
    js = (repo / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "min-height: 44px" in css
    assert "ouroboros-region-preset" in html
    assert "_clearAppliedProposalBadge" in js
    assert "syncOuroborosCurrentState" in js


def test_checkpoint_g_banding_macro_turn_filter_and_cel_dedup():
    """Checkpoint G: macroscopic texture-masked banding index, active-quartile flow coherence & cross-pool scaling."""
    from app.ouroboros_engine.metrics import _banding_index
    from app.ouroboros_engine.proposals import _score_candidate_for_profile
    from app.ouroboros_engine.synthesis import estimate_2d_flow_field

    # 1. Clean smooth ramp must score near 0 while truly posterized ramp scores high
    x = np.linspace(20, 220, 256, dtype=np.float32)[None, :, None]
    clean_ramp = np.broadcast_to(x, (160, 256, 3)).astype(np.uint8)
    banded_ramp = ((clean_ramp // 14) * 14).astype(np.uint8)
    assert _banding_index(clean_ramp) < 0.05
    assert _banding_index(banded_ramp) > 0.50

    # 2. Active-quartile temporalCoherence works even when 80% of the frame is stationary night sky
    frames = []
    yy, xx = np.mgrid[0:160, 0:240].astype(np.float32)
    for t in range(8):
        img = np.full((160, 240, 3), 14, dtype=np.uint8)
        # Moving cloud band in bottom 22% of frame
        cloud = 90.0 + 45.0 * np.sin(2.0 * np.pi * (xx - t * 3.0) / 48.0)
        img[124:, :, :] = np.clip(cloud[124:, :, None], 0, 255).astype(np.uint8)
        frames.append(img)
    _, stats = estimate_2d_flow_field(np.stack(frames))
    assert stats["temporalCoherence"] >= 0.65, stats

    # 3. Cross-pool upscaling from sparse 10f to 24f cannot unfairly beat native 24f on identical seamRatio
    base_costs = {
        "totalCost": 4.0,
        "seamRatio": 1.05,
        "seamSsim": 0.90,
        "internalMedianSsim": 0.92,
        "seamDirectionCosine": 0.85,
        "velocityJump": 0.1,
        "accelRatio": 1.1,
        "hasHardDefect": False,
    }
    native_cand = {"start": 1.0, "end": 3.0, "frames": 24, "wait": 0.083, "duration": 2.0, "costs": base_costs}
    cross_cand = dict(native_cand, _crossPool=True, _origFrames=10)
    assert _score_candidate_for_profile(native_cand, "balance") > _score_candidate_for_profile(cross_cand, "balance")

