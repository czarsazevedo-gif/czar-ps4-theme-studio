"""CPU measurements on small RGB proxies, in pixels per displayed frame."""

import cv2 as cv
import numpy as np

cv.setNumThreads(1)
PROXY = (128, 72)


def proxy(rgb):
    return cv.resize(rgb[:, :, :3], PROXY, interpolation=cv.INTER_AREA)


def gray(rgb):
    return cv.cvtColor(rgb, cv.COLOR_RGB2GRAY)


def flow(a, b):
    return cv.calcOpticalFlowFarneback(a, b, None, 0.5, 3, 15, 3, 5, 1.2, 0)


def mean_abs(a, b):
    return float(cv.absdiff(a, b).mean())


def gradient(a):
    return cv.magnitude(cv.Sobel(a, cv.CV_32F, 1, 0), cv.Sobel(a, cv.CV_32F, 0, 1))


def transition(a, b):
    ga, gb = gray(a), gray(b)
    field = flow(ga, gb)
    movement = np.linalg.norm(field, axis=2)
    diff = cv.absdiff(ga, gb)
    # A small subject can teleport on a largely stationary background.
    tiles = cv.resize(diff, (8, 6), interpolation=cv.INTER_AREA)
    hsv_a, hsv_b = (cv.cvtColor(frame, cv.COLOR_RGB2HSV) for frame in (a, b))
    y, x = np.mgrid[: ga.shape[0], : ga.shape[1]].astype(np.float32)
    aligned = cv.remap(
        gb, x + field[..., 0], y + field[..., 1], cv.INTER_LINEAR, borderMode=cv.BORDER_REPLICATE
    )
    residual = mean_abs(ga[4:-4, 4:-4], aligned[4:-4, 4:-4])
    return field, np.array(
        [
            mean_abs(a, b),
            abs(float(ga.mean() - gb.mean())),
            mean_abs(gradient(ga), gradient(gb)) / 4,
            float(tiles.max()),
            abs(float(hsv_a[:, :, 1].mean() - hsv_b[:, :, 1].mean())),
            float(np.mean(movement > 0.32)),
            residual,
        ]
    )


def disagreement(a, b):
    speed_a, speed_b = (np.linalg.norm(f, axis=2) for f in (a, b))
    mask = (speed_a > 0.32) | (speed_b > 0.32)
    if mask.mean() < 0.006:
        return 0.0, 0.0
    both = mask & (speed_a > 0.18) & (speed_b > 0.18)
    cosine = (a * b).sum(axis=2) / np.maximum(speed_a * speed_b, 1e-5)
    reverse = float((cosine[both] < -0.25).mean()) if both.any() else 0.0
    speed = float(
        np.mean(np.abs(speed_a[mask] - speed_b[mask]))
        / max(0.4, np.mean(np.maximum(speed_a[mask], speed_b[mask])))
    )
    return reverse, speed


def evaluate(frames):
    """Measure the wrap independently of the N-1 normal transitions."""
    fields, values = zip(*(transition(a, b) for a, b in zip(frames, frames[1:] + frames[:1])))
    values = np.asarray(values)
    internal, seam = values[:-1], values[-1]
    normal = np.median(internal, axis=0)
    upper = np.quantile(internal, 0.75, axis=0)
    reverse, speed = np.mean([disagreement(fields[-1], f) for f in (fields[0], fields[-2])], axis=0)
    inner_reverse = max(disagreement(a, b)[0] for a, b in zip(fields[:-2], fields[1:-1]))
    # Also compare directional consensus away from the wrap. A sinusoidal
    # reversal can pass through a stationary frame without an abrupt flip.
    vectors = []
    for field in fields[:-1]:
        mag = np.linalg.norm(field, axis=2)
        moving = field[mag > 0.35]
        if len(moving) > mag.size * 0.02:
            v = np.mean(moving / np.maximum(np.linalg.norm(moving, axis=1, keepdims=True), 0.01), axis=0)
            if np.linalg.norm(v) > 0.72:
                vectors.append(v / np.linalg.norm(v))
    opposed = bool(vectors and np.min(np.asarray(vectors) @ np.asarray(vectors).T) < -0.60)
    excess = np.maximum(0, (seam[:5] - upper[:5]) / (normal[:5] + [3, 2, 3, 10, 4]))
    flags = []
    if reverse > 0.46 or inner_reverse > 0.65 or opposed:
        flags.append("inversao")
    if min(values[:, 0]) < 0.35 and max(values[:, 0]) > 1.5:
        flags.append("repeticao")
    if (
        max(values[:, 0]) > max(32, normal[0] * 3.6)
        or excess[0] > 2.2
        or max(values[:, 6]) > max(10, normal[6] * 5)
    ):
        flags.append("corte")
    if max(values[:, 1]) > max(9, normal[1] * 3.8) or np.ptp([gray(f).mean() for f in frames]) > 35:
        flags.append("luminosidade")
    if excess[3] > 1.6 or (speed > 0.70 and seam[3] > normal[3] + 12):
        flags.append("salto")
    if max(values[:, 4]) > max(14, normal[4] * 4):
        flags.append("saturacao")
    if normal[5] < 0.012 or normal[0] < 0.55:
        flags.append("pouca_evidencia")
    image_cost = float(np.average(excess[[0, 2, 3]], weights=[2, 1, 1]))
    cost = image_cost + 1.8 * reverse + 0.85 * speed + 0.6 * excess[1] + 0.25 * excess[4]
    status = (
        "inadequado"
        if any(f != "pouca_evidencia" for f in flags)
        else ("revisar" if flags or cost > 0.92 else "candidato")
    )
    grid = np.mean(
        [cv.resize(np.linalg.norm(f, axis=2), (4, 3), interpolation=cv.INTER_AREA) for f in fields[:-1]],
        axis=0,
    )
    orientation = np.mean(vectors, axis=0) if vectors else np.zeros(2)
    direction = (
        round(float(np.degrees(np.arctan2(orientation[1], orientation[0]))), 1)
        if np.linalg.norm(orientation) > 0.6
        else None
    )
    return {
        "status": status,
        "flags": flags,
        "cost": round(float(cost), 5),
        "image": round(image_cost, 4),
        "reverse": round(float(reverse), 4),
        "speed": round(float(speed), 4),
        "seam": round(seam[0], 3),
        "internal": round(normal[0], 3),
        "edge": round(seam[2], 3),
        "brightness": round(seam[1], 3),
        "saturation": round(seam[4], 3),
        "flowResidual": round(seam[6], 3),
        "moving": round(normal[5], 4),
        "internalReverse": round(float(inner_reverse), 4),
        "directionDegrees": direction,
        "motionGrid": grid.astype(float).round(2).tolist(),
    }


