"""Private, bounded CPU jobs and immutable loop artifacts for one Linux Uvicorn worker."""

import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from . import compositor, jobs, motion_pipeline as engine
from .composition_model import Model, Surface
from .motion_model import AssetId, Build, JobId, Scan

router = APIRouter(prefix="/api/motion")
logger = logging.getLogger(__name__)
pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="loop")
lock = threading.RLock()


@dataclass
class Task:
    root: Path
    signature: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "working"
    stage: str = "Preparando a mídia"
    percent: int = 0
    key: str | None = None
    finished: float = 0.0
    cancel: threading.Event = field(default_factory=threading.Event)

    def check(self):
        if self.cancel.is_set():
            raise engine.Cancelled()

    def update(self, stage, percent):
        self.check()
        with lock:
            self.stage, self.percent = stage, percent

    def public(self):
        return {
            "id": self.id,
            "state": self.state,
            "stage": self.stage,
            "percent": self.percent,
            "key": self.key,
        }


tasks: dict[str, Task] = {}


def source(job_id):
    job = jobs.load("animated", job_id)
    if job is None:
        raise HTTPException(422, "Selecione uma mídia animada disponível neste projeto.")
    return job


def record(root, key, kind=None):
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise HTTPException(404, "Resultado não encontrado neste projeto.")
    directory = root / "motion-cache" / key
    try:
        data = json.loads((directory / "report.json").read_bytes())
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Resultado indisponível. Gere a prévia novamente.") from exc
    if kind and data["type"] != kind:
        raise HTTPException(422, "O derivado não corresponde à operação escolhida.")
    return directory, data


def complete(folder):
    """Read the inventory before reusing a cache entry, including after restart."""
    try:
        data = json.loads((folder / "report.json").read_bytes())
        inventory = data["files"]
        intact = bool(inventory) and all(
            (folder / name).stat().st_size == size for name, size in inventory.items()
        )
        if not intact or data["type"] != "render":
            return intact
        return engine.digest(folder / "background.raf.zip", lambda: None) == data["zipSha256"] and all(
            engine.digest(folder / "background.raf" / name, lambda: None) == checksum
            for name, checksum in data["sceneHashes"].items()
        )
    except (KeyError, ValueError, OSError):
        return False


def validate_saved(root, state):
    if not state.motion:
        return
    m = state.motion
    if not state.anim or m.job != state.anim.jobId:
        raise HTTPException(422, "O loop precisa da fonte animada deste projeto.")
    for key, kind in ((m.analysis, "analysis"), (m.rendered, "render"), (m.applied, "render")):
        if key:
            _, data = record(root, key, kind)
            if data["job"] != m.job or key == m.applied and not data.get("eligible"):
                raise HTTPException(422, "A referência do loop não corresponde à fonte ou à validação.")


def copy_saved(source_root, destination, state):
    if state.motion:
        validate_saved(source_root, state)
        for key in {state.motion.analysis, state.motion.rendered, state.motion.applied}:
            if key:
                # Completed directories are immutable. New edits get a new key.
                shutil.copytree(
                    source_root / "motion-cache" / key,
                    destination / "motion-cache" / key,
                    copy_function=os.link,
                )


def applied(job, raw_key, composition):
    if raw_key == "off":
        return None
    root = jobs.project_workspace.get()
    if raw_key is None and root is not None:
        from .projects import read

        state = read(root.parent).state
        raw_key = state.motion.applied if state and state.motion else None
    if not raw_key:
        return None
    folder, data = record(compositor.workspace(), raw_key, "render")
    surface = composition.surface("animated") if composition else None
    if not data["eligible"] or data["job"] != job.id or data["surfaceKey"] != engine.surface_key(surface):
        raise HTTPException(409, "A fonte ou as camadas mudaram. Renderize e aplique o loop novamente.")
    if not complete(folder):
        raise HTTPException(409, "Os arquivos do loop estão incompletos. Renderize o trecho novamente.")
    return folder, data


