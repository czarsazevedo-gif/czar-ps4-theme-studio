"""Canonical RAF assembly and transactional project application for unified Ouroboros loops."""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from .. import jobs, mdx, media, projects, raf, theme
from ..config import ANIMATED_SIZES, PKG_DEFAULTS, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from . import OUROBOROS_VERSION
from . import media_io as io_media


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


def compute_variable_wait_schedule(
    frames: np.ndarray,
    mean_wait: float,
    *,
    max_deviation: float = 0.22,
) -> list[float]:
    """Allocate per-frame `<case wait="...">` proportional to visual motion arc-length.

    Preserves exact total cycle duration (`sum(waits) == N * mean_wait`) while giving larger
    inter-frame steps slightly more display time and smaller turnaround steps slightly less,
    equalizing perceived angular/linear velocity on screen within the fixed PS4 RAF frame budget.
    """
    n = int(frames.shape[0])
    w_mean = max(0.02, float(mean_wait))
    if n < 4:
        return [round(w_mean, 4)] * n
    # Fast strided step energy
    small = frames[:, ::4, ::4].astype(np.float32)
    step_e = np.array(
        [float(np.abs(small[(i + 1) % n] - small[i]).mean()) for i in range(n)],
        dtype=np.float64,
    )
    med_e = float(np.median(step_e))
    if med_e < 0.35 or float(np.std(step_e) / max(med_e, 1e-4)) < 0.04:
        return [round(w_mean, 4)] * n
    # Smooth 3-tap circular filter on step energy so cadence modulates organically
    sm_e = 0.20 * np.roll(step_e, 1) + 0.60 * step_e + 0.20 * np.roll(step_e, -1)
    rel = np.clip(sm_e / max(float(np.mean(sm_e)), 1e-4), 1.0 - max_deviation, 1.0 + max_deviation)
    # Damp modulation exponent (0.55) so cadence feels natural and never jittery
    weights = np.power(rel, 0.55)
    weights = weights / max(float(np.mean(weights)), 1e-6)
    waits = np.clip(weights * w_mean, 0.02, 5.0)
    # Enforce exact total duration == n * w_mean
    waits = waits * (n * w_mean / max(float(np.sum(waits)), 1e-6))
    return [round(float(x), 4) for x in waits]


