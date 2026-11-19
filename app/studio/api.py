from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from ..config import ANIMATED_SIZES, AUDIO_SLOTS, IMAGE_SLOTS, RAF_SIZE_LIMIT, max_frames_for
from . import media, queue, storage
from .schema import Model, Settings, Title, Track, VERSION, safe_name
from .storage import StudioError

router = APIRouter(prefix="/api/studio")
COOKIE = "czar_studio"
COOKIE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TOKEN = "x-czar-token"
UPLOADS = asyncio.Semaphore(2)
PREVIEWS = asyncio.Semaphore(2)


class NewProject(Model):
    name: Title
    mode: str = "static"


class Edit(Model):
    revision: int
    settings: Settings


class StartJob(Model):
    kind: str


def error_response(exc: StudioError) -> JSONResponse:
    return JSONResponse({"code": exc.code, "message": exc.message, "details": exc.details},
                        status_code=exc.status, headers={"Cache-Control": "no-store"})


async def validation_error(request: Request, exc: RequestValidationError):
    if not request.url.path.startswith("/api/studio"):
        return await request_validation_exception_handler(request, exc)
    details = [{"field": ".".join(map(str, issue["loc"])), "type": issue["type"]} for issue in exc.errors()]
    return error_response(StudioError(422, "invalid_request", "Revise os campos informados.", details))


def valid_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    if not origin:
        return True
    parsed = urlparse(origin)
    name = os.environ.get("CODESPACE_NAME")
    domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
    hosts = {request.url.netloc.lower()}
    if name and domain:
        hosts.add(f"{name}-8000.{domain}".lower())
    return parsed.scheme in {"https", "http"} and parsed.netloc.lower() in hosts


def csrf(secret: str) -> str:
    return hashlib.sha256(("czar-csrf:" + secret).encode("ascii")).hexdigest()


def require_session(request: Request) -> str:
    secret = request.cookies.get(COOKIE, "")
    if not COOKIE_PATTERN.fullmatch(secret):
        raise StudioError(401, "session_required", "Abra o editor para iniciar uma sessão.")
    if not valid_origin(request):
        raise StudioError(403, "invalid_origin", "Origem da solicitação inválida.")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not secrets.compare_digest(request.headers.get(TOKEN, ""), csrf(secret)):
            raise StudioError(403, "csrf", "Sessão expirada. Recarregue o editor.")
    return hashlib.sha256(("czar-owner:" + secret).encode("ascii")).hexdigest()


def project_view(project, *, assets=True):
    result = project.model_dump(mode="json")
    if not assets:
        result = {k: result[k] for k in ("id", "createdAt", "updatedAt", "revision")}
        result["name"] = project.settings.name
        result["mode"] = project.settings.mode
    result["thumbnail"] = project.thumbnail
    return result


@router.get("/session")
def session(request: Request, response: Response):
    if not valid_origin(request):
        raise StudioError(403, "invalid_origin", "Origem da solicitação inválida.")
    secret = request.cookies.get(COOKIE, "")
    if not COOKIE_PATTERN.fullmatch(secret):
        secret = secrets.token_hex(32)
    response.set_cookie(COOKIE, secret, max_age=60 * 60 * 24 * 180, httponly=True,
                        secure=request.url.scheme == "https" or bool(os.environ.get("CODESPACE_NAME")),
                        samesite="strict")
    return {
        "version": VERSION, "token": csrf(secret), "backend": "external", "pkgAvailable": False,
        "uploadLimit": storage.UPLOAD_LIMIT, "projectQuota": storage.PROJECT_QUOTA,
        "presets": [{"width": str(w), "height": h, "max": max_frames_for((w, h))}
                    for w, h in ANIMATED_SIZES],
        "rafLimit": RAF_SIZE_LIMIT, "slots": {k: {"group": v["group"], "size": v["size"],
            "glow": "glow_path" in v} for k, v in IMAGE_SLOTS.items()},
        "audioSlots": list(AUDIO_SLOTS),
    }


