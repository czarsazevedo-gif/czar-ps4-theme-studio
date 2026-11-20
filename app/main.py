"""API + interface server.

Generates the theme's visual assets and, when the Fake PKG Tools are present,
packs them into a .pkg. Packaging is optional: without the tools everything
else keeps working.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import math
import shutil
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, ConfigDict

from . import at9, compositor, dxt1, jobs, library, mdx, media, ouroboros, pkg, projects, raf, system_health, theme, theme_xml, vag
from .forge import audio_router as forge_audio
from .forge import bundle_service as forge_bundle
from .forge import pkg_forge, pkg_router as forge_pkg, sfo as forge_sfo
from .forge.audio_validation import measure_loudness, validate_at9
from .config import (
    ANIMATED_FRAME_BYTES,
    ANIMATED_SIZE,
    ANIMATED_SIZES,
    DEFAULT_WAIT_SECONDS,
    FUNCTION_ICONS,
    AUDIO_EXTS,
    AUDIO_SLOTS,
    MAX_SFX_SECONDS,
    SOUND_EXTS,
    IMAGE_SLOTS,
    ICON_SIZE,
    IMAGE_EXTS,
    MAX_FRAMES,
    MAX_UPLOAD_BYTES,
    PIC1_SIZE,
    PKG_CATEGORIES,
    PKG_DEFAULTS,
    PKG_FIXED_PATHS,
    PKG_MODES,
    RAF_SIZE_LIMIT,
    SLOT_GROUPS,
    SCE_ICON_SIZE,
    STATIC_DIR,
    STATIC_SIZE,
    THEME_PATHS,
    MAX_WAIT_SECONDS,
    MIN_WAIT_SECONDS,
    VIDEO_EXTS,
    dds_bytes_for,
    max_frames_for,
)
from .dds import DEFAULT_BACKEND, available_backends, encode_frame, encode_sequence
from .media import MediaError

app = FastAPI(title="PS4 Theme Asset Creator", docs_url=None, redoc_url=None)
app.middleware("http")(projects.scope_jobs)
app.include_router(projects.router)
app.include_router(library.router)
app.include_router(compositor.router)
app.include_router(ouroboros.router)
app.include_router(forge_audio.router)
app.include_router(forge_bundle.router)
app.include_router(forge_pkg.router)

#: The UI used to allow going over the cap "so the red bar could be seen".
#: That let you assemble a theme the console rejects with CE-38196-7, so it no
#: longer does: neither the interface nor the API accept an invalid combination.
#: theme.check_* remains as a last net, but should never fire now.


@app.exception_handler(MediaError)
async def _media_error_handler(_request, exc: MediaError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


async def _read_upload(file: UploadFile, allowed: set[str]) -> bytes:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(
            415,
            f"Unsupported extension: {suffix or '(no extension)'}. "
            f"Accepted: {', '.join(sorted(allowed))}",
        )
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(400, "The file arrived empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"The file exceeds {MAX_UPLOAD_BYTES // 1048576} MB.")
    return data


def _get_job(kind: str, job_id: str) -> jobs.Job:
    job = jobs.load(kind, job_id)
    if job is None:
        raise HTTPException(404, "That job no longer exists. Upload the file again.")
    jobs.touch(job)
    return job


def _parse_size(raw: str) -> tuple[int, int]:
    """"1280x720" -> (1280, 720). Only the ones on the list are accepted.

    It also takes the multiplication sign (U+00D7), because that is what the
    pretty label carries. The interface used to send exactly that, the split on
    ASCII "x" failed, and EVERYTHING fell back to 1280x720 without a word: the
    resolution selector did nothing for a long time. An invalid value falling
    back to the default is reasonable; doing it silently is not.
    """
    texto = str(raw).lower().replace("×", "x")
    try:
        w, h = (int(x) for x in texto.split("x"))
    except ValueError:
        return ANIMATED_SIZE
    return (w, h) if (w, h) in ANIMATED_SIZES else ANIMATED_SIZE


def _clamp_frames(count: int, size: tuple[int, int] = ANIMATED_SIZE) -> int:
    """Clamps to that resolution's real cap. Never lets you go over."""
    return max(2, min(max_frames_for(size), count))


#: Audio gain cap, in dB. Going far down is harmless; going far up only adds
#: limiting and distortion, so the headroom upwards is short.
MIN_GAIN_DB = -24.0
MAX_GAIN_DB = 12.0


def _clamp_gain(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(MIN_GAIN_DB, min(MAX_GAIN_DB, v)), 2)


def _budget(frame_count: int, size: tuple[int, int] = ANIMATED_SIZE) -> dict:
    frame_bytes = dds_bytes_for(*size)
    total = frame_count * frame_bytes
    return {
        "frames": frame_count,
        "size": list(size),
        "frame_bytes": frame_bytes,
        "total_bytes": total,
        "limit_bytes": RAF_SIZE_LIMIT,
        "percent": round(total / RAF_SIZE_LIMIT * 100, 2),
        "over_limit": total > RAF_SIZE_LIMIT,
        "max_frames": max_frames_for(size),
    }


# --- System info & health -------------------------------------------------

_RAF_EXPORT_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="czar-raf-export")
_RAF_JOB_LOCKS_GUARD = threading.Lock()
_RAF_JOB_LOCKS: dict[str, threading.Lock] = {}


def _job_lock(key: str) -> threading.Lock:
    with _RAF_JOB_LOCKS_GUARD:
        lock = _RAF_JOB_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _RAF_JOB_LOCKS[key] = lock
        return lock


@app.get("/healthz")
def healthz() -> dict:
    """Lightweight operational readiness probe (independent of optional Forge tools)."""
    return {
        "ok": True,
        "status": "ok",
        "editor": True,
        "ffmpeg": bool(media.FFMPEG),
        "ffprobe": bool(media.FFPROBE),
        "atrac9_encoder": at9.available(),
        "pkg_forge": pkg.tools_available() or pkg_forge.pkg_forge_info()["available"],
    }


@app.get("/api/system")
def system_info(refresh: bool = False) -> dict:
    health = system_health.get_system_health(force=refresh)
    tools = health["tools"]
    return {
        "ffmpeg": tools["ffmpeg"]["state"] == "ok",
        "ffprobe": bool(media.FFPROBE),
        "magick": tools["magick"]["state"] == "ok",
        "pkg_tools": tools["pkg"]["state"] == "ok",
        "pkg_forge": pkg_forge.pkg_forge_info(),
        "at9tool": tools["at9"]["state"] == "ok",
        "at9_encoder": at9.encoder_info(),
        "health": health,
        "tools_setup": system_health.get_tool_setup_status(),
        "min_gain_db": MIN_GAIN_DB,
        "max_gain_db": MAX_GAIN_DB,
        "backends": available_backends(),
        "default_backend": DEFAULT_BACKEND,
        "default_wait_seconds": DEFAULT_WAIT_SECONDS,
        "min_wait_seconds": MIN_WAIT_SECONDS,
        "max_wait_seconds": MAX_WAIT_SECONDS,
        "max_frames": MAX_FRAMES,
        "animated_sizes": [
            {"size": list(sz), "label": f"{sz[0]}×{sz[1]}",
             "frame_bytes": dds_bytes_for(*sz), "max_frames": max_frames_for(sz)}
            for sz in ANIMATED_SIZES
        ],
        "limit_bytes": RAF_SIZE_LIMIT,
        "animated_frame_bytes": ANIMATED_FRAME_BYTES,
        "sizes": {
            "static": list(STATIC_SIZE),
            "animated": list(ANIMATED_SIZE),
            "icon": list(ICON_SIZE),
        },
        "accepted": {
            "video": sorted(VIDEO_EXTS),
            "image": sorted(IMAGE_EXTS),
        },
    }


@app.get("/api/system/health")
def api_system_health(refresh: bool = False) -> dict:
    """Real-time 3-state (ok/off/err) tool health snapshot for live UI traffic lights."""
    health = system_health.get_system_health(force=refresh)
    return {
        **health,
        "setup": system_health.get_tool_setup_status(),
    }


@app.post("/api/system/tools/setup")
async def api_system_tools_setup(request: Request) -> dict:
    """Trigger one-tap background activation/repair of allowlisted tools (all|pkg|at9|magick)."""
    target = "all"
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            body = await request.json()
            if isinstance(body, dict) and body.get("target"):
                target = str(body["target"])
        except Exception:
            pass
    else:
        q_target = request.query_params.get("target")
        if q_target:
            target = q_target
    try:
        res = system_health.start_tool_setup(target)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {
        **res,
        "health": system_health.get_system_health(force=False),
    }


