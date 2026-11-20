"""Reproducible Checkpoint 6 scientific & performance benchmark suite (Section 16)."""

from __future__ import annotations

import json
import platform
import resource
import tempfile
import time
from pathlib import Path

import numpy as np

from app import media, ouroboros_media as io_media
from app.deluxe.calibrator import calibrate_interval
from app.deluxe.circulo_supreme import search_supreme
from app.deluxe.finish import apply_deluxe_finish
from app.deluxe.genesis_supreme import synthesize_genesis_supreme
from app.deluxe.pipeline import render_deluxe_loop
from app.deluxe.reconstructor import reconstruct_best
from tests import fixtures_deluxe as fx


def run_benchmarks() -> dict:
    results: dict = {
        "hardware": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpuCount": __import__("os").cpu_count(),
        },
        "cases": [],
    }

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # 1. Real file: lua-sintetica.mp4 (640x360 Deluxe render + post-DDS)
        lua = Path("docs/examples/genesis/lua-sintetica.mp4")
        if lua.is_file():
            dur = media.probe(lua).duration
            lua_an = root / "lua_an"
            lua_an.mkdir()
            t0, c0 = time.perf_counter(), time.process_time()
            an = search_supreme(lua, dur, lua_an, shortlist_per_res=6)
            t1, c1 = time.perf_counter(), time.process_time()
            out_dir = root / "lua_ren"
            out_dir.mkdir()
            ren = render_deluxe_loop(
                lua, dur, out_dir, start=0.0, end=dur, frames=12, size=(640, 360), wait=0.1, mode="reconstruct_auto", deluxe_finish=True
            )
            t2, c2 = time.perf_counter(), time.process_time()
            results["cases"].append(
                {
                    "name": "lua-sintetica.mp4 (640x360, 12f)",
                    "analyzeWallSec": round(t1 - t0, 2),
                    "analyzeCpuSec": round(c1 - c0, 2),
                    "renderFinishWallSec": round(t2 - t1, 2),
                    "renderFinishCpuSec": round(c2 - c1, 2),
                    "sentinelaClass": an["sentinela"]["classification"],
                    "strategyUsed": ren["strategyUsed"],
                    "ddsBytes": ren["bytes"],
                    "beforeSeamMae": ren["baselinePostDds"]["metrics"]["visualSimilarity"]["seamMae"],
                    "afterSeamMae": ren["finalPostDds"]["metrics"]["visualSimilarity"]["seamMae"],
                    "beforeSeamSsim": ren["baselinePostDds"]["metrics"]["visualSimilarity"]["seamSsim"],
                    "afterSeamSsim": ren["finalPostDds"]["metrics"]["visualSimilarity"]["seamSsim"],
                    "beforeCost": ren["beforeAfterRaw"]["beforeCompositeCost"],
                    "afterCost": ren["beforeAfterRaw"]["afterCompositeCost"],
                    "finishSteps": ren["finish"]["appliedSteps"],
                    "rafZipBytes": (out_dir / "background.raf.zip").stat().st_size,
                }
            )

        # 2. Real fixture: tests/sample.mp4
        sample = Path("tests/sample.mp4")
        if sample.is_file():
            dur = media.probe(sample).duration
            samp_an = root / "samp_an"
            samp_cal = root / "samp_cal"
            samp_an.mkdir()
            samp_cal.mkdir()
            t0, c0 = time.perf_counter(), time.process_time()
            an = search_supreme(sample, dur, samp_an, shortlist_per_res=6)
            t1, c1 = time.perf_counter(), time.process_time()
            cal = calibrate_interval(sample, 0.0, min(2.0, dur), source_fps=15.0, folder=samp_cal, verify_dds=False)
            t2, c2 = time.perf_counter(), time.process_time()
            results["cases"].append(
                {
                    "name": "tests/sample.mp4 (7.3s)",
                    "analyzeWallSec": round(t1 - t0, 2),
                    "analyzeCpuSec": round(c1 - c0, 2),
                    "calibrateWallSec": round(t2 - t1, 2),
                    "calibrateCpuSec": round(c2 - c1, 2),
                    "sentinelaClass": an["sentinela"]["classification"],
                    "calibratedConfigs": cal["evaluatedCount"],
                }
            )

        # 3. Synthetic Scenario D (Quasi-periodic drift) -> Full Reconstruct + Deluxe Finish at 640x360
        quasi = fx.scenario_d_quasi_periodic(16, size=(640, 360))
        t0, c0 = time.perf_counter(), time.process_time()
        rec_d = reconstruct_best(quasi, wait=0.066667, verify_dds=True)
        fin_d = apply_deluxe_finish(rec_d["frames"], wait=0.066667, reference_frames=quasi)
        t1, c1 = time.perf_counter(), time.process_time()
        results["cases"].append(
            {
                "name": "Scenario D quasi-periodic (640x360, 16f)",
                "wallSec": round(t1 - t0, 2),
                "cpuSec": round(c1 - c0, 2),
                "strategy": rec_d["selectedStrategy"],
                "beforeSeamDeltaL": rec_d["baselineEvaluation"]["metrics"]["luminanceContinuity"]["seamDeltaL"],
                "afterSeamDeltaL": fin_d["finalPostDds"]["metrics"]["luminanceContinuity"]["seamDeltaL"],
                "beforeSeamMae": rec_d["baselineEvaluation"]["metrics"]["visualSimilarity"]["seamMae"],
                "afterSeamMae": fin_d["finalPostDds"]["metrics"]["visualSimilarity"]["seamMae"],
                "ddsEncodeMs": fin_d["finalPostDds"]["metrics"]["ddsDelta"]["encodeMs"],
                "ddsBytes": fin_d["ddsBytes"],
            }
        )

        # 4. Synthetic Scenario H2 (Artificial Flicker) -> Deluxe Finish at 640x360
        flk = fx.scenario_h_illumination(16, size=(640, 360))["artificial_flicker"]
        t0, c0 = time.perf_counter(), time.process_time()
        fin_h = apply_deluxe_finish(flk, wait=0.05)
        t1, c1 = time.perf_counter(), time.process_time()
        results["cases"].append(
            {
                "name": "Scenario H2 artificial flicker (640x360, 16f)",
                "wallSec": round(t1 - t0, 2),
                "cpuSec": round(c1 - c0, 2),
                "appliedSteps": fin_h["appliedSteps"],
                "beforeFlicker": fin_h["baselinePostDds"]["metrics"]["flicker"]["score"],
                "afterFlicker": fin_h["finalPostDds"]["metrics"]["flicker"]["score"],
                "ddsEncodeMs": fin_h["finalPostDds"]["metrics"]["ddsDelta"]["encodeMs"],
                "ddsBytes": fin_h["ddsBytes"],
            }
        )

        # 5. Synthetic Scenario K (Static + Dynamic) -> Genesis Supreme 2D at 960x540 (24f)
        sk, pmask = fx.scenario_k_static_dynamic(12, size=(960, 540))
        t0, c0 = time.perf_counter(), time.process_time()
        gen_k = synthesize_genesis_supreme(sk, n_frames=24, wait=0.05, mask=~pmask, protected_mask=pmask, verify_dds=True)
        t1, c1 = time.perf_counter(), time.process_time()
        results["cases"].append(
            {
                "name": "Scenario K Genesis Supreme 2D (960x540, 24f)",
                "wallSec": round(t1 - t0, 2),
                "cpuSec": round(c1 - c0, 2),
                "protectedMaxDiff": gen_k["evaluation"]["metrics"]["staticIntegrity"]["maxAbsDiff"],
                "distinctFrames": gen_k["evaluation"]["metrics"]["ddsDelta"]["distinctFrames"],
                "ddsEncodeMs": gen_k["evaluation"]["metrics"]["ddsDelta"]["encodeMs"],
                "ddsBytes": gen_k["evaluation"]["metrics"]["ddsDelta"]["totalBytes"],
            }
        )

    results["peakRssMiB"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    return results


if __name__ == "__main__":
    print(json.dumps(run_benchmarks(), indent=2, ensure_ascii=False))
