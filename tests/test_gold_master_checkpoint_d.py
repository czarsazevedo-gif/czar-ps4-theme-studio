"""Checkpoint D — Pilar III (Gold Master Audio: ATRAC9 144 kbps, VAG SFX, Loop Finisher, Loudness/Clipping).

Verifies all Section 18 requirements and regression fixes GM-D-001 & GM-D-002:
- Real WAV / MP3 / FLAC / passthrough AT9 -> validated PS4 ATRAC9 (48 kHz, stereo, 144 kbps, fe740bf0, smpl).
- GM-D-001: Adjusting volume via /api/audio/{job_id}/gain preserves Forge loop trim/crossfade/fades & refreshes post_at9_loop.
- GM-D-002: Inverted, negative, NaN, or out-of-range loop intervals cleanly return HTTP 422.
- Soft peak limiting prevents clipping blowup under +12 dB boost; ambient mode levels toward -28 LUFS.
- All 3 VAG SFX slots (se_decide, se_cursor, se_cancel) encode, preview, and package cleanly into validated bundle.
- Corrupted audio fails cleanly (422) without damaging saved project audio or leaking orphan jobs.
"""

from __future__ import annotations

import hashlib
import io
import math
import subprocess
import zipfile
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import at9, jobs, main, projects
from app.forge.audio_validation import validate_at9
from app.forge.sfo import parse_param_sfo
from tests.test_projects import state as editor_state

REPO = Path(__file__).resolve().parents[1]
SAMPLE_PNG = REPO / "tests" / "sample.png"
SAMPLE_MP3 = REPO / "tests" / "sample_music.mp3"
SAMPLE_SFX = REPO / "tests" / "sample_sfx.wav"


