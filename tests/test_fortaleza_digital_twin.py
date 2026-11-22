"""Operação Fortaleza — Fase 6: PS4 Theme Digital Twin automated test suite.

Validates that `simulate_extracted_theme` operates strictly on extracted content,
explicitly labels itself `SEMANTIC_SIMULATION_ONLY` (`physical_execution_proven=False`),
simulates >= 10 RAF cycles and >= 3 ATRAC9 audio cycles, and detects temporal, visual,
icon-duplication, and audio defects.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np
from PIL import Image

from app import at9, dxt1, mdx, pkg, raf, theme, theme_xml, vag
from app.forge.digital_twin import simulate_extracted_theme
from app.forge.sfo import build_param_sfo_bytes


def _write_png(path: Path, size: tuple[int, int], color: tuple[int, ...], mode: str = "RGBA") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color).save(path, "PNG")


def _build_extracted_animated_theme(root: Path, *, frames: int = 8, wait: float = 0.1, with_audio: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write_png(root / "texture" / "preview.png", (740, 416), (25, 35, 60), "RGB")
    _write_png(root / "sce_sys" / "icon0.png", (512, 512), (25, 35, 60), "RGB")
    _write_png(root / "sce_sys" / "pic1.png", (1920, 1080), (25, 35, 60), "RGB")
    _write_png(root / "texture" / "function_icon" / "trophy.png", (128, 128), (240, 190, 40, 230), "RGBA")
    _write_png(root / "texture" / "function_icon" / "trophy_glow.png", (152, 152), (250, 210, 80, 180), "RGBA")
    _write_png(root / "texture" / "content_icon" / "disc.png", (512, 512), (60, 160, 240, 235), "RGBA")
    _write_png(root / "texture" / "content_icon" / "browser.png", (512, 512), (180, 90, 240, 235), "RGBA")

    raf_dir = root / "scene" / "background.raf"
    raf_dir.mkdir(parents=True, exist_ok=True)
    names = []
    xs = np.linspace(0, 2 * math.pi, 640, dtype=np.float32)[None, :]
    ys = np.linspace(0, 2 * math.pi, 360, dtype=np.float32)[:, None]
    for i in range(frames):
        ph = 2.0 * math.pi * i / float(frames)
        r = (127.5 + 80.0 * np.sin(xs + ph)).astype(np.uint8)
        g = (127.5 + 80.0 * np.cos(ys + ph)).astype(np.uint8)
        b = np.full((360, 640), 110, dtype=np.uint8)
        a = np.full((360, 640), 255, dtype=np.uint8)
        rgba = np.stack([np.broadcast_to(r, (360, 640)), np.broadcast_to(g, (360, 640)), b, a], axis=-1)
        nm = theme.frame_name(i)
        (raf_dir / nm).write_bytes(dxt1.encode_dds(rgba))
        names.append(nm)
    hw, hh = raf.plane_half_size()
    (raf_dir / raf.MODEL_REAL_FILE).write_bytes(mdx.quad_model(node_name=raf.MODEL_ID, half_w=hw, half_h=hh))
    theme.write_index_xml(raf_dir / "index.xml", raf.build_scene(names, wait))

    bgm_loud = None
    if with_audio:
        wav_p = root.parent / "twin_bgm.wav"
        sr = 48000
        dur = 2.0
        t = np.arange(int(sr * dur), dtype=np.float64) / sr
        s = 0.28 * np.sin(2.0 * math.pi * 220.0 * t)
        pcm16 = np.clip(np.stack([s, s], axis=1) * 32767.0, -32768, 32767).astype("<i2")
        with wave.open(str(wav_p), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(pcm16.tobytes())
        at9_p = root / "sound" / "bgm_home.at9"
        at9_p.parent.mkdir(parents=True, exist_ok=True)
        at9.encode(wav_p, at9_p, loop=True)
        bgm_loud = pkg.calc_loudness(at9_p)
        sfx_pcm = (0.35 * np.sin(2.0 * math.pi * 780.0 * np.arange(9600) / 48000.0)).astype(np.float32)
        (root / "sound" / "se_decide.vag").write_bytes(vag.encode(sfx_pcm, 48000, "se_decide"))

    (root / "theme.xml").write_text(
        theme_xml.build_theme_xml(label="Twin Theme", themecolor=2, homebgm_enable=with_audio),
        encoding="utf-8",
    )
    meta = pkg.PackageMeta(
        title="Twin Theme",
        title_id="CUSA22222",
        content_id="UP9000-CUSA22222_00-TWINTHEME0000001",
        category="ac",
        passcode="0" * 32,
        mode="theme",
        bgm_loudness=bgm_loud,
    )
    (root / "sce_sys" / "param.sfo").write_bytes(build_param_sfo_bytes(meta))
    return root


def test_digital_twin_simulates_10_raf_cycles_and_3_audio_cycles_honestly(tmp_path):
    root = _build_extracted_animated_theme(tmp_path / "extracted", frames=8, wait=0.1, with_audio=True)
    rep = simulate_extracted_theme(root, cycles=10, audio_cycles=3)

    assert rep["passed"] is True, rep["errors"]
    assert rep["verification_kind"] == "SEMANTIC_SIMULATION_ONLY"
    assert rep["physical_execution_proven"] is False
    assert "não constitui prova" in rep["disclaimer"].lower()

    raf_s = rep["raf_simulation"]
    assert raf_s["valid"] is True
    assert raf_s["frames"] == 8
    assert raf_s["simulated_cycles"] == 10
    assert raf_s["simulated_steps"] == 80
    assert abs(raf_s["cycle_duration_sec"] - 0.8) < 1e-3
    assert raf_s["order_matches_canonical"] is True
    assert raf_s["duplicate_adjacent_pairs"] == []

    ico_s = rep["icons_and_presentation"]
    assert ico_s["valid"] is True
    assert ico_s["function_icons_count"] == 1
    assert ico_s["function_glows_count"] == 1
    assert ico_s["content_icons_count"] == 2
    assert ico_s["duplicate_icon_groups"] == []

    aud_s = rep["audio_simulation"]
    assert aud_s["valid"] is True
    mc = aud_s["bgm"]["multi_cycle_simulation"]
    assert mc["cycles"] == 3
    assert mc["decoded_duration_sec"] >= 5.5
    assert mc["peak_dbfs"] > -30.0
    assert aud_s["sfx"]["se_decide.vag"]["valid"] is True


def test_digital_twin_detects_out_of_order_sequence_and_duplicate_icons(tmp_path):
    root = _build_extracted_animated_theme(tmp_path / "broken", frames=6, wait=0.1, with_audio=False)
    # 1. Swap two <case> elements in index.xml -> breaks temporal order!
    idx_p = root / "scene" / "background.raf" / "index.xml"
    xml_txt = idx_p.read_text(encoding="utf-8")
    xml_txt = xml_txt.replace('id="f01"', 'id="__TMP__"').replace('id="f02"', 'id="f01"').replace('id="__TMP__"', 'id="f02"')
    idx_p.write_text(xml_txt, encoding="utf-8")

    # 2. Make browser.png byte-identical to disc.png -> triggers duplicate icon warning!
    (root / "texture" / "content_icon" / "browser.png").write_bytes(
        (root / "texture" / "content_icon" / "disc.png").read_bytes()
    )

    rep = simulate_extracted_theme(root)
    assert rep["passed"] is False
    assert any("ordem temporal" in e.lower() for e in rep["errors"])
    assert rep["icons_and_presentation"]["duplicate_icon_groups"] == [["ct:browser", "ct:disc"]]
    assert any("idêntico" in w.lower() for w in rep["warnings"])


def test_digital_twin_detects_duplicate_adjacent_frames_and_wrong_expected_hashes(tmp_path):
    root = _build_extracted_animated_theme(tmp_path / "dup_frames", frames=6, wait=0.1, with_audio=False)
    # Overwrite 01.dds with 00.dds -> frozen adjacent frame pair (0, 1)
    (root / "scene" / "background.raf" / "01.dds").write_bytes(
        (root / "scene" / "background.raf" / "00.dds").read_bytes()
    )
    rep = simulate_extracted_theme(root, expected_frame_order_hashes=["deadbeef"] * 6)
    assert rep["passed"] is False
    assert (0, 1) in rep["raf_simulation"]["duplicate_adjacent_pairs"]
    assert rep["raf_simulation"]["order_matches_expected_hashes"] is False
    assert any("manifesto" in e.lower() for e in rep["errors"])
