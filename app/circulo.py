"""Search natural cuts only. Everything displayed advances along the source clock."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from . import dds, ouroboros_media as media, sentinela
from .config import ANIMATED_SIZES, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for

APPROVED = "LOOP NATURAL APROVADO"
CONDITIONAL = "LOOP NATURAL CONDICIONAL"
REBUILD = "RECONSTRUÇÃO NECESSÁRIA"
UNSUITABLE = "MATERIAL INADEQUADO"
ORDER = [APPROVED, CONDITIONAL, REBUILD, UNSUITABLE]


def assess(frames, fps, source_risks=()):
    data = sentinela.describe(frames)
    diag = sentinela.diagnose(data)
    wrap = sentinela.describe(np.stack([frames[-2], frames[-1], frames[0], frames[1]]))
    delta = data["difference"]
    typical = float(np.median(delta))
    seam = float(wrap["difference"][1])
    tile_base = np.quantile(data["changes"], 0.75, axis=0)
    local_excess = float(np.max((wrap["changes"][1] - tile_base) / (tile_base + 5)))
    visual = max(0.0, seam - np.quantile(delta, 0.75)) / (typical + 1.0)
    before, join, after = wrap["vectors"].astype(float)
    directions = []
    speeds = []
    headings = []
    for adjacent in (before, after):
        a = np.linalg.norm(adjacent, axis=-1)
        b = np.linalg.norm(join, axis=-1)
        moving = (a + b) > 0
        if moving.any():
            speeds.append(float(np.mean(abs(a - b)[moving] / np.maximum(1, np.maximum(a, b))[moving])))
        tracked = (a * b) > 0
        if tracked.any():
            cosine = (adjacent * join).sum(axis=-1)[tracked] / (a * b)[tracked]
            directions.append(float(np.mean(cosine < 0)))
            headings.append(float(np.mean(1 - np.clip(cosine, -1, 1))))
    heading = max(headings, default=0.0)
    reverse = max(directions, default=0.0)
    speed = max(speeds, default=0.0)
    light = float(abs(wrap["light"][1]))
    light_jump = light > max(4.0, 2.8 * max(abs(data["light"])))
    # A true stationary light pulse can dwell at an extremum; require a trajectory
    # before interpreting an almost identical pair as a motion freeze.
    structural = "A" in diag["categories"]
    repeated = np.array_equal(frames[-1], frames[0])
    frozen = structural and (repeated or (typical > 1 and (seam < 0.15 * typical or bool(data["freezes"]))))
    problems = list(source_risks)
    if diag["turns"] or reverse > 0.42:
        problems.append("Reversão de trajetória.")
    if diag["cuts"] or 1 in wrap["cuts"]:
        problems.append("Corte estrutural.")
    if frozen:
        problems.append("Quadros repetidos em movimento.")
    if visual > 1.1 or local_excess > 2.2:
        problems.append("Salto visual na junção.")
    if light_jump:
        problems.append("Salto de luminosidade.")
    color_jump = float(wrap["chroma"][1]) > max(5.0, 2.8 * np.quantile(data["chroma"], 0.95))
    if color_jump:
        problems.append("Salto de cor na junção.")
    if structural and heading > 0.78:
        problems.append("Mudança brusca de orientação na junção.")
    if structural and speed > 0.72:
        problems.append("Ruptura de velocidade.")
    # Continuity of a changing light is assessed on its own derivative.
    lum_slope = float(abs(wrap["light"][1] - (wrap["light"][0] + wrap["light"][2]) / 2))
    if diag["photometric"] and lum_slope > max(2.2, 1.45 * np.quantile(abs(data["light"]), 0.85)):
        problems.append("A pulsação muda de fase na junção.")
    if "E" in diag["categories"]:
        verdict = UNSUITABLE
    elif problems:
        verdict = REBUILD
    elif (
        diag["transientRisk"]
        or local_excess > 0.9
        or speed > 0.4
        or visual > 0.35
        or fps < 8
        or heading > 0.32
        or (structural and not directions)
    ):
        verdict = CONDITIONAL
    elif (
        not structural
        and not diag["periodSamples"]
        and (typical < 1.4 or not (np.min(data["light"]) < -0.2 and np.max(data["light"]) > 0.2))
    ):
        verdict = CONDITIONAL
    else:
        verdict = APPROVED
    if verdict == CONDITIONAL:
        problems.append("Cadência, deslocamento ou evidência periódica pedem inspeção visual.")
    if verdict == UNSUITABLE:
        problems.append("Evidência insuficiente de movimento.")
    return {
        "verdict": verdict,
        "issues": list(dict.fromkeys(problems)),
        "reason": "A volta acompanha as transições internas." if verdict == APPROVED else problems[0],
        "visualCost": round(float(visual), 4),
        "temporalCost": round(heading + reverse + speed + lum_slope / (4 + typical), 4),
        "oppositeFraction": round(reverse, 4),
        "speedChange": round(speed, 4),
        "headingChange": round(heading, 4),
        "seamDifference": round(seam, 3),
        "internalDifference": round(typical, 3),
        "directionMeasured": bool(directions),
        "confidence": "forte"
        if verdict == APPROVED and (diag["coherence"] > 0.7 or diag["periodSamples"])
        else "limitada",
    }


def seeds(frames, offset, step, size, source_fps):
    """Bounded lag bank per resolution; linear in the number of surveyed instants."""
    cap = max_frames_for(size)
    desc = np.asarray(frames[:, ::3, ::3], dtype=np.float32)
    rates = sorted({min(source_fps, r) for r in (8, 10, 12, 16)})
    lengths = sorted(
        {
            max(3, round(n / r / step))
            for r in rates
            for n in (6, 8, 10, 12, 16, 20, 24, 32, 40, 48, cap)
            if n <= cap
        }
    )
    min_count = min(6, max(4, int(len(desc) * step * source_fps) - 1))
    options = []
    activity = np.r_[0.0, np.cumsum(np.abs(np.diff(desc, axis=0)).mean(axis=(1, 2, 3)))]
    for lag in lengths:
        length = lag * step
        if lag + 2 > len(desc):
            continue
        # Compare both the natural successor and its forward neighborhood.
        dist = np.abs(desc[:-lag] - desc[lag:]).mean(axis=(1, 2, 3))
        # At the end of the recording only the preceding neighborhood exists.
        # This keeps a cut ending exactly at the last source interval eligible.
        middle = (dist[:-1] + dist[1:]) / 2
        cost = np.r_[middle, dist[-1], dist[-2:].mean()]
        active = (activity[lag - 1 :] - activity[: len(cost)]) / (lag - 1)
        cost = cost + np.where(active < 0.35, 80.0, 0.0)
        for rate in rates:
            count = round(length * rate)
            if not min_count <= count <= cap or length / count < 0.02 or length / count > 5:
                continue
            # Preserve candidates from different parts of the recording.
            for segment in np.array_split(np.arange(len(cost)), min(6, len(cost))):
                remaining = segment
                for _ in range(3):
                    if not len(remaining):
                        break
                    i = int(remaining[cost[remaining].argmin()])
                    score = float(cost[i]) + 0.5 * abs(rate - 10) / 10 + 0.4 * (1 - count / cap)
                    options.append(
                        (score, round(offset + i * step, 6), round(offset + (i + lag) * step, 6), count)
                    )
                    remaining = remaining[np.abs(remaining - i) * step >= max(0.25, length * 0.25)]
    if not options:
        duration = len(desc) * step
        count = min(cap, round(duration * source_fps))
        if count >= 4 and 0.02 <= duration / count <= 5:
            options.append((0.0, offset, offset + duration, count))
    return options


def diverse(options, limit):
    chosen = []
    for option in sorted(options):
        _, a, b, _ = option
        if all(max(abs(a - x), abs(b - y)) >= max(0.24, min(b - a, y - x) * 0.22) for _, x, y, _ in chosen):
            chosen.append(option)
            if len(chosen) == limit:
                break
    return chosen


def rank(item):
    q = item["quality"]
    return ORDER.index(q["verdict"]), q["visualCost"] + q["temporalCost"], -item["frames"]


def search(path, duration, folder, progress, check):
    diagnosis, overview = sentinela.survey(path, duration, folder, progress, check)
    duration = diagnosis["source"]["duration"]
    windows = [(overview, 0.0, diagnosis["step"])]
    fps = diagnosis["source"]["sampleFps"] or diagnosis["source"]["fps"] or 10.0
    # If the overview is too sparse for short cuts, inspect distributed windows
    # plus the strongest visual change. Record their actual coverage.
    if diagnosis["step"] > 0.16:
        span = min(6.0, duration)
        change = np.abs(np.diff(overview[:, ::4, ::4].astype(float), axis=0)).mean(axis=(1, 2, 3))
        peak = np.clip(int(change.argmax()) * diagnosis["step"] - span / 2, 0.0, duration - span)
        for start in sorted({float(peak), *np.linspace(0, duration - span, 8)}):
            n = max(4, min(72, int(span * fps)))
            windows.append(
                (
                    media.extract(path, start, start + span, n, sentinela.SIZE, folder, check),
                    start,
                    span / n,
                )
            )
    result = []
    evaluations = {}
    for si, size in enumerate(ANIMATED_SIZES):
        options = []
        for frames, offset, step in windows:
            check()
            options.extend(seeds(frames, offset, step, size, fps))
        shortlist = diverse(options, 18)
        group = []
        for idx, (_, a, b, n) in enumerate(shortlist):
            progress(
                f"Verificando cortes em {size[0]}×{size[1]}",
                24 + si * 24 + int(23 * idx / max(1, len(shortlist))),
            )
            check()
            # Different resolutions can independently select the same source clock.
            # Reuse only the source measurement. DDS inspection uses the real size.
            wait = float(f"{(b - a) / n:.6g}")
            recipe = a, b, n
            if recipe not in evaluations:
                try:
                    sampled = media.extract(path, a, b, n, sentinela.SIZE, folder, check)
                except media.MotionError:
                    evaluations[recipe] = None
                else:
                    risks = []
                    if any(a < t <= b - (b - a) / n for t in diagnosis["turnSeconds"]):
                        risks.append("Retorno estrutural observado na fonte.")
                    if any(a < t <= b - (b - a) / n for t in diagnosis["cutSeconds"]):
                        risks.append("Corte observado na fonte.")
                    evaluations[recipe] = assess(sampled, 1 / wait, risks)
            q = evaluations[recipe]
            if q is None:
                continue
            group.append(
                dict(
                    start=a,
                    end=b,
                    frames=n,
                    size=size,
                    wait=wait,
                    fps=1 / wait,
                    duration=n * wait,
                    estimatedBytes=n * dds_bytes_for(*size),
                    quality=q,
                )
            )
        if shortlist and not group:
            raise media.MotionError(
                "Não foi possível decodificar os cortes desta resolução. Confira a fonte."
            )
        group.sort(key=rank)
        result.append(
            {
                "size": size,
                "maxFrames": max_frames_for(size),
                "candidates": group[:3],
                "evaluated": len(group),
                "unreadable": len(shortlist) - len(group),
                "verdict": group[0]["quality"]["verdict"] if group else UNSUITABLE,
            }
        )
    return {
        "sentinela": diagnosis,
        "groups": result,
        "windows": [[round(start, 5), round(start + len(fs) * dt, 5)] for fs, start, dt in windows],
    }


def preview(path, candidate, folder, progress, check):
    size = tuple(candidate["size"])
    n = candidate["frames"]
    if size not in ANIMATED_SIZES or not 4 <= n <= max_frames_for(size):
        raise media.MotionError("Quadros ou resolução fora do orçamento RAF.")
    samples = media.extract(path, candidate["start"], candidate["end"], n, size, folder, check)
    proxies = []
    total = 0
    for i, f in enumerate(samples):
        check()
        progress("Codificando e decodificando os DDS canônicos", 10 + int(85 * i / n))
        blob = dds.encode_frame(np.asarray(Image.fromarray(f).convert("RGBA")))
        total += len(blob)
        with Image.open(io.BytesIO(blob)) as frame:
            rgb = frame.convert("RGB")
            rgb.save(folder / f"{i:02d}.webp", "WEBP", lossless=True, method=0)
            proxies.append(np.asarray(rgb.resize(sentinela.SIZE, Image.Resampling.BOX)))
    if total != candidate["estimatedBytes"] or total > RAF_SIZE_LIMIT or n > max_frames_for(size):
        raise media.MotionError("Os DDS excederam o orçamento da cena.")
    return {
        "frames": n,
        "wait": candidate["wait"],
        "bytes": total,
        "size": size,
        "quality": assess(
            np.stack(proxies),
            candidate["fps"],
            [r for r in candidate["quality"]["issues"] if "na fonte" in r],
        ),
        "sourceVerdict": candidate["quality"]["verdict"],
        "type": "DDS canônico decodificado",
    }
