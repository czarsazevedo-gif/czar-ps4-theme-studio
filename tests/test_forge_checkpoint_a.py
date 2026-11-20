"""Checkpoint A: audit baseline, contracts validation, and reproduction of real audio/404 defects."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import at9, pkg, projects
from app.forge.contracts import (
    EXTERNAL_TOOLS,
    PS4_ATRAC9_BITRATE_KBPS,
    PS4_ATRAC9_BLOCK_ALIGN,
    PS4_ATRAC9_BYTE_RATE,
    PS4_ATRAC9_CHANNELS,
    PS4_ATRAC9_CONFIG_DATA_HEX,
    PS4_ATRAC9_SAMPLE_RATE,
    PS4_BGM_REFERENCE_LUFS,
    PS4_PKG_CONTENT_TYPE_AC,
    PS4_PKG_DEFAULT_PASSCODE,
    PS4_THEME_CATEGORY,
    PS4_THEME_IRO_TAG,
    PS4_THEME_VOLUME_TYPE,
    At9ValidationReport,
    BundleValidationReport,
    LoopFinishReport,
    LoudnessReport,
    PkgValidationReport,
)
from app.main import app

HERE = Path(__file__).resolve().parent


class ForgeCheckpointAContractsTest(unittest.TestCase):
    def test_ps4_canonical_constants(self):
        self.assertEqual(PS4_ATRAC9_SAMPLE_RATE, 48000)
        self.assertEqual(PS4_ATRAC9_CHANNELS, 2)
        self.assertEqual(PS4_ATRAC9_BITRATE_KBPS, 144)
        self.assertEqual(PS4_ATRAC9_BYTE_RATE, 18000)
        self.assertEqual(PS4_ATRAC9_BLOCK_ALIGN, 384)
        self.assertEqual(PS4_ATRAC9_CONFIG_DATA_HEX, "fe740bf0")
        self.assertEqual(PS4_BGM_REFERENCE_LUFS, -32.0)
        self.assertEqual(PS4_THEME_VOLUME_TYPE, "pkg_ps4_theme")
        self.assertEqual(PS4_THEME_CATEGORY, "ac")
        self.assertEqual(PS4_THEME_IRO_TAG, 2)
        self.assertEqual(PS4_PKG_CONTENT_TYPE_AC, 0x1B)
        self.assertEqual(PS4_PKG_DEFAULT_PASSCODE, "0" * 32)
        self.assertIn("psgfx", EXTERNAL_TOOLS)
        self.assertEqual(EXTERNAL_TOOLS["psgfx"]["license"], "GPL-3.0-or-later")

    def test_contract_models_roundtrip(self):
        loud = LoudnessReport(integrated_lufs=-32.0, bgmhome_loud="-32.00")
        self.assertEqual(loud.reference_lufs, -32.0)
        loop = LoopFinishReport(
            start_sample=0,
            end_sample=48000,
            duration_seconds=1.0,
            boundary_discontinuity_before=0.1,
            boundary_discontinuity_after=0.01,
            spectral_similarity=0.95,
            phase_correlation=0.9,
            leading_silence_sec=0.0,
            trailing_silence_sec=0.0,
            click_risk_before=0.1,
            click_risk_after=0.01,
            crossfade_ms=0.0,
        )
        self.assertTrue(loop.convincing)
        at9_rep = At9ValidationReport(
            valid=True,
            codec="atrac9",
            sample_rate=48000,
            channels=2,
            bitrate_bps=144000,
            byte_rate=18000,
            block_align=384,
            config_data_hex="fe740bf0",
        )
        self.assertTrue(at9_rep.valid)
        bundle = BundleValidationReport(valid=True, theme_kind="static", has_music=False, has_sfx=False)
        self.assertEqual(bundle.theme_kind, "static")
        pkg_rep = PkgValidationReport(pkg_created=False, structure_validated=False, theme_type_valid=False)
        self.assertEqual(pkg_rep.physical_compatibility, "pending")


class ForgeCheckpointAAudioAuditReproductionTest(unittest.TestCase):
    """Documents and reproduces the exact baseline audio/404 failure modes audited in Checkpoint A."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(projects, "ROOT", self.tmp))
        self.client = self.enterContext(TestClient(app))

    def test_reproduces_audio_catalog_404_with_stale_or_unauthenticated_project_query(self):
        """Opening ?project=<id> without a matching session cookie causes /api/audio/slots to fail with 404."""
        stale_project = "a" * 32
        resp = self.client.get(f"/api/audio/slots?project={stale_project}")
        # Baseline defect before Checkpoint B fix: stateless catalog route returns 404 instead of 200
        self.assertEqual(resp.status_code, 404)

    def test_reproduces_wav_bgm_rejection_without_linux_encoder(self):
        """Without a native Linux ATRAC9 encoder, uploading a real WAV to /api/audio/bgm/upload fails with 415."""
        self.assertFalse(at9.available())
        wav = HERE / "sample_sfx.wav"
        self.assertTrue(wav.is_file(), "Run tests/make_fixtures.py first")
        with wav.open("rb") as fh:
            resp = self.client.post("/api/audio/bgm/upload", files={"file": ("music.wav", fh, "audio/wav")})
        self.assertEqual(resp.status_code, 415)

    def test_reproduces_linux_loudness_unavailable(self):
        """On Linux without orbis-pub-cmd.exe, pkg.calc_loudness returns None."""
        self.assertFalse(pkg.tools_available())
        self.assertIsNone(pkg.calc_loudness(HERE / "sample_sfx.wav"))

    def test_reproduces_missing_audio_preview_route_404(self):
        """Before Checkpoint B, /api/audio/{job_id}/preview does not exist (returns 404)."""
        self.client.get("/api/projects")
        proj_id = self.client.post("/api/projects", json={"name": "Audit"}).json()["id"]
        with (HERE / "sample_sfx.wav").open("rb") as fh:
            up = self.client.post(
                f"/api/audio/se_decide/upload?project={proj_id}",
                files={"file": ("click.wav", fh, "audio/wav")},
            )
        self.assertEqual(up.status_code, 200)
        job_id = up.json()["job_id"]
        prev = self.client.get(f"/api/audio/{job_id}/preview?project={proj_id}")
        self.assertEqual(prev.status_code, 404)


if __name__ == "__main__":
    unittest.main()
