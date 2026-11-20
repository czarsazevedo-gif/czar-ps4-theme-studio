"""Checkpoint B: CZAR ATRAC9 Studio, native psgfx-at9 encoding, independent validation,
Audio Loop Finisher, Professional Volume Control, VAG SFX, and full server-restart persistence."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from app import at9, media, pkg, projects
from app.forge.audio_loop import analyze_audio_loop, evaluate_post_at9_loop, finish_audio_loop
from app.forge.audio_validation import (
    compare_roundtrip_audio,
    decode_to_float64_stereo,
    measure_loudness,
    parse_at9_riff,
    validate_at9,
    write_pcm16_wav,
)
from app.forge.audio_volume import apply_volume_mode
from app.main import app
from test_projects import state as editor_state

HERE = Path(__file__).resolve().parent


def _make_musical_wav(dest: Path, duration: float = 4.0, sr: int = 48000, amp: float = 0.35) -> Path:
    """Create a rich harmonic stereo WAV with integer cycles for clean spectral + loop testing."""
    t = np.arange(int(sr * duration), dtype=np.float64) / float(sr)
    left = amp * (0.65 * np.sin(2.0 * np.pi * 220.0 * t) + 0.35 * np.sin(2.0 * np.pi * 440.0 * t))
    right = amp * (0.65 * np.sin(2.0 * np.pi * 330.0 * t) + 0.35 * np.sin(2.0 * np.pi * 660.0 * t))
    pcm = np.stack([left, right], axis=1)
    return write_pcm16_wav(pcm, dest, sample_rate=sr)


def _make_clicky_wav(dest: Path, duration: float = 3.0, sr: int = 48000) -> Path:
    """Create a stereo WAV with leading silence and a strong amplitude step at the boundary."""
    n = int(sr * duration)
    t = np.arange(n, dtype=np.float64) / float(sr)
    sig = 0.35 * np.cos(2.0 * np.pi * 220.0 * t) + 0.20 * (t / duration)
    # Add 0.4s silence at start so pcm[0]==0 while pcm[-1]≈+0.55 (audible click on wrap)
    pad = int(sr * 0.4)
    left = np.concatenate([np.zeros(pad), sig])
    right = left.copy()
    return write_pcm16_wav(np.stack([left, right], axis=1), dest, sample_rate=sr)


class ForgeCheckpointBUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_roundtrip_wav_to_atrac9_to_wav_and_independent_validation(self):
        self.assertTrue(at9.available(), "psgfx-at9 must be built via scripts/install_forge_tools.sh at9")
        wav = _make_musical_wav(self.tmp / "music.wav", duration=4.0)
        out_at9 = self.tmp / "bgm_home.at9"
        at9.encode(wav, out_at9, bitrate=144, loop=True, validate=True)

        # Independent RIFF inspection
        riff = parse_at9_riff(out_at9.read_bytes())
        self.assertEqual(riff["sample_rate"], 48000)
        self.assertEqual(riff["channels"], 2)
        self.assertEqual(riff["byte_rate"], 18000)
        self.assertEqual(riff["block_align"], 384)
        self.assertEqual(riff["config_data_hex"], "fe740bf0")
        self.assertTrue(riff["has_fact"])
        self.assertTrue(riff["has_smpl"])
        self.assertEqual(riff["play_count"], 0)
        self.assertTrue(riff["superframe_aligned"])

        # Independent FFmpeg + RIFF validator
        rep = validate_at9(out_at9, require_ps4_profile=True, require_loop=True)
        self.assertTrue(rep.valid, rep.errors)
        self.assertEqual(rep.codec, "atrac9")
        self.assertFalse(rep.truncated)
        self.assertGreater(rep.decoded_samples, 48000 * 3)

        # Round-trip perceptual comparison WAV -> ATRAC9 -> decoded WAV
        orig_pcm = decode_to_float64_stereo(wav)
        rt = compare_roundtrip_audio(orig_pcm, out_at9)
        self.assertTrue(rt["perceptual_ok"], rt)
        self.assertLess(rt["duration_delta_sec"], 0.1)
        self.assertLess(abs(rt["energy_delta_db"]), 1.5)
        self.assertGreater(rt["spectral_cosine_similarity"], 0.95)
        self.assertEqual(rt["decoded_clipped_samples"], 0)

        # Post-ATRAC9 loop evaluation
        post_loop = evaluate_post_at9_loop(out_at9)
        self.assertTrue(post_loop["evaluated_after_atrac9"])
        self.assertTrue(post_loop["convincing"], post_loop)

    def test_refuses_silent_168kbps_substitution(self):
        wav = _make_musical_wav(self.tmp / "m.wav", duration=2.0)
        with self.assertRaises(media.MediaError):
            at9.encode(wav, self.tmp / "bad168.at9", bitrate=168)

    def test_rejects_fake_and_corrupted_at9(self):
        # Renamed WAV is NOT valid ATRAC9
        fake_at9 = self.tmp / "fake.at9"
        _make_musical_wav(fake_at9, duration=1.5)
        rep = validate_at9(fake_at9)
        self.assertFalse(rep.valid)
        self.assertTrue(rep.errors)

        # Corrupted/truncated ATRAC9
        good = self.tmp / "good.at9"
        at9.encode(fake_at9, good)
        corrupt = self.tmp / "corrupt.at9"
        corrupt.write_bytes(good.read_bytes()[: len(good.read_bytes()) // 2])
        rep2 = validate_at9(corrupt)
        self.assertFalse(rep2.valid)

    def test_audio_loop_finisher_and_volume_modes(self):
        wav = _make_clicky_wav(self.tmp / "clicky.wav", duration=3.0)
        pcm = decode_to_float64_stereo(wav)
        analysis = analyze_audio_loop(pcm)
        self.assertGreater(analysis["leading_silence_sec"], 0.3)
        self.assertGreater(analysis["click_risk"], 0.1)

        # Auto-refined with silence trim reduces silence & click risk
        refined, rep = finish_audio_loop(pcm, mode="auto_refined", trim_silence=True)
        self.assertLess(rep.leading_silence_sec, 0.05)
        self.assertLess(rep.click_risk_after, rep.click_risk_before)

        # Original volume mode does NOT silently normalize
        orig_pcm = decode_to_float64_stereo(_make_musical_wav(self.tmp / "clean.wav", duration=3.0, amp=0.4))
        out_orig, p_orig, b_orig, a_orig = apply_volume_mode(orig_pcm, mode="original")
        self.assertEqual(p_orig["applied_gain_db"], 0.0)
        self.assertEqual(b_orig.integrated_lufs, a_orig.integrated_lufs)

        # Ambient mode levels near -28 LUFS
        out_amb, p_amb, _b_amb, a_amb = apply_volume_mode(orig_pcm, mode="ambient")
        self.assertIsNotNone(a_amb.integrated_lufs)
        self.assertAlmostEqual(a_amb.integrated_lufs, -28.0, delta=1.2)
        self.assertEqual(a_amb.clipped_samples, 0)

        # Assisted mode levels to custom target (-32.0 LUFS PS4 reference)
        out_ast, p_ast, _b_ast, a_ast = apply_volume_mode(orig_pcm, mode="assisted", target_lufs=-32.0)
        self.assertAlmostEqual(a_ast.integrated_lufs, -32.0, delta=1.2)

        # Manual boost uses limiter to prevent harsh clipping
        out_man, p_man, _b_man, a_man = apply_volume_mode(orig_pcm, mode="manual", gain_db=12.0)
        self.assertTrue(p_man["limiter_applied"])
        self.assertLessEqual(float(np.max(np.abs(out_man))), 0.996)


class ForgeCheckpointBIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(projects, "ROOT", self.tmp / "workspace"))
        self.client = self.enterContext(TestClient(app))
        self.assertEqual(self.client.get("/api/projects").status_code, 200)

    def _create_project(self, name: str) -> str:
        r = self.client.post("/api/projects", json={"name": name})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def test_mandatory_wav_convert_apply_save_restart_export_bundle_flow(self):
        """Section 11 mandatory test:
        Create project -> Import WAV -> Convert ATRAC9 -> Apply -> Save -> Reload ->
        Restart server -> Reopen project -> Export bundle -> Verify real ATRAC9 bgm_home.at9 & theme.xml."""
        proj_id = self._create_project("Tema Musical PS4")
        params = {"project": proj_id}

        # Upload required home background
        with (HERE / "sample.png").open("rb") as fh:
            home = self.client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", fh, "image/png")})
        self.assertEqual(home.status_code, 200, home.text)
        home_job = home.json()["job_id"]

        # Create real stereo WAV and inspect it via CZAR ATRAC9 Studio
        wav_path = _make_musical_wav(self.tmp / "tema_bgm.wav", duration=4.0)
        with wav_path.open("rb") as fh:
            insp = self.client.post(
                "/api/forge/audio/inspect",
                params=params,
                files={"file": ("tema_bgm.wav", fh, "audio/wav")},
            )
        self.assertEqual(insp.status_code, 200, insp.text)
        insp_data = insp.json()
        self.assertEqual(insp_data["codec"], "pcm_s16le")
        self.assertIsNotNone(insp_data["loudness"]["integrated_lufs"])
        # Preview WAV must be streamable
        prev = self.client.get(insp_data["preview_url"], params=params)
        self.assertEqual(prev.status_code, 200)
        self.assertTrue(prev.content.startswith(b"RIFF"))

        # Preview before/after with ambient mode
        pp = self.client.post(
            "/api/forge/audio/preview-process",
            params=params,
            data={"staging_id": insp_data["staging_id"], "loop_mode": "auto_refined", "volume_mode": "ambient"},
        )
        self.assertEqual(pp.status_code, 200, pp.text)
        self.assertAlmostEqual(pp.json()["after_loudness"]["integrated_lufs"], -28.0, delta=1.2)

        # Click "CONVERTER E APLICAR MÚSICA"
        applied = self.client.post(
            "/api/forge/audio/convert-apply",
            params=params,
            data={"staging_id": insp_data["staging_id"], "loop_mode": "auto_refined", "volume_mode": "ambient"},
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        bgm_data = applied.json()
        self.assertEqual(bgm_data["codec"], "atrac9")
        self.assertEqual(bgm_data["rate"], 48000)
        self.assertEqual(bgm_data["channels"], 2)
        self.assertTrue(bgm_data["validation"]["valid"])
        self.assertEqual(bgm_data["validation"]["config_data_hex"], "fe740bf0")
        self.assertEqual(bgm_data["validation"]["play_count"], 0)
        self.assertTrue(bgm_data["post_at9_loop"]["evaluated_after_atrac9"])
        self.assertIsNotNone(bgm_data["loudness"])
        self.assertTrue(bgm_data["derived_id"])

        # Add VAG SFX too
        sfx_ids = {}
        for slot in ("se_decide", "se_cursor", "se_cancel"):
            with (HERE / "sample_sfx.wav").open("rb") as fh:
                sr = self.client.post(
                    f"/api/audio/{slot}/upload",
                    params=params,
                    files={"file": (f"{slot}.wav", fh, "audio/wav")},
                )
            self.assertEqual(sr.status_code, 200, sr.text)
            sfx_ids[slot] = sr.json()["job_id"]
            # Preview of VAG SFX works
            sp = self.client.get(sr.json()["preview"], params=params)
            self.assertEqual(sp.status_code, 200)
            self.assertTrue(sp.content.startswith(b"RIFF"))

        # Save project manifest
        st = editor_state()
        st["slots"]["home"] = home_job
        st["bgm"] = bgm_data["job_id"]
        st["bgmGain"] = bgm_data["gain_db"]
        st["sfx"] = sfx_ids
        saved = self.client.put(
            f"/api/projects/{proj_id}",
            json={"revision": 1, "name": "Tema Musical PS4", "state": st},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        cookies = self.client.cookies

        # Simulate full server restart with a fresh TestClient using persisted session cookie
        with TestClient(app, cookies=cookies) as restarted:
            reopened = restarted.get(f"/api/projects/{proj_id}")
            self.assertEqual(reopened.status_code, 200)
            re_state = reopened.json()["state"]
            self.assertEqual(re_state["bgm"], bgm_data["job_id"])
            self.assertEqual(re_state["sfx"], sfx_ids)

            info = restarted.get(f"/api/audio/{re_state['bgm']}/info", params=params)
            self.assertEqual(info.status_code, 200, info.text)
            self.assertEqual(info.json()["codec"], "atrac9")
            self.assertEqual(info.json()["derived_id"], bgm_data["derived_id"])
            self.assertAlmostEqual(info.json()["loudness"], bgm_data["loudness"], places=2)

            # Audio preview & seam preview work after restart
            self.assertEqual(restarted.get(info.json()["preview"], params=params).status_code, 200)
            self.assertEqual(restarted.get(info.json()["seam_preview"], params=params).status_code, 200)

            # Export theme bundle ZIP and inspect Image0 contents
            exp = restarted.post(
                "/api/package/build",
                params=params,
                data={
                    "title": "Tema Musical PS4",
                    "title_id": "CUSA00123",
                    "content_id": "UP9000-CUSA00123_00-TEMAMUSICAL00000",
                    "category": "ac",
                    "passcode": "0" * 32,
                    "mode": "theme",
                    "slots": json.dumps({"home": home_job}),
                    "audio_job": re_state["bgm"],
                    "sfx": json.dumps(sfx_ids),
                    "artifacts_only": "true",
                },
            )
            self.assertEqual(exp.status_code, 200, exp.text)
            dl = restarted.get(exp.json()["download"], params=params)
            self.assertEqual(dl.status_code, 200)

            with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
                names = set(zf.namelist())
                self.assertIn("sound/bgm_home.at9", names)
                for slot in ("se_decide", "se_cursor", "se_cancel"):
                    self.assertIn(f"sound/{slot}.vag", names)
                    self.assertEqual(zf.read(f"sound/{slot}.vag")[:4], b"VAGp")
                theme_xml_txt = zf.read("theme.xml").decode("utf-8")
                self.assertIn("<homebgm-enable>True</homebgm-enable>", theme_xml_txt)

                # Independently validate exported bgm_home.at9
                exported_at9 = self.tmp / "exported_bgm_home.at9"
                exported_at9.write_bytes(zf.read("sound/bgm_home.at9"))
                ev = validate_at9(exported_at9, require_ps4_profile=True, require_loop=True)
                self.assertTrue(ev.valid, ev.errors)
                self.assertEqual(ev.codec, "atrac9")
                self.assertEqual(ev.config_data_hex, "fe740bf0")

                # Verify param.sfx carries real bgmhome_loud
                sfx_xml = pkg.build_param_sfx(
                    pkg.PackageMeta(
                        title="Tema Musical PS4",
                        title_id="CUSA00123",
                        content_id="UP9000-CUSA00123_00-TEMAMUSICAL00000",
                        category="ac",
                        passcode="0" * 32,
                        mode="theme",
                        bgm_loudness=info.json()["loudness"],
                    )
                )
                self.assertIn(f"bgmhome_loud={info.json()['loudness']:.2f}", sfx_xml)

    def test_mp3_flac_and_valid_at9_imports_and_invalid_cleanup(self):
        proj_id = self._create_project("Formatos")
        params = {"project": proj_id}

        # 1. MP3 -> ATRAC9 via standard /api/audio/bgm/upload
        with (HERE / "sample_music.mp3").open("rb") as fh:
            r_mp3 = self.client.post("/api/audio/bgm/upload", params=params, files={"file": ("track.mp3", fh)})
        self.assertEqual(r_mp3.status_code, 200, r_mp3.text)
        self.assertEqual(r_mp3.json()["codec"], "atrac9")
        self.assertTrue(r_mp3.json()["validation"]["valid"])

        # 2. FLAC -> ATRAC9 via /api/forge/audio/convert-apply
        flac_path = self.tmp / "track.flac"
        subprocess.run(
            [media.FFMPEG, "-v", "error", "-y", "-i", str(HERE / "sample_music.mp3"), "-c:a", "flac", str(flac_path)],
            check=True,
            timeout=30,
        )
        with flac_path.open("rb") as fh:
            r_flac = self.client.post(
                "/api/forge/audio/convert-apply",
                params=params,
                files={"file": ("track.flac", fh, "audio/flac")},
            )
        self.assertEqual(r_flac.status_code, 200, r_flac.text)
        self.assertEqual(r_flac.json()["codec"], "atrac9")
        self.assertEqual(r_flac.json()["source_codec"], "flac")

        # 3. Valid .at9 -> passthrough preserved without re-encoding
        valid_at9 = self.tmp / "valid.at9"
        at9.encode(_make_musical_wav(self.tmp / "src_at9.wav", duration=2.0), valid_at9)
        with valid_at9.open("rb") as fh:
            r_at9 = self.client.post(
                "/api/forge/audio/convert-apply",
                params=params,
                files={"file": ("valid.at9", fh)},
            )
        self.assertEqual(r_at9.status_code, 200, r_at9.text)
        self.assertTrue(r_at9.json()["passthrough"])
        self.assertEqual(r_at9.json()["encoder_backend"], "passthrough-at9")

        # 4. Corrupted .at9 and invalid WAV rejected cleanly (422) without orphan audio jobs
        owner_dir = next((self.tmp / "workspace").iterdir())
        media_dir = owner_dir / proj_id / "media"
        before_jobs = set(p.name for p in media_dir.iterdir() if p.is_dir())

        r_bad_at9 = self.client.post(
            "/api/audio/bgm/upload",
            params=params,
            files={"file": ("corrupt.at9", b"RIFF\x10\x00\x00\x00WAVEfmt broken", "audio/wav")},
        )
        self.assertEqual(r_bad_at9.status_code, 422)

        r_bad_wav = self.client.post(
            "/api/forge/audio/convert-apply",
            params=params,
            files={"file": ("broken.wav", b"not a real wav file at all", "audio/wav")},
        )
        self.assertEqual(r_bad_wav.status_code, 422)

        after_jobs = set(p.name for p in media_dir.iterdir() if p.is_dir())
        self.assertEqual(before_jobs, after_jobs, "Failed uploads must not leave orphan job folders")

    def test_library_wav_reuse_across_projects_and_duplicate_isolation(self):
        wav = _make_musical_wav(self.tmp / "shared_bgm.wav", duration=3.0)
        with wav.open("rb") as fh:
            up = self.client.post("/api/library", files={"file": ("shared_bgm.wav", fh, "audio/wav")})
        self.assertEqual(up.status_code, 201, up.text)
        asset = up.json()

        p1 = self._create_project("P1")
        p2 = self._create_project("P2")
        r1 = self.client.post(f"/api/library/{asset['id']}/apply?project={p1}", json={"slot": "bgm"})
        r2 = self.client.post(f"/api/library/{asset['id']}/apply?project={p2}", json={"slot": "bgm"})
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r1.json()["codec"], "atrac9")
        self.assertEqual(r2.json()["codec"], "atrac9")

        # Changing volume in P2 must NOT affect P1's derived .at9
        g2 = self.client.post(f"/api/audio/{r2.json()['job_id']}/gain?project={p2}", data={"gain_db": "-6.0"})
        self.assertEqual(g2.status_code, 200, g2.text)
        info1 = self.client.get(f"/api/audio/{r1.json()['job_id']}/info?project={p1}").json()
        info2 = self.client.get(f"/api/audio/{r2.json()['job_id']}/info?project={p2}").json()
        self.assertEqual(info1["gain_db"], 0.0)
        self.assertEqual(info2["gain_db"], -6.0)
        self.assertNotEqual(info1["converted_sha256"], info2["converted_sha256"])


if __name__ == "__main__":
    unittest.main()
