"""Checkpoint 7 — ATRAC9 internal-loop + vgmstream-cli verification, strict Sc0/param.sfo PKG validation,
zero-halo SVG icon import (128/152/512 + XXE/XSS guard), and zero-video benchmark exit code enforcement."""

from __future__ import annotations

import hashlib
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import at9, jobs, main, media, pkg, projects
from app.forge import pkg_forge, sfo
from app.forge.audio_loop import evaluate_post_at9_loop
from app.forge.audio_validation import find_vgmstream_cli, patch_at9_smpl_loop, validate_at9
from app.forge.pkg_inspector import compare_param_sfo_fields, inspect_and_validate_pkg

REPO = Path(__file__).resolve().parent.parent


def test_at9_internal_loop_evaluated_correctly_in_both_directions(tmp_path: Path) -> None:
    """Section 11: evaluate_post_at9_loop MUST evaluate the repeated [loop_start, loop_end] interval, NOT outer file ends."""
    assert find_vgmstream_cli() is not None, "tools/bin/vgmstream-cli must be installed"
    sr = 48000
    n_total = sr * 3  # 3.0s
    t = np.arange(n_total, dtype=np.float64) / float(sr)

    # CASE A: Clean phase-locked internal loop [1.0s..2.0s] surrounded by terrible outer ends (silence + DC step)
    pcm_a = np.zeros((n_total, 2), dtype=np.float64)
    pcm_a[: sr // 2, :] = 0.85  # huge DC step at file start
    # [0.5s..1.0s] silence, [2.0s..3.0s] silence
    t_loop = np.arange(sr, dtype=np.float64) / float(sr)
    sine_440 = 0.35 * np.sin(2.0 * math.pi * 440.0 * t_loop)  # 440 exact cycles in 1.0s -> seamless!
    pcm_a[sr : 2 * sr, 0] = sine_440
    pcm_a[sr : 2 * sr, 1] = sine_440

    at9_a = tmp_path / "clean_internal_dirty_ends.at9"
    at9.encode_pcm(pcm_a, at9_a, bitrate=144, loop=True, validate=False)
    patch_at9_smpl_loop(at9_a, sr, 2 * sr)

    val_a = validate_at9(at9_a, require_ps4_profile=True, require_loop=True)
    assert val_a.valid is True, val_a.errors
    assert "vgmstream" in val_a.validator

    ev_a = evaluate_post_at9_loop(at9_a, sr=sr)
    assert ev_a["vgmstream_verified"] is True
    assert ev_a["vgmstream_multi_cycle_verified"] is True
    assert ev_a["internal_loop_evaluated"] is True
    assert abs(ev_a["pcm_loop_start"] - sr) <= 2
    assert abs(ev_a["pcm_loop_end"] - 2 * sr) <= 2
    assert ev_a["convincing"] is True, ev_a
    assert ev_a["click_risk"] <= 0.14, ev_a
    assert ev_a["leading_silence_sec"] < 0.05 and ev_a["trailing_silence_sec"] < 0.05, ev_a

    # CASE B: Dirty discontinuous internal loop [1.0s..2.0s] inside silent/clean outer ends
    pcm_b = np.zeros((n_total, 2), dtype=np.float64)
    # Ramp smoothly from +0.80 at 1.0s down to -0.80 at 2.0s -> huge 1.60 seam jump right at loop_end -> loop_start!
    ramp = np.linspace(0.80, -0.80, sr, dtype=np.float64)
    pcm_b[sr : 2 * sr, 0] = ramp
    pcm_b[sr : 2 * sr, 1] = ramp

    at9_b = tmp_path / "dirty_internal_clean_ends.at9"
    at9.encode_pcm(pcm_b, at9_b, bitrate=144, loop=True, validate=False)
    patch_at9_smpl_loop(at9_b, sr, 2 * sr)

    ev_b = evaluate_post_at9_loop(at9_b, sr=sr)
    assert ev_b["internal_loop_evaluated"] is True
    assert ev_b["vgmstream_verified"] is True
    assert ev_b["click_risk"] > 0.25, f"Expected bad internal loop click_risk > 0.25, got {ev_b}"
    assert ev_b["convincing"] is False, ev_b


def test_valid_ps4_at9_passthrough_not_reencoded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Section 11: valid PS4 48kHz 144kbps .at9 must pass through untouched without unnecessary re-encoding."""
    monkeypatch.setattr(projects, "ROOT", tmp_path / "projects")
    monkeypatch.setattr(jobs, "WORKSPACE", tmp_path / "standalone")

    sr = 48000
    t = np.arange(sr * 2, dtype=np.float64) / float(sr)
    pcm = np.stack([0.25 * np.sin(2.0 * math.pi * 220.0 * t)] * 2, axis=1)
    src_at9 = tmp_path / "ready_ps4.at9"
    at9.encode_pcm(pcm, src_at9, bitrate=144, loop=True, validate=True)
    orig_sha = hashlib.sha256(src_at9.read_bytes()).hexdigest()

    client = TestClient(main.app)
    assert client.get("/api/projects").status_code == 200
    pid = client.post("/api/projects", json={"name": "AT9 Passthrough"}).json()["id"]
    with src_at9.open("rb") as fh:
        res = client.post(
            f"/api/forge/audio/convert-apply?project={pid}",
            files={"file": ("ready_ps4.at9", fh, "audio/wav")},
            data={"loop_enabled": "true", "loop_mode": "whole", "volume_mode": "original", "gain_db": "0"},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["encoder_backend"] == "passthrough-at9"
    assert body["converted_sha256"] == orig_sha
    assert body["validation"]["valid"] is True
    assert "vgmstream" in body["validation"]["validator"]


def test_strict_param_sfo_pkg_comparison_catches_unauthorized_mutations(tmp_path: Path) -> None:
    """Section 12: Sc0/param.sfo MUST match expected bundle except documented packager keys."""
    cdir = tmp_path / "content"
    (cdir / "sce_sys").mkdir(parents=True)
    (cdir / "texture" / "background").mkdir(parents=True)
    (cdir / "sound").mkdir(parents=True)

    Image.new("RGB", (512, 512), (40, 80, 160)).save(cdir / "sce_sys" / "icon0.png")
    Image.new("RGB", (1920, 1080), (20, 40, 90)).save(cdir / "sce_sys" / "pic1.png")
    Image.new("RGB", (740, 416), (20, 40, 90)).save(cdir / "texture" / "preview.png")
    Image.new("RGB", (1920, 1080), (20, 40, 90)).save(cdir / "texture" / "background" / "homescreen.png")
    Image.new("RGB", (1920, 1080), (20, 40, 90)).save(cdir / "texture" / "background" / "functionscreen.png")

    sr = 48000
    t = np.arange(sr * 2, dtype=np.float64) / float(sr)
    pcm = np.stack([0.2 * np.sin(2.0 * math.pi * 220.0 * t)] * 2, axis=1)
    at9.encode_pcm(pcm, cdir / "sound" / "bgm_home.at9", bitrate=144, loop=True, validate=False)

    from app import theme_xml

    (cdir / "theme.xml").write_text(
        theme_xml.build_theme_xml(label="Strict SFO Theme", homebgm_enable=True),
        encoding="utf-8",
    )

    meta = pkg.PackageMeta(
        title="Strict SFO Theme",
        title_id="CUSA54321",
        content_id="UP0001-CUSA54321_00-0000000000000001",
        category="ac",
        passcode="0" * 32,
        mode="theme",
        bgm_loudness=-24.0,
    )
    orig_sfo_bytes = sfo.build_param_sfo_bytes(meta)
    (cdir / "sce_sys" / "param.sfo").write_bytes(orig_sfo_bytes)

    files = sorted(p.relative_to(cdir).as_posix() for p in cdir.rglob("*") if p.is_file())
    gp4 = tmp_path / "theme.gp4"
    gp4.write_text(pkg.build_gp4(meta, files, "content"), encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    pkg_path, brep, prep, _ = pkg_forge.build_and_validate_theme_pkg(cdir, gp4, meta, out_dir)
    assert brep.valid, brep.errors
    assert prep.verification_level == "full_bundle_verified", prep.errors
    assert prep.bundle_hashes_matched is True

    # 1. Tamper TITLE in expected bundle SFO -> MUST reject full_bundle_verified!
    bad_meta_title = pkg.PackageMeta(
        title="DIFFERENT TITLE",
        title_id="CUSA54321",
        content_id="UP0001-CUSA54321_00-0000000000000001",
        category="ac",
        passcode="0" * 32,
        mode="theme",
        bgm_loudness=-24.0,
    )
    (cdir / "sce_sys" / "param.sfo").write_bytes(sfo.build_param_sfo_bytes(bad_meta_title))
    rep_title = inspect_and_validate_pkg(
        pkg_path, expected_content_dir=cdir, pkgtool_bin=pkg_forge.find_liborbispkg_tool()
    )
    assert rep_title.verification_level == "failed"
    assert rep_title.bundle_hashes_matched is False
    assert any("TITLE" in m for m in rep_title.hash_mismatches), rep_title.hash_mismatches

    # 2. Tamper PUBTOOLINFO bgmhome_loud in expected bundle SFO -> MUST reject full_bundle_verified!
    bad_meta_loud = pkg.PackageMeta(
        title="Strict SFO Theme",
        title_id="CUSA54321",
        content_id="UP0001-CUSA54321_00-0000000000000001",
        category="ac",
        passcode="0" * 32,
        mode="theme",
        bgm_loudness=-16.5,
    )
    (cdir / "sce_sys" / "param.sfo").write_bytes(sfo.build_param_sfo_bytes(bad_meta_loud))
    rep_loud = inspect_and_validate_pkg(
        pkg_path, expected_content_dir=cdir, pkgtool_bin=pkg_forge.find_liborbispkg_tool()
    )
    assert rep_loud.verification_level == "failed"
    assert rep_loud.bundle_hashes_matched is False
    assert any("bgmhome_loud" in m for m in rep_loud.hash_mismatches), rep_loud.hash_mismatches

    # 3. Direct unit check: packager-allowed keys (PUBTOOLVER, img0_l0_size, c_date) produce zero diffs,
    #    while unauthorized extra key CUSTOM_ROGUE is caught!
    base_entries = sfo.build_sfo_entries(meta, c_date="20261120")
    allowed_entries = [
        e if e.key != "PUBTOOLINFO" else sfo.SfoEntry("PUBTOOLINFO", sfo.FMT_UTF8, "c_date=20261121,img0_l0_size=1234,bgmhome_loud=-24.00", 512)
        for e in base_entries
    ] + [sfo.SfoEntry("PUBTOOLVER", sfo.FMT_INT32, 0x02890000, 4)]
    assert compare_param_sfo_fields(sfo.encode_param_sfo(base_entries), sfo.encode_param_sfo(allowed_entries)) == []

    rogue_entries = base_entries + [sfo.SfoEntry("CUSTOM_ROGUE", sfo.FMT_INT32, 99, 4)]
    rogue_diffs = compare_param_sfo_fields(sfo.encode_param_sfo(base_entries), sfo.encode_param_sfo(rogue_entries))
    assert any("CUSTOM_ROGUE" in d for d in rogue_diffs), rogue_diffs


def test_svg_icon_import_zero_halo_and_security_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Section 13: Tabler white-stroke SVG icons rasterize cleanly to 128/152/512 with ZERO dark halo; unsafe SVGs rejected."""
    monkeypatch.setattr(projects, "ROOT", tmp_path / "projects")
    monkeypatch.setattr(jobs, "WORKSPACE", tmp_path / "standalone")

    tabler_src = REPO / "tools" / "icons" / "tabler" / "icons" / "outline" / "settings.svg"
    if tabler_src.is_file():
        white_svg_text = tabler_src.read_text(encoding="utf-8").replace("currentColor", "#ffffff")
    else:
        white_svg_text = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
            'fill="none" stroke="#ffffff" stroke-width="2"><circle cx="12" cy="12" r="8"/></svg>'
        )
    white_svg = tmp_path / "settings_white.svg"
    white_svg.write_text(white_svg_text, encoding="utf-8")

    # 1. Verify direct rasterization at 128x128, 152x152 (glow), and 512x512 has ZERO dark halo!
    for sz in ((128, 128), (152, 152), (512, 512)):
        out_png = tmp_path / f"icon_{sz[0]}.png"
        media.render_image(white_svg, out_png, sz)
        im = Image.open(out_png).convert("RGBA")
        assert im.size == sz
        arr = np.asarray(im)
        assert int(arr[..., 3].max()) > 200 and int(arr[..., 3].min()) == 0
        white_bg = Image.new("RGBA", sz, (255, 255, 255, 255))
        white_bg.alpha_composite(im)
        assert int(np.asarray(white_bg)[..., :3].min()) >= 250, f"Dark halo detected on SVG {sz}"

    # 2. Verify end-to-end API upload into slot (fn:setting), content icon (ct:browser), and library
    client = TestClient(main.app)
    assert client.get("/api/projects").status_code == 200
    pid = client.post("/api/projects", json={"name": "SVG Icons"}).json()["id"]

    with white_svg.open("rb") as fh:
        r_fn = client.post(f"/api/slot/fn:setting/upload?project={pid}", files={"file": ("settings.svg", fh, "image/svg+xml")})
    assert r_fn.status_code == 200, r_fn.text
    with white_svg.open("rb") as fh:
        r_ct = client.post(f"/api/slot/ct:browser/upload?project={pid}", files={"file": ("browser.svg", fh, "image/svg+xml")})
    assert r_ct.status_code == 200, r_ct.text
    with white_svg.open("rb") as fh:
        r_lib = client.post("/api/library", files={"file": ("settings.svg", fh, "image/svg+xml")})
    assert r_lib.status_code == 201, r_lib.text
    assert r_lib.json()["kind"] == "image"

    # 3. Verify unsafe SVG (<script> / <!ENTITY) is strictly rejected!
    evil_svg = tmp_path / "evil.svg"
    evil_svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', encoding="utf-8")
    with evil_svg.open("rb") as fh:
        r_bad = client.post("/api/library", files={"file": ("evil.svg", fh, "image/svg+xml")})
    assert r_bad.status_code == 422, r_bad.text


