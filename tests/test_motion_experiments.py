"""Controlled signals with known temporal direction; PROXY pixels/step."""

import json
import unittest

import cv2 as cv
import numpy as np

from app import motion_metrics as m
from app.motion_model import Clip
from app.motion_pipeline import candidate_series


def cases():
    rng = np.random.default_rng(124)
    cloud = cv.GaussianBlur(rng.integers(0, 255, (72, 32, 3), np.uint8), (5, 5), 1.1)
    texture = np.tile(cloud, (1, 4, 1))
    cycle = [np.roll(texture, i * 2, axis=1) for i in range(16)]
    y = np.arange(72)[:, None]
    x = np.arange(128)[None, :]
    water = [
        np.uint8(
            115
            + 37 * np.sin((x - i * 2) * np.pi / 8 + 0.045 * y)
            + 27 * np.cos((x - i * 2) * np.pi / 16 + np.sin(0.14 * y))
        )
        for i in range(16)
    ]
    cloudframes = [cv.GaussianBlur(f, (9, 9), 2.0) for f in cycle]
    still = [f.copy() for f in cycle]
    for f in still:
        f[:30] = texture[:30]
    crossing = [np.full_like(texture, 75) for _ in cycle]
    for i, f in enumerate(crossing):
        cv.rectangle(f, (i * 6, 24), (i * 6 + 13, 45), (242, 210, 180), -1)
    return {
        "água": [cv.cvtColor(f, cv.COLOR_GRAY2RGB) for f in water],
        "nuvens": cloudframes,
        "periódica": cycle,
        "corte": cycle[:8] + [255 - f for f in cycle[8:]],
        "reversão": cycle[:8] + list(reversed(cycle[:8])),
        "iluminação": [cv.convertScaleAbs(f, alpha=0.7, beta=i * 5) for i, f in enumerate(cycle)],
        "objeto": crossing,
        "região estática": still,
        "inconclusiva": [texture.copy() for _ in cycle],
    }


class Experiments(unittest.TestCase):
    def test_known_signals(self):
        expected = {
            "água": "candidato",
            "nuvens": "candidato",
            "periódica": "candidato",
            "corte": "inadequado",
            "reversão": "inadequado",
            "iluminação": "inadequado",
            "objeto": "inadequado",
            "região estática": "candidato",
            "inconclusiva": "revisar",
        }
        for name, sequence in cases().items():
            with self.subTest(name=name):
                q = m.evaluate(sequence)
                print(name, json.dumps(q, ensure_ascii=False))
                self.assertEqual(q["status"], expected[name])
                if expected[name] == "candidato":
                    self.assertLess(q["reverse"], 0.15)

    def test_endpoint_alignment_is_measured_and_static_area_protected(self):
        frames = cases()["periódica"]
        # Delayed last sample simulates a small subpixel endpoint error.
        frames[-1] = np.roll(frames[-1], -1, axis=1)
        clip = Clip(
            start=0, end=1, size=(640, 360), frames=len(frames), strategy="align", protect=(0, 0, 0.25, 1)
        )
        delta, weights = candidate_series(frames, clip)
        self.assertIsNotNone(delta)
        corrected = [m.warp(f, delta, w) if w else f for f, w in zip(frames, weights)]
        before, after = m.evaluate(frames), m.evaluate(corrected)
        print("alinhamento", before["cost"], after["cost"])
        self.assertTrue(m.improved(before, after))
        self.assertTrue(np.array_equal(corrected[-1][:, :32], frames[-1][:, :32]))
        self.assertEqual(after["reverse"], 0)
        # A perfect source receives no refinement.
        self.assertIsNone(candidate_series(cases()["periódica"], clip)[0])


if __name__ == "__main__":
    unittest.main()
