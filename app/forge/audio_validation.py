"""Independent ATRAC9 validation, RIFF/smpl/fact inspection, round-trip audio checks,
and ITU-R BS.1770-4 / EBU R128 loudness metering (native Linux via FFmpeg + numpy)."""

from __future__ import annotations

import json
import math
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .. import media
from .contracts import (
    PS4_ATRAC9_BITRATE_KBPS,
    PS4_ATRAC9_BLOCK_ALIGN,
    PS4_ATRAC9_BYTE_RATE,
    PS4_ATRAC9_CHANNELS,
    PS4_ATRAC9_CONFIG_DATA_HEX,
    PS4_ATRAC9_SAMPLE_RATE,
    PS4_BGM_REFERENCE_LUFS,
    At9ValidationReport,
    LoudnessReport,
)

from ..config import PROJECT_ROOT

ATRAC9_SUBFORMAT_GUID = bytes([
    0xD2, 0x42, 0xE1, 0x47, 0xBA, 0x36, 0x8D, 0x4D,
    0x88, 0xFC, 0x61, 0x65, 0x4F, 0x8C, 0x83, 0x6C,
])


def find_vgmstream_cli() -> str | None:
    """Locate vgmstream-cli preferring the project-local tools/bin installation."""
    local = PROJECT_ROOT / "tools" / "bin" / "vgmstream-cli"
    if local.is_file():
        return str(local)
    return shutil.which("vgmstream-cli") or shutil.which("vgmstream_cli")


VGMSTREAM_CLI = find_vgmstream_cli()


def decode_to_float64_stereo(
    path: Path,
    *,
    sample_rate: int = PS4_ATRAC9_SAMPLE_RATE,
    max_seconds: float | None = None,
) -> np.ndarray:
    """Decode any FFmpeg-readable audio file to planar-ready shape (N, 2) float64 in [-1, 1]."""
    media.require_tools()
    cmd = [media.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path)]
    if max_seconds is not None and max_seconds > 0:
        cmd += ["-t", f"{max_seconds:.6f}"]
    cmd += ["-f", "f64le", "-acodec", "pcm_f64le", "-ac", "2", "-ar", str(sample_rate), "-"]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        creationflags=media.NO_WINDOW,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise media.MediaError("\n".join(tail[-5:]) or f"ffmpeg falhou ao decodificar {path.name}")
    raw = proc.stdout
    if not raw or len(raw) < 16:
        raise media.MediaError("Nenhum áudio decodificável foi encontrado no arquivo.")
    n_samples = len(raw) // 16
    return np.frombuffer(raw[: n_samples * 16], dtype="<f8").reshape(n_samples, 2).copy()


