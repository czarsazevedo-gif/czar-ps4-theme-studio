"""End-to-end Ouroboros Deluxe render pipeline, canonical RAF assembly, and transactional project application."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import numpy as np
from PIL import Image

from .. import jobs, mdx, media, ouroboros_media as io_media, projects, raf, theme
from ..config import (
    ANIMATED_SIZES,
    MAX_WAIT_SECONDS,
    MIN_WAIT_SECONDS,
    PKG_DEFAULTS,
    RAF_SIZE_LIMIT,
    dds_bytes_for,
    max_frames_for,
)
from . import DELUXE_VERSION
from .finish import apply_deluxe_finish
from .genesis_supreme import SynthesisRejected, synthesize_genesis_supreme
from .metrics import compare_before_after, evaluate_post_dds
from .reconstructor import (
    level2_photometric_harmonize,
    level3_motion_compensated_seam,
    level4_content_bridge_reconstruct,
    reconstruct_best,
)
from .regions import RegionalRecipe, apply_regional_recipe, compute_diff_stats, rasterize_region_mask

RenderMode = Literal[
    "natural",
    "reconstruct_auto",
    "photometric",
    "motion_comp",
    "content_bridge",
    "genesis_2d",
    "regional",
]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_lossless_mp4(path: Path, frames: np.ndarray, fps: float) -> None:
    """Write exact RGB frames into a lossless libx264rgb MP4 for canonical compatibility."""
    _, h, w, _ = frames.shape
    subprocess.run(
        [
            media.FFMPEG,
            "-nostdin",
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
            f"{max(fps, 0.2):.6f}",
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
        timeout=90,
    )


def _assemble_canonical_raf(dds_blobs: list[bytes], wait: float, dest_zip: Path) -> tuple[int, str]:
    """Build canonical scene/background.raf/ ZIP (DXT1a DDS + plane.mdx + index.xml)."""
    count = len(dds_blobs)
    total_dds = sum(len(b) for b in dds_blobs)
    theme.check_frame_count(count)
    theme.check_budget(total_dds)

    with tempfile.TemporaryDirectory(dir=dest_zip.parent) as tmp:
        tree = Path(tmp) / "background.raf"
        tree.mkdir()
        names = []
        for i, blob in enumerate(dds_blobs):
            name = theme.frame_name(i)
            (tree / name).write_bytes(blob)
            names.append(name)
        half_w, half_h = raf.plane_half_size()
        (tree / raf.MODEL_REAL_FILE).write_bytes(mdx.quad_model(node_name=raf.MODEL_ID, half_w=half_w, half_h=half_h))
        index_xml = raf.build_scene(names, wait)
        theme.write_index_xml(tree / "index.xml", index_xml)
        theme.zip_raf_folder(tree, dest_zip)
    return total_dds, index_xml


def render_deluxe_loop(
    source_path: Path,
    source_duration: float,
    out_folder: Path,
    *,
    start: float,
    end: float,
    frames: int,
    size: tuple[int, int],
    wait: float,
    mode: RenderMode = "reconstruct_auto",
    recipe: RegionalRecipe | None = None,
    deluxe_finish: bool = True,
    stabilize_camera: bool = False,
    speed_scale: float = 1.0,
    source_sha256: str = "",
    progress: Callable[[str, int], None] = lambda *_: None,
    check: Callable[[], None] = lambda: None,
) -> dict:
    """Render, refine, apply Deluxe Finish, encode/decode canonical DDS DXT1a, validate, and build RAF."""
    t0 = time.perf_counter()
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
    if end_c <= start_c + 0.05:
        raise io_media.MotionError("Intervalo de origem inválido.")

    progress("Extraindo quadros da fonte na resolução de saída", 10)
    check()
    raw_frames = np.array(io_media.extract(source_path, start_c, end_c, frames, size, out_folder, check))
    h, w = raw_frames.shape[1:3]
    Image.fromarray(raw_frames[0]).save(out_folder / "source.png")

    # Optional lead-in / lead-out content pieces from the source for Level 4 content bridging
    lead_in = None
    lead_out = None
    step_sec = (end_c - start_c) / float(frames)
    bridge_k = min(5, max(2, frames // 3))
    if mode in {"reconstruct_auto", "content_bridge"}:
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

    # Build protected mask union if regional recipe defines protected regions
    protected_mask = None
    if recipe is not None and recipe.regions:
        p_union = np.zeros((h, w), dtype=bool)
        for reg in recipe.regions:
            if reg.enabled and reg.strategy == "protect":
                p_union |= rasterize_region_mask(reg, (w, h))
        if p_union.any():
            protected_mask = p_union

    progress(f"Executando motor de movimento ({mode})", 32)
    check()
    strategy_used = mode
    strategy_meta: dict = {}

    if mode == "natural":
        candidate_frames = raw_frames
    elif mode == "photometric":
        candidate_frames = level2_photometric_harmonize(raw_frames, protected_mask=protected_mask)
    elif mode == "motion_comp":
        candidate_frames, strategy_meta = level3_motion_compensated_seam(raw_frames, protected_mask=protected_mask)
    elif mode == "content_bridge":
        candidate_frames, strategy_meta = level4_content_bridge_reconstruct(
            raw_frames,
            lead_in=lead_in,
            lead_out=lead_out,
            protected_mask=protected_mask,
            stabilize_camera=stabilize_camera,
        )
    elif mode == "genesis_2d":
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
    elif mode == "regional":
        if recipe is None or not recipe.regions:
            raise io_media.MotionError("Adicione ao menos uma região antes de renderizar em modo regional.")
        r_res = apply_regional_recipe(raw_frames, wait, recipe, verify_dds=False)
        candidate_frames = r_res["frames"]
        strategy_meta = r_res["meta"]
    else:  # reconstruct_auto
        from .sentinela_supreme import analyze_levels

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

    progress("Aplicando Ouroboros Deluxe Finish e validando após DDS DXT1a", 65)
    check()
    # Baseline post-DDS of raw extracted frames for true before/after scientific matrix
    raw_post_dds = evaluate_post_dds(raw_frames, wait, protected_mask=protected_mask)

    if deluxe_finish:
        fin = apply_deluxe_finish(
            candidate_frames,
            wait,
            protected_mask=protected_mask,
            reference_frames=raw_frames,
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
    else:
        ev_res = evaluate_post_dds(
            candidate_frames, wait, protected_mask=protected_mask, reference_frames=raw_frames, return_blobs=True
        )
        final_frames = candidate_frames
        dds_blobs = ev_res["blobs"]
        decoded_frames = ev_res["decoded"]
        final_post_dds = ev_res["report"]
        finish_meta = {"enabled": False, "appliedSteps": [], "rejectedSteps": []}

    overall_cmp = compare_before_after(raw_post_dds, final_post_dds)

    progress("Gravando prévias lossless dos DDS decodificados e cena RAF canônica", 88)
    check()
    frames_dir = out_folder / "deluxe_frames"
    dds_dir = out_folder / "deluxe_dds"
    frames_dir.mkdir(exist_ok=True)
    dds_dir.mkdir(exist_ok=True)

    dds_hashes: dict[str, str] = {}
    for i in range(frames):
        check()
        # Exact lossless pre-DDS frame (used if compositor overlays are added later)
        Image.fromarray(final_frames[i]).save(frames_dir / f"{i:02d}.png", "PNG")
        # Exact canonical DDS blob
        d_name = theme.frame_name(i)
        (dds_dir / d_name).write_bytes(dds_blobs[i])
        dds_hashes[d_name] = _sha256_bytes(dds_blobs[i])
        # Lossless WebP from REAL decoded DDS frame (what PS4 displays!)
        Image.fromarray(decoded_frames[i]).save(out_folder / f"{i:02d}.webp", "WEBP", lossless=True, method=0)

    # Visual diff heatmap against raw source
    diff_info = compute_diff_stats(raw_frames, decoded_frames)
    Image.fromarray(diff_info["heatmap"]).save(out_folder / "diff.png", "PNG")

    total_dds_bytes, _ = _assemble_canonical_raf(dds_blobs, wait, out_folder / "background.raf.zip")

    return {
        "type": "deluxe_render",
        "version": DELUXE_VERSION,
        "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
        "frames": frames,
        "wait": float(wait),
        "fps": round(1.0 / float(wait), 4),
        "duration": round(frames * float(wait), 4),
        "size": list(size),
        "bytes": total_dds_bytes,
        "mode": mode,
        "strategyUsed": strategy_used,
        "strategyMeta": strategy_meta,
        "finish": finish_meta,
        "diffStats": {k: v for k, v in diff_info.items() if k != "heatmap"},
        "baselinePostDds": raw_post_dds,
        "finalPostDds": final_post_dds,
        "beforeAfterRaw": overall_cmp,
        "ddsHashes": dds_hashes,
        "recipe": {
            "algorithmVersion": DELUXE_VERSION,
            "sourceSha256": source_sha256,
            "start": start_c,
            "end": end_c,
            "frames": frames,
            "wait": float(wait),
            "size": list(size),
            "mode": mode,
            "strategyUsed": strategy_used,
            "deluxeFinish": deluxe_finish,
            "speedScale": speed_scale,
            "stabilizeCamera": stabilize_camera,
            "regionalRecipe": recipe.model_dump() if recipe is not None else None,
        },
    }


def _default_editor_state() -> projects.Editor:
    return projects.Editor(
        campos=projects.ThemeFields(
            **{
                "pkg-title": "Tema PS4 Deluxe",
                "pkg-title-id": PKG_DEFAULTS["title_id"],
                "pkg-content-id": f"{PKG_DEFAULTS['content_prefix']}-{PKG_DEFAULTS['title_id']}_00-0000000000000000",
                "pkg-themecolor": "0",
                "pkg-fontcolor": "#ffffff",
                "pkg-fontshadow": "#000000",
                "pkg-focuscolor": "#1e9bff",
            }
        ),
        bgMode="animated",
        contentIdTouched=False,
        slots={},
        bgm=None,
        bgmGain=0.0,
        sfx={},
        anim=None,
    )


def apply_deluxe_to_project(
    project_root: Path,
    result_folder: Path,
    report: dict,
    *,
    display_name: str | None = None,
    acquire_lock: bool = True,
) -> dict:
    """Transactionally apply a completed Deluxe render to the active project's animated background.

    Guarantees:
      - Verifies every DDS blob, hash, and RAF budget before mutating the project.
      - Stages the new animated job completely before updating manifest.json under revision_lock.
      - On any failure, rolls back the staged job and preserves the prior project version untouched!
    """
    if report.get("type") != "deluxe_render":
        raise ValueError("Somente resultados renderizados do Ouroboros Deluxe podem ser aplicados.")

    frames = int(report["frames"])
    size = tuple(report["size"])
    wait = float(report["wait"])
    dds_hashes: dict[str, str] = report.get("ddsHashes") or {}

    if size not in ANIMATED_SIZES or not 4 <= frames <= max_frames_for(size):
        raise ValueError("Orçamento ou resolução incompatível com o projeto.")
    expected_bytes = frames * dds_bytes_for(*size)
    if expected_bytes > RAF_SIZE_LIMIT:
        raise ValueError("Sequência DDS excede o limite de 6 MiB.")

    dds_src = result_folder / "deluxe_dds"
    png_src = result_folder / "deluxe_frames"
    raf_src = result_folder / "background.raf.zip"
    if not (dds_src.is_dir() and png_src.is_dir() and raf_src.is_file()):
        raise ValueError("Artefatos renderizados incompletos para aplicação.")

    # Verify every DDS blob and PNG frame before staging
    loaded_frames = []
    total_bytes = 0
    for i in range(frames):
        d_name = theme.frame_name(i)
        d_path = dds_src / d_name
        p_path = png_src / f"{i:02d}.png"
        w_path = result_folder / f"{i:02d}.webp"
        if not (d_path.is_file() and p_path.is_file() and w_path.is_file()):
            raise ValueError(f"Quadro {i:02d} ausente nos derivados renderizados.")
        blob = d_path.read_bytes()
        if dds_hashes.get(d_name) and _sha256_bytes(blob) != dds_hashes[d_name]:
            raise ValueError(f"Integridade SHA-256 falhou para {d_name}.")
        if blob[:4] != b"DDS " or blob[84:88] != b"DXT1":
            raise ValueError(f"Cabeçalho DDS inválido em {d_name}.")
        total_bytes += len(blob)
        with Image.open(p_path) as im:
            loaded_frames.append(np.asarray(im.convert("RGB")))

    if total_bytes != expected_bytes:
        raise ValueError("Soma real de bytes DDS diverge do orçamento esperado.")

    media_root = project_root / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    final_job_dir = media_root / f"animated-{job_id}"

    # Stage everything in a temporary directory inside media_root first
    stage_parent = Path(tempfile.mkdtemp(dir=media_root, prefix=".deluxe-apply-"))
    moved = False
    try:
        job_stage = stage_parent / f"animated-{job_id}"
        job_stage.mkdir()
        (job_stage / "preview").mkdir()
        (job_stage / "deluxe_preview").mkdir()
        (job_stage / "out").mkdir()
        (job_stage / "deluxe_frames").mkdir()
        (job_stage / "deluxe_dds").mkdir()

        # Encode lossless MP4 source so standard tools & fallback re-framing continue working seamlessly
        source_mp4 = job_stage / "source.mp4"
        _write_lossless_mp4(source_mp4, np.stack(loaded_frames), fps=1.0 / wait)

        for i in range(frames):
            shutil.copyfile(result_folder / f"{i:02d}.webp", job_stage / "preview" / f"{i:02d}.webp")
            shutil.copyfile(result_folder / f"{i:02d}.webp", job_stage / "deluxe_preview" / f"{i:02d}.webp")
            shutil.copyfile(png_src / f"{i:02d}.png", job_stage / "deluxe_frames" / f"{i:02d}.png")
            shutil.copyfile(dds_src / theme.frame_name(i), job_stage / "deluxe_dds" / theme.frame_name(i))
        shutil.copyfile(raf_src, job_stage / "out" / "background.raf.zip")

        orig_name = display_name or f"Ouroboros Deluxe ({report.get('strategyUsed', 'deluxe')}).mp4"
        job_meta = {
            "frames": frames,
            "duration": round(frames * wait, 6),
            "size": list(size),
            "source_size": list(size),
            "codec": "h264",
            "fps_used": round(1.0 / wait, 6),
            "preview_source": "deluxe",
            "deluxe": {
                "version": DELUXE_VERSION,
                "resultKey": report.get("key", ""),
                "frames": frames,
                "wait": wait,
                "size": list(size),
                "bytes": total_bytes,
                "ddsHashes": dds_hashes,
                "recipe": report.get("recipe"),
                "strategyUsed": report.get("strategyUsed"),
                "appliedAt": datetime.now(timezone.utc).isoformat(),
            },
        }
        job_obj = jobs.Job(
            id=job_id,
            kind="animated",
            root=job_stage,
            source=source_mp4,
            original_name=orig_name,
            meta=job_meta,
        )
        job_obj.save()

        # Atomic directory publish + transactional manifest update under revision_lock
        job_stage.rename(final_job_dir)
        moved = True

        from contextlib import nullcontext

        lock_ctx = projects.revision_lock(project_root) if acquire_lock else nullcontext()
        with lock_ctx:
            manifest = projects.read(project_root)
            base_state = manifest.state.model_copy(deep=True) if manifest.state is not None else _default_editor_state()
            new_anim = projects.Animation(
                jobId=job_id,
                frames=frames,
                wait=wait,
                size=size,
                backend="native",
            )
            new_state = base_state.model_copy(update={"bgMode": "animated", "anim": new_anim})
            # Verify all references resolve cleanly before writing manifest!
            projects.find_references(project_root, new_state)
            updated = manifest.model_copy(
                update={
                    "state": new_state,
                    "revision": manifest.revision + 1,
                    "updatedAt": datetime.now(timezone.utc),
                }
            )
            projects.atomic_write(project_root, updated)

        return {
            "applied": True,
            "projectId": updated.id,
            "revision": updated.revision,
            "jobId": job_id,
            "frames": frames,
            "wait": wait,
            "size": list(size),
            "ddsBytes": total_bytes,
            "ddsHashes": dds_hashes,
            "rafEquivalent": True,
        }
    except BaseException:
        if moved:
            shutil.rmtree(final_job_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)
