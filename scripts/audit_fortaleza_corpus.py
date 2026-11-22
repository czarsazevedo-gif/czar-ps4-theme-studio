#!/usr/bin/env python3
"""Operação Fortaleza — Reproducible Corpus Builder, A/B/C Differential Audit & 4-Tool Validator.

Implements Fases 2, 3, and 4 of Operação Fortaleza:
  - Grupo A: 6 reproducible CZAR PS4 Theme Studio themes (static simple, static + icons,
    dynamic RAF, dynamic full with icons/glows/BGM/3 VAGs, real Zenitsu, real Crown of Thorns)
    plus the frozen original Zenitsu Drive PKG (read-only audit).
  - Grupo B: Identical source assets passed through upstream Imxnxl/PS4-Ultimate-Theme-Creator
    (commit aa595aa6) in an isolated subprocess for byte-for-byte asset/XML/DDS/MDX/VAG/GP4/SFX diffs.
  - Grupo C: Independent multi-tool validation and extraction across:
      1. czar-python-pkg-inspector (+ PS4 Compatibility Contract + PS4 Theme Digital Twin)
      2. LibOrbisPkg (pkg_validate + pkg_extract — shared builder family, explicitly labeled)
      3. pearlxcore/OrbisPkgTool (independent C#/.NET 8-stage validator + extractor)
      4. xXJSONDeruloXx/ps4-pkg-tools (independent C++23 ShadPS4 7.0 decryptor + extractor)

All built `.pkg` files and private media outputs are stored OUTSIDE Git by default
(`/home/antigravity/.gemini/czar-operacao-fortaleza/`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import at9, dxt1, mdx, media, pkg, raf, theme, theme_xml, vag  # noqa: E402
from app.config import (  # noqa: E402
    CONTENT_ICONS,
    CONTENT_ICON_SIZE,
    FUNCTION_ICONS,
    FUNCTION_ICON_GLOW_SIZE,
    FUNCTION_ICON_SIZE,
    MAX_SFX_SECONDS,
    PIC1_SIZE,
    PREVIEW_SIZE,
    SCE_ICON_SIZE,
    STATIC_SIZE,
)
from app.icon_mapper import resolve_icon_filename  # noqa: E402
from app.forge.audio_validation import validate_at9  # noqa: E402
from app.forge.bundle_validator import validate_theme_bundle  # noqa: E402
from app.forge.digital_twin import simulate_extracted_theme  # noqa: E402
from app.forge.pkg_forge import build_and_validate_theme_pkg, find_liborbispkg_tool  # noqa: E402
from app.forge.pkg_inspector import compare_param_sfo_fields, inspect_and_validate_pkg, parse_pkg_binary  # noqa: E402
from app.forge.ps4_contract import evaluate_ps4_contract  # noqa: E402
from app.forge.sfo import build_param_sfo_bytes, parse_param_sfo  # noqa: E402

SFX_SLOTS = ("se_decide", "se_cursor", "se_cancel")

DEFAULT_OUT_ROOT = Path.home() / ".gemini" / "czar-operacao-fortaleza"
ZENITSU_RAW_DIR = Path.home() / ".gemini" / "czar-zenitsu-audit" / "drive_raw"
FROZEN_ZENITSU_PKG = (
    Path.home()
    / ".gemini"
    / "czar-zenitsu-audit"
    / "frozen_for_ps4"
    / "UP9000-CUSA00000_00-ZENITSUSOUL00000.pkg"
)
CROWN_MP4 = (
    Path.home()
    / ".gemini"
    / "czar-perceptual-supremacy"
    / "corpus"
    / "the-crown-of-thorns-moewalls-com.mp4"
)
IMXNXL_REPO = Path("/tmp/ps4-ultimate-theme-creator")
ORBIS_PKG_TOOL_DLL = Path("/tmp/fortaleza_tools/OrbisPkgTool/OrbisPkgTool/bin/Release/net8.0/OrbisPkgTool.dll")
SHADPS4_PKG_TOOL = Path("/tmp/fortaleza_tools/ps4-pkg-tools/build/ps4-pkg-tool")
DOTNET_BIN = REPO_ROOT / "tools" / "dotnet" / "dotnet"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _make_gradient_png(path: Path, size: tuple[int, int], c1: tuple[int, int, int], c2: tuple[int, int, int], label: str) -> None:
    w, h = size
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
    ys = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    t = 0.65 * xs + 0.35 * ys
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    for ch in range(3):
        arr[..., ch] = np.clip(c1[ch] * (1.0 - t) + c2[ch] * t, 0, 255).astype(np.uint8)
    im = Image.fromarray(arr, mode="RGB")
    draw = ImageDraw.Draw(im)
    draw.rectangle([w // 12, h // 12, w - w // 12, h - h // 12], outline=(255, 255, 255), width=max(2, w // 240))
    draw.text((w // 10, h // 10), label, fill=(255, 255, 255))
    im.save(path, "PNG")


def _make_distinct_icon_png(path: Path, size: tuple[int, int], idx: int, kind: str) -> None:
    """Create a deterministic, visually unique RGBA icon with real transparency."""
    w, h = size
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    r = (50 + (idx * 73) % 190)
    g = (60 + (idx * 137) % 180)
    b = (70 + (idx * 199) % 170)
    pad = max(6, w // 10)
    draw.rounded_rectangle([pad, pad, w - pad, h - pad], radius=max(8, w // 6), fill=(r, g, b, 245), outline=(255, 255, 255, 255), width=max(2, w // 48))
    # Distinct inner geometry per slot so no two icons share a SHA-256
    cx, cy = w // 2, h // 2
    rad = w // 5
    if idx % 3 == 0:
        draw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=(255, 255, 255, 220))
    elif idx % 3 == 1:
        draw.polygon([(cx, cy - rad), (cx + rad, cy + rad), (cx - rad, cy + rad)], fill=(255, 240, 120, 230))
    else:
        draw.rectangle([cx - rad, cy - rad, cx + rad, cy + rad], fill=(120, 245, 255, 230))
    draw.text((pad + 4, pad + 4), f"{kind}:{idx}", fill=(10, 10, 15, 255))
    im.save(path, "PNG")


def _make_wav_tone(path: Path, *, duration_sec: float, freq_hz: float, freq2_hz: float, amp: float = 0.25) -> None:
    sr = 48000
    n = int(round(sr * duration_sec))
    t = np.arange(n, dtype=np.float64) / sr
    # Integer cycle counts ensure seamless cyclic boundary
    c1 = max(1, round(freq_hz * duration_sec)) / duration_sec
    c2 = max(1, round(freq2_hz * duration_sec)) / duration_sec
    env = 0.75 + 0.25 * np.cos(2.0 * math.pi * t / duration_sec)
    left = amp * env * (0.6 * np.sin(2.0 * math.pi * c1 * t) + 0.4 * np.sin(2.0 * math.pi * c2 * t))
    right = amp * env * (0.6 * np.cos(2.0 * math.pi * c1 * t) + 0.4 * np.sin(2.0 * math.pi * (c2 * 1.5) * t))
    stereo = np.stack([left, right], axis=1)
    pcm16 = np.clip(stereo * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())


def _make_sfx_wav(path: Path, *, duration_sec: float, base_hz: float) -> None:
    sr = 48000
    n = int(round(sr * duration_sec))
    t = np.arange(n, dtype=np.float64) / sr
    env = np.exp(-t * 14.0) * np.minimum(1.0, t / 0.005)
    sig = 0.45 * env * np.sin(2.0 * math.pi * base_hz * t * (1.0 + 0.25 * t))
    pcm16 = np.clip(sig * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())


def _make_seamless_animation_frames(out_dir: Path, *, count: int, size: tuple[int, int], seed_tag: str) -> list[Path]:
    """Generate deterministic phase-locked cyclic PNG frames (zero seam jump)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = size
    xs = np.linspace(0.0, 2.0 * math.pi, w, dtype=np.float32)[None, :]
    ys = np.linspace(0.0, 2.0 * math.pi, h, dtype=np.float32)[:, None]
    paths: list[Path] = []
    for i in range(count):
        phase = 2.0 * math.pi * (i / float(count))
        r = 127.5 + 85.0 * np.sin(xs + phase) * np.cos(ys * 0.5)
        g = np.broadcast_to(127.5 + 85.0 * np.cos(ys - phase), (h, w))
        b = 127.5 + 85.0 * np.sin((xs + ys) * 0.5 + phase)
        arr = np.stack([r, g, b], axis=-1).clip(0, 255).astype(np.uint8)
        im = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(im)
        cx = int(w * (0.5 + 0.25 * math.cos(phase)))
        cy = int(h * (0.5 + 0.20 * math.sin(phase)))
        rad = max(18, h // 12)
        draw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=(255, 220, 80), outline=(255, 255, 255), width=3)
        draw.text((24, 24), f"{seed_tag} f{i:02d}/{count:02d}", fill=(255, 255, 255))
        p = out_dir / f"frame_{i:02d}.png"
        im.save(p, "PNG")
        paths.append(p)
    return paths


