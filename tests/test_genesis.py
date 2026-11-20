"""Run as a script with GENESIS_DEMO_DIR to retain the private demo artifacts."""

import os
import subprocess
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image
import io
import resource
import time
from app import dds
from app import genesis


def moon(path, vertical=False):
    h, w = (160, 90) if vertical else (90, 160)
    y, x = np.mgrid[:h, :w]
    sky = np.stack([12 + y // 5, 18 + y // 6, 54 + y // 3], -1).astype("uint8")
    sky[(x - 32) ** 2 + (y - 17) ** 2 < 95] = [235, 222, 186]
    frames = []
    for i in range(10):
        rgb = sky.copy()
        for a, b, speed in [(36, 52, 2), (60, 78, 4)]:
            phase = 2 * np.pi * (x[a:b] - speed * i) / 24
            cloud = 0.5 + 0.25 * np.sin(phase + 0.22 * y[a:b]) + 0.18 * np.cos(2 * phase - 0.14 * y[a:b])
            cloud *= np.sin(np.pi * (y[a:b] - a + 0.5) / (b - a))
            rgb[a:b] += np.uint8(cloud[:, :, None] * [95, 69, 100])
        frames.append(rgb.transpose(1, 0, 2) if vertical else rgb)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            "160x90",
            "-r",
            "10",
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
        input=np.array(frames).tobytes(),
        check=True,
    )


class GenesisTest(unittest.TestCase):
    def test_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.environ.get("GENESIS_DEMO_DIR", tmp))
            root.mkdir(exist_ok=True, parents=True)
            for axis in ("horizontal", "vertical"):
                movie = root / (axis + ".mp4")
                moon(movie, axis == "vertical")
                for size in genesis.ANIMATED_SIZES if axis == "horizontal" else [(640, 360)]:
                    folder = root / f"{axis}-{size[0]}"
                    folder.mkdir(exist_ok=True)
                    start = time.process_time()
                    r = genesis.render(
                        movie,
                        1.0,
                        folder,
                        size,
                        axis,
                        1.0,
                        lambda *a: None,
                        lambda: None,
                    )
                    self.assertEqual([b[3] for b in r["bands"]], [2, 4])
                    self.assertTrue(np.all(np.asarray(r["measured"]) > 0))
                    with zipfile.ZipFile(folder / "background.raf.zip") as z:
                        prefix = "scene/background.raf/"
                        self.assertTrue(z.read(prefix + "plane.mdx"))
                        cases = ET.fromstring(z.read(prefix + "index.xml")).findall("sequence/case")
                        self.assertEqual(len(cases), r["frames"])
                        self.assertAlmostEqual(float(cases[0].get("wait")), r["wait"])
                        ds = [z.read(k) for k in z.namelist() if k.endswith(".dds")]
                        self.assertTrue(all(d[84:88] == b"DXT1" for d in ds))
                        self.assertEqual(sum(map(len, ds)), r["bytes"])
                    rgb = np.array(Image.open(folder / "source.png").convert("RGBA"))
                    base = np.asarray(Image.open(io.BytesIO(dds.encode_frame(rgb))).convert("RGB"))
                    reference = base[:25, :80] if axis == "vertical" else base[: size[1] // 3]
                    for ix in range(r["frames"]):
                        pixels = np.asarray(Image.open(folder / f"{ix:02d}.webp"))
                        decoded = np.asarray(Image.open(io.BytesIO(ds[ix])).convert("RGB"))
                        self.assertTrue(np.array_equal(decoded, pixels))
                        area = pixels[:25, :80] if axis == "vertical" else pixels[: size[1] // 3]
                        self.assertTrue(np.array_equal(area, reference))
                        self.assertTrue(pixels[:, -1].any() and pixels[:, 0].any())
                    self.assertEqual(len(set(ds)), len(ds))
                    print(
                        "CPU/RSS",
                        round(time.process_time() - start, 2),
                        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    )
                    print(
                        "costura/mediana",
                        r["appearanceSteps"][-1],
                        np.median(r["appearanceSteps"][:-1]),
                    )
                    print(
                        axis,
                        size,
                        r["frames"],
                        r["bytes"],
                        r["wait"],
                        r["measured"][-1],
                        flush=True,
                    )

    def test_mutations(self):
        rng = np.random.default_rng(1)
        a = rng.integers(0, 255, (32, 96, 3), dtype="uint8")
        for step in [-2, 0, 7]:
            with self.assertRaises(genesis.media.MotionError):
                genesis.verify_pair(a, np.roll(a, step, axis=1), [(0, 32)], [2])
        with self.assertRaises(genesis.media.MotionError):
            genesis.strips(np.stack([np.roll(a, i, axis=1) for i in range(8)]))


if __name__ == "__main__":
    unittest.main()