def assemble_canonical_raf(
    dds_blobs: list[bytes],
    wait: float,
    dest_zip: Path,
    *,
    wait_list: list[float] | None = None,
) -> tuple[int, str]:
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
        index_xml = raf.build_scene(names, wait, wait_list=wait_list)
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
    file_hashes: dict[str, str] = report.get("files") or {}
    frame_png_hashes: dict[str, str] = report.get("framePngHashes") or {}
    recipe = report.get("recipe")

    if size not in ANIMATED_SIZES or not 4 <= frames <= max_frames_for(size):
        raise ValueError("Orçamento ou resolução incompatível com o projeto PS4.")
    if not math.isfinite(wait) or not (0.01 <= wait <= 10.0):
        raise ValueError("Intervalo temporal wait inválido para aplicação no projeto.")
    expected_bytes = frames * dds_bytes_for(*size)
    if expected_bytes > RAF_SIZE_LIMIT:
        raise ValueError("Sequência DDS excede o limite canônico de 6 MiB.")

    # Fail-closed parameter consistency against recipe & cryptographic renderKey when present
    if isinstance(recipe, dict):
        if "frames" in recipe and int(recipe["frames"]) != frames:
            raise ValueError("Inconsistência de integridade: frames diverge da receita registrada.")
        if "size" in recipe and tuple(recipe["size"]) != size:
            raise ValueError("Inconsistência de integridade: resolução diverge da receita registrada.")
        if "wait" in recipe and abs(float(recipe["wait"]) - wait) > 1e-4:
            raise ValueError("Inconsistência de integridade: wait diverge da receita registrada.")
        pid = report.get("profileId")
        src_sha = recipe.get("sourceSha256")
        strat_used = report.get("strategyUsed") or recipe.get("strategyUsed")
        src_interval = report.get("sourceInterval")
        if pid and src_sha and strat_used and report.get("renderKey"):
            st_v = src_interval[0] if isinstance(src_interval, (list, tuple)) and len(src_interval) == 2 else recipe.get("start")
            en_v = src_interval[1] if isinstance(src_interval, (list, tuple)) and len(src_interval) == 2 else recipe.get("end")
            if st_v is not None and en_v is not None:
                expected_rk = io_media.fingerprint(
                    [
                        OUROBOROS_VERSION,
                        src_sha,
                        str(pid),
                        list(size),
                        st_v,
                        en_v,
                        frames,
                        wait,
                        strat_used,
                        dds_hashes,
                    ]
                )
                if expected_rk != report["renderKey"]:
                    raise ValueError(
                        "Identidade criptográfica renderKey diverge dos parâmetros ou hashes DDS da proposta."
                    )

    # Fail-closed completeness check on ddsHashes and files manifests
    expected_dds_names = [theme.frame_name(i) for i in range(frames)]
    if not isinstance(dds_hashes, dict) or set(dds_hashes.keys()) != set(expected_dds_names):
        raise ValueError("Manifesto ddsHashes ausente ou incompleto — aplicação bloqueada (fail-closed).")
    if not isinstance(file_hashes, dict) or not file_hashes.get("background.raf.zip"):
        raise ValueError("Manifesto files sem hash de background.raf.zip — aplicação bloqueada (fail-closed).")
    for i in range(frames):
        if not file_hashes.get(f"{i:02d}.webp"):
            raise ValueError(f"Manifesto files sem hash de {i:02d}.webp — aplicação bloqueada (fail-closed).")

    dds_src = result_folder / "deluxe_dds"
    png_src = result_folder / "deluxe_frames"
    raf_src = result_folder / "background.raf.zip"
    if not (dds_src.is_dir() and png_src.is_dir() and raf_src.is_file()):
        raise ValueError("Artefatos renderizados incompletos para aplicação.")

    raf_bytes = raf_src.read_bytes()
    if sha256_bytes(raf_bytes) != file_hashes["background.raf.zip"]:
        raise ValueError("Integridade SHA-256 falhou para background.raf.zip.")
    for opt_extra in ("source.png", "diff.png"):
        if file_hashes.get(opt_extra):
            ep = result_folder / opt_extra
            if not ep.is_file() or sha256_bytes(ep.read_bytes()) != file_hashes[opt_extra]:
                raise ValueError(f"Integridade SHA-256 falhou para {opt_extra}.")

    # Verify internal contents of background.raf.zip (every DDS matches ddsHashes, valid plane.mdx & index.xml)
    try:
        with zipfile.ZipFile(raf_src, "r") as zf:
            znames = [n for n in zf.namelist() if not n.endswith("/")]
            for zn in znames:
                if zn.startswith("/") or ".." in Path(zn).parts:
                    raise ValueError(f"Caminho inseguro em background.raf.zip: {zn}")
            # theme.zip_raf_folder packs entries under `scene/background.raf/<name>`
            by_norm: dict[str, str] = {}
            for zn in znames:
                if zn.startswith("scene/background.raf/"):
                    norm = zn[len("scene/background.raf/"):]
                elif zn.startswith("background.raf/"):
                    norm = zn[len("background.raf/"):]
                else:
                    norm = zn
                if "/" in norm:
                    raise ValueError(f"Estrutura inesperada em background.raf.zip: {zn}")
                by_norm[norm] = zn
            zip_dds = {n for n in by_norm if n.endswith(".dds")}
            if zip_dds != set(expected_dds_names):
                raise ValueError("Lista de arquivos DDS dentro de background.raf.zip diverge do esperado.")
            for d_name in expected_dds_names:
                if sha256_bytes(zf.read(by_norm[d_name])) != dds_hashes[d_name]:
                    raise ValueError(f"DDS interno em background.raf.zip diverge de ddsHashes: {d_name}.")
            if raf.MODEL_REAL_FILE not in by_norm or "index.xml" not in by_norm:
                raise ValueError("background.raf.zip não contém plane.mdx e index.xml.")
            mdx.parse(zf.read(by_norm[raf.MODEL_REAL_FILE]))
            idx_text = zf.read(by_norm["index.xml"]).decode("utf-8")
            if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", idx_text, re.IGNORECASE):
                raise ValueError("index.xml dentro de background.raf.zip contém DOCTYPE/ENTITY proibido.")
            idx_root = ET.fromstring(idx_text)
            idx_cases = idx_root.findall(".//sequence/case")
            if len(idx_cases) != frames:
                raise ValueError(
                    f"index.xml dentro de background.raf.zip tem {len(idx_cases)} etapas (esperado {frames})."
                )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"background.raf.zip inválido ou corrompido: {exc}") from exc

    loaded_frames = []
    total_bytes = 0
    for i in range(frames):
        d_name = theme.frame_name(i)
        p_name = f"{i:02d}.png"
        w_name = f"{i:02d}.webp"
        d_path = dds_src / d_name
        p_path = png_src / p_name
        w_path = result_folder / w_name
        if not (d_path.is_file() and p_path.is_file() and w_path.is_file()):
            raise ValueError(f"Quadro {i:02d} ausente nos derivados renderizados.")
        blob = d_path.read_bytes()
        if sha256_bytes(blob) != dds_hashes[d_name]:
            raise ValueError(f"Integridade SHA-256 falhou para {d_name}.")
        if sha256_bytes(w_path.read_bytes()) != file_hashes[w_name]:
            raise ValueError(f"Integridade SHA-256 falhou para {w_name}.")
        if frame_png_hashes:
            if p_name not in frame_png_hashes or sha256_bytes(p_path.read_bytes()) != frame_png_hashes[p_name]:
                raise ValueError(f"Integridade SHA-256 falhou para deluxe_frames/{p_name}.")
        if blob[:4] != b"DDS " or blob[84:88] != b"DXT1":
            raise ValueError(f"Cabeçalho DDS inválido em {d_name}.")
        total_bytes += len(blob)
        with Image.open(p_path) as im:
            im.load()
            if im.size != size:
                raise ValueError(
                    f"Resolução de deluxe_frames/{p_name} ({im.size[0]}×{im.size[1]}) diverge de {size[0]}×{size[1]}."
                )
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