def write_pcm16_wav(pcm: np.ndarray, dest: Path, sample_rate: int = PS4_ATRAC9_SAMPLE_RATE) -> Path:
    """Write shape (N, 2) or (N,) float audio in [-1, 1] to standard 16-bit PCM WAV."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    arr = np.asarray(pcm, dtype=np.float64)
    if arr.ndim == 1:
        channels = 1
        frames = arr.shape[0]
        interleaved = arr
    elif arr.ndim == 2:
        frames, channels = arr.shape
        interleaved = arr.reshape(-1)
    else:
        raise ValueError("PCM deve ter 1 ou 2 dimensões.")

    clipped = np.clip(np.rint(interleaved * 32767.0), -32768, 32767).astype("<i2")
    payload = clipped.tobytes()
    pad = b"\x00" if (len(payload) & 1) else b""
    riff_size = 4 + 8 + 16 + 8 + len(payload) + len(pad)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        riff_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        sample_rate * channels * 2,
        channels * 2,
        16,
        b"data",
        len(payload),
    )
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(header + payload + pad)
    tmp.replace(dest)
    return dest


def parse_at9_riff(data: bytes) -> dict[str, Any]:
    """Independent binary parser for RIFF/WAVE ATRAC9 (.at9) containers."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("Arquivo não possui cabeçalho RIFF/WAVE válido.")

    riff_size = struct.unpack_from("<I", data, 4)[0]
    offset = 12
    chunks: dict[bytes, bytes] = {}
    chunk_order: list[str] = []
    truncated_riff = (riff_size + 8) > len(data)

    while offset + 8 <= len(data):
        cid = data[offset : offset + 4]
        sz = struct.unpack_from("<I", data, offset + 4)[0]
        body_start = offset + 8
        body_end = body_start + sz
        if body_end > len(data):
            truncated_riff = True
            chunks[cid] = data[body_start:]
            chunk_order.append(cid.decode("latin1"))
            break
        chunks[cid] = data[body_start:body_end]
        chunk_order.append(cid.decode("latin1"))
        offset = body_end + (sz & 1)

    if b"fmt " not in chunks or len(chunks[b"fmt "]) < 48:
        raise ValueError("Chunk 'fmt ' ausente ou incompleto para ATRAC9 (esperados >= 48 bytes).")
    if b"data" not in chunks or len(chunks[b"data"]) == 0:
        raise ValueError("Chunk 'data' ausente ou vazio.")

    fmt = chunks[b"fmt "]
    w_format_tag, channels, sample_rate, byte_rate, block_align, bits_per_sample, cb_size = (
        struct.unpack_from("<HHIIHHH", fmt, 0)
    )
    samples_per_block, channel_mask = struct.unpack_from("<HI", fmt, 18)
    subformat_guid = fmt[24:40]
    at9_version = struct.unpack_from("<I", fmt, 40)[0]
    config_data = fmt[44:48]

    fact_samples = None
    encoder_delay = None
    if b"fact" in chunks and len(chunks[b"fact"]) >= 4:
        fact = chunks[b"fact"]
        fact_samples = struct.unpack_from("<I", fact, 0)[0]
        if len(fact) >= 8:
            encoder_delay = struct.unpack_from("<I", fact, 4)[0]

    has_smpl = b"smpl" in chunks and len(chunks[b"smpl"]) >= 60
    loop_start = None
    loop_end = None
    play_count = None
    if has_smpl:
        smpl = chunks[b"smpl"]
        num_loops = struct.unpack_from("<I", smpl, 28)[0]
        if num_loops >= 1:
            loop_start = struct.unpack_from("<I", smpl, 44)[0]
            loop_end = struct.unpack_from("<I", smpl, 48)[0]
            play_count = struct.unpack_from("<I", smpl, 56)[0]

    data_len = len(chunks[b"data"])
    superframe_aligned = block_align > 0 and (data_len % block_align == 0)

    return {
        "riff_size": riff_size,
        "file_size": len(data),
        "truncated_riff": truncated_riff,
        "chunks": chunk_order,
        "format_tag": w_format_tag,
        "channels": channels,
        "sample_rate": sample_rate,
        "byte_rate": byte_rate,
        "bitrate_bps": byte_rate * 8,
        "block_align": block_align,
        "bits_per_sample": bits_per_sample,
        "cb_size": cb_size,
        "samples_per_block": samples_per_block,
        "channel_mask": channel_mask,
        "subformat_guid_ok": subformat_guid == ATRAC9_SUBFORMAT_GUID,
        "at9_version": at9_version,
        "config_data_hex": config_data.hex(),
        "has_fact": b"fact" in chunks,
        "fact_samples": fact_samples,
        "encoder_delay": encoder_delay,
        "has_smpl": has_smpl,
        "loop_start": loop_start,
        "loop_end": loop_end,
        "play_count": play_count,
        "data_bytes": data_len,
        "superframe_aligned": superframe_aligned,
    }