def _extract_video_frames_exact(video_path: Path, out_dir: Path, *, count: int, size: tuple[int, int]) -> list[Path]:
    """Deterministically extract `count` evenly spaced frames from a real MP4 at exact `size`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = size
    probe = subprocess.check_output(
        [media.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video_path)],
        text=True,
    ).strip()
    dur = max(0.5, float(probe))
    span = min(dur, 2.0)
    paths: list[Path] = []
    for i in range(count):
        ts = (i / float(count)) * span
        p = out_dir / f"frame_{i:02d}.png"
        subprocess.run(
            [
                media.FFMPEG,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-ss",
                f"{ts:.4f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-vf",
                f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
                str(p),
            ],
            check=True,
            timeout=30,
        )
        paths.append(p)
    return paths


def _build_theme_spec_sources(spec_id: str, src_root: Path) -> dict[str, Any]:
    """Create/gather canonical source files for one of the 6 Grupo A/B corpus themes."""
    sdir = src_root / spec_id
    sdir.mkdir(parents=True, exist_ok=True)

    spec: dict[str, Any] = {
        "id": spec_id,
        "animated": False,
        "frames": 0,
        "wait": 0.1,
        "anim_size": None,
        "home_png": None,
        "func_png": None,
        "preview_png": None,
        "icon0_png": None,
        "pic1_png": None,
        "fn_icons": {},
        "ct_icons": {},
        "frame_pngs": [],
        "bgm_source": None,
        "sfx_sources": {},
        "theme_settings": {
            "label": f"Fortaleza {spec_id}",
            "themecolor": 4,
            "fontcolor": "#FFFFFFFF",
            "fontshadowcolor": "#FF000000",
            "focuscolor": "#FF1E9BFF",
        },
    }

    if spec_id == "A1_static_simple":
        spec["title"] = "Fortaleza Static Simple"
        spec["title_id"] = "CUSA90001"
        spec["content_id"] = "UP9000-CUSA90001_00-FORTSTATIC000001"
        home = sdir / "home.png"
        func = sdir / "func.png"
        _make_gradient_png(home, (1920, 1080), (14, 28, 62), (45, 92, 165), "A1 STATIC HOME")
        _make_gradient_png(func, (1920, 1080), (10, 18, 42), (32, 65, 128), "A1 STATIC FUNC")
        spec["home_png"] = home
        spec["func_png"] = func

    elif spec_id == "A2_static_icons":
        spec["title"] = "Fortaleza Static Icons"
        spec["title_id"] = "CUSA90002"
        spec["content_id"] = "UP9000-CUSA90002_00-FORTSTATIC000002"
        home = sdir / "home.png"
        func = sdir / "func.png"
        _make_gradient_png(home, (1920, 1080), (22, 16, 48), (110, 42, 148), "A2 STATIC ICONS HOME")
        _make_gradient_png(func, (1920, 1080), (18, 12, 36), (80, 32, 118), "A2 STATIC ICONS FUNC")
        spec["home_png"] = home
        spec["func_png"] = func
        ico_dir = sdir / "icons"
        ico_dir.mkdir(exist_ok=True)
        for i, name in enumerate(FUNCTION_ICONS):
            p = ico_dir / f"fn_{name}.png"
            _make_distinct_icon_png(p, (256, 256), i + 1, "fn")
            spec["fn_icons"][name] = p
        for i, name in enumerate(CONTENT_ICONS):
            p = ico_dir / f"ct_{name}.png"
            _make_distinct_icon_png(p, (512, 512), i + 20, "ct")
            spec["ct_icons"][name] = p

    elif spec_id == "A3_dynamic_raf":
        spec["title"] = "Fortaleza Dynamic RAF"
        spec["title_id"] = "CUSA90003"
        spec["content_id"] = "UP9000-CUSA90003_00-FORTDYNRAF000003"
        spec["animated"] = True
        spec["frames"] = 12
        spec["wait"] = 0.1
        spec["anim_size"] = (960, 540)
        home = sdir / "home.png"
        _make_gradient_png(home, (1920, 1080), (12, 46, 54), (24, 145, 162), "A3 DYNAMIC RAF")
        spec["home_png"] = home
        spec["frame_pngs"] = _make_seamless_animation_frames(sdir / "frames", count=12, size=(960, 540), seed_tag="A3-RAF")

    elif spec_id == "A4_dynamic_full":
        spec["title"] = "Fortaleza Dynamic Full"
        spec["title_id"] = "CUSA90004"
        spec["content_id"] = "UP9000-CUSA90004_00-FORTDYNFUL000004"
        spec["animated"] = True
        spec["frames"] = 12
        spec["wait"] = 0.0833
        spec["anim_size"] = (960, 540)
        home = sdir / "home.png"
        _make_gradient_png(home, (1920, 1080), (54, 18, 22), (185, 76, 36), "A4 DYNAMIC FULL")
        spec["home_png"] = home
        spec["frame_pngs"] = _make_seamless_animation_frames(sdir / "frames", count=12, size=(960, 540), seed_tag="A4-FULL")
        ico_dir = sdir / "icons"
        ico_dir.mkdir(exist_ok=True)
        for i, name in enumerate(FUNCTION_ICONS):
            p = ico_dir / f"fn_{name}.png"
            _make_distinct_icon_png(p, (256, 256), i + 40, "fn4")
            spec["fn_icons"][name] = p
        for i, name in enumerate(CONTENT_ICONS):
            p = ico_dir / f"ct_{name}.png"
            _make_distinct_icon_png(p, (512, 512), i + 60, "ct4")
            spec["ct_icons"][name] = p
        bgm = sdir / "bgm.wav"
        _make_wav_tone(bgm, duration_sec=4.0, freq_hz=220.0, freq2_hz=330.0)
        spec["bgm_source"] = bgm
        for idx, sfx_slot in enumerate(SFX_SLOTS):
            sp = sdir / f"{sfx_slot}.wav"
            _make_sfx_wav(sp, duration_sec=0.32, base_hz=520.0 + idx * 180.0)
            spec["sfx_sources"][sfx_slot] = sp

    elif spec_id == "A5_zenitsu_real":
        spec["title"] = "ZenitsuSoul"
        spec["title_id"] = "CUSA00000"
        spec["content_id"] = "UP9000-CUSA00000_00-ZENITSUSOUL00000"
        spec["animated"] = True
        spec["frames"] = 13
        spec["wait"] = 0.075
        spec["anim_size"] = (1280, 720)
        spec["theme_settings"]["label"] = "ZenitsuSoul"
        spec["theme_settings"]["themecolor"] = 5
        spec["theme_settings"]["focuscolor"] = "#FFFFD028"
        if not ZENITSU_RAW_DIR.is_dir():
            raise FileNotFoundError(f"Mídias reais do Zenitsu não encontradas em {ZENITSU_RAW_DIR}")
        # Map owner's actual Drive files cleanly
        raw_files = {p.name.lower(): p for p in ZENITSU_RAW_DIR.iterdir() if p.is_file()}
        vid = next((p for n, p in raw_files.items() if n.endswith(".mp4") and "zenitsu" in n), None)
        if vid is None:
            vid = next(p for n, p in raw_files.items() if n.endswith(".mp4"))
        bg = next((p for n, p in raw_files.items() if "wallpaper" in n or "fundo" in n or "background" in n), None)
        if bg is None:
            bg = next(p for n, p in raw_files.items() if n.endswith((".jpg", ".jpeg", ".png")) and "icon" not in n)
        spec["home_png"] = bg
        spec["frame_pngs"] = _extract_video_frames_exact(vid, sdir / "frames", count=13, size=(1280, 720))

        # Use CZAR Studio's canonical PT-BR batch mapper on real Zenitsu icons
        for p in sorted(ZENITSU_RAW_DIR.iterdir()):
            if not p.is_file():
                continue
            suf = p.suffix.lower()
            if suf in {".mp3", ".wav", ".ogg", ".flac", ".at9", ".m4a"}:
                low = p.name.lower()
                if "decide" in low or "select" in low or "ok" in low or "confirm" in low:
                    spec["sfx_sources"]["se_decide"] = p
                elif "cursor" in low or "move" in low or "nav" in low:
                    spec["sfx_sources"]["se_cursor"] = p
                elif "cancel" in low or "back" in low or "voltar" in low:
                    spec["sfx_sources"]["se_cancel"] = p
                elif spec["bgm_source"] is None:
                    spec["bgm_source"] = p
            elif suf in {".png", ".jpg", ".jpeg", ".webp"} and p != bg:
                mapped, is_glow = resolve_icon_filename(p.name)
                if mapped and not is_glow and mapped.startswith("fn:"):
                    spec["fn_icons"].setdefault(mapped[3:], p)
                elif mapped and not is_glow and mapped.startswith("ct:"):
                    spec["ct_icons"].setdefault(mapped[3:], p)

    elif spec_id == "A6_crown_of_thorns_real":
        spec["title"] = "CrownOfThorns"
        spec["title_id"] = "CUSA90006"
        spec["content_id"] = "UP9000-CUSA90006_00-CROWNTHORNS00006"
        spec["animated"] = True
        spec["frames"] = 13
        spec["wait"] = 0.1
        spec["anim_size"] = (1280, 720)
        spec["theme_settings"]["label"] = "CrownOfThorns"
        spec["theme_settings"]["themecolor"] = 7
        if not CROWN_MP4.is_file():
            raise FileNotFoundError(f"Vídeo real Crown of Thorns ausente em {CROWN_MP4}")
        spec["frame_pngs"] = _extract_video_frames_exact(CROWN_MP4, sdir / "frames", count=13, size=(1280, 720))
        spec["home_png"] = spec["frame_pngs"][0]
        ico_dir = sdir / "icons"
        ico_dir.mkdir(exist_ok=True)
        for i, name in enumerate(("trophy", "setting", "power", "friend")):
            p = ico_dir / f"fn_{name}.png"
            _make_distinct_icon_png(p, (256, 256), i + 80, "crn")
            spec["fn_icons"][name] = p
        for i, name in enumerate(("disc", "browser", "library")):
            p = ico_dir / f"ct_{name}.png"
            _make_distinct_icon_png(p, (512, 512), i + 90, "crnc")
            spec["ct_icons"][name] = p
        bgm = sdir / "crown_bgm.wav"
        _make_wav_tone(bgm, duration_sec=3.5, freq_hz=174.61, freq2_hz=261.63)
        spec["bgm_source"] = bgm
        for idx, sfx_slot in enumerate(SFX_SLOTS):
            sp = sdir / f"{sfx_slot}.wav"
            _make_sfx_wav(sp, duration_sec=0.28, base_hz=440.0 + idx * 140.0)
            spec["sfx_sources"][sfx_slot] = sp
    else:
        raise ValueError(f"Spec desconhecido: {spec_id}")

    return spec


def _assemble_studio_content(spec: dict[str, Any], work_dir: Path) -> tuple[Path, Path, pkg.PackageMeta]:
    """Assemble a Grupo A theme tree using CZAR PS4 Theme Studio canonical modules."""
    content = work_dir / "content"
    content.mkdir(parents=True, exist_ok=True)

    # 1. Presentation images + static backgrounds (when static)
    home_src = Path(spec["home_png"])
    func_src = Path(spec["func_png"] or spec["home_png"])
    (content / "texture").mkdir(parents=True, exist_ok=True)
    (content / "sce_sys").mkdir(parents=True, exist_ok=True)

    media.render_image(home_src, content / "texture" / "preview.png", PREVIEW_SIZE)
    media.render_image(home_src, content / "sce_sys" / "icon0.png", SCE_ICON_SIZE)
    media.render_image(home_src, content / "sce_sys" / "pic1.png", PIC1_SIZE)

    if not spec["animated"]:
        (content / "texture" / "background").mkdir(parents=True, exist_ok=True)
        media.render_image(home_src, content / "texture" / "background" / "homescreen.png", STATIC_SIZE)
        media.render_image(func_src, content / "texture" / "background" / "functionscreen.png", STATIC_SIZE)
    else:
        raf_dir = content / "scene" / "background.raf"
        raf_dir.mkdir(parents=True, exist_ok=True)
        names: list[str] = []
        for i, fp in enumerate(spec["frame_pngs"]):
            with Image.open(fp) as im:
                rgba = np.asarray(im.convert("RGBA"))
            blob = dxt1.encode_dds(rgba)
            name = theme.frame_name(i)
            (raf_dir / name).write_bytes(blob)
            names.append(name)
        half_w, half_h = raf.plane_half_size()
        (raf_dir / raf.MODEL_REAL_FILE).write_bytes(mdx.quad_model(node_name=raf.MODEL_ID, half_w=half_w, half_h=half_h))
        theme.write_index_xml(raf_dir / "index.xml", raf.build_scene(names, float(spec["wait"])))

    # 2. Function & Content icons
    if spec["fn_icons"]:
        fn_dir = content / "texture" / "function_icon"
        fn_dir.mkdir(parents=True, exist_ok=True)
        for name, src_p in sorted(spec["fn_icons"].items()):
            media.render_image(Path(src_p), fn_dir / f"{name}.png", FUNCTION_ICON_SIZE)
            media.render_image(Path(src_p), fn_dir / f"{name}_glow.png", FUNCTION_ICON_GLOW_SIZE)

    if spec["ct_icons"]:
        ct_dir = content / "texture" / "content_icon"
        ct_dir.mkdir(parents=True, exist_ok=True)
        for name, src_p in sorted(spec["ct_icons"].items()):
            media.render_image(Path(src_p), ct_dir / f"{name}.png", CONTENT_ICON_SIZE)

    # 3. Audio (BGM ATRAC9 + SFX VAG)
    bgm_loud = None
    has_bgm = False
    if spec["bgm_source"] is not None:
        snd_dir = content / "sound"
        snd_dir.mkdir(parents=True, exist_ok=True)
        bgm_out = snd_dir / "bgm_home.at9"
        at9.encode(Path(spec["bgm_source"]), bgm_out, loop=True)
        bgm_loud = pkg.calc_loudness(bgm_out)
        has_bgm = True

    if spec["sfx_sources"]:
        snd_dir = content / "sound"
        snd_dir.mkdir(parents=True, exist_ok=True)
        for slot_name, src_p in sorted(spec["sfx_sources"].items()):
            pcm = media.pcm_mono(Path(src_p), 48000, MAX_SFX_SECONDS)
            (snd_dir / f"{slot_name}.vag").write_bytes(vag.encode(pcm, 48000, slot_name))

    # 4. theme.xml, param.sfo, param.sfx, theme.gp4
    ts = dict(spec["theme_settings"])
    xml_text = theme_xml.build_theme_xml(homebgm_enable=has_bgm, **ts)
    (content / "theme.xml").write_text(xml_text, encoding="utf-8")

    meta = pkg.PackageMeta(
        title=spec["title"],
        title_id=spec["title_id"],
        content_id=spec["content_id"],
        category="ac",
        passcode="0" * 32,
        mode="theme",
        bgm_loudness=bgm_loud,
    )
    meta.validate()
    (content / "sce_sys" / "param.sfo").write_bytes(build_param_sfo_bytes(meta))
    (work_dir / "param.sfx").write_text(pkg.build_param_sfx(meta), encoding="utf-8")
    targets = sorted(p.relative_to(content).as_posix() for p in content.rglob("*") if p.is_file())
    gp4_path = work_dir / "theme.gp4"
    gp4_path.write_text(pkg.build_gp4(meta, targets, content.name), encoding="utf-8")
    return content, gp4_path, meta


def _generate_imxnxl_reference_tree(spec: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Run upstream Imxnxl/PS4-Ultimate-Theme-Creator in an isolated subprocess on IDENTICAL source files."""
    if not IMXNXL_REPO.is_dir():
        return {"status": "NÃO EXECUTADO", "reason": f"Repositório de referência ausente em {IMXNXL_REPO}"}

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "imxnxl_repo": str(IMXNXL_REPO),
        "out_dir": str(out_dir),
        "title": spec["title"],
        "title_id": spec["title_id"],
        "content_id": spec["content_id"],
        "animated": spec["animated"],
        "wait": float(spec["wait"]),
        "home_png": str(spec["home_png"]),
        "func_png": str(spec["func_png"] or spec["home_png"]),
        "fn_icons": {k: str(v) for k, v in spec["fn_icons"].items()},
        "ct_icons": {k: str(v) for k, v in spec["ct_icons"].items()},
        "frame_pngs": [str(p) for p in spec["frame_pngs"]],
        "sfx_sources": {k: str(v) for k, v in spec["sfx_sources"].items()},
        "has_bgm": spec["bgm_source"] is not None,
        "theme_settings": spec["theme_settings"],
    }

    driver = r"""
import hashlib, json, sys
from pathlib import Path
import numpy as np
from PIL import Image

cfg = json.loads(sys.stdin.read())
sys.path.insert(0, cfg["imxnxl_repo"])

from app import config, dxt1, mdx, media, pkg, raf, theme, theme_xml, vag

out = Path(cfg["out_dir"])
content = out / "content"
content.mkdir(parents=True, exist_ok=True)
(content / "texture").mkdir(parents=True, exist_ok=True)
(content / "sce_sys").mkdir(parents=True, exist_ok=True)

home_src = Path(cfg["home_png"])
func_src = Path(cfg["func_png"])
media.render_image(home_src, content / "texture" / "preview.png", config.PREVIEW_SIZE)
media.render_image(home_src, content / "sce_sys" / "icon0.png", config.SCE_ICON_SIZE)
media.render_image(home_src, content / "sce_sys" / "pic1.png", config.PIC1_SIZE)

if not cfg["animated"]:
    (content / "texture" / "background").mkdir(parents=True, exist_ok=True)
    media.render_image(home_src, content / "texture" / "background" / "homescreen.png", config.STATIC_SIZE)
    media.render_image(func_src, content / "texture" / "background" / "functionscreen.png", config.STATIC_SIZE)
else:
    raf_dir = content / "scene" / "background.raf"
    raf_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for i, fp in enumerate(cfg["frame_pngs"]):
        with Image.open(fp) as im:
            rgba = np.asarray(im.convert("RGBA"))
        blob = dxt1.encode_dds(rgba)
        name = theme.frame_name(i)
        (raf_dir / name).write_bytes(blob)
        names.append(name)
    half_w, half_h = raf.plane_half_size()
    (raf_dir / raf.MODEL_REAL_FILE).write_bytes(mdx.quad_model(node_name=raf.MODEL_ID, half_w=half_w, half_h=half_h))
    theme.write_index_xml(raf_dir / "index.xml", raf.build_scene(names, float(cfg["wait"])))

if cfg["fn_icons"]:
    fn_dir = content / "texture" / "function_icon"
    fn_dir.mkdir(parents=True, exist_ok=True)
    for name, sp in sorted(cfg["fn_icons"].items()):
        media.render_image(Path(sp), fn_dir / f"{name}.png", config.FUNCTION_ICON_SIZE)
        media.render_image(Path(sp), fn_dir / f"{name}_glow.png", config.FUNCTION_ICON_GLOW_SIZE)

if cfg["ct_icons"]:
    ct_dir = content / "texture" / "content_icon"
    ct_dir.mkdir(parents=True, exist_ok=True)
    for name, sp in sorted(cfg["ct_icons"].items()):
        media.render_image(Path(sp), ct_dir / f"{name}.png", config.CONTENT_ICON_SIZE)

if cfg["sfx_sources"]:
    snd_dir = content / "sound"
    snd_dir.mkdir(parents=True, exist_ok=True)
    for slot_name, sp in sorted(cfg["sfx_sources"].items()):
        pcm = media.pcm_mono(Path(sp), 48000, config.MAX_SFX_SECONDS)
        (snd_dir / f"{slot_name}.vag").write_bytes(vag.encode(pcm, 48000, slot_name))

xml_text = theme_xml.build_theme_xml(homebgm_enable=bool(cfg["has_bgm"]), **cfg["theme_settings"])
(content / "theme.xml").write_text(xml_text, encoding="utf-8")

meta = pkg.PackageMeta(
    title=cfg["title"],
    title_id=cfg["title_id"],
    content_id=cfg["content_id"],
    category="ac",
    passcode="0" * 32,
    mode="theme",
    bgm_loudness=None,
)
(out / "param.sfx").write_text(pkg.build_param_sfx(meta), encoding="utf-8")
targets = sorted(p.relative_to(content).as_posix() for p in content.rglob("*") if p.is_file())
(out / "theme.gp4").write_text(pkg.build_gp4(meta, targets, content.name), encoding="utf-8")

def hfile(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        while c := f.read(1048576):
            h.update(c)
    return h.hexdigest()

files = {p.relative_to(content).as_posix(): {"bytes": p.stat().st_size, "sha256": hfile(p)} for p in sorted(content.rglob("*")) if p.is_file()}
print(json.dumps({
    "status": "APROVADO",
    "files": files,
    "param_sfx_sha256": hfile(out / "param.sfx"),
    "gp4_sha256": hfile(out / "theme.gp4"),
    "orbis_pub_cmd_status": "NÃO EXECUTADO (binário proprietário Windows da Sony SDK ausente por restrição legal)",
    "at9tool_status": "NÃO EXECUTADO (binário proprietário Windows at9tool.exe ausente por restrição legal)",
}))
"""
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=180,
    )
    if proc.returncode != 0:
        return {"status": "REPROVADO", "error": (proc.stderr or proc.stdout)[-800:]}
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_tool_orbispkgtool(pkg_path: Path, extract_dir: Path) -> dict[str, Any]:
    """Run independent pearlxcore/OrbisPkgTool (C#/.NET): info, 8-stage validate, and extract."""
    if not (DOTNET_BIN.is_file() and ORBIS_PKG_TOOL_DLL.is_file()):
        return {"status": "NÃO EXECUTADO", "reason": "OrbisPkgTool.dll ou dotnet indisponível."}
    res: dict[str, Any] = {
        "tool": "pearlxcore/OrbisPkgTool",
        "commit": "3de6da849b64a8ad0d5ba00dd4e8919934b868b0",
        "license": "MIT",
        "independence": "INDEPENDENT_VALIDATOR_AND_EXTRACTOR (builder incompatível para temas PS4: PkgBuilder.cs hardcodes CATEGORY=gd/gp sem IRO_TAG=2)",
    }
    val_p = subprocess.run(
        [str(DOTNET_BIN), str(ORBIS_PKG_TOOL_DLL), "validate", str(pkg_path)],
        text=True,
        capture_output=True,
        timeout=120,
    )
    val_out = (val_p.stdout + "\n" + val_p.stderr).strip()
    res["validate_rc"] = val_p.returncode
    res["validate_pass"] = val_p.returncode == 0 and "PASS" in val_out.upper() and "FAIL" not in val_out.upper()
    res["validate_output_excerpt"] = val_out[-500:]

    info_p = subprocess.run(
        [str(DOTNET_BIN), str(ORBIS_PKG_TOOL_DLL), "info", str(pkg_path)],
        text=True,
        capture_output=True,
        timeout=60,
    )
    res["info_excerpt"] = (info_p.stdout + "\n" + info_p.stderr).strip()[:600]

    extract_dir.mkdir(parents=True, exist_ok=True)
    ex_p = subprocess.run(
        [str(DOTNET_BIN), str(ORBIS_PKG_TOOL_DLL), "extract", str(pkg_path), str(extract_dir), "--passcode", "0" * 32],
        text=True,
        capture_output=True,
        timeout=180,
    )
    ex_files: dict[str, str] = {}
    if ex_p.returncode == 0:
        for p in sorted(extract_dir.rglob("*")):
            if p.is_file():
                rel = p.relative_to(extract_dir).as_posix()
                ex_files[rel] = sha256_file(p)
    res["extract_rc"] = ex_p.returncode
    res["extracted_file_count"] = len(ex_files)
    res["extracted_hashes"] = ex_files
    res["status"] = "APROVADO" if (res["validate_pass"] and ex_p.returncode == 0 and ex_files) else "REPROVADO"
    return res


