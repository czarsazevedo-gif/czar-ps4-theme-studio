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


def _score_candidate_for_profile(
    cand: dict[str, Any],
    profile_id: str,
    *,
    source_duration: float = 10.0,
    used_intervals: list[tuple[float, float, int]] | None = None,
) -> float:
    """Profile-specific multi-objective ranking with perceptual duration floor, RAF smoothness & Pareto boost."""
    costs = cand.get("costs") or {}
    total_cost = float(costs.get("totalCost", 10.0))
    seam_ratio = float(costs.get("seamRatio", 2.0))
    seam_ssim = float(costs.get("seamSsim", 0.5))
    int_ssim = float(costs.get("internalMedianSsim", 0.90))
    dir_cos = float(costs.get("seamDirectionCosine", 0.5))
    dur = float(cand.get("duration", 1.0))
    frames = int(cand.get("frames", 8))
    wait = float(cand.get("wait", 0.1))
    fps = 1.0 / max(wait, 1e-4)
    vel_jump = float(costs.get("velocityJump", 0.5))
    accel_ratio = float(costs.get("accelRatio", 1.2))
    pareto_bonus = 0.35 if cand.get("paretoFrontier") else 0.0
    hard_veto = -4.5 if costs.get("hasHardDefect") else 0.0

    # Intervention 01 (CZAR-ABS-AUD-006): quadratic perceptual floor for micro-loops when source allows >= 2.0s
    micro_pen = 0.0
    if source_duration >= 2.0 and dur < 1.1:
        micro_pen = ((1.1 - dur) ** 2) * 5.8
    elif source_duration >= 1.2 and dur < 0.75:
        micro_pen = ((0.75 - dur) ** 2) * 4.5

    # Cadence bounds: reject frantic (<55ms) and slideshow (>240ms in 720p/540p)
    frantic_pen = max(0.0, (0.055 - wait) / 0.055) * 1.45 if wait < 0.055 else 0.0
    slideshow_pen = 0.0
    if wait > 0.22:
        slideshow_pen = ((wait - 0.22) / 0.10) ** 1.4 * 1.35
    # Penalize severe frame-to-frame fragmentation (e.g. 3.3 FPS on fast motion -> low internal SSIM)
    frag_pen = max(0.0, 0.85 - int_ssim) * 3.2
    accel_pen = max(0.0, accel_ratio - 1.5) * 0.45

    # Cross-pool caution: if a candidate was evaluated at many more frames (e.g. 24f in 540p -> 13f in 720p),
    # its cached smoothness metrics overestimate quality at the sparser frame rate.
    cross_pen = 0.0
    if cand.get("_crossPool"):
        orig_n = int(cand.get("_origFrames", frames))
        if frames < orig_n:
            cross_pen = 0.35 + max(0.0, (orig_n - frames) / max(orig_n, 1)) * 1.40
        else:
            cross_pen = 0.12

    # Intervention 03: mild diversity nudge when another profile already took the exact same window & frame count
    dup_pen = 0.0
    if used_intervals:
        a, b = float(cand.get("start", 0.0)), float(cand.get("end", dur))
        for ua, ub, ucnt in used_intervals:
            if abs(a - ua) < 0.18 and abs(b - ub) < 0.18:
                dup_pen = max(dup_pen, 0.55 if abs(frames - ucnt) <= 2 else 0.20)

    if profile_id == "quality":
        # Proposta 1 (720p, <=13..16 frames): prioritize visual fidelity + clean motion at >= 6 FPS (1.0-2.2s sweet spot)
        sweet_dur = min(dur, 2.2) * 0.65 - max(0.0, dur - 2.6) * 0.45
        return (
            seam_ssim * 2.6
            + int_ssim * 1.35
            + dir_cos * 1.45
            + min(fps, 12.0) / 12.0 * 1.25
            + sweet_dur
            + pareto_bonus
            + hard_veto
            - seam_ratio * 1.25
            - total_cost * 0.48
            - vel_jump * 0.75
            - accel_pen
            - micro_pen
            - frantic_pen
            - slideshow_pen * 1.2
            - frag_pen
            - cross_pen
            - dup_pen
        )
    if profile_id == "flexibility":
        # Proposta 3 (360p, <=48 frames): prioritize continuity, frame count, temporal regularity & longer cycle
        return (
            min(frames / 48.0, 1.0) * 2.1
            + min(dur, 4.0) / 4.0 * 1.9
            + min(fps, 20.0) / 20.0 * 1.15
            + dir_cos * 1.25
            + seam_ssim * 1.25
            + int_ssim * 0.85
            + pareto_bonus
            + hard_veto
            - seam_ratio * 1.05
            - total_cost * 0.42
            - vel_jump * 0.90
            - accel_pen
            - micro_pen
            - frantic_pen * 0.7
            - slideshow_pen
            - frag_pen
            - cross_pen
            - dup_pen
        )
    # "balance" (540p, <=24 frames): natural movement, balanced continuity, duration (1.6-3.2s) & definition
    return (
        seam_ssim * 2.1
        + int_ssim * 1.15
        + dir_cos * 1.45
        + min(dur, 3.0) / 3.0 * 1.45
        + min(frames / 24.0, 1.0) * 0.95
        + min(fps, 15.0) / 15.0 * 0.90
        + pareto_bonus
        + hard_veto
        - seam_ratio * 1.10
        - total_cost * 0.50
        - vel_jump * 0.85
        - accel_pen
        - micro_pen
        - frantic_pen
        - slideshow_pen
        - frag_pen
        - cross_pen
        - dup_pen
    )


