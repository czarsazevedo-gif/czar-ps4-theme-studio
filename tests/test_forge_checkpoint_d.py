"""Checkpoint D: CZAR PKG Forge native Linux PS4 Theme PKG generation, binary IRO_TAG=2
and cryptographic digest verification, independent extraction + SHA-256 parity,
tamper/negative detection, async job polling, and restart persistence."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from app import dds, projects
from app.forge.audio_validation import validate_at9, write_pcm16_wav
from app.forge.pkg_forge import find_liborbispkg_tool, pkg_forge_info
from app.forge.pkg_inspector import inspect_and_validate_pkg, parse_pkg_binary
from app.forge.sfo import parse_param_sfo
from app.main import app
from test_projects import state as editor_state

HERE = Path(__file__).resolve().parent


def _make_wav(dest: Path, duration: float = 2.5, sr: int = 48000) -> Path:
    t = np.arange(int(sr * duration), dtype=np.float64) / float(sr)
    sig = 0.25 * np.sin(2.0 * np.pi * 220.0 * t) + 0.1 * np.sin(2.0 * np.pi * 440.0 * t)
    return write_pcm16_wav(np.stack([sig, sig], axis=1), dest, sample_rate=sr)


class ForgeCheckpointDPkgTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(projects, "ROOT", self.tmp / "workspace"))
        self.client = self.enterContext(TestClient(app))
        self.assertEqual(self.client.get("/api/projects").status_code, 200)
        self.assertTrue(
            pkg_forge_info()["available"],
            "Native Linux LibOrbisPkg PkgTool not installed; run ./scripts/install_forge_tools.sh pkg",
        )

    def _create_project(self, name: str) -> str:
        r = self.client.post("/api/projects", json={"name": name})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def _poll_pkg_job(self, client: TestClient, task_id: str, params: dict, timeout: float = 120.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = client.get(f"/api/forge/pkg/jobs/{task_id}", params=params)
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            if data["status"] in {"completed", "failed", "cancelled"}:
                return data
            time.sleep(0.2)
        self.fail(f"PKG Forge task {task_id} timed out after {timeout}s")

    def test_full_animated_music_sfx_icons_theme_pkg_build_independent_validation_and_restart(self):
        """End-to-end PS4 Theme PKG: animated Deluxe RAF + ATRAC9 music + VAG SFX + icons ->
        async build -> independent binary/crypto/Sc0/Image0 verification -> tamper rejection -> restart."""
        proj_id = self._create_project("Forja PS4 Completa")
        params = {"project": proj_id}

        # 1. Home + function icon + content icon
        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh)}).json()
        with (HERE / "sample.png").open("rb") as fh:
            fn_pow = self.client.post("/api/slot/fn:power/upload", params=params, files={"file": ("pow.png", fh)}).json()
        with (HERE / "sample.png").open("rb") as fh:
            ct_lib = self.client.post("/api/slot/ct:library/upload", params=params, files={"file": ("lib.png", fh)}).json()

        # 2. Animated background with pre-approved Deluxe DDS frames
        with (HERE / "sample.mp4").open("rb") as fh:
            anim = self.client.post(
                "/api/animated/upload",
                params=params,
                data={"frames": "4", "size": "640x360"},
                files={"file": ("loop.mp4", fh)},
            ).json()
        owner_dir = next((self.tmp / "workspace").iterdir())
        anim_root = owner_dir / proj_id / "media" / f"animated-{anim['job_id']}"
        dds_dir = anim_root / "deluxe_dds"
        dds_dir.mkdir(exist_ok=True)
        for i in range(4):
            rgba = np.full((360, 640, 4), [30 + i * 50, 90, 210 - i * 40, 255], dtype=np.uint8)
            (dds_dir / f"{i:02d}.dds").write_bytes(dds.encode_frame(rgba, "native"))
        jj = anim_root / "job.json"
        meta_raw = json.loads(jj.read_text(encoding="utf-8"))
        meta_raw["meta"]["deluxe"] = {"frames": 4, "size": [640, 360], "wait": 0.10}
        jj.write_text(json.dumps(meta_raw, indent=2), encoding="utf-8")

        # 3. Music (WAV -> ATRAC9 via CZAR ATRAC9 Studio ambient mode) + VAG SFX
        wav = _make_wav(self.tmp / "theme_music.wav", duration=2.5)
        with wav.open("rb") as fh:
            bgm = self.client.post(
                "/api/forge/audio/convert-apply",
                params=params,
                data={"volume_mode": "ambient", "loop_mode": "whole"},
                files={"file": ("theme_music.wav", fh, "audio/wav")},
            ).json()
        self.assertEqual(bgm["codec"], "atrac9")
        self.assertIsNotNone(bgm["loudness"])

        sfx_ids = {}
        for slot in ("se_decide", "se_cursor", "se_cancel"):
            with (HERE / "sample_sfx.wav").open("rb") as fh:
                sr = self.client.post(f"/api/audio/{slot}/upload", params=params, files={"file": (f"{slot}.wav", fh)}).json()
            sfx_ids[slot] = sr["job_id"]

        # 4. Save project
        st = editor_state()
        st["slots"] = {"home": home["job_id"], "fn:power": fn_pow["job_id"], "ct:library": ct_lib["job_id"]}
        st["bgMode"] = "animated"
        st["anim"] = {"jobId": anim["job_id"], "size": [640, 360], "frames": 4, "wait": 0.10, "backend": "native"}
        st["bgm"] = bgm["job_id"]
        st["bgmGain"] = bgm["gain_db"]
        st["sfx"] = sfx_ids
        st["campos"]["pkg-title"] = "Czar Forja Teste"
        st["campos"]["pkg-title-id"] = "CUSA00123"
        st["campos"]["pkg-content-id"] = "UP9000-CUSA00123_00-CZARFORJATESTE00"
        self.assertEqual(
            self.client.put(f"/api/projects/{proj_id}", json={"revision": 1, "name": "Forja PS4 Completa", "state": st}).status_code,
            200,
        )

        # 5. Export validated bundle first (for cross-check)
        bun = self.client.post("/api/forge/bundle/export", params=params, data={"use_saved_project": "true"})
        self.assertEqual(bun.status_code, 200, bun.text)
        bun_bytes = self.client.get(bun.json()["download"], params=params).content

        # 6. Launch async PKG Forge job from saved project state!
        launch = self.client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true"})
        self.assertEqual(launch.status_code, 202, launch.text)
        task_id = launch.json()["task_id"]

        done = self._poll_pkg_job(self.client, task_id, params)
        self.assertEqual(done["status"], "completed", done)
        pkg_rep = done["pkg_report"]
        self.assertTrue(pkg_rep["pkg_created"])
        self.assertTrue(pkg_rep["structure_validated"], pkg_rep["errors"])
        self.assertTrue(pkg_rep["theme_type_valid"])
        self.assertEqual(pkg_rep["volume_type"], "pkg_ps4_theme")
        self.assertEqual(pkg_rep["category"], "ac")
        self.assertEqual(pkg_rep["iro_tag_header"], 2)
        self.assertEqual(pkg_rep["iro_tag_sfo"], 2)
        self.assertEqual(pkg_rep["content_type_header"], 0x1B)
        self.assertTrue(pkg_rep["extracted_match"])
        self.assertEqual(pkg_rep["hash_mismatches"], [])
        self.assertEqual(pkg_rep["physical_compatibility"], "pending")
        self.assertIsNotNone(pkg_rep["bgmhome_loud"])
        self.assertAlmostEqual(float(pkg_rep["bgmhome_loud"]), bgm["loudness"], delta=0.02)

        # 7. Download .pkg and independently verify raw binary bytes!
        dl = self.client.get(done["download"], params=params)
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(len(dl.content), done["pkg_bytes"])
        self.assertEqual(hashlib.sha256(dl.content).hexdigest(), pkg_rep["pkg_sha256"])

        pkg_file = self.tmp / done["pkg_name"]
        pkg_file.write_bytes(dl.content)

        # Direct raw binary checks at exact Sony PS4 offsets
        raw_hdr = dl.content[:0x1100]
        self.assertEqual(raw_hdr[0:4], b"\x7fCNT")
        self.assertEqual(struct.unpack_from(">I", raw_hdr, 0x74)[0], 0x1B)  # ContentType.AC
        self.assertEqual(struct.unpack_from(">I", raw_hdr, 0x98)[0], 2)     # IROTag.SS_THEME
        self.assertEqual(hashlib.sha256(raw_hdr[0:0xFE0]).digest(), raw_hdr[0xFE0:0x1000])

        parsed = parse_pkg_binary(pkg_file)
        self.assertTrue(parsed["header_digest_ok"])
        self.assertTrue(parsed["header_signature_present"])
        self.assertTrue(parsed["body_digest_ok"])
        self.assertTrue(parsed["digest_table_ok"])
        self.assertTrue(parsed["pfs_image_digest_ok"])
        self.assertTrue(parsed["pfs_signed_digest_ok"])
        self.assertTrue(parsed["sc_entries1_hash_ok"])
        self.assertTrue(parsed["sc_entries2_hash_ok"])
        self.assertEqual(parsed["entry_digest_mismatches"], [])

        # Verify Sc0/param.sfo parsed directly from raw PKG bytes
        sfo_vals = parse_param_sfo(parsed["sc0_blobs"]["Sc0/param.sfo"])["values"]
        self.assertEqual(sfo_vals["CATEGORY"], "ac")
        self.assertEqual(sfo_vals["IRO_TAG"], 2)
        self.assertEqual(sfo_vals["CONTENT_ID"], "UP9000-CUSA00123_00-CZARFORJATESTE00")
        self.assertNotIn("APP_TYPE", sfo_vals)
        self.assertIn(f"bgmhome_loud={bgm['loudness']:.2f}", sfo_vals["PUBTOOLINFO"])

        # Cross-verify every Image0 file hash in pkg_report matches the validated bundle ZIP!
        pkg_hashes = {f["path"]: f["sha256"] for f in pkg_rep["files"]}
        with zipfile.ZipFile(io.BytesIO(bun_bytes)) as zf:
            for member in zf.namelist():
                if not member.startswith("Image0/"):
                    continue
                self.assertIn(member, pkg_hashes, f"Missing {member} in PKG")
                self.assertEqual(
                    hashlib.sha256(zf.read(member)).hexdigest(),
                    pkg_hashes[member],
                    f"SHA-256 mismatch between bundle ZIP and PKG on {member}",
                )

        # 8. Tamper detection tests (Sections 18, 20, 23 Q/R/S)
        # 8a. Corrupt IRO_TAG (0x98 -> 0) after build
        bad_iro = self.tmp / "bad_iro.pkg"
        tampered = bytearray(dl.content)
        struct.pack_into(">I", tampered, 0x98, 0)
        bad_iro.write_bytes(tampered)
        rep_bad_iro = inspect_and_validate_pkg(bad_iro, pkgtool_bin=find_liborbispkg_tool())
        self.assertFalse(rep_bad_iro.structure_validated)
        self.assertFalse(rep_bad_iro.theme_type_valid)
        self.assertEqual(rep_bad_iro.iro_tag_header, 0)
        self.assertTrue(any("IRO_TAG" in e for e in rep_bad_iro.errors))
        self.assertTrue(any("HeaderDigest" in e for e in rep_bad_iro.errors))

        # 8b. Corrupt ContentType (0x74 -> 0x1A GD app)
        bad_ct = self.tmp / "bad_ct.pkg"
        tampered2 = bytearray(dl.content)
        struct.pack_into(">I", tampered2, 0x74, 0x1A)
        bad_ct.write_bytes(tampered2)
        rep_bad_ct = inspect_and_validate_pkg(bad_ct, pkgtool_bin=find_liborbispkg_tool())
        self.assertFalse(rep_bad_ct.theme_type_valid)
        self.assertTrue(any("content_type" in e for e in rep_bad_ct.errors))

        # 8c. Truncated incomplete PKG
        trunc = self.tmp / "truncated.pkg"
        trunc.write_bytes(dl.content[: len(dl.content) // 2])
        rep_trunc = inspect_and_validate_pkg(trunc, pkgtool_bin=find_liborbispkg_tool())
        self.assertFalse(rep_trunc.structure_validated)

        # 8d. Standalone /api/forge/pkg/inspect endpoint
        with pkg_file.open("rb") as fh:
            ins_ok = self.client.post("/api/forge/pkg/inspect", files={"file": (pkg_file.name, fh)})
        self.assertEqual(ins_ok.status_code, 200, ins_ok.text)
        self.assertTrue(ins_ok.json()["structure_validated"])
        self.assertTrue(ins_ok.json()["theme_type_valid"])

        with bad_iro.open("rb") as fh:
            ins_bad = self.client.post("/api/forge/pkg/inspect", files={"file": ("bad_iro.pkg", fh)})
        self.assertEqual(ins_bad.status_code, 200)
        self.assertFalse(ins_bad.json()["structure_validated"])
        self.assertFalse(ins_bad.json()["theme_type_valid"])

        # 9. Server restart recovery: persisted PKG history, report, and download still work!
        cookies = self.client.cookies
        with TestClient(app, cookies=cookies) as restarted:
            hist = restarted.get("/api/forge/pkg/history", params=params)
            self.assertEqual(hist.status_code, 200)
            items = hist.json()["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["pkg_id"], done["pkg_id"])
            rep_after = restarted.get(items[0]["report_url"], params=params)
            self.assertEqual(rep_after.status_code, 200)
            self.assertEqual(rep_after.json()["pkg_report"]["iro_tag_header"], 2)
            dl_after = restarted.get(items[0]["download"], params=params)
            self.assertEqual(dl_after.status_code, 200)
            self.assertEqual(dl_after.content, dl.content)

    def test_static_theme_without_music_pkg_has_no_bgmhome_loud_and_valid_iro_tag(self):
        proj_id = self._create_project("Estatico PKG")
        params = {"project": proj_id}
        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh)}).json()
        st = editor_state()
        st["slots"] = {"home": home["job_id"]}
        st["bgMode"] = "static"
        st["campos"]["pkg-title"] = "Tema Estatico"
        st["campos"]["pkg-content-id"] = "UP9000-CUSA00123_00-TEMAESTATICO0000"
        self.client.put(f"/api/projects/{proj_id}", json={"revision": 1, "name": "Estatico PKG", "state": st})

        r = self.client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
        self.assertEqual(r.status_code, 200, r.text)
        rep = r.json()["pkg_report"]
        self.assertTrue(rep["structure_validated"], rep["errors"])
        self.assertTrue(rep["theme_type_valid"])
        self.assertEqual(rep["iro_tag_header"], 2)
        self.assertEqual(rep["iro_tag_sfo"], 2)
        self.assertEqual(rep["category"], "ac")
        self.assertIsNone(rep["bgmhome_loud"])
        paths = {f["path"] for f in rep["files"]}
        self.assertIn("Image0/texture/background/homescreen.png", paths)
        self.assertIn("Image0/texture/background/functionscreen.png", paths)
        self.assertFalse(any("sound/" in p for p in paths))


if __name__ == "__main__":
    unittest.main()
