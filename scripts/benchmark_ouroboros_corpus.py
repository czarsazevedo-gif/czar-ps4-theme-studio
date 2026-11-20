#!/usr/bin/env python3
"""Reproducible Ouroboros Perceptual real-video corpus benchmark (pre/post-DDS + CZAR_ACCEPTANCE_VIDEO)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app import media, ouroboros_media as io_media
from app.config import ANIMATED_SIZES, dds_bytes_for
from app.deluxe.circulo_supreme import search_supreme
from app.deluxe.pipeline import render_deluxe_loop


def _probe_duration(path: Path) -> float:
    raw = io_media.command(
        [media.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        lambda: None,
    )
    return float(raw.strip())


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _pick_best_candidate(analysis: dict) -> dict | None:
    cats = analysis.get("categories") or {}
    for key in ("best_balance", "best_natural", "best_correctable", "best_convincing_duration", "best_synthesized"):
        if cats.get(key):
            return cats[key]
    for g in analysis.get("groups") or []:
        if g.get("candidates"):
            return g["candidates"][0]
    return None


def benchmark_video(entry: dict) -> dict:
    path = Path(entry["path"])
    if not path.is_file():
        return {"id": entry["id"], "error": f"Arquivo ausente: {path}"}

    sha = entry.get("sha256") or _sha256_file(path)
    t_total = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix=f"czar-bench-{entry['id']}-") as td:
        work = Path(td)
        dur_hint = _probe_duration(path)
        info = io_media.source_info(path, dur_hint, lambda: None)
        dur = float(info["duration"])

        an_dir = work / "an"
        an_dir.mkdir()
        t_an = time.perf_counter()
        analysis = search_supreme(path, dur, an_dir, source_sha256=sha)
        an_ms = round((time.perf_counter() - t_an) * 1000.0, 1)

        sent = analysis["sentinela"]
        src = sent["source"]
        cand = _pick_best_candidate(analysis)
        if cand is None:
            return {
                "id": entry["id"],
                "split": entry.get("split", "custom"),
                "category": entry.get("category", "custom"),
                "license": entry.get("license", "local"),
                "name": path.name,
                "sha256": sha,
                "source": src,
                "classification": sent["classification"],
                "analysisMs": an_ms,
                "error": "Nenhum candidato RAF viável encontrado.",
            }

        size = tuple(cand["size"])
        res_idx = ANIMATED_SIZES.index(size) if size in ANIMATED_SIZES else 2
        frames = int(cand["frames"])
        wait = float(cand["wait"])
        start = float(cand["start"])
        end = float(cand["end"])

        # Compare Natural Cut (post-DDS) vs Reconstruct Auto + Deluxe Finish (post-DDS)
        nat_dir = work / "nat"
        nat_dir.mkdir()
        t_nat = time.perf_counter()
        nat_res = render_deluxe_loop(
            path,
            dur,
            nat_dir,
            start=start,
            end=end,
            frames=frames,
            size=size,
            wait=wait,
            mode="natural",
            deluxe_finish=False,
            source_sha256=sha,
        )
        nat_ms = round((time.perf_counter() - t_nat) * 1000.0, 1)

        rec_dir = work / "rec"
        rec_dir.mkdir()
        t_rec = time.perf_counter()
        rec_res = render_deluxe_loop(
            path,
            dur,
            rec_dir,
            start=start,
            end=end,
            frames=frames,
            size=size,
            wait=wait,
            mode="reconstruct_auto",
            deluxe_finish=True,
            source_sha256=sha,
        )
        rec_ms = round((time.perf_counter() - t_rec) * 1000.0, 1)

        def _extract_kpis(rep: dict) -> dict:
            m = rep["metrics"]
            return {
                "passed": rep["passed"],
                "issues": rep["issues"],
                "seamRatio": m["visualSimilarity"]["seamRatio"],
                "seamSsim": m["visualSimilarity"]["seamSsim"],
                "seamFlowErrPx": m["flowContinuity"]["seamFlowErrorPx"],
                "flowErrorRatio": m["flowContinuity"]["flowErrorRatio"],
                "seamDirCos": m["flowContinuity"]["seamDirectionCosine"],
                "seamOppFrac": m["flowContinuity"]["seamOppositeFraction"],
                "dirMeasured": m["flowContinuity"]["directionMeasured"],
                "velJump": m["velocityContinuity"]["relativeJump"],
                "medSpeedPx": m["velocityContinuity"]["internalMedianSpeedPx"],
                "edgeRatio": m["edgeConsistency"]["edgeRatio"],
                "ghosting": m["ghosting"]["score"],
                "flicker": m["flicker"]["score"],
                "sharpRetention": m["texturePreservation"]["sharpnessRetention"],
                "reversals": m["reversals"]["detected"],
                "freezes": m["freezes"]["detected"],
                "ddsMeanPsnr": (m.get("ddsDelta") or {}).get("meanPsnr"),
                "ddsBandingPost": (m.get("ddsDelta") or {}).get("bandingPostDds"),
                "ddsDistinct": (m.get("ddsDelta") or {}).get("distinctFrames"),
                "withinBudget": (m.get("ddsDelta") or {}).get("withinBudget"),
            }

        return {
            "id": entry["id"],
            "split": entry.get("split", "custom"),
            "category": entry.get("category", "custom"),
            "license": entry.get("license", "local"),
            "url": entry.get("url", ""),
            "name": path.name,
            "sha256": sha,
            "durationSec": round(dur, 3),
            "resolution": [src["width"], src["height"]],
            "fps": round(float(src["fps"]), 3),
            "codec": src["codec"],
            "isVFR": src["isVFR"],
            "windowsSampled": src.get("windowsSampled", 1),
            "sentinelaClass": sent["classification"],
            "sentinelaConfidence": sent["confidence"],
            "motion": {
                "meanSpeedPx": sent["levelC"]["meanSpeedPx"],
                "spatialCoherence": sent["levelC"]["spatialCoherence"],
                "temporalConsistency": sent["levelC"]["temporalConsistency"],
                "cameraMoving": sent["levelA"]["cameraMoving"],
                "staticFrac": sent["levelB"]["staticFraction"],
                "progressiveFrac": sent["levelB"]["progressiveFraction"],
                "oscillatoryFrac": sent["levelB"]["oscillatoryFraction"],
                "transientFrac": sent["levelB"]["transientFraction"],
                "trueRecurrence": sent["levelD"]["trueRecurrence"],
                "periodSec": sent["levelD"]["periodSeconds"],
                "cuts": len(sent["levelA"]["cuts"]),
            },
            "bestTake": {
                "start": start,
                "end": end,
                "frames": frames,
                "size": list(size),
                "resolutionIdx": res_idx,
                "wait": wait,
                "fps": round(1.0 / wait, 2),
                "duration": round(frames * wait, 3),
                "ddsBytes": frames * dds_bytes_for(*size),
                "classification": cand["costs"]["classification"],
                "totalCost": cand["costs"]["totalCost"],
            },
            "strategyUsed": rec_res["strategyUsed"],
            "finishSteps": rec_res["finish"]["appliedSteps"],
            "naturalPostDds": _extract_kpis(nat_res["finalPostDds"]),
            "refinedPostDds": _extract_kpis(rec_res["finalPostDds"]),
            "beforeAfterRaw": rec_res["beforeAfterRaw"],
            "timingsMs": {
                "analysis": an_ms,
                "naturalRender": nat_ms,
                "refinedRender": rec_ms,
                "total": round((time.perf_counter() - t_total) * 1000.0, 1),
            },
        }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="/tmp/czar-real-corpus/manifest.json")
    ap.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    ap.add_argument("--ids", default="", help="Comma-separated list of entry IDs to run")
    ap.add_argument("--include-repo-sample", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    wanted_ids = {x.strip() for x in args.ids.split(",") if x.strip()}
    entries: list[dict] = []
    mpath = Path(args.manifest)
    if mpath.is_file():
        for item in json.loads(mpath.read_text(encoding="utf-8")):
            if wanted_ids:
                if item.get("id") in wanted_ids:
                    entries.append(item)
            elif args.split == "all" or item.get("split") == args.split:
                entries.append(item)

    if args.include_repo_sample or "repo_sample_mp4" in wanted_ids:
        entries.insert(
            0,
            {
                "id": "repo_sample_mp4",
                "split": "dev",
                "category": "amostra_repo",
                "license": "repo-fixture",
                "path": str(REPO / "tests" / "sample.mp4"),
            },
        )

    owner_vid = os.environ.get("CZAR_ACCEPTANCE_VIDEO", "").strip()
    if owner_vid and Path(owner_vid).is_file() and (not wanted_ids or "owner_acceptance_video" in wanted_ids):
        entries.append(
            {
                "id": "owner_acceptance_video",
                "split": "owner",
                "category": "proprietario",
                "license": "private-owner",
                "path": owner_vid,
            }
        )

    results = []
    out_p = Path(args.out) if args.out else None
    if out_p is not None:
        out_p.parent.mkdir(parents=True, exist_ok=True)
    for e in entries:
        print(f"==> Benchmarking {e['id']} ({e.get('category')}) ...", flush=True)
        res = benchmark_video(e)
        results.append(res)
        if out_p is not None:
            out_p.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        if "error" in res:
            print(f"    ERR: {res['error']}", flush=True)
        else:
            nat = res["naturalPostDds"]
            ref = res["refinedPostDds"]
            print(
                f"    class={res['sentinelaClass']} take={res['bestTake']['size'][0]}x{res['bestTake']['size'][1]}@{res['bestTake']['frames']}f "
                f"strat={res['strategyUsed']} | "
                f"NAT(seam={nat['seamRatio']:.2f}, cos={nat['seamDirCos']:.2f}, opp={nat['seamOppFrac']:.2f}, vj={nat['velJump']:.2f}, iss={len(nat['issues'])}) -> "
                f"REF(seam={ref['seamRatio']:.2f}, cos={ref['seamDirCos']:.2f}, opp={ref['seamOppFrac']:.2f}, vj={ref['velJump']:.2f}, iss={len(ref['issues'])}) "
                f"[{res['timingsMs']['total']:.0f}ms]",
                flush=True,
            )

    if out_p is not None:
        print(f"Saved benchmark JSON to {out_p}")


if __name__ == "__main__":
    main()
