"""Isolated diagnostic API. Only completed project-local derivatives are exposed."""

import json
import logging
import re
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field
from typing import Literal

from . import circulo, jobs, library, ouroboros_media as media, projects
from .composition_model import AssetId, Model
from .projects import JobId

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ouroboros")
pool = ThreadPoolExecutor(max_workers=2)
lock = Lock()
VERSION = "ouroboros-foundation-1"


class Source(Model):
    kind: Literal["library", "animated"]
    id: AssetId | JobId


class Inspect(Model):
    analysis: AssetId
    resolution: int = Field(ge=0, le=2)
    candidate: int = Field(ge=0, le=2)


@dataclass
class Task:
    root: Path
    signature: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    stop: Event = field(default_factory=Event)
    state: str = "working"
    stage: str = "Preparando análise"
    percent: int = 0
    key: str | None = None
    finished: float = 0.0

    def check(self):
        if self.stop.is_set():
            raise media.Cancelled()

    def progress(self, stage, percent):
        self.check()
        with lock:
            self.stage, self.percent = stage, percent

    def public(self):
        return dict(
            id=self.id,
            state=self.state,
            stage=self.stage,
            percent=self.percent,
            key=self.key,
        )


tasks: dict[str, Task] = {}


def workspace():
    root = jobs.project_workspace.get()
    if not root:
        raise HTTPException(400, "Abra um projeto para executar o diagnóstico.")
    return root.parent


def source(request, selection):
    if selection.kind == "library":
        asset, path = library.get_asset(request, selection.id)
        if asset.kind in {"video", "animation"} and asset.duration:
            return path, asset.duration, asset.originalName
    else:
        job = jobs.load("animated", selection.id)
        if job and job.meta.get("duration"):
            return job.source, job.meta["duration"], job.original_name
    raise HTTPException(422, "Selecione um vídeo disponível no projeto ou na biblioteca.")


def record(root, key):
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise HTTPException(404, "Resultado indisponível.")
    folder = root / "ouroboros-v1" / key
    try:
        return folder, json.loads((folder / "report.json").read_bytes())
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Resultado indisponível neste projeto.") from exc


def submit(root, signature, calculate):
    with lock:
        for ident, t in list(tasks.items()):
            if t.finished and time.monotonic() - t.finished > 600:
                del tasks[ident]
        running = [t for t in tasks.values() if t.state == "working"]
        for task in running:
            if task.root == root:
                if task.signature == signature and not task.stop.is_set():
                    return task.public()
                raise HTTPException(409, "Aguarde ou cancele a operação deste projeto.")
        if len(running) >= 2:
            raise HTTPException(429, "Os processadores estão ocupados.")
        task = Task(root, signature)
        tasks[task.id] = task
        pool.submit(execute, task, calculate)
        return task.public()


def healthy(folder, key, check):
    try:
        header = json.loads((folder / "report.json").read_bytes())
        if header["key"] != key or header["version"] != VERSION:
            return False
        if header["type"] == "analysis":
            return len(header["groups"]) == 3 and "sentinela" in header
        count = header["frames"]
        if not 1 <= count <= 48:
            return False
        names = [f"{i:02d}.webp" for i in range(count)]
        if header["type"] == "genesis":
            names.extend(["source.png", "background.raf.zip"])
        for name in names:
            check()
            if media.digest(folder / name, check) != header["files"][name]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def execute(task, calculate):
    try:
        key, fn = calculate(task)
        cache = task.root / "ouroboros-v1"
        cache.mkdir(exist_ok=True)
        dest = cache / key
        if not healthy(dest, key, task.check):
            with tempfile.TemporaryDirectory(dir=cache, prefix=".stage-") as tmp:
                stage = Path(tmp) / "result"
                stage.mkdir()
                data = fn(stage)
                data.update(key=key, version=VERSION)
                if "frames" in data:
                    data["files"] = {p.name: media.digest(p, task.check) for p in stage.glob("*.webp")}
                if data["type"] == "genesis":
                    for name in ("source.png", "background.raf.zip"):
                        data["files"][name] = media.digest(stage / name, task.check)
                (stage / "report.json").write_text(json.dumps(data, allow_nan=False), encoding="utf-8")
                with lock:
                    task.check()
                    if not dest.exists():
                        stage.rename(dest)
                    else:
                        # Readers see complete files from the same immutable recipe.
                        for name in data.get("files", {}):
                            (stage / name).replace(dest / name)
                        (stage / "report.json").replace(dest / "report.json")
        with lock:
            task.check()
            task.state, task.stage, task.percent, task.key = (
                "ready",
                "Concluído",
                100,
                key,
            )
    except media.Cancelled:
        with lock:
            task.state, task.stage = "cancelled", "Cancelado."
    except Exception as exc:
        log.warning(
            "ouroboros job=%s project=%s error=%s",
            task.id,
            task.root.name,
            type(exc).__name__,
        )
        with lock:
            task.state = "error"
            task.stage = (
                str(exc)
                if isinstance(exc, media.MotionError)
                else "Falha ao analisar. Confira a fonte e o espaço disponível."
            )
    finally:
        with lock:
            task.finished = time.monotonic()


