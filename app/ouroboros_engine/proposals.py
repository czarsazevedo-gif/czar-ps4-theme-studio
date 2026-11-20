"""Independent 3-proposal Ouroboros generator (1280×720 Quality, 960×540 Balance, 640×360 Flexibility).

Each proposal is optimized INDEPENDENTLY for its target resolution and priority:
  1. `quality`     (1280×720) — Máxima Qualidade Visual (definition, sharpness, detail fidelity)
  2. `balance`     (960×540)  — Melhor Equilíbrio (continuity, definition, duration, cadence)
  3. `flexibility` (640×360)  — Maior Flexibilidade Temporal (frame count, temporal regularity, duration)

Never simply resizes the same take across all 3 profiles by default. Every proposal carries:
  - Its own optimal interval, frame count, canonical wait_ms, internal strategy (A..F), and post-DDS finish
  - Its own real DXT1a DDS blobs, lossless WebP previews decoded from those DDS blobs, diff.png & RAF ZIP
  - Its own cryptographic renderKey, exact DDS byte budget, 10-cycle audit, and honest verdict
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from ..config import ANIMATED_SIZES, MAX_WAIT_SECONDS, MIN_WAIT_SECONDS, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from . import OUROBOROS_VERSION, PROPOSAL_PROFILES
from . import media_io as io_media
from .calibrator import WAIT_GRID_SEC, optimize_wait_scipy
from .regions import RegionalRecipe


def _score_candidate_for_profile(cand: dict[str, Any], profile_id: str) -> float:
    """Profile-specific ranking over evaluated candidates inside the target resolution group."""
    costs = cand.get("costs") or {}
    total_cost = float(costs.get("totalCost", 10.0))
    seam_ratio = float(costs.get("seamRatio", 2.0))
    seam_ssim = float(costs.get("seamSsim", 0.5))
    dir_cos = float(costs.get("seamDirectionCosine", 0.5))
    dur = float(cand.get("duration", 1.0))
    frames = int(cand.get("frames", 8))
    wait = float(cand.get("wait", 0.1))
    fps = 1.0 / max(wait, 1e-4)
    vel_jump = float(costs.get("velocityJump", 0.5))

    if profile_id == "quality":
        # Prioritize visual fidelity, crispness, clean seam SSIM and directional fidelity
        return (
            seam_ssim * 3.0
            + dir_cos * 1.4
            - seam_ratio * 1.15
            - total_cost * 0.45
            - vel_jump * 0.7
            + min(dur, 2.0) * 0.25
        )
    if profile_id == "flexibility":
        # Prioritize useful frame count, temporal smoothness (higher fps / longer duration) and flow continuity
        return (
            min(frames / 48.0, 1.0) * 2.1
            + min(dur, 4.0) / 4.0 * 1.8
            + min(fps, 25.0) / 25.0 * 1.1
            + dir_cos * 1.2
            + seam_ssim * 1.2
            - total_cost * 0.40
            - vel_jump * 0.9
        )
    # "balance": balanced continuity + definition + convincing duration + natural cadence
    return (
        seam_ssim * 2.1
        + dir_cos * 1.4
        + min(dur, 3.0) / 3.0 * 1.35
        + min(frames / 24.0, 1.0) * 0.85
        - seam_ratio * 0.95
        - total_cost * 0.50
        - vel_jump * 0.85
    )


def _select_profile_plan(
    analysis: dict[str, Any],
    profile: dict[str, Any],
    source_duration: float,
    source_fps: float,
) -> dict[str, Any]:
    """Select an independent (start, end, frames, wait) plan for one target resolution profile."""
    size = tuple(profile["size"])
    profile_id = str(profile["id"])
    cap = max_frames_for(size)
    gif_info = analysis.get("gifInfo") or {}
    distinct_gif = int(gif_info["distinctFrameCount"]) if gif_info.get("isGif") else None

    # Find matching resolution group from full-duration global search
    matching_group = None
    for grp in analysis.get("groups") or []:
        if tuple(grp.get("size") or ()) == size:
            matching_group = grp
            break

    cands = list((matching_group or {}).get("candidates") or [])
    if cands:
        best = max(cands, key=lambda c: _score_candidate_for_profile(c, profile_id))
        start = max(0.0, float(best["start"]))
        end = min(source_duration, float(best["end"]))
        frames = max(4, min(cap, int(best["frames"])))
        wait = float(best["wait"])
        # Refine wait cleanly against canonical grid when beneficial
        span = max(0.15, end - start)
        if profile_id == "flexibility" and frames < cap and span >= 1.2 and (distinct_gif is None or frames < distinct_gif):
            # Allow higher frame density in 360p when source has enough duration & frames
            target_n = min(cap, max(frames, int(round(span * min(max(source_fps, 12.0), 20.0)))))
            if distinct_gif is not None and not gif_info.get("hasVariableDelays"):
                target_n = min(target_n, distinct_gif)
            if target_n * dds_bytes_for(*size) <= RAF_SIZE_LIMIT and target_n >= 4:
                opt = optimize_wait_scipy(span, target_n, grid_sec=WAIT_GRID_SEC)
                if 0.78 <= opt["speedRatioAtSnapped"] <= 1.28:
                    frames = target_n
                    wait = opt["snappedWaitSec"]
        return {
            "start": round(start, 4),
            "end": round(end, 4),
            "frames": frames,
            "wait": float(min(MAX_WAIT_SECONDS, max(MIN_WAIT_SECONDS, wait))),
            "candidateSource": "global_search",
        }

    # Independent profile-tailored fallback when search group had no shortlisted seeds
    if profile_id == "quality":
        target_dur = min(source_duration, 1.3)
        target_n = min(cap, max(6, int(round(target_dur * 10.0))))
    elif profile_id == "flexibility":
        target_dur = min(source_duration, 3.2)
        target_n = min(cap, max(8, int(round(target_dur * 15.0))))
    else:
        target_dur = min(source_duration, 2.0)
        target_n = min(cap, max(8, int(round(target_dur * 12.0))))

    if distinct_gif is not None and not gif_info.get("hasVariableDelays"):
        target_n = max(4, min(target_n, distinct_gif))
    while target_n > 4 and target_n * dds_bytes_for(*size) > RAF_SIZE_LIMIT:
        target_n -= 1

    start = 0.0
    end = max(0.2, min(source_duration, target_dur))
    opt = optimize_wait_scipy(end - start, target_n, grid_sec=WAIT_GRID_SEC)
    return {
        "start": round(start, 4),
        "end": round(end, 4),
        "frames": target_n,
        "wait": float(min(MAX_WAIT_SECONDS, max(MIN_WAIT_SECONDS, opt["snappedWaitSec"]))),
        "candidateSource": "profile_calibrated_fallback",
    }


def _compact_metrics_summary(render_report: dict[str, Any]) -> dict[str, Any]:
    ev = render_report.get("finalPostDds") or {}
    m = ev.get("metrics") or {}
    vs = m.get("visualSimilarity") or {}
    fc = m.get("flowContinuity") or {}
    vc = m.get("velocityContinuity") or {}
    gh = m.get("ghosting") or {}
    fl = m.get("flicker") or {}
    dds_d = m.get("ddsDelta") or {}
    ten = render_report.get("tenCycleAudit") or {}
    return {
        "passed": bool(ev.get("passed", False)),
        "seamSsim": round(float(vs.get("seamSsim", 0.0)), 4),
        "seamRatio": round(float(vs.get("seamRatio", 0.0)), 4),
        "seamDirectionCosine": round(float(fc.get("seamDirectionCosine", 0.0)), 4),
        "seamOppositeFraction": round(float(fc.get("seamOppositeFraction", 0.0)), 4),
        "velocityJump": round(float(vc.get("relativeJump", 0.0)), 4),
        "ghostingScore": round(float(gh.get("score", 0.0)), 4),
        "flickerScore": round(float(fl.get("score", 0.0)), 4),
        "ddsMeanPsnr": dds_d.get("meanPsnr"),
        "distinctFrames": dds_d.get("distinctFrames"),
        "withinBudget": bool(dds_d.get("withinBudget", True)),
        "tenCyclePass": bool(ten.get("continuousTenCyclePass", False)),
        "tenCycleDurationSec": ten.get("tenCycleDurationSec"),
        "issues": list(ev.get("issues") or []),
    }


def _verdict_rank(verdict: str) -> int:
    return {"APROVADO": 3, "CONDICIONAL": 2, "REPROVADO": 1}.get(verdict, 0)


def generate_three_proposals(
    engine,
    source_path: Path,
    source_duration: float,
    out_folder: Path,
    *,
    filename: str = "",
    source_sha256: str = "",
    recipe: RegionalRecipe | None = None,
    stabilize_camera: bool = False,
    progress: Callable[[str, int], None] = lambda *_: None,
    check: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """Run full-duration analysis + generate 3 independently optimized PS4 loop proposals (720p, 540p, 360p)."""
    t0 = time.perf_counter()
    out_folder.mkdir(parents=True, exist_ok=True)
    sha = source_sha256 or io_media.digest(source_path, check)

    progress("Inspecionando mídia e buscando melhores ciclos em toda a duração", 8)
    check()
    analysis_dir = out_folder / "_analysis"
    analysis_dir.mkdir(exist_ok=True)
    analysis = engine.analyze_and_search(
        source_path,
        source_duration,
        analysis_dir,
        filename=filename or source_path.name,
        source_sha256=sha,
        progress=lambda stg, pct: progress(stg, min(38, 8 + int(pct * 0.30))),
        check=check,
    )

    eff_dur = float(
        (analysis.get("gifInfo") or {}).get("totalDurationSec")
        or (analysis.get("sentinela") or {}).get("source", {}).get("duration")
        or source_duration
    )
    src_fps = float((analysis.get("sentinela") or {}).get("source", {}).get("fps") or 12.0)

    proposals: list[dict[str, Any]] = []
    profile_reports: dict[str, dict[str, Any]] = {}

    for idx, prof in enumerate(PROPOSAL_PROFILES):
        check()
        pid = str(prof["id"])
        size = tuple(prof["size"])
        base_pct = 40 + idx * 18
        progress(f"Otimizando e validando {prof['title']} ({size[0]}×{size[1]})", base_pct)

        plan = _select_profile_plan(analysis, prof, eff_dur, src_fps)
        prof_dir = out_folder / pid
        prof_dir.mkdir(exist_ok=True)

        rend = engine.render_loop(
            source_path,
            eff_dur,
            prof_dir,
            start=plan["start"],
            end=plan["end"],
            frames=plan["frames"],
            size=size,
            wait=plan["wait"],
            mode="regional" if (recipe is not None and recipe.regions) else "auto",
            recipe=recipe,
            finish_enabled=True,
            stabilize_camera=stabilize_camera,
            source_sha256=sha,
            progress=lambda stg, pct, bp=base_pct, t=prof["shortTitle"]: progress(
                f"{t}: {stg}", min(95, bp + int(pct * 0.16))
            ),
            check=check,
        )

        # Cryptographic identity binding exact rendered DDS sequence to this proposal!
        render_key = io_media.fingerprint(
            [
                OUROBOROS_VERSION,
                sha,
                pid,
                rend["size"],
                plan["start"],
                plan["end"],
                rend["frames"],
                rend["wait"],
                rend["strategyUsed"],
                rend["ddsHashes"],
            ]
        )

        # Record frame & artifact digests for cache integrity validation
        artifact_digests = {p.name: io_media.digest(p, check) for p in sorted(prof_dir.glob("*.webp"))}
        for extra_name in ("source.png", "diff.png", "background.raf.zip"):
            artifact_digests[extra_name] = io_media.digest(prof_dir / extra_name, check)

        rend["profileId"] = pid
        rend["profileTitle"] = prof["title"]
        rend["renderKey"] = render_key
        rend["files"] = artifact_digests
        profile_reports[pid] = rend

        proposal_card = {
            "profileId": pid,
            "order": prof["order"],
            "title": prof["title"],
            "shortTitle": prof["shortTitle"],
            "priority": prof["priority"],
            "renderKey": render_key,
            "size": rend["size"],
            "resolutionLabel": f"{size[0]}×{size[1]}",
            "frames": rend["frames"],
            "maxFrames": max_frames_for(size),
            "wait": rend["wait"],
            "waitMs": rend["waitMs"],
            "fps": rend["fps"],
            "duration": rend["duration"],
            "sourceInterval": [plan["start"], plan["end"]],
            "candidateSource": plan["candidateSource"],
            "ddsBytes": rend["bytes"],
            "budgetPercent": rend["budgetPercent"],
            "strategyCode": rend["strategyCode"],
            "strategyUsed": rend["strategyUsed"],
            "strategyLabel": rend["strategyLabel"],
            "finishSteps": (rend.get("finish") or {}).get("appliedSteps") or [],
            "verdict": rend["verdict"],
            "verdictSummary": rend["verdictSummary"],
            "prohibitedArtifacts": rend["prohibitedArtifacts"],
            "issueAttribution": rend["issueAttribution"],
            "tenCycleAudit": rend["tenCycleAudit"],
            "metrics": _compact_metrics_summary(rend),
            "ddsHashes": rend["ddsHashes"],
            "files": artifact_digests,
            "recipe": rend["recipe"],
        }
        proposals.append(proposal_card)

    # Pick recommended proposal honestly (best verdict first, then balance preference & lower seam ratio)
    def _rec_sort(p: dict[str, Any]) -> tuple[int, int, float, float]:
        is_bal = 1 if p["profileId"] == "balance" else 0
        m = p["metrics"]
        return (
            _verdict_rank(p["verdict"]),
            1 if m["tenCyclePass"] else 0,
            float(m["seamSsim"]) - float(m["seamRatio"]) * 0.35 + is_bal * 0.08,
            float(p["duration"]),
        )

    best_prop = max(proposals, key=_rec_sort)
    progress("Três propostas Ouroboros prontas para comparação e aplicação", 100)

    return {
        "type": "ouroboros_proposals",
        "version": OUROBOROS_VERSION,
        "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
        "name": filename or source_path.name,
        "sha256": sha,
        "duration": round(eff_dur, 4),
        "gifInfo": analysis.get("gifInfo") or {"isGif": False},
        "sentinela": analysis.get("sentinela"),
        "mediaDiagnostic": analysis.get("mediaDiagnostic"),
        "proposals": proposals,
        "recommendedProfileId": best_prop["profileId"],
        "profileReports": profile_reports,
    }
