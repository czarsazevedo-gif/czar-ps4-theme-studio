"""Checkpoint C: binary param.sfo, CZAR Bundle Validator, RAF byte-equivalence,
icon integrity, static/animated/audio/SFX combinations, and saved-project export."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from app import dds, jobs, pkg, projects
from app.forge.audio_validation import validate_at9, write_pcm16_wav
from app.forge.bundle_validator import validate_theme_bundle
from app.forge.sfo import FMT_INT32, FMT_UTF8, SfoEntry, build_param_sfo_bytes, encode_param_sfo, parse_param_sfo
from app.main import app
from test_projects import state as editor_state

HERE = Path(__file__).resolve().parent


def _make_wav(dest: Path, duration: float = 2.5, sr: int = 48000) -> Path:
    t = np.arange(int(sr * duration), dtype=np.float64) / float(sr)
    sig = 0.3 * np.sin(2.0 * np.pi * 220.0 * t)
    return write_pcm16_wav(np.stack([sig, sig], axis=1), dest, sample_rate=sr)


class ForgeCheckpointCSfoTest(unittest.TestCase):
    def test_param_sfo_binary_theme_roundtrip(self):
        meta = pkg.PackageMeta(
            title="Meu Tema PS4",
            title_id="CUSA00123",
            content_id="UP9000-CUSA00123_00-MEUTEMA000000000",
            category="ac",
            passcode="0" * 32,
            mode="theme",
            bgm_loudness=-28.15,
        )
        raw = build_param_sfo_bytes(meta, c_date="20261120")
        self.assertTrue(raw.startswith(b"\x00PSF"))
        parsed = parse_param_sfo(raw)
        vals = parsed["values"]
        self.assertEqual(vals["CATEGORY"], "ac")
        self.assertEqual(vals["IRO_TAG"], 2)
        self.assertEqual(vals["FORMAT"], "obs")
        self.assertEqual(vals["TITLE"], "Meu Tema PS4")
        self.assertEqual(vals["TITLE_ID"], "CUSA00123")
        self.assertEqual(vals["CONTENT_ID"], "UP9000-CUSA00123_00-MEUTEMA000000000")
        self.assertEqual(vals["CONTENT_VER"], "01.00")
        self.assertEqual(vals["VERSION"], "01.00")
        self.assertEqual(vals["PUBTOOLINFO"], "c_date=20261120,bgmhome_loud=-28.15")
        self.assertNotIn("APP_TYPE", vals)
        self.assertNotIn("APP_VER", vals)
        self.assertEqual(parsed["keys"], sorted(parsed["keys"]))

        # Without music, bgmhome_loud must NOT appear
        meta_silent = pkg.PackageMeta(
            title="Silencioso",
            title_id="CUSA00123",
            content_id="UP9000-CUSA00123_00-SILENCIOSO000000",
            category="ac",
            passcode="0" * 32,
            mode="theme",
            bgm_loudness=None,
        )
        vals_s = parse_param_sfo(build_param_sfo_bytes(meta_silent, c_date="20261120"))["values"]
        self.assertEqual(vals_s["PUBTOOLINFO"], "c_date=20261120")
        self.assertNotIn("bgmhome_loud", vals_s["PUBTOOLINFO"])


class ForgeCheckpointCBundleValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(projects, "ROOT", self.tmp / "workspace"))
        self.client = self.enterContext(TestClient(app))
        self.assertEqual(self.client.get("/api/projects").status_code, 200)

    def _create_project(self, name: str) -> str:
        r = self.client.post("/api/projects", json={"name": name})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def test_static_theme_without_audio_validates_and_exports(self):
        proj_id = self._create_project("Estatico Limpo")
        params = {"project": proj_id}

        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh)}).json()
        with (HERE / "sample.png").open("rb") as fh:
            fn_icon = self.client.post("/api/slot/fn:trophy/upload", params=params, files={"file": ("trophy.png", fh)}).json()
        with (HERE / "sample.png").open("rb") as fh:
            ct_icon = self.client.post("/api/slot/ct:browser/upload", params=params, files={"file": ("browser.png", fh)}).json()

        st = editor_state()
        st["slots"] = {"home": home["job_id"], "fn:trophy": fn_icon["job_id"], "ct:browser": ct_icon["job_id"]}
        st["bgMode"] = "static"
        st["campos"]["pkg-content-id"] = "UP9000-CUSA00123_00-ESTATICO00000000"
        self.assertEqual(
            self.client.put(f"/api/projects/{proj_id}", json={"revision": 1, "name": "Estatico Limpo", "state": st}).status_code,
            200,
        )

        # Validate from saved project state
        val = self.client.post("/api/forge/validate", params=params, data={"use_saved_project": "true"})
        self.assertEqual(val.status_code, 200, val.text)
        rep = val.json()["report"]
        self.assertTrue(rep["valid"], rep["errors"])
        self.assertEqual(rep["theme_kind"], "static")
        self.assertFalse(rep["has_music"])
        self.assertFalse(rep["has_sfx"])

        # Export validated bundle ZIP
        exp = self.client.post("/api/forge/bundle/export", params=params, data={"use_saved_project": "true"})
        self.assertEqual(exp.status_code, 200, exp.text)
        dl = self.client.get(exp.json()["download"], params=params)
        self.assertEqual(dl.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
            names = set(zf.namelist())
            self.assertIn("Image0/theme.xml", names)
            self.assertIn("Image0/texture/background/homescreen.png", names)
            self.assertIn("Image0/texture/background/functionscreen.png", names)
            self.assertIn("Image0/texture/preview.png", names)
            self.assertIn("Image0/texture/function_icon/trophy.png", names)
            self.assertIn("Image0/texture/function_icon/trophy_glow.png", names)
            self.assertIn("Image0/texture/content_icon/browser.png", names)
            self.assertIn("Sc0/param.sfo", names)
            self.assertIn("Sc0/icon0.png", names)
            self.assertIn("Sc0/pic1.png", names)
            self.assertIn("theme.gp4", names)
            self.assertIn("forge-bundle-report.json", names)
            self.assertFalse(any("sound/" in n for n in names))
            self.assertFalse(any("background.raf" in n for n in names))
            # Check exact icon sizes inside ZIP
            with Image.open(io.BytesIO(zf.read("Image0/texture/function_icon/trophy.png"))) as im:
                self.assertEqual(im.size, (128, 128))
            with Image.open(io.BytesIO(zf.read("Image0/texture/function_icon/trophy_glow.png"))) as im:
                self.assertEqual(im.size, (152, 152))
            with Image.open(io.BytesIO(zf.read("Image0/texture/content_icon/browser.png"))) as im:
                self.assertEqual(im.size, (512, 512))
            with Image.open(io.BytesIO(zf.read("Sc0/icon0.png"))) as im:
                self.assertEqual(im.size, (512, 512))

    def test_animated_theme_with_music_sfx_and_deluxe_raf_byte_equivalence(self):
        """Animated theme with Deluxe/Ouroboros DDS frames, ATRAC9 music, and VAG SFX
        must produce byte-for-byte identical RAF files between editor export and theme bundle."""
        proj_id = self._create_project("Animado Completo")
        params = {"project": proj_id}

        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh)}).json()
        with (HERE / "sample.mp4").open("rb") as fh:
            anim = self.client.post(
                "/api/animated/upload",
                params=params,
                data={"frames": "6", "size": "640x360"},
                files={"file": ("clip.mp4", fh)},
            ).json()

        # Simulate an applied Deluxe/Ouroboros job with exact pre-approved DDS frames
        owner_dir = next((self.tmp / "workspace").iterdir())
        anim_root = owner_dir / proj_id / "media" / f"animated-{anim['job_id']}"
        dds_dir = anim_root / "deluxe_dds"
        dds_dir.mkdir(exist_ok=True)
        for i in range(6):
            rgba = np.full((360, 640, 4), [i * 35, 100, 200 - i * 25, 255], dtype=np.uint8)
            (dds_dir / f"{i:02d}.dds").write_bytes(dds.encode_frame(rgba, "native"))
        job_json = anim_root / "job.json"
        meta_raw = json.loads(job_json.read_text(encoding="utf-8"))
        meta_raw["meta"]["deluxe"] = {"frames": 6, "size": [640, 360], "wait": 0.12}
        job_json.write_text(json.dumps(meta_raw, indent=2), encoding="utf-8")

        # Export RAF via editor endpoint
        raf_exp = self.client.post(
            f"/api/animated/{anim['job_id']}/export",
            params=params,
            data={"frames": "6", "wait_seconds": "0.12", "backend": "native"},
        )
        self.assertEqual(raf_exp.status_code, 200, raf_exp.text)
        raf_zip_bytes = self.client.get(raf_exp.json()["download"], params=params).content

        # Add music (WAV -> ATRAC9) and SFX (WAV -> VAG)
        wav = _make_wav(self.tmp / "bgm.wav")
        with wav.open("rb") as fh:
            bgm = self.client.post(
                "/api/forge/audio/convert-apply",
                params=params,
                files={"file": ("bgm.wav", fh, "audio/wav")},
            ).json()
        with (HERE / "sample_sfx.wav").open("rb") as fh:
            se_dec = self.client.post("/api/audio/se_decide/upload", params=params, files={"file": ("dec.wav", fh)}).json()

        st = editor_state()
        st["slots"] = {"home": home["job_id"]}
        st["bgMode"] = "animated"
        st["anim"] = {"jobId": anim["job_id"], "size": [640, 360], "frames": 6, "wait": 0.12, "backend": "native"}
        st["bgm"] = bgm["job_id"]
        st["sfx"] = {"se_decide": se_dec["job_id"]}
        st["campos"]["pkg-content-id"] = "UP9000-CUSA00123_00-ANIMADO000000000"
        self.assertEqual(
            self.client.put(f"/api/projects/{proj_id}", json={"revision": 1, "name": "Animado Completo", "state": st}).status_code,
            200,
        )

        # Export complete validated bundle
        exp = self.client.post("/api/forge/bundle/export", params=params, data={"use_saved_project": "true"})
        self.assertEqual(exp.status_code, 200, exp.text)
        rep = exp.json()["report"]
        self.assertTrue(rep["valid"], rep["errors"])
        self.assertEqual(rep["theme_kind"], "animated")
        self.assertTrue(rep["has_music"])
        self.assertTrue(rep["has_sfx"])

        bundle_bytes = self.client.get(exp.json()["download"], params=params).content
        with zipfile.ZipFile(io.BytesIO(raf_zip_bytes)) as z_raf, zipfile.ZipFile(io.BytesIO(bundle_bytes)) as z_bun:
            # Byte-for-byte RAF equivalence!
            for member in z_raf.namelist():
                self.assertIn(f"Image0/{member}", z_bun.namelist())
                self.assertEqual(
                    z_raf.read(member),
                    z_bun.read(f"Image0/{member}"),
                    f"RAF mismatch on {member}",
                )
            # No dead texture/background/ files in animated theme
            self.assertFalse(any(n.startswith("Image0/texture/background/") for n in z_bun.namelist()))
            # Validate Sc0/param.sfo inside bundle
            sfo_vals = parse_param_sfo(z_bun.read("Sc0/param.sfo"))["values"]
            self.assertEqual(sfo_vals["CATEGORY"], "ac")
            self.assertEqual(sfo_vals["IRO_TAG"], 2)
            self.assertIn("bgmhome_loud=", sfo_vals["PUBTOOLINFO"])
            # Independently validate exported bgm_home.at9
            at9_tmp = self.tmp / "bun_bgm.at9"
            at9_tmp.write_bytes(z_bun.read("Image0/sound/bgm_home.at9"))
            self.assertTrue(validate_at9(at9_tmp, require_ps4_profile=True, require_loop=True).valid)

    def test_validator_catches_invalid_iro_tag_category_xml_contradiction_icons_and_raf_budget(self):
        """Negative tests ensuring CZAR Bundle Validator catches every critical PS4 defect."""
        proj_id = self._create_project("Base Valida")
        params = {"project": proj_id}
        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh)}).json()
        st = editor_state()
        st["slots"] = {"home": home["job_id"]}
        st["campos"]["pkg-content-id"] = "UP9000-CUSA00123_00-NEGATIVOS0000000"
        self.client.put(f"/api/projects/{proj_id}", json={"revision": 1, "name": "Base Valida", "state": st})
        exp = self.client.post("/api/forge/bundle/export", params=params, data={"use_saved_project": "true"})
        self.assertEqual(exp.status_code, 200, exp.text)

        # Locate assembled content_dir to mutate isolated copies
        owner_dir = next((self.tmp / "workspace").iterdir())
        src_build = owner_dir / proj_id / "media" / f"package-{exp.json()['job_id']}" / "build"
        src_content = src_build / "content"
        src_gp4 = src_build / "theme.gp4"

        def clone_case(name: str) -> tuple[Path, Path]:
            dst = self.tmp / name
            shutil.copytree(src_content, dst / "content")
            shutil.copyfile(src_gp4, dst / "theme.gp4")
            return dst / "content", dst / "theme.gp4"

        # 1. XML contradicts audio (homebgm-enable=True without bgm_home.at9)
        c1, g1 = clone_case("case_xml_bgm")
        tx = c1 / "theme.xml"
        tx.write_text(tx.read_text(encoding="utf-8").replace("False", "True"), encoding="utf-8")
        r1 = validate_theme_bundle(c1, gp4_path=g1)
        self.assertFalse(r1.valid)
        self.assertTrue(any("bgm_home.at9" in e for e in r1.errors))

        # 2. Wrong IRO_TAG (0 instead of 2) in param.sfo
        c2, g2 = clone_case("case_iro_tag")
        bad_sfo = encode_param_sfo([
            SfoEntry("ATTRIBUTE", FMT_INT32, 0, 4),
            SfoEntry("CATEGORY", FMT_UTF8, "ac", 4),
            SfoEntry("CONTENT_ID", FMT_UTF8, "UP9000-CUSA00123_00-NEGATIVOS0000000", 48),
            SfoEntry("FORMAT", FMT_UTF8, "obs", 4),
            SfoEntry("IRO_TAG", FMT_INT32, 0, 4),
            SfoEntry("PUBTOOLINFO", FMT_UTF8, "c_date=20261120", 512),
            SfoEntry("TITLE", FMT_UTF8, "Bad IRO", 128),
            SfoEntry("TITLE_ID", FMT_UTF8, "CUSA00123", 12),
            SfoEntry("VERSION", FMT_UTF8, "01.00", 8),
        ])
        (c2 / "sce_sys" / "param.sfo").write_bytes(bad_sfo)
        r2 = validate_theme_bundle(c2, gp4_path=g2)
        self.assertFalse(r2.valid)
        self.assertTrue(any("IRO_TAG" in e for e in r2.errors))

        # 3. Wrong CATEGORY ('gd' instead of 'ac') in param.sfo
        c3, g3 = clone_case("case_category")
        bad_cat = encode_param_sfo([
            SfoEntry("ATTRIBUTE", FMT_INT32, 0, 4),
            SfoEntry("CATEGORY", FMT_UTF8, "gd", 4),
            SfoEntry("CONTENT_ID", FMT_UTF8, "UP9000-CUSA00123_00-NEGATIVOS0000000", 48),
            SfoEntry("FORMAT", FMT_UTF8, "obs", 4),
            SfoEntry("IRO_TAG", FMT_INT32, 2, 4),
            SfoEntry("PUBTOOLINFO", FMT_UTF8, "c_date=20261120", 512),
            SfoEntry("TITLE", FMT_UTF8, "Bad Cat", 128),
            SfoEntry("TITLE_ID", FMT_UTF8, "CUSA00123", 12),
            SfoEntry("VERSION", FMT_UTF8, "01.00", 8),
        ])
        (c3 / "sce_sys" / "param.sfo").write_bytes(bad_cat)
        r3 = validate_theme_bundle(c3, gp4_path=g3)
        self.assertFalse(r3.valid)
        self.assertTrue(any("CATEGORY" in e for e in r3.errors))

        # 4. Missing glow icon & wrong function icon dimensions
        c4, g4 = clone_case("case_icons")
        fn_dir = c4 / "texture" / "function_icon"
        fn_dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(fn_dir / "power.png")
        r4 = validate_theme_bundle(c4)
        self.assertFalse(r4.valid)
        self.assertTrue(any("128×128" in e or "glow" in e.lower() for e in r4.errors))


if __name__ == "__main__":
    unittest.main()
