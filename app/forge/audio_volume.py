"""Professional volume & loudness processing for PS4 theme menu music.

Supports four explicit modes — never silently normalizing without user consent:
- original: untouched source level (0.0 dB gain).
- ambient: gentle PS4 menu ambient target (-28.0 LUFS default, referencing Sony's -32.0 LKFS).
- manual: exact user gain_db (-24.0 to +12.0 dB) with soft true-peak limiter when boosting.
- assisted: user-chosen target LUFS (-34.0 to -14.0 LUFS) with true-peak protection (-1.5 dBTP).
"""

from __future__ import annotations

import math

import numpy as np

from .audio_validation import measure_loudness_pcm
from .contracts import PS4_ATRAC9_SAMPLE_RATE, PS4_BGM_REFERENCE_LUFS, LoudnessReport, VolumeMode

MIN_GAIN_DB = -24.0
MAX_GAIN_DB = 12.0
AMBIENT_DEFAULT_LUFS = -28.0
TRUE_PEAK_CEILING_DB = -1.5

BROWSER_VS_PS4_LOUDNESS_NOTE = (
    "A prévia no navegador reproduz o áudio com o ganho aplicado ao arquivo, mas sem a "
    "curva de nivelamento interno do sistema do PS4 (que utiliza bgmhome_loud no param.sfo "
    "em referência a -32.00 LKFS). O volume percebido no console pode variar conforme a TV/headset."
)


def clamp_gain_db(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    return round(max(MIN_GAIN_DB, min(MAX_GAIN_DB, v)), 2)


def clamp_target_lufs(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return AMBIENT_DEFAULT_LUFS
    if not math.isfinite(v):
        return AMBIENT_DEFAULT_LUFS
    return round(max(-36.0, min(-12.0, v)), 2)


def _soft_peak_limit(pcm: np.ndarray, ceiling_db: float = TRUE_PEAK_CEILING_DB) -> np.ndarray:
    """Transparent look-ahead-free soft knee limiter preventing harsh digital clipping."""
    ceiling = 10.0 ** (ceiling_db / 20.0)
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    if peak <= ceiling:
        return pcm
    knee = ceiling * 0.90
    out = pcm.copy()
    mag = np.abs(out)
    over = mag > knee
    if np.any(over):
        excess = (mag[over] - knee) / max(ceiling - knee, 1e-6)
        shaped = knee + (ceiling - knee) * np.tanh(excess)
        out[over] = np.sign(out[over]) * shaped
    # Final hard safety clamp slightly below 1.0
    return np.clip(out, -0.995, 0.995)


def apply_volume_mode(
    pcm: np.ndarray,
    *,
    mode: VolumeMode = "original",
    gain_db: float = 0.0,
    target_lufs: float | None = None,
    sr: int = PS4_ATRAC9_SAMPLE_RATE,
) -> tuple[np.ndarray, dict, LoudnessReport, LoudnessReport]:
    """Apply explicit volume mode and return (processed_pcm, params_dict, before_loudness, after_loudness)."""
    arr = np.asarray(pcm, dtype=np.float64)
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=1)

    before = measure_loudness_pcm(arr, sample_rate=sr)
    applied_gain_db = 0.0
    applied_limiter = False
    chosen_target: float | None = None

    if mode == "original":
        out = arr.copy()
    elif mode == "manual":
        applied_gain_db = clamp_gain_db(gain_db)
        scale = 10.0 ** (applied_gain_db / 20.0)
        out = arr * scale
        if applied_gain_db > 0.0 or float(np.max(np.abs(out))) > 0.98:
            out = _soft_peak_limit(out, ceiling_db=-0.5)
            applied_limiter = True
    elif mode in {"ambient", "assisted"}:
        chosen_target = (
            AMBIENT_DEFAULT_LUFS
            if mode == "ambient" and target_lufs is None
            else clamp_target_lufs(target_lufs if target_lufs is not None else AMBIENT_DEFAULT_LUFS)
        )
        if before.integrated_lufs is not None:
            raw_delta = chosen_target - before.integrated_lufs
            applied_gain_db = clamp_gain_db(raw_delta)
        else:
            applied_gain_db = 0.0
        scale = 10.0 ** (applied_gain_db / 20.0)
        out = arr * scale
        if float(np.max(np.abs(out))) > 10.0 ** (TRUE_PEAK_CEILING_DB / 20.0):
            out = _soft_peak_limit(out, ceiling_db=TRUE_PEAK_CEILING_DB)
            applied_limiter = True
    else:
        raise ValueError(f"Modo de volume desconhecido: {mode}")

    after = before if (mode == "original" and not applied_limiter) else measure_loudness_pcm(out, sample_rate=sr)
    params = {
        "mode": mode,
        "requested_gain_db": clamp_gain_db(gain_db),
        "applied_gain_db": applied_gain_db,
        "target_lufs": chosen_target,
        "reference_lufs": PS4_BGM_REFERENCE_LUFS,
        "limiter_applied": applied_limiter,
        "browser_vs_ps4_note": BROWSER_VS_PS4_LOUDNESS_NOTE,
    }
    return out, params, before, after
