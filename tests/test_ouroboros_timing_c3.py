"""Checkpoint 3 tests — Gifsicle (periodic, 50/150ms variable delays, transparency/optimized), SciPy & OpenCV."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image

from app.ouroboros_engine import OuroborosEngine
from app.ouroboros_engine.calibrator import WAIT_GRID_MS, optimize_wait_scipy
from app.ouroboros_engine.gif_inspector import extract_gif_continuous_cadence, find_gifsicle, inspect_gif
from app.ouroboros_engine.metrics import evaluate_loop
from app.ouroboros_engine.search import detect_scipy_periodicity


def _make_periodic_wave_frames(n: int, w: int = 160, h: int = 90) -> list[np.ndarray]:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    frames = []
    for k in range(n):
        phase = 2.0 * math.pi * (k / float(n))
        val = (np.sin(xx * 0.12 - phase) * 0.5 + 0.5) * 200.0 + 30.0
        g = (np.cos(yy * 0.1 + phase) * 0.5 + 0.5) * 160.0 + 40.0
        b = np.full_like(val, 140.0)
        rgb = np.stack([val, g, b], axis=-1).clip(0, 255).astype(np.uint8)
        frames.append(rgb)
    return frames


def test_gifsicle_periodic_uniform_gif_selects_mode_a_preserve(tmp_path):
    assert find_gifsicle() is not None, "tools/bin/gifsicle must be installed"
    gif_p = tmp_path / "periodic_uniform.gif"
    frames = [Image.fromarray(f, "RGB") for f in _make_periodic_wave_frames(12)]
    frames[0].save(gif_p, save_all=True, append_images=frames[1:], duration=100, loop=0)

    info = inspect_gif(gif_p, work_dir=tmp_path)
    assert info["isGif"] is True
    assert info["tool"] == "gifsicle"
    assert info["usedUnoptimize"] is True
    assert info["frameCount"] == 12
    assert info["hasVariableDelays"] is False
    assert info["modeA_preserve"]["feasible"] is True
    assert info["recommendedMode"] == "mode_a_preserve"
    assert abs(info["medianDelayMs"] - 100.0) <= 1.0


def test_gifsicle_historic_50_150ms_variable_delay_gif_mode_a_vs_mode_b(tmp_path):
    """Historic bug (Section 7): alternating 50 ms and 150 ms delays must be detected and reconstructed in Mode B."""
    import cv2

    gif_p = tmp_path / "var_50_150.gif"
    # Render textured progressive horizontal flow at true physical timestamps matching cumulative 50/150 ms delays!
    delays_ms = [50, 150] * 8  # 16 frames, total duration = 1.60 s
    total_s = sum(delays_ms) / 1000.0
    rng = np.random.default_rng(42)
    base_tex = cv2.GaussianBlur(rng.integers(30, 225, (90, 160, 3), dtype=np.uint8), (3, 3), 0.9)
    t_cur = 0.0
    pil_frames = []
    for d_ms in delays_ms:
        shift_x = int(round(48.0 * (t_cur / total_s)))
        pil_frames.append(Image.fromarray(np.roll(base_tex, shift_x, axis=1), "RGB"))
        t_cur += d_ms / 1000.0

    pil_frames[0].save(gif_p, save_all=True, append_images=pil_frames[1:], duration=delays_ms, loop=0, optimize=False)

    info = inspect_gif(gif_p, work_dir=tmp_path)
    assert info["hasVariableDelays"] is True
    assert 50.0 in info["uniqueDelaysMs"] and 150.0 in info["uniqueDelaysMs"]
    # Mode A MUST honestly report infeasibility under uniform RAF XML
    assert info["modeA_preserve"]["feasible"] is False
    assert "uniforme" in info["modeA_preserve"]["reason"].lower()
    assert info["recommendedMode"] == "mode_b_reconstruct"

    # Extract with Mode B continuous cadence vs naive uniform 100ms readout (ignoring 50/150ms timing!)
    reconstructed, meta = extract_gif_continuous_cadence(
        gif_p, 0.0, total_s, 16, (640, 360), tmp_path, mode="mode_b_reconstruct"
    )
    assert reconstructed.shape == (16, 360, 640, 3)
    assert meta["appliedMode"] == "mode_b_reconstruct"
    assert meta["hasVariableDelays"] is True
    assert meta["interpolatedFrames"] >= 4

    # Compare cadence regularity against naive frame-index uniform 100ms playback
    naive_frames = np.stack([np.asarray(im.resize((640, 360), Image.Resampling.LANCZOS)) for im in pil_frames])
    ev_naive = evaluate_loop(naive_frames, 0.1)
    ev_mode_b = evaluate_loop(reconstructed, 0.1)
    assert ev_mode_b["metrics"]["cadenceRegularity"]["speedCV"] < ev_naive["metrics"]["cadenceRegularity"]["speedCV"], (
        f"Mode B did not smooth 50/150ms cadence: mode_b={ev_mode_b['metrics']['cadenceRegularity']} "
        f"vs naive={ev_naive['metrics']['cadenceRegularity']}"
    )
    assert ev_mode_b["metrics"]["ghosting"]["detected"] is False

    # Full unified orchestrator render + calibration on the variable-delay GIF
    engine = OuroborosEngine()
    cal = engine.calibrate(gif_p, 0.0, total_s, 10.0, tmp_path / "cal")
    assert cal["candidateWaitsMs"] == list(WAIT_GRID_MS)
    assert cal["gifTiming"]["hasVariableDelays"] is True
    assert cal["rafXmlTimingSemantics"]["physicalPs4Verified"] is False

    rend = engine.render_loop(
        gif_p, total_s, tmp_path / "rend_gif", start=0.0, end=total_s, frames=12, size=(640, 360), wait=0.1, mode="auto"
    )
    assert rend["gifTiming"]["appliedMode"] == "mode_b_reconstruct"
    assert (tmp_path / "rend_gif" / "background.raf.zip").is_file()


def test_gifsicle_transparent_optimized_and_repeated_frames_gif(tmp_path):
    """Verify detection of transparency, disposal, partial rects (-O2), and repeated frames without faking smoothness."""
    import subprocess

    gs = find_gifsicle()
    assert gs is not None
    # Create individual single-frame transparent GIFs and assemble with gifsicle so repeats are truly stored!
    single_paths = []
    for k in range(6):
        im = Image.new("RGBA", (64, 36), (0, 0, 0, 0))
        x0 = 6 + (k if k < 4 else 3) * 8  # frames 4 & 5 repeat frame 3!
        for x in range(x0, min(64, x0 + 12)):
            for y in range(10, 26):
                im.putpixel((x, y), (255, 255, 255, 255))
        sp = tmp_path / f"f_{k}.gif"
        im.save(sp, "GIF")
        single_paths.append(str(sp))

    rep_gif = tmp_path / "transparent_repeated.gif"
    subprocess.run([str(gs), "--delay=8", "--loop", "--disposal=background", *single_paths, "-o", str(rep_gif)], check=True)
    info = inspect_gif(rep_gif, work_dir=tmp_path)
    assert info["isGif"] is True
    assert info["hasTransparency"] is True
    assert info["repeatedFrameCount"] >= 2
    assert "background" in info["disposals"]

    # Also test -O2 partial-frame optimized GIF
    opt_gif = tmp_path / "partial_opt.gif"
    subprocess.run([str(gs), "-O2", "--delay=8", "--loop", *single_paths[:4], "-o", str(opt_gif)], check=True)
    info_opt = inspect_gif(opt_gif, work_dir=tmp_path)
    assert info_opt["isOptimized"] is True
    assert info_opt["partialFrameCount"] >= 1


def test_scipy_periodicity_and_wait_optimization():
    # 3 full periods of a 12-frame cycle = 36 frames @ 10 fps (period = 1.2 s)
    cycle = _make_periodic_wave_frames(12, w=80, h=45)
    seq = np.stack(cycle * 3, axis=0)
    per = detect_scipy_periodicity(seq, step=0.1)
    assert per["detected"] is True
    assert any(abs(p - 1.2) <= 0.15 for p in per["periodsSec"]), per

    opt = optimize_wait_scipy(source_span=1.6, frames=16)
    assert opt["snappedWaitMs"] == 100.0
    assert abs(opt["speedRatioAtSnapped"] - 1.0) <= 0.05
