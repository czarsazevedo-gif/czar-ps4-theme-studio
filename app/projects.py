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
    ANIMATED_SIZES, AUDIO_SLOTS, IMAGE_SLOTS, MEDIA_FOLDERS, MEDIA_FOLDER_BY_ID,
    MIN_WAIT_SECONDS, MAX_WAIT_SECONDS, WORKSPACE, max_frames_for,
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
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    minted = getattr(request.state, "czar_session", None)
    return minted if isinstance(minted, str) and re.fullmatch(r"[0-9a-f]{64}", minted) else None


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
    "/api/system",
    "/api/system/health",
    "/api/system/tools/setup",
    "/api/system/tools/setup/status",
}


async def scope_jobs(request: Request, call_next):
    """Use the existing media endpoints with a private project directory."""
    project = request.query_params.get("project")
    path = request.url.path
    library_apply = path.startswith("/api/library/") and path.endswith("/apply")
    project_tool = path.startswith(("/api/composition/", "/api/ouroboros/"))
    is_uploads = path.startswith("/api/uploads")
    is_keepalive = path == "/api/keepalive"
    protected = (
        project is not None
        or project_tool
        or is_uploads
        or path.startswith(("/api/projects", "/api/library", "/api/forge", "/api/system/tools/"))
    )
    if protected and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if not valid_origin(request):
            return JSONResponse({"detail": "Origem da solicitação não autorizada."}, status_code=403)
    if (library_apply or project_tool) and project is None:
        return JSONResponse({"detail": "Abra um projeto antes de aplicar a mídia."}, status_code=400)
    if path in STATELESS_CATALOG_PATHS:
        return await call_next(request)
    if is_keepalive:
        token = None
        if project is not None:
            try:
                root = project_path(request, project)
                token = jobs.project_workspace.set(root / "media")
            except HTTPException:
                token = None
        try:
            response = await call_next(request)
        finally:
            if token is not None:
                jobs.project_workspace.reset(token)
        response.headers["Cache-Control"] = "private, no-store"
        return response
    if is_uploads:
        session = owner(request)
        minted = False
        if session is None:
            session = secrets.token_hex(32)
            minted = True
            request.state.czar_session = session
        token = None
        if project is not None:
            try:
                root = project_path(request, project)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            token = jobs.project_workspace.set(root / "media")
        try:
            response = await call_next(request)
        finally:
            if token is not None:
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
    if project is None and path.startswith("/api/forge/"):
        session = owner(request)
        minted = False
        if session is None:
            session = secrets.token_hex(32)
            minted = True
            request.state.czar_session = session
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
        ("/api/animated/", "/api/slot/", "/api/slots/", "/api/audio/", "/api/convert/", "/api/package/", "/api/forge/")
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
        port = os.environ.get("CZAR_PORT", "8000").strip()
        if port:
            hosts.add(f"{name}-{port}.{domain}".lower())
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


def folder_for_slot(slot_id: str | None, kind: str = "") -> str:
    """Map any editor slot or job kind onto one of the 5 dedicated project media folders."""
    sid = str(slot_id or "")
    if sid.startswith("ct:"):
        return "content_icons"
    if sid.startswith("fn:"):
        return "function_icons"
    if sid in AUDIO_SLOTS or kind == "audio":
        return "audio"
    if sid == "animated" or kind == "animated":
        return "animated_bg"
    return "theme_image"


def ensure_project_folders(project_root: Path) -> Path:
    """Create the 5 dedicated media subfolders inside a project root without altering legacy media/ layout."""
    base = project_root / "folders"
    for spec in MEDIA_FOLDERS:
        (base / spec["subdir"]).mkdir(parents=True, exist_ok=True)
    return base


def _link_or_copy(src: Path, dst: Path) -> None:
    if not src.is_file():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{secrets.token_hex(4)}.tmp")
    try:
        try:
            os.link(src, tmp)
        except OSError:
            shutil.copyfile(src, tmp)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def _safe_label(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(name or "media").name).strip("._")
    return cleaned[:96] or "media"