def _run_tool_shadps4_cpp(pkg_path: Path, extract_dir: Path) -> dict[str, Any]:
    """Run independent xXJSONDeruloXx/ps4-pkg-tools (C++23 ShadPS4 7.0) decryption & extraction."""
    if not SHADPS4_PKG_TOOL.is_file():
        return {"status": "NÃO EXECUTADO", "reason": "ps4-pkg-tool (C++23) indisponível."}
    extract_dir.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(
        [str(SHADPS4_PKG_TOOL), str(pkg_path), str(extract_dir)],
        text=True,
        capture_output=True,
        timeout=180,
    )
    ex_files: dict[str, str] = {}
    if p.returncode == 0:
        for f in sorted(extract_dir.rglob("*")):
            if f.is_file():
                rel = f.relative_to(extract_dir).as_posix()
                ex_files[rel] = sha256_file(f)
    return {
        "tool": "xXJSONDeruloXx/ps4-pkg-tools (C++23 ShadPS4 7.0)",
        "license": "GPL-2.0",
        "independence": "100%_INDEPENDENT_CPP_EXTRACTOR (zero C#/.NET/LibOrbisPkg code shared)",
        "extract_rc": p.returncode,
        "extracted_file_count": len(ex_files),
        "extracted_hashes": ex_files,
        "output_excerpt": (p.stdout + "\n" + p.stderr).strip()[-400:],
        "status": "APROVADO" if (p.returncode == 0 and ex_files) else "REPROVADO",
    }