@router.get("/components")
def components(owner: str = Depends(require_session)):
    import subprocess

    from .. import at9, media as upstream
    data = {"editor": "MIT", "pillow": "HPND", "numpy": "BSD-3-Clause",
            "fastapi": "MIT", "pydantic": "MIT", "uvicorn": "BSD-3-Clause",
            "python-multipart": "Apache-2.0", "ffmpeg": None, "at9Encoder": False, "at9ToolDetected": at9.available(), "pkg": False}
    if upstream.FFMPEG:
        try:
            output = subprocess.check_output([upstream.FFMPEG, "-version"], timeout=3, text=True)
            data["ffmpeg"] = [line for line in output.splitlines() if line.startswith(("ffmpeg version", "configuration:"))]
        except (OSError, subprocess.SubprocessError):
            pass
    return data


@router.get("/projects")
def projects(owner: str = Depends(require_session)):
    return [project_view(p, assets=False) for p in storage.list_projects(owner)]


@router.post("/projects", status_code=201)
def create_project(data: NewProject, owner: str = Depends(require_session)):
    if data.mode not in {"static", "animated"}:
        raise StudioError(422, "mode", "Escolha fundo estático ou animado.")
    return project_view(storage.create(owner, Settings(name=data.name, mode=data.mode)))


async def stream_to(request: Request, path: Path, maximum: int):
    size = 0
    content_length = request.headers.get("content-length", "")
    if content_length.isdecimal() and (len(content_length) > 20 or int(content_length) > maximum):
        raise StudioError(413, "upload_size", "O arquivo excede o tamanho permitido.")
    try:
        with path.open("wb") as target:
            async for data in request.stream():
                size += len(data)
                if size > maximum:
                    raise StudioError(413, "upload_size", "O arquivo excede o tamanho permitido.")
                await run_in_threadpool(target.write, data)
    except ClientDisconnect:
        raise StudioError(400, "upload_cancelled", "Envio cancelado.") from None
    if not size:
        raise StudioError(422, "upload_empty", "O arquivo está vazio.")


def temporary() -> Path:
    directory = storage.ROOT / "staging"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


@router.post("/projects/import", status_code=201)
async def import_project(request: Request, owner: str = Depends(require_session)):
    async with UPLOADS:
        with tempfile.TemporaryDirectory(dir=temporary()) as staged:
            root = Path(staged)
            path = root / "import.zip"
            await stream_to(request, path, storage.PROJECT_QUOTA + 3 * storage.MANIFEST_LIMIT)
            project = await run_in_threadpool(storage.import_zip, owner, path, root)
            return project_view(project)


@router.get("/projects/{ident}")
def get_project(ident: str, owner: str = Depends(require_session)):
    return project_view(storage.load(owner, ident)[1])


@router.put("/projects/{ident}")
def edit_project(ident: str, request: Edit, owner: str = Depends(require_session)):
    with storage.LOCK:
        root, project = storage.load(owner, ident)
        if request.revision != project.revision:
            raise StudioError(409, "revision_conflict", "Uma versão mais recente do projeto já foi salva.",
                              {"revision": project.revision})
        try:
            project.check_references(request.settings)
        except ValueError as exc:
            raise StudioError(422, "invalid_reference", str(exc)) from None
        project.settings = request.settings
        project.revision += 1
        project.updatedAt = storage.now()
        storage.save(root, project)
    return project_view(project)


@router.post("/projects/{ident}/duplicate", status_code=201)
def duplicate_project(ident: str, data: NewProject, owner: str = Depends(require_session)):
    with storage.LOCK:
        root, project = storage.load(owner, ident)
        settings = project.settings.model_copy(update={"name": data.name})
        return project_view(storage.create(owner, settings, root, project))


@router.delete("/projects/{ident}", status_code=204)
def delete_project(ident: str, owner: str = Depends(require_session)):
    with queue.GUARD, storage.LOCK:
        root = storage.project_path(owner, ident)
        if any(job["projectId"] == ident for job in queue.ACTIVE.values()):
            raise StudioError(409, "job_active", "Cancele a tarefa ativa antes de excluir o projeto.")
        shutil.rmtree(root)
    return Response(status_code=204)


