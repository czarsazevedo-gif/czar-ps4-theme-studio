#!/usr/bin/env python3
"""Reproducible environment & tool diagnostics for CZAR FORGE (PS4 Theme Studio).

Run with:
    PYTHONPATH=. python3 scripts/diagnose_forge.py
or inside a virtualenv:
    PYTHONPATH=. /home/antigravity/.venv/bin/python scripts/diagnose_forge.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import at9  # noqa: E402
from app.forge.audio_validation import compare_roundtrip_audio, decode_to_float64_stereo, validate_at9  # noqa: E402
from app.forge.pkg_forge import pkg_forge_info  # noqa: E402


def _cmd_version(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        first = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        return first[0] if first else f"exit={proc.returncode}"
    except Exception:
        return None


def _check_ffmpeg_atrac9() -> dict:
    ff = shutil.which("ffmpeg")
    if not ff:
        return {"available": False, "atrac9_decoder": False, "ebur128": False}
    dec = subprocess.run([ff, "-hide_banner", "-decoders"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
    flt = subprocess.run([ff, "-hide_banner", "-filters"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
    return {
        "available": True,
        "path": ff,
        "version": _cmd_version([ff, "-version"]),
        "atrac9_decoder": "atrac9" in dec.stdout.decode("utf-8", "replace").lower(),
        "ebur128": "ebur128" in flt.stdout.decode("utf-8", "replace").lower(),
    }


def _at9_self_test() -> dict:
    if not at9.available():
        return {"executed": False, "ok": False, "reason": "Nenhum codificador ATRAC9 instalado."}
    with tempfile.TemporaryDirectory(prefix="czar-diag-") as td:
        d = Path(td)
        wav = d / "tone.wav"
        out = d / "tone.at9"
        ff = shutil.which("ffmpeg")
        if not ff:
            return {"executed": False, "ok": False, "reason": "ffmpeg indisponível."}
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.2", "-ac", "2", str(wav)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=15,
        )
        at9.encode(wav, out)
        rep = validate_at9(out)
        orig_pcm = decode_to_float64_stereo(wav)
        rt = compare_roundtrip_audio(orig_pcm, out)
        return {
            "executed": True,
            "ok": rep.valid and bool(rt["perceptual_ok"]),
            "bitrate_bps": rep.bitrate_bps,
            "sample_rate": rep.sample_rate,
            "channels": rep.channels,
            "has_smpl": rep.has_smpl,
            "config_data_hex": rep.config_data_hex,
            "validator": rep.validator,
            "roundtrip_energy_delta_db": rt["energy_delta_db"],
            "roundtrip_spectral_cosine": rt["spectral_cosine_similarity"],
            "errors": rep.errors,
        }


def main() -> int:
    ff_info = _check_ffmpeg_atrac9()
    at9_info = at9.encoder_info()
    pkg_info = pkg_forge_info()
    vgm = shutil.which("vgmstream-cli") or shutil.which("vgmstream123")
    at9_selftest = _at9_self_test()

    report = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "ffmpeg": ff_info,
        "atrac9_encoder": at9_info,
        "atrac9_selftest": at9_selftest,
        "pkg_forge": pkg_info,
        "vgmstream_optional": {"available": bool(vgm), "path": vgm},
        "physical_ps4_compatibility": "pending (requer teste físico em PS4 Slim 12.52)",
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))

    ok = (
        ff_info["available"]
        and ff_info["atrac9_decoder"]
        and ff_info["ebur128"]
        and at9_info["available"]
        and at9_selftest["ok"]
        and pkg_info["available"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
