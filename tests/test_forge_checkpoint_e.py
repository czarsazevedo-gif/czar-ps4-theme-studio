"""Checkpoint E — Complete A–Z integration matrix + full 19-step End-to-End test."""
from __future__ import annotations

import hashlib
import io
import json
import math
import shutil
import subprocess
import tempfile
import time
import unittest
import wave
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from app import dds, jobs, main, pkg, projects
from app.forge import pkg_forge, pkg_router
from app.forge.bundle_validator import validate_theme_bundle
from app.forge.pkg_inspector import inspect_and_validate_pkg
from app.forge.sfo import SfoEntry, build_param_sfo_bytes, encode_param_sfo
from tests.test_projects import state as editor_state

HERE = Path(__file__).resolve().parent


def _make_wav(path: Path, *, duration: float = 2.2, sr: int = 48000, freq: float = 330.0) -> Path:
    n = int(duration * sr)
    t = np.arange(n, dtype=np.float64) / sr
    left = 0.25 * np.sin(2.0 * math.pi * freq * t)
    right = 0.25 * np.sin(2.0 * math.pi * (freq * 1.25) * t)
    stereo = np.stack([left, right], axis=1)
    pcm16 = np.clip(np.round(stereo * 32767.0), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())
    return path


class ForgeCheckpointEIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="czar-forge-e-")))
        self.enterContext(mock.patch.object(projects, "ROOT", self.tmp / "workspace"))
        self.client = self.enterContext(TestClient(main.app))
        self.assertEqual(self.client.get("/api/projects").status_code, 200)

    def _new_project(self, client: TestClient, name: str) -> str:
        r = client.post("/api/projects", json={"name": name})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def _poll_pkg(self, client: TestClient, task_id: str, params: dict, timeout: float = 60.0) -> dict:
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            r = client.get(f"/api/forge/pkg/jobs/{task_id}", params=params)
            self.assertEqual(r.status_code, 200, r.text)
            last = r.json()
            if last["status"] in ("completed", "failed", "cancelled"):
                return last
            time.sleep(0.15)
        self.fail(f"PKG task {task_id} timed out: {last}")

    # =========================================================================
    # SECTION 24 — FULL 19-STEP END-TO-END FLOW (also covers B, L, M, T, U, V, W, Y)
    # =========================================================================
    def test_e2e_19_steps_create_to_ps4_pkg_download_and_restart(self):
        # 1. Criar projeto
        proj_id = self._new_project(self.client, "Tema Completo E2E")
        params = {"project": proj_id}

        # 2 & 3. Selecionar fundo animado + aplicar loop (Deluxe DDS pré-aprovado)
        with (HERE / "sample.mp4").open("rb") as fh:
            anim = self.client.post(
                "/api/animated/upload",
                params=params,
                data={"frames": "4", "size": "640x360"},
                files={"file": ("bg.mp4", fh)},
            )
        self.assertEqual(anim.status_code, 200, anim.text)
        anim_id = anim.json()["job_id"]
        owner_dir = next((self.tmp / "workspace").iterdir())
        anim_root = owner_dir / proj_id / "media" / f"animated-{anim_id}"
        dds_dir = anim_root / "deluxe_dds"
        dds_dir.mkdir(exist_ok=True)
        expected_dds = {}
        for i in range(4):
            rgba = np.full((360, 640, 4), [20 + i * 55, 110, 220 - i * 35, 255], dtype=np.uint8)
            blob = dds.encode_frame(rgba, "native")
            (dds_dir / f"{i:02d}.dds").write_bytes(blob)
            expected_dds[f"scene/background.raf/{i:02d}.dds"] = hashlib.sha256(blob).hexdigest()
        jj = anim_root / "job.json"
        raw = json.loads(jj.read_text(encoding="utf-8"))
        raw["meta"]["deluxe"] = {"frames": 4, "size": [640, 360], "wait": 0.12}
        jj.write_text(json.dumps(raw, indent=2), encoding="utf-8")

        # 4. Configurar ícones (home + function + content)
        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh)}).json()
        with (HERE / "sample.png").open("rb") as fh:
            fn_pow = self.client.post("/api/slot/fn:power/upload", params=params, files={"file": ("pow.png", fh)}).json()
        with (HERE / "sample.png").open("rb") as fh:
            ct_lib = self.client.post("/api/slot/ct:library/upload", params=params, files={"file": ("lib.png", fh)}).json()

        # 5, 6, 7. Importar música WAV -> Converter para ATRAC9 -> Aplicar música
        wav = _make_wav(self.tmp / "e2e_bgm.wav", duration=2.4)
        with wav.open("rb") as fh:
            bgm_r = self.client.post(
                "/api/forge/audio/convert-apply",
                params=params,
                data={"volume_mode": "ambient", "loop_mode": "whole"},
                files={"file": ("e2e_bgm.wav", fh, "audio/wav")},
            )
        self.assertEqual(bgm_r.status_code, 200, bgm_r.text)
        bgm = bgm_r.json()
        self.assertEqual(bgm["codec"], "atrac9")
        self.assertEqual(bgm["bitrate_kbps"], 144)
        self.assertIsNotNone(bgm["loudness"])

        # 8. Configurar efeitos VAG
        sfx_ids = {}
        for slot in ("se_decide", "se_cursor", "se_cancel"):
            with (HERE / "sample_sfx.wav").open("rb") as fh:
                sr = self.client.post(f"/api/audio/{slot}/upload", params=params, files={"file": (f"{slot}.wav", fh)})
            self.assertEqual(sr.status_code, 200, sr.text)
            sfx_ids[slot] = sr.json()["job_id"]
            # Prévia WAV do efeito funciona
            pv = self.client.get(f"/api/audio/{sfx_ids[slot]}/preview", params=params)
            self.assertEqual(pv.status_code, 200)
            self.assertEqual(pv.content[:4], b"RIFF")

        # 9. Salvar projeto
        st = editor_state()
        st["slots"] = {"home": home["job_id"], "fn:power": fn_pow["job_id"], "ct:library": ct_lib["job_id"]}
        st["bgMode"] = "animated"
        st["anim"] = {"jobId": anim_id, "size": [640, 360], "frames": 4, "wait": 0.12, "backend": "native"}
        st["bgm"] = bgm["job_id"]
        st["bgmGain"] = bgm["gain_db"]
        st["sfx"] = sfx_ids
        st["campos"]["pkg-title"] = "Czar E2E Tema"
        st["campos"]["pkg-title-id"] = "CUSA00999"
        st["campos"]["pkg-content-id"] = "UP9000-CUSA00999_00-CZARE2ETEMA00000"
        save_r = self.client.put(f"/api/projects/{proj_id}", json={"revision": 1, "name": "Tema Completo E2E", "state": st})
        self.assertEqual(save_r.status_code, 200, save_r.text)

        # 10 & 11. Recarregar / simular reinício do cliente e reabrir projeto
        client2 = TestClient(main.app)
        client2.cookies.update(self.client.cookies)
        reopened = client2.get(f"/api/projects/{proj_id}")
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(reopened.json()["state"]["bgm"], bgm["job_id"])
        self.assertEqual(client2.get(f"/api/audio/{bgm['job_id']}/preview", params=params).status_code, 200)

        # 12. Validar recursos (usando estado salvo)
        val = client2.post("/api/forge/validate", params=params, data={"use_saved_project": "true"})
        self.assertEqual(val.status_code, 200, val.text)
        self.assertTrue(val.json()["report"]["valid"], val.json())

        # 13. Exportar bundle validado
        bun = client2.post("/api/forge/bundle/export", params=params, data={"use_saved_project": "true"})
        self.assertEqual(bun.status_code, 200, bun.text)
        bun_dl = client2.get(bun.json()["download"], params=params)
        self.assertEqual(bun_dl.status_code, 200)
        self.assertIn("attachment", bun_dl.headers.get("content-disposition", "").lower())
        with zipfile.ZipFile(io.BytesIO(bun_dl.content)) as zf:
            names = set(zf.namelist())
            self.assertIn("Image0/theme.xml", names)
            self.assertIn("Image0/sound/bgm_home.at9", names)
            self.assertIn("Sc0/param.sfo", names)
            self.assertNotIn("Image0/texture/background/homescreen.png", names)  # L: animated excludes static home bg
            for rel, exp_sha in expected_dds.items():
                self.assertEqual(hashlib.sha256(zf.read(f"Image0/{rel}")).hexdigest(), exp_sha)

        # 14. Gerar PKG assincronamente
        launch = client2.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true"})
        self.assertEqual(launch.status_code, 202, launch.text)
        done = self._poll_pkg(client2, launch.json()["task_id"], params)
        self.assertEqual(done["status"], "completed", done)

        # 15, 16, 17. Validar PKG, Extrair PKG, Comparar hashes SHA-256
        pr = done["pkg_report"]
        self.assertTrue(pr["pkg_created"])
        self.assertTrue(pr["structure_validated"], pr)
        self.assertTrue(pr["theme_type_valid"])
        self.assertEqual(pr["volume_type"], "pkg_ps4_theme")
        self.assertEqual(pr["iro_tag_header"], 2)
        self.assertEqual(pr["iro_tag_sfo"], 2)
        self.assertEqual(pr["category"], "ac")
        self.assertIsNotNone(pr["bgmhome_loud"])
        self.assertTrue(pr["extracted_match"])
        self.assertEqual(pr["hash_mismatches"], [])
        file_shas = {f["path"]: f["sha256"] for f in pr["files"]}
        for rel, exp_sha in expected_dds.items():
            self.assertEqual(file_shas.get(f"Image0/{rel}"), exp_sha)
        for slot in ("se_decide", "se_cursor", "se_cancel"):
            self.assertIn(f"Image0/sound/{slot}.vag", file_shas)

        # 18. Baixar PKG (Y: Chrome Android streaming headers + exact bytes + SHA-256)
        dl = client2.get(done["download"], params=params)
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.headers.get("content-type"), "application/octet-stream")
        self.assertIn("attachment", dl.headers.get("content-disposition", "").lower())
        self.assertIn("UP9000-CUSA00999_00-CZARE2ETEMA00000.pkg", dl.headers.get("content-disposition", ""))
        self.assertEqual(int(dl.headers["content-length"]), len(dl.content))
        self.assertEqual(hashlib.sha256(dl.content).hexdigest(), pr["pkg_sha256"])

        # 19. Confirmar estado pendente de teste físico + sobrevivência a novo reinício (W)
        self.assertEqual(pr["physical_compatibility"], "pending")
        self.assertEqual(done["physical_compatibility"], "pending")
        client3 = TestClient(main.app)
        client3.cookies.update(self.client.cookies)
        hist = client3.get("/api/forge/pkg/history", params=params).json()["items"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["pkg_id"], done["pkg_id"])
        dl_after_restart = client3.get(hist[0]["download"], params=params)
        self.assertEqual(dl_after_restart.status_code, 200)
        self.assertEqual(dl_after_restart.content, dl.content)

    # =========================================================================
    # SCENARIOS A, C, D, E, F, K, N — Audio & Theme Variant Matrix
    # =========================================================================
    def test_scenarios_a_c_d_e_f_k_n_audio_and_static_matrix(self):
        proj_id = self._new_project(self.client, "Matriz Audio e Estatico")
        params = {"project": proj_id}

        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh)}).json()

        # E: Corrupted .at9 rejected with no orphan directory
        media_dir = next((self.tmp / "workspace").iterdir()) / proj_id / "media"
        before_audio_dirs = {p.name for p in media_dir.glob("audio-*")}
        bad_at9 = self.client.post(
            "/api/forge/audio/convert-apply",
            params=params,
            files={"file": ("broken.at9", io.BytesIO(b"RIFF\x20\x00\x00\x00WAVEfmt corrupted"), "application/octet-stream")},
        )
        self.assertEqual(bad_at9.status_code, 422)
        self.assertEqual({p.name for p in media_dir.glob("audio-*")}, before_audio_dirs)

        # F: Invalid WAV rejected cleanly
        bad_wav = self.client.post(
            "/api/forge/audio/convert-apply",
            params=params,
            files={"file": ("bad.wav", io.BytesIO(b"NOT A WAV FILE AT ALL"), "audio/wav")},
        )
        self.assertEqual(bad_wav.status_code, 422)

        # C: MP3 -> real ATRAC9 144 kbps
        with (HERE / "sample_music.mp3").open("rb") as fh:
            mp3_r = self.client.post(
                "/api/forge/audio/convert-apply",
                params=params,
                data={"volume_mode": "original", "loop_mode": "whole"},
                files={"file": ("track.mp3", fh, "audio/mpeg")},
            )
        self.assertEqual(mp3_r.status_code, 200, mp3_r.text)
        self.assertEqual(mp3_r.json()["bitrate_kbps"], 144)

        # D: Valid .at9 imported directly
        valid_at9_path = media_dir / f"audio-{mp3_r.json()['job_id']}" / "out" / "bgm.at9"
        with valid_at9_path.open("rb") as fh:
            at9_r = self.client.post(
                "/api/forge/audio/convert-apply",
                params=params,
                files={"file": ("ready.at9", fh, "application/octet-stream")},
            )
        self.assertEqual(at9_r.status_code, 200, at9_r.text)
        self.assertEqual(at9_r.json()["codec"], "atrac9")

        # A + K + N: Static theme with NO music and NO SFX -> valid PKG without bgmhome_loud or se_*.vag
        st = editor_state()
        st["slots"] = {"home": home["job_id"]}
        st["bgMode"] = "static"
        st["bgm"] = None
        st["sfx"] = {}
        st["campos"]["pkg-title"] = "Estatico Limpo"
        st["campos"]["pkg-title-id"] = "CUSA00321"
        st["campos"]["pkg-content-id"] = "UP9000-CUSA00321_00-ESTATICOLIMPO000"
        self.client.put(f"/api/projects/{proj_id}", json={"revision": 1, "name": "Matriz Audio e Estatico", "state": st})

        r = self.client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
        self.assertEqual(r.status_code, 200, r.text)
        pr = r.json()["pkg_report"]
        self.assertTrue(pr["structure_validated"], pr)
        self.assertIsNone(pr["bgmhome_loud"])
        paths = {f["path"] for f in pr["files"]}
        self.assertIn("Image0/texture/background/homescreen.png", paths)
        self.assertIn("Image0/texture/background/functionscreen.png", paths)
        self.assertNotIn("Image0/sound/bgm_home.at9", paths)
        self.assertFalse(any(p.endswith(".vag") for p in paths))
        self.assertFalse(any("background.raf" in p for p in paths))

    # =========================================================================
    # SCENARIOS G, H, I, J — Cancellation, Storage Fault, Duplicate & Session Isolation
    # =========================================================================
    def test_scenarios_g_h_i_j_cancellation_storage_fault_duplicate_and_cross_session(self):
        proj_id = self._new_project(self.client, "Isolamento e Falhas")
        params = {"project": proj_id}
        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh)}).json()
        st = editor_state()
        st["slots"] = {"home": home["job_id"]}
        st["bgMode"] = "static"
        st["campos"]["pkg-title"] = "Original OK"
        st["campos"]["pkg-title-id"] = "CUSA00444"
        st["campos"]["pkg-content-id"] = "UP9000-CUSA00444_00-ORIGINALOK000000"
        self.client.put(f"/api/projects/{proj_id}", json={"revision": 1, "name": "Isolamento e Falhas", "state": st})

        # Build a valid PKG first (baseline preserved state for H)
        ok_r = self.client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
        self.assertEqual(ok_r.status_code, 200, ok_r.text)
        baseline_pkg_id = ok_r.json()["pkg_id"]

        # G: Cancel an in-flight build cleanly (simulated slow builder checking cancellation)
        def _slow_cancellable(*args, check=None, **kwargs):
            for _ in range(40):
                if check:
                    check()
                time.sleep(0.05)
            raise AssertionError("Should have been cancelled")

        with mock.patch.object(pkg_router, "build_and_validate_theme_pkg", side_effect=_slow_cancellable):
            launch = self.client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true"})
            self.assertEqual(launch.status_code, 202)
            tid = launch.json()["task_id"]
            cancel = self.client.post(f"/api/forge/pkg/jobs/{tid}/cancel", params=params)
            self.assertEqual(cancel.status_code, 200)
            done = self._poll_pkg(self.client, tid, params)
            self.assertEqual(done["status"], "cancelled")

        # H: Storage write failure preserves prior valid PKG intact
        with mock.patch.object(pkg_router, "build_and_validate_theme_pkg", side_effect=OSError("No space left on device")):
            fail_r = self.client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
            self.assertEqual(fail_r.status_code, 422)
        hist_after_fail = self.client.get("/api/forge/pkg/history", params=params).json()["items"]
        self.assertEqual(len(hist_after_fail), 1)
        self.assertEqual(hist_after_fail[0]["pkg_id"], baseline_pkg_id)
        self.assertEqual(self.client.get(hist_after_fail[0]["download"], params=params).status_code, 200)

        # I: Duplicated project is isolated from original
        dup = self.client.post(f"/api/projects/{proj_id}/duplicate", json={"name": "Copia Isolada"})
        self.assertEqual(dup.status_code, 201, dup.text)
        dup_id = dup.json()["id"]
        wav = _make_wav(self.tmp / "dup_only.wav", duration=1.5)
        with wav.open("rb") as fh:
            bgm_dup = self.client.post(
                "/api/forge/audio/convert-apply",
                params={"project": dup_id},
                files={"file": ("dup_only.wav", fh, "audio/wav")},
            )
        self.assertEqual(bgm_dup.status_code, 200)
        orig_state = self.client.get(f"/api/projects/{proj_id}").json()["state"]
        self.assertIsNone(orig_state["bgm"])

        # J: Wrong / cross-session cannot access project or download PKG (HTTP 404)
        intruder = TestClient(main.app)
        self.assertEqual(intruder.get(f"/api/projects/{proj_id}").status_code, 404)
        self.assertEqual(intruder.get(f"/api/forge/pkg/{baseline_pkg_id}/download", params=params).status_code, 404)
        self.assertEqual(intruder.get("/api/forge/pkg/history", params=params).status_code, 404)

    # =========================================================================
    # SCENARIOS O, P, Q, R, S — Budget Limits, Invalid Metadata, Tampered/Truncated PKG
    # =========================================================================
    def test_scenarios_o_p_q_r_s_limits_metadata_iro_category_and_truncation(self):
        proj_id = self._new_project(self.client, "Limites e Adulteracao")
        params = {"project": proj_id}
        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh)}).json()

        # P: Invalid metadata (bad Title ID / Content ID) rejected clearly
        st = editor_state()
        st["slots"] = {"home": home["job_id"]}
        st["bgMode"] = "static"
        st["campos"]["pkg-title"] = "Meta Ruim"
        st["campos"]["pkg-title-id"] = "INVALID"
        st["campos"]["pkg-content-id"] = "NOT-A-CONTENT-ID"
        self.client.put(f"/api/projects/{proj_id}", json={"revision": 1, "name": "Limites e Adulteracao", "state": st})
        bad_meta = self.client.post("/api/forge/validate", params=params, data={"use_saved_project": "true"})
        self.assertIn(bad_meta.status_code, (400, 422))

        # O: RAF budget limit (exactly 48 frames at 640x360 <= 6 MiB passes; 49 frames fails)
        st["campos"]["pkg-title-id"] = "CUSA00555"
        st["campos"]["pkg-content-id"] = "UP9000-CUSA00555_00-RAF48LIMIT000000"
        with (HERE / "sample.mp4").open("rb") as fh:
            anim = self.client.post(
                "/api/animated/upload",
                params=params,
                data={"frames": "4", "size": "640x360"},
                files={"file": ("bg.mp4", fh)},
            ).json()
        owner_dir = next((self.tmp / "workspace").iterdir())
        dds_dir = owner_dir / proj_id / "media" / f"animated-{anim['job_id']}" / "deluxe_dds"
        dds_dir.mkdir(exist_ok=True)
        one_dds = dds.encode_frame(np.full((360, 640, 4), [80, 90, 140, 255], dtype=np.uint8), "native")
        for i in range(48):
            (dds_dir / f"{i:02d}.dds").write_bytes(one_dds)
        jj = dds_dir.parent / "job.json"
        raw = json.loads(jj.read_text(encoding="utf-8"))
        raw["meta"]["deluxe"] = {"frames": 48, "size": [640, 360], "wait": 0.1}
        jj.write_text(json.dumps(raw, indent=2), encoding="utf-8")

        st["bgMode"] = "animated"
        st["anim"] = {"jobId": anim["job_id"], "size": [640, 360], "frames": 48, "wait": 0.1, "backend": "native"}
        self.client.put(f"/api/projects/{proj_id}", json={"revision": 2, "name": "Limites e Adulteracao", "state": st})
        ok_48 = self.client.post("/api/forge/validate", params=params, data={"use_saved_project": "true"})
        self.assertEqual(ok_48.status_code, 200, ok_48.text)
        self.assertTrue(ok_48.json()["report"]["valid"], ok_48.json())

        # Push over 48 frames: API assembler clamps safely to 48, and direct bundle validator rejects 49 DDS frames!
        (dds_dir / "48.dds").write_bytes(one_dds)
        raw["meta"]["deluxe"]["frames"] = 49
        jj.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        st["anim"]["frames"] = 49
        self.client.put(f"/api/projects/{proj_id}", json={"revision": 3, "name": "Limites e Adulteracao", "state": st})
        clamped = self.client.post("/api/forge/validate", params=params, data={"use_saved_project": "true"})
        self.assertEqual(clamped.status_code, 200)
        dds_files = [f for f in clamped.json()["report"]["files"] if f["path"].endswith(".dds")]
        self.assertEqual(len(dds_files), 48)
        # Direct validator test on an actual 49-frame RAF directory on disk must FAIL
        over_dir = self.tmp / "over_48_bundle"
        raf_over = over_dir / "scene" / "background.raf"
        raf_over.mkdir(parents=True)
        for i in range(49):
            (raf_over / f"{i:02d}.dds").write_bytes(one_dds)
        rep_over = validate_theme_bundle(over_dir)
        self.assertFalse(rep_over.valid)
        self.assertTrue(any("48" in e or "quadros" in e.lower() or "6 MiB" in e for e in rep_over.errors), rep_over.errors)

        # Switch back to static to build a clean PKG and test Q, R, S
        st["bgMode"] = "static"
        self.client.put(f"/api/projects/{proj_id}", json={"revision": 4, "name": "Limites e Adulteracao", "state": st})
        pkg_res = self.client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
        self.assertEqual(pkg_res.status_code, 200, pkg_res.text)
        pkg_bytes = self.client.get(pkg_res.json()["download"], params=params).content

        # Q: Wrong IRO_TAG (0x98 zeroed) caught by independent inspector
        bad_iro = bytearray(pkg_bytes)
        bad_iro[0x98:0x9C] = b"\x00\x00\x00\x00"
        bad_iro_path = self.tmp / "wrong_iro.pkg"
        bad_iro_path.write_bytes(bytes(bad_iro))
        rep_q = inspect_and_validate_pkg(bad_iro_path, pkgtool_bin=pkg_forge.find_liborbispkg_tool())
        self.assertFalse(rep_q.structure_validated)
        self.assertEqual(rep_q.iro_tag_header, 0)

        # R: Wrong CATEGORY ("gd") rejected by BOTH PackageMeta.validate() AND bundle validator on raw SFO
        bad_meta = pkg.PackageMeta(
            title="Bad Cat",
            title_id="CUSA00555",
            content_id="UP9000-CUSA00555_00-RAF48LIMIT000000",
            category="gd",
            passcode="0" * 32,
        )
        with self.assertRaises(pkg.PackageError):
            build_param_sfo_bytes(bad_meta)

        sfo_dir = self.tmp / "bad_cat"
        (sfo_dir / "sce_sys").mkdir(parents=True)
        raw_bad_sfo = encode_param_sfo(
            [
                SfoEntry("ATTRIBUTE", 0x0404, 0, 4),
                SfoEntry("CATEGORY", 0x0204, "gd", 4),
                SfoEntry("CONTENT_ID", 0x0204, "UP9000-CUSA00555_00-RAF48LIMIT000000", 48),
                SfoEntry("FORMAT", 0x0204, "obs", 4),
                SfoEntry("IRO_TAG", 0x0404, 2, 4),
                SfoEntry("TITLE", 0x0204, "Bad Cat", 128),
                SfoEntry("TITLE_ID", 0x0204, "CUSA00555", 12),
                SfoEntry("VERSION", 0x0204, "01.00", 8),
            ]
        )
        (sfo_dir / "sce_sys" / "param.sfo").write_bytes(raw_bad_sfo)
        rep_r = validate_theme_bundle(sfo_dir)
        self.assertFalse(rep_r.valid)
        self.assertTrue(any("CATEGORY" in e for e in rep_r.errors))

        # S: Incomplete / truncated PKG rejected both directly and via /api/forge/pkg/inspect
        trunc = pkg_bytes[: len(pkg_bytes) // 2]
        insp = self.client.post(
            "/api/forge/pkg/inspect",
            params=params,
            files={"file": ("truncated.pkg", io.BytesIO(trunc), "application/octet-stream")},
        )
        self.assertEqual(insp.status_code, 200)
        self.assertFalse(insp.json()["structure_validated"])

    # =========================================================================
    # SCENARIOS X & Z — Touch Web UI Markup/JS + Canonical Core Sanity
    # =========================================================================
    def test_scenarios_x_and_z_touch_ui_and_canonical_sanity(self):
        # X: Served HTML contains viewport, all mandatory Forge buttons, status badge, audio button & report dialog
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn('name="viewport"', html)
        for label in (
            "CONVERTER E APLICAR MÚSICA",
            "VALIDAR MEU TEMA",
            "EXPORTAR RECURSOS",
            "GERAR PKG",
            "BAIXAR PKG",
            "VER RELATÓRIO",
            "forge-status-badge",
            "forge-summary-grid",
            "forge-report-dialog",
        ):
            self.assertIn(label, html, f"UI missing required element/label: {label}")

        # Verify all static JS modules pass Node syntax check
        static_dir = HERE.parent / "app" / "static"
        if shutil.which("node"):
            for js_file in sorted(static_dir.glob("*.js")):
                proc = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, f"JS syntax error in {js_file.name}: {proc.stderr}")

        # Z: Canonical endpoints still work cleanly
        sys_info = self.client.get("/api/system").json()
        self.assertTrue(sys_info["at9tool"])
        self.assertTrue(sys_info["pkg_tools"])
        self.assertIn("psgfx", sys_info["at9_encoder"]["backend"])
        self.assertIn("liborbispkg", sys_info["pkg_forge"]["backend"])
        self.assertEqual(self.client.get("/api/audio/slots").status_code, 200)
        self.assertEqual(self.client.get("/api/package/defaults").status_code, 200)
        self.assertEqual(self.client.get("/api/forge/status").status_code, 200)


if __name__ == "__main__":
    unittest.main()