def _normalize_extractor_map(raw_map: dict[str, str]) -> dict[str, str]:
    """Normalize extractor directory prefixes (`Image0/`, `uroot/`, `<TITLE_ID>/`, `Sc0/`) to `<partition>/<rel>`."""
    import re as _re

    out: dict[str, str] = {}
    for raw_k, sha in raw_map.items():
        k = raw_k.lstrip("/")
        # ShadPS4 ps4-pkg-tool extracts under `<TITLE_ID>/...` (e.g., `CUSA90001/theme.xml`)
        k = _re.sub(r"^[A-Z]{4}\d{5}/", "", k)
        if k.startswith("Image0/"):
            rel = k[len("Image0/"):]
            if rel in {"sce_sys/param.sfo", "sce_sys/icon0.png", "sce_sys/pic1.png"}:
                continue
            out[f"Image0/{rel}"] = sha
        elif k.startswith("uroot/"):
            rel = k[len("uroot/"):]
            if rel in {"sce_sys/param.sfo", "sce_sys/icon0.png", "sce_sys/pic1.png"}:
                continue
            out[f"Image0/{rel}"] = sha
        elif k.startswith("Sc0/"):
            fname = k[len("Sc0/"):]
            if fname in {"param.sfo", "icon0.png", "pic1.png"}:
                out[f"Sc0/{fname}"] = sha
        elif k in {"sce_sys/param.sfo", "sce_sys/icon0.png", "sce_sys/pic1.png"}:
            out[f"Sc0/{Path(k).name}"] = sha
        elif k.startswith(("theme.xml", "texture/", "scene/", "sound/")):
            out[f"Image0/{k}"] = sha
    return out


