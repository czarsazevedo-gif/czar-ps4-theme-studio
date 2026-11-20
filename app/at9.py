"""ATRAC9 (.at9) encoding via native Linux external CLI (psgfx-at9, GPL-3.0-or-later)
or Windows Sony SDK at9tool.exe when present.

PS4 reference parameters (verified against Sony retail PS4 themes that play on console):
- 48000 Hz, Stereo (2 channels)
- 144 kbps (18000 B/s, blockAlign 384, configData fe 74 0b f0)
- Valid RIFF/WAVE container with fmt (0xFFFE + ATRAC9 GUID), fact, smpl (playCount=0), data

Never silently substitutes 168 kbps and never renames PCM WAV to .at9.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from .config import PKG_TOOLS_DIR, PROJECT_ROOT
from .media import NO_WINDOW, MediaError

#: Legacy Windows Sony SDK tool names.
TOOL_NAMES = ("ps4_at9tool.exe", "at9tool.exe")
#: Native Linux open-source external CLI names (built outside repo into tools/bin/).
NATIVE_TOOL_NAMES = ("psgfx-at9", "at9check")

#: 144 kbps, measured on the bgm_home.at9 of a theme that DOES play on the
#: console: 18000 B/s, block align 384, configData fe 74 0b f0.
#: This used to be 168, which is ATRACTool-Reloaded's default and comes from no
#: PS4 theme: it gives align 448 and configData fe 74 0d f0. The music stayed
#: silent with that.
DEFAULT_BITRATE = 144
SAMPLE_RATE = 48000

#: A theme needs no more; keeps waits short and packages small.
MAX_SECONDS = 300


def find_native_tool() -> Path | None:
    """Locate externally built native Linux ATRAC9 CLI (psgfx-at9 / at9check)."""
    env_override = os.environ.get("CZAR_AT9_ENCODER", "").strip()
    if env_override:
        p = Path(env_override)
        if p.is_file() and os.access(p, os.X_OK):
            return p
    for folder in (PROJECT_ROOT / "tools" / "bin", PKG_TOOLS_DIR / "bin", PKG_TOOLS_DIR):
        for name in NATIVE_TOOL_NAMES:
            candidate = folder / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    for name in NATIVE_TOOL_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def find_windows_tool() -> Path | None:
    """Locate legacy Windows at9tool.exe when running on Windows."""
    if os.name != "nt":
        # Avoid falsely treating a Windows .exe as directly executable on Linux
        return None
    for folder in (PKG_TOOLS_DIR, PKG_TOOLS_DIR / "ext", PROJECT_ROOT):
        for name in TOOL_NAMES:
            candidate = folder / name
            if candidate.is_file():
                return candidate
    for name in TOOL_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def find_tool() -> Path | None:
    """Looks for a usable ATRAC9 encoder (native Linux psgfx-at9 first, then Windows at9tool.exe)."""
    return find_native_tool() or find_windows_tool()


def encoder_info() -> dict:
    """Report active ATRAC9 encoder backend, path, and licensing boundary."""
    native = find_native_tool()
    if native is not None:
        return {
            "available": True,
            "backend": "psgfx-at9",
            "path": str(native),
            "license": "GPL-3.0-or-later (external CLI subprocess)",
            "bitrate_kbps": DEFAULT_BITRATE,
            "sample_rate": SAMPLE_RATE,
            "channels": 2,
        }
    win = find_windows_tool()
    if win is not None:
        return {
            "available": True,
            "backend": "at9tool.exe",
            "path": str(win),
            "license": "Proprietary (user-supplied Sony SDK binary)",
            "bitrate_kbps": DEFAULT_BITRATE,
            "sample_rate": SAMPLE_RATE,
            "channels": 2,
        }
    return {
        "available": False,
        "backend": None,
        "path": None,
        "license": None,
        "bitrate_kbps": DEFAULT_BITRATE,
        "sample_rate": SAMPLE_RATE,
        "channels": 2,
    }


def available() -> bool:
    return find_tool() is not None


def encode_pcm(
    pcm: np.ndarray,
    dest: Path,
    *,
    bitrate: int = DEFAULT_BITRATE,
    loop: bool = True,
    loop_start_sample: int | None = None,
    loop_end_sample: int | None = None,
    validate: bool = True,
) -> Path:
    """Encode 48 kHz float64 stereo PCM array shape (N, 2) -> validated PS4 .at9 file atomically."""
    if bitrate != DEFAULT_BITRATE:
        raise MediaError(
            f"Bitrate ATRAC9 inválido para tema PS4: {bitrate} kbps. "
            f"A especificação verificada no console exige exatamente {DEFAULT_BITRATE} kbps."
        )
    native = find_native_tool()
    if native is None:
        # Fallback via WAV if Windows at9tool.exe is present
        from .forge.audio_validation import write_pcm16_wav

        with tempfile.TemporaryDirectory(prefix="czar-at9-wav-") as td:
            wav_path = write_pcm16_wav(pcm, Path(td) / "input.wav", sample_rate=SAMPLE_RATE)
            return encode(
                wav_path,
                dest,
                bitrate=bitrate,
                loop=loop,
                loop_start_sample=loop_start_sample,
                loop_end_sample=loop_end_sample,
                validate=validate,
            )

    arr = np.asarray(pcm, dtype=np.float64)
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=1)
    if arr.ndim != 2 or arr.shape[1] != 2 or arr.shape[0] < 256:
        raise MediaError("PCM inválido para codificação ATRAC9 (esperado estéreo 48 kHz com >= 256 amostras).")

    dest.parent.mkdir(parents=True, exist_ok=True)
    planar = np.concatenate([arr[:, 0], arr[:, 1]]).astype("<f8", copy=False)

    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".at9-") as td:
        td_path = Path(td)
        pcm_file = td_path / "planar.f64"
        tmp_out = td_path / "encoded.at9"
        pcm_file.write_bytes(planar.tobytes())

        cmd = [
            str(native),
            "--encode-raw",
            str(pcm_file),
            str(tmp_out),
            "--bitrate",
            str(bitrate),
        ]
        if not loop:
            cmd.append("--no-loop")
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=600,
                creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaError("O codificador ATRAC9 excedeu o tempo limite (600s).") from exc

        if proc.returncode != 0 or not tmp_out.is_file() or tmp_out.stat().st_size == 0:
            tail = proc.stdout.decode("utf-8", "replace").strip().splitlines()
            raise MediaError(
                "Codificador ATRAC9 (psgfx-at9) falhou: " + ("\n".join(tail[-4:]) or f"código {proc.returncode}")
            )

        if loop and (loop_start_sample is not None or loop_end_sample is not None):
            from .forge.audio_validation import patch_at9_smpl_loop

            ls = 0 if loop_start_sample is None else int(loop_start_sample)
            le = (arr.shape[0] - 1) if loop_end_sample is None else int(loop_end_sample)
            patch_at9_smpl_loop(tmp_out, ls, le, play_count=0)

        if validate:
            from .forge.audio_validation import validate_at9

            report = validate_at9(tmp_out, require_ps4_profile=True, require_loop=loop)
            if not report.valid:
                raise MediaError(
                    "Validação independente do ATRAC9 gerado falhou: " + "; ".join(report.errors)
                )

        tmp_out.replace(dest)
    return dest


def encode(
    wav_path: Path,
    dest: Path,
    bitrate: int = DEFAULT_BITRATE,
    *,
    loop: bool = True,
    loop_start_sample: int | None = None,
    loop_end_sample: int | None = None,
    validate: bool = True,
) -> Path:
    """Audio/WAV -> validated PS4 .at9. Uses native psgfx-at9 on Linux or at9tool.exe on Windows."""
    if bitrate != DEFAULT_BITRATE:
        raise MediaError(
            f"Bitrate ATRAC9 inválido para tema PS4: {bitrate} kbps (exigido {DEFAULT_BITRATE} kbps)."
        )

    native = find_native_tool()
    if native is not None:
        from .forge.audio_validation import decode_to_float64_stereo

        pcm = decode_to_float64_stereo(wav_path, sample_rate=SAMPLE_RATE, max_seconds=MAX_SECONDS)
        return encode_pcm(
            pcm,
            dest,
            bitrate=bitrate,
            loop=loop,
            loop_start_sample=loop_start_sample,
            loop_end_sample=loop_end_sample,
            validate=validate,
        )

    tool = find_windows_tool()
    if tool is None:
        raise MediaError(
            "Codificador ATRAC9 não encontrado. Execute ./scripts/install_forge_tools.sh at9 "
            "para compilar o codificador nativo Linux (psgfx-at9), ou envie um arquivo .at9 válido."
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".at9-") as td:
        tmp_out = Path(td) / "encoded.at9"
        cmd = [str(tool), "-e", "-br", str(bitrate), "-fs", str(SAMPLE_RATE)]
        if loop:
            cmd.append("-wholeloop")
        cmd += [str(wav_path), str(tmp_out)]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=600,
            creationflags=NO_WINDOW,
        )
        if not tmp_out.is_file() or tmp_out.stat().st_size == 0:
            tail = proc.stdout.decode("utf-8", "replace").strip().splitlines()
            raise MediaError("at9tool produced no file: " + ("\n".join(tail[-4:]) or "no output"))
        if validate:
            from .forge.audio_validation import validate_at9

            report = validate_at9(tmp_out, require_ps4_profile=True, require_loop=loop)
            if not report.valid:
                raise MediaError("Validação independente do .at9 falhou: " + "; ".join(report.errors))
        tmp_out.replace(dest)
    return dest
