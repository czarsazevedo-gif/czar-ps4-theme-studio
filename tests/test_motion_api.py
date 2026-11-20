"""Real FFmpeg inputs, canonical RAF bytes, errors, isolation and durable results."""

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from app import mdx, motion_pipeline as engine, projects, raf
from app.config import RAF_SIZE_LIMIT, dds_bytes_for
from app.main import app
from test_compositor import layer, surface
from test_motion_experiments import cases
from test_projects import state


def film(path, sequence, fps=16):
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
            "128x72",
            "-r",
            str(fps),
            "-i",
            "pipe:0",
            "-an",
            "-threads",
            "1",
            "-c:v",
            "libx264rgb",
            "-crf",
            "0",
            str(path),
        ],
        input=np.stack(sequence).tobytes(),
        check=True,
        timeout=20,
    )


def wait(client, task, project):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        response = client.get("/api/motion/jobs/" + task, params={"project": project})
        response.raise_for_status()
        data = response.json()
        if data["state"] != "working":
            return data
        time.sleep(0.075)
    raise AssertionError("Tempo excedido no teste do trabalho")


class MotionApi(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(projects, "ROOT", self.folder / "projects"))
        self.client = self.enterContext(TestClient(app))
        self.client.get("/api/projects")
        self.project = self.client.post("/api/projects", json={"name": "Laboratório"}).json()["id"]
        self.params = {"project": self.project}
        self.clip = dict(start=0.0, end=1.0, frames=16, size=[640, 360], profile="smooth", strategy="cut")

    def upload(self, path):
        with path.open("rb") as stream:
            response = self.client.post(
                "/api/animated/upload",
                params=self.params,
                files={"file": (path.name, stream)},
                data={"frames": 3, "size": "640x360"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["job_id"]

    def operation(self, kind, data):
        task = self.client.post("/api/motion/" + kind, params=self.params, json=data)
        self.assertEqual(task.status_code, 202, task.text)
        done = wait(self.client, task.json()["id"], self.project)
        self.assertEqual(done["state"], "ready", done)
        return self.client.get("/api/motion/results/" + done["key"], params=self.params).json()

    def test_forward_raf_layers_and_persistence(self):
        path = self.folder / "textura.mp4"
        film(path, cases()["periódica"] * 3)
        job = self.upload(path)
        before = self.operation("analyze", {"job": job, "profile": "smooth"})
        self.assertGreater(len(before["candidates"]), 1)
        self.assertEqual(before["candidates"][0]["quality"]["status"], "candidato")
        self.assertGreater(len(before["source"]["timestamps"]), 20)
        self.assertTrue(before["marks"])
        image = io.BytesIO()
        Image.new("RGBA", (16, 16), (255, 0, 0, 255)).save(image, "PNG")
        asset = self.client.post("/api/library", files={"file": ("marca.png", image.getvalue())}).json()[
            "id"
        ]
        overlay = surface(layer(asset, "marca", x=300, y=300, width=100, height=100))
        data = {"job": job, "clip": self.clip, "surface": overlay}
        result = self.operation("render", data)
        self.assertTrue(result["eligible"], result["quality"])
        key = result["key"]
        url = "/api/motion/results/" + key
        with patch.object(engine, "build", side_effect=AssertionError("cache miss")):
            self.assertEqual(self.operation("render", data), result)
        self.assertEqual(result["ddsBytes"], 16 * dds_bytes_for(640, 360))
        self.assertLess(result["ddsBytes"], RAF_SIZE_LIMIT)
        archive = self.client.get(url + "/background.raf.zip", params=self.params).content
        ref = "scene/background.raf/"
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            self.assertEqual(len(z.namelist()), 18)
            self.assertEqual(
                z.read(ref + "plane.mdx"),
                mdx.quad_model(
                    node_name=raf.MODEL_ID, half_w=raf.plane_half_size()[0], half_h=raf.plane_half_size()[1]
                ),
            )
            xml = ET.fromstring(z.read(ref + "index.xml"))
            self.assertEqual([float(e.get("wait")) for e in xml.findall("./sequence/case")], [1 / 16] * 16)
            self.assertFalse(xml.findall(".//*[@target]"))
            for i in range(16):
                name = f"{i:02d}"
                blob = z.read(ref + name + ".dds")
                self.assertEqual(blob[84:88], b"DXT1")
                preview = self.client.get(url + f"/raf-{name}.webp", params=self.params).content
                with Image.open(io.BytesIO(blob)) as dd, Image.open(io.BytesIO(preview)) as webp:
                    self.assertEqual(dd.convert("RGB").tobytes(), webp.convert("RGB").tobytes())
                    self.assertEqual(webp.getpixel((100, 100))[:3], (255, 0, 0))
            color_state = {
                "version": 1,
                "surfaces": {"animated": overlay, "home": surface(layer(asset, "base"))},
            }
            owner = state()
            owner["bgMode"] = "animated"
            owner["composition"] = color_state
            owner["anim"]["jobId"] = job
            owner["motion"] = {
                "version": 1,
                "job": job,
                "clip": self.clip,
                "analysis": before["key"],
                "rendered": key,
                "applied": key,
            }
            project_url = "/api/projects/" + self.project
            save = self.client.put(project_url, json={"revision": 1, "name": "Loop", "state": owner})
            self.assertEqual(save.status_code, 200, save.text)
            self.assertEqual(
                self.client.put(
                    project_url, json={"revision": 1, "name": "Conflito", "state": owner}
                ).status_code,
                409,
            )
            unchanged = self.client.post(
                "/api/animated/" + job + "/export", params=self.params, data={"frames": 2}
            ).json()
            self.assertEqual(
                (unchanged["frames"], unchanged["download"]), (16, url + "/background.raf.zip")
            )
            response = self.client.post(
                "/api/package/build",
                params=self.params,
                data={
                    "title": "Tema",
                    "title_id": "CUSA00100",
                    "content_id": "UP9000-CUSA00100_00-TESTE00000000000",
                    "animated_job": job,
                    "artifacts_only": "true",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            with zipfile.ZipFile(
                io.BytesIO(self.client.get(response.json()["download"], params=self.params).content)
            ) as packed:
                for name in z.namelist():
                    self.assertEqual(z.read(name), packed.read(name))
        stale = self.client.post(
            "/api/motion/view", params=self.params, json={"job": job, "key": key, "surface": None}
        )
        self.assertEqual(stale.status_code, 409)
        duplicate = self.client.post(project_url + "/duplicate", json={"name": "Cópia"}).json()
        params = {"project": duplicate["id"]}
        self.assertEqual(self.client.get(url + "/background.raf.zip", params=params).content, archive)
        owner["motion"]["applied"] = None
        owner["motion"]["clip"]["end"] = 0.75
        self.assertEqual(
            self.client.put(
                "/api/projects/" + duplicate["id"], json={"revision": 1, "name": "Nova", "state": owner}
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get(project_url).json()["state"]["motion"]["applied"], key)
        other = self.enterContext(TestClient(app))
        other.get("/api/projects")
        self.assertEqual(other.get(url, params=self.params).status_code, 404)
        sibling = self.client.post("/api/projects", json={"name": "Separado"}).json()["id"]
        self.assertEqual(self.client.get(url, params={"project": sibling}).status_code, 404)

    def test_refinement_and_observed_texture_recurrence(self):
        sequence = cases()["periódica"]
        sequence[-1] = np.roll(sequence[-1], -1, axis=1)
        path = self.folder / "periodos.mp4"
        film(path, sequence * 3)
        job = self.upload(path)
        refined = self.operation(
            "render", {"job": job, "clip": {**self.clip, "strategy": "align", "protect": [0, 0, 0.25, 1]}}
        )
        self.assertTrue(refined["refined"])
        self.assertTrue(refined["eligible"])
        self.assert_forward(refined)
        metrics = refined["quality"]
        self.assertLess(metrics["raf"]["cost"], metrics["cutDds"]["cost"])
        print("ALINHAMENTO DDS", metrics["cutDds"]["cost"], metrics["raf"]["cost"])
        for i in (12, 15):
            images = []
            for group in ("cut", "opt"):
                url = f"/api/motion/results/{refined['key']}/{group}-{i:02d}.webp"
                with Image.open(io.BytesIO(self.client.get(url, params=self.params).content)) as im:
                    images.append(np.array(im.convert("RGB")))
            self.assertTrue(np.array_equal(images[0][:, :160], images[1][:, :160]))
        scan = self.operation("analyze", {"job": job, "profile": "long", "strategy": "texture"})
        self.assertTrue(scan["candidates"])
        self.assertTrue(all(c["clip"]["strategy"] == "texture" for c in scan["candidates"]))
        texture = self.operation("render", {"job": job, "clip": {**self.clip, "strategy": "texture"}})
        self.assertTrue(texture["eligible"])
        self.assertTrue(texture["recurrence"]["supported"])
        self.assert_forward(texture)
        unproven = self.operation(
            "render", {"job": job, "clip": {**self.clip, "end": 2.0, "frames": 32, "strategy": "texture"}}
        )
        self.assertFalse(unproven["eligible"])
        self.assertFalse(unproven["recurrence"]["supported"])

    def test_interruption_failures_and_previous_result(self):
        path = self.folder / "teste.mp4"
        film(path, cases()["periódica"] * 2)
        job = self.upload(path)
        payload = {"job": job, "clip": self.clip}
        done = self.operation("render", payload)
        url = "/api/motion/results/" + done["key"] + "/background.raf.zip"
        prior = self.client.get(url, params=self.params).content
        for changes in (
            {"frames": 49},
            {"frames": 24, "size": [1280, 720]},
            {"end": 10},
            {"end": 0},
            {"strategy": "reverse"},
        ):
            self.assertEqual(
                self.client.post(
                    "/api/motion/render",
                    params=self.params,
                    json={**payload, "clip": {**self.clip, **changes}},
                ).status_code,
                422,
            )
        self.assertEqual(
            self.client.post(
                "/api/motion/analyze", params=self.params, json={"job": "aaaaaaaaaaaa"}
            ).status_code,
            422,
        )
        self.assertEqual(self.client.post("/api/motion/render", json=payload).status_code, 400)
        self.assertEqual(
            self.client.post(
                "/api/motion/render",
                params=self.params,
                json=payload,
                headers={"sec-fetch-site": "cross-site"},
            ).status_code,
            403,
        )

        def slow(job, spec, stage, progress, check):
            (stage / "partial").write_bytes(b"partial")
            while True:
                check()
                time.sleep(0.025)

        alternate = {**payload, "clip": {**self.clip, "end": 0.875}}
        with patch.object(engine, "build", side_effect=slow):
            task = self.client.post("/api/motion/render", params=self.params, json=alternate).json()["id"]
            self.assertEqual(
                self.client.post("/api/motion/analyze", params=self.params, json={"job": job}).status_code,
                409,
            )
            self.client.post(f"/api/motion/jobs/{task}/cancel", params=self.params)
            self.assertEqual(wait(self.client, task, self.project)["state"], "cancelled")
        with patch.object(engine, "build", side_effect=OSError("private/path")):
            task = self.client.post("/api/motion/render", params=self.params, json=alternate).json()["id"]
            message = wait(self.client, task, self.project)
            self.assertEqual(message["state"], "error")
            self.assertNotIn("private", message["stage"])
        self.assertEqual(self.client.get(url, params=self.params).content, prior)
        self.assertFalse(list(self.folder.rglob(".work-*")))
        # A damaged immutable export is refused, then repaired by the normal render route.
        damaged = next(self.folder.rglob("background.raf/00.dds"))
        original = damaged.read_bytes()
        damaged.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        self.assertEqual(self.client.get(url, params=self.params).status_code, 409)
        restored = self.operation("render", payload)
        self.assertEqual(restored["key"], done["key"])
        self.assertEqual(damaged.read_bytes(), original)

    def test_nine_encoded_signals(self):
        approved = {"água", "nuvens", "periódica", "região estática"}
        for name, sequence in cases().items():
            with self.subTest(name=name):
                path = self.folder / "sinal.mp4"
                film(path, sequence * 2)
                ident = self.upload(path)
                data = self.operation("render", {"job": ident, "clip": self.clip})
                self.assertEqual(data["eligible"], name in approved, data["quality"])
                self.assertEqual(data["sampleTimes"], [i / 16 for i in range(16)])
                q = data["quality"]["raf"]
                print("SINAL RAF", name, q["status"], q["seam"], q["internal"], q["flags"])
                if name == "periódica":
                    self.assert_forward(data)

    def assert_forward(self, result):
        # This texture was generated with positive horizontal translation.
        # Independent of optical flow, register each DDS by scalar error
        # against integer translations of its predecessor, including the wrap.
        frames = []
        for index in range(result["frames"]):
            path = f"/api/motion/results/{result['key']}/raf-{index:02d}.webp"
            with Image.open(io.BytesIO(self.client.get(path, params=self.params).content)) as image:
                frames.append(np.asarray(image.convert("RGB").resize((128, 72)), dtype=np.float32))
        for a, b in zip(frames, frames[1:] + frames[:1]):
            shifts = range(-4, 5)
            best = min(shifts, key=lambda shift: np.abs(np.roll(a, shift, axis=1) - b).mean())
            self.assertGreater(best, 0)

    def test_real_video(self):
        path = os.environ.get("CZAR_ACCEPTANCE_VIDEO")
        if not path:
            self.skipTest("Defina CZAR_ACCEPTANCE_VIDEO com um arquivo real disponível.")
        job = self.upload(Path(path))
        result = self.operation("analyze", {"job": job, "profile": "smooth"})
        self.assertTrue(result["source"]["timestamps"])
        self.assertTrue(result["marks"])
        if result["candidates"]:
            for candidate in result["candidates"][:2]:
                clip = dict(candidate["clip"], strategy="align")
                built = self.operation("render", {"job": job, "clip": clip})
                print(
                    "MATERIAL REAL",
                    json.dumps(
                        {
                            "name": result["source"]["name"],
                            "sourceFps": result["source"]["sampleFps"],
                            "analysisSeconds": result["seconds"],
                            "candidates": len(result["candidates"]),
                            "clip": clip,
                            "seconds": built["seconds"],
                            "quality": built["quality"],
                            "bytes": built["ddsBytes"],
                            "eligible": built["eligible"],
                            "refined": built["refined"],
                        }
                    ),
                )
                self.assertEqual(
                    self.client.get(
                        "/api/motion/results/" + built["key"] + "/background.raf.zip", params=self.params
                    ).status_code,
                    200 if built["eligible"] else 404,
                )
        else:
            print("MATERIAL REAL sem candidatos adequados", result["source"]["name"])


if __name__ == "__main__":
    unittest.main()