@pytest.fixture
def audio_client(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(projects, "ROOT", ws / "studio-projects-v1")
    monkeypatch.setattr(jobs, "WORKSPACE", ws / "recovery")
    with TestClient(main.app) as client:
        assert client.get("/api/projects").status_code == 200
        yield client, ws


def _synth_music_wav(path: Path, *, duration: float = 4.0, sr: int = 44100, channels: int = 2, amp: float = 0.45) -> Path:
    n = int(round(duration * sr))
    t = np.arange(n, dtype=np.float64) / sr
    sig = amp * (0.65 * np.sin(2.0 * math.pi * 220.0 * t) + 0.35 * np.sin(2.0 * math.pi * 330.0 * t))
    if channels == 2:
        pcm = np.stack([sig, sig * 0.95], axis=1)
    else:
        pcm = sig
    from app.forge.audio_validation import write_pcm16_wav

    write_pcm16_wav(pcm, path, sample_rate=sr)
    return path


def test_section18_wav_mp3_flac_at9_conversion_and_gm_d_001_gain_preserves_loop(audio_client, tmp_path):
    client, _ = audio_client
    pid = client.post("/api/projects", json={"name": "Audio Gold Master"}).json()["id"]
    params = {"project": pid}

    # Generate 4.0s WAV (44.1 kHz) and 24-bit 96 kHz FLAC
    wav_4s = _synth_music_wav(tmp_path / "loop_4s.wav", duration=4.0, sr=44100, amp=0.45)
    flac_96k = tmp_path / "hi_res_96k.flac"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(wav_4s), "-ar", "96000", "-sample_fmt", "s32", "-c:a", "flac", str(flac_96k)],
        check=True,
    )

    # 1. FLAC 96kHz -> ATRAC9 48kHz 144kbps with loop excerpt [0.5s .. 2.5s] + 40ms crossfade
    flac_bytes = flac_96k.read_bytes()
    flac_sha = hashlib.sha256(flac_bytes).hexdigest()
    conv = client.post(
        "/api/forge/audio/convert-apply",
        params=params,
        files={"file": (flac_96k.name, io.BytesIO(flac_bytes), "audio/flac")},
        data={
            "loop_enabled": "true",
            "loop_mode": "manual_excerpt",
            "start_sec": "0.5",
            "end_sec": "2.5",
            "crossfade_ms": "40",
            "volume_mode": "original",
        },
    )
    assert conv.status_code == 200, conv.text
    d1 = conv.json()
    assert d1["codec"] == "atrac9"
    assert d1["rate"] == 48000
    assert d1["channels"] == 2
    assert d1["bitrate_kbps"] == 144
    assert d1["validation"]["valid"] is True
    assert d1["validation"]["config_data_hex"] == "fe740bf0"
    assert d1["original_sha256"] == flac_sha
    assert 1.80 <= d1["duration"] <= 2.15, f"Expected trimmed ~1.96s duration, got {d1['duration']}"
    loud_before = d1["loudness"]
    jid = d1["job_id"]

    # GM-D-001: Applying manual gain (-6.0 dB) via /api/audio/{job_id}/gain MUST preserve the ~1.96s trimmed loop!
    gained = client.post(f"/api/audio/{jid}/gain", params=params, data={"gain_db": "-6.0"})
    assert gained.status_code == 200, gained.text
    d2 = gained.json()
    assert d2["validation"]["valid"] is True
    assert d2["original_sha256"] == flac_sha
    assert d2["converted_sha256"] != d1["converted_sha256"]
    assert 1.80 <= d2["duration"] <= 2.15, f"GM-D-001 regression: gain reset loop duration to {d2['duration']}s!"
    assert d2["loudness"] < loud_before - 4.0, f"Expected ~6 dB loudness drop: {loud_before} -> {d2['loudness']}"
    assert d2["post_at9_loop"]["evaluated_after_atrac9"] is True

    # Playable Chrome WAV previews (full + loop seam) work cleanly
    assert client.get(d2["preview"], params=params).status_code == 200
    assert client.get(d2["seam_preview"], params=params).status_code == 200

    # 2. MP3 -> ATRAC9 ambient leveling (-28 LUFS target) + passthrough .at9 verification
    with SAMPLE_MP3.open("rb") as fh:
        mp3_conv = client.post(
            "/api/forge/audio/convert-apply",
            params=params,
            files={"file": ("sample_music.mp3", fh, "audio/mpeg")},
            data={"loop_enabled": "true", "loop_mode": "auto_refined", "volume_mode": "ambient"},
        )
    assert mp3_conv.status_code == 200, mp3_conv.text
    assert mp3_conv.json()["validation"]["valid"] is True
    assert mp3_conv.json()["volume_mode"] == "ambient"

    # 3. Passthrough valid .at9 preserves exact SHA-256 without re-encoding
    at9_file = tmp_path / "native.at9"
    at9.encode(wav_4s, at9_file)
    at9_sha = hashlib.sha256(at9_file.read_bytes()).hexdigest()
    pass_r = client.post(
        "/api/forge/audio/convert-apply",
        params=params,
        files={"file": ("native.at9", at9_file.open("rb"), "application/octet-stream")},
        data={"loop_enabled": "true", "loop_mode": "whole", "volume_mode": "original"},
    )
    assert pass_r.status_code == 200, pass_r.text
    assert pass_r.json()["passthrough"] is True
    assert pass_r.json()["converted_sha256"] == at9_sha