def patch_at9_smpl_loop(
    at9_path: Path,
    loop_start_sample: int,
    loop_end_sample: int,
    *,
    play_count: int = 0,
) -> None:
    """Safely updates the smpl chunk loop points (offset by encoder_delay) in an existing .at9 file."""
    raw = bytearray(at9_path.read_bytes())
    parsed = parse_at9_riff(bytes(raw))
    delay = int(parsed["encoder_delay"] or 0)
    total = int(parsed["fact_samples"] or 0)
    start_s = max(0, int(loop_start_sample))
    end_s = min(max(start_s + 1, int(loop_end_sample)), max(total - 1, start_s + 1))

    # Locate smpl chunk offset
    offset = 12
    while offset + 8 <= len(raw):
        cid = bytes(raw[offset : offset + 4])
        sz = struct.unpack_from("<I", raw, offset + 4)[0]
        if cid == b"smpl" and sz >= 60:
            struct.pack_into("<I", raw, offset + 8 + 44, start_s + delay)
            struct.pack_into("<I", raw, offset + 8 + 48, end_s + delay)
            struct.pack_into("<I", raw, offset + 8 + 56, int(play_count))
            tmp = at9_path.with_suffix(".at9.tmp")
            tmp.write_bytes(raw)
            tmp.replace(at9_path)
            return
        offset += 8 + sz + (sz & 1)
    raise ValueError("Arquivo .at9 não contém bloco 'smpl' para atualizar pontos de loop.")


