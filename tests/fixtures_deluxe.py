"""Deterministic scientific fixture generator covering Scenarios A through R (Section 14)."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np


def write_mp4(path: Path, frames: np.ndarray, fps: float = 10.0) -> Path:
    """Encode an RGB uint8 stack (N, H, W, 3) into a lossless libx264rgb MP4."""
    n, h, w, _ = frames.shape
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{w}x{h}",
            "-r",
            str(fps),
            "-i",
            "pipe:0",
            "-c:v",
            "libx264rgb",
            "-crf",
            "0",
            "-threads",
            "1",
            str(path),
        ],
        input=np.ascontiguousarray(frames, dtype=np.uint8).tobytes(),
        check=True,
        timeout=45,
    )
    return path


def _smooth_noise_texture(h: int, w: int, seed: int = 1, period_x: int | None = None, period_y: int | None = None) -> np.ndarray:
    """Create a rich, natural-looking periodic or general multi-octave RGB texture."""
    rng = np.random.default_rng(seed)
    px = period_x or w
    py = period_y or h
    yy, xx = np.mgrid[0:py, 0:px].astype(np.float32)
    img = np.zeros((py, px, 3), dtype=np.float32)
    for k in range(1, 5):
        fx = 2.0 * np.pi * k / px
        fy = 2.0 * np.pi * k / py
        amp = 28.0 / k
        for c in range(3):
            ph = float(rng.uniform(0, 2 * np.pi))
            ph2 = float(rng.uniform(0, 2 * np.pi))
            img[..., c] += amp * np.sin(fx * xx + ph) * np.cos(fy * yy + ph2)
            img[..., c] += (amp * 0.6) * np.cos(fx * xx * 2.0 - fy * yy + ph)
    # Add fine micro-texture so optical flow & DXT1 see realistic detail
    fine = rng.normal(0.0, 9.0, (py, px, 3)).astype(np.float32)
    fine = cv2.GaussianBlur(fine, (3, 3), 0.7)
    base = np.array([115.0, 128.0, 145.0], dtype=np.float32)
    tile = np.clip(base + img + fine, 15, 240).astype(np.uint8)
    reps_y = (h + py - 1) // py
    reps_x = (w + px - 1) // px
    return np.tile(tile, (reps_y, reps_x, 1))[:h, :w]


def _shift_subpixel(img: np.ndarray, dx: float, dy: float, wrap: bool = True) -> np.ndarray:
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    mx = (xx - dx) % w if wrap else np.clip(xx - dx, 0, w - 1)
    my = (yy - dy) % h if wrap else np.clip(yy - dy, 0, h - 1)
    border = cv2.BORDER_WRAP if wrap else cv2.BORDER_REPLICATE
    return cv2.remap(img, mx, my, interpolation=cv2.INTER_CUBIC, borderMode=border)


def scenario_a_river(n: int = 16, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario A: Unidirectional flowing water texture, continuous across seam."""
    w, h = size
    period = 32
    water = _smooth_noise_texture(h, w, seed=101, period_x=period, period_y=h)
    # Tint towards river blues/greens
    water = np.clip(water.astype(np.float32) * np.array([0.65, 0.95, 1.15]), 10, 245).astype(np.uint8)
    step = period / float(n)
    return np.stack([_shift_subpixel(water, i * step, 0.0, wrap=True) for i in range(n)])


