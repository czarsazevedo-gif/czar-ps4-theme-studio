"""Checkpoint 1 scientific verification: Sentinela Supreme (A–E), Círculo Supreme, 16 Metrics & Scenarios A–R."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app import media
from app.config import ANIMATED_SIZES, RAF_SIZE_LIMIT, max_frames_for
from app.deluxe.circulo_supreme import compute_multicriteria_cost, duration_penalty, search_supreme
from app.deluxe.metrics import compare_before_after, evaluate_loop, evaluate_post_dds
from app.deluxe.sentinela_supreme import (
    CLASS_CORRECTABLE,
    CLASS_NATURAL,
    CLASS_REGIONAL,
    CLASS_SYNTHESIS,
    CLASS_UNSUITABLE,
    analyze_levels,
    probe_source_extended,
)
from tests import fixtures_deluxe as fx


class Checkpoint1ScientificSuite(unittest.TestCase):
    def test_scenario_a_unidirectional_river(self):
        frames = fx.scenario_a_river(16)
        ev = evaluate_loop(frames, wait=0.1)
        self.assertTrue(ev["passed"], ev["issues"])
        self.assertFalse(ev["metrics"]["reversals"]["detected"])
        self.assertGreater(ev["metrics"]["flowContinuity"]["seamDirectionCosine"], 0.75)
        self.assertLess(ev["metrics"]["visualSimilarity"]["seamRatio"], 1.35)

    def test_scenario_b_multispeed_clouds(self):
        frames = fx.scenario_b_clouds(24)
        ev = evaluate_loop(frames, wait=0.05)
        self.assertTrue(ev["passed"], ev["issues"])
        sent = analyze_levels(frames, dt=0.05)
        self.assertGreater(sent["levelB"]["progressiveFraction"], 0.5)
        self.assertNotEqual(sent["classification"], CLASS_UNSUITABLE)

    def test_scenario_c_perfect_periodic_and_d_quasi_periodic(self):
        perfect = fx.scenario_c_periodic(16)
        quasi = fx.scenario_d_quasi_periodic(16)
        ev_c = evaluate_loop(perfect, wait=0.1)
        ev_d = evaluate_loop(quasi, wait=0.1)
        self.assertTrue(ev_c["passed"], ev_c["issues"])
        self.assertGreater(ev_d["metrics"]["visualSimilarity"]["seamMae"], ev_c["metrics"]["visualSimilarity"]["seamMae"])
        self.assertGreater(abs(ev_d["metrics"]["luminanceContinuity"]["seamDeltaL"]), 3.0)
        sent_c = analyze_levels(np.concatenate([perfect, perfect], axis=0), dt=0.1)
        self.assertTrue(sent_c["levelD"]["trueRecurrence"])
        self.assertEqual(sent_c["levelD"]["periodSamples"], 16)
        self.assertEqual(sent_c["classification"], CLASS_NATURAL)

    def test_scenario_e_non_periodic_rejected(self):
        frames = fx.scenario_e_non_periodic(20)
        ev = evaluate_loop(frames, wait=0.1)
        self.assertFalse(ev["passed"])
        costs = compute_multicriteria_cost(ev, duration_sec=2.0, source_duration=2.0, source_fps=10.0, wait=0.1)
        self.assertNotEqual(costs["classification"], CLASS_NATURAL)

    def test_scenario_f_ping_pong_rejected(self):
        frames = fx.scenario_f_ping_pong(24)
        ev = evaluate_loop(frames, wait=0.1)
        self.assertTrue(ev["metrics"]["reversals"]["detected"], ev["metrics"]["reversals"])
        self.assertFalse(ev["passed"])
        sent = analyze_levels(frames, dt=0.1)
        self.assertTrue(sent["levelD"]["reverseMotion"])
        self.assertNotEqual(sent["classification"], CLASS_NATURAL)

    def test_scenario_g_abrupt_cut_detected(self):
        frames = fx.scenario_g_abrupt_cut(16)
        ev = evaluate_loop(frames, wait=0.1)
        self.assertTrue(ev["metrics"]["cuts"]["detected"])
        self.assertFalse(ev["passed"])
        sent = analyze_levels(frames, dt=0.1)
        self.assertTrue(sent["levelA"]["cuts"])

    def test_scenario_h_natural_pulse_vs_artificial_flicker(self):
        h = fx.scenario_h_illumination(16)
        ev_nat = evaluate_loop(h["natural_pulse"], wait=0.1)
        ev_flk = evaluate_loop(h["artificial_flicker"], wait=0.1)
        self.assertFalse(ev_nat["metrics"]["flicker"]["artificialFlicker"])
        self.assertTrue(ev_flk["metrics"]["flicker"]["artificialFlicker"])
        self.assertGreater(ev_flk["metrics"]["flicker"]["score"], ev_nat["metrics"]["flicker"]["score"] * 4.0)

    def test_scenario_i_transient_object_detected(self):
        frames = fx.scenario_i_transient_object(16)
        ev = evaluate_loop(frames, wait=0.1)
        self.assertFalse(ev["passed"])
        self.assertGreater(ev["metrics"]["edgeConsistency"]["localEdgeExcess"], 1.8)
        self.assertGreater(ev["metrics"]["visualSimilarity"]["localSeamExcess"], 1.8)

    def test_scenario_j_occlusions_measured(self):
        frames = fx.scenario_j_occlusions(16)
        ev = evaluate_loop(frames, wait=0.1)
        self.assertGreater(ev["metrics"]["flowContinuity"]["meanOcclusionFraction"], 0.01)

    def test_scenario_k_static_dynamic_and_protected_mask(self):
        frames, pmask = fx.scenario_k_static_dynamic(16)
        ev = evaluate_loop(frames, wait=0.1, protected_mask=pmask)
        self.assertTrue(ev["metrics"]["staticIntegrity"]["checked"])
        self.assertEqual(ev["metrics"]["staticIntegrity"]["maxAbsDiff"], 0.0)
        sent = analyze_levels(frames, dt=0.1)
        self.assertTrue(sent["levelB"]["hasMixedStaticDynamic"])
        self.assertGreater(sent["levelB"]["staticFraction"], 0.30)
        self.assertGreater(sent["levelB"]["progressiveFraction"], 0.25)

        # Mutate protected region -> must be detected!
        corrupted = frames.copy()
        corrupted[5, 5:15, 5:15] = 250
        ev_bad = evaluate_loop(corrupted, wait=0.1, protected_mask=pmask)
        self.assertGreater(ev_bad["metrics"]["staticIntegrity"]["maxAbsDiff"], 10.0)
        self.assertFalse(ev_bad["passed"])

    def test_scenario_l_independent_motions(self):
        frames = fx.scenario_l_independent_motions(24)
        sent = analyze_levels(frames, dt=0.05)
        self.assertTrue(sent["levelB"]["hasMixedStaticDynamic"])
        self.assertTrue(sent["levelB"]["hasIndependentSpeeds"])
        self.assertGreaterEqual(len(sent["levelB"]["regions"]), 3)

    def test_scenario_m_fixed_camera_vs_n_moving_camera(self):
        fixed = fx.scenario_m_fixed_camera(16)
        moving = fx.scenario_n_moving_camera(16)
        sent_m = analyze_levels(fixed, dt=0.1)
        sent_n = analyze_levels(moving, dt=0.1)
        self.assertFalse(sent_m["levelA"]["cameraMoving"])
        self.assertTrue(sent_n["levelA"]["cameraMoving"])
        self.assertGreater(sent_n["levelC"]["cameraMotion"]["inlierRatio"], 0.65)

    def test_scenario_o_subpixel_motion(self):
        frames = fx.scenario_o_subpixel(20)
        sent = analyze_levels(frames, dt=0.1)
        self.assertTrue(sent["levelC"]["subpixel"]["detected"])
        self.assertAlmostEqual(sent["levelC"]["subpixel"]["medianShiftPx"], 0.35, delta=0.18)
        self.assertNotEqual(sent["classification"], CLASS_UNSUITABLE)

    def test_scenario_p_vfr_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            mp4 = fx.scenario_p_vfr_mp4(Path(tmp) / "vfr.mp4")
            info = probe_source_extended(mp4, media.probe(mp4).duration)
            self.assertTrue(info["isVFR"], info)
            self.assertGreater(info["vfrMaxDeviation"], 0.15)
            self.assertGreaterEqual(info["timestampCount"], 15)

    def test_scenario_q_motion_blur(self):
        frames = fx.scenario_q_motion_blur(16)
        ev = evaluate_loop(frames, wait=0.1)
        self.assertTrue(ev["passed"], ev["issues"])
        self.assertGreater(ev["metrics"]["flowContinuity"]["seamDirectionCosine"], 0.65)

    def test_scenario_r_gradients_post_dds_banding(self):
        frames = fx.scenario_r_gradients_banding(8, size=(640, 360))
        post = evaluate_post_dds(frames, wait=0.1)
        dds_info = post["metrics"]["ddsDelta"]
        self.assertTrue(dds_info["withinBudget"])
        self.assertTrue(dds_info["validHeaders"])
        self.assertEqual(post["evaluatedOn"], "decoded_dds_native_resolution")
        self.assertGreater(dds_info["bandingPostDds"], dds_info["bandingPreDds"])
        self.assertLess(dds_info["totalBytes"], RAF_SIZE_LIMIT)

    def test_static_scene_never_approved(self):
        static = np.stack([fx.scenario_c_periodic(4)[0]] * 12)
        ev = evaluate_loop(static, wait=0.1)
        self.assertFalse(ev["passed"])
        sent = analyze_levels(static, dt=0.1)
        self.assertEqual(sent["classification"], CLASS_UNSUITABLE)

    def test_duration_penalty_prevents_short_take_dominance(self):
        self.assertGreater(duration_penalty(0.4, 5.0), duration_penalty(2.4, 5.0) + 1.5)

    def test_before_after_rejects_regression(self):
        good = fx.scenario_c_periodic(16)
        # Artificially blur/ghost the seam frame -> must be rejected!
        bad = good.copy()
        bad[-1] = ((good[-2].astype(np.float32) + good[0].astype(np.float32)) * 0.5).astype(np.uint8)
        bad[0] = ((good[-1].astype(np.float32) + good[1].astype(np.float32)) * 0.5).astype(np.uint8)
        ev_good = evaluate_loop(good, wait=0.1)
        ev_bad = evaluate_loop(bad, wait=0.1)
        cmp_res = compare_before_after(ev_good, ev_bad)
        self.assertFalse(cmp_res["accepted"], cmp_res)

    def test_circulo_supreme_full_duration_and_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            # 16s video: 8s static, then 8s periodic river (period 1.6s at 10fps)
            river = fx.scenario_c_periodic(16)
            static = np.stack([river[0]] * 80)
            seq = np.concatenate([static, river, river, river, river, river], axis=0)[:160]
            movie = fx.write_mp4(folder / "long_periodic.mp4", seq, fps=10.0)
            res = search_supreme(movie, media.probe(movie).duration, folder, source_sha256="a" * 64)
            self.assertEqual(len(res["groups"]), 3)
            for g in res["groups"]:
                sz = tuple(g["size"])
                self.assertIn(sz, ANIMATED_SIZES)
                self.assertEqual(g["maxFrames"], max_frames_for(sz))
                for c in g["candidates"]:
                    self.assertLessEqual(c["frames"], max_frames_for(sz))
                    self.assertLessEqual(c["estimatedBytes"], RAF_SIZE_LIMIT)
                    self.assertIn("recipe", c)
            # 640x360 & 960x540 must discover natural loop in the dynamic second half (>= 7.5s)
            cats = res["categories"]
            self.assertIsNotNone(cats["best_natural"], res["groups"])
            self.assertGreaterEqual(cats["best_natural"]["start"], 7.0)
            self.assertIsNotNone(cats["best_convincing_duration"])
            self.assertIsNotNone(cats["best_balance"])

    def test_real_examples_lua_sintetica_and_sample(self):
        for rel in ("docs/examples/genesis/lua-sintetica.mp4", "tests/sample.mp4"):
            p = Path(rel)
            if not p.is_file():
                continue
            with tempfile.TemporaryDirectory() as tmp:
                res = search_supreme(p, media.probe(p).duration, Path(tmp), source_sha256="b" * 64, shortlist_per_res=6)
                self.assertEqual(len(res["groups"]), 3)
                self.assertIn(
                    res["sentinela"]["classification"],
                    {CLASS_NATURAL, CLASS_CORRECTABLE, CLASS_SYNTHESIS, CLASS_REGIONAL, CLASS_UNSUITABLE},
                )
                if p.name == "lua-sintetica.mp4":
                    # Must detect spatial repetition and recommend synthesis!
                    self.assertTrue(res["sentinela"]["levelD"]["spatialRepetition"]["detected"])
                    self.assertIn(res["sentinela"]["classification"], {CLASS_SYNTHESIS, CLASS_REGIONAL})
                    self.assertIsNotNone(res["categories"]["best_synthesized"])


if __name__ == "__main__":
    unittest.main()