@app.get("/api/system/tools/setup/status")
def api_system_tools_setup_status(refresh: bool = False) -> dict:
    setup = system_health.get_tool_setup_status()
    health = system_health.get_system_health(force=refresh)
    return {
        "setup": setup,
        "health": health,
    }


# --- Animated background --------------------------------------------------

def _restore_deluxe_previews(job: jobs.Job, frame_count: int, size: tuple[int, int]) -> list[Path] | None:
    """Restore exact lossless WebP previews of the Deluxe decoded DDS frames (CZAR-AUD-002).

    Prevents stale extracted frames from another resolution (e.g. 360p -> 540p -> 360p)
    from masquerading as the immutable Deluxe preview.
    """
    from PIL import Image

    deluxe_prev_dir = job.root / "deluxe_preview"
    deluxe_dds_dir = job.root / "deluxe_dds"

    has_immutable_prev = (
        deluxe_prev_dir.is_dir()
        and all((deluxe_prev_dir / f"{i:02d}.webp").is_file() for i in range(frame_count))
    )
    has_dds = (
        deluxe_dds_dir.is_dir()
        and all((deluxe_dds_dir / theme.frame_name(i)).is_file() for i in range(frame_count))
    )
    if not has_immutable_prev and not has_dds:
        # Legacy fallback: only accept existing preview_dir files if every frame has exact dimensions AND preview_source=="deluxe"
        if job.meta.get("preview_source") != "deluxe":
            return None
        existing = [job.preview_dir / f"{i:02d}.webp" for i in range(frame_count)]
        if not all(p.is_file() for p in existing):
            return None
        for p in existing:
            with Image.open(p) as im:
                if im.size != tuple(size):
                    return None
        return existing

    job.preview_dir.mkdir(parents=True, exist_ok=True)
    for stale in job.preview_dir.glob("*.webp"):
        stale.unlink(missing_ok=True)

    restored: list[Path] = []
    if has_immutable_prev:
        dims_ok = True
        for i in range(frame_count):
            with Image.open(deluxe_prev_dir / f"{i:02d}.webp") as im:
                if im.size != tuple(size):
                    dims_ok = False
                    break
        if dims_ok:
            for i in range(frame_count):
                dst = job.preview_dir / f"{i:02d}.webp"
                shutil.copyfile(deluxe_prev_dir / f"{i:02d}.webp", dst)
                restored.append(dst)
            return restored

    if has_dds:
        deluxe_prev_dir.mkdir(exist_ok=True)
        for i in range(frame_count):
            rgba = dxt1.decode_dds((deluxe_dds_dir / theme.frame_name(i)).read_bytes())
            im = Image.fromarray(rgba[..., :3])
            dst = job.preview_dir / f"{i:02d}.webp"
            im.save(dst, "WEBP", lossless=True, method=0)
            shutil.copyfile(dst, deluxe_prev_dir / f"{i:02d}.webp")
            restored.append(dst)
        return restored

    return None


def _build_animated_preview(job: jobs.Job, frame_count: int,
                            size: tuple[int, int] = ANIMATED_SIZE) -> dict:
    info = media.probe(job.source)
    deluxe = job.meta.get("deluxe")
    deluxe_match = (
        isinstance(deluxe, dict)
        and deluxe.get("frames") == frame_count
        and list(deluxe.get("size") or []) == list(size)
    )
    restored = _restore_deluxe_previews(job, frame_count, size) if deluxe_match else None
    reuse_deluxe = restored is not None
    if reuse_deluxe:
        frames = restored
        job.meta["preview_source"] = "deluxe"
    else:
        frames = media.extract_preview_frames(
            job.source, job.preview_dir, frame_count, size, info.duration
        )
        job.meta["preview_source"] = "extracted"
    job.meta["size"] = list(size)

    job.meta.update(
        {
            "frames": len(frames),
            "duration": info.duration,
            "source_size": [info.width, info.height],
            "codec": info.codec,
            "fps_used": (1.0 / float(deluxe["wait"])) if reuse_deluxe else media.extraction_fps(frame_count, info.duration),
        }
    )
    job.save()

    # The querystring keeps the browser from reusing frames from a past run.
    stamp = int(time.time() * 1000)
    return {
        "job_id": job.id,
        "frames": [
            f"/api/animated/{job.id}/preview/{p.name}?v={stamp}" for p in frames
        ],
        "source": {
            "name": job.original_name,
            "duration": round(info.duration, 3),
            "width": info.width,
            "height": info.height,
            "codec": info.codec,
            "frame_count": info.frame_count,
        },
        "fps_used": round(job.meta["fps_used"], 4),
        "budget": _budget(len(frames), size),
    }


@app.post("/api/animated/upload")
async def animated_upload(
    file: UploadFile = File(...),
    frames: int = Form(12),
    size: str = Form(""),
) -> dict:
    data = await _read_upload(file, VIDEO_EXTS)
    job = jobs.create("animated", file.filename or "source", data)
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    try:
        return await loop.run_in_executor(_RAF_EXPORT_POOL, ctx.run, _prepare_animated_upload, job, frames, size)
    except BaseException:
        shutil.rmtree(job.root, ignore_errors=True)
        raise


def _prepare_animated_upload(job: jobs.Job, frames: int, size: str) -> dict:
    sz = _parse_size(size)
    return _build_animated_preview(job, _clamp_frames(frames, sz), sz)


@app.post("/api/animated/{job_id}/frames")
async def animated_reframe(job_id: str, frames: int = Form(...),
                           size: str = Form("")) -> dict:
    """Regenerates the preview with a different frame count or resolution."""
    job = _get_job("animated", job_id)
    sz = _parse_size(size or "x".join(str(v) for v in (job.meta.get("size") or ANIMATED_SIZE)))
    clamped = _clamp_frames(frames, sz)
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()

    def _run():
        with _job_lock(str(job.root)):
            return _build_animated_preview(job, clamped, sz)

    return await loop.run_in_executor(_RAF_EXPORT_POOL, ctx.run, _run)


@app.get("/api/animated/{job_id}/preview/{name}")
def animated_preview_frame(job_id: str, name: str) -> FileResponse:
    job = _get_job("animated", job_id)
    if Path(name).name != name or not name.endswith(".webp"):
        raise HTTPException(404, "Frame not found.")
    path = job.preview_dir / name
    if not path.is_file():
        raise HTTPException(404, "Quadro não encontrado.")
    return FileResponse(path, media_type="image/webp")


@app.post("/api/animated/{job_id}/export")
async def animated_export(
    job_id: str,
    frames: int = Form(...),
    wait_seconds: float = Form(DEFAULT_WAIT_SECONDS),
    backend: str = Form(DEFAULT_BACKEND),
    composition: str | None = Form(None),
    animation_size: str = Form(""),
) -> dict:
    """Converts to .dds, writes the full RAF scene and assembles the .zip without blocking the event loop."""
    job = _get_job("animated", job_id)
    state = compositor.load_composition(composition)
    has_overlay = bool(state and state.surface("animated"))
    size = _composed_animation_size(job, animation_size if has_overlay else "")
    frame_count = _clamp_frames(frames, size)
    wait = max(MIN_WAIT_SECONDS, min(MAX_WAIT_SECONDS, wait_seconds))

    try:
        theme.check_frame_count(frame_count)
        theme.check_budget(frame_count * dds_bytes_for(*size))
    except ValueError as exc:
        raise HTTPException(413, str(exc)) from exc
    if has_overlay and frame_count != frames:
        raise HTTPException(422, "A composição ultrapassa o orçamento RAF da resolução selecionada.")
    if has_overlay and not math.isfinite(wait_seconds):
        raise HTTPException(422, "A temporização da composição é inválida.")

    started = time.time()
    zip_path = job.out_dir / "background.raf.zip"

    def _run_export() -> tuple[int, int, str, int]:
        with _job_lock(str(job.root)):
            overlay = _render_animation_overlay(state, size)
            job.out_dir.mkdir(parents=True, exist_ok=True)
            # Always stage into a temporary directory and atomically replace background.raf.zip
            # so any failure/interruption preserves the last valid ZIP intact (CZAR-AUD-006).
            with tempfile.TemporaryDirectory(dir=job.out_dir, prefix=".raf-stage-") as staging:
                work = Path(staging)
                dds_total, written_count, xml_text, mdx_sz = _write_raf_export(
                    job, work, frame_count, size, wait, backend, overlay
                )
                (work / zip_path.name).replace(zip_path)
                return dds_total, written_count, xml_text, mdx_sz

    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    try:
        dds_bytes, written, index_xml, mdx_bytes = await loop.run_in_executor(
            _RAF_EXPORT_POOL, ctx.run, _run_export
        )
    except OSError as exc:
        if has_overlay:
            raise HTTPException(503, "Não foi possível gravar o RAF composto. Confira o espaço em disco.") from exc
        raise HTTPException(503, "Não foi possível gravar o arquivo RAF. Confira o espaço em disco.") from exc

    return {
        "job_id": job.id,
        "download": f"/api/animated/{job.id}/download",
        "frames": written,
        "wait": wait,
        "backend": backend,
        "size": list(size),
        "dds_bytes": dds_bytes,
        "mdx_bytes": mdx_bytes,
        "scene_bytes": dds_bytes + mdx_bytes + len(index_xml.encode("utf-8")),
        "zip_bytes": zip_path.stat().st_size,
        "elapsed": round(time.time() - started, 2),
        "index_xml": index_xml,
        "budget": _budget(written, size),
    }


