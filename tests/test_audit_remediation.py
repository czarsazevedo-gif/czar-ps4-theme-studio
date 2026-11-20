"""Comprehensive regression suite proving remediation of ALL 18 audit findings (CZAR-AUD-001 .. CZAR-AUD-018)."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import warnings
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from app import at9, dxt1, jobs, main, media, pkg, projects
from app.ouroboros_engine import calibrator, media_io as io_media
from app.ouroboros_engine.analyzer import probe_source_extended
from app.ouroboros_engine.metrics import evaluate_loop
from app.forge import pkg_forge, pkg_router
from app.forge.pkg_inspector import inspect_and_validate_pkg
from app.forge.subproc import kill_process_tree, run_cancellable_process
from tests.test_projects import state as editor_state

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def _make_wav_24bit_mono_44100(path: Path, duration: float = 2.0) -> tuple[Path, int, str]:
    """Generate a genuine 24-bit PCM, mono, 44.1 kHz WAV file (non-48kHz stereo)."""
    sr = 44100
    n = int(round(sr * duration))
    t = np.arange(n, dtype=np.float64) / sr
    sig = 0.28 * np.sin(2.0 * math.pi * 261.63 * t)
    ints = np.clip(np.round(sig * ((1 << 23) - 1)), -(1 << 23), (1 << 23) - 1).astype(np.int32)
    raw = bytearray()
    for val in ints:
        b = int(val).to_bytes(4, "little", signed=True)[:3]
        raw.extend(b)
    data_bytes = bytes(raw)
    riff_size = 36 + len(data_bytes)
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        riff_size,
        b"WAVE",
        b"fmt ",
        16,
        1,          # PCM
        1,          # mono
        sr,
        sr * 3,     # byte rate
        3,          # block align
        24,         # bits per sample
        b"data",
        len(data_bytes),
    )
    blob = hdr + data_bytes
    path.write_bytes(blob)
    return path, len(blob), hashlib.sha256(blob).hexdigest()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class AuditRemediationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="czar-aud-fix-")))
        self.enterContext(mock.patch.object(projects, "ROOT", self.tmp / "workspace"))
        self.enterContext(mock.patch.object(jobs, "WORKSPACE", self.tmp / "recovery"))
        self.client = self.enterContext(TestClient(main.app))
        self.assertEqual(self.client.get("/api/projects").status_code, 200)

    def _new_project(self, client: TestClient, name: str) -> str:
        r = client.post("/api/projects", json={"name": name})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def _poll_ouroboros(self, ticket_id: str, params: dict, timeout: float = 180.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = self.client.get(f"/api/ouroboros/jobs/{ticket_id}", params=params)
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            if body["state"] != "working":
                return body
            time.sleep(0.15)
        self.fail(f"Ouroboros job {ticket_id} timed out")

    # =========================================================================
    # CZAR-AUD-001 (P0) & CZAR-AUD-002 (P1) — Selection B cannot publish A;
    # 360p -> 540p -> 360p preview restores exact DDS pixels + duplicate parity
    # =========================================================================
    def test_aud_001_and_002_take_identity_enforcement_and_exact_preview_restoration(self):
        proj_id = self._new_project(self.client, "Take A vs B")
        params = {"project": proj_id}

        # Upload real video into project library
        with (HERE / "sample.mp4").open("rb") as fh:
            lib = self.client.post("/api/library", files={"file": ("sample.mp4", fh, "video/mp4")})
        self.assertEqual(lib.status_code, 201, lib.text)
        lib_id = lib.json()["id"]

        # Run unified OUROBOROS -> 3 independent proposals (720p, 540p, 360p)
        run_r = self.client.post("/api/ouroboros/run", params=params, json={"kind": "library", "id": lib_id})
        self.assertEqual(run_r.status_code, 202, run_r.text)
        done = self._poll_ouroboros(run_r.json()["id"], params)
        self.assertEqual(done["state"], "ready", done)
        key = done["key"]
        report = self.client.get(f"/api/ouroboros/result/{key}", params=params).json()
        props = {p["profileId"]: p for p in report["proposals"]}
        prop_a = props["flexibility"]  # 640x360
        prop_b = props["balance"]      # 960x540
        self.assertNotEqual(prop_a["renderKey"], prop_b["renderKey"])

        # CZAR-AUD-001: Attempt to apply flexibility while expecting balance renderKey -> MUST reject 409!
        stale_apply = self.client.post(
            "/api/ouroboros/apply",
            params=params,
            json={"key": key, "profileId": "flexibility", "expectedRenderKey": prop_b["renderKey"]},
        )
        self.assertEqual(stale_apply.status_code, 409, stale_apply.text)
        self.assertIsNone(self.client.get(f"/api/projects/{proj_id}").json()["state"])

        # Apply Proposal A (flexibility 640x360) honestly and test 360 -> 540 -> 360 preview restoration
        ok_a = self.client.post(
            "/api/ouroboros/apply",
            params=params,
            json={"key": key, "profileId": "flexibility", "expectedRenderKey": prop_a["renderKey"]},
        )
        self.assertEqual(ok_a.status_code, 200, ok_a.text)
        job_a = ok_a.json()["jobId"]
        n_a = int(prop_a["frames"])
        w_a = float(prop_a["wait"])

        exp_a = self.client.post(
            f"/api/animated/{job_a}/export",
            params=params,
            data={"frames": str(n_a), "wait_seconds": str(w_a), "backend": "native"},
        )
        self.assertEqual(exp_a.status_code, 200, exp_a.text)
        zip_a_bytes = self.client.get(f"/api/animated/{job_a}/download", params=params).content
        with zipfile.ZipFile(io.BytesIO(zip_a_bytes)) as zf:
            dds_rgb_a = [dxt1.decode_dds(zf.read(f"scene/background.raf/{i:02d}.dds"))[..., :3] for i in range(n_a)]
            a_hashes = {n: hashlib.sha256(zf.read(n)).hexdigest() for n in zf.namelist()}

        # CZAR-AUD-002: Reframe 360p -> 540p -> 360p and verify ALL previews match 360p DDS pixels!
        r540 = self.client.post(
            f"/api/animated/{job_a}/frames",
            params=params,
            data={"frames": str(min(n_a, 24)), "size": "960x540"},
        )
        self.assertEqual(r540.status_code, 200, r540.text)
        with Image.open(io.BytesIO(self.client.get(r540.json()["frames"][0], params=params).content)) as im540:
            self.assertEqual(im540.size, (960, 540))

        r360 = self.client.post(
            f"/api/animated/{job_a}/frames",
            params=params,
            data={"frames": str(n_a), "size": "640x360"},
        )
        self.assertEqual(r360.status_code, 200, r360.text)
        for i, prev_url in enumerate(r360.json()["frames"]):
            with Image.open(io.BytesIO(self.client.get(prev_url, params=params).content)) as im:
                self.assertEqual(im.size, (640, 360))
                arr = np.asarray(im.convert("RGB"))
                self.assertTrue(np.array_equal(arr, dds_rgb_a[i]), f"Frame {i} preview diverged from DDS after 360->540->360")

        # Duplicate project and verify 360 -> 540 -> 360 pixel fidelity on duplicate too
        dup = self.client.post(f"/api/projects/{proj_id}/duplicate", json={"name": "Copia 002"})
        self.assertEqual(dup.status_code, 201, dup.text)
        dup_id = dup.json()["id"]
        dup_params = {"project": dup_id}
        self.assertEqual(
            self.client.post(f"/api/animated/{job_a}/frames", params=dup_params, data={"frames": str(min(n_a, 24)), "size": "960x540"}).status_code,
            200,
        )
        dup360 = self.client.post(f"/api/animated/{job_a}/frames", params=dup_params, data={"frames": str(n_a), "size": "640x360"})
        self.assertEqual(dup360.status_code, 200)
        for i, prev_url in enumerate(dup360.json()["frames"]):
            with Image.open(io.BytesIO(self.client.get(prev_url, params=dup_params).content)) as im:
                self.assertEqual(im.size, (640, 360))
                self.assertTrue(np.array_equal(np.asarray(im.convert("RGB")), dds_rgb_a[i]))

        # Now apply Proposal B (balance 960x540) -> exported ZIP must match B byte-for-byte, NOT A!
        ok_b = self.client.post(
            "/api/ouroboros/apply",
            params=params,
            json={"key": key, "profileId": "balance", "expectedRenderKey": prop_b["renderKey"]},
        )
        self.assertEqual(ok_b.status_code, 200, ok_b.text)
        job_b = ok_b.json()["jobId"]
        n_b = int(prop_b["frames"])
        w_b = float(prop_b["wait"])
        exp_b = self.client.post(
            f"/api/animated/{job_b}/export",
            params=params,
            data={"frames": str(n_b), "wait_seconds": str(w_b), "backend": "native"},
        )
        self.assertEqual(exp_b.status_code, 200, exp_b.text)
        zip_b_bytes = self.client.get(f"/api/animated/{job_b}/download", params=params).content
        with zipfile.ZipFile(io.BytesIO(zip_b_bytes)) as zf:
            b_dds_names = sorted(n for n in zf.namelist() if n.endswith(".dds"))
            self.assertEqual(len(b_dds_names), n_b)
            for i in range(n_b):
                fn = f"{i:02d}.dds"
                actual_sha = hashlib.sha256(zf.read(f"scene/background.raf/{fn}")).hexdigest()
                self.assertEqual(actual_sha, prop_b["ddsHashes"][fn])
            b_hashes = {n: hashlib.sha256(zf.read(n)).hexdigest() for n in zf.namelist()}
            self.assertNotEqual(b_hashes, a_hashes)

    # =========================================================================
    # CZAR-AUD-003 (P1) — Inspecting & converting WAV preserves original bytes & SHA-256
    # =========================================================================
    def test_aud_003_wav_24bit_44100_mono_provenance_preserved_across_inspect_preview_and_apply(self):
        proj_id = self._new_project(self.client, "WAV Provenance")
        params = {"project": proj_id}
        wav_path, orig_bytes, orig_sha = _make_wav_24bit_mono_44100(self.tmp / "mono24_44k.wav", duration=2.0)

        with wav_path.open("rb") as fh:
            ins = self.client.post(
                "/api/forge/audio/inspect",
                params=params,
                files={"file": ("mono24_44k.wav", fh, "audio/wav")},
            )
        self.assertEqual(ins.status_code, 200, ins.text)
        body = ins.json()
        self.assertEqual(body["bytes"], orig_bytes)
        self.assertEqual(body["original_bytes"], orig_bytes)
        self.assertEqual(body["sha256"], orig_sha)
        self.assertEqual(body["original_sha256"], orig_sha)
        self.assertNotEqual(body["preview_sha256"], orig_sha)
        self.assertEqual(body["sample_rate"], 44100)
        self.assertEqual(body["channels"], 1)

        # Playable browser preview still works cleanly
        pv = self.client.get(body["preview_url"], params=params)
        self.assertEqual(pv.status_code, 200)
        self.assertEqual(pv.content[:4], b"RIFF")

        # Preview-process preserves original_sha256
        pp = self.client.post(
            "/api/forge/audio/preview-process",
            params=params,
            data={"staging_id": body["staging_id"], "volume_mode": "original", "loop_mode": "whole"},
        )
        self.assertEqual(pp.status_code, 200, pp.text)
        self.assertEqual(pp.json()["original_sha256"], orig_sha)

        # Convert-apply from staging_id persists exact original_sha256 and exact original bytes on disk!
        conv = self.client.post(
            "/api/forge/audio/convert-apply",
            params=params,
            data={"staging_id": body["staging_id"], "volume_mode": "original", "loop_mode": "whole"},
        )
        self.assertEqual(conv.status_code, 200, conv.text)
        cjson = conv.json()
        self.assertEqual(cjson["original_sha256"], orig_sha)
        self.assertEqual(cjson["codec"], "atrac9")

        # Check on-disk job source is still byte-identical to uploaded 24-bit mono 44.1 kHz WAV
        owner_dir = next((self.tmp / "workspace").iterdir())
        job_src = owner_dir / proj_id / "media" / f"audio-{cjson['job_id']}" / "source.wav"
        self.assertEqual(job_src.stat().st_size, orig_bytes)
        self.assertEqual(hashlib.sha256(job_src.read_bytes()).hexdigest(), orig_sha)

    # =========================================================================
    # CZAR-AUD-004 (P1) — Partial PKG validation never reports full validation
    # =========================================================================
    def test_aud_004_pkg_verification_levels_distinguish_partial_uncompared_and_full(self):
        proj_id = self._new_project(self.client, "PKG Levels")
        params = {"project": proj_id}
        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh)}).json()
        st = editor_state()
        st["slots"] = {"home": home["job_id"]}
        st["bgMode"] = "static"
        st["campos"]["pkg-title"] = "Levels Test"
        st["campos"]["pkg-title-id"] = "CUSA00777"
        st["campos"]["pkg-content-id"] = "UP9000-CUSA00777_00-LEVELSTEST000000"
        self.client.put(f"/api/projects/{proj_id}", json={"revision": 1, "name": "PKG Levels", "state": st})

        r = self.client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
        self.assertEqual(r.status_code, 200, r.text)
        pr = r.json()["pkg_report"]
        # Regime 3: Full bundle verified
        self.assertEqual(pr["verification_level"], "full_bundle_verified")
        self.assertTrue(pr["structure_validated"])
        self.assertTrue(pr["image0_extracted"])
        self.assertTrue(pr["bundle_compared"])
        self.assertTrue(pr["bundle_hashes_matched"])
        self.assertTrue(pr["extracted_match"])

        pkg_file = self.tmp / "sample_built.pkg"
        pkg_file.write_bytes(self.client.get(r.json()["download"], params=params).content)

        # Regime 1: Standalone inspection WITHOUT extractor -> header_and_sc0_only, structure_validated=False!
        with mock.patch.object(pkg, "tools_available", return_value=False):
            rep_no_ex = inspect_and_validate_pkg(pkg_file, pkgtool_bin=None)
        self.assertEqual(rep_no_ex.verification_level, "header_and_sc0_only")
        self.assertTrue(rep_no_ex.header_and_crypto_valid)
        self.assertTrue(rep_no_ex.sc0_validated)
        self.assertFalse(rep_no_ex.image0_extracted)
        self.assertFalse(rep_no_ex.bundle_compared)
        self.assertFalse(rep_no_ex.extracted_match)
        self.assertFalse(rep_no_ex.structure_validated)

        # Regime 2: Standalone inspection WITH extractor but NO bundle -> extracted_uncompared, extracted_match=False!
        rep_uncomp = inspect_and_validate_pkg(pkg_file, pkgtool_bin=pkg_forge.find_liborbispkg_tool())
        self.assertEqual(rep_uncomp.verification_level, "extracted_uncompared")
        self.assertTrue(rep_uncomp.structure_validated)
        self.assertTrue(rep_uncomp.image0_extracted)
        self.assertFalse(rep_uncomp.bundle_compared)
        self.assertFalse(rep_uncomp.bundle_hashes_matched)
        self.assertFalse(rep_uncomp.extracted_match)

        # Regime 4: Tampered reference bundle -> failed
        bad_bundle = self.tmp / "wrong_bundle"
        bad_bundle.mkdir()
        (bad_bundle / "theme.xml").write_text("<theme/>", encoding="utf-8")
        rep_mismatch = inspect_and_validate_pkg(
            pkg_file, expected_content_dir=bad_bundle, pkgtool_bin=pkg_forge.find_liborbispkg_tool()
        )
        self.assertEqual(rep_mismatch.verification_level, "failed")
        self.assertFalse(rep_mismatch.structure_validated)
        self.assertFalse(rep_mismatch.bundle_hashes_matched)

    # =========================================================================
    # CZAR-AUD-005 (P1) — Standalone Forge mode (no ?project=) isolates sessions
    # =========================================================================
    def test_aud_005_standalone_forge_sessions_are_isolated_from_each_other_and_global_recovery(self):
        client_a = self.enterContext(TestClient(main.app))
        client_b = self.enterContext(TestClient(main.app))

        wav_path, _sz, orig_sha = _make_wav_24bit_mono_44100(self.tmp / "standalone.wav", duration=1.5)
        with wav_path.open("rb") as fh:
            ins_a = client_a.post(
                "/api/forge/audio/inspect",
                files={"file": ("standalone.wav", fh, "audio/wav")},
            )
        self.assertEqual(ins_a.status_code, 200, ins_a.text)
        # Session A got its own HttpOnly cookie and can access its own preview
        self.assertIn(projects.COOKIE, client_a.cookies)
        prev_url = ins_a.json()["preview_url"]
        self.assertEqual(client_a.get(prev_url).status_code, 200)
        self.assertEqual(ins_a.json()["original_sha256"], orig_sha)

        # Session B (different standalone session) MUST NOT see Session A's staging preview or stage!
        self.assertEqual(client_b.get(prev_url).status_code, 404)
        hijack = client_b.post(
            "/api/forge/audio/preview-process",
            data={"staging_id": ins_a.json()["staging_id"]},
        )
        self.assertEqual(hijack.status_code, 404)

        # Nothing leaked into shared global jobs.WORKSPACE!
        self.assertFalse((self.tmp / "recovery" / "forge-audio-stage").exists())
        self.assertFalse((self.tmp / "recovery" / "pkg-forge-v1").exists())

    # =========================================================================
    # CZAR-AUD-006 (P1) & CZAR-AUD-008 (P2) — Non-blocking RAF export + exact dds_bytes & budget
    # =========================================================================
    def test_aud_006_and_008_exact_raf_dds_accounting_and_non_blocking_export(self):
        proj_id = self._new_project(self.client, "RAF Budget")
        params = {"project": proj_id}

        for size_str, sz_tuple, frames_n in (("640x360", [640, 360], 6), ("960x540", [960, 540], 4), ("1280x720", [1280, 720], 4)):
            with (HERE / "sample.mp4").open("rb") as fh:
                up = self.client.post(
                    "/api/animated/upload",
                    params=params,
                    data={"frames": str(frames_n), "size": size_str},
                    files={"file": ("sample.mp4", fh)},
                )
            self.assertEqual(up.status_code, 200, up.text)
            jid = up.json()["job_id"]
            exp = self.client.post(
                f"/api/animated/{jid}/export",
                params=params,
                data={"frames": str(frames_n), "wait_seconds": "0.10", "backend": "native"},
            )
            self.assertEqual(exp.status_code, 200, exp.text)
            body = exp.json()
            zbytes = self.client.get(body["download"], params=params).content
            with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
                dds_sum = sum(zf.getinfo(n).file_size for n in zf.namelist() if n.endswith(".dds"))
                mdx_sz = zf.getinfo("scene/background.raf/plane.mdx").file_size
            self.assertEqual(body["dds_bytes"], dds_sum)
            self.assertEqual(body["mdx_bytes"], mdx_sz)
            self.assertEqual(body["size"], sz_tuple)
            self.assertEqual(body["budget"]["size"], sz_tuple)
            self.assertEqual(body["budget"]["total_bytes"], dds_sum)

        # CZAR-AUD-006: Live Uvicorn concurrency test — /healthz stays responsive during a heavy RAF export!
        port = _free_port()
        env = dict(os.environ, PYTHONPATH=str(REPO), CZAR_WORKSPACE=str(self.tmp / "uvicorn_006"))
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(REPO),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(50):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                        if r.status == 200:
                            break
                except Exception:
                    time.sleep(0.1)
            else:
                self.fail("Uvicorn failed to start")

            import httpx
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0, trust_env=False) as hc:
                with (HERE / "sample.mp4").open("rb") as fh:
                    up = hc.post("/api/animated/upload", data={"frames": "12", "size": "1280x720"}, files={"file": ("sample.mp4", fh)})
                self.assertEqual(up.status_code, 200)
                jid = up.json()["job_id"]

                export_done = threading.Event()
                export_res = {}

                def _do_export():
                    r = hc.post(f"/api/animated/{jid}/export", data={"frames": "12", "wait_seconds": "0.1", "backend": "native"})
                    export_res["status"] = r.status_code
                    export_done.set()

                t = threading.Thread(target=_do_export)
                t.start()
                time.sleep(0.12)
                ping_latencies = []
                while not export_done.is_set() and len(ping_latencies) < 4:
                    t0 = time.perf_counter()
                    hr = hc.get("/healthz")
                    dt = time.perf_counter() - t0
                    self.assertEqual(hr.status_code, 200)
                    ping_latencies.append(dt)
                    time.sleep(0.05)
                t.join(timeout=30.0)
                self.assertEqual(export_res.get("status"), 200)
                self.assertTrue(ping_latencies, "Expected at least one concurrent /healthz ping during export")
                self.assertLess(max(ping_latencies), 0.75, f"Event loop blocked! Latencies={ping_latencies}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # =========================================================================
    # CZAR-AUD-007 (P1) & CZAR-AUD-018 (P2) — /healthz & offline start.sh
    # =========================================================================
    def test_aud_007_and_018_healthz_works_offline_and_start_sh_is_decoupled(self):
        with mock.patch.object(at9, "available", return_value=False), mock.patch.object(
            pkg_forge, "pkg_forge_info", return_value={"available": False, "backend": None}
        ):
            r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["editor"])
        self.assertFalse(body["atrac9_encoder"])
        self.assertFalse(body["pkg_forge"])

        # start.sh must not run install_forge_tools.sh unless CZAR_AUTO_INSTALL_FORGE=1
        start_txt = (REPO / "start.sh").read_text(encoding="utf-8")
        self.assertIn('CZAR_AUTO_INSTALL_FORGE:-0', start_txt)
        self.assertIn("/healthz", start_txt)

    # =========================================================================
    # CZAR-AUD-009 (P2) — Late VFR transition beyond frame 600 detected; CFR stays False
    # =========================================================================
    def test_aud_009_late_vfr_760_frames_detected_and_cfr_control_stays_false(self):
        cfr_24 = self.tmp / "seg30.mp4"
        seg_10 = self.tmp / "seg10.mp4"
        late_vfr = self.tmp / "late_vfr.mp4"
        cfr_full = self.tmp / "cfr_full.mp4"
        ff = media.FFMPEG

        # 24s @ 30fps (720 frames) + 4s @ 10fps (40 frames) -> 760 frames total, late cadence shift!
        subprocess.run(
            [ff, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x36:r=30:d=24",
             "-c:v", "libx264", "-preset", "ultrafast", "-video_track_timescale", "30000", str(cfr_24)],
            check=True,
            timeout=30,
        )
        subprocess.run(
            [ff, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x36:r=10:d=4",
             "-c:v", "libx264", "-preset", "ultrafast", "-video_track_timescale", "30000", str(seg_10)],
            check=True,
            timeout=30,
        )
        lst = self.tmp / "concat.txt"
        lst.write_text(f"file '{cfr_24}'\nfile '{seg_10}'\n", encoding="utf-8")
        subprocess.run(
            [ff, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(late_vfr)],
            check=True,
            timeout=30,
        )
        subprocess.run(
            [ff, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=green:s=64x36:r=30:d=27",
             "-c:v", "libx264", "-preset", "ultrafast", str(cfr_full)],
            check=True,
            timeout=30,
        )

        vfr_info = probe_source_extended(late_vfr, 28.0)
        self.assertTrue(vfr_info["isVFR"], vfr_info)
        self.assertGreaterEqual(vfr_info["windowsSampled"], 2)
        self.assertGreaterEqual(vfr_info["coverageRatio"], 0.85)

        cfr_info = probe_source_extended(cfr_full, 27.0)
        self.assertFalse(cfr_info["isVFR"], cfr_info)
        self.assertGreaterEqual(cfr_info["windowsSampled"], 2)

    # =========================================================================
    # CZAR-AUD-010 (P2) — Calibrator caches shared extractions (20 -> <= 10)
    # =========================================================================
    def test_aud_010_calibrator_reuses_shared_proxy_extractions_with_identical_results(self):
        real_extract = io_media.extract
        calls: list[tuple[int, tuple[int, int]]] = []

        def _counting_extract(source, start, end, frames, size, folder, check):
            calls.append((frames, tuple(size)))
            return real_extract(source, start, end, frames, size, folder, check)

        work = self.tmp / "cal_work"
        work.mkdir()
        with mock.patch.object(io_media, "extract", side_effect=_counting_extract):
            res = calibrator.calibrate_interval(HERE / "sample.mp4", 0.0, 1.2, source_fps=30.0, folder=work, verify_dds=False)

        unique_keys = set(calls)
        self.assertEqual(len(calls), len(unique_keys), f"Duplicate extractions occurred: {calls}")
        self.assertLess(len(calls), res["evaluatedCount"])

        # Verify numeric parity: re-evaluating recommended configs from fresh direct extractions yields identical scores
        for rec_name, rec in res["recommendations"].items():
            w, h = rec["size"]
            render_sz = (min(w, 320), min(h, 180))
            fresh = np.array(real_extract(HERE / "sample.mp4", 0.0, 1.2, rec["frames"], render_sz, work, lambda: None))
            ev = evaluate_loop(fresh, rec["wait"])
            self.assertEqual(ev["metrics"]["visualSimilarity"]["seamSsim"], rec["seamSsim"], f"{rec_name} seamSsim diverged")
            self.assertEqual(ev["metrics"]["visualSimilarity"]["seamRatio"], rec["seamRatio"], f"{rec_name} seamRatio diverged")
            self.assertEqual(ev["metrics"]["velocityContinuity"]["relativeJump"], rec["velocityJump"], f"{rec_name} velocityJump diverged")

    # =========================================================================
    # CZAR-AUD-011 (P1) & CZAR-AUD-012 (P2) — 256 KiB log drain without deadlock
    # & cancellation propagated into pkg_extract with zero surviving child processes
    # =========================================================================
    def test_aud_011_and_012_verbose_subprocess_no_deadlock_and_extract_cancellation(self):
        # 256 KiB of stdout (> 4x Linux pipe buffer 64 KiB) must finish fast with no ResourceWarning
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            t0 = time.perf_counter()
            rc, out = run_cancellable_process(
                [sys.executable, "-c", "import sys; sys.stdout.write('X' * 262144); sys.stdout.flush()"],
                timeout=3.0,
            )
            dt = time.perf_counter() - t0
        self.assertEqual(rc, 0)
        self.assertLess(dt, 2.5)
        self.assertGreater(len(out), 1000)
        res_warns = [w for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(res_warns, [], f"ResourceWarning leaked: {res_warns}")

        # CZAR-AUD-012: Cancel during pkg_extract kills BOTH wrapper shell AND child `sleep 15` immediately!
        fake_tool = self.tmp / "slow_pkgtool.sh"
        pid_file = self.tmp / "child.pids"
        fake_tool.write_text(
            f"#!/usr/bin/env bash\n"
            f"if [[ \"$1\" == \"pkg_validate\" ]]; then exit 0; fi\n"
            f"sleep 15 &\n"
            f"child=$!\n"
            f"printf '%s %s\\n' \"$$\" \"$child\" > '{pid_file}'\n"
            f"wait \"$child\"\n",
            encoding="utf-8",
        )
        fake_tool.chmod(0o755)

        # Build a tiny real PKG first so header parsing succeeds and reaches pkg_extract
        proj_id = self._new_project(self.client, "Cancel Extract")
        params = {"project": proj_id}
        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh)}).json()
        st = editor_state()
        st["slots"] = {"home": home["job_id"]}
        st["bgMode"] = "static"
        st["campos"]["pkg-title"] = "Cancel Test"
        st["campos"]["pkg-title-id"] = "CUSA00888"
        st["campos"]["pkg-content-id"] = "UP9000-CUSA00888_00-CANCELTEST000000"
        self.client.put(f"/api/projects/{proj_id}", json={"revision": 1, "name": "Cancel Extract", "state": st})
        built = self.client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
        self.assertEqual(built.status_code, 200, built.text)
        pkg_file = self.tmp / "cancel_target.pkg"
        pkg_file.write_bytes(self.client.get(built.json()["download"], params=params).content)

        stop_evt = threading.Event()
        active_holder: dict = {"proc": None}

        def _check():
            if stop_evt.is_set():
                raise pkg_forge.PkgForgeCancelled("Cancelado durante extração")

        def _reg(p):
            active_holder["proc"] = p

        def _trigger_cancel():
            time.sleep(0.35)
            stop_evt.set()
            kill_process_tree(active_holder.get("proc"))

        t_cancel = threading.Thread(target=_trigger_cancel)
        t_cancel.start()
        t0 = time.perf_counter()
        with self.assertRaises(pkg_forge.PkgForgeCancelled):
            inspect_and_validate_pkg(
                pkg_file,
                pkgtool_bin=fake_tool,
                check=_check,
                register_proc=_reg,
            )
        dt = time.perf_counter() - t0
        t_cancel.join(timeout=2.0)
        self.assertLess(dt, 2.5)
        self.assertTrue(pid_file.is_file(), "Fake extractor never reached sleep stage")
        pids = [int(tok) for tok in pid_file.read_text(encoding="utf-8").split()]
        self.assertEqual(len(pids), 2)
        time.sleep(0.1)
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            # On Linux containers without an active subreaper, a SIGKILLed grandchild can remain briefly as state 'Z'
            stat_path = Path(f"/proc/{pid}/stat")
            if stat_path.exists():
                state = stat_path.read_text(encoding="utf-8").rsplit(")", 1)[-1].split()[0]
                self.assertEqual(state, "Z", f"Surviving live child PID {pid} in state {state}")

    # =========================================================================
    # CZAR-AUD-013 (P1) & CZAR-AUD-017 (P2) — Active job recovery on reload +
    # real successive Uvicorn OS process restart verification
    # =========================================================================
    def test_aud_013_and_017_active_job_recovery_and_real_uvicorn_process_restart(self):
        proj_id = self._new_project(self.client, "Reload Recovery")
        params = {"project": proj_id}
        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh)}).json()
        st = editor_state()
        st["slots"] = {"home": home["job_id"]}
        st["bgMode"] = "static"
        st["campos"]["pkg-title"] = "Reload PKG"
        st["campos"]["pkg-title-id"] = "CUSA00666"
        st["campos"]["pkg-content-id"] = "UP9000-CUSA00666_00-RELOADPKG0000000"
        self.client.put(f"/api/projects/{proj_id}", json={"revision": 1, "name": "Reload Recovery", "state": st})

        # Simulate reload while build is actively in-flight: GET /api/forge/pkg/active MUST expose the running task!
        started_evt = threading.Event()
        release_evt = threading.Event()
        real_builder = pkg_router.build_and_validate_theme_pkg

        def _gated_builder(*args, **kwargs):
            started_evt.set()
            release_evt.wait(timeout=10.0)
            return real_builder(*args, **kwargs)

        with mock.patch.object(pkg_router, "build_and_validate_theme_pkg", side_effect=_gated_builder):
            launch = self.client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true"})
            self.assertEqual(launch.status_code, 202, launch.text)
            tid = launch.json()["task_id"]
            self.assertTrue(started_evt.wait(timeout=5.0))

            # Reloaded client (same session cookie) queries /api/forge/pkg/active -> recovers live task!
            act = self.client.get("/api/forge/pkg/active", params=params)
            self.assertEqual(act.status_code, 200, act.text)
            self.assertIsNotNone(act.json()["active"])
            self.assertEqual(act.json()["active"]["task_id"], tid)
            self.assertIn(act.json()["active"]["status"], ("processing", "validating"))

            release_evt.set()
            deadline = time.monotonic() + 60.0
            final = {}
            while time.monotonic() < deadline:
                final = self.client.get(f"/api/forge/pkg/jobs/{tid}", params=params).json()
                if final["status"] in ("completed", "failed", "cancelled"):
                    break
                time.sleep(0.15)
            self.assertEqual(final["status"], "completed", final)

        # Now run a REAL two-process Uvicorn restart test (Process 1 -> terminate -> Process 2)!
        import httpx

        shared_ws = self.tmp / "two_process_ws"
        env = dict(os.environ, PYTHONPATH=str(REPO), CZAR_WORKSPACE=str(shared_ws))

        def _boot_uvicorn(port: int) -> subprocess.Popen:
            p = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=str(REPO),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(60):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                        if r.status == 200:
                            return p
                except Exception:
                    time.sleep(0.1)
            p.kill()
            self.fail(f"Uvicorn failed to boot on port {port}")

        port1 = _free_port()
        p1 = _boot_uvicorn(port1)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port1}", timeout=60.0, trust_env=False) as c1:
                self.assertEqual(c1.get("/api/projects").status_code, 200)
                cookie_val = c1.cookies.get(projects.COOKIE)
                self.assertTrue(cookie_val)
                p_res = c1.post("/api/projects", json={"name": "Two Process PKG"})
                self.assertEqual(p_res.status_code, 201)
                pid2 = p_res.json()["id"]
                prms2 = {"project": pid2}
                with (HERE / "sample.png").open("rb") as fh:
                    h2 = c1.post("/api/slot/home/upload", params=prms2, files={"file": ("home.png", fh)}).json()
                st2 = editor_state()
                st2["slots"] = {"home": h2["job_id"]}
                st2["bgMode"] = "static"
                st2["campos"]["pkg-title"] = "Restart Verify"
                st2["campos"]["pkg-title-id"] = "CUSA00555"
                st2["campos"]["pkg-content-id"] = "UP9000-CUSA00555_00-RESTARTVERIFY000"
                self.assertEqual(
                    c1.put(f"/api/projects/{pid2}", json={"revision": 1, "name": "Two Process PKG", "state": st2}).status_code,
                    200,
                )
                b2 = c1.post("/api/forge/pkg/build", params=prms2, data={"use_saved_project": "true", "sync": "true"})
                self.assertEqual(b2.status_code, 200, b2.text)
                pkg_id2 = b2.json()["pkg_id"]
                task_id2 = b2.json()["task_id"]
                dl1 = c1.get(b2.json()["download"], params=prms2)
                self.assertEqual(dl1.status_code, 200)
                sha1 = hashlib.sha256(dl1.content).hexdigest()
        finally:
            p1.terminate()
            try:
                p1.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p1.kill()

        # Boot brand-new OS process Uvicorn #2 on a new port sharing only the on-disk workspace!
        port2 = _free_port()
        p2 = _boot_uvicorn(port2)
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port2}",
                cookies={projects.COOKIE: cookie_val},
                timeout=30.0,
                trust_env=False,
            ) as c2:
                self.assertEqual(c2.get("/healthz").status_code, 200)
                job_after = c2.get(f"/api/forge/pkg/jobs/{task_id2}", params=prms2)
                self.assertEqual(job_after.status_code, 200, job_after.text)
                self.assertEqual(job_after.json()["status"], "completed")

                act_after = c2.get("/api/forge/pkg/active", params=prms2).json()
                self.assertIsNone(act_after["active"])
                self.assertEqual(act_after["latest"]["pkg_id"], pkg_id2)

                hist2 = c2.get("/api/forge/pkg/history", params=prms2).json()["items"]
                self.assertEqual(len(hist2), 1)
                self.assertEqual(hist2[0]["pkg_id"], pkg_id2)
                self.assertEqual(hist2[0]["iro_tag_header"], 2)
                self.assertEqual(hist2[0]["category"], "ac")
                self.assertEqual(hist2[0]["verification_level"], "full_bundle_verified")

                dl2 = c2.get(hist2[0]["download"], params=prms2)
                self.assertEqual(dl2.status_code, 200)
                self.assertEqual(hashlib.sha256(dl2.content).hexdigest(), sha1)
        finally:
            p2.terminate()
            try:
                p2.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p2.kill()

    # =========================================================================
    # CZAR-AUD-014, 015, 016 — Honest readiness card, 44px touch targets &
    # honest post-DDS finish message verified in live headless Chromium!
    # =========================================================================
    def test_aud_014_015_016_ui_readiness_touch_targets_and_honest_finish_wording(self):
        # Verify ouroboros.js never prints misleading 'nenhum ajuste extra necessário'
        ouro_js = (REPO / "app" / "static" / "ouroboros.js").read_text(encoding="utf-8")
        self.assertNotIn("nenhum ajuste extra necessário", ouro_js)
        self.assertIn("REPROVADO/CONDICIONAL pós-DDS", ouro_js)
        self.assertIn("Esta configuração exige novo render antes de aplicar", ouro_js)

        # Verify style.css enforces 44px touch minimums on frequent controls
        css = (REPO / "app" / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("CZAR-AUD-015", css)
        self.assertIn("min-height: 44px;", css)

        # Verify live Chromium render at BOTH tablet landscape (1480x924) and portrait (924x1480)!
        if shutil.which("chromium"):
            port = _free_port()
            env = dict(os.environ, PYTHONPATH=str(REPO), CZAR_WORKSPACE=str(self.tmp / "uvicorn_ui"))
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=str(REPO),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(50):
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                            if r.status == 200:
                                break
                    except Exception:
                        time.sleep(0.1)
                for win_size in ("1480,924", "924,1480"):
                    dom = subprocess.check_output(
                        [
                            "chromium",
                            "--headless=new",
                            "--no-sandbox",
                            "--disable-gpu",
                            f"--window-size={win_size}",
                            "--virtual-time-budget=3000",
                            "--dump-dom",
                            f"http://127.0.0.1:{port}/",
                        ],
                        stderr=subprocess.DEVNULL,
                        timeout=30,
                    ).decode("utf-8", "replace")
                    self.assertIn('id="forge-sum-pkg">Adicione a imagem inicial<', dom)
                    self.assertNotIn('id="forge-sum-pkg">Pronto para gerar<', dom)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    unittest.main()
