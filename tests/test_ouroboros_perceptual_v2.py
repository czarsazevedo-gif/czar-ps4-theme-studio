"""Checkpoint A scientific & perceptual regression suite for Ouroboros Perceptual V2."""

from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from app.deluxe.circulo_supreme import compute_multicriteria_cost, _generate_seeds
from app.deluxe.genesis_supreme import synthesize_genesis_supreme
from app.deluxe.metrics import compare_before_after, evaluate_loop, evaluate_post_dds, evaluate_ten_cycles
from app.deluxe.reconstructor import level3_motion_compensated_seam, level4_content_bridge_reconstruct, reconstruct_best
from app.deluxe.sentinela_supreme import CLASS_CORRECTABLE, CLASS_NATURAL, CLASS_UNSUITABLE, analyze_levels

HERE = Path(__file__).resolve().parent


def _make_waterfall_clip(n: int = 24, h: int = 180, w: int = 320, dy: float = 3.5) -> np.ndarray:
    """Create a realistic cascading vertical-texture clip (water falling downward +y) with static rocky sides."""
    rng = np.random.default_rng(101)
    # Build tall seamless-free random water texture (non-periodic vertically so natural cut has a real seam!)
    tex = rng.integers(50, 230, (h * 4, w, 3), dtype=np.uint8)
    tex = cv2.GaussianBlur(tex, (5, 9), 1.4)
    # Add foam streaks
    yy, xx = np.mgrid[0 : h * 4, 0:w].astype(np.float32)
    foam = (30.0 * np.sin(yy * 0.19 + np.sin(xx * 0.08) * 2.0)).astype(np.float32)
    tex = np.clip(tex.astype(np.float32) + foam[..., None], 0, 255).astype(np.uint8)
    # Static rock cliff on left 25%
    rock = rng.integers(35, 95, (h, w // 4, 3), dtype=np.uint8)
    rock = cv2.GaussianBlur(rock, (5, 5), 1.0)
    frames = []
    for i in range(n):
        y0 = int(round((n - i) * dy)) + 20
        f = tex[y0 : y0 + h, :, :].copy()
        f[:, : w // 4] = rock
        frames.append(f)
    return np.stack(frames)


def _make_swaying_pendulum_clip(n: int = 40, h: int = 90, w: int = 160) -> np.ndarray:
    """Oscillating swaying target (like a flower in wind): same position at different phases has opposite velocity!"""
    frames = []
    for i in range(n):
        img = np.full((h, w, 3), (35, 55, 40), dtype=np.uint8)
        phase = 2.0 * math.pi * i / 16.0  # true period = 16 frames
        cx = int(round(w * 0.5 + 22.0 * math.sin(phase)))
        cv2.circle(img, (cx, h // 2), 14, (240, 190, 50), -1)
        cv2.circle(img, (cx - 4, h // 2 - 4), 4, (255, 250, 200), -1)
        frames.append(img)
    return np.stack(frames)


class OuroborosPerceptualV2Test(unittest.TestCase):
    def test_compare_before_after_rejects_directional_degradation_and_motion_dampening(self):
        # Construct realistic baseline & regressed reports mimicking the flower.mp4 loophole
        base = {
            "frames": 12,
            "wait": 0.1,
            "issues": ["Descontinuidade visual significativa na junção circular."],
            "passed": False,
            "metrics": {
                "visualSimilarity": {"seamMae": 12.0, "internalMedianMae": 5.0, "seamRatio": 2.4, "seamSsim": 0.80, "ssimDrop": 0.12},
                "flowContinuity": {"seamFlowErrorPx": 1.2, "flowErrorRatio": 1.4, "seamDirectionCosine": 0.72, "seamOppositeFraction": 0.08, "directionMeasured": True},
                "velocityContinuity": {"relativeJump": 0.20, "internalMedianSpeedPx": 3.0},
                "luminanceContinuity": {"seamDeltaL": 0.5},
                "chromaticContinuity": {"seamDeltaChroma": 0.5},
                "edgeConsistency": {"edgeRatio": 1.3},
                "flicker": {"score": 0.1, "artificialFlicker": False},
                "ghosting": {"score": 0.1, "detected": False},
                "texturePreservation": {"sharpnessRetention": 0.98},
                "staticIntegrity": {"checked": False, "maxAbsDiff": 0.0},
                "reversals": {"detected": False},
                "freezes": {"detected": False},
            },
        }
        # Candidate A improves seamMae/seamRatio but breaks direction (cos 0.72 -> 0.10, opp 0.08 -> 0.35)!
        bad_dir = json_clone(base)
        bad_dir["metrics"]["visualSimilarity"]["seamMae"] = 5.5
        bad_dir["metrics"]["visualSimilarity"]["seamRatio"] = 1.1
        bad_dir["metrics"]["flowContinuity"]["seamDirectionCosine"] = 0.10
        bad_dir["metrics"]["flowContinuity"]["seamOppositeFraction"] = 0.35
        bad_dir["issues"] = []
        cmp_dir = compare_before_after(base, bad_dir)
        self.assertFalse(cmp_dir["accepted"], cmp_dir)
        self.assertTrue(any("direcional" in r.lower() for r in cmp_dir["hardRejectReasons"]), cmp_dir)

        # Candidate B cuts motion amplitude in half (3.0 px -> 1.1 px) to fake low seam error!
        slow_cheat = json_clone(base)
        slow_cheat["metrics"]["visualSimilarity"]["seamMae"] = 5.0
        slow_cheat["metrics"]["visualSimilarity"]["seamRatio"] = 1.0
        slow_cheat["metrics"]["velocityContinuity"]["internalMedianSpeedPx"] = 1.1
        slow_cheat["issues"] = []
        cmp_slow = compare_before_after(base, slow_cheat)
        self.assertFalse(cmp_slow["accepted"], cmp_slow)
        self.assertTrue(any("amplitude de movimento" in r.lower() for r in cmp_slow["hardRejectReasons"]), cmp_slow)

    def test_video_textures_motion_phase_seeds_prefer_full_period_over_half_period_reversal(self):
        clip = _make_swaying_pendulum_clip(n=40)
        # True period is 16 frames (1.6s at step=0.1s); half-period is 8 frames (same position, OPPOSITE velocity!)
        opts = _generate_seeds(clip, offset=0.0, step=0.1, size=(640, 360), source_fps=10.0, source_duration=4.0)
        self.assertTrue(opts)
        best = min(opts, key=lambda o: o[0])
        best_dur = best[2] - best[1]
        # Must prefer full cycle (~1.6s) rather than opposite-phase half-cycle (~0.8s)!
        self.assertGreaterEqual(best_dur, 1.3, f"Selected opposite-phase half cycle: {best}")

    def test_genesis_anti_ghosting_waterfall_preserves_speed_sharpness_and_direction_post_dds(self):
        clip = _make_waterfall_clip(n=20, h=180, w=320, dy=3.5)
        prot = np.zeros((180, 320), dtype=bool)
        prot[:, :80] = True  # rock cliff protected

        res = synthesize_genesis_supreme(clip[:12], n_frames=12, wait=0.08, protected_mask=prot, verify_dds=True)
        post = res["evaluation"]
        m = post["metrics"]
        self.assertFalse(m["ghosting"]["detected"], post)
        self.assertFalse(m["reversals"]["detected"], post)
        self.assertGreaterEqual(m["texturePreservation"]["sharpnessRetention"], 0.80, post)
        self.assertEqual(m["staticIntegrity"]["maxAbsDiff"], 0.0)
        if m["flowContinuity"]["directionMeasured"]:
            self.assertGreater(m["flowContinuity"]["seamDirectionCosine"], 0.65, post)
            self.assertLess(m["flowContinuity"]["seamOppositeFraction"], 0.15, post)

        ten = evaluate_ten_cycles(post)
        self.assertEqual(ten["cycles"], 10)
        self.assertEqual(ten["totalFramesPlayed"], 120)
        self.assertTrue(ten["directionalContinuityOk"], ten)

    def test_upstream_aligned_level4_bridge_and_direction_safe_level3(self):
        full_clip = _make_waterfall_clip(n=24, h=180, w=320, dy=3.5)
        lead_in = full_clip[:4]
        core = full_clip[4:16]
        lead_out = full_clip[16:20]
        prot = np.zeros((180, 320), dtype=bool)
        prot[:, :80] = True

        # Level 3 direction-safe check: must never create a reversal or break protected mask
        l3, l3_meta = level3_motion_compensated_seam(core, protected_mask=prot)
        ev3 = evaluate_loop(l3, 0.08, protected_mask=prot)
        self.assertEqual(ev3["metrics"]["staticIntegrity"]["maxAbsDiff"], 0.0)

        # Level 4 upstream-aligned bridge (both lead_in and lead_out)
        l4_in, meta_in = level4_content_bridge_reconstruct(core, lead_in=lead_in, protected_mask=prot, prefer="lead_in")
        self.assertTrue(meta_in["applied"])
        l4_out, meta_out = level4_content_bridge_reconstruct(core, lead_out=lead_out, protected_mask=prot, prefer="lead_out")
        self.assertTrue(meta_out["applied"])

        # Reconstruct best on waterfall must improve over raw cut without introducing ghosting or reversals!
        rec = reconstruct_best(core, 0.08, lead_in=lead_in, lead_out=lead_out, protected_mask=prot, verify_dds=True)
        self.assertTrue(rec["improved"], rec["attempts"])
        self.assertFalse(rec["finalEvaluation"]["metrics"]["reversals"]["detected"])
        self.assertFalse(rec["finalEvaluation"]["metrics"]["ghosting"]["detected"])
        self.assertLess(rec["comparison"]["afterCompositeCost"], rec["comparison"]["beforeCompositeCost"])

    def test_sentinela_visual_quality_and_oscillatory_texture_classification(self):
        # Create a continuous stochastic turbulent texture (fire/sparks style: fluctuating everywhere, no cuts)
        rng = np.random.default_rng(77)
        frames = []
        base = rng.integers(20, 60, (90, 160, 3), dtype=np.uint8)
        for i in range(20):
            f = base.copy()
            # Turbulent flickering flame zone
            flames = rng.integers(80, 245, (50, 90, 3), dtype=np.uint8)
            flames = cv2.GaussianBlur(flames, (5, 5), 1.2)
            # Small upward shift + random flicker
            f[20:70, 35:125] = flames
            frames.append(f)
        rep = analyze_levels(np.stack(frames), dt=0.1, source_info={"width": 1920, "height": 1080, "fps": 30.0, "duration": 2.0, "codec": "h264", "isVFR": False})
        self.assertIn("levelQuality", rep)
        self.assertIn("lightingStability", rep["levelA"])
        self.assertIn("cameraClass", rep["levelC"]["cameraMotion"])
        self.assertTrue(rep["levelQuality"]["matchesPs416x9"])
        self.assertEqual(rep["levelQuality"]["resolutionTier"], "1080p_ou_superior")
        # Must not drop valid turbulent dynamic texture into static/unsuitable when non-static & continuous
        self.assertNotEqual(rep["classification"], CLASS_NATURAL)

    def test_optional_owner_acceptance_video_hook(self):
        env_path = os.environ.get("CZAR_ACCEPTANCE_VIDEO", "").strip()
        if not env_path:
            self.skipTest("Defina CZAR_ACCEPTANCE_VIDEO=/caminho/video.mp4 para executar o ensaio com mídia privada.")
        p = Path(env_path)
        self.assertTrue(p.is_file(), f"CZAR_ACCEPTANCE_VIDEO não encontrado: {p}")
        from scripts.benchmark_ouroboros_corpus import benchmark_video
        res = benchmark_video({"id": "owner_env", "split": "owner", "category": "proprietario", "path": str(p)})
        self.assertNotIn("error", res, res)
        self.assertTrue(res["refinedPostDds"]["withinBudget"])


def json_clone(obj):
    import json
    return json.loads(json.dumps(obj))


if __name__ == "__main__":
    unittest.main()