def _write_raf_export(job, destination, frame_count, size, wait, backend, overlay=None):
    raf_dir = destination / "background.raf"
    if raf_dir.exists():
        for stale in raf_dir.iterdir():
            stale.unlink()
    raf_dir.mkdir(parents=True, exist_ok=True)

    dds_bytes = 0
    written = 0
    names: list[str] = []

    deluxe = job.meta.get("deluxe")
    deluxe_match = (
        isinstance(deluxe, dict)
        and deluxe.get("frames") == frame_count
        and list(deluxe.get("size") or []) == list(size)
    )
    dds_dir = job.root / "deluxe_dds"
    png_dir = job.root / "deluxe_frames"

    if (
        deluxe_match
        and overlay is None
        and backend == DEFAULT_BACKEND
        and dds_dir.is_dir()
        and all((dds_dir / theme.frame_name(i)).is_file() for i in range(frame_count))
    ):
        for index in range(frame_count):
            name = theme.frame_name(index)
            blob = (dds_dir / name).read_bytes()
            (raf_dir / name).write_bytes(blob)
            names.append(name)
            dds_bytes += len(blob)
            written += 1
    elif (
        deluxe_match
        and png_dir.is_dir()
        and all((png_dir / f"{i:02d}.png").is_file() for i in range(frame_count))
    ):
        from PIL import Image
        import numpy as np

        def _deluxe_rgba():
            for index in range(frame_count):
                with Image.open(png_dir / f"{index:02d}.png") as im:
                    yield np.asarray(im.convert("RGBA"))

        for index, blob in encode_sequence(compositor.compose_frames(_deluxe_rgba(), overlay), backend):
            name = theme.frame_name(index)
            (raf_dir / name).write_bytes(blob)
            names.append(name)
            dds_bytes += len(blob)
            written += 1
    else:
        info = media.probe(job.source)
        frames_in = media.iter_raw_frames(job.source, frame_count, size, info.duration)
        for index, blob in encode_sequence(compositor.compose_frames(frames_in, overlay), backend):
            name = theme.frame_name(index)
            (raf_dir / name).write_bytes(blob)
            names.append(name)
            dds_bytes += len(blob)
            written += 1

    if written != frame_count:
        raise HTTPException(500, f"Expected {frame_count} frames but wrote {written}.")

    # The plane shared by every frame, and the scene that cross-fades them.
    half_w, half_h = raf.plane_half_size()
    plane = mdx.quad_model(node_name=raf.MODEL_ID, half_w=half_w, half_h=half_h)
    (raf_dir / raf.MODEL_REAL_FILE).write_bytes(plane)
    mdx_bytes = len(plane)

    index_xml = raf.build_scene(names, wait)
    theme.write_index_xml(raf_dir / "index.xml", index_xml)

    theme.zip_raf_folder(raf_dir, destination / "background.raf.zip")
    return dds_bytes, written, index_xml, mdx_bytes


@app.get("/api/animated/{job_id}/download")
def animated_download(job_id: str) -> FileResponse:
    job = _get_job("animated", job_id)
    zip_path = job.out_dir / "background.raf.zip"
    if not zip_path.is_file():
        raise HTTPException(404, "This background has not been exported yet.")
    return FileResponse(zip_path, media_type="application/zip", filename="background.raf.zip")


# --- Image slots ----------------------------------------------------------
# Every customisable slot (backgrounds and icons) is uploaded through here. The
# slot name contains ':' so it cannot be a folder name: jobs are all stored
# with kind "slot" and the slot itself goes in their meta.

@app.get("/api/slots")
def slot_catalog() -> dict:
    """Which slots exist, where they go and which one they copy when empty."""
    return {
        "groups": SLOT_GROUPS,
        "slots": [
            {
                "id": slot_id,
                "label": spec["label"],
                # The icon's file name: the label is translated but the file
                # is fixed by the console, and it helps to see which is which.
                "file": spec.get("file"),
                "path": spec["path"],
                "size": list(spec["size"]),
                "group": spec["group"],
                "required": spec.get("required", False),
                "fallback": spec.get("fallback"),
                "glow": list(spec["glow_size"]) if "glow_size" in spec else None,
                # With an animated background the scene covers this slot: the
                # interface says so instead of letting you find out on the
                # console. If it also carries raf_label, the slot still serves
                # another purpose and merely gets renamed.
                "hidden_by_raf": spec.get("hidden_by_raf", False),
                "raf_label": spec.get("raf_label"),
            }
            for slot_id, spec in IMAGE_SLOTS.items()
        ],
    }


@app.post("/api/slot/{slot_id}/upload")
async def slot_upload(slot_id: str, file: UploadFile = File(...)) -> dict:
    spec = IMAGE_SLOTS.get(slot_id)
    if spec is None:
        raise HTTPException(404, f"Unknown slot: {slot_id}")

    data = await _read_upload(file, IMAGE_EXTS)
    job = jobs.create("slot", file.filename or "source", data)
    try:
        return _prepare_slot_upload(job, slot_id, spec)
    except BaseException:
        shutil.rmtree(job.root, ignore_errors=True)
        raise


def _prepare_slot_upload(job: jobs.Job, slot_id: str, spec: dict) -> dict:
    job.meta["slot"] = slot_id
    job.save()

    info = media.probe(job.source)
    preview = media.render_image(job.source, job.preview_dir / "preview.webp", spec["size"])

    return {
        "slot": slot_id,
        "job_id": job.id,
        "preview": f"/api/slot/{job.id}/preview?v={int(time.time() * 1000)}",
        "bytes": preview.stat().st_size,
        "target": list(spec["size"]),
        "path": spec["path"],
        "source": {"name": job.original_name, "width": info.width, "height": info.height},
    }


@app.get("/api/slot/{job_id}/preview")
def slot_preview(job_id: str) -> FileResponse:
    job = _get_job("slot", job_id)
    path = job.preview_dir / "preview.webp"
    if not path.is_file():
        raise HTTPException(404, "Preview not found.")
    return FileResponse(path, media_type="image/webp")


@app.get("/api/audio/slots")
def audio_catalog() -> dict:
    return {
        "slots": [
            {
                "id": sid, "label": spec["label"], "path": spec["path"],
                "convert": spec["convert"], "rate": spec["rate"],
                "exts": sorted(spec["exts"] or SOUND_EXTS),
            }
            for sid, spec in AUDIO_SLOTS.items()
        ],
        "max_sfx_seconds": MAX_SFX_SECONDS,
        "at9tool": at9.available(),
        "at9_encoder": at9.encoder_info(),
    }


@app.post("/api/audio/{slot_id}/upload")
async def audio_upload(slot_id: str, file: UploadFile = File(...),
                       gain_db: float = Form(0.0)) -> dict:
    """Uploads menu music or sound effects.

    With a native ATRAC9 encoder (psgfx-at9) or at9tool.exe present, any
    supported audio is converted to PS4 ATRAC9 (48 kHz, stereo, 144 kbps, loop);
    without an encoder, uploaded .at9 files are strictly validated.
    """
    spec = AUDIO_SLOTS.get(slot_id)
    if spec is None:
        raise HTTPException(404, f"Unknown audio slot: {slot_id}")
    filename = file.filename or ""
    data = await _read_upload(file, _audio_upload_extensions(filename, spec))
    job = jobs.create("audio", filename or "source", data)
    try:
        return _prepare_audio_upload(job, slot_id, spec, gain_db)
    except BaseException:
        shutil.rmtree(job.root, ignore_errors=True)
        raise


