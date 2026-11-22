"""Operação Fortaleza — Fase 7: Complete Adversarial & Security Test Suite.

CRITICAL PRINCIPLE:
  Not only tests malformed files directly, but ALSO packs semantic attacks into
  REAL, cryptographically valid-digest `.pkg` containers using LibOrbisPkg (`pkg_build`)
  so that `pkg_validate` and raw SHA-256 header checks PASS — proving that CZAR's
  post-extraction validator + PS4 Compatibility Contract + Digital Twin catch semantic
  defects that container-only validators miss!
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from PIL import Image

from app import at9, dxt1, mdx, media, pkg, raf, theme, theme_xml, vag
from app.forge import audio_validation, bundle_validator, pkg_forge, pkg_inspector
from app.forge.sfo import build_param_sfo_bytes
from app.forge.subproc import SubprocessTimeoutError, run_cancellable_process


def _write_png(path: Path, size: tuple[int, int], color: tuple[int, ...], mode: str = "RGBA") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color).save(path, "PNG")


def _write_valid_wav(path: Path, dur: float = 1.8, freq: float = 240.0) -> None:
    sr = 48000
    n = int(sr * dur)
    t = np.arange(n, dtype=np.float64) / sr
    s = 0.28 * np.sin(2.0 * math.pi * freq * t)
    pcm = np.clip(np.stack([s, s], axis=1) * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def _build_base_theme(root: Path, *, animated: bool = True, with_bgm: bool = False) -> tuple[Path, pkg.PackageMeta]:
    content = root / "content"
    content.mkdir(parents=True, exist_ok=True)
    _write_png(content / "texture" / "preview.png", (740, 416), (20, 40, 80), "RGB")
    _write_png(content / "sce_sys" / "icon0.png", (512, 512), (20, 40, 80), "RGB")
    _write_png(content / "sce_sys" / "pic1.png", (1920, 1080), (20, 40, 80), "RGB")
    _write_png(content / "texture" / "function_icon" / "trophy.png", (128, 128), (240, 200, 40, 230), "RGBA")
    _write_png(content / "texture" / "function_icon" / "trophy_glow.png", (152, 152), (255, 220, 90, 190), "RGBA")
    _write_png(content / "texture" / "content_icon" / "disc.png", (512, 512), (60, 140, 240, 240), "RGBA")
    _write_png(content / "texture" / "content_icon" / "browser.png", (512, 512), (220, 70, 140, 240), "RGBA")

    if not animated:
        _write_png(content / "texture" / "background" / "homescreen.png", (1920, 1080), (20, 40, 80), "RGB")
        _write_png(content / "texture" / "background" / "functionscreen.png", (1920, 1080), (15, 30, 60), "RGB")
    else:
        raf_dir = content / "scene" / "background.raf"
        raf_dir.mkdir(parents=True, exist_ok=True)
        names = []
        for i in range(6):
            arr = np.full((360, 640, 4), (30 + i * 32, 60 + i * 18, 130, 255), dtype=np.uint8)
            nm = theme.frame_name(i)
            (raf_dir / nm).write_bytes(dxt1.encode_dds(arr))
            names.append(nm)
        hw, hh = raf.plane_half_size()
        (raf_dir / raf.MODEL_REAL_FILE).write_bytes(mdx.quad_model(node_name=raf.MODEL_ID, half_w=hw, half_h=hh))
        theme.write_index_xml(raf_dir / "index.xml", raf.build_scene(names, 0.1))

    bgm_loud = None
    if with_bgm:
        wav_p = root / "bgm.wav"
        _write_valid_wav(wav_p)
        at9_p = content / "sound" / "bgm_home.at9"
        at9_p.parent.mkdir(parents=True, exist_ok=True)
        at9.encode(wav_p, at9_p, loop=True)
        bgm_loud = pkg.calc_loudness(at9_p)

    (content / "theme.xml").write_text(
        theme_xml.build_theme_xml(label="AdvTheme", themecolor=4, homebgm_enable=with_bgm),
        encoding="utf-8",
    )
    meta = pkg.PackageMeta(
        title="AdvTheme",
        title_id="CUSA77777",
        content_id="UP9000-CUSA77777_00-ADVTHEME00000001",
        category="ac",
        passcode="0" * 32,
        mode="theme",
        bgm_loudness=bgm_loud,
    )
    (content / "sce_sys" / "param.sfo").write_bytes(build_param_sfo_bytes(meta))
    return content, meta


def _pack_raw_pkg_bypassing_validator(content: Path, meta: pkg.PackageMeta, out_dir: Path) -> Path:
    """Deliberately bypass CZAR's pre-build validator and seal a REAL valid-digest PKG with LibOrbisPkg!"""
    pkgtool = pkg_forge.find_liborbispkg_tool()
    assert pkgtool is not None, "pkgtool required for valid-hash adversarial PKG tests"
    targets = sorted(p.relative_to(content).as_posix() for p in content.rglob("*") if p.is_file())
    gp4 = content.parent / "adv-linux.gp4"
    gp4.write_text(pkg.build_gp4(meta, targets, content.name).replace("\\", "/"), encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([str(pkgtool), "pkg_build", str(gp4), str(out_dir)], text=True, capture_output=True, timeout=120)
    assert proc.returncode == 0, f"LibOrbisPkg failed to pack adversarial fixture: {proc.stdout}\n{proc.stderr}"
    built = sorted(out_dir.glob("*.pkg"))
    assert built, "No .pkg emitted"
    return built[0]


# --- Valid-Hash Real PKG Semantic Attacks (packed via LibOrbisPkg) ---------


@pytest.mark.skipif(pkg_forge.find_liborbispkg_tool() is None, reason="liborbispkg pkgtool not installed")
@pytest.mark.parametrize(
    "attack_id,mutate_fn,expected_substring",
    [
        (
            "ADV03_xml_missing_mandatory_fields",
            lambda c: (c / "theme.xml").write_text(
                '<?xml version="1.0" encoding="utf-8"?><theme><format-version>4.0</format-version>'
                "<themecolor>4</themecolor><homebgm-enable>False</homebgm-enable></theme>",
                encoding="utf-8",
            ),
            "label",
        ),
        (
            "ADV04_xml_semantic_wrong_version_and_color",
            lambda c: (c / "theme.xml").write_text(
                (c / "theme.xml").read_text(encoding="utf-8").replace("<format-version>4.0</format-version>", "<format-version>3.0</format-version>"),
                encoding="utf-8",
            ),
            "format-version",
        ),
        (
            "ADV05_missing_dds_referenced_by_index",
            lambda c: (c / "scene" / "background.raf" / "05.dds").unlink(),
            "05.dds",
        ),
        (
            "ADV05b_orphan_dds_unreferenced",
            lambda c: shutil.copyfile(
                c / "scene" / "background.raf" / "00.dds",
                c / "scene" / "background.raf" / "99.dds",
            ),
            "99.dds",
        ),
        (
            "ADV06_raf_out_of_order_sequence",
            lambda c: (c / "scene" / "background.raf" / "index.xml").write_text(
                (c / "scene" / "background.raf" / "index.xml")
                .read_text(encoding="utf-8")
                .replace('id="f01"', 'id="__SWP__"')
                .replace('id="f02"', 'id="f01"')
                .replace('id="__SWP__"', 'id="f02"'),
                encoding="utf-8",
            ),
            "ordem",
        ),
        (
            "ADV07_raf_wait_ms_instead_of_seconds",
            lambda c: (c / "scene" / "background.raf" / "index.xml").write_text(
                (c / "scene" / "background.raf" / "index.xml")
                .read_text(encoding="utf-8")
                .replace('wait="0.1"', 'wait="100"'),
                encoding="utf-8",
            ),
            "wait",
        ),
        (
            "ADV08_truncated_plane_mdx_with_valid_magic",
            lambda c: (c / "scene" / "background.raf" / "plane.mdx").write_bytes(
                (c / "scene" / "background.raf" / "plane.mdx").read_bytes()[:48]
            ),
            "plane.mdx",
        ),
        (
            "ADV10_right_name_wrong_content_dds",
            lambda c: (c / "scene" / "background.raf" / "00.dds").write_bytes(
                b"NOT_A_DDS_FILE" + b"\x00" * (len((c / "scene" / "background.raf" / "00.dds").read_bytes()) - 14)
            ),
            "00.dds",
        ),
        (
            "ADV11_homebgm_true_without_at9",
            lambda c: (c / "theme.xml").write_text(
                (c / "theme.xml").read_text(encoding="utf-8").replace("<homebgm-enable>False</homebgm-enable>", "<homebgm-enable>True</homebgm-enable>"),
                encoding="utf-8",
            ),
            "bgm_home.at9",
        ),
        (
            "ADV13_wrong_png_dimensions_and_missing_alpha",
            lambda c: _write_png(c / "texture" / "function_icon" / "trophy.png", (128, 128), (255, 0, 0), "RGB"),
            "alfa",
        ),
        (
            "ADV16_unexpected_orphan_file_in_image0",
            lambda c: (c / "texture" / "unlisted_payload.png").write_bytes(b"intruder"),
            "esquema canônico",
        ),
    ],
)
def test_valid_digest_real_pkg_with_semantic_attack_is_rejected(tmp_path, attack_id, mutate_fn, expected_substring):
    """Even when the PKG container passes ALL cryptographic SHA-256 digests & pkg_validate,
    semantic defects inside Image0 MUST fail closed (`structure_validated=False`, `release_gate=BUILD_FAILED`)."""
    content, meta = _build_base_theme(tmp_path / attack_id, animated=True, with_bgm=False)
    mutate_fn(content)
    pkg_path = _pack_raw_pkg_bypassing_validator(content, meta, tmp_path / attack_id / "pkg")

    rep = pkg_inspector.inspect_and_validate_pkg(
        pkg_path,
        pkgtool_bin=pkg_forge.find_liborbispkg_tool(),
    )
    # Cryptographic container digests ARE genuinely valid — proving the attack truly bypassed container-only checks!
    assert rep.header_and_crypto_valid is True, f"{attack_id}: expected valid CNT digests"
    assert rep.image0_extracted is True, f"{attack_id}: expected Image0 extracted"
    # Post-extraction semantic validation + Digital Twin MUST reject it!
    assert rep.structure_validated is False, f"{attack_id}: FALSE POSITIVE! Semantic attack passed!"
    assert rep.verification_level == "failed"
    assert rep.release_gate == "BUILD_FAILED"
    assert any(expected_substring.lower() in e.lower() for e in rep.errors), (
        f"{attack_id}: expected '{expected_substring}' in errors, got: {rep.errors}"
    )


@pytest.mark.skipif(pkg_forge.find_liborbispkg_tool() is None, reason="liborbispkg pkgtool not installed")
def test_adv12_valid_digest_pkg_with_out_of_range_atrac9_smpl_loop_rejected(tmp_path):
    """Decodable ATRAC9 file whose smpl loop_end exceeds total decoded samples MUST be rejected inside a real PKG."""
    content, meta = _build_base_theme(tmp_path / "adv12", animated=False, with_bgm=True)
    at9_path = content / "sound" / "bgm_home.at9"
    blob = bytearray(at9_path.read_bytes())
    smpl_pos = blob.find(b"smpl")
    assert smpl_pos != -1
    # RIFF smpl chunk: header (8) + 36B header + SampleLoop[0]: dwIdentifier(0), dwType(4), dwStart(8), dwEnd(12)
    loop_end_off = smpl_pos + 8 + 36 + 12
    struct.pack_into("<I", blob, loop_end_off, 99_000_000)
    at9_path.write_bytes(bytes(blob))

    pkg_path = _pack_raw_pkg_bypassing_validator(content, meta, tmp_path / "adv12" / "pkg")
    rep = pkg_inspector.inspect_and_validate_pkg(pkg_path, pkgtool_bin=pkg_forge.find_liborbispkg_tool())
    assert rep.header_and_crypto_valid is True
    assert rep.structure_validated is False
    assert rep.release_gate == "BUILD_FAILED"
    assert any("loop_end" in e.lower() or "smpl" in e.lower() or "vgmstream" in e.lower() for e in rep.errors), rep.errors


@pytest.mark.skipif(pkg_forge.find_liborbispkg_tool() is None, reason="liborbispkg pkgtool not installed")
def test_adv09_swapped_same_size_icons_caught_by_bundle_parity(tmp_path):
    """Swapping two same-size icons (ct:disc <-> ct:browser) after bundle creation MUST fail SHA-256 parity."""
    expected_content, meta = _build_base_theme(tmp_path / "expected", animated=False, with_bgm=False)
    tampered_content = tmp_path / "tampered" / "content"
    shutil.copytree(expected_content, tampered_content)
    disc_p = tampered_content / "texture" / "content_icon" / "disc.png"
    brw_p = tampered_content / "texture" / "content_icon" / "browser.png"
    disc_b, brw_b = disc_p.read_bytes(), brw_p.read_bytes()
    disc_p.write_bytes(brw_b)
    brw_p.write_bytes(disc_b)

    pkg_path = _pack_raw_pkg_bypassing_validator(tampered_content, meta, tmp_path / "tampered" / "pkg")
    rep = pkg_inspector.inspect_and_validate_pkg(
        pkg_path,
        expected_content_dir=expected_content,
        pkgtool_bin=pkg_forge.find_liborbispkg_tool(),
    )
    assert rep.structure_validated is False
    assert rep.bundle_hashes_matched is False
    assert rep.release_gate == "BUILD_FAILED"
    assert any("disc.png" in m for m in rep.hash_mismatches)
    assert any("browser.png" in m for m in rep.hash_mismatches)


# --- Container Identity, Limits, Duplicate IDs & Inner-PFS Divergence -------


def test_adv01_adv02_wrong_category_missing_iro_tag_and_duplicate_cnt_ids(tmp_path):
    """Container with CATEGORY=gd, missing IRO_TAG=2, or duplicate CNT entry IDs MUST fail closed."""
    from app.forge.sfo import FMT_INT32, FMT_UTF8, SfoEntry, encode_param_sfo

    cid = "UP9000-CUSA77777_00-ADVTHEME00000001"
    bad_sfo = encode_param_sfo(
        [
            SfoEntry("CATEGORY", FMT_UTF8, "gd", 4),
            SfoEntry("CONTENT_ID", FMT_UTF8, cid, 48),
            SfoEntry("FORMAT", FMT_UTF8, "obs", 4),
            SfoEntry("IRO_TAG", FMT_INT32, 0, 4),
            SfoEntry("TITLE", FMT_UTF8, "BadApp", 128),
            SfoEntry("TITLE_ID", FMT_UTF8, "CUSA77777", 12),
            SfoEntry("VERSION", FMT_UTF8, "01.00", 8),
        ]
    )
    parsed_bad = {
        "file_size": 100,
        "package_size": 100,
        "drm_type": 0xF,
        "content_type": 0x1A,  # app instead of 0x1B AC
        "iro_tag": 0,          # missing SS_THEME=2
        "content_id": cid,
        "header_digest_ok": True,
        "header_signature_present": True,
        "body_digest_ok": True,
        "digest_table_ok": True,
        "pfs_image_digest_ok": True,
        "pfs_signed_digest_ok": True,
        "sc_entries1_hash_ok": True,
        "sc_entries2_hash_ok": True,
        "entry_digest_mismatches": [],
        "duplicate_entry_ids": ["0x1000"],
        "sc0_blobs": {
            "Sc0/param.sfo": bad_sfo,
            "Sc0/icon0.png": b"\x89PNG\r\n\x1a\n",
            "Sc0/pic1.png": b"\x89PNG\r\n\x1a\n",
        },
    }
    fake_pkg = tmp_path / "bad_cnt.pkg"
    fake_pkg.write_bytes(b"FAKEPKG!")
    with (
        mock.patch.object(pkg_inspector, "parse_pkg_binary", return_value=parsed_bad),
        mock.patch.object(pkg_inspector, "_extract_pkg_image0", return_value=(False, "skip")),
    ):
        rep = pkg_inspector.inspect_and_validate_pkg(fake_pkg)
    assert rep.structure_validated is False
    assert rep.theme_type_valid is False
    assert rep.release_gate == "BUILD_FAILED"
    assert any("duplicados" in e.lower() for e in rep.errors)
    assert any("0x1b" in e.lower() for e in rep.errors)
    assert any("iro_tag" in e.lower() for e in rep.errors)


def test_adv14_raf_over_6mib_and_over_48_frames_rejected(tmp_path):
    """RAF exceeding 6 MiB (14 frames @ 720p) or > 48 frames (49 frames @ 360p) MUST be rejected."""
    # Case A: 14 frames @ 1280x720 -> 14 * 460,928 = 6,452,992 B > 6,291,456 B
    c_big, _ = _build_base_theme(tmp_path / "over6m", animated=True, with_bgm=False)
    raf_dir = c_big / "scene" / "background.raf"
    for f in raf_dir.glob("*.dds"):
        f.unlink()
    big_rgba = np.full((720, 1280, 4), (40, 80, 120, 255), dtype=np.uint8)
    big_dds = dxt1.encode_dds(big_rgba)
    names = []
    for i in range(14):
        nm = theme.frame_name(i)
        (raf_dir / nm).write_bytes(big_dds)
        names.append(nm)
    theme.write_index_xml(raf_dir / "index.xml", raf.build_scene(names, 0.1))
    rep_big = bundle_validator.validate_theme_bundle(c_big)
    assert rep_big.valid is False
    assert any("6 mib" in e.lower() or "orçamento" in e.lower() for e in rep_big.errors)

    # Case B: 49 frames @ 640x360 -> exceeds MAX_FRAME_COUNT=48
    c_49, _ = _build_base_theme(tmp_path / "over48f", animated=True, with_bgm=False)
    raf49 = c_49 / "scene" / "background.raf"
    base_dds = (raf49 / "00.dds").read_bytes()
    names49 = []
    for i in range(49):
        nm = theme.frame_name(i)
        (raf49 / nm).write_bytes(base_dds)
        names49.append(nm)
    theme.write_index_xml(raf49 / "index.xml", raf.build_scene(names49, 0.1))
    rep_49 = bundle_validator.validate_theme_bundle(c_49)
    assert rep_49.valid is False
    assert any("48" in e or "quadros" in e.lower() for e in rep_49.errors)


# --- Security & Robustness Attacks (XML XXE/Bomb, SVG XSS, GP4 Traversal, Malformed PNG, Timeout) ---


def test_adv16_security_xml_bomb_svg_xss_gp4_traversal_malformed_png_and_timeout(tmp_path):
    # 1. XML DOCTYPE / ENTITY billion-laughs bomb in theme.xml & index.xml & GP4
    c_xml, meta = _build_base_theme(tmp_path / "xxe", animated=True, with_bgm=False)
    bomb = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]><theme/>'
    (c_xml / "theme.xml").write_text(bomb, encoding="utf-8")
    (c_xml / "scene" / "background.raf" / "index.xml").write_text(bomb.replace("<theme/>", "<raf/>"), encoding="utf-8")
    gp4_trav = tmp_path / "xxe" / "evil.gp4"
    gp4_trav.write_text(
        '<?xml version="1.0"?><psproject><volume><volume_type>pkg_ps4_theme</volume_type>'
        f'<package content_id="{meta.content_id}" passcode="{meta.passcode}"/></volume>'
        '<files><file targ_path="../etc/passwd" orig_path="content/theme.xml"/>'
        '<file targ_path="theme.xml" orig_path="content/theme.xml"/>'
        '<file targ_path="theme.xml" orig_path="content/theme.xml"/></files></psproject>',
        encoding="utf-8",
    )
    rep = bundle_validator.validate_theme_bundle(c_xml, gp4_path=gp4_trav)
    assert rep.valid is False
    assert any("doctype" in e.lower() or "entity" in e.lower() for e in rep.errors)
    assert any("inseguro" in e.lower() or "duplicado" in e.lower() for e in rep.errors)

    # 2. Malicious SVG payloads (script, event handler, XXE entity, external file:// href)
    for idx, payload in enumerate(
        [
            '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg" onload="fetch(1)"><rect width="10" height="10"/></svg>',
            '<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><svg>&xxe;</svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>',
        ]
    ):
        svg_p = tmp_path / f"evil_{idx}.svg"
        svg_p.write_text(payload, encoding="utf-8")
        with pytest.raises(media.MediaError):
            media.validate_svg_safe(svg_p)

    # 3. Malformed / truncated PNG inside theme bundle
    c_png, _ = _build_base_theme(tmp_path / "bad_png", animated=False, with_bgm=False)
    (c_png / "texture" / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
    rep_png = bundle_validator.validate_theme_bundle(c_png)
    assert rep_png.valid is False
    assert any("preview.png" in e for e in rep_png.errors)

    # 4. Inner PFS sce_sys/icon0.png diverging from outer Sc0/icon0.png MUST be rejected!
    c_pfs, meta_pfs = _build_base_theme(tmp_path / "pfs_div", animated=False, with_bgm=False)
    sc0_icon = (c_pfs / "sce_sys" / "icon0.png").read_bytes()
    sc0_pic1 = (c_pfs / "sce_sys" / "pic1.png").read_bytes()
    sc0_sfo = (c_pfs / "sce_sys" / "param.sfo").read_bytes()
    parsed_ok = {
        "file_size": 100,
        "package_size": 100,
        "drm_type": 0xF,
        "content_type": 0x1B,
        "iro_tag": 2,
        "content_id": meta_pfs.content_id,
        "header_digest_ok": True,
        "header_signature_present": True,
        "body_digest_ok": True,
        "digest_table_ok": True,
        "pfs_image_digest_ok": True,
        "pfs_signed_digest_ok": True,
        "sc_entries1_hash_ok": True,
        "sc_entries2_hash_ok": True,
        "entry_digest_mismatches": [],
        "duplicate_entry_ids": [],
        "sc0_blobs": {"Sc0/param.sfo": sc0_sfo, "Sc0/icon0.png": sc0_icon, "Sc0/pic1.png": sc0_pic1},
    }

    def _fake_extract_divergent(_p, dest, **_kw):
        shutil.copytree(c_pfs, dest / "uroot")
        # Tamper inner PFS sce_sys/icon0.png to differ from Sc0/icon0.png!
        _write_png(dest / "uroot" / "sce_sys" / "icon0.png", (512, 512), (255, 0, 255), "RGB")
        return True, "ok"

    fake_pkg = tmp_path / "div.pkg"
    fake_pkg.write_bytes(b"FAKEPKG!")
    with (
        mock.patch.object(pkg_inspector, "parse_pkg_binary", return_value=parsed_ok),
        mock.patch.object(pkg_inspector, "_extract_pkg_image0", side_effect=_fake_extract_divergent),
    ):
        rep_div = pkg_inspector.inspect_and_validate_pkg(fake_pkg)
    assert rep_div.structure_validated is False
    assert any("divergência sha-256" in e.lower() and "icon0.png" in e.lower() for e in rep_div.errors)

    # 5. Subprocess timeout fails cleanly & deterministically
    with pytest.raises(SubprocessTimeoutError):
        run_cancellable_process(["python3", "-c", "import time; time.sleep(5)"], timeout=0.2)


def test_fortaleza_fase10_dxt1_minor_eigenvector_block_byte_identical_to_reference():
    """Regression for Operação Fortaleza Fase 3 finding: anti-correlated gradient block whose
    bounding-box diagonal aligns with an unstable minor eigenvector MUST stay 100% byte-identical
    to upstream Imxnxl reference arithmetic."""
    from tests.test_zenitsu_bench_regressions import _reference_compress_dxt1a

    # Exact 4x4 block discovered in A3_dynamic_raf (cov = [[8, 0, -6], [0, 0, 0], [-6, 0, 8]])
    blk = np.array(
        [
            [[126, 42, 96, 255], [126, 42, 96, 255], [126, 42, 96, 255], [126, 42, 95, 255]],
            [[127, 42, 96, 255], [127, 42, 95, 255], [127, 42, 95, 255], [127, 42, 95, 255]],
            [[127, 42, 95, 255], [127, 42, 95, 255], [127, 42, 95, 255], [127, 42, 94, 255]],
            [[128, 42, 95, 255], [128, 42, 94, 255], [128, 42, 94, 255], [128, 42, 94, 255]],
        ],
        dtype=np.uint8,
    )
    tiled = np.tile(blk, (135, 240, 1))  # full 960x540 frame (32,400 blocks)
    assert dxt1.compress_dxt1a(tiled) == _reference_compress_dxt1a(tiled)

