"""Render, export and manifest tests backed by FFmpeg and real encoders."""

import copy
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from app import dds, media, projects, theme
from app.config import IMAGE_SLOTS, max_frames_for
from app.main import app
from test_projects import state

HERE = Path(__file__).resolve().parent


def layer(asset, ident, **values):
    return {
        "id": ident, "asset": asset, "name": ident,
        "x": 960, "y": 540, "width": 640, "height": 360,
        "angle": 0, "opacity": 1, "fit": "stretch", **values,
    }


def surface(*layers):
    return {"base": False, "layers": list(layers)}


def composition(**surfaces):
    return {"version": 1, "surfaces": surfaces}


def pixels(blob):
    with Image.open(io.BytesIO(blob)) as loaded:
        return loaded.convert("RGBA")


class CompositorTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(projects, "ROOT", self.temp / "projects"))
        self.client = self.enterContext(TestClient(app))
        self.client.get("/api/projects")
        self.project = self.client.post("/api/projects", json={"name": "Composição"}).json()["id"]
        self.params = {"project": self.project}

    def import_swatch(self, name, rgb, size=(72, 40)):
        raw = io.BytesIO()
        Image.new("RGBA", size, rgb).save(raw, "PNG")
        result = self.client.post("/api/library", files={"file": (name + ".png", raw.getvalue())})
        self.assertEqual(result.status_code, 201, result.text)
        asset = result.json()
        self.assertEqual(hashlib.sha256(raw.getvalue()).hexdigest(), asset["id"])
        return asset

    def render(self, target, item, base=None):
        result = self.client.post("/api/composition/image", params=self.params,
                                  json={"target": target, "surface": item, "base": base})
        self.assertEqual(result.status_code, 200, result.text[:300])
        return pixels(result.content)

    def package(self, comp=None, **fields):
        data = {"title": "Tema", "title_id": "CUSA00100", "content_id": "UP9000-CUSA00100_00-TESTE00000000000",
                "artifacts_only": "true", **fields}
        if comp is not None:
            data["composition"] = json.dumps(comp)
        result = self.client.post("/api/package/build", params=self.params, data=data)
        self.assertEqual(result.status_code, 200, result.text)
        downloaded = self.client.get(result.json()["download"], params=self.params)
        self.assertEqual(downloaded.status_code, 200)
        return zipfile.ZipFile(io.BytesIO(downloaded.content))

    def save(self, editor, revision=1):
        return self.client.put(f"/api/projects/{self.project}",
                               json={"name": "Composição", "revision": revision, "state": editor})

    def test_static_pixel_order_alpha_and_canonical_slot_outputs(self):
        red = self.import_swatch("vermelho", (255, 0, 0, 255))
        blue = self.import_swatch("azul", (0, 0, 255, 255))
        bottom = layer(red["id"], "base")
        top = layer(blue["id"], "top", opacity=0.5, angle=90)
        home = surface(bottom, top)
        preview = self.render("home", home)
        self.assertEqual(preview.size, (1920, 1080))
        self.assertEqual(preview.getpixel((960, 540)), (127, 0, 128, 255))
        self.assertEqual(preview.getpixel((750, 540)), (255, 0, 0, 255))
        self.assertEqual(preview.getpixel((960, 815)), (0, 0, 128, 255))
        self.assertEqual(preview.getpixel((630, 540)), (0, 0, 0, 255))
        self.assertEqual(self.render("home", surface(top, bottom)).getpixel((960, 540)), (255, 0, 0, 255))
        icon = surface(layer(blue["id"], "icone", x=64, y=64, width=64, height=64,
                             fit="contain", opacity=0.5))
        content = surface(layer(red["id"], "conteudo", x=256, y=256, width=256, height=256))
        icon_preview = self.render("fn:friend", icon)
        self.assertEqual(icon_preview.getpixel((0, 0))[3], 0)
        self.assertEqual(icon_preview.getpixel((64, 64)), (0, 0, 255, 128))
        with self.package(composition(home=home, **{"fn:friend": icon, "ct:folder": content})) as archive:
            self.assertEqual(pixels(archive.read(IMAGE_SLOTS["home"]["path"])).tobytes(), preview.tobytes())
            self.assertEqual(pixels(archive.read(IMAGE_SLOTS["function"]["path"])).tobytes(), preview.tobytes())
            self.assertEqual(pixels(archive.read(IMAGE_SLOTS["fn:friend"]["path"])).tobytes(), icon_preview.tobytes())
            self.assertEqual(pixels(archive.read(IMAGE_SLOTS["fn:friend"]["glow_path"])).size, (152, 152))
            self.assertEqual(pixels(archive.read(IMAGE_SLOTS["ct:folder"]["path"])).size, (512, 512))
            self.assertIn("theme.xml", archive.namelist())
        self.assertEqual(self.client.get(blue["original"]).status_code, 200)
        self.assertEqual(hashlib.sha256(self.client.get(blue["original"]).content).hexdigest(), blue["id"])

    def test_animation_uses_preview_pixels_and_preserves_scene_contract(self):
        red = self.import_swatch("marca", (240, 20, 50, 255))
        overlay = surface(layer(red["id"], "overlay", x=480, y=270, width=480, height=270))
        comp = composition(animated=overlay)
        prefix = "scene/background.raf/"
        for width, height in [(640, 360), (960, 540), (1280, 720)]:
            with self.subTest(size=(width, height)):
                with (HERE / "sample.mp4").open("rb") as video:
                    result = self.client.post("/api/animated/upload", params=self.params,
                                              data={"frames": 2, "size": f"{width}x{height}"},
                                              files={"file": ("sample.mp4", video)})
                self.assertEqual(result.status_code, 200, result.text)
                job = result.json()["job_id"]
                route = f"/api/animated/{job}/export"
                preview = self.client.post("/api/composition/animation", params=self.params, json={
                    "job": job, "frames": 2, "size": [width, height], "surface": overlay,
                })
                self.assertEqual(preview.status_code, 200, preview.text)
                first = self.client.get(preview.json()["frames"][0], params=self.params)
                image = pixels(first.content)
                self.assertEqual(image.size, (width, height))
                for channel, expected in zip(image.getpixel((width // 4, height // 4)), (240, 20, 50, 255)):
                    self.assertAlmostEqual(channel, expected, delta=1)
                export_args = {"frames": 2, "wait_seconds": 0.12, "backend": "native"}
                canonical = self.client.post(route, params=self.params, data=export_args)
                self.assertEqual(canonical.status_code, 200, canonical.text)
                before = self.client.get(canonical.json()["download"], params=self.params).content
                composed = self.client.post(route, params=self.params, data={
                    **export_args, "composition": json.dumps(comp), "animation_size": f"{width}x{height}",
                })
                self.assertEqual(composed.status_code, 200, composed.text)
                after = self.client.get(composed.json()["download"], params=self.params).content
                with zipfile.ZipFile(io.BytesIO(before)) as original, zipfile.ZipFile(io.BytesIO(after)) as changed:
                    self.assertEqual(original.namelist(), changed.namelist())
                    for name in ("plane.mdx", "index.xml"):
                        self.assertEqual(original.read(prefix + name), changed.read(prefix + name))
                    expected = dds.encode_frame(np.asarray(image), "native")
                    self.assertEqual(changed.read(prefix + "00.dds"), expected)
                    self.assertNotEqual(original.read(prefix + "00.dds"), expected)
                invalid = self.client.post(route, params=self.params, data={
                    **export_args, "composition": json.dumps(comp), "frames": max_frames_for((width, height)) + 1,
                })
                self.assertEqual(invalid.status_code, 422)
                if width == 640:
                    with patch.object(theme, "zip_raf_folder", side_effect=OSError("simulated disk full")):
                        failure = self.client.post(route, params=self.params, data={
                            **export_args, "composition": json.dumps(comp), "animation_size": "640x360",
                        })
                    self.assertEqual(failure.status_code, 503)
                    self.assertEqual(self.client.get(composed.json()["download"], params=self.params).content, after)

    def test_manifest_legacy_validation_revision_and_session(self):
        red = self.import_swatch("original", (255, 0, 0, 255))
        editor = state()
        self.assertEqual(self.save(editor).status_code, 200)
        editor["composition"] = composition(home=surface(layer(red["id"], "fundo")))
        saved = self.save(editor, 2)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.save(editor, 1).status_code, 409)
        alternative = copy.deepcopy(editor)
        alternative["composition"]["surfaces"]["home"]["layers"][0]["asset"] = "f" * 64
        self.assertEqual(self.save(alternative, 3).status_code, 422)
        alternative["composition"]["surfaces"]["home"]["layers"][0]["asset"] = "../original"
        self.assertEqual(self.save(alternative, 3).status_code, 422)
        alternative = copy.deepcopy(editor)
        alternative["composition"]["surfaces"]["home"]["layers"][0]["x"] = "NaN"
        self.assertEqual(self.save(alternative, 3).status_code, 422)
        with TestClient(app) as stranger:
            stranger.get("/api/projects")
            self.assertEqual(stranger.get(f"/api/composition/raster/{red['id']}", params=self.params).status_code, 404)
            other = stranger.post("/api/projects", json={"name": "Outro"}).json()["id"]
            self.assertEqual(stranger.get(f"/api/composition/raster/{red['id']}", params={"project": other}).status_code, 422)
        clone = self.client.post(f"/api/projects/{self.project}/duplicate", json={"name": "Cópia"}).json()
        self.assertEqual(clone["state"]["composition"], saved.json()["state"]["composition"])
        copy_state = clone["state"]
        copy_state["composition"]["surfaces"]["home"]["layers"][0]["x"] = 20
        self.assertEqual(self.client.put(f"/api/projects/{clone['id']}", json={
            "revision": 1, "name": "Cópia", "state": copy_state,
        }).status_code, 200)
        self.assertEqual(self.client.get(f"/api/projects/{self.project}").json()["state"], saved.json()["state"])
        source = next(self.temp.rglob("source.png"))
        source.rename(source.with_name("simulated-missing.png"))
        failed = self.client.post("/api/composition/image", params=self.params, json={
            "target": "home", "surface": editor["composition"]["surfaces"]["home"],
        })
        self.assertEqual(failed.status_code, 422)
        self.assertEqual(self.client.get(f"/api/projects/{self.project}").json()["state"], saved.json()["state"])

    def test_uncomposed_png_matches_canonical_encoder(self):
        with (HERE / "sample.png").open("rb") as image:
            raw = self.client.post("/api/slot/home/upload", params=self.params,
                                   files={"file": ("sample.png", image)})
        self.assertEqual(raw.status_code, 200)
        output = self.temp / "expected.png"
        spec = IMAGE_SLOTS["home"]
        media.render_image(HERE / "sample.png", output, spec["size"])
        with self.package(slots=json.dumps({"home": raw.json()["job_id"]})) as archive:
            self.assertEqual(archive.read(spec["path"]), output.read_bytes())


if __name__ == "__main__":
    unittest.main()
