"""Temporal diagnosis on chronological RGB proxies, using only NumPy and Pillow."""

from __future__ import annotations

import numpy as np
from PIL import Image

SIZE = (96, 54)
GRID = (6, 6)
SHIFTS = [(x, y) for y in range(-4, 5) for x in range(-4, 5)]
ZERO = SHIFTS.index((0, 0))


def proxies(frames):
    return np.stack([np.asarray(Image.fromarray(f).resize(SIZE, Image.Resampling.BOX)) for f in frames])


def tiles(gray):
    n, h, w = gray.shape
    return gray.reshape(n, GRID[0], h // GRID[0], GRID[1], w // GRID[1]).transpose(0, 1, 3, 2, 4)


def register(a, b):
    """Track textured tiles by minimum local contrast error, in source→destination order."""
    n, h, w = a.shape
    patches = tiles(a)[:, :, :, 2:-2, 3:-3]
    centered = patches - patches.mean(axis=(-2, -1), keepdims=True)
    padded = np.pad(b, ((0, 0), (4, 4), (4, 4)), mode="edge")
    costs = []
    for x, y in SHIFTS:
        other = tiles(padded[:, 4 + y : 4 + y + h, 4 + x : 4 + x + w])[:, :, :, 2:-2, 3:-3]
        other = other - other.mean(axis=(-2, -1), keepdims=True)
        costs.append(np.abs(centered - other).mean(axis=(-2, -1)))
    costs = np.stack(costs, axis=-1)
    best = costs.argmin(axis=-1)
    error = costs.min(axis=-1)
    zero = costs[..., ZERO]
    # A shift that does not clearly improve the stationary match is unobserved.
    reliable = (patches.std(axis=(-2, -1)) > 2.2) & (zero - error > 0.9) & (error < zero * 0.74)
    vectors = np.asarray(SHIFTS, dtype=np.int8)[best] * reliable[..., None]
    return vectors, error


def describe(frames, check=lambda: None):
    rgb = np.asarray(frames)
    if len(rgb) < 4 or rgb.shape[1:] != (SIZE[1], SIZE[0], 3):
        raise ValueError("A análise requer quatro ou mais amostras RGB na resolução de diagnóstico.")
    gray = np.asarray(Image.fromarray(rgb[0]).convert("L"))  # same luminance weights throughout
    gray = np.stack([gray, *(np.asarray(Image.fromarray(f).convert("L")) for f in rgb[1:])]).astype(
        "float32"
    )
    vec, residual = [], []
    for i in range(0, len(rgb) - 1, 12):
        check()
        stop = min(i + 12, len(rgb) - 1)
        v, e = register(gray[i:stop], gray[i + 1 : stop + 1])
        vec.extend(v)
        residual.extend(e)
    vec, residual = np.asarray(vec), np.asarray(residual)
    delta = np.abs(np.diff(gray, axis=0))
    light = np.diff(gray.mean(axis=(1, 2)))
    rgb_mean = rgb.mean(axis=(1, 2))
    chroma = rgb_mean - rgb_mean.mean(axis=1, keepdims=True)
    movement = np.any(vec != 0, axis=-1)
    # Sparse objects must retain a vote against the stationary majority.
    changes = tiles(delta).mean(axis=(-2, -1))
    difference = delta.mean(axis=(1, 2))
    cuts = np.flatnonzero(
        (residual.mean(axis=(1, 2)) > max(12, 3.5 * np.median(residual)))
        & (difference > max(18, 2.8 * np.median(difference)))
    )
    freezes = np.flatnonzero(difference < 0.25) if difference.max() > 1.2 else np.array([], int)
    return dict(
        gray=gray,
        vectors=vec,
        changes=changes,
        residual=residual,
        light=light,
        chroma=np.linalg.norm(np.diff(chroma, axis=0), axis=1),
        moving=movement,
        cuts=cuts.tolist(),
        freezes=freezes.tolist(),
        difference=difference,
    )


def turns(data):
    """Compare a direct run with the opposite side of a possible turnaround."""
    g, v = data["gray"], data["vectors"].astype(float)
    events = []
    for pivot in range(2, len(g) - 2):
        for hold in (0, 1):
            span = min(5, pivot, len(g) - 1 - pivot - hold)
            if span < 2:
                continue
            past, future = v[pivot - span : pivot], v[pivot + hold : pivot + hold + span][::-1]
            valid = (np.linalg.norm(past, axis=-1) > 0) & (np.linalg.norm(future, axis=-1) > 0)
            opposite = valid & ((past * future).sum(axis=-1) < 0)
            if opposite.sum() < max(3, span) or opposite.any(axis=(1, 2)).sum() < 2:
                continue
            fraction = float(opposite.sum() / valid.sum())
            if fraction < 0.65:
                continue
            # Compare the actual trajectory to its mirrored neighborhood. Local
            # intensity drift cancels when comparing spatially centered patches.
            a = tiles(g[pivot - span : pivot])
            b = tiles(g[pivot + hold + 1 : pivot + hold + span + 1][::-1])
            diff = a - b
            mirrored = np.abs(diff - diff.mean(axis=(-2, -1), keepdims=True)).mean(axis=(-2, -1))
            normal = data["changes"][pivot - span : pivot]
            ratio = float(mirrored[opposite].mean() / max(1.0, normal[opposite].mean()))
            events.append(
                {
                    "index": pivot,
                    "opposition": round(fraction, 3),
                    "mirrorRatio": round(ratio, 3),
                    "mirrored": ratio < 0.75,
                    "tiles": int(opposite.any(axis=0).sum()),
                }
            )
            break
    return events


def diagnose(data):
    move, change = data["moving"], data["changes"]
    active = move.mean(axis=0) > 0.22
    moving_fraction = float(active.mean())
    continuous = np.ones(len(move), dtype=bool)
    continuous[data["cuts"]] = False
    observed = bool(move[continuous].sum() >= max(3, len(move) * 0.5))
    turn = turns(data)
    cuts, freezes = data["cuts"], data["freezes"]
    brightness = data["gray"].mean(axis=(1, 2))
    local_variation = float(change.max(axis=0).max())
    pulse = (
        not cuts
        and not observed
        and local_variation > 2
        and (np.ptp(brightness) > 2 or np.count_nonzero(change.mean(axis=0) > 0.6) > 0)
    )
    transient = bool(
        observed and moving_fraction < 0.45 and np.mean(move.any(axis=0)) > moving_fraction + 0.12
    )
    labels, reasons = [], []
    if observed:
        labels.append("A")
        reasons.append("Deslocamento espacial mensurável em blocos com textura.")
    if pulse or observed and moving_fraction < 0.22:
        labels.append("B")
        reasons.append("Variação de intensidade ou atividade limitada a parte da imagem.")
    if observed and 0.025 < moving_fraction < 0.86:
        labels.append("C")
        reasons.append("Regiões móveis coexistem com um fundo relativamente imóvel.")
    if turn or cuts or transient:
        labels.append("D")
        reasons.append("Retorno estrutural, corte ou trajetória transitória na amostra.")
    if not observed and not pulse and not cuts:
        labels.append("E")
        reasons.append("A amostra oferece pouca evidência de mudança.")
    vector = data["vectors"].sum(axis=(0, 1, 2)).astype(float)
    length = np.linalg.norm(data["vectors"], axis=-1).sum()
    coherence = float(np.linalg.norm(vector) / max(length, 1))
    # Recurrence requires a whole repeated neighborhood, not just endpoints.
    g = data["gray"][:, ::3, ::3]
    typical = max(0.5, float(np.median(data["difference"])))
    lag = None
    if observed or pulse:
        for k in range(3, len(g) // 2 + 1):
            if np.abs(g[k:] - g[:-k]).mean() < typical * 0.65:
                lag = k
                break
    return {
        "categories": labels,
        "reasons": reasons,
        "pingPong": any(e["mirrored"] for e in turn),
        "turns": turn,
        "cuts": cuts,
        "freezes": freezes,
        "photometric": pulse,
        "transientRisk": transient,
        "periodSamples": lag,
        "coherence": round(coherence, 3),
        "directionXY": vector.tolist(),
        "dynamicFraction": round(moving_fraction, 3),
        "activityGrid": move.mean(axis=0).round(2).tolist(),
        "brightnessRange": round(float(np.ptp(brightness)), 3),
        "maxColorStep": round(float(max(data["chroma"])), 3),
        "confidence": "forte"
        if (turn and any(e["mirrorRatio"] < 0.25 for e in turn))
        or coherence > 0.8
        or (not observed and local_variation < 0.4)
        else "limitada",
        "samples": len(g),
    }


def survey(source, duration, folder, progress, check):
    """Uniform overview for the entire source. Preserve its source-time coordinates."""
    from . import ouroboros_media as io

    info = io.source_info(source, duration, check)
    duration = info["duration"]
    n = min(240, int(duration * min(12.0, info["sampleFps"] or info["fps"] or 10.0)))
    if n < 4:
        raise io.MotionError("Há menos de quatro instantes para avaliar a trajetória.")
    progress("Lendo a duração completa em baixa resolução", 8)
    frames = io.extract(source, 0.0, duration, n, SIZE, folder, check)
    progress("Medindo trajetória e simetria temporal", 17)
    report = diagnose(describe(frames, check))
    dt = duration / n
    report.update(
        {
            "step": dt,
            "source": info,
            "coverage": [0.0, duration],
            "turnSeconds": [round(e["index"] * dt, 5) for e in report["turns"]],
            "cutSeconds": [round((i + 1) * dt, 5) for i in report["cuts"]],
        }
    )
    return report, frames