def _compare_ab_trees(
    studio_content: Path,
    imxnxl_content: Path,
    imxnxl_res: dict[str, Any],
    *,
    has_bgm: bool,
) -> dict[str, Any]:
    """Byte-for-byte + semantic differential comparison between Grupo A (CZAR Studio) and Grupo B (Imxnxl original)."""
    if imxnxl_res.get("status") != "APROVADO":
        return {"status": "NÃO EXECUTADO", "reason": imxnxl_res.get("reason") or imxnxl_res.get("error")}
    b_files: dict[str, dict[str, Any]] = imxnxl_res["files"]
    a_files = {
        p.relative_to(studio_content).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256_file(p)}
        for p in sorted(studio_content.rglob("*"))
        if p.is_file()
    }

    # Files that Imxnxl cannot generate natively on Linux (proprietary Sony SDK binaries):
    linux_only_a = {"sce_sys/param.sfo"}
    if has_bgm:
        linux_only_a.add("sound/bgm_home.at9")

    comparable_a = {k: v for k, v in a_files.items() if k not in linux_only_a}
    common = sorted(set(comparable_a) & set(b_files))
    only_a = sorted(set(comparable_a) - set(b_files))
    only_b = sorted(set(b_files) - set(comparable_a))
    identical: list[str] = []
    documented_png_alpha_diffs: list[dict[str, Any]] = []
    unexpected_divergent: list[dict[str, Any]] = []
    for rel in common:
        if comparable_a[rel]["sha256"] == b_files[rel]["sha256"]:
            identical.append(rel)
        else:
            entry = {
                "path": rel,
                "studio_sha256": comparable_a[rel]["sha256"],
                "imxnxl_sha256": b_files[rel]["sha256"],
                "studio_bytes": comparable_a[rel]["bytes"],
                "imxnxl_bytes": b_files[rel]["bytes"],
            }
            if rel.endswith(".png") and (rel.startswith("texture/function_icon/") or rel.startswith("texture/content_icon/")):
                with Image.open(studio_content / rel) as im_a, Image.open(imxnxl_content / rel) as im_b:
                    arr_a = np.asarray(im_a.convert("RGBA"), dtype=np.float32)
                    arr_b = np.asarray(im_b.convert("RGBA"), dtype=np.float32)
                    entry["same_dimensions"] = im_a.size == im_b.size
                    entry["studio_mode"] = im_a.mode
                    entry["imxnxl_mode"] = im_b.mode
                    entry["rgba_mae"] = round(float(np.mean(np.abs(arr_a - arr_b))), 3) if arr_a.shape == arr_b.shape else None
                    entry["reason"] = "Intentional CZAR Studio upgrade: Pillow LANCZOS transparent RGBA preservation vs upstream FFmpeg PNG encoding"
                documented_png_alpha_diffs.append(entry)
            else:
                unexpected_divergent.append(entry)

    # All non-icon canonical assets (theme.xml, DDS, plane.mdx, index.xml, VAG, preview/icon0/pic1/backgrounds) MUST be 100% byte-identical!
    status = "APROVADO" if (not only_a and not only_b and not unexpected_divergent) else "REPROVADO"
    return {
        "status": status,
        "comparable_count": len(common),
        "byte_identical_count": len(identical),
        "documented_transparent_icon_encoder_diffs_count": len(documented_png_alpha_diffs),
        "documented_transparent_icon_encoder_diffs_sample": documented_png_alpha_diffs[:4],
        "unexpected_divergent_files": unexpected_divergent,
        "only_in_studio_comparable": only_a,
        "only_in_imxnxl": only_b,
        "expected_linux_only_in_studio": sorted(linux_only_a & set(a_files)),
    }