def scenario_b_clouds(n: int = 24, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario B: Two cloud layers moving continuously at distinct speeds."""
    w, h = size
    p1, p2 = 24, 48
    yy = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    sky = np.clip(np.broadcast_to(np.array([40, 75, 145], np.float32) * (1 - 0.35 * yy) + np.array([95, 130, 185], np.float32) * 0.35 * yy, (h, w, 3)), 0, 255).astype(np.uint8)
    c1 = _smooth_noise_texture(h // 2, w, seed=201, period_x=p1, period_y=h // 2)
    c2 = _smooth_noise_texture(h - h // 2, w, seed=202, period_x=p2, period_y=h - h // 2)
    frames = []
    for i in range(n):
        f = sky.copy()
        f[: h // 2] = cv2.addWeighted(f[: h // 2], 0.35, _shift_subpixel(c1, i * (p1 / n), 0.0), 0.65, 0)
        f[h // 2 :] = cv2.addWeighted(f[h // 2 :], 0.35, _shift_subpixel(c2, i * (p2 / n), 0.0), 0.65, 0)
        frames.append(f)
    return np.stack(frames)


def scenario_c_periodic(n: int = 16, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario C: Exact periodic translating texture."""
    w, h = size
    period = 32
    tex = _smooth_noise_texture(h, w, seed=301, period_x=period, period_y=h)
    step = period / float(n)
    return np.stack([_shift_subpixel(tex, i * step, 0.0, wrap=True) for i in range(n)])


def scenario_d_quasi_periodic(n: int = 16, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario D: Quasi-periodic texture with gradual tonal drift and small phase mismatch."""
    base = scenario_c_periodic(n, size).astype(np.float32)
    frames = []
    for i in range(n):
        drift = 14.0 * (i / max(1, n - 1))  # gradual luminance drift creates seam step
        shifted = _shift_subpixel(base[i].astype(np.uint8), i * 0.25, 0.0, wrap=True).astype(np.float32)
        frames.append(np.clip(shifted + drift, 0, 255).astype(np.uint8))
    return np.stack(frames)


def scenario_e_non_periodic(n: int = 20, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario E: Monotonic non-periodic evolution (expanding bright disc + shifting scene)."""
    w, h = size
    bg = _smooth_noise_texture(h, w * 2, seed=501)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    frames = []
    for i in range(n):
        crop_x = int(i * 3)
        f = bg[:, crop_x : crop_x + w].copy()
        rad = 4.0 + i * 2.4
        cx, cy = w * 0.2 + i * 4.0, h * 0.5
        mask = (xx - cx) ** 2 + (yy - cy) ** 2 < rad * rad
        f[mask] = np.clip(f[mask].astype(np.int16) + 95, 0, 255).astype(np.uint8)
        frames.append(f)
    return np.stack(frames)


def scenario_f_ping_pong(n: int = 24, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario F: Forward then reversed trajectory (ping-pong)."""
    w, h = size
    tex = _smooth_noise_texture(h, w, seed=601, period_x=w, period_y=h)
    half = n // 2
    path = list(range(half)) + list(range(half - 2, -1, -1))
    path = (path + [0])[:n]
    return np.stack([_shift_subpixel(tex, p * 2.5, 0.0, wrap=True) for p in path])


def scenario_g_abrupt_cut(n: int = 16, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario G: Hard abrupt cut mid-sequence."""
    w, h = size
    a = _smooth_noise_texture(h, w, seed=701, period_x=32)
    b = 255 - _smooth_noise_texture(h, w, seed=702, period_x=32)
    b = np.clip(b.astype(np.float32) * np.array([1.2, 0.5, 0.4]), 0, 255).astype(np.uint8)
    frames = []
    for i in range(n):
        src = a if i < n // 2 else b
        frames.append(_shift_subpixel(src, i * 2.0, 0.0, wrap=True))
    return np.stack(frames)


def scenario_h_illumination(n: int = 16, size: tuple[int, int] = (160, 90)) -> dict[str, np.ndarray]:
    """Scenario H: Natural smooth lighting pulse vs artificial frame-to-frame flicker."""
    w, h = size
    base = _smooth_noise_texture(h, w, seed=801).astype(np.float32)
    natural = np.stack([
        np.clip(base + 22.0 * np.sin(2.0 * np.pi * i / n), 0, 255).astype(np.uint8)
        for i in range(n)
    ])
    flicker = np.stack([
        np.clip(base + (20.0 if i % 2 == 0 else -20.0), 0, 255).astype(np.uint8)
        for i in range(n)
    ])
    return {"natural_pulse": natural, "artificial_flicker": flicker}


def scenario_i_transient_object(n: int = 16, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario I: Periodic water with a bright transient object crossing late (pop-out at seam)."""
    frames = scenario_a_river(n, size).copy()
    h, w = frames.shape[1:3]
    for i in range(n // 2, n):
        # Object enters at n//2 and is still fully visible at frame n-1, then vanishes at frame 0
        cx = int(w * 0.25 + (i - n // 2) * (w * 0.08))
        cy = h // 2
        cv2.circle(frames[i], (cx, cy), max(6, h // 9), (245, 50, 40), -1)
        cv2.circle(frames[i], (cx, cy), max(6, h // 9), (255, 255, 255), 2)
    return frames


def scenario_j_occlusions(n: int = 16, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario J: Textured foreground object translating over textured static background (occlusion/disocclusion)."""
    w, h = size
    bg = _smooth_noise_texture(h, w, seed=1001)
    fg = _smooth_noise_texture(h // 3, w // 4, seed=1002)
    fg = np.clip(fg.astype(np.float32) * np.array([1.25, 0.75, 0.6]), 10, 250).astype(np.uint8)
    fh, fw = fg.shape[:2]
    y0 = (h - fh) // 2
    frames = []
    for i in range(n):
        f = bg.copy()
        x0 = (10 + i * 7) % (w - fw)
        f[y0 : y0 + fh, x0 : x0 + fw] = fg
        frames.append(f)
    return np.stack(frames)


def scenario_k_static_dynamic(n: int = 16, size: tuple[int, int] = (160, 90)) -> tuple[np.ndarray, np.ndarray]:
    """Scenario K: Static mountain/moon top half + dynamic flowing river bottom half, plus protected mask."""
    w, h = size
    top_h = h // 2
    static_bg = _smooth_noise_texture(h, w, seed=1101)
    # Paint a bright moon in top-left static sky
    cv2.circle(static_bg, (w // 5, top_h // 2), max(6, h // 10), (240, 232, 195), -1)
    river = scenario_a_river(n, size)
    protected_mask = np.zeros((h, w), dtype=bool)
    protected_mask[:top_h, :] = True
    frames = []
    for i in range(n):
        f = static_bg.copy()
        f[top_h:, :] = river[i, top_h:, :]
        frames.append(f)
    return np.stack(frames), protected_mask


def scenario_l_independent_motions(n: int = 24, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario L: Static top, slow clouds moving right (+1 px/f), fast river moving left (-3 px/f)."""
    w, h = size
    b1, b2 = h // 3, (2 * h) // 3
    top = _smooth_noise_texture(b1, w, seed=1201)
    mid = _smooth_noise_texture(b2 - b1, w, seed=1202, period_x=24)
    bot = _smooth_noise_texture(h - b2, w, seed=1203, period_x=24)
    frames = []
    for i in range(n):
        f = np.empty((h, w, 3), dtype=np.uint8)
        f[:b1] = top
        f[b1:b2] = _shift_subpixel(mid, i * 1.0, 0.0, wrap=True)
        f[b2:] = _shift_subpixel(bot, -i * 3.0, 0.0, wrap=True)
        frames.append(f)
    return np.stack(frames)


def scenario_m_fixed_camera(n: int = 16, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario M: Fixed camera frame with small localized natural periodic river."""
    frames, _ = scenario_k_static_dynamic(n, size)
    return frames


def scenario_n_moving_camera(n: int = 16, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario N: Global panning camera across a wide textured scene (+2.5 px/frame)."""
    w, h = size
    pano = _smooth_noise_texture(h, w + n * 4 + 16, seed=1401)
    # Add sharp architectural landmarks so LK corners lock strongly onto global camera motion
    for lx in range(20, pano.shape[1] - 20, 28):
        cv2.rectangle(pano, (lx, 18), (lx + 12, h - 18), (220, 210, 190), 2)
    frames = []
    for i in range(n):
        x0 = int(round(i * 2.5))
        frames.append(pano[:, x0 : x0 + w].copy())
    return np.stack(frames)


def scenario_o_subpixel(n: int = 20, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario O: Subtle subpixel drift (0.35 px/frame horizontal)."""
    w, h = size
    tex = _smooth_noise_texture(h, w, seed=1501, period_x=w, period_y=h)
    return np.stack([_shift_subpixel(tex, i * 0.35, 0.0, wrap=True) for i in range(n)])


def scenario_p_vfr_mp4(path: Path, size: tuple[int, int] = (160, 90)) -> Path:
    """Scenario P: Real VFR MP4 where first half runs at 100ms/frame and second half at 40ms/frame."""
    frames = scenario_c_periodic(20, size)
    with tempfile.TemporaryDirectory() as tmp:
        tdir = Path(tmp)
        lines = []
        for i, f in enumerate(frames):
            img_p = tdir / f"f{i:02d}.png"
            cv2.imwrite(str(img_p), cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
            dur = 0.10 if i < 10 else 0.04
            lines.append(f"file '{img_p}'")
            lines.append(f"duration {dur}")
        lines.append(f"file '{tdir / f'f{len(frames) - 1:02d}.png'}'")
        concat_file = tdir / "list.txt"
        concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-vsync",
                "vfr",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "10",
                str(path),
            ],
            check=True,
            timeout=45,
        )
    return path


def scenario_q_motion_blur(n: int = 16, size: tuple[int, int] = (160, 90)) -> np.ndarray:
    """Scenario Q: Flowing scene with realistic horizontal directional motion blur."""
    raw = scenario_a_river(n, size)
    ksize = 9
    kernel = np.zeros((ksize, ksize), dtype=np.float32)
    kernel[ksize // 2, :] = 1.0 / ksize
    return np.stack([cv2.filter2D(f, -1, kernel) for f in raw])


def scenario_r_gradients_banding(n: int = 8, size: tuple[int, int] = (640, 360)) -> np.ndarray:
    """Scenario R: Smooth twilight sky gradient prone to DXT1a quantization banding."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    frames = []
    for i in range(n):
        phase = 2.0 * np.pi * i / n
        r = 18.0 + 38.0 * (yy / h) + 6.0 * np.sin(phase + xx / w * 2.0 * np.pi)
        g = 24.0 + 48.0 * (yy / h) + 5.0 * np.cos(phase)
        b = 45.0 + 85.0 * (yy / h) + 8.0 * (xx / w)
        f = np.stack([r, g, b], axis=-1)
        frames.append(np.clip(np.rint(f), 0, 255).astype(np.uint8))
    return np.stack(frames)