def _audio_upload_extensions(filename: str, spec: dict) -> set[str]:
    # With an ATRAC9 encoder around, the music slot takes ordinary audio too.
    allowed = spec["exts"] or SOUND_EXTS
    if not spec["convert"]:
        if at9.available():
            allowed = SOUND_EXTS
        elif (
            (suffix := Path(filename).suffix.lower()) in SOUND_EXTS
            and suffix not in allowed
        ):
            # Valid audio, we just cannot convert it: better to say so.
            raise HTTPException(
                415,
                "O PS4 exige ATRAC9 (.at9) para a música do menu. Instale o codificador "
                "nativo (./scripts/install_forge_tools.sh at9) ou envie um .at9 válido.",
            )
    return allowed


def _prepare_audio_upload(job: jobs.Job, slot_id: str, spec: dict, gain_db: float) -> dict:
    job.meta["slot"] = slot_id

    return _procesar_audio(job, spec, _clamp_gain(gain_db))


class LibraryUse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: str
    frames: int = 12
    size: str = ""


@app.post("/api/library/{ident}/apply")
def apply_library_asset(request: Request, ident: str, selection: LibraryUse) -> dict:
    """Reuse the canonical preparation steps with an authorized, linked source."""
    if jobs.project_workspace.get() is None:
        raise HTTPException(400, "Abra um projeto para usar a mídia.")
    asset, source = library.get_asset(request, ident)
    slot = selection.slot
    if slot == "animated":
        kind, allowed = "animated", VIDEO_EXTS if asset.kind in {"video", "animation"} else set()
    elif slot in IMAGE_SLOTS:
        kind, allowed = "slot", IMAGE_EXTS if asset.kind in {"image", "animation"} else set()
    elif slot in AUDIO_SLOTS:
        kind = "audio"
        spec = AUDIO_SLOTS[slot]
        if slot == "bgm" and asset.kind == "audio" and asset.codec != "atrac9" and not at9.available():
            raise HTTPException(415, "A música do menu precisa de ATRAC9 (.at9) neste servidor.")
        allowed = (SOUND_EXTS if at9.available() and slot == "bgm" else spec["exts"] or SOUND_EXTS
                   ) if asset.kind == "audio" else set()
    else:
        raise HTTPException(404, "Destino inválido para a mídia.")
    if asset.extension not in allowed:
        raise HTTPException(415, "O formato da mídia não é compatível com este destino.")
    try:
        with jobs.from_library(kind, asset.originalName, source) as job:
            if kind == "animated":
                return _prepare_animated_upload(job, selection.frames, selection.size)
            if kind == "slot":
                return _prepare_slot_upload(job, slot, IMAGE_SLOTS[slot])
            job.meta["library_id"] = asset.id
            return _prepare_audio_upload(job, slot, AUDIO_SLOTS[slot], 0.0)
    except (MediaError, HTTPException) as exc:
        raise HTTPException(422, "O processador não conseguiu preparar esta mídia para o destino.") from exc
    except OSError as exc:
        raise library.storage_error() from exc


def _sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _procesar_audio(job: jobs.Job, spec: dict, gain_db: float = 0.0) -> dict:
    """Encodes a slot's audio into the format the console wants.

    It lives apart from the upload so the gain can be re-applied without
    uploading again: the original stays in job.source and is re-encoded from
    there every time. Applying the gain on top of the previous result would
    stack losses from a lossy codec.
    """
    slot_id = str(job.meta.get("slot") or "bgm")
    info = media.probe_audio(job.source)
    job.meta["gain_db"] = gain_db
    job.meta["source_codec"] = info.codec
    job.meta["original_sha256"] = _sha256_path(job.source)
    ganancia = {"gain_db": gain_db}
    stamp = int(time.time() * 1000)

    if spec["convert"]:
        # Effects can be generated here: PS-ADPCM is documented.
        pcm = media.pcm_mono(job.source, spec["rate"], MAX_SFX_SECONDS, gain_db)
        blob = vag.encode(pcm, spec["rate"], slot_id)
        out = job.out_dir / "sound.vag"
        tmp = out.with_suffix(".vag.tmp")
        tmp.write_bytes(blob)
        tmp.replace(out)
        conv_sha = hashlib.sha256(blob).hexdigest()
        job.meta.update(
            {
                "converted": out.name,
                "converted_sha256": conv_sha,
                "derived_id": conv_sha[:16],
                "codec": "vag (PS-ADPCM)",
                "rate": spec["rate"],
                "channels": 1,
                "duration": round(min(info.duration, MAX_SFX_SECONDS), 2),
            }
        )
        job.save()
        forge_audio.ensure_audio_job_previews(job)
        return {
            "slot": slot_id,
            "job_id": job.id,
            "path": spec["path"],
            "name": job.original_name,
            "codec": "vag (PS-ADPCM)",
            "rate": spec["rate"],
            "channels": 1,
            "duration": round(min(info.duration, MAX_SFX_SECONDS), 2),
            "bytes": len(blob),
            "source_codec": info.codec,
            "loudness": None,
            "slot_label": spec["label"],
            "derived_id": conv_sha[:16],
            "original_sha256": job.meta["original_sha256"],
            "converted_sha256": conv_sha,
            "preview": f"/api/audio/{job.id}/preview?v={stamp}",
            **ganancia,
        }

    # Music. Re-encoding is needed when the format is wrong OR a gain is asked
    # for: an already encoded .at9 cannot be turned up without redoing it.
    hay_que_convertir = info.codec != spec["codec"] or abs(gain_db) > 0.01
    if hay_que_convertir:
        if not at9.available():
            detalle = (
                "Alterar o volume exige recodificar o .at9; instale o codificador ATRAC9 "
                "(./scripts/install_forge_tools.sh at9)."
                if info.codec == spec["codec"] else
                f"O arquivo é {info.codec}, não {spec['codec']}. O console só aceita "
                "ATRAC9 em sound/bgm_home.at9. Instale o codificador ATRAC9 ou envie um .at9 válido."
            )
            raise HTTPException(422, detalle)

        wav = job.out_dir / "source.wav"
        try:
            media.to_wav(job.source, wav, at9.SAMPLE_RATE, at9.MAX_SECONDS, gain_db)
            encoded = at9.encode(wav, job.out_dir / "bgm.at9")
        finally:
            wav.unlink(missing_ok=True)
        val = validate_at9(encoded, require_ps4_profile=True, require_loop=True)
        if not val.valid:
            raise HTTPException(422, "Validação do ATRAC9 gerado falhou: " + "; ".join(val.errors))
        loud_rep = measure_loudness(encoded)
        loud = pkg.calc_loudness(encoded) if pkg.tools_available() else loud_rep.integrated_lufs
        post_at9_loop = forge_audio.evaluate_post_at9_loop(encoded)
        conv_sha = _sha256_path(encoded)
        enc_info = at9.encoder_info()
        job.meta.update(
            {
                "converted": encoded.name,
                "converted_sha256": conv_sha,
                "derived_id": conv_sha[:16],
                "codec": val.codec,
                "rate": val.sample_rate,
                "channels": val.channels,
                "bitrate_kbps": at9.DEFAULT_BITRATE,
                "duration": round(val.duration_seconds, 2),
                "loudness": loud,
                "loudness_report": loud_rep.model_dump(),
                "post_at9_loop": post_at9_loop,
                "volume_mode": "manual" if abs(gain_db) > 0.01 else "original",
                "loop_start_sample": val.loop_start_sample,
                "loop_end_sample": val.loop_end_sample,
                "play_count": val.play_count,
                "validation": val.model_dump(),
                "encoder_backend": enc_info["backend"],
            }
        )
        job.save()
        forge_audio.ensure_audio_job_previews(job)
        return {
            "slot": slot_id, "job_id": job.id, "path": spec["path"],
            "name": job.original_name, "codec": val.codec,
            "rate": val.sample_rate, "channels": val.channels,
            "duration": round(val.duration_seconds, 1),
            "bytes": encoded.stat().st_size,
            "source_codec": info.codec, "loudness": loud,
            "loudness_report": loud_rep.model_dump(),
            "post_at9_loop": post_at9_loop,
            "validation": val.model_dump(),
            "derived_id": conv_sha[:16],
            "original_sha256": job.meta["original_sha256"],
            "converted_sha256": conv_sha,
            "encoder_backend": enc_info["backend"],
            "preview": f"/api/audio/{job.id}/preview?v={stamp}",
            "seam_preview": f"/api/audio/{job.id}/seam-preview?v={stamp}",
            "slot_label": spec["label"], **ganancia,
        }

    if info.rate != spec["rate"]:
        raise HTTPException(
            422, f"It must be {spec['rate']} Hz; this one is {info.rate} Hz."
        )

    val = validate_at9(job.source, require_ps4_profile=True, require_loop=False)
    if not val.valid:
        raise HTTPException(
            422,
            "O arquivo .at9 enviado está inválido ou fora da especificação PS4: "
            + "; ".join(val.errors),
        )

    job.meta.pop("converted", None)
    loud_rep = measure_loudness(job.source)
    loudness = pkg.calc_loudness(job.source) if pkg.tools_available() else loud_rep.integrated_lufs
    src_sha = job.meta["original_sha256"]
    job.meta.update(
        {
            "converted_sha256": src_sha,
            "derived_id": src_sha[:16],
            "codec": val.codec,
            "rate": val.sample_rate,
            "channels": val.channels,
            "bitrate_kbps": at9.DEFAULT_BITRATE,
            "duration": round(val.duration_seconds, 2),
            "loudness": loudness,
            "loudness_report": loud_rep.model_dump(),
            "volume_mode": "original",
            "loop_start_sample": val.loop_start_sample,
            "loop_end_sample": val.loop_end_sample,
            "play_count": val.play_count,
            "validation": val.model_dump(),
            "encoder_backend": "passthrough-at9",
        }
    )
    job.save()
    forge_audio.ensure_audio_job_previews(job)
    return {
        "slot": slot_id,
        "job_id": job.id,
        "path": spec["path"],
        "name": job.original_name,
        "codec": info.codec,
        "rate": info.rate,
        "channels": info.channels,
        "duration": round(info.duration, 1),
        "bytes": job.source.stat().st_size,
        "source_codec": info.codec,
        "loudness": loudness,
        "loudness_report": loud_rep.model_dump(),
        "validation": val.model_dump(),
        "derived_id": src_sha[:16],
        "original_sha256": src_sha,
        "converted_sha256": src_sha,
        "encoder_backend": "passthrough-at9",
        "preview": f"/api/audio/{job.id}/preview?v={stamp}",
        "seam_preview": f"/api/audio/{job.id}/seam-preview?v={stamp}",
        "slot_label": spec["label"],
        **ganancia,
    }