def prune(root, current):
    from .projects import read

    cache = root / "motion-cache"
    state = read(root).state
    keep = {current}
    if state and state.motion:
        keep.update((state.motion.analysis, state.motion.rendered, state.motion.applied))
    # Protect recent independent drafts and the saved result, including when
    # another tab is inspecting a result that it hasn't applied yet.
    dirs = sorted(
        (p for p in cache.iterdir() if re.fullmatch(r"[a-f0-9]{64}", p.name)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for index, old in enumerate(dirs[6:], start=6):
        if old.name not in keep and (index >= 10 or time.time() - old.stat().st_mtime > 3600):
            shutil.rmtree(old)


def run(task, job, payload, kind):
    from .projects import revision_lock

    started = time.monotonic()
    try:
        source_digest = engine.digest(job.source, task.check)
        key = engine.fingerprint([engine.VERSION, kind, source_digest, payload.model_dump()])
        cache = task.root / "motion-cache"
        cache.mkdir(exist_ok=True)
        for old in cache.glob(".work-*"):
            if old.is_dir() and time.time() - old.stat().st_mtime > 3600:
                shutil.rmtree(old, ignore_errors=True)
        dest = cache / key
        if not complete(dest):
            with tempfile.TemporaryDirectory(dir=cache, prefix=".work-") as temporary:
                stage = Path(temporary) / "result"
                stage.mkdir()
                calculate = engine.search if kind == "analysis" else engine.build
                result = calculate(job, payload, stage, task.update, task.check)
                result.update(
                    {
                        "version": engine.VERSION,
                        "job": job.id,
                        "key": key,
                        "sourceSha256": source_digest,
                        "files": {
                            p.relative_to(stage).as_posix(): p.stat().st_size
                            for p in stage.rglob("*")
                            if p.is_file()
                        },
                        "seconds": round(time.monotonic() - started, 3),
                    }
                )
                (stage / "report.json").write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
                with revision_lock(task.root), lock:
                    task.check()
                    if not dest.exists():
                        stage.rename(dest)
                    elif not complete(dest):
                        # Same immutable key. Keep serving any intact members
                        # while atomically restoring missing/truncated files.
                        for name in [*result["files"], "report.json"]:
                            path = dest / name
                            path.parent.mkdir(parents=True, exist_ok=True)
                            (stage / name).replace(path)
                    prune(task.root, key)
        with lock:
            task.check()
            task.state, task.stage, task.percent, task.key = "ready", "Concluído", 100, key
    except engine.Cancelled:
        with lock:
            task.state, task.stage = "cancelled", "Processamento cancelado."
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, engine.MotionError)
            else "Falha ao processar o trecho. Verifique a fonte e o espaço disponível."
        )
        with lock:
            task.stage, task.state = message, "error"
        logger.warning(
            "motion_failure project=%s task=%s operation=%s exception=%s",
            task.root.name,
            task.id,
            kind,
            type(exc).__name__,
        )
    finally:
        with lock:
            task.finished = time.monotonic()
        logger.info(
            "motion_job project=%s task=%s operation=%s status=%s seconds=%.3f",
            task.root.name,
            task.id,
            kind,
            task.state,
            time.monotonic() - started,
        )


def enqueue(payload, kind):
    root = compositor.workspace()
    job = source(payload.job)
    if kind == "render" and payload.clip.end > job.meta["duration"] + 0.000001:
        raise HTTPException(422, "A saída ultrapassa a duração da fonte.")
    signature = engine.fingerprint([kind, payload.model_dump()])
    with lock:
        for ident, item in list(tasks.items()):
            if item.finished and time.monotonic() - item.finished > 600:
                del tasks[ident]
        running = [t for t in tasks.values() if t.state == "working"]
        for t in running:
            if t.root == root:
                if t.signature == signature and not t.cancel.is_set():
                    return t.public()
                raise HTTPException(409, "Aguarde ou cancele a operação atual deste projeto.")
        if len(running) >= 2:
            raise HTTPException(429, "Os dois processadores estão ocupados. Tente novamente em instantes.")
        task = Task(root, signature)
        tasks[task.id] = task
        pool.submit(copy_context().run, run, task, job, payload, kind)
        return task.public()


@router.post("/analyze", status_code=202)
def analyze(payload: Scan):
    return enqueue(payload, "analysis")


@router.post("/render", status_code=202)
def render(payload: Build):
    return enqueue(payload, "render")


def scoped_task(ident):
    task = tasks.get(ident)
    if not task or task.root != compositor.workspace():
        raise HTTPException(404, "Operação não encontrada. Após reiniciar, execute novamente.")
    return task


@router.get("/jobs/{ident}")
def status(ident: str):
    with lock:
        return scoped_task(ident).public()


@router.post("/jobs/{ident}/cancel")
def cancel(ident: str):
    with lock:
        task = scoped_task(ident)
        task.cancel.set()
        return task.public()


@router.get("/original/{ident}")
def original(ident: JobId):
    job = source(ident)
    mime = mimetypes.guess_type(job.source.name)[0]
    safe = {"video/mp4", "video/webm", "video/quicktime", "image/gif", "image/webp", "image/apng"}
    return FileResponse(
        job.source,
        media_type=mime if mime in safe else "application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


@router.get("/results/{key}")
def result(key: AssetId):
    return record(compositor.workspace(), key)[1]


class AppliedView(Model):
    job: JobId
    key: AssetId
    surface: Surface | None = None


@router.post("/view")
def view(payload: AppliedView):
    from .composition_model import Composition

    comp = Composition(surfaces={"animated": payload.surface}) if payload.surface else None
    return applied(source(payload.job), payload.key, comp)[1]


@router.get("/results/{key}/{name}")
def artifact(key: AssetId, name: str):
    folder, info = record(compositor.workspace(), key)
    if name == "background.raf.zip" and info.get("eligible"):
        if not complete(folder):
            raise HTTPException(409, "O arquivo do loop precisa ser gerado novamente.")
        return FileResponse(folder / name, media_type="application/zip", filename=name)
    if name == "base.webp" and info["type"] == "render":
        return FileResponse(folder / name, media_type="image/webp")
    if not re.fullmatch(r"(?:raf|cut|opt|thumb)-[0-9]{2}\.webp", name) or not (folder / name).is_file():
        raise HTTPException(404, "Arquivo de prévia indisponível.")
    return FileResponse(folder / name, media_type="image/webp")
