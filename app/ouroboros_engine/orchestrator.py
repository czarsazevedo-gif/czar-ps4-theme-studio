"""Single public orchestrator (`OuroborosEngine`) coordinating all internal loop strategies.

Canonical flow:
  IMPORTAR -> INSPECIONAR -> ANALISAR -> BUSCAR -> RECONSTRUIR -> CALIBRAR ->
  CODIFICAR -> VALIDAR -> APRESENTAR TRÊS LOOPS -> APLICAR
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Literal

import numpy as np
from PIL import Image

from .. import theme
from ..config import ANIMATED_SIZES, MAX_WAIT_SECONDS, MIN_WAIT_SECONDS, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from . import INTERNAL_STRATEGIES, OUROBOROS_VERSION
from . import media_io as io_media
from .analyzer import analyze_levels
from .applier import apply_loop_to_project, assemble_canonical_raf, sha256_bytes
from .calibrator import calibrate_interval
from .diagnostic import build_media_diagnostic
from .finish import apply_deluxe_finish
from .gif_inspector import extract_gif_continuous_cadence, inspect_gif, is_gif_file
from .metrics import compare_before_after, evaluate_post_dds, evaluate_ten_cycles
from .reconstruction import (
    level2_photometric_harmonize,
    level3_motion_compensated_seam,
    level4_content_bridge_reconstruct,
    reconstruct_best,
)
from .regions import RegionalRecipe, apply_regional_recipe, compute_diff_stats, rasterize_region_mask
from .search import search_supreme
from .synthesis import SynthesisRejected, synthesize_genesis_supreme

RenderMode = Literal[
    "auto",
    "natural",
    "reconstruct_auto",
    "photometric",
    "motion_comp",
    "content_bridge",
    "genesis_2d",
    "regional",
]


def _strategy_code_for(strategy_used: str) -> str:
    if strategy_used in {"natural", "level1_natural"}:
        return "A"
    if "photometric" in strategy_used or "bridge" in strategy_used or "level2" in strategy_used or "level4" in strategy_used:
        return "B"
    if "motion" in strategy_used or "level3" in strategy_used:
        return "C"
    if "regional" in strategy_used or "level6" in strategy_used:
        return "D"
    if "genesis" in strategy_used or "synthesis" in strategy_used or "level5" in strategy_used:
        return "E"
    return "A"


class OuroborosEngine:
    """Single public orchestrator for Ouroboros visual loop engineering."""

    version = OUROBOROS_VERSION
    strategies = INTERNAL_STRATEGIES

    def inspect_source(
        self,
        source_path: Path,
        duration: float,
        *,
        work_dir: Path | None = None,
        check: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        """Step 1: Inspect source metadata (FFprobe + PyAV PTS + Gifsicle timing for GIFs)."""
        info = io_media.source_info(source_path, duration, check)
        if is_gif_file(source_path):
            gif_info = inspect_gif(source_path, work_dir=work_dir, check=check)
            info["gifInfo"] = gif_info
            if gif_info.get("totalDurationSec", 0) > 0:
                info["duration"] = float(gif_info["totalDurationSec"])
            if gif_info.get("hasVariableDelays"):
                info["irregular"] = True
        else:
            info["gifInfo"] = {"isGif": False}
        return info

    def analyze_and_search(
        self,
        source_path: Path,
        duration: float,
        work_dir: Path,
        *,
        filename: str = "",
        source_sha256: str = "",
        progress: Callable[[str, int], None] = lambda *_: None,
        check: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        """Steps 2-3: Full-duration motion/scene analysis, Gifsicle inspection, global search & 21-field diagnostic."""
        sha = source_sha256 or io_media.digest(source_path, check)
        gif_info = inspect_gif(source_path, work_dir=work_dir, check=check) if is_gif_file(source_path) else {"isGif": False}
        eff_dur = float(gif_info["totalDurationSec"]) if gif_info.get("isGif") and gif_info.get("totalDurationSec", 0) > 0 else duration
        result = search_supreme(
            source_path,
            eff_dur,
            work_dir,
            source_sha256=sha,
            progress=progress,
            check=check,
        )
        result["version"] = OUROBOROS_VERSION
        result["sha256"] = sha
        result["name"] = filename or source_path.name
        result["gifInfo"] = gif_info
        result["mediaDiagnostic"] = build_media_diagnostic(result, filename=result["name"])
        return result

    def calibrate(
        self,
        source_path: Path,
        start: float,
        end: float,
        source_fps: float,
        work_dir: Path,
        *,
        verify_dds: bool = False,
        check: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        """Step 5: Temporal cadence calibration across canonical PS4 intervals (25..150 ms)."""
        return calibrate_interval(
            source_path,
            start,
            end,
            source_fps=source_fps,
            folder=work_dir,
            verify_dds=verify_dds,
            check=check,
        )

    def render_loop(
        self,
        source_path: Path,
        source_duration: float,
        out_folder: Path,
        *,
        start: float,
        end: float,
        frames: int,
        size: tuple[int, int],
        wait: float,
        mode: RenderMode = "auto",
        gif_timing_mode: Literal["auto", "mode_a_preserve", "mode_b_reconstruct"] = "auto",
        recipe: RegionalRecipe | None = None,
        finish_enabled: bool = True,
        stabilize_camera: bool = False,
        speed_scale: float = 1.0,
        source_sha256: str = "",
        preextracted_frames: np.ndarray | None = None,
        progress: Callable[[str, int], None] = lambda *_: None,
        check: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        """Steps 4, 6, 7: Reconstruct selectively -> Post-DDS finish -> Encode canonical DXT1a -> Validate."""
        t0 = time.perf_counter()
        eff_mode = "reconstruct_auto" if mode == "auto" else mode
        if size not in ANIMATED_SIZES:
            raise io_media.MotionError("Resolução fora das especificações canônicas do PS4.")
        if not 4 <= frames <= max_frames_for(size):
            raise io_media.MotionError("Quantidade de quadros excede o teto canônico para esta resolução.")
        if not (MIN_WAIT_SECONDS <= wait <= MAX_WAIT_SECONDS):
            raise io_media.MotionError("Intervalo wait fora dos limites canônicos (0.02s a 5.0s).")
        if frames * dds_bytes_for(*size) > RAF_SIZE_LIMIT:
            raise io_media.MotionError("Sequência excede o limite de 6 MiB do background.raf.")

        start_c = max(0.0, float(start))
        end_c = min(float(source_duration), float(end))
        if end_c <= start_c + 0.05 and preextracted_frames is None:
            raise io_media.MotionError("Intervalo de origem inválido.")

        out_folder.mkdir(parents=True, exist_ok=True)
        progress("Extraindo quadros da fonte na resolução de saída", 12)
        check()
        gif_timing_meta: dict[str, Any] | None = None
        if preextracted_frames is not None:
            raw_frames = np.asarray(preextracted_frames, dtype=np.uint8)
            frames = int(raw_frames.shape[0])
        elif is_gif_file(source_path):
            raw_frames, gif_timing_meta = extract_gif_continuous_cadence(
                source_path, start_c, end_c, frames, size, out_folder, mode=gif_timing_mode, check=check
            )
            frames = int(raw_frames.shape[0])
        else:
            raw_frames = np.array(io_media.extract(source_path, start_c, end_c, frames, size, out_folder, check))
            if eff_mode == "reconstruct_auto" and len(raw_frames) >= 8:
                step_maes = np.abs(raw_frames[1:].astype(np.float32) - raw_frames[:-1].astype(np.float32)).mean(
                    axis=(1, 2, 3)
                )
                med_step = float(np.median(step_maes))
                if med_step > 0.6:
                    stall_thresh = max(0.15, 0.14 * med_step)
                    keep_idx = [0] + [i + 1 for i, m in enumerate(step_maes) if float(m) >= stall_thresh]
                    # Also check circular wrap duplicate between last kept frame and frame 0
                    if len(keep_idx) > 6 and float(np.abs(raw_frames[keep_idx[-1]].astype(np.float32) - raw_frames[0].astype(np.float32)).mean()) < stall_thresh:
                        keep_idx.pop()
                    if 6 <= len(keep_idx) < len(raw_frames) and len(keep_idx) >= int(len(raw_frames) * 0.60):
                        from .calibrator import WAIT_GRID_SEC, optimize_wait_scipy

                        raw_frames = raw_frames[keep_idx]
                        frames = int(raw_frames.shape[0])
                        opt_w = optimize_wait_scipy(end_c - start_c, frames, grid_sec=WAIT_GRID_SEC)
                        if 0.78 <= opt_w["speedRatioAtSnapped"] <= 1.25:
                            wait = float(min(MAX_WAIT_SECONDS, max(MIN_WAIT_SECONDS, opt_w["snappedWaitSec"])))
                        else:
                            wait = float(min(MAX_WAIT_SECONDS, max(MIN_WAIT_SECONDS, (end_c - start_c) / float(frames))))
        h, w = raw_frames.shape[1:3]
        Image.fromarray(raw_frames[0]).save(out_folder / "source.png")

        lead_in = None
        lead_out = None
        step_sec = max(0.01, (end_c - start_c) / float(frames))
        bridge_k = min(5, max(2, frames // 3))
        if preextracted_frames is None and not is_gif_file(source_path) and eff_mode in {"reconstruct_auto", "content_bridge"}:
            if start_c >= bridge_k * step_sec + 0.02:
                try:
                    lead_in = np.array(
                        io_media.extract(source_path, start_c - bridge_k * step_sec, start_c, bridge_k, size, out_folder, check)
                    )
                except io_media.MotionError:
                    lead_in = None
            if end_c + bridge_k * step_sec <= source_duration - 0.02:
                try:
                    lead_out = np.array(
                        io_media.extract(source_path, end_c, end_c + bridge_k * step_sec, bridge_k, size, out_folder, check)
                    )
                except io_media.MotionError:
                    lead_out = None

        protected_mask = None
        if recipe is not None and recipe.regions:
            p_union = np.zeros((h, w), dtype=bool)
            for reg in recipe.regions:
                if reg.enabled and reg.strategy == "protect":
                    p_union |= rasterize_region_mask(reg, (w, h))
            if p_union.any():
                protected_mask = p_union

        progress("Selecionando estratégia interna Ouroboros", 35)
        check()
        strategy_used = eff_mode
        strategy_meta: dict[str, Any] = {}

        if eff_mode == "natural":
            candidate_frames = raw_frames
        elif eff_mode == "photometric":
            candidate_frames = level2_photometric_harmonize(raw_frames, protected_mask=protected_mask)
        elif eff_mode == "motion_comp":
            candidate_frames, strategy_meta = level3_motion_compensated_seam(raw_frames, protected_mask=protected_mask)
        elif eff_mode == "content_bridge":
            candidate_frames, strategy_meta = level4_content_bridge_reconstruct(
                raw_frames,
                lead_in=lead_in,
                lead_out=lead_out,
                protected_mask=protected_mask,
                stabilize_camera=stabilize_camera,
            )
        elif eff_mode == "genesis_2d":
            try:
                g_res = synthesize_genesis_supreme(
                    raw_frames,
                    n_frames=frames,
                    wait=wait,
                    protected_mask=protected_mask,
                    speed_scale=speed_scale,
                    verify_dds=False,
                )
                candidate_frames = g_res["frames"]
                strategy_meta = g_res["flowStats"]
            except SynthesisRejected as exc:
                raise io_media.MotionError(str(exc)) from exc
        elif eff_mode == "regional":
            if recipe is None or not recipe.regions:
                raise io_media.MotionError("Adicione ao menos uma região antes de renderizar em modo regional.")
            r_res = apply_regional_recipe(raw_frames, wait, recipe, verify_dds=False)
            candidate_frames = r_res["frames"]
            strategy_meta = r_res["meta"]
        else:
            auto_sent = analyze_levels(raw_frames, dt=step_sec, check=check)
            rec_res = reconstruct_best(
                raw_frames,
                wait,
                lead_in=lead_in,
                lead_out=lead_out,
                protected_mask=protected_mask,
                sentinela_report=auto_sent,
                stabilize_camera=stabilize_camera,
                verify_dds=False,
            )
            candidate_frames = rec_res["frames"]
            strategy_used = rec_res["selectedStrategy"]
            strategy_meta = {"attempts": rec_res["attempts"], "improved": rec_res["improved"]}
            del auto_sent, rec_res

        lead_in = None
        lead_out = None
        progress("Validando sequência DXT1a real e aplicando acabamento pós-DDS", 68)
        check()
        same_as_raw = candidate_frames is raw_frames
        raw_full = evaluate_post_dds(raw_frames, wait, protected_mask=protected_mask, return_blobs=same_as_raw)
        raw_post_dds = raw_full["report"] if same_as_raw else raw_full

        if finish_enabled:
            fin = apply_deluxe_finish(
                candidate_frames,
                wait,
                protected_mask=protected_mask,
                reference_frames=raw_frames,
                precomputed_baseline=raw_full if same_as_raw else None,
            )
            final_frames = fin["frames"]
            dds_blobs = fin["ddsBlobs"]
            decoded_frames = fin["decodedDdsFrames"]
            final_post_dds = fin["finalPostDds"]
            finish_meta = {
                "enabled": True,
                "appliedSteps": fin["appliedSteps"],
                "rejectedSteps": fin["rejectedSteps"],
                "beforeAfterFinish": fin["comparison"],
            }
            del fin
        elif same_as_raw:
            final_frames = candidate_frames
            dds_blobs = raw_full["blobs"]
            decoded_frames = raw_full["decoded"]
            final_post_dds = raw_post_dds
            finish_meta = {"enabled": False, "appliedSteps": [], "rejectedSteps": []}
        else:
            ev_res = evaluate_post_dds(
                candidate_frames, wait, protected_mask=protected_mask, reference_frames=raw_frames, return_blobs=True
            )
            final_frames = candidate_frames
            dds_blobs = ev_res["blobs"]
            decoded_frames = ev_res["decoded"]
            final_post_dds = ev_res["report"]
            finish_meta = {"enabled": False, "appliedSteps": [], "rejectedSteps": []}
            del ev_res

        overall_cmp = compare_before_after(raw_post_dds, final_post_dds)
        ten_cycle = evaluate_ten_cycles(final_post_dds)
        from .metrics import classify_artifact_provenance, classify_loop_verdict

        verdict_info = classify_loop_verdict(final_post_dds, ten_cycle, strategy_used=strategy_used)

        # Attribute issues honestly: present in source baseline vs newly introduced by reconstruction vs DXT1a compression
        raw_issues = set(raw_post_dds.get("issues") or [])
        final_issues = list(final_post_dds.get("issues") or [])
        source_issues = [iss for iss in final_issues if iss in raw_issues]
        introduced_issues = [iss for iss in final_issues if iss not in raw_issues]
        artifact_provenance = classify_artifact_provenance(
            final_issues,
            raw_issues,
            (final_post_dds.get("metrics") or {}).get("ddsDelta"),
            strategy_used=strategy_used,
        )

        progress("Gravando prévias dos DDS decodificados e cena RAF canônica", 88)
        check()
        frames_dir = out_folder / "deluxe_frames"
        dds_dir = out_folder / "deluxe_dds"
        frames_dir.mkdir(exist_ok=True)
        dds_dir.mkdir(exist_ok=True)

        dds_hashes: dict[str, str] = {}
        for i in range(frames):
            check()
            Image.fromarray(final_frames[i]).save(frames_dir / f"{i:02d}.png", "PNG", compress_level=1)
            d_name = theme.frame_name(i)
            (dds_dir / d_name).write_bytes(dds_blobs[i])
            dds_hashes[d_name] = sha256_bytes(dds_blobs[i])
            Image.fromarray(decoded_frames[i]).save(out_folder / f"{i:02d}.webp", "WEBP", lossless=True, method=0)

        diff_info = compute_diff_stats(raw_frames, decoded_frames)
        Image.fromarray(diff_info["heatmap"]).save(out_folder / "diff.png", "PNG", compress_level=1)
        from .applier import compute_variable_wait_schedule

        wait_schedule = compute_variable_wait_schedule(decoded_frames, wait)
        total_dds_bytes, _ = assemble_canonical_raf(
            dds_blobs, wait, out_folder / "background.raf.zip", wait_list=wait_schedule
        )
        del raw_frames, candidate_frames, final_frames, decoded_frames, dds_blobs, raw_full

        strat_code = _strategy_code_for(strategy_used)
        return {
            "type": "ouroboros_render",
            "version": OUROBOROS_VERSION,
            "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
            "frames": frames,
            "wait": float(wait),
            "waitMs": round(float(wait) * 1000.0, 3),
            "waitSchedule": wait_schedule,
            "fps": round(1.0 / float(wait), 4),
            "duration": round(frames * float(wait), 4),
            "size": list(size),
            "bytes": total_dds_bytes,
            "budgetPercent": round(total_dds_bytes / float(RAF_SIZE_LIMIT) * 100.0, 2),
            "mode": eff_mode,
            "strategyCode": strat_code,
            "strategyUsed": strategy_used,
            "strategyLabel": INTERNAL_STRATEGIES[strat_code][1],
            "strategyMeta": strategy_meta,
            "gifTiming": gif_timing_meta,
            "finish": finish_meta,
            "diffStats": {k: v for k, v in diff_info.items() if k != "heatmap"},
            "baselinePostDds": raw_post_dds,
            "finalPostDds": final_post_dds,
            "verdict": verdict_info["verdict"],
            "verdictSummary": verdict_info["summary"],
            "prohibitedArtifacts": verdict_info["prohibited"],
            "issueAttribution": {
                "sourceIssues": source_issues,
                "introducedIssues": introduced_issues,
            },
            "artifactProvenance": artifact_provenance,
            "tenCycleAudit": ten_cycle,
            "beforeAfterRaw": overall_cmp,
            "ddsHashes": dds_hashes,
            "recipe": {
                "algorithmVersion": OUROBOROS_VERSION,
                "sourceSha256": source_sha256,
                "start": start_c,
                "end": end_c,
                "frames": frames,
                "wait": float(wait),
                "size": list(size),
                "mode": eff_mode,
                "strategyUsed": strategy_used,
                "finishEnabled": finish_enabled,
                "speedScale": speed_scale,
                "stabilizeCamera": stabilize_camera,
                "regionalRecipe": recipe.model_dump() if recipe is not None else None,
            },
        }

    def generate_three_proposals(
        self,
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
        """Steps 1-9: Full pipeline producing 3 independently optimized proposals (720p, 540p, 360p)."""
        from .proposals import generate_three_proposals as _gen

        return _gen(
            self,
            source_path,
            source_duration,
            out_folder,
            filename=filename,
            source_sha256=source_sha256,
            recipe=recipe,
            stabilize_camera=stabilize_camera,
            progress=progress,
            check=check,
        )

    def apply_to_project(
        self,
        project_root: Path,
        result_folder: Path,
        report: dict[str, Any],
        *,
        display_name: str | None = None,
        acquire_lock: bool = True,
    ) -> dict[str, Any]:
        """Step 10: Transactionally apply a validated render/proposal directly to the active project."""
        return apply_loop_to_project(
            project_root,
            result_folder,
            report,
            display_name=display_name,
            acquire_lock=acquire_lock,
        )