def _adapt_candidate_to_profile(
    cand: dict[str, Any],
    size: tuple[int, int],
    source_duration: float,
    source_fps: float,
    distinct_gif: int | None,
    has_var_gif: bool,
) -> dict[str, Any] | None:
    """Adapt a strong candidate from another resolution group to `size` via SciPy wait optimization (Intervention 01)."""
    cap = max_frames_for(size)
    a = max(0.0, float(cand["start"]))
    b = min(source_duration, float(cand["end"]))
    span = b - a
    if span < 0.25:
        return None
    # Pick optimal frame count within budget targeting 8-14 fps (720p) / 10-16 fps (540p) / 12-18 fps (360p)
    target_fps = 9.5 if size[0] >= 1280 else (12.0 if size[0] >= 960 else 15.0)
    target_fps = min(target_fps, max(source_fps, 8.0))
    n = max(4, min(cap, int(round(span * target_fps))))
    if distinct_gif is not None and not has_var_gif:
        n = max(4, min(n, distinct_gif))
    while n > 4 and n * dds_bytes_for(*size) > RAF_SIZE_LIMIT:
        n -= 1
    opt = optimize_wait_scipy(span, n, grid_sec=WAIT_GRID_SEC)
    wait = float(min(MAX_WAIT_SECONDS, max(MIN_WAIT_SECONDS, opt["snappedWaitSec"])))
    if not (0.78 <= opt["speedRatioAtSnapped"] <= 1.25):
        wait = float(min(MAX_WAIT_SECONDS, max(MIN_WAIT_SECONDS, span / float(n))))
    adapted = dict(cand)
    adapted["_origFrames"] = int(cand.get("frames", n))
    adapted["frames"] = n
    adapted["wait"] = wait
    adapted["size"] = list(size)
    adapted["duration"] = round(n * wait, 4)
    adapted["fps"] = round(1.0 / max(wait, 1e-4), 4)
    adapted["_crossPool"] = True
    return adapted