def _reapply_forge_bgm_with_gain(job: jobs.Job, spec: dict, gain_db: float) -> dict:
    """Re-encode a Forge-processed BGM with updated manual gain while preserving loop finishing."""
    if not at9.available():
        raise HTTPException(
            422,
            "Alterar o volume exige recodificar o .at9; instale o codificador ATRAC9 "
            "(./scripts/install_forge_tools.sh at9).",
        )
    cp = dict(job.meta.get("conversion_params") or {})
    pcm_raw = forge_audio.decode_to_float64_stereo(
        job.source, sample_rate=at9.SAMPLE_RATE, max_seconds=at9.MAX_SECONDS
    )
    looped, loop_report = forge_audio.finish_audio_loop(
        pcm_raw,
        sr=at9.SAMPLE_RATE,
        mode=cp.get("loop_mode", "whole"),
        start_sec=cp.get("start_sec"),
        end_sec=cp.get("end_sec"),
        start_sample=cp.get("start_sample"),
        end_sample=cp.get("end_sample"),
        trim_silence=bool(cp.get("trim_silence", False)),
        crossfade_ms=float(cp.get("crossfade_ms", 0.0)),
        allow_fade=bool(cp.get("allow_fade", False)),
        fade_in_ms=float(cp.get("fade_in_ms", 0.0)),
        fade_out_ms=float(cp.get("fade_out_ms", 0.0)),
    )
    leveled, vol_params, _before_loud, _after_loud = forge_audio.apply_volume_mode(
        looped, mode="manual", gain_db=gain_db, sr=at9.SAMPLE_RATE
    )
    loop_enabled = bool(cp.get("loop_enabled", True))
    target_at9 = job.out_dir / "bgm.at9"
    at9.encode_pcm(
        leveled,
        target_at9,
        bitrate=at9.DEFAULT_BITRATE,
        loop=loop_enabled,
        validate=True,
    )
    val = validate_at9(target_at9, require_ps4_profile=True, require_loop=loop_enabled)
    if not val.valid:
        raise HTTPException(422, "Validação do ATRAC9 gerado falhou: " + "; ".join(val.errors))
    loud_rep = measure_loudness(target_at9)
    loud = pkg.calc_loudness(target_at9) if pkg.tools_available() else loud_rep.integrated_lufs
    post_at9_loop = forge_audio.evaluate_post_at9_loop(target_at9)
    conv_sha = _sha256_path(target_at9)
    enc_info = at9.encoder_info()
    cp.update({"volume_mode": "manual", "gain_db": gain_db, "target_lufs": None})
    job.meta.update(
        {
            "converted": target_at9.name,
            "converted_sha256": conv_sha,
            "derived_id": conv_sha[:16],
            "gain_db": gain_db,
            "volume_mode": "manual",
            "target_lufs": None,
            "codec": val.codec,
            "rate": val.sample_rate,
            "channels": val.channels,
            "bitrate_kbps": at9.DEFAULT_BITRATE,
            "duration": round(val.duration_seconds, 2),
            "loudness": loud,
            "loudness_report": loud_rep.model_dump(),
            "loop_report": loop_report.model_dump(),
            "post_at9_loop": post_at9_loop,
            "loop_start_sample": val.loop_start_sample,
            "loop_end_sample": val.loop_end_sample,
            "play_count": val.play_count,
            "validation": val.model_dump(),
            "encoder_backend": enc_info["backend"],
            "conversion_params": cp,
        }
    )
    job.save()
    forge_audio.ensure_audio_job_previews(job)
    stamp = int(time.time() * 1000)
    return {
        "slot": "bgm",
        "job_id": job.id,
        "path": spec["path"],
        "name": job.original_name,
        "codec": val.codec,
        "rate": val.sample_rate,
        "channels": val.channels,
        "duration": round(val.duration_seconds, 2),
        "bytes": target_at9.stat().st_size,
        "source_codec": job.meta.get("source_codec", val.codec),
        "loudness": loud,
        "loudness_report": loud_rep.model_dump(),
        "loop_report": loop_report.model_dump(),
        "post_at9_loop": post_at9_loop,
        "validation": val.model_dump(),
        "volume_mode": "manual",
        "volume_params": vol_params,
        "derived_id": conv_sha[:16],
        "original_sha256": job.meta.get("original_sha256"),
        "converted_sha256": conv_sha,
        "encoder_backend": enc_info["backend"],
        "preview": f"/api/audio/{job.id}/preview?v={stamp}",
        "seam_preview": f"/api/audio/{job.id}/seam-preview?v={stamp}",
        "slot_label": spec["label"],
        "gain_db": gain_db,
    }


@app.post("/api/audio/{job_id}/gain")
def audio_gain(job_id: str, gain_db: float = Form(0.0)) -> dict:
    """Re-applies the gain to an already uploaded audio, without re-uploading."""
    job = _get_job("audio", job_id)
    slot_id = str(job.meta.get("slot") or "bgm")
    spec = AUDIO_SLOTS.get(slot_id)
    if spec is None:
        raise HTTPException(404, "That job has no valid audio slot.")
    clamped = _clamp_gain(gain_db)
    if slot_id == "bgm" and isinstance(job.meta.get("conversion_params"), dict):
        return _reapply_forge_bgm_with_gain(job, spec, clamped)
    return _procesar_audio(job, spec, clamped)


@app.get("/api/audio/{job_id}/preview")
def audio_preview(job_id: str) -> FileResponse:
    """Streams a Chrome-playable WAV preview decoded from the active .at9 or .vag audio job."""
    job = _get_job("audio", job_id)
    preview_wav, _seam = forge_audio.ensure_audio_job_previews(job)
    return FileResponse(preview_wav, media_type="audio/wav", headers={"Cache-Control": "private, no-store"})


@app.get("/api/audio/{job_id}/seam-preview")
def audio_seam_preview(job_id: str) -> FileResponse:
    """Streams a short WAV focusing on the loop boundary (tail -> head) of the active BGM .at9."""
    job = _get_job("audio", job_id)
    _prev, seam_wav = forge_audio.ensure_audio_job_previews(job)
    if seam_wav is None or not seam_wav.is_file():
        raise HTTPException(404, "Prévia de costura de loop disponível apenas para música do menu.")
    return FileResponse(seam_wav, media_type="audio/wav", headers={"Cache-Control": "private, no-store"})