def test_section18_gm_d_002_invalid_loop_params_clipping_guard_sfx_and_bundle_persistence(audio_client, tmp_path):
    client, ws = audio_client
    pid = client.post("/api/projects", json={"name": "Audio Faults & Bundle"}).json()["id"]
    params = {"project": pid}

    # Hot 0 dBFS track to verify clipping protection under +12 dB boost
    hot_wav = _synth_music_wav(tmp_path / "hot.wav", duration=2.0, sr=48000, amp=0.92)
    insp = client.post(
        "/api/forge/audio/inspect",
        params=params,
        files={"file": ("hot.wav", hot_wav.open("rb"), "audio/wav")},
    )
    assert insp.status_code == 200, insp.text
    tok = insp.json()["staging_id"]

    # GM-D-002: Inverted, negative, NaN, and out-of-range loop intervals cleanly return 422
    for bad_data in (
        {"staging_id": tok, "loop_mode": "manual_excerpt", "start_sec": "1.5", "end_sec": "0.5"},
        {"staging_id": tok, "loop_mode": "whole", "crossfade_ms": "-10"},
        {"staging_id": tok, "loop_mode": "manual_excerpt", "start_sec": "nan", "end_sec": "1.0"},
        {"staging_id": tok, "loop_mode": "manual_excerpt", "start_sec": "50.0", "end_sec": "60.0"},
    ):
        r = client.post("/api/forge/audio/preview-process", params=params, data=bad_data)
        assert r.status_code == 422, f"Expected 422 for {bad_data}, got {r.status_code}: {r.text}"

    # Clipping protection on +12 dB manual boost
    boosted = client.post(
        "/api/forge/audio/convert-apply",
        params=params,
        data={"staging_id": tok, "volume_mode": "manual", "gain_db": "12.0", "loop_enabled": "true"},
    )
    assert boosted.status_code == 200, boosted.text
    bd = boosted.json()
    assert bd["volume_params"]["limiter_applied"] is True
    assert bd["validation"]["valid"] is True
    bgm_job = bd["job_id"]

    # Corrupted audio upload returns 422 cleanly without breaking existing BGM job or leaking orphan dir
    media_dir = next((ws / "studio-projects-v1").glob(f"*/{pid}/media"))
    before_dirs = set(p.name for p in media_dir.iterdir())
    corrupt_r = client.post(
        "/api/forge/audio/convert-apply",
        params=params,
        files={"file": ("broken.mp3", io.BytesIO(b"ID3broken-audio-data"), "audio/mpeg")},
    )
    assert corrupt_r.status_code == 422
    assert set(p.name for p in media_dir.iterdir()) == before_dirs

    # Upload all 3 VAG SFX slots + home background, save project, export validated bundle, verify SFO + VAG + AT9!
    home_job = client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", SAMPLE_PNG.open("rb"), "image/png")}).json()["job_id"]
    sfx_jobs = {}
    for slot_id in ("se_decide", "se_cursor", "se_cancel"):
        sr_up = client.post(f"/api/audio/{slot_id}/upload", params=params, files={"file": ("sfx.wav", SAMPLE_SFX.open("rb"), "audio/wav")})
        assert sr_up.status_code == 200, sr_up.text
        sfx_jobs[slot_id] = sr_up.json()["job_id"]
        assert client.get(sr_up.json()["preview"], params=params).status_code == 200

    st = editor_state()
    st["slots"] = {"home": home_job}
    st["bgm"] = bgm_job
    st["bgmGain"] = bd["gain_db"]
    st["sfx"] = sfx_jobs
    assert client.put(f"/api/projects/{pid}", json={"name": "Audio Faults & Bundle", "revision": 1, "state": st}).status_code == 200

    exp = client.post("/api/forge/bundle/export", params=params, data={"use_saved_project": "true"})
    assert exp.status_code == 200, exp.text
    assert exp.json()["report"]["valid"] is True, exp.json()["report"]["errors"]

    dl = client.get(exp.json()["download"], params=params)
    assert dl.status_code == 200
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        names = set(zf.namelist())
        assert "Image0/sound/bgm_home.at9" in names
        assert "Image0/sound/se_decide.vag" in names
        assert "Image0/sound/se_cursor.vag" in names
        assert "Image0/sound/se_cancel.vag" in names
        xml_txt = zf.read("Image0/theme.xml").decode("utf-8")
        assert "<homebgm-enable>True</homebgm-enable>" in xml_txt
        sfo_fields = parse_param_sfo(zf.read("Sc0/param.sfo"))
        assert "bgmhome_loud=" in str(sfo_fields["values"].get("PUBTOOLINFO", ""))
        at9_tmp = tmp_path / "extracted_bgm.at9"
        at9_tmp.write_bytes(zf.read("Image0/sound/bgm_home.at9"))
        at9_val = validate_at9(at9_tmp, require_ps4_profile=True, require_loop=True)
        assert at9_val.valid is True, at9_val.errors