def inspect_at9_vgmstream(path: Path) -> dict[str, Any] | None:
    """Inspect an .at9 file using vgmstream-cli -m, parsing codec, sample rate, channels, total & loop samples."""
    cli = find_vgmstream_cli()
    if not cli:
        return None
    try:
        proc = subprocess.run(
            [cli, "-m", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            creationflags=media.NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return {"ok": False, "output": proc.stderr.decode("utf-8", "replace")[:200]}
    text = proc.stdout.decode("utf-8", "replace")

    def _m_int(pattern: str) -> int | None:
        m = re.search(pattern, text, re.IGNORECASE)
        return int(m.group(1)) if m else None

    enc_m = re.search(r"encoding:\s*(.+)", text, re.IGNORECASE)
    encoding = enc_m.group(1).strip() if enc_m else ""
    sr = _m_int(r"sample rate:\s*(\d+)\s*Hz")
    ch = _m_int(r"channels:\s*(\d+)")
    total_samples = _m_int(r"stream total samples:\s*(\d+)")
    loop_start = _m_int(r"loop start:\s*(\d+)\s*samples")
    loop_end = _m_int(r"loop end:\s*(\d+)\s*samples")
    bitrate_kbps = _m_int(r"bitrate:\s*(\d+)\s*kbps")

    return {
        "ok": "ATRAC9" in encoding.upper() or "ATRAC9" in text.upper(),
        "encoding": encoding,
        "sample_rate": sr,
        "channels": ch,
        "total_samples": total_samples,
        "loop_start": loop_start,
        "loop_end": loop_end,
        "bitrate_kbps": bitrate_kbps,
        "output": text[:600],
    }


def decode_at9_vgmstream(
    path: Path,
    *,
    ignore_loop: bool = True,
    loops: int = 1,
    fade_sec: float = 0.0,
    sample_rate: int = PS4_ATRAC9_SAMPLE_RATE,
) -> np.ndarray:
    """Decode an .at9 file via vgmstream-cli with exact PS4 encoder-delay compensation or multi-cycle looping."""
    cli = find_vgmstream_cli()
    if not cli:
        raise media.MediaError("vgmstream-cli indisponível.")
    with tempfile.TemporaryDirectory(prefix="czar-vgm-") as td:
        out_wav = Path(td) / "decoded.wav"
        cmd = [cli]
        if ignore_loop:
            cmd.append("-i")
        else:
            cmd += ["-l", str(max(1, int(loops))), "-f", f"{max(0.0, float(fade_sec)):g}"]
        cmd += ["-o", str(out_wav), str(path)]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            creationflags=media.NO_WINDOW,
        )
        if proc.returncode != 0 or not out_wav.is_file():
            err = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
            raise media.MediaError(f"vgmstream-cli falhou ao decodificar {path.name}: {err[:200]}")
        return decode_to_float64_stereo(out_wav, sample_rate=sample_rate)


def _vgmstream_check(path: Path) -> dict[str, Any] | None:
    return inspect_at9_vgmstream(path)


def validate_at9(
    path: Path,
    *,
    require_ps4_profile: bool = True,
    require_loop: bool = True,
) -> At9ValidationReport:
    """Independently validates an .at9 file using binary RIFF inspection + FFprobe + full FFmpeg decode (+ optional vgmstream)."""
    errors: list[str] = []
    warnings: list[str] = []
    validator = "ffmpeg+riff"

    if not path.is_file() or path.stat().st_size == 0:
        return At9ValidationReport(
            valid=False,
            codec="unknown",
            sample_rate=0,
            channels=0,
            bitrate_bps=0,
            byte_rate=0,
            block_align=0,
            config_data_hex="",
            validator=validator,
            errors=["Arquivo .at9 ausente ou vazio."],
        )

    raw = path.read_bytes()
    try:
        riff = parse_at9_riff(raw)
    except ValueError as exc:
        return At9ValidationReport(
            valid=False,
            codec="unknown",
            sample_rate=0,
            channels=0,
            bitrate_bps=0,
            byte_rate=0,
            block_align=0,
            config_data_hex="",
            validator=validator,
            errors=[str(exc)],
        )

    if riff["truncated_riff"]:
        errors.append("Estrutura RIFF truncada: tamanho declarado excede o arquivo em disco.")
    if riff["format_tag"] != 0xFFFE:
        errors.append(f"wFormatTag inválido: 0x{riff['format_tag']:04X} (esperado 0xFFFE).")
    if not riff["subformat_guid_ok"]:
        errors.append("SubFormat GUID não corresponde ao codec ATRAC9.")
    if not riff["superframe_aligned"]:
        errors.append("Chunk 'data' não está alinhado ao tamanho do superframe ATRAC9 (possível truncamento).")

    if require_ps4_profile:
        if riff["sample_rate"] != PS4_ATRAC9_SAMPLE_RATE:
            errors.append(f"Taxa de amostragem inválida para PS4: {riff['sample_rate']} Hz (exigido 48000 Hz).")
        if riff["channels"] != PS4_ATRAC9_CHANNELS:
            errors.append(f"Canais inválidos para PS4: {riff['channels']} (exigido 2 — estéreo).")
        if riff["byte_rate"] != PS4_ATRAC9_BYTE_RATE or riff["block_align"] != PS4_ATRAC9_BLOCK_ALIGN:
            errors.append(
                f"Perfil de bitrate fora da referência PS4 144 kbps: {riff['byte_rate']} B/s, "
                f"blockAlign={riff['block_align']} (esperado 18000 B/s e blockAlign=384)."
            )
        if riff["config_data_hex"] != PS4_ATRAC9_CONFIG_DATA_HEX:
            errors.append(
                f"configData ATRAC9 divergente da referência PS4: {riff['config_data_hex']} "
                f"(esperado {PS4_ATRAC9_CONFIG_DATA_HEX})."
            )

    if not riff["has_fact"]:
        warnings.append("Bloco 'fact' ausente no contêiner RIFF.")

    if require_loop:
        if not riff["has_smpl"]:
            errors.append("Bloco 'smpl' ausente: a música de menu do PS4 precisa de ponto de loop válido.")
        else:
            if riff["play_count"] != 0:
                errors.append(f"smpl.playCount={riff['play_count']} (esperado 0 para loop infinito).")
            if riff["loop_start"] is None or riff["loop_end"] is None or riff["loop_end"] <= riff["loop_start"]:
                errors.append("Pontos de loop no bloco 'smpl' são inválidos.")
            else:
                delay = int(riff.get("encoder_delay") or 0)
                ls = int(riff["loop_start"])
                le = int(riff["loop_end"])
                if ls < delay:
                    errors.append(f"smpl.loop_start ({ls}) é menor que encoder_delay ({delay}).")
                if (le - ls) < 1024:
                    errors.append(f"Intervalo de loop muito curto ({le - ls} amostras < 1024).")
                if riff["fact_samples"]:
                    max_valid_end = int(riff["fact_samples"]) + delay + 2048
                    if le > max_valid_end:
                        errors.append(
                            f"smpl.loop_end ({le}) excede o total de amostras em 'fact' ({riff['fact_samples']} + delay {delay})."
                        )

    # Independent FFprobe inspection
    codec_name = "unknown"
    ff_sr = 0
    ff_ch = 0
    ff_dur = 0.0
    ff_br = 0
    try:
        info = media.probe_audio(path)
        codec_name = info.codec
        ff_sr = info.rate
        ff_ch = info.channels
        ff_dur = info.duration
        ff_br = info.bitrate
        if codec_name != "atrac9":
            errors.append(f"FFprobe identificou codec '{codec_name}' em vez de 'atrac9'.")
        if ff_sr != riff["sample_rate"]:
            errors.append(f"FFprobe divergiu na taxa de amostragem ({ff_sr} vs {riff['sample_rate']}).")
        if ff_ch != riff["channels"]:
            errors.append(f"FFprobe divergiu nos canais ({ff_ch} vs {riff['channels']}).")
    except media.MediaError as exc:
        errors.append(f"FFprobe falhou ao ler o .at9: {exc}")

    # Full independent FFmpeg decode round-trip check
    decoded_samples = 0
    decoded_dur = 0.0
    decoded_peak_dbfs = None
    decoded_clipped = 0
    truncated = bool(riff["truncated_riff"])
    try:
        pcm = decode_to_float64_stereo(path, sample_rate=riff["sample_rate"] or PS4_ATRAC9_SAMPLE_RATE)
        decoded_samples = int(pcm.shape[0])
        decoded_dur = decoded_samples / float(riff["sample_rate"] or PS4_ATRAC9_SAMPLE_RATE)
        peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
        decoded_peak_dbfs = round(20.0 * math.log10(peak), 2) if peak > 1e-12 else -120.0
        decoded_clipped = int(np.sum(np.abs(pcm) >= 0.9995))
        if decoded_peak_dbfs <= -70.0:
            warnings.append(f"Áudio ATRAC9 praticamente mudo/silencioso (pico {decoded_peak_dbfs} dBFS).")
        if riff["fact_samples"]:
            expected = int(riff["fact_samples"])
            # Lossy codec padding/delay allows up to 2 superframes (2048 samples) tolerance
            if decoded_samples + 2048 < expected:
                truncated = True
                errors.append(
                    f"Decodificação truncada: {decoded_samples} amostras decodificadas "
                    f"contra {expected} declaradas em 'fact'."
                )
        if require_loop and riff["has_smpl"] and riff["loop_end"] is not None and decoded_samples > 0:
            delay = int(riff.get("encoder_delay") or 0)
            if int(riff["loop_end"]) > decoded_samples + delay + 2048:
                errors.append(
                    f"smpl.loop_end ({riff['loop_end']}) ultrapassa o total decodificado ({decoded_samples} + delay {delay})."
                )
        if decoded_samples == 0:
            truncated = True
            errors.append("Decodificação independente produziu 0 amostras.")
    except media.MediaError as exc:
        truncated = True
        errors.append(f"FFmpeg falhou ao decodificar o .at9 completo: {exc}")

    vgm = _vgmstream_check(path)
    if vgm is not None:
        validator = "ffmpeg+vgmstream+riff"
        if not vgm["ok"]:
            errors.append(f"vgmstream-cli rejeitou o .at9: {vgm['output']}")
        else:
            if vgm.get("sample_rate") and vgm["sample_rate"] != riff["sample_rate"]:
                errors.append(
                    f"vgmstream-cli divergiu na taxa de amostragem ({vgm['sample_rate']} vs {riff['sample_rate']})."
                )
            if vgm.get("channels") and vgm["channels"] != riff["channels"]:
                errors.append(f"vgmstream-cli divergiu nos canais ({vgm['channels']} vs {riff['channels']}).")
            if require_loop and (vgm.get("loop_start") is None or vgm.get("loop_end") is None):
                errors.append("vgmstream-cli não detectou pontos de loop válidos no arquivo .at9.")
            elif (
                vgm.get("loop_start") is not None
                and vgm.get("loop_end") is not None
                and riff.get("loop_start") is not None
                and riff.get("loop_end") is not None
            ):
                if int(vgm["loop_end"]) <= int(vgm["loop_start"]):
                    errors.append(
                        f"vgmstream-cli reportou loop degenerado [{vgm['loop_start']}..{vgm['loop_end']}]."
                    )
                if vgm.get("total_samples") and int(vgm["loop_end"]) > int(vgm["total_samples"]) + 2048:
                    errors.append(
                        f"vgmstream-cli loop_end ({vgm['loop_end']}) excede stream total ({vgm['total_samples']})."
                    )
                delay = int(riff.get("encoder_delay") or 0)
                exp_ls = max(0, int(riff["loop_start"]) - delay)
                exp_le = max(exp_ls + 1, int(riff["loop_end"]) - delay + 1)
                if abs(int(vgm["loop_start"]) - exp_ls) > 2 or abs(int(vgm["loop_end"]) - exp_le) > 2:
                    warnings.append(
                        f"vgmstream loop [{vgm['loop_start']}..{vgm['loop_end']}] difere do smpl descontado [{exp_ls}..{exp_le}]."
                    )

    return At9ValidationReport(
        valid=len(errors) == 0,
        codec=codec_name,
        sample_rate=riff["sample_rate"],
        channels=riff["channels"],
        bitrate_bps=ff_br or riff["bitrate_bps"],
        byte_rate=riff["byte_rate"],
        block_align=riff["block_align"],
        config_data_hex=riff["config_data_hex"],
        riff_chunks=riff["chunks"],
        has_fact=riff["has_fact"],
        fact_samples=riff["fact_samples"],
        encoder_delay=riff["encoder_delay"],
        has_smpl=riff["has_smpl"],
        loop_start_sample=riff["loop_start"],
        loop_end_sample=riff["loop_end"],
        play_count=riff["play_count"],
        duration_seconds=round(ff_dur or decoded_dur, 4),
        decoded_samples=decoded_samples,
        decoded_duration_seconds=round(decoded_dur, 4),
        decoded_peak_dbfs=decoded_peak_dbfs,
        decoded_clipped_samples=decoded_clipped,
        truncated=truncated,
        validator=validator,
        errors=errors,
        warnings=warnings,
    )


def compare_roundtrip_audio(
    original_pcm: np.ndarray,
    at9_path: Path,
    sample_rate: int = PS4_ATRAC9_SAMPLE_RATE,
) -> dict[str, Any]:
    """Perceptual/objective comparison between source PCM and independently decoded ATRAC9 WAV.

    Respects lossy codec behavior (no bit-exact sample equality expected): evaluates
    duration match, RMS energy delta, band-spectrum similarity, clipping, and silence.
    """
    decoded = decode_to_float64_stereo(at9_path, sample_rate=sample_rate)
    orig = np.asarray(original_pcm, dtype=np.float64)
    if orig.ndim == 1:
        orig = np.stack([orig, orig], axis=1)

    orig_dur = orig.shape[0] / float(sample_rate)
    dec_dur = decoded.shape[0] / float(sample_rate)
    n = min(orig.shape[0], decoded.shape[0])
    orig_rms = float(np.sqrt(np.mean(orig[:n] ** 2))) if n else 0.0
    dec_rms = float(np.sqrt(np.mean(decoded[:n] ** 2))) if n else 0.0
    energy_delta_db = (
        round(20.0 * math.log10((dec_rms + 1e-12) / (orig_rms + 1e-12)), 3) if orig_rms > 1e-9 else 0.0
    )

    # Spectral magnitude cosine similarity over overlapping Hann windows (phase-invariant for lossy codec)
    win_len = min(4096, n)
    if win_len >= 256:
        step = max(256, (n - win_len) // 16 or win_len)
        window = np.hanning(win_len)
        o_specs = []
        d_specs = []
        for pos in range(0, max(1, n - win_len + 1), step):
            o_mono = np.mean(orig[pos : pos + win_len], axis=1) * window
            d_mono = np.mean(decoded[pos : pos + win_len], axis=1) * window
            o_specs.append(np.abs(np.fft.rfft(o_mono)))
            d_specs.append(np.abs(np.fft.rfft(d_mono)))
        o_mean = np.mean(o_specs, axis=0)
        d_mean = np.mean(d_specs, axis=0)
        denom = float(np.linalg.norm(o_mean) * np.linalg.norm(d_mean))
        spectral_cosine = round(float(np.dot(o_mean, d_mean) / denom), 5) if denom > 1e-12 else 1.0
    else:
        spectral_cosine = 1.0

    # Silence ratio (samples below -60 dBFS)
    orig_silence = float(np.mean(np.max(np.abs(orig[:n]), axis=1) < 1e-3)) if n else 1.0
    dec_silence = float(np.mean(np.max(np.abs(decoded[:n]), axis=1) < 1e-3)) if n else 1.0
    dec_clipped = int(np.sum(np.abs(decoded) >= 0.9995))

    return {
        "original_duration_sec": round(orig_dur, 4),
        "decoded_duration_sec": round(dec_dur, 4),
        "duration_delta_sec": round(abs(dec_dur - orig_dur), 4),
        "energy_delta_db": energy_delta_db,
        "spectral_cosine_similarity": spectral_cosine,
        "original_silence_ratio": round(orig_silence, 4),
        "decoded_silence_ratio": round(dec_silence, 4),
        "decoded_clipped_samples": dec_clipped,
        "perceptual_ok": (
            abs(dec_dur - orig_dur) <= 0.15
            and abs(energy_delta_db) <= 2.5
            and spectral_cosine >= 0.85
        ),
    }


def _parse_ebur128_summary(stderr_text: str) -> tuple[float | None, float | None, float | None]:
    """Extract Integrated LUFS, LRA (LU), and True Peak (dBTP) from FFmpeg ebur128 summary."""
    summary_idx = stderr_text.rfind("Summary:")
    block = stderr_text[summary_idx:] if summary_idx != -1 else stderr_text

    def _find(pattern: str) -> float | None:
        m = re.search(pattern, block)
        if not m:
            return None
        val = float(m.group(1))
        return round(val, 2) if math.isfinite(val) and val > -99.0 else None

    lufs = _find(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS")
    lra = _find(r"LRA:\s*(-?\d+(?:\.\d+)?)\s*LU")
    peak = _find(r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS")
    return lufs, lra, peak


def measure_loudness(path: Path) -> LoudnessReport:
    """Measure integrated LUFS/LKFS, true peak (dBTP), LRA, RMS, and clipping natively via FFmpeg ebur128."""
    media.require_tools()
    proc = subprocess.run(
        [
            media.FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=180,
        creationflags=media.NO_WINDOW,
    )
    stderr_text = proc.stderr.decode("utf-8", "replace")
    lufs, lra, true_peak = _parse_ebur128_summary(stderr_text)

    peak_dbfs = None
    rms_dbfs = None
    dyn_db = None
    clipped = 0
    try:
        pcm = decode_to_float64_stereo(path)
        if pcm.size:
            peak = float(np.max(np.abs(pcm)))
            rms = float(np.sqrt(np.mean(pcm ** 2)))
            peak_dbfs = round(20.0 * math.log10(peak), 2) if peak > 1e-12 else -120.0
            rms_dbfs = round(20.0 * math.log10(rms), 2) if rms > 1e-12 else -120.0
            if peak > 1e-12 and rms > 1e-12:
                dyn_db = round(peak_dbfs - rms_dbfs, 2)
            clipped = int(np.sum(np.abs(pcm) >= 0.9995))
    except media.MediaError:
        pass

    delta = round(lufs - PS4_BGM_REFERENCE_LUFS, 2) if lufs is not None else None
    bgm_str = f"{lufs:.2f}" if lufs is not None else None
    return LoudnessReport(
        integrated_lufs=lufs,
        true_peak_dbtp=true_peak if true_peak is not None else peak_dbfs,
        loudness_range_lu=lra,
        peak_dbfs=peak_dbfs,
        rms_dbfs=rms_dbfs,
        dynamic_range_db=dyn_db,
        clipped_samples=clipped,
        reference_lufs=PS4_BGM_REFERENCE_LUFS,
        delta_vs_reference_db=delta,
        bgmhome_loud=bgm_str,
        method="ffmpeg-ebur128",
    )


def measure_loudness_pcm(pcm: np.ndarray, sample_rate: int = PS4_ATRAC9_SAMPLE_RATE) -> LoudnessReport:
    """Measure loudness of an in-memory PCM buffer via temporary WAV + FFmpeg ebur128."""
    with tempfile.TemporaryDirectory(prefix="czar-loud-") as td:
        wav = write_pcm16_wav(pcm, Path(td) / "measure.wav", sample_rate=sample_rate)
        return measure_loudness(wav)
