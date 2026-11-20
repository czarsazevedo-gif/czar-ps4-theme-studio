"""Checkpoint 2 scientific verification: Gênesis Supreme 2D & Ouroboros Supreme 6-level reconstruction."""

from __future__ import annotations

import unittest
from pathlib import Path

import cv2
import numpy as np

from app import media, ouroboros_media as io_media
from app.deluxe.genesis_supreme import SynthesisRejected, synthesize_genesis_supreme
from app.deluxe.metrics import evaluate_loop, evaluate_post_dds
from app.deluxe.reconstructor import (
    level2_photometric_harmonize,
    level3_motion_compensated_seam,
    level4_content_bridge_reconstruct,
    level6_multi_region_reconstruct,
    reconstruct_best,
    remove_transient_region,
    stabilize_camera_sequence,
)
from app.deluxe.sentinela_supreme import analyze_levels
from tests import fixtures_deluxe as fx


class Checkpoint2ReconstructionSuite(unittest.TestCase):
    def test_level1_does_not_reconstruct_already_clean_natural_loop(self):
        clean = fx.scenario_c_periodic(16)
        res = reconstruct_best(clean, wait=0.1, verify_dds=False)
        self.assertEqual(res["selectedStrategy"], "level1_natural_cut")
        self.assertTrue(res["alreadyOptimal"])
        self.assertTrue(np.array_equal(res["frames"], clean))

    def test_level2_photometric_fixes_drift_after_dds(self):
        quasi = fx.scenario_d_quasi_periodic(16, size=(320, 180))
        before = evaluate_post_dds(quasi, wait=0.1)
        fixed = level2_photometric_harmonize(quasi)
        after = evaluate_post_dds(fixed, wait=0.1)
        self.assertLess(
            abs(after["metrics"]["luminanceContinuity"]["seamDeltaL"]),
            abs(before["metrics"]["luminanceContinuity"]["seamDeltaL"]) * 0.45,
        )
        self.assertLess(
            after["metrics"]["visualSimilarity"]["seamMae"],
            before["metrics"]["visualSimilarity"]["seamMae"],
        )

    def test_level3_motion_compensation_preserves_protected_mask(self):
        frames, pmask = fx.scenario_k_static_dynamic(16)
        # Introduce a slight phase mismatch at the tail so seam needs flow alignment
        shifted = frames.copy()
        shifted[8:, 45:, :] = np.roll(shifted[8:, 45:, :], 3, axis=1)
        repaired, meta = level3_motion_compensated_seam(shifted, protected_mask=pmask)
        self.assertTrue(meta["applied"], meta)
        ev = evaluate_post_dds(repaired, wait=0.1, protected_mask=pmask)
        self.assertEqual(ev["metrics"]["staticIntegrity"]["maxAbsDiff"], 0.0)
        self.assertFalse(ev["metrics"]["reversals"]["detected"])

    def test_level4_content_piece_bridge_unidirectional_after_dds(self):
        # Generate a 24-frame non-periodic unidirectional river stream (wide unique texture)
        w, h = 240, 136
        wide = fx._smooth_noise_texture(h, w + 120, seed=404)
        stream = np.stack([wide[:, i * 3 : i * 3 + w].copy() for i in range(24)])
        # Main clip = frames 6..22 (16 frames), lead_in = frames 0..6 (real preceding content pieces!)
        clip = stream[6:22]
        lead_in = stream[0:6]
        before = evaluate_post_dds(clip, wait=0.066667)
        bridged, meta = level4_content_bridge_reconstruct(clip, lead_in=lead_in)
        after = evaluate_post_dds(bridged, wait=0.066667)
        self.assertTrue(meta["applied"])
        self.assertEqual(meta["bridgeType"], "lead_in_content_pieces")
        self.assertFalse(after["metrics"]["reversals"]["detected"])
        self.assertLess(
            after["metrics"]["visualSimilarity"]["seamMae"],
            before["metrics"]["visualSimilarity"]["seamMae"] * 0.72,
        )
        self.assertGreater(
            after["metrics"]["visualSimilarity"]["seamSsim"],
            before["metrics"]["visualSimilarity"]["seamSsim"],
        )

    def test_level4_transient_object_removal_and_camera_stabilization(self):
        # Transient removal on Scenario I
        trans = fx.scenario_i_transient_object(16)
        h, w = trans.shape[1:3]
        # Build mask covering the horizontal band where the red circle crossed
        tmask = np.zeros((h, w), dtype=bool)
        tmask[h // 2 - 14 : h // 2 + 15, int(w * 0.15) : int(w * 0.95)] = True
        before_i = evaluate_loop(trans, wait=0.1)
        cleaned = remove_transient_region(trans, tmask)
        after_i = evaluate_loop(cleaned, wait=0.1)
        self.assertLess(
            after_i["metrics"]["visualSimilarity"]["localSeamExcess"],
            before_i["metrics"]["visualSimilarity"]["localSeamExcess"] * 0.55,
        )

        # Camera stabilization on Scenario N (moving camera)
        pan = fx.scenario_n_moving_camera(12)
        stab, smeta = stabilize_camera_sequence(pan)
        self.assertTrue(smeta["applied"])
        ev_stab = evaluate_loop(stab, wait=0.1)
        self.assertEqual(ev_stab["metrics"]["reconstructionArtifacts"]["emptyBorderFraction"], 0.0)
        # Stabilized sequence has significantly lower inter-frame displacement than raw pan
        ev_raw = evaluate_loop(pan, wait=0.1)
        self.assertLess(
            ev_stab["metrics"]["velocityContinuity"]["internalMedianSpeedPx"],
            ev_raw["metrics"]["velocityContinuity"]["internalMedianSpeedPx"] * 0.65,
        )

    def test_genesis_supreme_freeform_mask_2d_diagonal_flow_and_dds_protection(self):
        # Non-periodic diagonal flowing water inside an irregular freeform ellipse, surrounded by static frame
        w, h = 320, 180
        tex = fx._smooth_noise_texture(h, w, seed=909)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        ellipse = (((xx - w * 0.55) / (w * 0.32)) ** 2 + ((yy - h * 0.60) / (h * 0.30)) ** 2) <= 1.0
        protected = ~ellipse

        # Synthesize 8 source frames with diagonal (+2.0, +1.0) flow ONLY inside ellipse
        src = []
        for i in range(8):
            moved = fx._shift_subpixel(tex, i * 2.0, i * 1.0, wrap=True)
            f = tex.copy()
            f[ellipse] = moved[ellipse]
            src.append(f)
        src_arr = np.stack(src)

        res = synthesize_genesis_supreme(
            src_arr,
            n_frames=16,
            wait=0.05,
            mask=ellipse,
            protected_mask=protected,
            verify_dds=True,
        )
        ev = res["evaluation"]
        self.assertEqual(ev["evaluatedOn"], "decoded_dds_native_resolution")
        self.assertEqual(ev["metrics"]["staticIntegrity"]["maxAbsDiff"], 0.0)
        self.assertFalse(ev["metrics"]["reversals"]["detected"])
        self.assertFalse(ev["metrics"]["freezes"]["exactDuplicates"])
        self.assertEqual(ev["metrics"]["ddsDelta"]["distinctFrames"], 16)
        self.assertLess(ev["metrics"]["visualSimilarity"]["seamRatio"], 1.45)

    def test_genesis_supreme_honestly_rejects_static_and_ping_pong(self):
        static = np.stack([fx.scenario_c_periodic(4)[0]] * 8)
        with self.assertRaises(SynthesisRejected):
            synthesize_genesis_supreme(static, n_frames=12, wait=0.1, verify_dds=False)

        pp = fx.scenario_f_ping_pong(20)
        with self.assertRaises(SynthesisRejected):
            synthesize_genesis_supreme(pp, n_frames=12, wait=0.1, verify_dds=False)

    def test_level6_multi_region_independent_composition_after_dds(self):
        # 3 regions: static top mountains/moon, slow middle clouds, fast bottom river
        frames = fx.scenario_l_independent_motions(16, size=(320, 180))
        h, w = frames.shape[1:3]
        b1, b2 = h // 3, (2 * h) // 3
        m_static = np.zeros((h, w), dtype=bool)
        m_static[:b1, :] = True
        m_clouds = np.zeros((h, w), dtype=bool)
        m_clouds[b1:b2, :] = True
        m_river = np.zeros((h, w), dtype=bool)
        m_river[b2:, :] = True

        region_specs = [
            {"id": "mountains", "mask": m_static, "strategy": "protect"},
            {"id": "clouds", "mask": m_clouds, "strategy": "animate", "speedScale": 0.8, "directionXY": [1.5, 0.0]},
            {"id": "river", "mask": m_river, "strategy": "animate", "speedScale": 1.4, "directionXY": [-3.0, 0.0]},
        ]
        out, meta = level6_multi_region_reconstruct(frames, wait=0.05, region_specs=region_specs)
        self.assertTrue(meta["applied"])
        self.assertEqual(meta["regionsProcessed"], 3)
        post = evaluate_post_dds(out, wait=0.05, protected_mask=m_static)
        self.assertEqual(post["metrics"]["staticIntegrity"]["maxAbsDiff"], 0.0)
        self.assertFalse(post["metrics"]["reversals"]["detected"])
        self.assertEqual(post["metrics"]["ddsDelta"]["distinctFrames"], 16)

    def test_reconstruct_best_improves_quasi_periodic_and_lua_sintetica(self):
        # 1) Quasi-periodic clip must be genuinely improved by reconstruct_best after DDS
        quasi = fx.scenario_d_quasi_periodic(16, size=(320, 180))
        res = reconstruct_best(quasi, wait=0.066667, verify_dds=True)
        self.assertTrue(res["improved"], res["attempts"])
        self.assertTrue(res["comparison"]["accepted"])
        self.assertLess(
            res["comparison"]["afterCompositeCost"],
            res["comparison"]["beforeCompositeCost"],
        )

        # 2) Real lua-sintetica.mp4 (which Círculo natural cuts rejected) -> reconstruct_best improves via synthesis/regional!
        lua_path = Path("docs/examples/genesis/lua-sintetica.mp4")
        if lua_path.is_file():
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                dur = media.probe(lua_path).duration
                frames = np.array(io_media.extract(lua_path, 0.0, dur, 10, (320, 180), Path(tmp), lambda: None))
                sent = analyze_levels(frames, dt=dur / 10.0)
                res_lua = reconstruct_best(frames, wait=0.1, sentinela_report=sent, verify_dds=True)
                self.assertTrue(res_lua["improved"], res_lua["attempts"])
                self.assertFalse(res_lua["finalEvaluation"]["metrics"]["reversals"]["detected"])


if __name__ == "__main__":
    unittest.main()