def _resolve_slot_jobs(raw: dict[str, str]) -> dict[str, jobs.Job]:
    """Turns {slot: job_id} into jobs, applying the fallbacks.

    An empty slot with a `fallback` reuses another one's image, which is what
    lets you upload just the home background and get the rest for free.
    """
    direct: dict[str, jobs.Job] = {}
    for slot_id, job_id in raw.items():
        if not job_id:
            continue
        if slot_id not in IMAGE_SLOTS:
            raise HTTPException(422, f"Slot desconhecido: {slot_id}")
        direct[slot_id] = _get_job("slot", job_id)

    resolved = dict(direct)
    for slot_id, spec in IMAGE_SLOTS.items():
        if slot_id in resolved:
            continue
        source = spec.get("fallback")
        if source and source in direct:
            resolved[slot_id] = direct[source]
    return resolved

@app.get("/api/audio/{job_id}/info")
def audio_info(job_id: str) -> dict:
    """Data about an already uploaded audio.

    It exists so the interface can restore the session after a reload: the
    files are still in workspace/, but the browser only remembers the id.
    Answers 404 when jobs.prune() has already taken it.
    """
    job = _get_job("audio", job_id)
    slot_id = str(job.meta.get("slot") or "bgm")
    spec = AUDIO_SLOTS.get(slot_id)
    if spec is None:
        raise HTTPException(404, f"Slot de áudio desconhecido: {slot_id}")

    converted = job.meta.get("converted")
    blob = job.out_dir / str(converted) if converted else job.source
    if not blob.is_file():
        raise HTTPException(404, "The file is gone.")

    info = media.probe_audio(blob)
    stamp = int(time.time() * 1000)
    return {
        "slot": slot_id,
        "job_id": job.id,
        "path": spec["path"],
        "name": job.original_name,
        "codec": info.codec,
        "rate": info.rate,
        "channels": info.channels,
        "duration": round(info.duration, 1),
        "bytes": blob.stat().st_size,
        "loudness": job.meta.get("loudness"),
        "loudness_report": job.meta.get("loudness_report"),
        "validation": job.meta.get("validation"),
        "loop_report": job.meta.get("loop_report"),
        "post_at9_loop": job.meta.get("post_at9_loop"),
        "volume_mode": job.meta.get("volume_mode", "original"),
        "derived_id": job.meta.get("derived_id"),
        "original_sha256": job.meta.get("original_sha256"),
        "converted_sha256": job.meta.get("converted_sha256"),
        "encoder_backend": job.meta.get("encoder_backend"),
        "preview": f"/api/audio/{job.id}/preview?v={stamp}",
        "seam_preview": f"/api/audio/{job.id}/seam-preview?v={stamp}" if slot_id == "bgm" else None,
        # The interface matches the effect row by its label.
        "slot_label": spec["label"],
        "gain_db": job.meta.get("gain_db", 0.0),
    }


# --- Standalone converter -------------------------------------------------
# The theme slots already convert on their own; this is the same machinery
# exposed as a utility, for when you just want the file.

CONVERT_TARGETS = {
    "wav": {
        "label": "WAV (PCM 16 bits)",
        "ext": ".wav",
        "needs_tool": None,
        "note": "Intermediate step; this is what at9tool takes.",
    },
    "at9": {
        "label": "ATRAC9 (.at9)",
        "ext": ".at9",
        "needs_tool": "at9tool",
        "note": "Menu music. Needs at9tool.",
    },
    "vag": {
        "label": "VAG (PS-ADPCM)",
        "ext": ".vag",
        "needs_tool": None,
        "note": "Sound effects. Generated here, mono.",
    },
}


@app.get("/api/convert/targets")
def convert_targets() -> dict:
    return {
        "targets": [
            {
                "id": key,
                "label": spec["label"],
                "ext": spec["ext"],
                "note": spec["note"],
                "available": spec["needs_tool"] != "at9tool" or at9.available(),
            }
            for key, spec in CONVERT_TARGETS.items()
        ],
        "accepted": sorted(SOUND_EXTS),
    }


@app.post("/api/convert/{target}")
async def convert_audio(target: str, file: UploadFile = File(...)) -> dict:
    """Converts a standalone audio file and leaves it ready to download."""
    spec = CONVERT_TARGETS.get(target)
    if spec is None:
        raise HTTPException(404, f"Unknown format: {target}")
    if spec["needs_tool"] == "at9tool" and not at9.available():
        raise HTTPException(
            422, "at9tool was not found, so ATRAC9 cannot be generated."
        )

    data = await _read_upload(file, SOUND_EXTS)
    job = jobs.create("convert", file.filename or "source", data)
    try:
        info = media.probe_audio(job.source)

        stem = Path(job.original_name).stem or "audio"
        dest = job.out_dir / f"{stem}{spec['ext']}"

        if target == "wav":
            media.to_wav(job.source, dest, at9.SAMPLE_RATE)
        elif target == "at9":
            wav = job.out_dir / "_tmp.wav"
            try:
                media.to_wav(job.source, wav, at9.SAMPLE_RATE, at9.MAX_SECONDS)
                at9.encode(wav, dest)
            finally:
                wav.unlink(missing_ok=True)
        else:
            pcm = media.pcm_mono(job.source, at9.SAMPLE_RATE, MAX_SFX_SECONDS)
            dest.write_bytes(vag.encode(pcm, at9.SAMPLE_RATE, stem[:15]))

        job.meta["out"] = dest.name
        job.save()
    except BaseException:
        shutil.rmtree(job.root, ignore_errors=True)
        raise

    out_info = media.probe_audio(dest) if target != "vag" else None
    return {
        "job_id": job.id,
        "download": f"/api/convert/{job.id}/download",
        "name": dest.name,
        "bytes": dest.stat().st_size,
        "source": {"name": job.original_name, "codec": info.codec,
                   "rate": info.rate, "channels": info.channels,
                   "duration": round(info.duration, 1)},
        "result": {"codec": out_info.codec if out_info else "vag (PS-ADPCM)",
                   "rate": out_info.rate if out_info else at9.SAMPLE_RATE,
                   "channels": out_info.channels if out_info else 1},
    }


@app.get("/api/convert/{job_id}/download")
def convert_download(job_id: str) -> FileResponse:
    job = _get_job("convert", job_id)
    name = str(job.meta.get("out") or "")
    path = job.out_dir / name
    if not name or Path(name).name != name or not path.is_file():
        raise HTTPException(404, "That file is gone. Convert again.")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


# --- Packaging ------------------------------------------------------------

def _add(included: list[dict], asset: str, path: Path, target: str) -> None:
    included.append({"asset": asset, "path": target, "bytes": path.stat().st_size})


def _composed_animation_size(job: jobs.Job, raw: str) -> tuple[int, int]:
    if not raw:
        return tuple(job.meta.get("size") or ANIMATED_SIZE)
    for size in ANIMATED_SIZES:
        if raw == f"{size[0]}x{size[1]}":
            return size
    raise HTTPException(422, "Resolução RAF inválida.")


def _render_animation_overlay(composition, size):
    try:
        return compositor.animation_overlay(composition, size)
    except (OSError, MediaError) as exc:
        raise HTTPException(422, "A composição da animação não pôde ser renderizada.") from exc


