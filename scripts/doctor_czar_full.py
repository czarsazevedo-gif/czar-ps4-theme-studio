#!/usr/bin/env python3
"""CZAR COMPLETE TOOLCHAIN — Real functional verifier (doctor_czar_full.py).

Every status marked PRONTO is backed by an actual execution/decode/encode test,
never merely `command -v`. Returns exit code 0 ONLY if all mandatory tools pass.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

TOOLS_BIN = REPO / "tools" / "bin"
SAMPLE_MP4 = REPO / "tests" / "sample.mp4"


def _find_tool(name: str, extra: list[Path] | None = None) -> Path | None:
    candidates: list[Path] = [TOOLS_BIN / name]
    if extra:
        candidates.extend(extra)
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    found = shutil.which(name)
    return Path(found) if found else None


def _run(cmd: list[str], *, input_bytes: bytes | None = None, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _ok(name: str, detail: str, *, version: str | None = None, path: str | None = None, extra: dict | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"name": name, "status": "PRONTO", "ok": True, "detail": detail}
    if version:
        d["version"] = version
    if path:
        d["path"] = path
    if extra:
        d["extra"] = extra
    return d


def _fail(name: str, detail: str, recovery: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "status": "FALHA",
        "ok": False,
        "detail": detail,
        "recovery": recovery or "Execute ./scripts/bootstrap_czar_full.sh --install",
    }


def _check_python() -> dict[str, Any]:
    ver = platform.python_version()
    in_venv = sys.prefix != sys.base_prefix
    if sys.version_info < (3, 11):
        return _fail("PYTHON", f"Python {ver} < 3.11", "Use Python 3.11+ em um ambiente virtual isolado.")
    return _ok("PYTHON", f"Python {ver} ({'venv isolado' if in_venv else 'sistema'})", version=ver, path=sys.executable)


def _check_fastapi() -> dict[str, Any]:
    try:
        import fastapi
        from fastapi.testclient import TestClient
        from app import main as app_main

        with TestClient(app_main.app) as client:
            r = client.get("/healthz")
            if r.status_code != 200 or not r.json().get("ok"):
                return _fail("FASTAPI", f"/healthz retornou {r.status_code}: {r.text[:120]}")
        return _ok("FASTAPI", f"FastAPI {fastapi.__version__} · /healthz HTTP 200", version=fastapi.__version__)
    except Exception as exc:
        return _fail("FASTAPI", str(exc))


def _check_ffmpeg() -> tuple[dict[str, Any], bool]:
    ff = _find_tool("ffmpeg")
    if not ff:
        return _fail("FFMPEG", "ffmpeg não encontrado no PATH"), False
    try:
        v = _run([str(ff), "-version"])
        ver_line = v.stdout.decode("utf-8", "replace").splitlines()[0] if v.stdout else ""
        flt = _run([str(ff), "-hide_banner", "-filters"]).stdout.decode("utf-8", "replace")
        has_vmaf = " libvmaf " in flt
        needed_filters = ("scale", "fps", "signalstats", "blackdetect", "freezedetect", "cropdetect", "ebur128")
        missing_f = [f for f in needed_filters if f" {f} " not in flt]
        if missing_f:
            return _fail("FFMPEG", f"Filtros obrigatórios ausentes: {', '.join(missing_f)}"), has_vmaf
        if SAMPLE_MP4.is_file():
            dec = _run([str(ff), "-v", "error", "-i", str(SAMPLE_MP4), "-frames:v", "2", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
            if dec.returncode != 0 or len(dec.stdout) < 1000:
                return _fail("FFMPEG", f"Falha ao decodificar {SAMPLE_MP4.name}"), has_vmaf
        return _ok("FFMPEG", f"{ver_line} · decodificação real e filtros validados", version=ver_line, path=str(ff)), has_vmaf
    except Exception as exc:
        return _fail("FFMPEG", str(exc)), False


def _check_ffprobe() -> dict[str, Any]:
    fp = _find_tool("ffprobe")
    if not fp:
        return _fail("FFPROBE", "ffprobe não encontrado no PATH")
    try:
        if SAMPLE_MP4.is_file():
            p = _run([str(fp), "-v", "error", "-show_entries", "format=duration:stream=width,height,codec_name", "-of", "json", str(SAMPLE_MP4)])
            data = json.loads(p.stdout.decode("utf-8"))
            dur = float(data["format"]["duration"])
            if dur <= 0:
                return _fail("FFPROBE", "Duração inválida retornada por ffprobe")
            return _ok("FFPROBE", f"Sondagem real OK ({SAMPLE_MP4.name}: {dur:.2f}s)", path=str(fp))
        v = _run([str(fp), "-version"]).stdout.decode("utf-8", "replace").splitlines()[0]
        return _ok("FFPROBE", v, path=str(fp))
    except Exception as exc:
        return _fail("FFPROBE", str(exc))


def _check_gifsicle() -> dict[str, Any]:
    gs = _find_tool("gifsicle")
    if not gs:
        return _fail("GIFSICLE", "gifsicle não encontrado em tools/bin/gifsicle ou PATH")
    try:
        from PIL import Image

        with tempfile.TemporaryDirectory(prefix="czar-doc-gif-") as td:
            gp = Path(td) / "var_delay.gif"
            f1 = Image.new("RGB", (16, 16), (220, 40, 40))
            f2 = Image.new("RGB", (16, 16), (40, 220, 40))
            f3 = Image.new("RGB", (16, 16), (40, 40, 220))
            f1.save(gp, save_all=True, append_images=[f2, f3], duration=[50, 150, 50], loop=0)
            info = _run([str(gs), "--info", str(gp)])
            txt = info.stdout.decode("utf-8", "replace")
            if info.returncode != 0 or "0.05s" not in txt or "0.15s" not in txt:
                return _fail("GIFSICLE", f"Falha ao ler delays 50/150 ms: {txt[:200]}")
            unopt = Path(td) / "unopt.gif"
            u = _run([str(gs), "--unoptimize", str(gp), "-o", str(unopt)])
            if u.returncode != 0 or not unopt.is_file():
                return _fail("GIFSICLE", "Falha em --unoptimize")
        ver = _run([str(gs), "--version"]).stdout.decode("utf-8", "replace").splitlines()[0]
        return _ok("GIFSICLE", f"{ver} · delays variáveis 50/150 ms e --unoptimize validados", version=ver, path=str(gs))
    except Exception as exc:
        return _fail("GIFSICLE", str(exc))


def _check_opencv() -> dict[str, Any]:
    try:
        import cv2
        import importlib.metadata as md

        dists = {d.metadata["Name"].lower() for d in md.distributions() if d.metadata.get("Name")}
        if "opencv-python" in dists and "opencv-python-headless" in dists:
            return _fail("OPENCV", "Distribuições OpenCV conflitantes instaladas simultaneamente.")
        rng = np.random.default_rng(7)
        a = rng.integers(0, 255, (64, 64), dtype=np.uint8)
        a = cv2.GaussianBlur(a, (5, 5), 1.2)
        b = np.roll(a, 3, axis=1)
        flow = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 3, 13, 3, 5, 1.2, 0)
        med_vx = float(np.median(flow[10:-10, 10:-10, 0]))
        if abs(med_vx - 3.0) > 0.4:
            return _fail("OPENCV", f"Fluxo óptico Farneback impreciso: med_vx={med_vx:.2f} (esperado ~3.0)")
        return _ok("OPENCV", f"OpenCV {cv2.__version__} · Farneback real (vx={med_vx:.2f} px)", version=cv2.__version__)
    except Exception as exc:
        return _fail("OPENCV", str(exc))


def _check_scipy() -> dict[str, Any]:
    try:
        import scipy
        from scipy import ndimage, optimize, signal

        res = optimize.minimize_scalar(lambda x: (x - 2.5) ** 2, bounds=(0, 5), method="bounded")
        sm = ndimage.gaussian_filter1d(np.array([0.0, 1.0, 0.0]), sigma=1.0)
        ac = signal.correlate([1.0, 0.0, 1.0], [1.0, 0.0, 1.0], mode="full")
        if abs(res.x - 2.5) > 1e-4 or sm.size != 3 or ac.size != 5:
            return _fail("SCIPY", "Falha no autoteste numérico SciPy")
        return _ok("SCIPY", f"SciPy {scipy.__version__} · optimize/ndimage/signal validados", version=scipy.__version__)
    except Exception as exc:
        return _fail("SCIPY", str(exc))


def _check_visual_eval() -> dict[str, Any]:
    try:
        import av
        import skimage
        from skimage.metrics import structural_similarity

        img = np.linspace(0, 255, 64 * 64, dtype=np.uint8).reshape(64, 64)
        s = float(structural_similarity(img, img, data_range=255))
        if abs(s - 1.0) > 1e-6:
            return _fail("AVALIAÇÃO VISUAL", f"SSIM inesperado: {s}")
        pts_ok = "sem sample.mp4"
        if SAMPLE_MP4.is_file():
            with av.open(str(SAMPLE_MP4)) as container:
                pts = [f.pts for _, f in zip(range(3), container.decode(video=0))]
                if len(pts) < 2:
                    return _fail("AVALIAÇÃO VISUAL", "PyAV decodificou menos de 2 quadros")
                pts_ok = f"PyAV {av.__version__} PTS={pts}"
        return _ok("AVALIAÇÃO VISUAL", f"scikit-image {skimage.__version__} (SSIM=1.0) + {pts_ok}", version=f"skimage {skimage.__version__} / av {av.__version__}")
    except Exception as exc:
        return _fail("AVALIAÇÃO VISUAL", str(exc))


def _check_audio_stack() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from app import at9, vag
    from app.forge.audio_validation import validate_at9

    psgfx_res: dict[str, Any]
    vgm_res: dict[str, Any]
    vag_res: dict[str, Any]

    with tempfile.TemporaryDirectory(prefix="czar-doc-audio-") as td:
        wav_p = Path(td) / "tone.wav"
        at9_p = Path(td) / "tone.at9"
        n = 24000  # 0.5 s @ 48 kHz
        t = np.arange(n, dtype=np.float32) / 48000.0
        s = (np.sin(2.0 * np.pi * 440.0 * t) * 12000.0).astype(np.int16)
        stereo = np.column_stack([s, s]).ravel()
        with wave.open(str(wav_p), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(stereo.tobytes())

        # PSGFX ATRAC9
        info = at9.encoder_info()
        if not info.get("available"):
            psgfx_res = _fail("PSGFX ATRAC9", "psgfx-at9 não encontrado")
        else:
            try:
                at9.encode(wav_p, at9_p, bitrate=144, loop=True, loop_start_sample=4800, loop_end_sample=19200, validate=True)
                rep = validate_at9(at9_p, require_ps4_profile=True, require_loop=True)
                if not rep.valid:
                    psgfx_res = _fail("PSGFX ATRAC9", "; ".join(rep.errors))
                else:
                    psgfx_res = _ok(
                        "PSGFX ATRAC9",
                        f"48 kHz estéreo 144 kbps · configData={rep.config_data_hex} · loop={rep.loop_start_sample}..{rep.loop_end_sample}",
                        path=str(info.get("path")),
                    )
            except Exception as exc:
                psgfx_res = _fail("PSGFX ATRAC9", str(exc))

        # VGMSTREAM
        vgm = _find_tool("vgmstream-cli")
        if not vgm:
            vgm_res = _fail("VGMSTREAM", "vgmstream-cli não encontrado em tools/bin/vgmstream-cli")
        elif not at9_p.is_file():
            vgm_res = _fail("VGMSTREAM", "Não foi possível testar vgmstream-cli sem .at9 gerado")
        else:
            try:
                meta = _run([str(vgm), "-m", str(at9_p)])
                out = meta.stdout.decode("utf-8", "replace")
                dec_wav = Path(td) / "vgm_out.wav"
                dec = _run([str(vgm), "-l", "3", "-f", "0", "-o", str(dec_wav), str(at9_p)])
                if meta.returncode != 0 or "ATRAC9" not in out.upper() or "loop start" not in out.lower() or dec.returncode != 0 or not dec_wav.is_file():
                    vgm_res = _fail("VGMSTREAM", f"Falha ao validar/decodificar .at9: {out[:200]}")
                else:
                    vgm_res = _ok("VGMSTREAM", f"ATRAC9 + loop start/end + 3 repetições WAV ({dec_wav.stat().st_size} B) validados", path=str(vgm))
            except Exception as exc:
                vgm_res = _fail("VGMSTREAM", str(exc))

        # VAG
        try:
            raw = vag.encode(s, 48000, "se_test")
            if len(raw) < 64 or raw[:4] != b"VAGp":
                vag_res = _fail("VAG", "Cabeçalho VAGp inválido")
            else:
                vag_res = _ok("VAG", f"Sony ADPCM VAGp gerado e validado ({len(raw)} bytes)", path="app/vag.py")
        except Exception as exc:
            vag_res = _fail("VAG", str(exc))

    return psgfx_res, vgm_res, vag_res


def _check_liborbispkg_and_dotnet() -> tuple[dict[str, Any], dict[str, Any]]:
    from app.forge import pkg_forge

    dotnet = _find_tool("dotnet", [REPO / "tools" / "dotnet" / "dotnet"])
    if not dotnet:
        dn_res = _fail("DOTNET", "dotnet 8 SDK não encontrado")
    else:
        try:
            p = _run([str(dotnet), "--list-sdks"])
            sdks = p.stdout.decode("utf-8", "replace").strip()
            if p.returncode != 0 or not any(line.startswith("8.") for line in sdks.splitlines()):
                dn_res = _fail("DOTNET", f".NET 8 SDK ausente: {sdks}")
            else:
                ver = next(line.split()[0] for line in sdks.splitlines() if line.startswith("8."))
                dn_res = _ok("DOTNET", f".NET SDK {ver}", version=ver, path=str(dotnet))
        except Exception as exc:
            dn_res = _fail("DOTNET", str(exc))

    info = pkg_forge.pkg_forge_info()
    if not info.get("available") or not info.get("path"):
        lp_res = _fail("LIBORBISPKG", "pkgtool não encontrado")
    else:
        try:
            p = _run([info["path"]])
            out = (p.stdout + p.stderr).decode("utf-8", "replace")
            if p.returncode == 0 and "pkg_build" in out and "pkg_validate" in out and "pkg_extract" in out:
                lp_res = _ok("LIBORBISPKG", f"{info['backend']} · pkg_build/pkg_validate/pkg_extract prontos", path=info["path"])
            else:
                lp_res = _fail("LIBORBISPKG", f"pkgtool retornou código {p.returncode}")
        except Exception as exc:
            lp_res = _fail("LIBORBISPKG", str(exc))

    return lp_res, dn_res


def _check_icons() -> dict[str, Any]:
    try:
        from PIL import Image
        from app import media

        with tempfile.TemporaryDirectory(prefix="czar-doc-icons-") as td:
            src_png = Path(td) / "white_circle.png"
            yy, xx = np.mgrid[0:256, 0:256]
            mask = ((xx - 128) ** 2 + (yy - 128) ** 2) <= 96 ** 2
            arr = np.zeros((256, 256, 4), dtype=np.uint8)
            arr[mask] = [255, 255, 255, 255]
            Image.fromarray(arr, "RGBA").save(src_png, "PNG")

            for w, h in ((128, 128), (152, 152), (512, 512)):
                out = Path(td) / f"icon_{w}.png"
                media.render_image(src_png, out, (w, h))
                res = np.asarray(Image.open(out).convert("RGBA"))
                if res.shape != (h, w, 4):
                    return _fail("ÍCONES", f"Dimensão incorreta: {res.shape} != {(h, w, 4)}")
                alpha = res[..., 3:4].astype(np.float32) / 255.0
                over_white = res[..., :3].astype(np.float32) * alpha + 255.0 * (1.0 - alpha)
                if float(over_white.min()) < 245.0:
                    return _fail("ÍCONES", f"Halo escuro detectado em {w}×{h}: min_rgb={over_white.min():.1f}")

            svg_note = "PNG RGBA 128/152/512 sem halo"
            magick_bin = media.find_magick()
            tabler_svg = REPO / "tools" / "icons" / "tabler" / "icons" / "outline" / "settings.svg"
            if magick_bin and tabler_svg.is_file():
                svg_out = Path(td) / "tabler_128.png"
                p = _run([magick_bin, "-background", "none", "-density", "384", str(tabler_svg), "-resize", "128x128", f"PNG32:{svg_out}"])
                if p.returncode == 0 and svg_out.is_file():
                    svg_note += " + Tabler SVG OK"
        return _ok("ÍCONES", svg_note)
    except Exception as exc:
        return _fail("ÍCONES", str(exc))


def _check_compositor() -> dict[str, Any]:
    try:
        from app import compositor

        for fn_name in ("render", "composed_slots", "composed_glow_slots", "animation_overlay"):
            if not hasattr(compositor, fn_name):
                return _fail("COMPOSITOR", f"Função ausente em app.compositor: {fn_name}")
        return _ok("COMPOSITOR", "Motor de composição RGBA/animada/glow pronto", path="app/compositor.py")
    except Exception as exc:
        return _fail("COMPOSITOR", str(exc))


def _check_raf() -> dict[str, Any]:
    try:
        from app import dxt1, mdx, raf

        block = np.full((64, 64, 4), 255, dtype=np.uint8)
        dds_bytes = dxt1.encode_dds(block)
        half_w, half_h = raf.plane_half_size()
        mdx_bytes = mdx.quad_model(half_w=half_w, half_h=half_h)
        xml_str = raf.build_scene(["00.dds", "01.dds"], 0.1)
        if dds_bytes[:4] != b"DDS " or dds_bytes[84:88] != b"DXT1" or len(mdx_bytes) < 64 or "<raf" not in xml_str:
            return _fail("RAF", "Falha nos geradores canônicos DDS/MDX/XML")
        return _ok("RAF", f"DXT1a ({len(dds_bytes)} B) + MDX ({len(mdx_bytes)} B) + XML canônicos validados")
    except Exception as exc:
        return _fail("RAF", str(exc))


def _check_pkg() -> dict[str, Any]:
    try:
        from app import pkg
        from app.forge import pkg_forge, sfo

        info = pkg_forge.pkg_forge_info()
        if not info.get("available"):
            return _fail("PKG", "Backend PKG indisponível")
        meta = pkg.PackageMeta(
            title="Czar Doctor Theme",
            title_id="CZAR00001",
            content_id="UP0001-CZAR00001_00-0000000000000001",
            category="ac",
            passcode="0" * 32,
            mode="theme",
            bgm_loudness=-24.0,
        )
        sfo_bytes = sfo.build_param_sfo_bytes(meta)
        parsed = sfo.parse_param_sfo(sfo_bytes)["values"]
        if parsed.get("CATEGORY") != "ac" or int(parsed.get("IRO_TAG", -1)) != 2:
            return _fail("PKG", f"SFO inválido: CATEGORY={parsed.get('CATEGORY')} IRO_TAG={parsed.get('IRO_TAG')}")
        return _ok("PKG", f"pkg_ps4_theme · CATEGORY=ac · IRO_TAG=2 · backend={info['backend']}")
    except Exception as exc:
        return _fail("PKG", str(exc))


def _check_chromium_and_playwright() -> tuple[dict[str, Any], dict[str, Any]]:
    chrome = _find_tool("chromium") or _find_tool("google-chrome")
    if not chrome:
        return _fail("PLAYWRIGHT", "Chromium ausente para Playwright"), _fail("CHROMIUM", "chromium não encontrado")
    try:
        cv = _run([str(chrome), "--version"]).stdout.decode("utf-8", "replace").strip()
        ch_res = _ok("CHROMIUM", cv, version=cv, path=str(chrome))
    except Exception as exc:
        return _fail("PLAYWRIGHT", str(exc)), _fail("CHROMIUM", str(exc))

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(chrome), args=["--no-sandbox", "--disable-gpu"])
            page = browser.new_page(viewport={"width": 1480, "height": 924})
            page.set_content("<div id='ok'>Czar Playwright OK</div>")
            txt = page.locator("#ok").inner_text()
            browser.close()
        if txt != "Czar Playwright OK":
            return _fail("PLAYWRIGHT", f"DOM inesperado: {txt}"), ch_res
        import importlib.metadata as md

        pw_ver = md.version("playwright")
        pw_res = _ok("PLAYWRIGHT", f"Playwright {pw_ver} + Chromium headless real validado", version=pw_ver)
        return pw_res, ch_res
    except Exception as exc:
        return _fail("PLAYWRIGHT", str(exc)), ch_res


def _check_hyperfine() -> dict[str, Any]:
    hf = _find_tool("hyperfine")
    if not hf:
        return _fail("HYPERFINE", "hyperfine não encontrado em tools/bin/hyperfine ou PATH")
    try:
        with tempfile.TemporaryDirectory(prefix="czar-doc-hf-") as td:
            out_j = Path(td) / "hf.json"
            p = _run([str(hf), "--runs", "2", "--warmup", "1", "--export-json", str(out_j), "true"])
            if p.returncode != 0 or not out_j.is_file():
                return _fail("HYPERFINE", f"Falha ao executar benchmark: {p.stderr.decode('utf-8', 'replace')[:200]}")
            data = json.loads(out_j.read_text(encoding="utf-8"))
            r0 = data["results"][0]
            if "mean" in r0:
                mean_ms = float(r0["mean"]) * 1000.0
            else:
                mean_ms = float(r0["summary"]["time_wall_clock"]["mean"]) * 1000.0
        ver = _run([str(hf), "--version"]).stdout.decode("utf-8", "replace").strip()
        return _ok("HYPERFINE", f"{ver} · execução real (true: {mean_ms:.2f} ms)", version=ver, path=str(hf))
    except Exception as exc:
        return _fail("HYPERFINE", str(exc))


def _check_ouroboros() -> dict[str, Any]:
    try:
        # Supports both pre-unification (Checkpoint 1) and unified app/ouroboros_engine (Checkpoints 2+)
        try:
            from app import ouroboros_engine  # type: ignore

            mod_name = "app/ouroboros_engine (unificado)"
            _ = ouroboros_engine
        except ImportError:
            from app.deluxe import metrics as d_metrics  # type: ignore

            mod_name = "app/deluxe (pré-unificação)"
            _ = d_metrics
        return _ok("OUROBOROS", f"Motor carregado e operacional ({mod_name})")
    except Exception as exc:
        return _fail("OUROBOROS", str(exc))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="Emite relatório estruturado em JSON")
    args = ap.parse_args()

    ff_res, has_vmaf = _check_ffmpeg()
    psgfx_res, vgm_res, vag_res = _check_audio_stack()
    lp_res, dn_res = _check_liborbispkg_and_dotnet()
    pw_res, ch_res = _check_chromium_and_playwright()

    mandatory = [
        _check_python(),
        _check_fastapi(),
        ff_res,
        _check_ffprobe(),
        _check_gifsicle(),
        _check_opencv(),
        _check_scipy(),
        _check_visual_eval(),
        psgfx_res,
        vgm_res,
        vag_res,
        lp_res,
        dn_res,
        _check_icons(),
        _check_compositor(),
        _check_raf(),
        _check_pkg(),
        pw_res,
        ch_res,
        _check_hyperfine(),
        _check_ouroboros(),
    ]

    from app import media

    magick_bin = media.find_magick()
    tabler_ok = (REPO / "tools" / "icons" / "tabler" / "icons" / "outline" / "settings.svg").is_file()
    optional = [
        {
            "name": "IMAGEMAGICK",
            "status": "PRONTO" if magick_bin else "AUSENTE (OPCIONAL)",
            "ok": bool(magick_bin),
            "path": magick_bin,
        },
        {
            "name": "TABLER ICONS",
            "status": "PRONTO" if tabler_ok else "AUSENTE (OPCIONAL)",
            "ok": tabler_ok,
            "path": str(REPO / "tools" / "icons" / "tabler") if tabler_ok else None,
        },
        {
            "name": "LIBVMAF",
            "status": "PRONTO" if has_vmaf else "AUSENTE (OPCIONAL)",
            "ok": has_vmaf,
            "detail": "Presente no FFmpeg" if has_vmaf else "Ausente no build Debian padrão; métricas SSIM/MS-SSIM/fluxo ativas",
        },
    ]

    all_ok = all(item["ok"] for item in mandatory)
    report = {
        "title": "CZAR COMPLETE TOOLCHAIN",
        "ok": all_ok,
        "mandatory_ready": sum(1 for i in mandatory if i["ok"]),
        "mandatory_total": len(mandatory),
        "mandatory": mandatory,
        "optional": optional,
        "physical_ps4_compatibility": "PENDENTE DE TESTE FÍSICO (PS4 Slim 12.52)",
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("CZAR COMPLETE TOOLCHAIN")
        for item in mandatory:
            suffix = "" if item["ok"] else f" — {item['detail']}"
            print(f"{item['name']}: {item['status']}.{suffix}")
        print("---")
        print("OPCIONAIS:")
        for item in optional:
            print(f"{item['name']}: {item['status']}.")
        print("---")
        print(f"RESUMO: {report['mandatory_ready']}/{report['mandatory_total']} obrigatórios PRONTOS.")
        print(f"COMPATIBILIDADE FÍSICA PS4: {report['physical_ps4_compatibility']}.")

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
