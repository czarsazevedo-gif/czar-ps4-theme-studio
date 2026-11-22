"""Operação Fortaleza — Fase 5: PS4 Theme Compatibility Contract automated test suite.

Verifies every documented contract rule (`PS4-CNT-001` .. `PS4-SFX-001`),
explicit community-tool divergence notes, and conservative fail-closed enforcement
over static and animated theme trees.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np
from PIL import Image

from app import at9, dxt1, mdx, pkg, raf, theme, theme_xml, vag
from app.forge.contracts import (
    PS4_PKG_CONTENT_TYPE_AC,
    PS4_THEME_CATEGORY,
    PS4_THEME_IRO_TAG,
    PS4_THEME_VOLUME_TYPE,
)
from app.forge.ps4_contract import PS4_COMPATIBILITY_CONTRACT_RULES, contract_catalog, evaluate_ps4_contract
from app.forge.sfo import build_param_sfo_bytes


def _write_png(path: Path, size: tuple[int, int], color: tuple[int, ...], mode: str = "RGBA") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color).save(path, "PNG")


def _write_wav(path: Path, dur: float = 2.0, freq: float = 220.0) -> None:
    sr = 48000
    n = int(sr * dur)
    t = np.arange(n, dtype=np.float64) / sr
    s = 0.25 * np.sin(2.0 * math.pi * freq * t)
    stereo = np.stack([s, s], axis=1)
    pcm = np.clip(stereo * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def _build_valid_tree(root: Path, *, animated: bool = False, with_audio: bool = False) -> tuple[Path, pkg.PackageMeta]:
    content = root / "content"
    content.mkdir(parents=True, exist_ok=True)
    _write_png(content / "texture" / "preview.png", (740, 416), (20, 40, 80), "RGB")
    _write_png(content / "sce_sys" / "icon0.png", (512, 512), (20, 40, 80), "RGB")
    _write_png(content / "sce_sys" / "pic1.png", (1920, 1080), (20, 40, 80), "RGB")
    _write_png(content / "texture" / "function_icon" / "trophy.png", (128, 128), (255, 200, 50, 220), "RGBA")
    _write_png(content / "texture" / "function_icon" / "trophy_glow.png", (152, 152), (255, 220, 80, 180), "RGBA")
    _write_png(content / "texture" / "content_icon" / "disc.png", (512, 512), (80, 180, 255, 240), "RGBA")

    if not animated:
        _write_png(content / "texture" / "background" / "homescreen.png", (1920, 1080), (15, 25, 45), "RGB")
        _write_png(content / "texture" / "background" / "functionscreen.png", (1920, 1080), (10, 20, 35), "RGB")
    else:
        raf_dir = content / "scene" / "background.raf"
        raf_dir.mkdir(parents=True, exist_ok=True)
        names = []
        for i in range(6):
            arr = np.full((360, 640, 4), (40 + i * 25, 80, 120, 255), dtype=np.uint8)
            blob = dxt1.encode_dds(arr)
            nm = theme.frame_name(i)
            (raf_dir / nm).write_bytes(blob)
            names.append(nm)
        hw, hh = raf.plane_half_size()
        (raf_dir / raf.MODEL_REAL_FILE).write_bytes(mdx.quad_model(node_name=raf.MODEL_ID, half_w=hw, half_h=hh))
        theme.write_index_xml(raf_dir / "index.xml", raf.build_scene(names, 0.1))

    bgm_loud = None
    if with_audio:
        wav_p = root / "src_bgm.wav"
        _write_wav(wav_p, dur=2.0)
        at9_p = content / "sound" / "bgm_home.at9"
        at9_p.parent.mkdir(parents=True, exist_ok=True)
        at9.encode(wav_p, at9_p, loop=True)
        bgm_loud = pkg.calc_loudness(at9_p)
        # Also include a valid VAG effect
        pcm = (0.3 * np.sin(2.0 * math.pi * 660.0 * np.arange(8192) / 48000.0)).astype(np.float32)
        (content / "sound" / "se_decide.vag").write_bytes(vag.encode(pcm, 48000, "se_decide"))

    (content / "theme.xml").write_text(
        theme_xml.build_theme_xml(label="Contract Theme", themecolor=4, homebgm_enable=with_audio),
        encoding="utf-8",
    )
    meta = pkg.PackageMeta(
        title="Contract Theme",
        title_id="CUSA12345",
        content_id="UP9000-CUSA12345_00-CONTRACTTHEME001",
        category="ac",
        passcode="0" * 32,
        mode="theme",
        bgm_loudness=bgm_loud,
    )
    (content / "sce_sys" / "param.sfo").write_bytes(build_param_sfo_bytes(meta))
    targets = sorted(p.relative_to(content).as_posix() for p in content.rglob("*") if p.is_file())
    gp4 = root / "theme.gp4"
    gp4.write_text(pkg.build_gp4(meta, targets, content.name), encoding="utf-8")
    return content, meta


def test_contract_catalog_documents_all_rules_and_community_divergences():
    cat = contract_catalog()
    ids = {r["id"] for r in cat}
    assert len(cat) == len(PS4_COMPATIBILITY_CONTRACT_RULES) == 9
    assert {
        "PS4-CNT-001",
        "PS4-CNT-002",
        "PS4-TREE-001",
        "PS4-XML-001",
        "PS4-RAF-001",
        "PS4-BG-001",
        "PS4-ICON-001",
        "PS4-AUD-001",
        "PS4-SFX-001",
    } == ids
    # Community tool divergences must be explicitly documented (not hidden as consensus)
    divergences = {r["id"]: r["divergence_note"] for r in cat if r["divergence_note"]}
    assert "PS4-CNT-001" in divergences and "OrbisPkgTool" in divergences["PS4-CNT-001"]
    assert "PS4-RAF-001" in divergences and "segundos" in divergences["PS4-RAF-001"]
    assert "PS4-AUD-001" in divergences and "144 kbps" in divergences["PS4-AUD-001"]


def test_contract_passes_valid_static_and_animated_audio_themes(tmp_path):
    static_content, _ = _build_valid_tree(tmp_path / "static", animated=False, with_audio=False)
    rep_s = evaluate_ps4_contract(
        static_content,
        pkg_header_meta={
            "content_type_header": PS4_PKG_CONTENT_TYPE_AC,
            "iro_tag_header": PS4_THEME_IRO_TAG,
            "iro_tag_sfo": PS4_THEME_IRO_TAG,
            "category": PS4_THEME_CATEGORY,
            "volume_type": PS4_THEME_VOLUME_TYPE,
        },
        gp4_path=tmp_path / "static" / "theme.gp4",
    )
    assert rep_s["passed"] is True, rep_s["errors"]
    assert rep_s["physical_execution_proven"] is False
    assert all(r["status"] == "PASS" for r in rep_s["rules"])

    anim_content, _ = _build_valid_tree(tmp_path / "anim", animated=True, with_audio=True)
    rep_a = evaluate_ps4_contract(
        anim_content,
        pkg_header_meta={
            "content_type_header": PS4_PKG_CONTENT_TYPE_AC,
            "iro_tag_header": PS4_THEME_IRO_TAG,
            "iro_tag_sfo": PS4_THEME_IRO_TAG,
            "category": PS4_THEME_CATEGORY,
            "volume_type": PS4_THEME_VOLUME_TYPE,
        },
        gp4_path=tmp_path / "anim" / "theme.gp4",
    )
    assert rep_a["passed"] is True, rep_a["errors"]
    assert rep_a["theme_kind"] == "animated"
    assert all(r["status"] == "PASS" for r in rep_a["rules"])


def test_contract_rejects_wrong_container_header_and_orphan_file(tmp_path):
    content, _ = _build_valid_tree(tmp_path / "bad", animated=False, with_audio=False)
    (content / "texture" / "unexpected_junk.png").write_bytes(b"junk")

    rep = evaluate_ps4_contract(
        content,
        pkg_header_meta={
            "content_type_header": 0x1A,  # wrong: application instead of AC
            "iro_tag_header": 0,          # wrong: missing SS_THEME=2
            "iro_tag_sfo": 2,
            "category": "gd",             # wrong: game instead of ac
            "volume_type": "pkg_ps4_app",
        },
    )
    assert rep["passed"] is False
    by_id = {r["id"]: r["status"] for r in rep["rules"]}
    assert by_id["PS4-CNT-001"] == "FAIL"
    assert by_id["PS4-TREE-001"] == "FAIL"