@router.get("/current")
def current():
    state = projects.read(workspace()).state
    job = jobs.load("animated", state.anim.jobId) if state and state.anim and state.anim.jobId else None
    return {"id": job.id, "name": job.original_name} if job else None


@router.post("/analyze", status_code=202)
def analyze(request: Request, item: Source):
    root = workspace()
    path, duration, name = source(request, item)

    def prepare(task):
        sha = media.digest(path, task.check)
        key = media.fingerprint([VERSION, sha, item.model_dump()])

        def calculate(folder):
            result = circulo.search(path, duration, folder, task.progress, task.check)
            result.update(source=item.model_dump(), name=name, sha256=sha, type="analysis")
            return result

        return key, calculate

    return submit(root, item.model_dump_json(), prepare)


@router.post("/preview", status_code=202)
def preview(request: Request, item: Inspect):
    root = workspace()
    _, report = record(root, item.analysis)
    try:
        clip = report["groups"][item.resolution]["candidates"][item.candidate]
    except (IndexError, KeyError) as exc:
        raise HTTPException(422, "Candidato indisponível.") from exc
    path, _, _ = source(request, Source(**report["source"]))

    def prepare(task):
        if media.digest(path, task.check) != report["sha256"]:
            raise media.MotionError("A fonte mudou. Execute uma nova análise.")
        return media.fingerprint([VERSION, item.model_dump()]), lambda folder: circulo.preview(
            path, clip, folder, task.progress, task.check
        )

    return submit(root, item.model_dump_json(), prepare)


def task_for(ident):
    task = tasks.get(ident)
    if not task or task.root != workspace():
        raise HTTPException(404, "Operação indisponível neste projeto.")
    return task


@router.get("/jobs/{ident}")
def status(ident: str):
    with lock:
        return task_for(ident).public()


@router.post("/jobs/{ident}/cancel")
def cancel(ident: str):
    with lock:
        task = task_for(ident)
        task.stop.set()
        return task.public()


@router.get("/result/{key}")
def report(key: AssetId):
    return record(workspace(), key)[1]


@router.get("/result/{key}/{name}")
def frame(key: AssetId, name: str):
    folder, _ = record(workspace(), key)
    if not re.fullmatch(r"[0-9]{2}\.webp", name) or not (folder / name).is_file():
        raise HTTPException(404, "Quadro indisponível.")
    return FileResponse(folder / name, media_type="image/webp")


class Synthesis(Source):
    axis: Literal["horizontal", "vertical"] = "horizontal"
    resolution: int = Field(ge=0, le=2)
    rate: float = Field(ge=0.5, le=2)


@router.post("/genesis", status_code=202)
def synthesize(request: Request, item: Synthesis):
    from . import genesis

    root = workspace()
    path, duration, _ = source(request, item)

    def prepare(task):
        key = media.fingerprint(["genesis-1", media.digest(path, task.check), item.model_dump()])
        return key, lambda folder: genesis.render(
            path,
            duration,
            folder,
            genesis.ANIMATED_SIZES[item.resolution],
            item.axis,
            item.rate,
            task.progress,
            task.check,
        )

    return submit(root, item.model_dump_json(), prepare)


@router.get("/genesis/{key}/{artifact}")
def synthesis_artifact(key: AssetId, artifact: Literal["source.png", "background.raf.zip"]):
    folder, data = record(workspace(), key)
    if data.get("type") != "genesis" or not (folder / artifact).is_file():
        raise HTTPException(404, "Derivado indisponível.")
    return FileResponse(folder / artifact, filename=artifact if artifact.endswith(".zip") else None)


@router.get("/genesis-source/{kind}/{ident}")
def genesis_source(request: Request, kind: Literal["animated", "library"], ident: AssetId | JobId):
    path, _, _ = source(request, Source(kind=kind, id=ident))
    mime = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}.get(path.suffix.lower())
    if not mime:
        raise HTTPException(415, "Use o quadro de referência para comparar este formato.")
    return FileResponse(path, media_type=mime, headers={"X-Content-Type-Options": "nosniff"})
