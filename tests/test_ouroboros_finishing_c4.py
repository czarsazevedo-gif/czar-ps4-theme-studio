"""Checkpoint 4 tests — Post-DDS finishing, decoded-RAF evaluation, prohibited artifacts & Scenarios A-P."""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.ouroboros_engine import OuroborosEngine
from app.ouroboros_engine.analyzer import analyze_levels, probe_source_extended
from app.ouroboros_engine.finish import apply_deluxe_finish
from app.ouroboros_engine.metrics import (
    check_prohibited_artifacts,
    classify_loop_verdict,
    evaluate_loop,
    evaluate_post_dds,
    evaluate_raf_zip,
)
from app.ouroboros_engine.reconstruction import reconstruct_best
from app.ouroboros_engine.synthesis import SynthesisRejected, synthesize_genesis_supreme
from tests.fixtures_deluxe import (
    scenario_a_river,
    scenario_b_clouds,
    scenario_e_non_periodic,
    scenario_f_ping_pong,
    scenario_g_abrupt_cut,
    scenario_h_illumination,
    scenario_i_transient_object,
    scenario_j_occlusions,
    scenario_k_static_dynamic,
    scenario_n_moving_camera,
    scenario_p_vfr_mp4,
    scenario_r_gradients_banding,
)

REPO = Path(__file__).resolve().parents[1]
CORPUS_DIR = Path("/tmp/czar-real-corpus")


def test_prohibited_artifacts_reject_ping_pong_freeze_and_static():
    # Ping-pong MUST NEVER be approved
    pp = scenario_f_ping_pong(16)
    ev_pp = evaluate_post_dds(pp, 0.1)
    ver_pp = classify_loop_verdict(ev_pp)
    assert ver_pp["verdict"] == "REPROVADO"
    assert ver_pp["approved"] is False

    # Static scene MUST NEVER be approved and synthesis MUST reject it
    st = np.full((12, 90, 160, 3), 120, dtype=np.uint8)
    ev_st = evaluate_post_dds(st, 0.1)
    assert classify_loop_verdict(ev_st)["approved"] is False
    with pytest.raises(SynthesisRejected):
        synthesize_genesis_supreme(st, n_frames=12, wait=0.1)

    # Disguised freeze (repeating identical frame) MUST be flagged as prohibited
    river = scenario_a_river(12)
    frozen = river.copy()
    frozen[5] = frozen[4]
    frozen[6] = frozen[4]
    ev_fz = evaluate_loop(frozen, 0.1)
    proh = check_prohibited_artifacts(ev_fz)
    assert proh["clean"] is False


def test_progressive_river_clouds_static_protected_and_decoded_raf_verification(tmp_path):
    """Verify progressive forward motion is preserved (no reversal), protected static mountain stays crisp,
    and decoded background.raf.zip is independently verified post-DXT1a."""
    river = scenario_a_river(12)
    res = reconstruct_best(river, wait=0.1, verify_dds=True)
    fin = apply_deluxe_finish(res["frames"], 0.1, reference_frames=river)
    ev = fin["finalPostDds"]
    assert ev["metrics"]["reversals"]["detected"] is False
    assert ev["metrics"]["flowContinuity"]["seamDirectionCosine"] >= 0.45

    # Static mountain + dynamic water protection
    scene, static_mask = scenario_k_static_dynamic(12)
    res_sd = reconstruct_best(scene, wait=0.1, protected_mask=static_mask, verify_dds=True)
    ev_sd = evaluate_post_dds(res_sd["frames"], 0.1, protected_mask=static_mask)
    assert ev_sd["metrics"]["staticIntegrity"]["checked"] is True
    assert ev_sd["metrics"]["staticIntegrity"]["maxAbsDiff"] <= 6.0

    # Render through unified orchestrator and independently decode & evaluate background.raf.zip!
    engine = OuroborosEngine()
    out_dir = tmp_path / "raf_eval"
    rend = engine.render_loop(
        REPO / "tests" / "sample.mp4",
        2.0,
        out_dir,
        start=0.0,
        end=1.2,
        frames=12,
        size=(640, 360),
        wait=0.1,
        mode="auto",
        finish_enabled=True,
        preextracted_frames=np.stack([cv2.resize(f, (640, 360), interpolation=cv2.INTER_CUBIC) for f in river]),
    )
    assert rend["verdict"] in {"APROVADO", "CONDICIONAL", "REPROVADO"}
    raf_eval = evaluate_raf_zip(out_dir / "background.raf.zip")
    assert raf_eval["evaluatedFromRafZip"] is True
    assert raf_eval["frames"] == 12
    assert raf_eval["withinBudget"] is True
    assert raf_eval["size"] == [640, 360]