def _add_animated_scene(
    job: jobs.Job, frames: int, wait_seconds: float, backend: str, content_dir: Path,
    composition=None, animation_size: str = "",
) -> dict:
    """Writes scene/background.raf/ with the frames, the plane and the scene.

    Reuses pre-approved Ouroboros/Deluxe DDS frames byte-for-byte when available
    and no compositor overlay alters them, preserving exact RAF equivalence.
    """
    raf_dir = content_dir / THEME_PATHS["raf_dir"]
    raf_dir.mkdir(parents=True, exist_ok=True)

    size = _composed_animation_size(job, animation_size)
    overlay = _render_animation_overlay(composition, size)
    names: list[str] = []
    total = 0

    deluxe = job.meta.get("deluxe")
    deluxe_match = (
        isinstance(deluxe, dict)
        and deluxe.get("frames") == frames
        and list(deluxe.get("size") or []) == list(size)
    )
    dds_dir = job.root / "deluxe_dds"
    png_dir = job.root / "deluxe_frames"

    if (
        deluxe_match
        and overlay is None
        and backend == DEFAULT_BACKEND
        and dds_dir.is_dir()
        and all((dds_dir / theme.frame_name(i)).is_file() for i in range(frames))
    ):
        for index in range(frames):
            name = theme.frame_name(index)
            blob = (dds_dir / name).read_bytes()
            (raf_dir / name).write_bytes(blob)
            names.append(name)
            total += len(blob)
    elif (
        deluxe_match
        and png_dir.is_dir()
        and all((png_dir / f"{i:02d}.png").is_file() for i in range(frames))
    ):
        from PIL import Image
        import numpy as np

        def _deluxe_rgba():
            for index in range(frames):
                with Image.open(png_dir / f"{index:02d}.png") as im:
                    yield np.asarray(im.convert("RGBA"))

        for index, blob in encode_sequence(compositor.compose_frames(_deluxe_rgba(), overlay), backend):
            name = theme.frame_name(index)
            (raf_dir / name).write_bytes(blob)
            names.append(name)
            total += len(blob)
    else:
        info = media.probe(job.source)
        frames_in = media.iter_raw_frames(job.source, frames, size, info.duration)
        for index, blob in encode_sequence(compositor.compose_frames(frames_in, overlay), backend):
            name = theme.frame_name(index)
            (raf_dir / name).write_bytes(blob)
            names.append(name)
            total += len(blob)

    half_w, half_h = raf.plane_half_size()
    plane = mdx.quad_model(node_name=raf.MODEL_ID, half_w=half_w, half_h=half_h)
    (raf_dir / raf.MODEL_REAL_FILE).write_bytes(plane)

    theme.write_index_xml(raf_dir / "index.xml", raf.build_scene(names, wait_seconds))

    return {
        "asset": "Fundo animado",
        "path": f"{THEME_PATHS['raf_dir']}/ ({len(names)} .dds + plano + cena)",
        "bytes": total + len(plane),
    }


def _collect_slot_assets(
    slot_sources: dict[str, Path],
    content_dir: Path,
    animated: bool = False,
    glow_sources: dict[str, Path] | None = None,
) -> list[dict]:
    """Renderiza cada hueco a su destino dentro del tema.

    With an animated background the background.raf scene is drawn over both
    screens and the texture/background/ PNGs are never seen. Verified on a
    console, and it matches the reference theme, which carries no
    texture/background/ at all when it has a scene. Copying them would be ~3 MB
    of dead weight, so they are skipped; the slots still exist because the
    thumbnail, the icon and pic1 all come from the home one.
    """
    included: list[dict] = []
    by_group: dict[str, list[str]] = {}
    glow_map = glow_sources or {}

    for slot_id, source in slot_sources.items():
        spec = IMAGE_SLOTS[slot_id]
        if animated and spec.get("hidden_by_raf"):
            continue
        dest = content_dir / spec["path"]
        media.render_image(source, dest, spec["size"])
        total = dest.stat().st_size

        if "glow_path" in spec:
            glow = content_dir / spec["glow_path"]
            glow_src = glow_map.get(slot_id, source)
            if glow_src is not source and glow_src.is_file():
                glow.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(glow_src, glow)
            else:
                media.render_image(glow_src, glow, spec["glow_size"])
            total += glow.stat().st_size

        by_group.setdefault(spec["group"], []).append(slot_id)
        if spec["group"] == "background":
            included.append({"asset": spec["label"]["en"], "path": spec["path"], "bytes": total})
        else:
            # Icons are summarised per group: 19 rows add nothing.
            group = by_group[spec["group"]]
            entry = next((e for e in included if e.get("_group") == spec["group"]), None)
            if entry is None:
                label = next(g["label"]["en"] for g in SLOT_GROUPS if g["id"] == spec["group"])
                entry = {"asset": label, "path": "", "bytes": 0, "_group": spec["group"]}
                included.append(entry)
            entry["bytes"] += total
            entry["path"] = f"{Path(spec['path']).parent.as_posix()}/ ({len(group)})"

    for entry in included:
        entry.pop("_group", None)
    return included


