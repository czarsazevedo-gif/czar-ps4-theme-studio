"""CZAR Audio Loop Finisher: musical seam analysis, boundary refinement, selective
phase/crossfade finishing, post-ATRAC9 verification, and loop preview rendering."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

import numpy as np

from .audio_validation import decode_to_float64_stereo, write_pcm16_wav
from .contracts import PS4_ATRAC9_SAMPLE_RATE, LoopFinishReport

SILENCE_THRESHOLD = 10.0 ** (-55.0 / 20.0)  # -55 dBFS


def _detect_silence_margins(pcm: np.ndarray, sr: int) -> tuple[float, float]:
    """Return (leading_silence_sec, trailing_silence_sec) below -55 dBFS."""
    if pcm.shape[0] == 0:
        return 0.0, 0.0
    env = np.max(np.abs(pcm), axis=1)
    active = np.flatnonzero(env >= SILENCE_THRESHOLD)
    if active.size == 0:
        dur = pcm.shape[0] / float(sr)
        return dur, dur
    lead = int(active[0]) / float(sr)
    trail = (pcm.shape[0] - 1 - int(active[-1])) / float(sr)
    return round(lead, 4), round(trail, 4)


def _onset_strength_near(pcm: np.ndarray, idx: int, sr: int, window_ms: float = 40.0) -> float:
    """Estimate transient/attack energy near a sample index to avoid slicing musical attacks."""
    half = max(64, int(sr * window_ms / 1000.0))
    lo = max(0, idx - half)
    hi = min(pcm.shape[0], idx + half)
    seg = np.mean(pcm[lo:hi], axis=1)
    if seg.size < 64:
        return 0.0
    hop = max(16, seg.size // 16)
    rms_blocks = [
        float(np.sqrt(np.mean(seg[i : i + hop] ** 2)))
        for i in range(0, seg.size - hop + 1, hop)
    ]
    if len(rms_blocks) < 2:
        return 0.0
    diffs = np.diff(rms_blocks)
    pos_flux = float(np.max(np.maximum(diffs, 0.0)))
    base = float(np.mean(rms_blocks)) + 1e-6
    return round(pos_flux / base, 4)


def _seam_metrics(pcm: np.ndarray, sr: int) -> dict[str, float]:
    """Compute click risk, boundary jump, phase correlation, energy delta, and spectral match across end -> start."""
    n = pcm.shape[0]
    if n < 256:
        return {
            "discontinuity": 0.0,
            "click_risk": 0.0,
            "energy_delta_db": 0.0,
            "spectral_similarity": 1.0,
            "phase_correlation": 1.0,
        }

    # Instantaneous amplitude jump + first-derivative jump across loop seam
    jump = float(np.max(np.abs(pcm[-1] - pcm[0])))
    slope_end = pcm[-1] - pcm[-2]
    slope_start = pcm[1] - pcm[0]
    slope_jump = float(np.max(np.abs(slope_start - slope_end)))
    click_risk = round(min(1.0, jump * 0.7 + slope_jump * 0.6), 5)

    # Windowed comparison: tail vs head (up to 120 ms)
    w = min(int(sr * 0.12), n // 4)
    w = max(128, w)
    tail = pcm[-w:]
    head = pcm[:w]

    rms_tail = float(np.sqrt(np.mean(tail ** 2)))
    rms_head = float(np.sqrt(np.mean(head ** 2)))
    energy_delta_db = round(
        20.0 * math.log10((rms_head + 1e-7) / (rms_tail + 1e-7)), 3
    )

    # Spectral cosine similarity (Hann windowed log-magnitude spectrum)
    hann = np.hanning(w)
    spec_t = np.log1p(np.abs(np.fft.rfft(np.mean(tail, axis=1) * hann)) * 10.0)
    spec_h = np.log1p(np.abs(np.fft.rfft(np.mean(head, axis=1) * hann)) * 10.0)
    denom_s = float(np.linalg.norm(spec_t) * np.linalg.norm(spec_h))
    spectral_sim = round(float(np.dot(spec_t, spec_h) / denom_s), 5) if denom_s > 1e-12 else 1.0

    # Short-window phase correlation (20 ms)
    pw = min(int(sr * 0.02), w)
    t_m = np.mean(pcm[-pw:], axis=1)
    h_m = np.mean(pcm[:pw], axis=1)
    t_m = t_m - np.mean(t_m)
    h_m = h_m - np.mean(h_m)
    denom_p = float(np.linalg.norm(t_m) * np.linalg.norm(h_m))
    phase_corr = round(float(np.dot(t_m, h_m) / denom_p), 5) if denom_p > 1e-12 else 1.0

    return {
        "discontinuity": round(jump, 5),
        "click_risk": click_risk,
        "energy_delta_db": energy_delta_db,
        "spectral_similarity": max(0.0, min(1.0, spectral_sim)),
        "phase_correlation": max(-1.0, min(1.0, phase_corr)),
    }


def _estimate_rhythmic_period_sec(pcm: np.ndarray, sr: int) -> float | None:
    """Estimate dominant beat/bar periodicity via onset-envelope autocorrelation."""
    n = pcm.shape[0]
    if n < sr * 2:
        return None
    hop = int(sr * 0.01)  # 10 ms frames = 100 Hz envelope
    mono = np.mean(pcm, axis=1)
    n_frames = n // hop
    if n_frames < 120:
        return None
    blocks = mono[: n_frames * hop].reshape(n_frames, hop)
    env = np.sqrt(np.mean(blocks ** 2, axis=1))
    onset = np.maximum(np.diff(env, prepend=env[0]), 0.0)
    onset = onset - np.mean(onset)
    norm = float(np.dot(onset, onset))
    if norm < 1e-12:
        return None
    corr = np.correlate(onset, onset, mode="full")[len(onset) - 1 :] / norm
    # Search 0.25s..2.0s (30..240 BPM)
    lo = min(25, len(corr) - 1)
    hi = min(200, len(corr))
    if hi <= lo:
        return None
    peak_idx = lo + int(np.argmax(corr[lo:hi]))
    if float(corr[peak_idx]) < 0.22:
        return None
    return round(peak_idx * 0.01, 3)


def _snap_zero_crossing(pcm: np.ndarray, target: int, search_samples: int) -> int:
    """Find the lowest-amplitude upward-crossing sample within +/- search_samples."""
    n = pcm.shape[0]
    lo = max(1, target - search_samples)
    hi = min(n - 2, target + search_samples)
    if hi <= lo:
        return max(0, min(n - 1, target))
    mono = np.mean(pcm[lo : hi + 1], axis=1)
    # Prefer rising zero crossings with minimal stereo amplitude
    best_idx = target
    best_cost = float("inf")
    for k in range(1, len(mono)):
        idx = lo + k
        amp = float(np.max(np.abs(pcm[idx])))
        slope = float(mono[k] - mono[k - 1])
        crossing = (mono[k - 1] <= 0.0 and mono[k] >= 0.0)
        cost = amp + (0.0 if crossing else 0.08) + abs(idx - target) * 1e-6 + (0.02 if slope < 0 else 0.0)
        if cost < best_cost:
            best_cost = cost
            best_idx = idx
    return best_idx


def _refine_boundaries_auto(
    pcm: np.ndarray,
    start_s: int,
    end_s: int,
    sr: int,
) -> tuple[int, int]:
    """Search subtle boundary adjustments (+/- 200 ms on end, +/- 20 ms zero-crossing on start)
    maximizing spectral/phase/energy continuity without cutting musical attacks."""
    n = pcm.shape[0]
    min_len = max(sr // 2, 24000)
    if end_s - start_s <= min_len:
        return start_s, end_s

    # Preserve initial attack if start sits on a strong transient
    start_attack = _onset_strength_near(pcm, start_s, sr, window_ms=25.0)
    if start_attack > 0.8:
        cand_start = start_s
    else:
        cand_start = _snap_zero_crossing(pcm, start_s, int(sr * 0.015))
        cand_start = max(0, min(cand_start, end_s - min_len))

    # Search candidate ends around end_s within +/- 220 ms (coarse 10 ms step, then zero-crossing snap)
    radius = min(int(sr * 0.22), (end_s - cand_start) // 6)
    step = max(64, int(sr * 0.01))
    best_end = end_s
    best_score = -1e9

    for delta in range(-radius, radius + 1, step):
        c_end = end_s + delta
        if c_end > n or c_end - cand_start < min_len:
            continue
        snapped = _snap_zero_crossing(pcm, c_end, int(sr * 0.008))
        snapped = min(n, max(cand_start + min_len, snapped))
        sub = pcm[cand_start:snapped]
        m = _seam_metrics(sub, sr)
        attack_pen = 0.25 if _onset_strength_near(pcm, snapped, sr, window_ms=20.0) > 1.2 else 0.0
        score = (
            m["spectral_similarity"] * 2.0
            + m["phase_correlation"] * 1.2
            - abs(m["energy_delta_db"]) * 0.12
            - m["click_risk"] * 2.5
            - attack_pen
        )
        if score > best_score:
            best_score = score
            best_end = snapped

    return cand_start, best_end


def _apply_localized_crossfade(pcm: np.ndarray, crossfade_samples: int) -> np.ndarray:
    """Equal-power wrap-around crossfade of the tail into the head, trimming crossfade_samples."""
    k = int(crossfade_samples)
    if k <= 4 or pcm.shape[0] <= k * 4:
        return pcm
    out = pcm[:-k].copy()
    tail = pcm[-k:]
    head = out[:k]
    t = np.linspace(0.0, 1.0, k, endpoint=False, dtype=np.float64)[:, None]
    # Equal-power cosine/sine curve prevents audible volume dip during crossfade
    w_head = np.sin(t * (math.pi * 0.5))
    w_tail = np.cos(t * (math.pi * 0.5))
    out[:k] = head * w_head + tail * w_tail
    return out


def _apply_authorized_fades(pcm: np.ndarray, sr: int, fade_in_ms: float, fade_out_ms: float) -> np.ndarray:
    """Apply smooth raised-cosine fades ONLY when explicitly authorized by the user."""
    out = pcm.copy()
    fi = min(int(sr * max(0.0, fade_in_ms) / 1000.0), out.shape[0] // 4)
    fo = min(int(sr * max(0.0, fade_out_ms) / 1000.0), out.shape[0] // 4)
    if fi > 1:
        curve = 0.5 * (1.0 - np.cos(np.linspace(0.0, math.pi, fi, dtype=np.float64)))[:, None]
        out[:fi] *= curve
    if fo > 1:
        curve = 0.5 * (1.0 + np.cos(np.linspace(0.0, math.pi, fo, dtype=np.float64)))[:, None]
        out[-fo:] *= curve
    return out


def analyze_audio_loop(
    pcm: np.ndarray,
    *,
    sr: int = PS4_ATRAC9_SAMPLE_RATE,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> dict:
    """Analyze a stereo PCM track for loop suitability without modifying audio."""
    n = pcm.shape[0]
    start_s = max(0, min(n - 1, int(round((start_sec or 0.0) * sr))))
    end_s = n if end_sec is None or end_sec <= 0 else max(start_s + 1, min(n, int(round(end_sec * sr))))
    excerpt = pcm[start_s:end_s]
    lead_sil, trail_sil = _detect_silence_margins(excerpt, sr)
    metrics = _seam_metrics(excerpt, sr)
    beat_period = _estimate_rhythmic_period_sec(excerpt, sr)
    clipped = int(np.sum(np.abs(excerpt) >= 0.9995))

    warnings: list[str] = []
    if lead_sil > 0.25 or trail_sil > 0.25:
        warnings.append(
            f"Silêncio detectado nas extremidades (início: {lead_sil:.2f}s, fim: {trail_sil:.2f}s), "
            "o que pode criar pausa perceptível na repetição."
        )
    if metrics["click_risk"] > 0.08:
        warnings.append(
            f"Risco de clique na costura ({metrics['click_risk']:.3f}): recomenda-se ajuste de fronteira."
        )
    if abs(metrics["energy_delta_db"]) > 6.0:
        warnings.append(
            f"Diferença brusca de energia entre fim e início ({metrics['energy_delta_db']:+.1f} dB)."
        )
    if metrics["spectral_similarity"] < 0.55:
        warnings.append(
            f"Baixa correspondência espectral entre fim e início ({metrics['spectral_similarity']:.2f})."
        )
    if clipped > 0:
        warnings.append(f"Fonte apresenta {clipped} amostras clipadas.")

    convincing = (
        metrics["click_risk"] <= 0.12
        and abs(metrics["energy_delta_db"]) <= 7.0
        and metrics["spectral_similarity"] >= 0.55
        and lead_sil <= 0.6
        and trail_sil <= 0.6
    )

    return {
        "total_samples": n,
        "sample_rate": sr,
        "total_duration_sec": round(n / float(sr), 4),
        "start_sample": start_s,
        "end_sample": end_s,
        "excerpt_duration_sec": round(excerpt.shape[0] / float(sr), 4),
        "leading_silence_sec": lead_sil,
        "trailing_silence_sec": trail_sil,
        "rhythmic_period_sec": beat_period,
        "clipped_samples": clipped,
        "convincing": convincing,
        "warnings": warnings,
        **metrics,
    }


def finish_audio_loop(
    pcm: np.ndarray,
    *,
    sr: int = PS4_ATRAC9_SAMPLE_RATE,
    mode: Literal["whole", "manual_excerpt", "auto_refined"] = "whole",
    start_sec: float | None = None,
    end_sec: float | None = None,
    start_sample: int | None = None,
    end_sample: int | None = None,
    trim_silence: bool = False,
    crossfade_ms: float = 0.0,
    allow_fade: bool = False,
    fade_in_ms: float = 0.0,
    fade_out_ms: float = 0.0,
) -> tuple[np.ndarray, LoopFinishReport]:
    """Prepare a musical loop cleanly without destroying attacks or applying indiscriminate crossfades."""
    arr = np.asarray(pcm, dtype=np.float64)
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=1)
    n = arr.shape[0]
    if n < 256:
        raise ValueError("Áudio curto demais para preparar um loop válido.")

    if start_sample is not None:
        s0 = max(0, min(n - 256, int(start_sample)))
    elif start_sec is not None and start_sec > 0:
        s0 = max(0, min(n - 256, int(round(start_sec * sr))))
    else:
        s0 = 0

    if end_sample is not None and end_sample > s0:
        s1 = min(n, max(s0 + 256, int(end_sample)))
    elif end_sec is not None and end_sec > 0:
        s1 = min(n, max(s0 + 256, int(round(end_sec * sr))))
    else:
        s1 = n

    raw_excerpt = arr[s0:s1]
    before_metrics = _seam_metrics(raw_excerpt, sr)
    lead_sil, trail_sil = _detect_silence_margins(raw_excerpt, sr)

    cur_s0, cur_s1 = s0, s1
    if trim_silence and raw_excerpt.shape[0] > sr:
        env = np.max(np.abs(raw_excerpt), axis=1)
        active = np.flatnonzero(env >= SILENCE_THRESHOLD)
        if active.size > 256:
            cur_s0 = s0 + int(active[0])
            cur_s1 = s0 + int(active[-1]) + 1

    if mode == "auto_refined":
        cur_s0, cur_s1 = _refine_boundaries_auto(arr, cur_s0, cur_s1, sr)
    elif mode == "whole" and before_metrics["click_risk"] > 0.05:
        # Micro zero-crossing snap within +/- 8 ms ONLY when there is a measurable click risk
        if _onset_strength_near(arr, cur_s0, sr, window_ms=20.0) <= 0.8:
            cur_s0 = _snap_zero_crossing(arr, cur_s0, int(sr * 0.008))
        cur_s1 = _snap_zero_crossing(arr, cur_s1, int(sr * 0.008))
        if cur_s1 <= cur_s0 + 256:
            cur_s0, cur_s1 = s0, s1

    processed = arr[cur_s0:cur_s1].copy()

    # Crossfade is NEVER applied indiscriminately: only when explicitly requested (>0)
    # or capped micro-crossfade in auto_refined when click risk remains elevated.
    applied_xfade_ms = 0.0
    requested_xfade_ms = max(0.0, min(250.0, float(crossfade_ms)))
    if requested_xfade_ms > 0.0:
        xf_samples = int(round(sr * requested_xfade_ms / 1000.0))
        if processed.shape[0] > xf_samples * 4:
            processed = _apply_localized_crossfade(processed, xf_samples)
            applied_xfade_ms = round(xf_samples * 1000.0 / sr, 2)
    elif mode == "auto_refined":
        mid_m = _seam_metrics(processed, sr)
        if mid_m["click_risk"] > 0.06 and processed.shape[0] > sr:
            # Localized 18 ms equal-power seam stitch only to suppress audible clicks
            xf_samples = int(round(sr * 0.018))
            candidate = _apply_localized_crossfade(processed, xf_samples)
            cand_m = _seam_metrics(candidate, sr)
            if cand_m["click_risk"] < mid_m["click_risk"] * 0.8:
                processed = candidate
                applied_xfade_ms = round(xf_samples * 1000.0 / sr, 2)

    fade_applied = False
    if allow_fade and (fade_in_ms > 0.0 or fade_out_ms > 0.0):
        processed = _apply_authorized_fades(processed, sr, fade_in_ms, fade_out_ms)
        fade_applied = True

    after_metrics = _seam_metrics(processed, sr)
    after_lead, after_trail = _detect_silence_margins(processed, sr)
    warnings: list[str] = []
    if after_metrics["click_risk"] > 0.12:
        warnings.append(
            f"Ainda há risco de clique na junção ({after_metrics['click_risk']:.3f}). "
            "Experimente ajustar início/fim manualmente ou habilitar um crossfade curto."
        )
    if abs(after_metrics["energy_delta_db"]) > 6.5:
        warnings.append(
            f"Diferença de energia entre fim e início permanece alta ({after_metrics['energy_delta_db']:+.1f} dB)."
        )
    if after_metrics["spectral_similarity"] < 0.55:
        warnings.append(
            f"Timbre do fim difere do início (similaridade espectral {after_metrics['spectral_similarity']:.2f}); "
            "um loop perfeitamente imperceptível pode não existir neste trecho."
        )
    if after_lead > 0.5 or after_trail > 0.5:
        warnings.append(
            f"Silêncio nas bordas (início {after_lead:.2f}s, fim {after_trail:.2f}s) pode soar como pausa no loop."
        )

    convincing = (
        after_metrics["click_risk"] <= 0.12
        and abs(after_metrics["energy_delta_db"]) <= 7.0
        and after_metrics["spectral_similarity"] >= 0.55
    )

    report = LoopFinishReport(
        mode=mode,
        start_sample=cur_s0,
        end_sample=cur_s0 + processed.shape[0],
        sample_rate=sr,
        duration_seconds=round(processed.shape[0] / float(sr), 4),
        boundary_discontinuity_before=before_metrics["discontinuity"],
        boundary_discontinuity_after=after_metrics["discontinuity"],
        energy_continuity_db=after_metrics["energy_delta_db"],
        spectral_similarity=after_metrics["spectral_similarity"],
        phase_correlation=after_metrics["phase_correlation"],
        leading_silence_sec=after_lead,
        trailing_silence_sec=after_trail,
        click_risk_before=before_metrics["click_risk"],
        click_risk_after=after_metrics["click_risk"],
        crossfade_ms=applied_xfade_ms,
        fade_applied=fade_applied,
        convincing=convincing,
        warnings=warnings,
    )
    return processed, report


def evaluate_post_at9_loop(at9_path: Path, sr: int = PS4_ATRAC9_SAMPLE_RATE) -> dict:
    """Decode the final ATRAC9 file independently via FFmpeg and re-evaluate seam quality post-encoding."""
    pcm = decode_to_float64_stereo(at9_path, sample_rate=sr)
    metrics = _seam_metrics(pcm, sr)
    lead_sil, trail_sil = _detect_silence_margins(pcm, sr)
    convincing = (
        metrics["click_risk"] <= 0.14
        and abs(metrics["energy_delta_db"]) <= 7.5
        and metrics["spectral_similarity"] >= 0.50
    )
    return {
        "evaluated_after_atrac9": True,
        "decoded_samples": int(pcm.shape[0]),
        "decoded_duration_sec": round(pcm.shape[0] / float(sr), 4),
        "leading_silence_sec": lead_sil,
        "trailing_silence_sec": trail_sil,
        "convincing": convincing,
        **metrics,
    }


def render_loop_seam_preview_wav(
    pcm: np.ndarray,
    dest: Path,
    *,
    sr: int = PS4_ATRAC9_SAMPLE_RATE,
    window_sec: float = 3.0,
    repeats: int = 2,
) -> Path:
    """Render a short WAV showcasing the loop seam (tail -> head repeated) for instant listening."""
    arr = np.asarray(pcm, dtype=np.float64)
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=1)
    n = arr.shape[0]
    w = min(int(round(sr * window_sec)), max(1, n // 2))
    tail = arr[-w:]
    head = arr[:w]
    seam_cycle = np.concatenate([tail, head], axis=0)
    # Repeat tail->head so the user hears the actual loop boundary clearly
    if repeats > 1 and n <= int(sr * window_sec * 2.5):
        out = np.tile(arr, (repeats, 1))
    else:
        out = seam_cycle
    return write_pcm16_wav(out, dest, sample_rate=sr)
