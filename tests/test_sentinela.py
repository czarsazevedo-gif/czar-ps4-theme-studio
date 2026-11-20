"""Known trajectories on the 96×54 measurement grid. No owner media required."""

import os
import tempfile
from pathlib import Path
import json
import unittest

import numpy as np
from app.sentinela import describe, diagnose


def signals():
    rng = np.random.default_rng(420)
    tile = rng.integers(45, 183, (54, 16, 3), dtype=np.uint8)
    texture = np.tile(tile, (1, 6, 1))
    static = rng.integers(35, 165, texture.shape, dtype=np.uint8)

    def shifted(path, axis=1):
        return [np.roll(texture, t, axis=axis) for t in path]

    forward = shifted(range(32))
    mirror = shifted([*range(16), *range(14, -1, -1)])
    mixed = [f.copy() for f in forward]
    moon = [f.copy() for f in mirror]
    for f in mixed + moon:
        f[:31] = static[:31]
    pulse = [np.uint8(static.astype(float) + 30 * (1 - np.cos(t * 2 * np.pi / 16))) for t in range(32)]
    crown = [static.copy() for _ in range(32)]
    y, x = np.mgrid[:54, :96]
    band = abs(y - (17 + x * 0.14)) < 4
    for i, f in enumerate(crown):
        f[band] = np.clip(f[band].astype(float) + 45 * (1 - np.cos(i * np.pi / 8)), 0, 255)
    particles = [static.copy() for _ in range(32)]
    for i, f in enumerate(particles):
        for j in range(3):
            k = (i + 21 * j) % 83
            f[10 + j * 9 : 15 + j * 9, k : k + 8] = texture[3:8, :8]
    return {
        "horizontal": forward,
        "vertical": [np.roll(static, t, axis=0) for t in range(32)],
        "pingpong": mirror,
        "parcial": shifted([*range(13), 12, 11, 10, 9, 8, 9, 10, 11, 12, 13, 14]),
        "periódica": forward,
        "luz": pulse,
        "estática": [static] * 32,
        "partículas": particles,
        "corte": [static] * 16 + [255 - static] * 16,
        "mista": mixed,
        "Lua sintética": moon,
        "Coroa sintética": crown,
    }


class SentinelaTest(unittest.TestCase):
    def test_known_phenomena(self):
        expected = {
            "horizontal": "A",
            "vertical": "A",
            "pingpong": "D",
            "parcial": "D",
            "periódica": "A",
            "luz": "B",
            "estática": "E",
            "partículas": "B",
            "corte": "D",
            "mista": "C",
            "Lua sintética": "C",
            "Coroa sintética": "B",
        }
        for name, frames in signals().items():
            with self.subTest(name=name):
                result = diagnose(describe(frames))
                print(
                    name,
                    json.dumps(
                        {
                            k: result[k]
                            for k in (
                                "categories",
                                "pingPong",
                                "dynamicFraction",
                                "coherence",
                                "periodSamples",
                            )
                        }
                    ),
                )
                self.assertIn(expected[name], result["categories"])
                self.assertEqual(result["pingPong"], name in {"pingpong", "parcial", "Lua sintética"})
                if name in {"luz", "Coroa sintética"}:
                    self.assertTrue(result["photometric"])
                if name == "corte":
                    self.assertTrue(result["cuts"])
                if name == "periódica":
                    self.assertEqual(result["periodSamples"], 16)
                if name == "horizontal":
                    self.assertGreater(result["directionXY"][0], 0)
                if name == "vertical":
                    self.assertGreater(result["directionXY"][1], 0)

    def test_available_media(self):
        from app.media import probe
        from app.sentinela import survey

        sources = [
            os.environ.get(k) for k in ("CZAR_ACCEPTANCE_VIDEO", "CZAR_LUA_VIDEO", "CZAR_COROA_VIDEO")
        ]
        if not any(sources):
            self.skipTest("Defina uma fonte privada para o ensaio adicional.")
        with tempfile.TemporaryDirectory() as tmp:
            for value in filter(None, sources):
                path = Path(value)
                report, frames = survey(
                    path, probe(path).duration, Path(tmp), lambda *args: None, lambda: None
                )
                self.assertEqual(len(frames), report["samples"])
                self.assertTrue(report["source"]["timestamps"])
                print(path.name, report["categories"], report["pingPong"], report["samples"])


if __name__ == "__main__":
    unittest.main()