def _select_profile_plan(
    analysis: dict[str, Any],
    profile: dict[str, Any],
    source_duration: float,
    source_fps: float,
    *,
    used_intervals: list[tuple[float, float, int]] | None = None,
) -> dict[str, Any]:
    """Select an independent (start, end, frames, wait) plan using native + cross-resolution pool & SciPy calibration."""
    size = tuple(profile["size"])
    profile_id = str(profile["id"])
    cap = max_frames_for(size)
    gif_info = analysis.get("gifInfo") or {}
    distinct_gif = int(gif_info["distinctFrameCount"]) if gif_info.get("isGif") else None
    has_var_gif = bool(gif_info.get("hasVariableDelays"))

    # Estimate effective distinct motion rate (protects anime-on-twos/threes like Crown of Thorns from duplicate frames!)
    sent = analysis.get("sentinela") or {}
    freezes_in_survey = len((sent.get("levelD") or {}).get("freezeIndices") or [])
    survey_n = max(1, int((sent.get("levelA") or {}).get("frames") or 1))
    dup_frac = freezes_in_survey / float(survey_n)
    eff_motion_fps = min(max(source_fps, 8.0), 14.0 if dup_frac > 0.08 else 18.0)

    # Collect native candidates AND cross-resolution pool candidates (Intervention 01)
    cands: list[dict[str, Any]] = []
    for grp in analysis.get("groups") or []:
        g_size = tuple(grp.get("size") or ())
        for c in grp.get("candidates") or []:
            if g_size == size:
                cands.append(c)
            elif not (c.get("costs") or {}).get("isStatic"):
                adapted = _adapt_candidate_to_profile(c, size, source_duration, eff_motion_fps, distinct_gif, has_var_gif)
                if adapted is not None:
                    cands.append(adapted)

    if cands:
        best = max(
            cands,
            key=lambda c: _score_candidate_for_profile(
                c, profile_id, source_duration=source_duration, used_intervals=used_intervals
            ),
        )
        start = max(0.0, float(best["start"]))
        end = min(source_duration, float(best["end"]))
        frames = max(4, min(cap, int(best["frames"])))
        wait = float(best["wait"])
        span = max(0.15, end - start)
        cand_src = "cross_pool_search" if best.get("_crossPool") else "global_search"

        # Intervention 03: expand flexibility (360p) starting at span >= 0.95s, capped by effective motion FPS
        if (
            profile_id == "flexibility"
            and frames < cap
            and span >= 0.95
            and (distinct_gif is None or frames < distinct_gif)
        ):
            target_n = min(cap, max(frames, int(round(span * eff_motion_fps))))
            if distinct_gif is not None and not has_var_gif:
                target_n = min(target_n, distinct_gif)
            if target_n > frames and target_n * dds_bytes_for(*size) <= RAF_SIZE_LIMIT and target_n >= 4:
                opt = optimize_wait_scipy(span, target_n, grid_sec=WAIT_GRID_SEC)
                if 0.80 <= opt["speedRatioAtSnapped"] <= 1.24:
                    frames = target_n
                    wait = opt["snappedWaitSec"]
        return {
            "start": round(start, 4),
            "end": round(end, 4),
            "frames": frames,
            "wait": float(min(MAX_WAIT_SECONDS, max(MIN_WAIT_SECONDS, wait))),
            "candidateSource": cand_src,
            "paretoFrontier": bool(best.get("paretoFrontier", False)),
            "phaseLocked": bool(best.get("phaseLocked", False)),
        }

    # Independent profile-tailored fallback when search group had no shortlisted seeds
    if profile_id == "quality":
        target_dur = min(source_duration, 1.5)
        target_n = min(cap, max(6, int(round(target_dur * 9.0))))
    elif profile_id == "flexibility":
        target_dur = min(source_duration, 3.2)
        target_n = min(cap, max(8, int(round(target_dur * min(eff_motion_fps, 15.0)))))
    else:
        target_dur = min(source_duration, 2.2)
        target_n = min(cap, max(8, int(round(target_dur * 11.0))))

    if distinct_gif is not None and not has_var_gif:
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
        "paretoFrontier": False,
        "phaseLocked": False,
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
    ctq = dds_d.get("compressedTemporalQuality") or {}
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
        "ddsMeanLabDeltaE": dds_d.get("meanLabDeltaE"),
        "staticBlockShimmerMad": ctq.get("staticBlockShimmerMad"),
        "bc1Backend": dds_d.get("backend"),
        "distinctFrames": dds_d.get("distinctFrames"),
        "withinBudget": bool(dds_d.get("withinBudget", True)),
        "tenCyclePass": bool(ten.get("continuousTenCyclePass", False)),
        "tenCycleDurationSec": ten.get("tenCycleDurationSec"),
        "issues": list(ev.get("issues") or []),
    }


