"""Bounded temporal search and final, source-clocked RAF rendering."""

import hashlib
import io
import json
import math
import os
import subprocess
import time
import zipfile
from fractions import Fraction
from xml.etree import ElementTree as ET

import cv2 as cv
import numpy as np
from PIL import Image

from . import compositor, dds, media, theme
from . import motion_metrics as quality
from .config import RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from .motion_model import Clip, PROFILES

VERSION = "motion-1.0"


class MotionError(Exception):
    pass


class Cancelled(Exception):
    pass


def digest(path, check):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            check()
            result.update(chunk)
    return result.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def surface_key(surface):
    return fingerprint(surface.model_dump() if surface and surface.layers else None)


def command(argv, check, timeout=75):
    with subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as proc:
        deadline = time.monotonic() + timeout
        try:
            while True:
                check()
                try:
                    output, _ = proc.communicate(timeout=0.15)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise MotionError(
                            "A leitura do trecho excedeu o tempo disponível. Tente um trecho menor."
                        )
            if proc.returncode:
                raise MotionError("O decodificador não conseguiu ler o trecho da mídia.")
            return output
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def source_info(job, check):
    raw = command(
        [
            media.FFPROBE,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-select_streams",
            "v:0",
            "-read_intervals",
            "%+#240",
            "-show_frames",
            "-show_entries",
            "stream=width,height,avg_frame_rate,codec_name:frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(job.source),
        ],
        check,
    )
    data = json.loads(raw)
    stream = data["streams"][0]
    pts = [
        float(f["best_effort_timestamp_time"])
        for f in data.get("frames", [])
        if "best_effort_timestamp_time" in f
    ]
    steps = np.diff(pts)
    steps = steps[steps > 0]
    try:
        declared = float(Fraction(stream.get("avg_frame_rate", "0")))
    except (ZeroDivisionError, ValueError):
        declared = 0.0
    measured = float(1 / np.median(steps)) if len(steps) else None
    return {
        "name": job.original_name,
        "duration": job.meta["duration"],
        "width": stream["width"],
        "height": stream["height"],
        "codec": stream["codec_name"],
        "fps": declared or measured,
        "sampleFps": measured,
        "timestamps": pts,
        "irregular": bool(len(steps) and np.max(np.abs(steps / np.median(steps) - 1)) > 0.08),
    }


def extract(source, start, end, count, size, folder, check):
    """Only the selected window is decoded. Output is a bounded disk-backed RGB array."""
    if not 0 <= start < end or count < 1:
        raise MotionError("Intervalo de amostragem inválido.")
    path = folder / "samples.rgb"
    fps = count / (end - start)
    # Decode the frame already on screen at the in-point. Seeking directly to
    # a fractional in-point can duplicate the first post-seek frame with fps.
    seek = max(0.0, start - max(1.0, 1 / fps))
    vf = (
        f"setpts=PTS-{start - seek:.10f}/TB,fps={fps:.10f}:start_time=0:round=up,"
        f"{media.scale_crop_filter(*size)}"
    )
    command(
        [
            media.FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-threads",
            "2",
            "-filter_threads",
            "1",
            "-protocol_whitelist",
            "file",
            "-ss",
            f"{seek:.8f}",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-vf",
            vf,
            "-frames:v",
            str(count),
            "-fps_mode",
            "passthrough",
            "-pix_fmt",
            "rgb24",
            "-threads:v",
            "1",
            "-f",
            "rawvideo",
            str(path),
        ],
        check,
    )
    if path.stat().st_size != count * size[0] * size[1] * 3:
        path.unlink(missing_ok=True)
        raise MotionError("Faltam quadros decodificáveis no fim do trecho. Antecipe a saída.")
    samples = np.memmap(path, dtype=np.uint8, mode="r", shape=(count, size[1], size[0], 3))
    path.unlink()  # Linux keeps mapped pages until the caller releases the array.
    return samples