def test_scenarios_i_j_l_m_n_o_nonperiodic_camera_transient_occlusion_lighting_vfr(tmp_path):
    # Non-periodic linear scene must NOT be falsified as natural periodic
    np_seq = scenario_e_non_periodic(14)
    sent_np = analyze_levels(np_seq, dt=0.1)
    assert sent_np["levelD"]["trueRecurrence"] is False

    # Moving camera pan detected
    cam = scenario_n_moving_camera(14)
    sent_cam = analyze_levels(cam, dt=0.1)
    assert sent_cam["levelA"]["cameraMoving"] is True

    # Transient object (Scenario L) and occlusion (Scenario M) measured by loop evaluator
    tr = scenario_i_transient_object(16)
    ev_tr = evaluate_loop(tr, 0.1)
    assert ev_tr["passed"] is False
    assert ev_tr["metrics"]["visualSimilarity"]["localSeamExcess"] > 1.8
    assert ev_tr["metrics"]["edgeConsistency"]["localEdgeExcess"] > 1.8

    occ = scenario_j_occlusions(16)
    ev_occ = evaluate_loop(occ, 0.1)
    assert ev_occ["metrics"]["flowContinuity"]["meanOcclusionFraction"] > 0.01

    cut = scenario_g_abrupt_cut(14)
    sent_cut = analyze_levels(cut, dt=0.1)
    assert len(sent_cut["levelA"]["cuts"]) >= 1

    illum = scenario_h_illumination(16)
    flick = illum["artificial_flicker"]
    pulse = illum["natural_pulse"]
    fin_f = apply_deluxe_finish(flick, 0.1)
    fin_p = apply_deluxe_finish(pulse, 0.1)
    assert fin_f["finalPostDds"]["metrics"]["flicker"]["score"] <= evaluate_post_dds(flick, 0.1)["metrics"]["flicker"]["score"] + 0.1
    assert fin_p["finalPostDds"]["metrics"]["reversals"]["detected"] is False

    grad = scenario_r_gradients_banding(8)
    fin_g = apply_deluxe_finish(grad, 0.1)
    assert fin_g["finalPostDds"]["metrics"]["flicker"]["artificialFlicker"] is False

    clouds = scenario_b_clouds(12)
    res_c = reconstruct_best(clouds, 0.1, verify_dds=True)
    assert res_c["frames"].shape == clouds.shape

    vfr_mp4 = scenario_p_vfr_mp4(tmp_path / "vfr.mp4")
    vfr_info = probe_source_extended(vfr_mp4, 4.0)
    assert vfr_info["isVFR"] is True


@pytest.mark.skipif(not (CORPUS_DIR / "dev_river.webm").is_file(), reason="Real Wikimedia corpus not present in /tmp")
def test_real_corpus_river_smoke_waves_post_dds_raf(tmp_path):
    """Run unified Ouroboros finishing + decoded-RAF audit on real Wikimedia river, smoke, and waves clips."""
    engine = OuroborosEngine()
    for clip in ("dev_river.webm", "holdout_smoke.webm", "holdout_waves.webm"):
        p = CORPUS_DIR / clip
        if not p.is_file():
            continue
        info = engine.inspect_source(p, 30.0)
        out_dir = tmp_path / p.stem
        rend = engine.render_loop(
            p,
            float(info["duration"]),
            out_dir,
            start=0.2,
            end=min(float(info["duration"]) - 0.1, 1.5),
            frames=10,
            size=(640, 360),
            wait=0.1,
            mode="auto",
            finish_enabled=True,
        )
        raf_check = evaluate_raf_zip(out_dir / "background.raf.zip")
        assert raf_check["withinBudget"] is True
        assert raf_check["frames"] == 10
        assert rend["prohibitedArtifacts"] is not None
        assert "sourceIssues" in rend["issueAttribution"]


@pytest.mark.skipif(
    not os.environ.get("CZAR_ACCEPTANCE_VIDEO") or not Path(os.environ.get("CZAR_ACCEPTANCE_VIDEO", "")).is_file(),
    reason="Defina CZAR_ACCEPTANCE_VIDEO=/caminho/video.mp4 para validar mídia privada do proprietário",
)
def test_optional_owner_private_video_hook(tmp_path):
    vid = Path(os.environ["CZAR_ACCEPTANCE_VIDEO"])
    engine = OuroborosEngine()
    info = engine.inspect_source(vid, 60.0)
    an = engine.analyze_and_search(vid, float(info["duration"]), tmp_path / "an", filename=vid.name)
    assert len(an["groups"]) == 3
