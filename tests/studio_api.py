import hashlib
import io
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from app import dxt1, mdx, raf, vag
from app.main import app
from app.studio import media, sfo, storage
from app.studio.schema import Animation, safe_name
from app.studio.validate import raf_metadata
from tools.verify_bundle import verify

HERE = Path(__file__).resolve().parent
PREFIX = "/api/studio"


def former_entry(name, data):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, data)
    return stream.getvalue()


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = patch.object(storage, "ROOT", Path(self.directory.name))
        root.start()
        self.addCleanup(root.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        cfg = self.client.get(PREFIX + "/session").json()
        self.auth = {"x-czar-token": cfg["token"]}
        self.project = self.create()
        self.url = PREFIX + "/projects/" + self.project["id"]

    def create(self, mode="static"):
        response = self.client.post(PREFIX + "/projects", headers=self.auth, json={"name": "Tema de teste", "mode": mode})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def add(self, filename, display=None):
        result = self.client.post(self.url + "/assets", headers={**self.auth, "x-filename": display or filename},
                                  content=(HERE / filename).read_bytes())
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def save(self):
        response = self.client.put(self.url, headers=self.auth, json={
            "revision": self.project["revision"], "settings": self.project["settings"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.project = response.json()

    def run_job(self, kind):
        created = self.client.post(self.url + "/jobs", headers=self.auth, json={"kind": kind})
        self.assertEqual(created.status_code, 202, created.text)
        endpoint = self.url + "/jobs/" + created.json()["id"]
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            job = self.client.get(endpoint).json()
            if job["state"] in {"done", "failed", "cancelled"}:
                return job, endpoint
            time.sleep(.1)
        self.fail("A tarefa não terminou.")

    def download(self, endpoint):
        response = self.client.get(endpoint + "/download")
        self.assertEqual(response.status_code, 200, response.text[:150])
        return zipfile.ZipFile(io.BytesIO(response.content))

    def test_static_icons_pcm_and_vag(self):
        picture = self.add("sample.png", "..%2F..%5Cedi%C3%A7%C3%A3o.png")
        self.assertEqual(picture["name"], "edição.png")
        effect = self.add("sample_sfx.wav")
        self.assertGreater(max(effect["peak"]), 0)
        self.assertIsNotNone(effect["loudness"])
        self.project["settings"].update(background={"home": {"asset": picture["id"],
            "transform": {"crop": [.12, .05, .92, .92], "rotation": 10, "flipX": True, "brightness": 1.2}}},
            icons={"fn:power": {"asset": picture["id"], "transform": {"fit": "contain"}, "glow": .7}},
            audio={"se_cancel": {"asset": effect["id"], "start": .05, "end": .3, "fadeOut": .05},
                   "bgm": {"asset": effect["id"], "gain": -5}})
        self.save()
        preview = self.client.post(self.url + "/audio-preview/se_cancel", headers=self.auth,
                                   json=self.project["settings"]["audio"]["se_cancel"])
        self.assertEqual(preview.content[:4], b"RIFF")
        job, endpoint = self.run_job("bundle")
        self.assertEqual(job["report"]["errors"], [])
        self.assertEqual(job["report"]["packageValidation"]["status"], "unverified")
        archive = self.client.get(endpoint + "/download")
        self.assertEqual(archive.status_code, 200)
        bundle_path = storage.ROOT / "bundle.zip"
        bundle_path.write_bytes(archive.content)
        self.assertEqual(verify(bundle_path), job["id"])
        with zipfile.ZipFile(bundle_path) as bundle:
            names = set(bundle.namelist())
            self.assertIn("Image0/texture/function_icon/power_glow.png", names)
            self.assertIn("Image0/texture/background/functionscreen.png", names)
            self.assertIn("audio-preparado/bgm_home.wav", names)
            self.assertNotIn("Image0/sound/bgm_home.at9", names)
            expected_widths = {"Image0/texture/function_icon/power.png": 128,
                               "Image0/texture/function_icon/power_glow.png": 152, "Sc0/icon0.png": 512}
            for key, width in expected_widths.items():
                with Image.open(io.BytesIO(bundle.read(key))) as img:
                    self.assertEqual(img.size, (width, width))
            pcm, rate = vag.decode(bundle.read("Image0/sound/se_cancel.vag"))
            self.assertGreater(len(pcm), 9000)
            self.assertEqual(rate, 48000)
            self.assertIn(b"<homebgm-enable>False", bundle.read("Image0/theme.xml"))
            meta = sfo.parse(bundle.read("Sc0/param.sfo"))
            self.assertEqual((meta["CATEGORY"], meta["IRO_TAG"]), ("ac", 2))
            for row in bundle.read("SHA256SUMS").decode().splitlines():
                digest, name = row.split("  ", 1)
                self.assertEqual(digest, hashlib.sha256(bundle.read(name)).hexdigest())

    def test_clip_manual_gif_and_raf(self):
        video = self.add("sample.mp4")
        gif = self.add("sample.gif")
        self.assertEqual((video["kind"], gif["kind"]), ("video", "animation"))
        self.assertGreater(video["fps"], 20)
        for asset, moments in ((video, [1, 1.2, 1.8]), (gif, [.1, .9, 1.7])):
            with self.subTest(asset=asset["kind"]):
                response = self.client.get(f"{self.url}/assets/{asset['id']}/frame", params={"t": moments[1]})
                self.assertEqual(response.status_code, 200, response.text[:90])
                self.project["settings"].update(mode="animated", animation={"source": {"asset": asset["id"]}, "width": "640",
                    "start": moments[0], "end": 2, "wait": .1, "speed": 2, "sampling": "manual",
                    "timeline": moments, "reverse": True}, layers=[{"color": "#00FF00", "transform": {"opacity": .1}}])
                self.save()
                job, endpoint = self.run_job("raf")
                report = job["report"]
                self.assertEqual(report["errors"], [])
                self.assertAlmostEqual(report["raf"]["fps"], 20)
                self.assertEqual(report["raf"]["frames"], 3)
                self.assertEqual(report["raf"]["ddsBytes"], 3 * 115328)
                with self.download(endpoint) as zipped:
                    root = "scene/background.raf/"
                    self.assertEqual(dxt1.decode_dds(zipped.read(root + "00.dds")).shape, (360, 640, 4))
                    self.assertTrue(mdx.parse(zipped.read(root + "plane.mdx")))
                    scene = zipped.read(root + "index.xml")
                    self.assertIn(b'wait="0.05"', scene)
                    self.assertNotIn(b"target=", scene)

    def test_project_isolation_reopen_and_archive(self):
        asset = self.add("sample.png")
        self.project["settings"]["background"]["home"] = {"asset": asset["id"]}
        self.save()
        same_session = TestClient(app, cookies=self.client.cookies)
        self.addCleanup(same_session.close)
        self.assertEqual(same_session.get(self.url).json()["settings"]["name"], "Tema de teste")
        stranger = TestClient(app)
        self.addCleanup(stranger.close)
        stranger.get(PREFIX + "/session")
        self.assertEqual(stranger.get(self.url).status_code, 404)
        self.assertEqual(stranger.get(f"{self.url}/assets/{asset['id']}/content").status_code, 404)
        self.assertEqual(stranger.get(PREFIX + "/projects").json(), [])
        self.assertEqual(self.client.get(PREFIX + "/projects/not-an-id").status_code, 404)
        self.assertEqual(self.client.delete(self.url).status_code, 403)
        self.assertEqual(self.client.delete(self.url, headers={**self.auth, "origin": "https://otro.invalid"}).status_code, 403)
        conflicted = self.client.put(self.url, headers=self.auth, json={"revision": 0, "settings": self.project["settings"]})
        self.assertEqual(conflicted.status_code, 409)
        _, endpoint = self.run_job("project")
        response = self.client.get(endpoint + "/download")
        imported = self.client.post(PREFIX + "/projects/import", headers=self.auth, content=response.content)
        self.assertEqual(imported.status_code, 201, imported.text)
        copy = imported.json()
        self.assertNotEqual(copy["id"], self.project["id"])
        self.assertEqual(copy["settings"], self.project["settings"])
        self.assertEqual(copy["assets"][asset["id"]]["sha256"], asset["sha256"])
        duplicated = self.client.post(self.url + "/duplicate", headers=self.auth, json={"name": "Outra cópia"})
        self.assertEqual(duplicated.status_code, 201)
        self.assertEqual(self.client.delete(self.url, headers=self.auth).status_code, 204)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_rejects_invalid_media_zip_and_limits(self):
        for name, content in [("fake.png", b"#!/bin/sh"), ("music.at9", (HERE / "notaudio.at9").read_bytes())]:
            response = self.client.post(self.url + "/assets", headers={**self.auth, "x-filename": name}, content=content)
            self.assertEqual(response.status_code, 415, response.text)
            self.assertEqual(response.json()["code"], "invalid_media")
        self.assertEqual(safe_name("..\\x/../one\0.png"), "one.png")
        for data in [former_entry("../escape", b"no"), former_entry("assets/" + "f" * 32, b"1" * (3 * 1024 ** 2))]:
            response = self.client.post(PREFIX + "/projects/import", headers=self.auth, content=data)
            self.assertEqual(response.status_code, 422, response.text)
        with patch.object(storage, "UPLOAD_LIMIT", 100):
            response = self.client.post(self.url + "/assets", headers=self.auth, content=b"x" * 101)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(list((storage.ROOT / "staging").iterdir()))
        with patch("app.studio.media.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            with self.assertRaisesRegex(storage.StudioError, "tempo permitido"):
                media.run("ffmpeg", [])

    def test_cancellation_does_not_publish_an_archive(self):
        started = threading.Event()
        proceed = threading.Event()

        def delayed(kind, project, root, work, build_id, progress):
            started.set()
            self.assertTrue(proceed.wait(5))
            progress("Compondo", 50)

        with patch("app.studio.queue.build.execute", side_effect=delayed):
            job = self.client.post(self.url + "/jobs", headers=self.auth, json={"kind": "bundle"}).json()
            self.assertTrue(started.wait(5))
            endpoint = self.url + "/jobs/" + job["id"]
            try:
                self.assertEqual(self.client.delete(endpoint, headers=self.auth).status_code, 200)
            finally:
                proceed.set()
            for _ in range(50):
                if self.client.get(endpoint).json()["state"] == "cancelled":
                    break
                time.sleep(.05)
            else:
                self.fail("A tarefa pedida para cancelar continuou ativa.")
        self.assertEqual(self.client.get(endpoint + "/download").status_code, 404)

    def test_budget_and_validator_reject_regressions(self):
        for width, cap in [("1280", 13), ("960", 24), ("640", 48)]:
            sample = Animation(width=width, frames=cap)
            self.assertEqual(sample.budget()["maxFrames"], cap)
            self.assertNotEqual(sample.budget()["status"], "Inválido")
            self.assertEqual(len(sample.timestamps(2)), cap)
        self.assertEqual(Animation(width="960", frames=48).budget()["status"], "Inválido")
        self.assertEqual(len(Animation(pingPong=True, frames=3).timestamps(1)), 3)
        self.project["settings"]["mode"] = "animated"
        self.project["settings"]["animation"] = {"width": "960", "frames": 48,
            "source": {"asset": self.add("sample.mp4")["id"]}}
        self.save()
        job, endpoint = self.run_job("bundle")
        self.assertEqual(job["report"]["status"], "error")
        self.assertEqual(self.client.get(endpoint + "/download").status_code, 404)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            header = dxt1.dds_header(640, 360, 115200)
            for i in range(2):
                (folder / f"{i:02}.dds").write_bytes(header + bytes(115200))
            (folder / "plane.mdx").write_bytes(mdx.quad_model(node_name="plane", half_w=1, half_h=1))
            xml = raf.build_scene(["00.dds", "01.dds"], .07)
            (folder / "index.xml").write_text(xml)
            self.assertEqual(raf_metadata(folder)["frames"], 2)
            (folder / "index.xml").write_text(xml.replace('wait="0.07"', 'wait="0.07" target="00.dds"'))
            with self.assertRaises(ValueError):
                raf_metadata(folder)
        with self.assertRaises((ValueError, struct.error)):
            sfo.parse(b"\x00PSF")
