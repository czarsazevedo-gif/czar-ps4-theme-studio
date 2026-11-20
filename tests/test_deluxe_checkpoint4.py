"""Checkpoint 4 verification: CZAR Temporal Calibrator Supreme & Ouroboros Deluxe Finish post-DDS pipeline."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from app import dxt1, media
from app.config import RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from app.deluxe.calibrator import WAIT_GRID_SEC, calibrate_interval
from app.deluxe.finish import apply_deluxe_finish, apply_stable_gradient_dither, suppress_artificial_flicker
from tests import fixtures_deluxe as fx


class Checkpoint4CalibratorAndFinishSuite(unittest.TestCase):
    def test_temporal_calibrator_5_recommendations_and_budget_compliance(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            seq = fx.scenario_c_periodic(32, size=(320, 180))
            mp4 = fx.write_mp4(folder / "periodic.mp4", seq, fps=16.0)
            info = media.probe(mp4)
            res = calibrate_interval(mp4, 0.0, 1.0, source_fps=16.0, folder=folder, verify_dds=False)
            self.assertGreater(res["evaluatedCount"], 0)
            recs = res["recommendations"]
            for key in ("best_fluidity", "best_definition", "best_continuity", "best_duration", "best_balance"):
                self.assertIn(key, recs)
                cfg = recs[key]
                sz = tuple(cfg["size"])
                self.assertLessEqual(cfg["frames"], max_frames_for(sz))
                self.assertEqual(cfg["ddsBytes"], cfg["frames"] * dds_bytes_for(*sz))
                self.assertLessEqual(cfg["ddsBytes"], RAF_SIZE_LIMIT)
                self.assertAlmostEqual(cfg["duration"], cfg["frames"] * cfg["wait"], places=3)
                # Respects natural speed without arbitrary distortion
                self.assertGreaterEqual(cfg["speedRatio"], 0.65)
                self.assertLessEqual(cfg["speedRatio"], 1.45)

    def test_deluxe_finish_fixes_artificial_flicker_but_preserves_natural_pulse(self):
        h = fx.scenario_h_illumination(16, size=(240, 136))

        # Artificial flicker -> MUST apply selective_anti_flicker and reduce post-DDS flicker score!
        res_flk = apply_deluxe_finish(h["artificial_flicker"], wait=0.05)
        self.assertIn("selective_anti_flicker", res_flk["appliedSteps"], res_flk["rejectedSteps"])
        self.assertLess(
            res_flk["finalPostDds"]["metrics"]["flicker"]["score"],
            res_flk["baselinePostDds"]["metrics"]["flicker"]["score"] * 0.45,
        )
        # Verify blobs decode identically to decodedDdsFrames
        for i, blob in enumerate(res_flk["ddsBlobs"]):
            self.assertTrue(np.array_equal(dxt1.decode_dds(blob)[..., :3], res_flk["decodedDdsFrames"][i]))

        # Natural smooth lighting pulse -> MUST NOT run selective_anti_flicker!
        res_nat = apply_deluxe_finish(h["natural_pulse"], wait=0.05)
        self.assertNotIn("selective_anti_flicker", res_nat["appliedSteps"])
        self.assertFalse(res_nat["finalPostDds"]["metrics"]["flicker"]["artificialFlicker"])

    def test_deluxe_finish_harmonizes_photometric_drift_post_dds(self):
        quasi = fx.scenario_d_quasi_periodic(12, size=(240, 136))
        res = apply_deluxe_finish(quasi, wait=0.066667)
        self.assertIn("photometric_lab_harmonize", res["appliedSteps"], res["rejectedSteps"])
        self.assertLess(
            abs(res["finalPostDds"]["metrics"]["luminanceContinuity"]["seamDeltaL"]),
            abs(res["baselinePostDds"]["metrics"]["luminanceContinuity"]["seamDeltaL"]) * 0.5,
        )
        self.assertEqual(res["ddsBytes"], 12 * dds_bytes_for(240, 136))

    def test_deluxe_finish_stable_gradient_dither_has_zero_temporal_flicker(self):
        grad = fx.scenario_r_gradients_banding(8, size=(640, 360))
        dithered = apply_stable_gradient_dither(grad, amplitude=4.5)
        # Verify static Bayer dither works in smooth gradients and finish pipeline validates post-DDS cleanly
        res = apply_deluxe_finish(grad, wait=0.1, enable_dither=True)
        self.assertFalse(res["finalPostDds"]["metrics"]["flicker"]["artificialFlicker"])
        self.assertTrue(res["finalPostDds"]["metrics"]["ddsDelta"]["withinBudget"])
        self.assertTrue(res["finalPostDds"]["metrics"]["ddsDelta"]["validHeaders"])
        self.assertEqual(dithered.shape, grad.shape)

    def test_deluxe_finish_preserves_clean_loop_and_protected_mask(self):
        frames, pmask = fx.scenario_k_static_dynamic(12, size=(240, 136))
        # Blur dynamic half slightly relative to sharp reference so sharpness guard can be exercised
        blurred = frames.copy()
        for i in range(len(blurred)):
            b = cv2.GaussianBlur(blurred[i], (5, 5), 1.1)
            blurred[i][~pmask] = b[~pmask]
        res = apply_deluxe_finish(blurred, wait=0.05, protected_mask=pmask, reference_frames=frames)
        self.assertEqual(res["finalPostDds"]["metrics"]["staticIntegrity"]["maxAbsDiff"], 0.0)
        self.assertFalse(res["finalPostDds"]["metrics"]["reversals"]["detected"])
        self.assertTrue(res["comparison"]["accepted"])


if __name__ == "__main__":
    unittest.main()