@router.post("/projects/{ident}/assets", status_code=201)
async def add_asset(ident: str, request: Request, owner: str = Depends(require_session)):
    storage.project_path(owner, ident)
    name = safe_name(unquote(request.headers.get("x-filename", "mídia")[:1024]))
    async with UPLOADS:
        with tempfile.TemporaryDirectory(dir=temporary()) as staged:
            stage = Path(staged)
            key = storage.new_id()
            path, preview = stage / key, stage / "preview.webp"
            await stream_to(request, path, storage.UPLOAD_LIMIT)
            asset = await run_in_threadpool(media.inspect_asset, path, name, preview)
            with storage.LOCK:
                root, project = storage.load(owner, ident)
                if (len(project.assets) >= storage.MAX_ASSETS or
                    sum(a.bytes for a in project.assets.values()) + asset.bytes > storage.PROJECT_QUOTA):
                    raise StudioError(413, "quota", "O projeto atingiu a cota de mídia.")
                target, thumb = root / "assets" / key, root / "previews" / key
                try:
                    path.replace(target)
                    if preview.is_file():
                        preview.replace(thumb)
                    project.assets[key] = asset
                    project.updatedAt = storage.now()
                    storage.save(root, project)
                except OSError:
                    target.unlink(missing_ok=True)
                    thumb.unlink(missing_ok=True)
                    raise
            return asset.model_dump(mode="json")


def asset_for(owner, ident, key):
    root, project = storage.load(owner, ident)
    asset = project.assets.get(key)
    if not asset:
        raise StudioError(404, "asset_missing", "Mídia indisponível.")
    return root, asset


@router.get("/projects/{ident}/assets/{key}/preview")
def asset_preview(ident: str, key: str, owner: str = Depends(require_session)):
    root, _ = asset_for(owner, ident, key)
    path = root / "previews" / key
    if not path.is_file():
        raise StudioError(404, "preview_missing", "A mídia não possui miniatura.")
    return FileResponse(path, media_type="image/webp")


@router.get("/projects/{ident}/assets/{key}/content")
def asset_content(ident: str, key: str, owner: str = Depends(require_session)):
    root, asset = asset_for(owner, ident, key)
    return FileResponse(root / "assets" / key, media_type=asset.mime)


@router.get("/projects/{ident}/assets/{key}/frame")
async def asset_frame(ident: str, key: str, t: float, owner: str = Depends(require_session)):
    import io

    root, asset = asset_for(owner, ident, key)
    if asset.kind not in {"animation", "video"} or not 0 <= t < asset.duration:
        raise StudioError(422, "frame_time", "Escolha um instante dentro da mídia animada.")
    async with PREVIEWS:
        frame = await run_in_threadpool(media.extract_frame, root / "assets" / key, t, (480, 270))
        output = io.BytesIO()
        frame.save(output, format="WEBP", quality=82)
    return Response(output.getvalue(), media_type="image/webp")


@router.post("/projects/{ident}/audio-preview/{slot}")
async def audio_preview(ident: str, slot: str, track: Track, owner: str = Depends(require_session)):
    root, asset = asset_for(owner, ident, track.asset)
    if slot not in AUDIO_SLOTS or asset.kind not in {"audio", "vag", "at9"}:
        raise StudioError(422, "audio_slot", "Selecione um áudio válido.")
    async with PREVIEWS:
        limit, channels = (30, 2) if slot == "bgm" else (5, 1)
        pcm = await run_in_threadpool(media.audio_pcm, root / "assets" / asset.id, asset, track, limit, channels)
        return Response(media.wav_bytes(pcm, channels), media_type="audio/wav")


@router.post("/projects/{ident}/jobs", status_code=202)
def new_job(ident: str, data: StartJob, owner: str = Depends(require_session)):
    with queue.GUARD, storage.LOCK:
        root, project = storage.load(owner, ident)
        return queue.submit(owner, root, project, data.kind)


@router.get("/projects/{ident}/jobs/{job_id}")
def job_status(ident: str, job_id: str, owner: str = Depends(require_session)):
    return queue.lookup(storage.project_path(owner, ident), job_id)[1]


@router.delete("/projects/{ident}/jobs/{job_id}")
def cancel_job(ident: str, job_id: str, owner: str = Depends(require_session)):
    return queue.cancel(storage.project_path(owner, ident), job_id)


@router.get("/projects/{ident}/jobs/{job_id}/download")
def download(ident: str, job_id: str, owner: str = Depends(require_session)):
    work, job = queue.lookup(storage.project_path(owner, ident), job_id)
    if job["state"] != "done" or not job.get("download"):
        raise StudioError(404, "output_missing", "A tarefa não tem um ZIP disponível.")
    return FileResponse(work / job["download"], filename=job["download"], media_type="application/zip")