def _collect_theme_assets(
    slot_jobs: dict[str, jobs.Job],
    theme_settings: dict, content_dir: Path,
    audio_job: str = "", sfx_jobs: dict[str, str] | None = None,
    animated_job: str = "", frames: int = 24, wait_seconds: float = 0.1,
    backend: str = DEFAULT_BACKEND,
    composition=None, direct_slots: set[str] | None = None, animation_size: str = "",
) -> list[dict]:
    """Assembles the theme tree inside the content folder.

    The layout is the one orbis-pub-cmd validates and the one a real extracted
    theme carries: no loose `background.dds` or `icon0.png` at the root, which
    do not exist in the format.
    """
    included: list[dict] = []

    # theme.xml -- without it the console crashes when applying the theme.
    # homebgm-enable is only turned on when there is music: True without a
    # sound/bgm_home.at9 inside makes the console play its own.
    xml = theme_xml.build_theme_xml(**theme_settings, homebgm_enable=bool(audio_job))
    theme.write_index_xml(content_dir / THEME_PATHS["theme_xml"], xml)
    _add(included, "theme.xml", content_dir / THEME_PATHS["theme_xml"], THEME_PATHS["theme_xml"])

    if audio_job:
        job = _get_job("audio", audio_job)
        spec = AUDIO_SLOTS[str(job.meta.get("slot") or "bgm")]
        # If it was converted, the good one is the converted file.
        converted = job.meta.get("converted")
        source = job.out_dir / str(converted) if converted else job.source
        dest = content_dir / spec["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
        _add(included, spec["label"]["en"], dest, spec["path"])

    for slot_id, job_id in (sfx_jobs or {}).items():
        spec = AUDIO_SLOTS.get(slot_id)
        if spec is None or not job_id:
            continue
        job = _get_job("audio", job_id)
        blob = job.out_dir / str(job.meta.get("converted") or "sound.vag")
        if not blob.is_file():
            continue
        dest = content_dir / spec["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob.read_bytes())
        _add(included, f"SFX: {spec['label']['en']}", dest, spec["path"])

    with tempfile.TemporaryDirectory(dir=content_dir.parent) as staging:
        orig_sources = {slot: job.source for slot, job in slot_jobs.items()}
        sources = compositor.composed_slots(
            composition, orig_sources,
            direct_slots or set(), Path(staging),
        )
        glow_sources = compositor.composed_glow_slots(composition, orig_sources, Path(staging))
        included += _collect_slot_assets(
            sources, content_dir, animated=bool(animated_job), glow_sources=glow_sources,
        )

        # pic1 uses the same home artwork as the canonical theme.
        home = sources.get("home")
        if home is not None:
            pic1 = content_dir / PKG_FIXED_PATHS["pic1"]
            media.render_image(home, pic1, PIC1_SIZE)
            _add(included, "pic1 (ficha)", pic1, PKG_FIXED_PATHS["pic1"])

    if animated_job:
        anim_job = _get_job("animated", animated_job)
        included.append(
            _add_animated_scene(anim_job, frames, wait_seconds, backend, content_dir, composition, animation_size)
        )

    return included


@app.get("/api/package/defaults")
def package_defaults() -> dict:
    forge_info = pkg_forge.pkg_forge_info()
    return {
        "available": pkg.tools_available() or forge_info["available"],
        "pkg_forge": forge_info,
        "tools_dir": str(pkg.PKG_TOOLS_DIR),
        "defaults": {
            "title": "Mi Tema",
            "title_id": PKG_DEFAULTS["title_id"],
            "category": PKG_DEFAULTS["category"],
            "passcode": PKG_DEFAULTS["passcode"],
            "mode": PKG_DEFAULTS["mode"],
            "content_id": pkg.default_content_id(PKG_DEFAULTS["title_id"], "MI TEMA"),
        },
        "fixed_paths": PKG_FIXED_PATHS,
        "categories": PKG_CATEGORIES,
        "theme_colors": [{"value": v, "label": lbl} for v, lbl in theme_xml.THEME_COLORS],
        "theme_defaults": {
            "themecolor": theme_xml.DEFAULTS["themecolor"],
            "fontcolor": theme_xml.DEFAULTS["fontcolor"],
            "fontshadowcolor": theme_xml.DEFAULTS["fontshadowcolor"],
            "focuscolor": theme_xml.DEFAULTS["focuscolor"],
            "format_version": theme_xml.FORMAT_VERSION,
        },
        "modes": [
            {
                "id": key,
                "label": profile["label"],
                "volume_type": profile["volume_type"],
                "iro_tag": profile["iro_tag"],
            }
            for key, profile in PKG_MODES.items()
        ],
    }


@app.get("/api/package/content-id")
def package_content_id(title_id: str, title: str = "") -> dict:
    """Suggested Content ID for the title id and name being typed."""
    clean_id = (title_id or "").strip().upper()
    if not pkg.TITLE_ID_RE.match(clean_id):
        raise HTTPException(422, "Invalid Title ID: 4 letters and 5 digits, e.g. CUSA00000.")
    return {"content_id": pkg.default_content_id(clean_id, title)}


@app.post("/api/package/build")
async def package_build(
    title: str = Form(...),
    title_id: str = Form(...),
    content_id: str = Form(...),
    category: str = Form(PKG_DEFAULTS["category"]),
    passcode: str = Form(PKG_DEFAULTS["passcode"]),
    mode: str = Form(PKG_DEFAULTS["mode"]),
    slots: str = Form("{}"),
    animated_job: str = Form(""),
    audio_job: str = Form(""),
    sfx: str = Form("{}"),
    frames: int = Form(24),
    wait_seconds: float = Form(0.1),
    backend: str = Form(DEFAULT_BACKEND),
    themecolor: int = Form(0),
    fontcolor: str = Form(theme_xml.DEFAULTS["fontcolor"]),
    fontshadowcolor: str = Form(theme_xml.DEFAULTS["fontshadowcolor"]),
    focuscolor: str = Form(theme_xml.DEFAULTS["focuscolor"]),
    composition: str | None = Form(None),
    composition_mode: str = Form(""),
    animation_size: str = Form(""),
    artifacts_only: bool = Form(False),
) -> dict:
    """Assembles the theme tree and builds the .pkg.

    `slots` is a JSON {slot: job_id} with each slot's image. Empty ones that
    have a fallback copy from it.
    """
    try:
        raw_slots = json.loads(slots or "{}")
        if not isinstance(raw_slots, dict):
            raise ValueError
    except ValueError as exc:
        raise HTTPException(422, "The `slots` field must be a JSON object.") from exc

    try:
        parsed_sfx = {str(k): str(v) for k, v in (json.loads(sfx or "{}") or {}).items()}
    except ValueError as exc:
        raise HTTPException(422, "The `sfx` field must be a JSON object.") from exc

    state = compositor.load_composition(composition)
    if artifacts_only:
        compositor.workspace()
    has_overlay = bool(state and state.surface("animated"))
    if animated_job and has_overlay and not math.isfinite(wait_seconds):
        raise HTTPException(422, "A temporização da composição é inválida.")
    if composition_mode == "animated" and has_overlay and not animated_job:
        raise HTTPException(422, "Selecione uma fonte animada para exportar as sobreposições.")
    slot_jobs = _resolve_slot_jobs({str(k): str(v) for k, v in raw_slots.items()})
    if "home" not in slot_jobs and not (state and state.surface("home")):
        raise HTTPException(
            400, "The home background is missing: it is the only required part of the theme."
        )

    frame_count = frames
    animation_size = animation_size if has_overlay else ""
    if animated_job:
        anim_size = _composed_animation_size(_get_job("animated", animated_job), animation_size)
        frame_count = _clamp_frames(frames, anim_size)
        if has_overlay and frame_count != frames:
            raise HTTPException(422, "A composição ultrapassa o orçamento RAF da resolução selecionada.")
        try:
            theme.check_frame_count(frame_count)
            theme.check_budget(frame_count * dds_bytes_for(*anim_size))
        except ValueError as exc:
            raise HTTPException(413, str(exc)) from exc

    try:
        theme_settings = {
            "label": title,
            "themecolor": themecolor,
            "fontcolor": fontcolor.strip(),
            "fontshadowcolor": fontshadowcolor.strip(),
            "focuscolor": focuscolor.strip(),
        }
        # Validate up front, before generating anything.
        theme_xml.build_theme_xml(**theme_settings)
    except theme_xml.ThemeXmlError as exc:
        raise HTTPException(422, str(exc)) from exc

    # The music's real loudness goes into param.sfo as bgmhome_loud: the
    # console uses it to level the theme's music against the rest of the system.
    bgm_loudness = None
    if audio_job:
        bgm_loudness = _get_job("audio", audio_job).meta.get("loudness")

    try:
        meta = pkg.PackageMeta(
            title=title.strip(),
            title_id=title_id.strip().upper(),
            content_id=content_id.strip().upper(),
            category=category.strip().lower(),
            passcode=passcode.strip(),
            mode=mode.strip().lower(),
            bgm_loudness=bgm_loudness,
        )
        meta.validate()
    except pkg.PackageError as exc:
        raise HTTPException(422, str(exc)) from exc

    build_job = jobs.create("package", "package.bin", b"\0")
    work_dir = build_job.root / "build"
    content_dir = work_dir / "content"
    content_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    pkg_report_dict = None
    log_steps: list[dict] = []
    try:
        included = _collect_theme_assets(
            slot_jobs, theme_settings, content_dir, audio_job=audio_job,
            sfx_jobs=parsed_sfx,
            animated_job=animated_job, frames=frame_count,
            wait_seconds=max(MIN_WAIT_SECONDS if has_overlay else 0.01,
                             min(MAX_WAIT_SECONDS if has_overlay else 10.0, wait_seconds)),
            backend=backend, composition=state,
            direct_slots={key for key, value in raw_slots.items() if value},
            animation_size=animation_size,
        )
        if artifacts_only:
            archive = work_dir / "recursos-tema.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(content_dir.rglob("*")):
                    if path.is_file():
                        bundle.write(path, path.relative_to(content_dir).as_posix())
            return {
                "download": f"/api/package/{build_job.id}/assets",
                "included": included, "bytes": archive.stat().st_size,
            }
        if pkg.tools_available():
            pkg_path, log = pkg.build_package(work_dir, content_dir, meta)
            log_steps = log.steps
        elif pkg_forge.pkg_forge_info()["available"]:
            sce_sys = content_dir / "sce_sys"
            sce_sys.mkdir(parents=True, exist_ok=True)
            (sce_sys / "param.sfo").write_bytes(forge_sfo.build_param_sfo_bytes(meta))
            targets = sorted(
                p.relative_to(content_dir).as_posix()
                for p in content_dir.rglob("*") if p.is_file()
            )
            gp4_path = work_dir / "theme.gp4"
            gp4_path.write_text(pkg.build_gp4(meta, targets, content_dir.name), encoding="utf-8")
            pkg_path, _b_rep, p_rep, forge_steps = pkg_forge.build_and_validate_theme_pkg(
                content_dir, gp4_path, meta, work_dir / "pkg"
            )
            pkg_report_dict = p_rep.model_dump()
            log_steps = [
                {"label": s["label"], "cmd": s["label"], "output": s["detail"], "seconds": s["seconds"]}
                for s in forge_steps
            ]
        else:
            raise pkg.PackageError(
                "Empacotador PKG não encontrado. Execute ./scripts/install_forge_tools.sh pkg "
                "para instalar o empacotador nativo Linux."
            )
    except (pkg.PackageError, pkg_forge.PkgForgeError, theme_xml.ThemeXmlError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, "Não foi possível gravar os recursos do tema.") from exc

    build_job.meta["pkg"] = pkg_path.name
    if pkg_report_dict is not None:
        build_job.meta["pkg_report"] = pkg_report_dict
    build_job.save()

    return {
        "job_id": build_job.id,
        "download": f"/api/package/{build_job.id}/download",
        "pkg_name": pkg_path.name,
        "pkg_bytes": pkg_path.stat().st_size,
        "elapsed": round(time.time() - started, 2),
        "included": included,
        "content_id": meta.content_id,
        "category": meta.category,
        "mode": meta.mode,
        "volume_type": meta.profile["volume_type"],
        "iro_tag": meta.profile["iro_tag"],
        "log": log_steps,
        "pkg_report": pkg_report_dict,
    }


@app.get("/api/package/{job_id}/download")
def package_download(job_id: str) -> FileResponse:
    job = _get_job("package", job_id)
    name = str(job.meta.get("pkg") or "")
    path = job.root / "build" / "pkg" / name
    if not name or Path(name).name != name or not path.is_file():
        raise HTTPException(404, "That .pkg is no longer available. Build it again.")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@app.get("/api/package/{job_id}/assets")
def asset_archive(job_id: str) -> FileResponse:
    compositor.workspace()
    job = _get_job("package", job_id)
    archive = job.root / "build" / "recursos-tema.zip"
    if not archive.is_file():
        raise HTTPException(404, "O ZIP não está disponível.")
    return FileResponse(archive, media_type="application/zip", filename=archive.name)


# --- Interface ------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
