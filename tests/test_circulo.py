import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from app import circulo, media, ouroboros_media
from app.config import ANIMATED_SIZES, dds_bytes_for, max_frames_for
from test_sentinela import signals


def film(path, seq, fps=10):
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-s",
            "96x54",
            "-pixel_format",
            "rgb24",
            "-framerate",
            str(fps),
            "-i",
            "pipe:0",
            "-c:v",
            "libx264rgb",
            "-crf",
            "0",
            "-threads",
            "1",
            str(path),
        ],
        input=np.asarray(seq).tobytes(),
        timeout=30,
        check=True,
    )


def search(path, folder):
    return circulo.search(path, media.probe(path).duration, folder, lambda *a: None, lambda: None)


class CircleTest(unittest.TestCase):
    def test_seam_cases(self):
        cases = signals()
        for label, frames, expected in [
            ("periódica", cases["periódica"][:16], circulo.APPROVED),
            ("reversão", cases["pingpong"], circulo.REBUILD),
            ("pulso", cases["luz"][:16], circulo.APPROVED),
            ("congelamento", cases["horizontal"][:16] + [cases["horizontal"][0]], circulo.REBUILD),
            ("estática", cases["estática"], circulo.UNSUITABLE),
        ]:
            with self.subTest(label=label):
                q = circulo.assess(np.asarray(frames), 10.0)
                print(label, q)
                self.assertEqual(q["verdict"], expected)

    def test_full_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            movie = folder / "original.mp4"
            cases = signals()
            # 20 seconds: first 12 have no motion. The 1.6s moving period at
            # the end exceeds the 720p budget at the source's 10 fps.
            seq = cases["estática"][:1] * 120 + cases["periódica"] * 3
            film(movie, seq[:200])
            digest = hashlib.sha256(movie.read_bytes()).digest()
            report = search(movie, folder)
            self.assertEqual(report["sentinela"]["coverage"], [0.0, 20.0])
            self.assertEqual([tuple(g["size"]) for g in report["groups"]], ANIMATED_SIZES)
            for group in report["groups"]:
                best = group["candidates"]
                self.assertEqual(len(best), 3)
                self.assertEqual(len({(c["start"], c["end"]) for c in best}), 3)
                self.assertTrue(any(c["start"] >= 12 for c in best), group)
                for c in best:
                    self.assertLessEqual(c["frames"], max_frames_for(tuple(c["size"])))
                    self.assertAlmostEqual(c["end"] - c["start"], c["frames"] * c["wait"], places=4)
                print(
                    "BUSCA",
                    group["size"],
                    [(c["start"], c["end"], c["frames"], c["quality"]["verdict"]) for c in best],
                )
            c = report["groups"][-1]["candidates"][0]
            self.assertEqual(c["quality"]["verdict"], circulo.APPROVED)
            actual = circulo.preview(movie, c, folder, lambda *a: None, lambda: None)
            self.assertEqual(actual["bytes"], c["frames"] * dds_bytes_for(*c["size"]))
            with Image.open(folder / "00.webp") as f:
                self.assertEqual(f.size, tuple(c["size"]))
            self.assertEqual(digest, hashlib.sha256(movie.read_bytes()).digest())
            with self.assertRaises(ouroboros_media.MotionError):
                circulo.preview(movie, {**c, "frames": 49}, folder, lambda *a: None, lambda: None)

    def test_decode_failure_is_not_a_motion_verdict(self):
        frames = np.asarray(signals()["horizontal"])
        report = {
            "source": {"duration": 3.2, "fps": 10.0, "sampleFps": 10.0},
            "turnSeconds": [],
            "cutSeconds": [],
            "step": 0.1,
        }
        with (
            patch("app.sentinela.survey", return_value=(report, frames)),
            patch.object(
                ouroboros_media, "extract", side_effect=ouroboros_media.MotionError("falha na fonte")
            ),
            self.assertRaisesRegex(ouroboros_media.MotionError, "decodificar"),
        ):
            circulo.search(None, 3.2, None, lambda *a: None, lambda: None)

    def test_short_source(self):
        frames = np.array(signals()["horizontal"][:4])
        for size in ANIMATED_SIZES:
            self.assertEqual(circulo.seeds(frames, 0.0, 0.1, size, 10), [(0.0, 0.0, 0.4, 4)])

    def test_no_progressive_cut_through_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            movie = folder / "lua.mp4"
            film(movie, signals()["Lua sintética"])
            result = search(movie, folder)
            self.assertTrue(result["sentinela"]["pingPong"])
            for group in result["groups"]:
                for c in group["candidates"]:
                    if any(
                        c["start"] < t < c["end"] - c["wait"] for t in result["sentinela"]["turnSeconds"]
                    ):
                        self.assertEqual(c["quality"]["verdict"], circulo.REBUILD)
                self.assertNotEqual(group["verdict"], circulo.APPROVED)

    def test_owner_source(self):
        paths = [os.environ.get(k) for k in ("CZAR_ACCEPTANCE_VIDEO", "CZAR_LUA_VIDEO", "CZAR_COROA_VIDEO")]
        if not any(paths):
            self.skipTest("Defina um vídeo de aceitação privado.")
        for path in filter(None, paths):
            with tempfile.TemporaryDirectory() as tmp:
                result = search(Path(path), Path(tmp))
                print("VÍDEO", Path(path).name)
                for g in result["groups"]:
                    print(
                        "MEDIDO",
                        g["size"],
                        [
                            (c["start"], c["end"], c["frames"], c["quality"]["verdict"])
                            for c in g["candidates"]
                        ],
                    )
                    self.assertIn(g["verdict"], circulo.ORDER)


if __name__ == "__main__":
    unittest.main()
