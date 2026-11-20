"""Persistent editor state layered over the canonical media jobs."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.responses import JSONResponse

from . import jobs
from .composition_model import Composition
from .config import (
    ANIMATED_SIZES, AUDIO_SLOTS, IMAGE_SLOTS, MIN_WAIT_SECONDS, MAX_WAIT_SECONDS,
    WORKSPACE, max_frames_for,
)

ROOT = WORKSPACE / "studio-projects-v1"
COOKIE = "czar_projects_session"
router = APIRouter(prefix="/api/projects")
JobId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12}$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Text = Annotated[str, StringConstraints(max_length=256)]
Color = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ThemeFields(Model):
    title: Text = Field(alias="pkg-title")
    title_id: Text = Field(alias="pkg-title-id")
    content_id: Text = Field(alias="pkg-content-id")
    theme_color: Literal["0", "1", "2", "3", "4", "5", "6", "7"] = Field(alias="pkg-themecolor")
    font: Color = Field(alias="pkg-fontcolor")
    shadow: Color = Field(alias="pkg-fontshadow")
    focus: Color = Field(alias="pkg-focuscolor")


class Animation(Model):
    jobId: JobId | None
    frames: int = Field(strict=True, ge=2)
    wait: float = Field(ge=MIN_WAIT_SECONDS, le=MAX_WAIT_SECONDS)
    size: tuple[int, int]
    backend: Literal["native", "magick"] = "native"

    @model_validator(mode="after")
    def check_budget(self):
        if self.size not in ANIMATED_SIZES or self.frames > max_frames_for(self.size):
            raise ValueError("Resolução ou quantidade de quadros inválida.")
        return self


class Editor(Model):
    campos: ThemeFields
    bgMode: Literal["static", "animated"]
    contentIdTouched: bool
    slots: dict[str, JobId] = Field(max_length=len(IMAGE_SLOTS))
    bgm: JobId | None
    bgmGain: float = Field(ge=-24, le=12)
    sfx: dict[str, JobId] = Field(max_length=len(AUDIO_SLOTS) - 1)
    anim: Animation | None
    composition: Composition | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def check_slots(self):
        if set(self.slots) - IMAGE_SLOTS.keys() or set(self.sfx) - (AUDIO_SLOTS.keys() - {"bgm"}):
            raise ValueError("Slot de mídia inválido.")
        return self


class NewProject(Model):
    name: Name


class SaveProject(NewProject):
    revision: int = Field(strict=True, ge=1)
    state: Editor


class Manifest(Model):
    schemaVersion: Literal[1] = 1
    id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    name: Name
    createdAt: datetime
    updatedAt: datetime
    revision: int = Field(strict=True, ge=1)
    state: Editor | None


def owner(request: Request) -> str | None:
    value = request.cookies.get(COOKIE, "")
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else None


def owner_path(request: Request) -> Path:
    session = owner(request)
    if session is None:
        raise HTTPException(403, "Sessão de projetos indisponível. Abra o editor novamente.")
    return ROOT / session


def project_path(request: Request, project: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", project):
        raise HTTPException(404, "Projeto não encontrado.")
    session = owner(request)
    root = ROOT / session / project if session else None
    if root is None or not (root / "manifest.json").is_file():
        raise HTTPException(404, "Projeto não encontrado nesta sessão.")
    return root


STATELESS_CATALOG_PATHS = {
    "/api/audio/slots",
    "/api/convert/targets",
    "/api/package/defaults",
    "/api/package/content-id",
    "/api/forge/audio/status",
    "/api/forge/status",
}


async def scope_jobs(request: Request, call_next):
    """Use the existing media endpoints with a private project directory."""
    project = request.query_params.get("project")
    path = request.url.path
    library_apply = path.startswith("/api/library/") and path.endswith("/apply")
    project_tool = path.startswith(("/api/composition/", "/api/ouroboros/"))
    protected = project is not None or project_tool or path.startswith(("/api/projects", "/api/library", "/api/forge"))
    if protected and request.method in {"POST", "PUT"}:
        if not valid_origin(request):
            return JSONResponse({"detail": "Origem da solicitação não autorizada."}, status_code=403)
    if (library_apply or project_tool) and project is None:
        return JSONResponse({"detail": "Abra um projeto antes de aplicar a mídia."}, status_code=400)
    if path in STATELESS_CATALOG_PATHS:
        return await call_next(request)
    if project is None and path.startswith("/api/forge/"):
        session = owner(request)
        minted = False
        if session is None:
            session = secrets.token_hex(32)
            minted = True
        standalone_ws = ROOT / session / "_standalone_forge"
        token = jobs.project_workspace.set(standalone_ws)
        try:
            response = await call_next(request)
        finally:
            jobs.project_workspace.reset(token)
        response.headers["Cache-Control"] = "private, no-store"
        if minted:
            response.set_cookie(
                COOKIE,
                session,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                max_age=60 * 60 * 24 * 365,
            )
        return response
    if project is None or not (library_apply or project_tool or path.startswith(
        ("/api/animated/", "/api/slot/", "/api/audio/", "/api/convert/", "/api/package/", "/api/forge/")
    )):
        return await call_next(request)
    try:
        root = project_path(request, project)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    token = jobs.project_workspace.set(root / "media")
    try:
        if request.method == "POST":
            import fcntl

            # Do not block the event loop while a sync manifest operation is
            # copying derivatives. No mutable output is shared across projects.
            with (root / ".revision.lock").open("a") as lock:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
                response = await call_next(request)
        else:
            response = await call_next(request)
    finally:
        jobs.project_workspace.reset(token)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def valid_origin(request: Request) -> bool:
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True
    hosts = {request.url.netloc.lower()}
    name = os.environ.get("CODESPACE_NAME")
    domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
    if name and domain:
        hosts.add(f"{name}-8000.{domain}".lower())
    try:
        parsed = urlsplit(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in hosts
    except ValueError:
        return False


def read(root: Path) -> Manifest:
    manifest = Manifest.model_validate_json((root / "manifest.json").read_bytes())
    if manifest.id != root.name:
        raise ValueError("Identificador do manifesto diferente do diretório.")
    return manifest


def atomic_write(root: Path, manifest: Manifest) -> None:
    path = root / "manifest.json"
    with tempfile.NamedTemporaryFile(dir=root, prefix=".manifest-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(manifest.model_dump_json(by_alias=True, indent=2).encode())
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def revision_lock(root: Path):
    """Serialize compare-and-replace across Linux workers."""
    import fcntl

    with (root / ".revision.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def find_references(root: Path, state: Editor) -> list[jobs.Job]:
    refs = [("slot", ident, slot) for slot, ident in state.slots.items()]
    refs.extend(("audio", ident, slot) for slot, ident in state.sfx.items())
    if state.bgm:
        refs.append(("audio", state.bgm, "bgm"))
    if state.anim and state.anim.jobId:
        refs.append(("animated", state.anim.jobId, None))
    token = jobs.project_workspace.set(root / "media")
    try:
        results = []
        for kind, ident, slot in refs:
            job = jobs.load(kind, ident)
            if not job or (slot is not None and job.meta.get("slot") != slot):
                raise HTTPException(422, "Referência de mídia inválida para este projeto.")
            if kind == "slot" and not (job.preview_dir / "preview.webp").is_file():
                raise HTTPException(422, "Referência de imagem incompleta.")
            if kind == "animated" and not job.meta.get("frames"):
                raise HTTPException(422, "Referência de animação incompleta.")
            if kind == "audio":
                derived = job.meta.get("converted")
                if (slot != "bgm" or derived) and (
                    derived not in {"sound.vag", "bgm.at9"} or
                    not (job.out_dir / derived).is_file()
                ):
                    raise HTTPException(422, "Referência de áudio incompleta.")
            results.append(job)
        return results
    finally:
        jobs.project_workspace.reset(token)


def copy_jobs(source: Path, destination: Path, state: Editor) -> None:
    for job in find_references(source, state):
        target = destination / "media" / job.root.name
        target.mkdir(parents=True)
        os.link(job.source, target / job.source.name)
        shutil.copyfile(job.meta_path, target / job.meta_path.name)
        (target / "out").mkdir()
        (target / "preview").mkdir()
        if job.kind == "slot":
            shutil.copyfile(job.preview_dir / "preview.webp", target / "preview" / "preview.webp")
        if job.kind == "animated" and job.meta.get("deluxe"):
            for sub in ("preview", "deluxe_preview", "deluxe_frames", "deluxe_dds"):
                src_sub = job.root / sub
                if src_sub.is_dir():
                    dst_sub = target / sub
                    dst_sub.mkdir(exist_ok=True)
                    for item_p in src_sub.iterdir():
                        if item_p.is_file():
                            shutil.copyfile(item_p, dst_sub / item_p.name)
        if job.kind == "audio" and job.meta.get("converted"):
            name = job.meta["converted"]
            shutil.copyfile(job.out_dir / name, target / "out" / name)


def new_project(parent: Path, name: str, previous: Path | None = None) -> Manifest:
    now = datetime.now(timezone.utc)
    state = read(previous).state if previous else None
    manifest = Manifest(
        id=secrets.token_hex(16), name=name, createdAt=now, updatedAt=now, revision=1, state=state,
    )
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=parent, prefix=".project-") as staging:
        root = Path(staging) / "new"
        root.mkdir(mode=0o700)
        if previous and state:
            copy_jobs(previous, root, state)
        atomic_write(root, manifest)
        root.rename(parent / manifest.id)
    return manifest


def read_error():
    return HTTPException(500, "Não foi possível ler o manifesto do projeto.")


@router.get("")
def list_projects(request: Request, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    session = owner(request)
    if session is None:
        response.set_cookie(
            COOKIE, secrets.token_hex(32), httponly=True, samesite="strict",
            secure=request.url.scheme == "https", max_age=60 * 60 * 24 * 365,
        )
        return []
    parent = ROOT / session
    try:
        manifests = [read(folder) for folder in parent.iterdir()
                     if re.fullmatch(r"[0-9a-f]{32}", folder.name)] if parent.exists() else []
    except (OSError, ValueError) as exc:
        raise read_error() from exc
    return [item.model_dump(mode="json", exclude={"state"}) for item in
            sorted(manifests, key=lambda item: item.updatedAt, reverse=True)]


@router.post("", status_code=201, response_model=Manifest)
def create_project(request: Request, payload: NewProject):
    try:
        return new_project(owner_path(request), payload.name)
    except OSError as exc:
        raise HTTPException(503, "Não foi possível criar o projeto no disco.") from exc


@router.get("/{project}", response_model=Manifest)
def get_project(request: Request, project: str, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return read(project_path(request, project))
    except (OSError, ValueError) as exc:
        raise read_error() from exc


@router.put("/{project}", response_model=Manifest)
def save_project(request: Request, project: str, payload: SaveProject):
    root = project_path(request, project)
    try:
        with revision_lock(root):
            current = read(root)
            if payload.revision != current.revision:
                if (payload.revision + 1 == current.revision and
                        payload.name == current.name and payload.state == current.state):
                    return current
                raise HTTPException(409, "Outra aba salvou uma revisão mais recente.")
            find_references(root, payload.state)
            if payload.state.composition:
                from .compositor import validate_references

                validate_references(root, payload.state.composition)
            updated = current.model_copy(update={
                "name": payload.name, "state": payload.state, "revision": current.revision + 1,
                "updatedAt": datetime.now(timezone.utc),
            })
            atomic_write(root, updated)
            return updated
    except OSError as exc:
        raise HTTPException(503, "Falha ao gravar. A última revisão salva foi preservada.") from exc
    except ValueError as exc:
        raise read_error() from exc


@router.post("/{project}/duplicate", status_code=201, response_model=Manifest)
def duplicate_project(request: Request, project: str, payload: NewProject):
    root = project_path(request, project)
    try:
        with revision_lock(root):
            return new_project(root.parent, payload.name, root)
    except OSError as exc:
        raise HTTPException(503, "Não foi possível duplicar o projeto.") from exc
    except ValueError as exc:
        raise read_error() from exc