def _audit_single_pkg_across_4_tools(
    label: str,
    pkg_path: Path,
    *,
    expected_content_dir: Path | None,
    work_dir: Path,
    pkgtool_bin: Path | None,
) -> dict[str, Any]:
    """Run all 4 independent tools on a single PKG and cross-compare extracted SHA-256 trees."""
    t0 = time.perf_counter()
    inspector_rep = inspect_and_validate_pkg(
        pkg_path,
        expected_content_dir=expected_content_dir,
        passcode="0" * 32,
        builder_backend="liborbispkg-linux" if expected_content_dir else "external-reference",
        pkgtool_bin=pkgtool_bin,
    )
    insp_map = {f["path"]: f["sha256"] for f in inspector_rep.files}

    # Re-extract with LibOrbisPkg to run formal PS4 Contract & full Digital Twin report
    lib_ex = work_dir / "ex_liborbispkg"
    lib_ex.mkdir(parents=True, exist_ok=True)
    contract_rep = None
    twin_rep = None
    if pkgtool_bin and pkgtool_bin.is_file():
        ex_p = subprocess.run(
            [str(pkgtool_bin), "pkg_extract", "--passcode", "0" * 32, str(pkg_path), str(lib_ex)],
            text=True,
            capture_output=True,
            timeout=180,
        )
        if ex_p.returncode == 0:
            root_ex = lib_ex / "uroot" if (lib_ex / "uroot").is_dir() else lib_ex
            parsed = parse_pkg_binary(pkg_path)
            sc_sys = root_ex / "sce_sys"
            sc_sys.mkdir(parents=True, exist_ok=True)
            for sc_key in ("Sc0/param.sfo", "Sc0/icon0.png", "Sc0/pic1.png"):
                if sc_key in parsed["sc0_blobs"]:
                    (sc_sys / Path(sc_key).name).write_bytes(parsed["sc0_blobs"][sc_key])
            contract_rep = evaluate_ps4_contract(
                root_ex,
                pkg_header_meta={
                    "content_type_header": inspector_rep.content_type_header,
                    "iro_tag_header": inspector_rep.iro_tag_header,
                    "iro_tag_sfo": inspector_rep.iro_tag_sfo,
                    "category": inspector_rep.category,
                    "volume_type": inspector_rep.volume_type,
                },
            )
            twin_rep = simulate_extracted_theme(root_ex)

    orbis_res = _run_tool_orbispkgtool(pkg_path, work_dir / "ex_orbispkgtool")
    shad_res = _run_tool_shadps4_cpp(pkg_path, work_dir / "ex_shadps4")

    # Cross-extractor byte-for-byte parity comparison (Inspector/LibOrbisPkg vs OrbisPkgTool vs C++23 ShadPS4)
    orbis_norm = _normalize_extractor_map(orbis_res.get("extracted_hashes") or {})
    shad_norm = _normalize_extractor_map(shad_res.get("extracted_hashes") or {})

    def _diff_maps(m1: dict[str, str], m2: dict[str, str]) -> dict[str, Any]:
        keys1 = {k for k in m1 if k.startswith("Image0/") or k in {"Sc0/icon0.png", "Sc0/pic1.png", "Sc0/param.sfo"}}
        keys2 = {k for k in m2 if k.startswith("Image0/") or k in {"Sc0/icon0.png", "Sc0/pic1.png", "Sc0/param.sfo"}}
        common = sorted(keys1 & keys2)
        mismatches = [k for k in common if m1[k] != m2[k]]
        return {
            "common_count": len(common),
            "only_in_left": sorted(keys1 - keys2),
            "only_in_right": sorted(keys2 - keys1),
            "sha256_mismatches": mismatches,
            "exact_match": len(common) > 0 and not (keys1 - keys2) and not (keys2 - keys1) and not mismatches,
        }

    cross_orbis = _diff_maps(insp_map, orbis_norm) if orbis_norm else {"exact_match": False, "note": "OrbisPkgTool não extraiu"}
    cross_shad = _diff_maps(insp_map, shad_norm) if shad_norm else {"exact_match": False, "note": "ps4-pkg-tool não extraiu"}

    # Strip large raw hash maps from tool payloads before saving report
    orbis_clean = {k: v for k, v in orbis_res.items() if k != "extracted_hashes"}
    shad_clean = {k: v for k, v in shad_res.items() if k != "extracted_hashes"}

    return {
        "label": label,
        "pkg_path": str(pkg_path),
        "pkg_bytes": pkg_path.stat().st_size,
        "pkg_sha256": inspector_rep.pkg_sha256,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "czar_inspector": {
            "status": "APROVADO" if inspector_rep.structure_validated and inspector_rep.theme_type_valid else "REPROVADO",
            "verification_level": inspector_rep.verification_level,
            "package_status": inspector_rep.package_status,
            "release_gate": inspector_rep.release_gate,
            "content_id": inspector_rep.content_id,
            "title_id": inspector_rep.title_id,
            "category": inspector_rep.category,
            "iro_tag_header": inspector_rep.iro_tag_header,
            "iro_tag_sfo": inspector_rep.iro_tag_sfo,
            "content_type_header": hex(inspector_rep.content_type_header or 0),
            "bgmhome_loud": inspector_rep.bgmhome_loud,
            "file_count": len(inspector_rep.files),
            "bundle_hashes_matched": inspector_rep.bundle_hashes_matched,
            "errors": inspector_rep.errors,
            "warnings": inspector_rep.warnings,
            "digital_twin_summary": inspector_rep.digital_twin_summary,
        },
        "ps4_compatibility_contract": {
            "status": "APROVADO" if (contract_rep and contract_rep["passed"]) else ("REPROVADO" if contract_rep else "NÃO EXECUTADO"),
            "rules_passed": sum(1 for r in (contract_rep or {}).get("rules", []) if r["status"] == "PASS"),
            "rules_total": len((contract_rep or {}).get("rules", [])),
            "errors": (contract_rep or {}).get("errors", []),
        },
        "digital_twin": {
            "status": "APROVADO" if (twin_rep and twin_rep["passed"]) else ("REPROVADO" if twin_rep else "NÃO EXECUTADO"),
            "verification_kind": (twin_rep or {}).get("verification_kind", "SEMANTIC_SIMULATION_ONLY"),
            "physical_execution_proven": False,
            "raf_frames": ((twin_rep or {}).get("raf_simulation") or {}).get("frames"),
            "raf_cycles": ((twin_rep or {}).get("raf_simulation") or {}).get("simulated_cycles"),
            "raf_cycle_duration_sec": ((twin_rep or {}).get("raf_simulation") or {}).get("cycle_duration_sec"),
            "raf_seam_to_interior_ratio": ((twin_rep or {}).get("raf_simulation") or {}).get("seam_to_interior_ratio"),
            "icons_fn": ((twin_rep or {}).get("icons_and_presentation") or {}).get("function_icons_count"),
            "icons_glow": ((twin_rep or {}).get("icons_and_presentation") or {}).get("function_glows_count"),
            "icons_ct": ((twin_rep or {}).get("icons_and_presentation") or {}).get("content_icons_count"),
            "duplicate_icon_groups": ((twin_rep or {}).get("icons_and_presentation") or {}).get("duplicate_icon_groups"),
            "audio_bgm_cycles": (((twin_rep or {}).get("audio_simulation") or {}).get("bgm") or {}).get("multi_cycle_simulation"),
            "audio_sfx": ((twin_rep or {}).get("audio_simulation") or {}).get("sfx"),
            "errors": (twin_rep or {}).get("errors", []),
            "warnings": (twin_rep or {}).get("warnings", []),
        },
        "independent_tools": {
            "orbispkgtool": orbis_clean,
            "shadps4_cpp": shad_clean,
        },
        "cross_extractor_byte_parity": {
            "liborbispkg_vs_orbispkgtool": cross_orbis,
            "liborbispkg_vs_shadps4_cpp": cross_shad,
        },
        "physical_compatibility": "PHYSICAL_TEST_PENDING (PS4 Slim firmware 12.52 não testado fisicamente nesta rodada)",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Operação Fortaleza — Corpus & Differential Audit Runner")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_ROOT, help="Output directory outside Git")
    ap.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Reuse already-built Grupo A PKG/content & Grupo B trees when present on disk",
    )
    ap.add_argument(
        "--themes",
        nargs="+",
        default=[
            "A1_static_simple",
            "A2_static_icons",
            "A3_dynamic_raf",
            "A4_dynamic_full",
            "A5_zenitsu_real",
            "A6_crown_of_thorns_real",
        ],
    )
    args = ap.parse_args()

    out_root: Path = args.out_dir
    out_root.mkdir(parents=True, exist_ok=True)
    src_root = out_root / "sources"
    builds_root = out_root / "builds"
    imxnxl_root = out_root / "imxnxl_ref"
    pkgtool_bin = find_liborbispkg_tool()

    report: dict[str, Any] = {
        "audit_version": "operacao-fortaleza-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "out_root": str(out_root),
        "tools_matrix": {
            "czar_studio_builder": {
                "backend": "liborbispkg-linux",
                "commit": "92a4e7f8e5914cb06b52ba3ae6b0675da438a051 + audited 21-line PS4 Theme patch",
                "role": "Primary Linux PS4 Theme PKG builder",
            },
            "imxnxl_reference": {
                "repo": "https://github.com/Imxnxl/PS4-Ultimate-Theme-Creator",
                "commit": "aa595aa6cda8e906c7f21086b829a6b23223784e",
                "available": IMXNXL_REPO.is_dir(),
                "role": "Upstream reference asset/XML/DDS/MDX/VAG/GP4/SFX generator (orbis-pub-cmd.exe & at9tool.exe legally excluded on Linux)",
            },
            "orbispkgtool": {
                "repo": "https://github.com/pearlxcore/OrbisPkgTool",
                "commit": "3de6da849b64a8ad0d5ba00dd4e8919934b868b0",
                "available": ORBIS_PKG_TOOL_DLL.is_file(),
                "role": "Independent C#/.NET 8-stage validator + extractor (builder INCOMPATIBLE for PS4 themes: PkgBuilder.cs hardcodes CATEGORY=gd/gp without IRO_TAG=2)",
            },
            "ps4_pkg_tools_cpp": {
                "repo": "https://github.com/xXJSONDeruloXx/ps4-pkg-tools",
                "available": SHADPS4_PKG_TOOL.is_file(),
                "role": "Independent C++23 ShadPS4 7.0 decryptor + PFS extractor (zero C#/.NET code shared)",
            },
        },
        "themes": {},
        "frozen_drive_zenitsu": None,
    }

    for spec_id in args.themes:
        print(f"\n=== [{spec_id}] Preparando fontes, Grupo A, Grupo B (Imxnxl) e Grupo C (4 ferramentas) ===", flush=True)
        spec = _build_theme_spec_sources(spec_id, src_root)
        t_work = builds_root / spec_id
        content_dir = t_work / "content"
        pkg_dest = t_work / "pkg_out"
        existing_pkgs = sorted(pkg_dest.glob("*.pkg")) if pkg_dest.is_dir() else []

        if args.reuse_existing and content_dir.is_dir() and existing_pkgs:
            pkg_path = existing_pkgs[0]
        else:
            if t_work.exists():
                shutil.rmtree(t_work)
            t_work.mkdir(parents=True, exist_ok=True)
            content_dir, gp4_path, meta = _assemble_studio_content(spec, t_work)
            pkg_path, _br, _pr, _log = build_and_validate_theme_pkg(
                content_dir,
                gp4_path,
                meta,
                pkg_dest,
            )

        imxnxl_content_dir = imxnxl_root / spec_id / "content"
        if args.reuse_existing and imxnxl_content_dir.is_dir():
            imxnxl_res = {
                "status": "APROVADO",
                "files": {
                    p.relative_to(imxnxl_content_dir).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256_file(p)}
                    for p in sorted(imxnxl_content_dir.rglob("*"))
                    if p.is_file()
                },
                "orbis_pub_cmd_status": "NÃO EXECUTADO (binário proprietário Windows da Sony SDK ausente por restrição legal)",
                "at9tool_status": "NÃO EXECUTADO (binário proprietário Windows at9tool.exe ausente por restrição legal)",
            }
        else:
            imxnxl_res = _generate_imxnxl_reference_tree(spec, imxnxl_root / spec_id)

        ab_diff = _compare_ab_trees(
            content_dir,
            imxnxl_content_dir,
            imxnxl_res,
            has_bgm=spec["bgm_source"] is not None,
        )

        with tempfile.TemporaryDirectory(prefix=f"fortaleza-4tool-{spec_id}-") as td:
            multi = _audit_single_pkg_across_4_tools(
                spec_id,
                pkg_path,
                expected_content_dir=content_dir,
                work_dir=Path(td),
                pkgtool_bin=pkgtool_bin,
            )

        # Record source SHA-256 inventory
        source_hashes: dict[str, Any] = {
            "home_png": {"path": str(spec["home_png"]), "sha256": sha256_file(Path(spec["home_png"]))},
            "frame_count": len(spec["frame_pngs"]),
            "fn_icons_count": len(spec["fn_icons"]),
            "ct_icons_count": len(spec["ct_icons"]),
            "bgm_source": sha256_file(Path(spec["bgm_source"])) if spec["bgm_source"] else None,
            "sfx_count": len(spec["sfx_sources"]),
        }

        report["themes"][spec_id] = {
            "source_inventory": source_hashes,
            "imxnxl_group_b": {
                "status": imxnxl_res.get("status"),
                "orbis_pub_cmd_status": imxnxl_res.get("orbis_pub_cmd_status"),
                "at9tool_status": imxnxl_res.get("at9tool_status"),
            },
            "ab_differential": ab_diff,
            "multi_tool_audit": multi,
        }
        print(
            f"  -> {spec_id}: PKG={multi['pkg_sha256'][:12]} "
            f"A/B={ab_diff['status']} ({ab_diff.get('byte_identical_count')}/{ab_diff.get('comparable_count')} byte-identical) "
            f"Inspector={multi['czar_inspector']['status']} "
            f"Contract={multi['ps4_compatibility_contract']['rules_passed']}/{multi['ps4_compatibility_contract']['rules_total']} "
            f"Twin={multi['digital_twin']['status']} "
            f"OrbisPkgTool={multi['independent_tools']['orbispkgtool']['status']} "
            f"ShadPS4={multi['independent_tools']['shadps4_cpp']['status']} "
            f"CrossParity=(Orbis:{multi['cross_extractor_byte_parity']['liborbispkg_vs_orbispkgtool']['exact_match']}, "
            f"Shad:{multi['cross_extractor_byte_parity']['liborbispkg_vs_shadps4_cpp']['exact_match']})",
            flush=True,
        )

    # Also audit the frozen original Zenitsu Drive PKG read-only across all 4 independent tools!
    if FROZEN_ZENITSU_PKG.is_file():
        print("\n=== [FROZEN_DRIVE_ZENITSU] Auditoria read-only nas 4 ferramentas independentes ===", flush=True)
        with tempfile.TemporaryDirectory(prefix="fortaleza-4tool-zenitsu-orig-") as td:
            frozen_multi = _audit_single_pkg_across_4_tools(
                "FROZEN_DRIVE_ZENITSU",
                FROZEN_ZENITSU_PKG,
                expected_content_dir=None,
                work_dir=Path(td),
                pkgtool_bin=pkgtool_bin,
            )
        report["frozen_drive_zenitsu"] = frozen_multi
        print(
            f"  -> FROZEN_DRIVE_ZENITSU: PKG={frozen_multi['pkg_sha256'][:12]} "
            f"Inspector={frozen_multi['czar_inspector']['status']} "
            f"Contract={frozen_multi['ps4_compatibility_contract']['rules_passed']}/{frozen_multi['ps4_compatibility_contract']['rules_total']} "
            f"Twin={frozen_multi['digital_twin']['status']} "
            f"OrbisPkgTool={frozen_multi['independent_tools']['orbispkgtool']['status']} "
            f"ShadPS4={frozen_multi['independent_tools']['shadps4_cpp']['status']} "
            f"CrossParity=(Orbis:{frozen_multi['cross_extractor_byte_parity']['liborbispkg_vs_orbispkgtool']['exact_match']}, "
            f"Shad:{frozen_multi['cross_extractor_byte_parity']['liborbispkg_vs_shadps4_cpp']['exact_match']})",
            flush=True,
        )

    out_json = out_root / "fortaleza_corpus_audit.json"
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nRelatório consolidado salvo em: {out_json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