def recurrence(first, second):
    """Verify repeatability at multiple phases specifically in moving tiles."""
    first = np.asarray(first, dtype=np.float32)
    second = np.asarray(second, dtype=np.float32)
    activity = np.abs(np.diff(first, axis=0)).mean(axis=(0, 3))
    residual = np.abs(first - second).mean(axis=(0, 3))
    activity, residual = (cv.resize(x, (8, 6), interpolation=cv.INTER_AREA) for x in (activity, residual))
    active = activity > 1.4
    support = float(np.mean(residual[active] <= 0.75 * activity[active] + 0.7)) if active.any() else 0.0
    return {
        "supported": bool(active.sum() >= 3 and support >= 0.8),
        "tileAgreement": round(support, 3),
        "movingTiles": int(active.sum()),
    }


def correction(frames, protect=None):
    """A small forward endpoint displacement, masked by flow reliability."""
    first, second, penult, last = map(gray, (frames[0], frames[1], frames[-2], frames[-1]))
    seam = flow(last, first)
    backward = flow(first, last)
    expected = (flow(penult, last) + flow(first, second)) / 2
    height, width = first.shape
    y, x = np.mgrid[:height, :width].astype(np.float32)
    back = cv.remap(
        backward, x + seam[..., 0], y + seam[..., 1], cv.INTER_LINEAR, borderMode=cv.BORDER_REPLICATE
    )
    residual = seam - expected
    amplitude = np.linalg.norm(residual, axis=2)
    reliable = (
        (np.linalg.norm(seam + back, axis=2) < 0.65)
        & (np.linalg.norm(expected, axis=2) > 0.3)
        & ((seam * expected).sum(axis=2) > 0)
        & (amplitude > 0.10)
        & (amplitude < 2.7)
    )
    weight = cv.GaussianBlur(reliable.astype(np.float32), (5, 5), 0) * reliable
    # Taper to zero at image edges. No wrapping or exposed borders.
    weight *= np.clip(np.minimum.reduce([x, y, width - 1 - x, height - 1 - y]) / 5, 0, 1)
    if protect:
        left, top, right, bottom = protect
        weight[
            (x >= left * width - 2)
            & (x <= right * width + 2)
            & (y >= top * height - 2)
            & (y <= bottom * height + 2)
        ] = 0
    field = residual * weight[..., None]
    # The inverse sampling map p - field(p) must remain locally one-to-one.
    # A folded mesh could duplicate a strong border even with a single image.
    vy, vx = np.gradient(field[..., 1])
    uy, ux = np.gradient(field[..., 0])
    for scale in (1.0, 0.5, 0.25):
        determinant = (1 - scale * ux) * (1 - scale * vy) - scale**2 * uy * vx
        if determinant.min() > 0.25:
            return field * scale
    return np.zeros_like(field)


def warp(rgb, displacement, weight):
    h, w = rgb.shape[:2]
    d = cv.resize(displacement, (w, h), interpolation=cv.INTER_LINEAR)
    y, x = np.mgrid[:h, :w].astype(np.float32)
    return cv.remap(
        rgb,
        x - d[..., 0] * (w / PROXY[0]) * weight,
        y - d[..., 1] * (h / PROXY[1]) * weight,
        cv.INTER_LINEAR,
        borderMode=cv.BORDER_REPLICATE,
    )


def improved(original, trial):
    return bool(
        not set(trial["flags"]) - set(original["flags"])
        and trial["cost"] + 0.025 < original["cost"]
        and trial["reverse"] <= original["reverse"] + 0.012
        and trial["speed"] <= original["speed"] + 0.025
        and trial["edge"] <= original["edge"] * 1.04 + 0.1
    )