def test_benchmark_scripts_reject_zero_videos_and_valid_hyperfine_syntax(tmp_path: Path) -> None:
    """Section 14: benchmark_ouroboros_corpus.py MUST exit non-zero when 0 videos run or all fail; hyperfine script syntax OK."""
    # 1. Missing manifest -> 0 entries -> non-zero exit
    p1 = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "benchmark_ouroboros_corpus.py"), "--manifest", str(tmp_path / "missing.json")],
        capture_output=True,
        text=True,
    )
    assert p1.returncode != 0, f"Expected non-zero exit for empty corpus, got 0: {p1.stdout}"

    # 2. Manifest pointing only to nonexistent video -> 0 successes -> non-zero exit
    bad_manifest = tmp_path / "bad_manifest.json"
    bad_manifest.write_text('[{"id": "ghost", "split": "dev", "path": "/tmp/does_not_exist_999.mp4"}]', encoding="utf-8")
    p2 = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "benchmark_ouroboros_corpus.py"), "--manifest", str(bad_manifest)],
        capture_output=True,
        text=True,
    )
    assert p2.returncode != 0, f"Expected non-zero exit when all videos fail, got 0: {p2.stdout}"

    # 3. Hyperfine benchmark script bash syntax check
    p3 = subprocess.run(["bash", "-n", str(REPO / "scripts" / "benchmark_hyperfine.sh")], capture_output=True, text=True)
    assert p3.returncode == 0, p3.stderr