def mirror_job_to_project_folder(job: jobs.Job, *, slot_id: str | None = None) -> Path | None:
    """Mirror a prepared job's assets into the project's dedicated subfolder (hardlinked when possible)."""
    ws = jobs.project_workspace.get()
    if ws is None or ws.name != "media" or not (ws.parent / "manifest.json").is_file():
        return None
    project_root = ws.parent
    try:
        base = ensure_project_folders(project_root)
        eff_slot = slot_id or str(job.meta.get("slot") or "")
        folder_id = folder_for_slot(eff_slot, job.kind)
        subdir = base / MEDIA_FOLDER_BY_ID[folder_id]["subdir"]
        subdir.mkdir(parents=True, exist_ok=True)

        if job.kind == "slot":
            spec = IMAGE_SLOTS.get(eff_slot) or {}
            stem = str(spec.get("file") or eff_slot.replace(":", "_") or job.id)
            conv = job.meta.get("converted")
            conv_glow = job.meta.get("converted_glow")
            if conv and (job.out_dir / str(conv)).is_file():
                target = subdir / f"{stem}.png"
                _link_or_copy(job.out_dir / str(conv), target)
                if conv_glow and (job.out_dir / str(conv_glow)).is_file():
                    _link_or_copy(job.out_dir / str(conv_glow), subdir / f"{stem}_glow.png")
                return target
            target = subdir / f"{stem}-{job.id}{job.source.suffix.lower()}"
            _link_or_copy(job.source, target)
            return target

        if job.kind == "animated":
            safe = _safe_label(job.original_name)
            ext = job.source.suffix.lower() or ".mp4"
            if not safe.lower().endswith(ext):
                safe = f"{Path(safe).stem}{ext}"
            target = subdir / f"{job.id}-{safe}"
            _link_or_copy(job.source, target)
            return target

        if job.kind == "audio":
            conv = job.meta.get("converted")
            src = (job.out_dir / str(conv)) if conv and (job.out_dir / str(conv)).is_file() else job.source
            ext = src.suffix.lower() or ".at9"
            prefix = eff_slot or "audio"
            target = subdir / f"{prefix}-{job.id}{ext}"
            _link_or_copy(src, target)
            return target
    except OSError:
        return None
    return None


def sync_project_folders_from_state(project_root: Path, state: Editor) -> None:
    """Ensure every active media reference in `state` is mirrored under `<project>/folders/<subdir>/`."""
    try:
        ensure_project_folders(project_root)
        token = jobs.project_workspace.set(project_root / "media")
        try:
            for slot_id, job_id in state.slots.items():
                j = jobs.load("slot", job_id)
                if j is not None:
                    mirror_job_to_project_folder(j, slot_id=slot_id)
            if state.bgm:
                j = jobs.load("audio", state.bgm)
                if j is not None:
                    mirror_job_to_project_folder(j, slot_id="bgm")
            for slot_id, job_id in state.sfx.items():
                j = jobs.load("audio", job_id)
                if j is not None:
                    mirror_job_to_project_folder(j, slot_id=slot_id)
            if state.anim and state.anim.jobId:
                j = jobs.load("animated", state.anim.jobId)
                if j is not None:
                    mirror_job_to_project_folder(j, slot_id="animated")
        finally:
            jobs.project_workspace.reset(token)
    except OSError:
        pass


def list_project_folders(project_root: Path) -> list[dict]:
    """Return metadata and file listings for all 5 dedicated project media subfolders."""
    base = ensure_project_folders(project_root)
    out = []
    for spec in MEDIA_FOLDERS:
        folder_dir = base / spec["subdir"]
        files = []
        if folder_dir.is_dir():
            for entry in sorted(folder_dir.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_file() and not entry.name.startswith("."):
                    st = entry.stat()
                    files.append({
                        "name": entry.name,
                        "bytes": st.st_size,
                        "updated_at": st.st_mtime,
                    })
        out.append({
            "id": spec["id"],
            "subdir": spec["subdir"],
            "label": spec["label"],
            "count": len(files),
            "files": files,
        })
    return out


def copy_jobs(source: Path, destination: Path, state: Editor) -> None:
    for job in find_references(source, state):
        target = destination / "media" / job.root.name
        target.mkdir(parents=True)
        os.link(job.source, target / job.source.name)
        shutil.copyfile(job.meta_path, target / job.meta_path.name)
        (target / "out").mkdir()
        (target / "preview").mkdir()
        if job.kind == "slot":
            for prev_file in job.preview_dir.glob("*.webp"):
                if prev_file.is_file():
                    shutil.copyfile(prev_file, target / "preview" / prev_file.name)
            for out_file in job.out_dir.glob("*.png"):
                if out_file.is_file():
                    shutil.copyfile(out_file, target / "out" / out_file.name)
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
        ensure_project_folders(root)
        if previous and state:
            copy_jobs(previous, root, state)
        if previous:
            recipes_src = previous / "ouroboros-deluxe" / "recipes"
            if recipes_src.is_dir():
                recipes_dst = root / "ouroboros-deluxe" / "recipes"
                recipes_dst.mkdir(parents=True, mode=0o700, exist_ok=True)
                for rec_file in recipes_src.glob("*.json"):
                    if rec_file.is_file():
                        shutil.copyfile(rec_file, recipes_dst / rec_file.name)
        atomic_write(root, manifest)
        if previous and state:
            sync_project_folders_from_state(root, state)
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


@router.get("/{project}/folders")
def get_project_folders(request: Request, project: str, response: Response):
    """Inspect the 5 organized media subfolders of a project."""
    response.headers["Cache-Control"] = "private, no-store"
    root = project_path(request, project)
    try:
        return {"project": project, "folders": list_project_folders(root)}
    except OSError as exc:
        raise HTTPException(503, "Não foi possível ler as pastas de mídia do projeto.") from exc


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
            sync_project_folders_from_state(root, payload.state)
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

