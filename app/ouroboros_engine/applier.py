"""Canonical RAF assembly and transactional project application for unified Ouroboros loops."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from .. import jobs, mdx, media, projects, raf, theme
from ..config import ANIMATED_SIZES, PKG_DEFAULTS, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from . import OUROBOROS_VERSION


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_lossless_mp4(path: Path, frames: np.ndarray, fps: float) -> None:
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


def assemble_canonical_raf(dds_blobs: list[bytes], wait: float, dest_zip: Path) -> tuple[int, str]:
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
        (tree / raf.MODEL_REAL_FILE).write_bytes(
            mdx.quad_model(node_name=raf.MODEL_ID, half_w=half_w, half_h=half_h)
        )
        index_xml = raf.build_scene(names, wait)
        theme.write_index_xml(tree / "index.xml", index_xml)
        theme.zip_raf_folder(tree, dest_zip)
    return total_dds, index_xml


def _default_editor_state() -> projects.Editor:
    return projects.Editor(
        campos=projects.ThemeFields(
            **{
                "pkg-title": "Tema PS4 Ouroboros",
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


def apply_loop_to_project(
    project_root: Path,
    result_folder: Path,
    report: dict,
    *,
    display_name: str | None = None,
    acquire_lock: bool = True,
) -> dict:
    """Transactionally apply a completed Ouroboros render/proposal to the active project.

    Guarantees:
      - Verifies every DDS blob, SHA-256 hash, DXT1 header, and 6 MiB RAF budget before mutating.
      - Stages the new animated job completely before updating manifest.json under revision_lock.
      - Preserves compositor overlays, project metadata, and historical `deluxe_dds`/`deluxe_frames` layout.
      - Rolls back cleanly on any failure without corrupting the previous project revision.
    """
    frames = int(report["frames"])
    size = tuple(report["size"])
    wait = float(report["wait"])
    dds_hashes: dict[str, str] = report.get("ddsHashes") or {}

    if size not in ANIMATED_SIZES or not 4 <= frames <= max_frames_for(size):
        raise ValueError("Orçamento ou resolução incompatível com o projeto PS4.")
    expected_bytes = frames * dds_bytes_for(*size)
    if expected_bytes > RAF_SIZE_LIMIT:
        raise ValueError("Sequência DDS excede o limite canônico de 6 MiB.")

    dds_src = result_folder / "deluxe_dds"
    png_src = result_folder / "deluxe_frames"
    raf_src = result_folder / "background.raf.zip"
    if not (dds_src.is_dir() and png_src.is_dir() and raf_src.is_file()):
        raise ValueError("Artefatos renderizados incompletos para aplicação.")

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
        if dds_hashes.get(d_name) and sha256_bytes(blob) != dds_hashes[d_name]:
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

    stage_parent = Path(tempfile.mkdtemp(dir=media_root, prefix=".ouroboros-apply-"))
    moved = False
    try:
        job_stage = stage_parent / f"animated-{job_id}"
        job_stage.mkdir()
        (job_stage / "preview").mkdir()
        (job_stage / "deluxe_preview").mkdir()
        (job_stage / "out").mkdir()
        (job_stage / "deluxe_frames").mkdir()
        (job_stage / "deluxe_dds").mkdir()

        source_mp4 = job_stage / "source.mp4"
        write_lossless_mp4(source_mp4, np.stack(loaded_frames), fps=1.0 / wait)

        for i in range(frames):
            shutil.copyfile(result_folder / f"{i:02d}.webp", job_stage / "preview" / f"{i:02d}.webp")
            shutil.copyfile(result_folder / f"{i:02d}.webp", job_stage / "deluxe_preview" / f"{i:02d}.webp")
            shutil.copyfile(png_src / f"{i:02d}.png", job_stage / "deluxe_frames" / f"{i:02d}.png")
            shutil.copyfile(dds_src / theme.frame_name(i), job_stage / "deluxe_dds" / theme.frame_name(i))
        shutil.copyfile(raf_src, job_stage / "out" / "background.raf.zip")

        profile_label = report.get("profileTitle") or report.get("strategyUsed") or "ouroboros"
        orig_name = display_name or f"Ouroboros ({profile_label}).mp4"
        render_key = report.get("renderKey") or report.get("key") or ""
        job_meta = {
            "frames": frames,
            "duration": round(frames * wait, 6),
            "size": list(size),
            "source_size": list(size),
            "codec": "h264",
            "fps_used": round(1.0 / wait, 6),
            "preview_source": "deluxe",
            "deluxe": {
                "version": OUROBOROS_VERSION,
                "resultKey": render_key,
                "profileId": report.get("profileId"),
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

        job_stage.rename(final_job_dir)
        moved = True

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
            "profileId": report.get("profileId"),
            "renderKey": render_key,
            "frames": frames,
            "wait": wait,
            "waitMs": round(wait * 1000.0, 3),
            "size": list(size),
            "ddsBytes": total_bytes,
            "budgetPercent": round(total_bytes / float(RAF_SIZE_LIMIT) * 100.0, 2),
            "ddsHashes": dds_hashes,
            "rafEquivalent": True,
        }
    except BaseException:
        if moved:
            shutil.rmtree(final_job_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)