def clip_options(start, end, source_fps):
    duration = end - start
    options = {}
    for name, (size, target_fps) in PROFILES.items():
        if name == "long" and duration * min(target_fps, source_fps) > max_frames_for(size):
            size = (640, 360)
        n = min(max_frames_for(size), round(duration * min(target_fps, source_fps)))
        if n >= 3 and 0.02 <= duration / n <= 5:
            options[name] = Clip(start=start, end=end, size=size, frames=n, profile=name)
    return options


def thumbnail(rgb, path):
    Image.fromarray(rgb).save(path, "WEBP", quality=82, method=2)


def search(job, spec, folder, progress, check):
    info = source_info(job, check)
    duration = info["duration"]
    rate = min(12.0, info["fps"] or 12.0)
    window = min(6.0, duration)
    count = min(72, math.floor(window * rate + 1e-5))
    if count < 4:
        raise MotionError("A fonte tem poucos instantes distintos para comparar uma junção.")
    step = window / count
    starts = np.linspace(0, max(0.0, duration - window), min(4, math.ceil(duration / window)))
    shortlist, marks, windows = [], [], []
    wanted = "smooth" if spec.profile == "custom" else spec.profile
    for wi, offset in enumerate(starts):
        progress("Analisando janelas da fonte", 8 + int(wi * 35 / len(starts)))
        try:
            frames = extract(job.source, offset, offset + window, count, quality.PROXY, folder, check)
        except MotionError:
            if wi == 0 and len(starts) == 1:
                raise
            continue
        windows.append([round(offset, 5), round(offset + window, 5)])
        small = np.stack([cv.resize(f, (16, 9), interpolation=cv.INTER_AREA) for f in frames]).astype(
            np.float32
        )
        differences = np.abs(np.diff(small, axis=0)).mean(axis=(1, 2, 3))
        cuts = differences > max(27, 4 * float(np.median(differences)))
        for i in range(0, count, max(1, count // 12)):
            name = f"thumb-{len(marks):02d}.webp"
            thumbnail(frames[i], folder / name)
            marks.append(
                {
                    "t": round(offset + i * step, 5),
                    "file": name,
                    "motion": round(float(differences[max(0, i - 1)]), 2),
                    "cut": bool(i and cuts[max(0, i - max(1, count // 12)) : i].any()),
                }
            )
        for i in range(count - 3):
            for j in range(i + max(3, round(0.55 / step)), count - 1):
                # j is the unused natural successor of j-1. Compare three
                # neighborhoods, then verify the actual displayed wrap by flow.
                if cuts[i:j].any():
                    continue
                if spec.strategy == "texture":
                    period = j - i
                    neighbor = j if j + period <= count else i - period
                    if (
                        neighbor < 0
                        or not quality.recurrence(small[i:j], small[neighbor : neighbor + period])[
                            "supported"
                        ]
                    ):
                        continue
                length = (j - i) * step
                options = clip_options(
                    round(offset + i * step, 6), round(offset + j * step, 6), info["fps"] or rate
                )
                if wanted not in options:
                    continue
                clip = options[wanted].model_copy(update={"strategy": spec.strategy})
                fps = 1 / clip.wait
                if fps < min(8, (info["fps"] or rate) * 0.8):
                    continue
                match = (
                    np.abs(small[i : i + 2] - small[j : j + 2]).mean()
                    + 0.45 * np.abs((small[j] - small[j - 1]) - (small[i + 1] - small[i])).mean()
                )
                cadence = max(0, 1 - fps / min(PROFILES[wanted][1], info["fps"] or rate))
                preference = 1 / length if wanted == "long" else 0.0
                shortlist.append((float(match) + 4 * cadence + preference, clip))
    shortlist.sort(key=lambda pair: pair[0])
    finalists = []
    for _, clip in shortlist:
        if all(
            abs(clip.start - other.start) + abs(clip.end - other.end) > max(0.25, 3 * step)
            for other in finalists
        ):
            finalists.append(clip)
        if len(finalists) >= 6:
            break
    candidates = []
    for i, clip in enumerate(finalists):
        progress("Conferindo direção, cadência e DXT1a", 45 + int(i * 50 / len(finalists)))
        samples = list(extract(job.source, clip.start, clip.end, clip.frames, quality.PROXY, folder, check))
        metrics = quality.evaluate(samples)
        compressed = [decode_dds(dds.encode_frame(cv.cvtColor(f, cv.COLOR_RGB2RGBA))) for f in samples]
        after = quality.evaluate(compressed)
        candidates.append(
            {
                "clip": clip.model_dump(),
                "quality": metrics,
                "proxyDds": after,
                "alternatives": [
                    c.model_dump() for c in clip_options(clip.start, clip.end, info["fps"] or rate).values()
                ],
            }
        )
    ranks = {"candidato": 0, "revisar": 1, "inadequado": 2}

    def ranking(c):
        clip = c["clip"]
        length = clip["end"] - clip["start"]
        cost = c["quality"]["cost"] + 0.35 * c["proxyDds"]["cost"]
        if wanted == "long":
            cost += 0.12 / length
        elif wanted == "smooth":
            cost += 0.1 * max(0.0, 1 - clip["frames"] / length / min(24, info["fps"] or 24))
        return max(ranks[c[k]["status"]] for k in ("quality", "proxyDds")), cost

    candidates.sort(key=ranking)
    return {
        "type": "analysis",
        "source": info,
        "windows": windows,
        "marks": marks,
        "candidates": candidates,
        "profile": spec.profile,
        "strategy": spec.strategy,
    }


def decode_dds(blob):
    with Image.open(io.BytesIO(blob)) as image:
        if image.convert("RGBA").getchannel("A").getextrema() != (255, 255):
            raise MotionError("O DDS do fundo contém transparência inesperada.")
        return np.array(image.convert("RGB"))


def lossless(rgb, path):
    Image.fromarray(rgb).save(path, "WEBP", lossless=True, method=0)


def candidate_series(proxies, clip):
    delta = quality.correction(proxies, clip.protect)
    tail = min(5, (clip.frames - 1) // 2)
    weights = [max(0.0, (i - (clip.frames - 1 - tail)) / tail) ** 2 for i in range(clip.frames)]
    trial = [quality.warp(f, delta, w) if w else f for f, w in zip(proxies, weights)]
    if quality.improved(quality.evaluate(proxies), quality.evaluate(trial)):
        return delta, weights
    return None, [0.0] * clip.frames


def detail(rgb):
    g = quality.gray(rgb)
    grad = quality.gradient(g)
    # Indicators only: gradients alone cannot diagnose banding or double edges.
    return float(grad.mean()), float(np.mean((grad > 0) & (grad < 9)))


def edge_risks(first, second):
    """Reference-relative alarms. An additional nearby edge can be a ghost."""
    size = (min(first.shape[1], 480), min(first.shape[0], 270))
    a, b = (quality.gray(cv.resize(x, size, interpolation=cv.INTER_AREA)) for x in (first, second))
    aa, bb = (cv.GaussianBlur(im, (3, 3), 0.8) for im in (a, b))
    edges = cv.Canny(aa, 28, 68)
    new_edges = cv.Canny(bb, 28, 68) > 0
    distances = cv.distanceTransform(255 - edges, cv.DIST_L2, 3)
    # Moving a contour moves its only edge. Count a possible double contour
    # only where the primary survives and a new edge of the same polarity
    # appears within a few pixels along its normal.
    survives = (edges > 0) & new_edges
    extra = new_edges & (distances > 1.6) & (distances < 5.0)
    echoes = np.zeros_like(extra)
    gx, gy = cv.Sobel(bb, cv.CV_32F, 1, 0), cv.Sobel(bb, cv.CV_32F, 0, 1)
    mag = np.maximum(1.0, cv.magnitude(gx, gy))
    gx, gy = gx / mag, gy / mag
    for dy, dx in ((0, 3), (3, 0), (2, 2), (2, -2)):
        along_normal = np.abs(gx * dx + gy * dy) > 0.8 * math.hypot(dx, dy)
        for sign in (-1, 1):
            shift = (sign * dy, sign * dx)
            same = gx * np.roll(gx, shift, (0, 1)) + gy * np.roll(gy, shift, (0, 1)) > 0.92
            echoes |= extra & along_normal & same & np.roll(survives, shift, (0, 1))
    echoes = float(echoes[5:-5, 5:-5].sum() / max(1, np.count_nonzero(edges[5:-5, 5:-5])))
    ga, gb = quality.gradient(a), quality.gradient(b)
    slopes = (ga > 3) & (ga < 24)
    plateaus = float(np.count_nonzero(slopes & (gb < 1)) / max(1, slopes.sum()))
    return echoes, plateaus


def build(job, spec, folder, progress, check):
    from .main import _write_raf_export

    clip = spec.clip
    progress("Amostrando o trecho na resolução RAF", 8)
    samples = extract(job.source, clip.start, clip.end, clip.frames, clip.size, folder, check)
    lossless(samples[0], folder / "base.webp")
    small = [quality.proxy(rgb) for rgb in samples]
    source_quality = quality.evaluate(small)
    recurrence = None
    if clip.strategy == "texture":
        period = clip.end - clip.start
        neighbor = clip.end if clip.end + period <= job.meta["duration"] else clip.start - period
        if neighbor >= 0:
            repeated = extract(
                job.source, neighbor, neighbor + period, clip.frames, quality.PROXY, folder, check
            )
            recurrence = quality.recurrence(small, repeated)
        else:
            recurrence = {"supported": False, "tileAgreement": None, "movingTiles": 0}
    delta, weights = None, [0.0] * clip.frames
    if clip.strategy == "align":
        progress("Testando o alinhamento local", 16)
        delta, weights = candidate_series(small, clip)
    overlay = (
        compositor.render(compositor.workspace(), "animated", spec.surface, clip.size)
        if spec.surface and spec.surface.layers
        else None
    )
    cut_small, cut_dds, trial_small, trial_dds, texture, warp_edges = [], [], [], [], [], []
    for i, raw in enumerate(samples):
        check()
        progress("Compondo e comparando os DDS", 20 + int(65 * i / clip.frames))

        def encode(rgb, label):
            rgba = cv.cvtColor(rgb, cv.COLOR_RGB2RGBA)
            if overlay is not None:
                rgba = np.asarray(Image.alpha_composite(Image.fromarray(rgba), overlay))
            blob = dds.encode_frame(rgba)
            decoded = decode_dds(blob)
            lossless(rgba[:, :, :3], folder / f"{label}-{i:02d}.webp")
            (folder / f"{label}-{i:02d}.dds").write_bytes(blob)
            return quality.proxy(rgba), quality.proxy(decoded)

        a, b = encode(raw, "cut")
        cut_small.append(a)
        cut_dds.append(b)
        if weights[i] and delta is not None:
            c, d = encode(quality.warp(raw, delta, weights[i]), "trial")
            with (
                Image.open(folder / f"trial-{i:02d}.webp") as trial,
                Image.open(folder / f"cut-{i:02d}.webp") as baseline,
            ):
                warp_edges.append(
                    edge_risks(np.asarray(baseline.convert("RGB")), np.asarray(trial.convert("RGB")))[0]
                )
        else:
            c, d = a, b
        trial_small.append(c)
        trial_dds.append(d)
    cut, encoded_cut = quality.evaluate(cut_small), quality.evaluate(cut_dds)
    optimized, encoded = quality.evaluate(trial_small), quality.evaluate(trial_dds)
    refined = (
        delta is not None
        and max(warp_edges, default=0) <= 0.04
        and quality.improved(cut, optimized)
        and quality.improved(encoded_cut, encoded)
    )
    if not refined:
        optimized, encoded = cut, encoded_cut

    def selected():
        for i in range(clip.frames):
            check()
            label = "trial" if refined and weights[i] else "cut"
            source = folder / f"{label}-{i:02d}.webp"
            os.link(source, folder / f"opt-{i:02d}.webp")
            blob = (folder / f"{label}-{i:02d}.dds").read_bytes()
            rgb = decode_dds(blob)
            lossless(rgb, folder / f"raf-{i:02d}.webp")
            with Image.open(source) as before:
                orig = np.asarray(before.convert("RGB"))
                sharp, flat = detail(orig)
                final_sharp, final_flat = detail(rgb)
                texture.append(
                    [
                        quality.mean_abs(orig, rgb),
                        final_sharp / max(0.1, sharp),
                        final_flat - flat,
                        *edge_risks(orig, rgb),
                    ]
                )
            yield i, blob

    progress("Gerando a cena e verificando o ZIP", 88)
    _, written, _ = _write_raf_export(
        job, folder, clip.frames, clip.size, clip.wait, "native", encoded=selected()
    )
    raf_dir = folder / "background.raf"
    names = [theme.frame_name(i) for i in range(clip.frames)]
    prefix = "scene/background.raf/"
    with zipfile.ZipFile(folder / "background.raf.zip") as archive:
        members = [*names, "plane.mdx", "index.xml"]
        if sorted(archive.namelist()) != sorted(prefix + n for n in members):
            raise MotionError("A estrutura do ZIP gerado divergiu da cena.")
        hashes = {}
        for name in members:
            check()
            content = archive.read(prefix + name)
            if content != (raf_dir / name).read_bytes():
                raise MotionError("O conteúdo do ZIP divergiu dos recursos conferidos.")
            hashes[name] = hashlib.sha256(content).hexdigest()
        actual = sum(archive.getinfo(prefix + name).file_size for name in names)
        xml = ET.fromstring(archive.read(prefix + "index.xml"))
    theme.check_frame_count(written)
    theme.check_budget(actual)
    waits = [float(e.attrib["wait"]) for e in xml.findall("./sequence/case")]
    if actual != clip.frames * dds_bytes_for(*clip.size) or waits != [clip.wait] * clip.frames:
        raise MotionError("A cena gerada divergiu da cadência ou do orçamento confirmado.")
    for pattern in ("cut-*.dds", "trial-*.dds", "trial-*.webp"):
        for path in folder.glob(pattern):
            path.unlink()
    eligible = (
        all(q["status"] != "inadequado" for q in (source_quality, optimized, encoded))
        and (not recurrence or recurrence["supported"])
        and any(not np.array_equal(small[0], f) for f in small[1:])
    )
    compression = dict(
        zip(
            ("rgbError", "sharpnessRatio", "weakGradientChange", "nearbyEdges", "gradientPlateaus"),
            np.mean(texture, axis=0).round(4).tolist(),
        )
    )
    compression["temporalErrorSwing"] = round(float(np.ptp(np.asarray(texture)[:, 0])), 3)
    return {
        "type": "render",
        "clip": clip.model_dump(),
        "surfaceKey": surface_key(spec.surface),
        "refined": refined,
        "eligible": eligible,
        "wait": waits[0],
        "duration": sum(waits),
        "frames": written,
        "ddsBytes": actual,
        "estimatedBytes": dds_bytes_for(*clip.size) * written,
        "limit": RAF_SIZE_LIMIT,
        "quality": {
            "source": source_quality,
            "cut": cut,
            "cutDds": encoded_cut,
            "optimized": optimized,
            "raf": encoded,
        },
        "compression": compression,
        "recurrence": recurrence,
        "sampleTimes": [
            round(clip.start + i * (clip.end - clip.start) / clip.frames, 8) for i in range(clip.frames)
        ],
        "zipBytes": (folder / "background.raf.zip").stat().st_size,
        "zipSha256": digest(folder / "background.raf.zip", check),
        "sceneHashes": hashes,
    }