def _verdict_rank(verdict: str) -> int:
    return {"APROVADO": 3, "CONDICIONAL": 2, "REPROVADO": 1}.get(verdict, 0)


def _build_proposal_tradeoffs(
    profile_id: str,
    size: tuple[int, int],
    rend: dict[str, Any],
    plan: dict[str, Any],
    metrics: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Build honest, evidence-backed advantages and limitations for each proposal card."""
    advantages: list[str] = []
    limitations: list[str] = []

    if profile_id == "quality":
        advantages.append(f"Maior nitidez espacial ({size[0]}×{size[1]}) para detalhes finos na TV")
    elif profile_id == "balance":
        advantages.append(f"Equilíbrio entre nitidez ({size[0]}×{size[1]}) e cadência fluida ({rend['fps']:.1f} FPS)")
    else:
        advantages.append(f"Maior margem temporal ({rend['frames']} quadros · {rend['duration']:.2f}s no teto de 6 MiB)")

    if metrics["passed"] and metrics["tenCyclePass"]:
        advantages.append("Validado pós-DDS e estável ao longo de 10 ciclos contínuos")
    elif metrics["seamSsim"] >= 0.90:
        advantages.append(f"Emenda visual discreta (SSIM {metrics['seamSsim']:.3f})")

    if metrics["ddsMeanPsnr"] is not None and float(metrics["ddsMeanPsnr"]) >= 38.0:
        advantages.append(f"Alta fidelidade pós-DXT1a ({float(metrics['ddsMeanPsnr']):.1f} dB)")
    if plan.get("phaseLocked"):
        advantages.append("Fechamento travado na fase exata do quadro nativo")

    if profile_id == "quality" and rend["frames"] <= 13:
        limitations.append(f"Limite físico de {max_frames_for(size)} quadros em 720p restringe duração/FPS")
    elif profile_id == "flexibility":
        limitations.append("Menor resolução espacial (640×360) quando ampliada na tela inteira")

    if rend["fps"] < 6.0:
        limitations.append(f"Cadência baixa ({rend['fps']:.1f} FPS) para caber no teto RAF")
    if rend["duration"] < 1.2:
        limitations.append(f"Ciclo curto ({rend['duration']:.2f}s) pode parecer repetitivo em cenas rápidas")

    for iss in list(metrics.get("issues") or [])[:2]:
        if iss not in limitations:
            limitations.append(iss)

    if not limitations:
        limitations.append("Limitado apenas pela compressão BC1/DXT1a (RGB565) e teto de 6 MiB do PS4")
    return advantages[:3], limitations[:3]


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
    used_intervals: list[tuple[float, float, int]] = []

    for idx, prof in enumerate(PROPOSAL_PROFILES):
        check()
        pid = str(prof["id"])
        size = tuple(prof["size"])
        base_pct = 40 + idx * 18
        progress(f"Otimizando e validando {prof['title']} ({size[0]}×{size[1]})", base_pct)

        plan = _select_profile_plan(analysis, prof, eff_dur, src_fps, used_intervals=used_intervals)
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
        used_intervals.append((float(plan["start"]), float(plan["end"]), int(rend["frames"])))

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

        compact_m = _compact_metrics_summary(rend)
        advantages, limitations = _build_proposal_tradeoffs(pid, size, rend, plan, compact_m)

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
            "paretoFrontier": bool(plan.get("paretoFrontier", False)),
            "phaseLocked": bool(plan.get("phaseLocked", False)),
            "advantages": advantages,
            "limitations": limitations,
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
            "metrics": compact_m,
            "ddsHashes": rend["ddsHashes"],
            "files": artifact_digests,
            "recipe": rend["recipe"],
        }
        proposals.append(proposal_card)
        import gc as _gc
        from .flow import clear_flow_cache as _clear_flow_cache

        _clear_flow_cache()
        _gc.collect()

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
