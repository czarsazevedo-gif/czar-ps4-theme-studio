"""Unified OUROBOROS FastAPI router — single loop optimization & 3-proposal engine."""

from __future__ import annotations

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
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field

from . import jobs, library, projects
from .composition_model import AssetId, Model
from .ouroboros_engine import OUROBOROS_VERSION, OuroborosEngine, media_io as engine_media
from .ouroboros_engine.regions import RegionalRecipe, list_recipes, load_recipe, save_recipe
from .projects import JobId

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ouroboros")
pool = ThreadPoolExecutor(max_workers=3)
lock = Lock()
VERSION = OUROBOROS_VERSION
_ENGINE = OuroborosEngine()


class Source(Model):
    kind: Literal["library", "animated"]
    id: AssetId | JobId


class UnifiedRunRequest(Model):
    kind: Literal["library", "animated"]
    id: AssetId | JobId
    stabilizeCamera: bool = False
    recipeId: str | None = None
    recipe: RegionalRecipe | None = None
    forceNew: bool = False


class UnifiedApplyRequest(Model):
    key: AssetId
    profileId: Literal["quality", "balance", "flexibility"]
    expectedRenderKey: AssetId | None = None
    displayName: str | None = Field(default=None, max_length=120)


@dataclass
class Task:
    root: Path
    signature: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    stop: Event = field(default_factory=Event)
    state: str = "working"
    stage: str = "Preparando Ouroboros"
    percent: int = 0
    key: str | None = None
    finished: float = 0.0

    def check(self) -> None:
        if self.stop.is_set():
            raise engine_media.Cancelled()

    def progress(self, stage: str, percent: int) -> None:
        self.check()
        with lock:
            self.stage, self.percent = stage, percent

    def public(self) -> dict:
        return dict(
            id=self.id,
            state=self.state,
            stage=self.stage,
            percent=self.percent,
            key=self.key,
        )


tasks: dict[str, Task] = {}


def workspace() -> Path:
    root = jobs.project_workspace.get()
    if not root:
        raise HTTPException(400, "Abra um projeto para executar o Ouroboros.")
    return root.parent


def source(request: Request, selection: Source) -> tuple[Path, float, str]:
    if selection.kind == "library":
        asset, path = library.get_asset(request, selection.id)
        if asset.kind in {"video", "animation"} and asset.duration:
            return path, asset.duration, asset.originalName
    else:
        job = jobs.load("animated", selection.id)
        if job and job.meta.get("duration"):
            return job.source, job.meta["duration"], job.original_name
    raise HTTPException(422, "Selecione um vídeo ou GIF disponível no projeto ou na biblioteca.")


def record(root: Path, key: str) -> tuple[Path, dict]:
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise HTTPException(404, "Resultado indisponível.")
    folder = root / "ouroboros-v1" / key
    try:
        return folder, json.loads((folder / "report.json").read_bytes())
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Resultado indisponível neste projeto.") from exc


def submit(root: Path, signature: str, calculate, *, preempt: bool = True, force_new: bool = False) -> dict:
    with lock:
        for ident, t in list(tasks.items()):
            if t.finished and time.monotonic() - t.finished > 600:
                del tasks[ident]
        running = [t for t in tasks.values() if t.state == "working"]
        active_other = 0
        for task in running:
            if task.root == root:
                if not force_new and task.signature == signature and not task.stop.is_set():
                    return task.public()
                if preempt:
                    task.stop.set()
                    continue
                raise HTTPException(409, "Aguarde ou cancele a operação deste projeto.")
            elif not task.stop.is_set():
                active_other += 1
        if active_other >= 2:
            raise HTTPException(429, "Os processadores estão ocupados.")
        task = Task(root, signature)
        tasks[task.id] = task
        pool.submit(execute, task, calculate)
        return task.public()


def healthy(folder: Path, key: str, check) -> bool:
    try:
        header = json.loads((folder / "report.json").read_bytes())
        if header.get("key") != key or header.get("version") != OUROBOROS_VERSION:
            return False
        if header.get("type") != "ouroboros_proposals":
            return False
        proposals = header.get("proposals")
        if not isinstance(proposals, list) or len(proposals) != 3:
            return False
        seen_profiles = set()
        for prop in proposals:
            pid = prop.get("profileId")
            if pid not in {"quality", "balance", "flexibility"}:
                return False
            seen_profiles.add(pid)
            count = int(prop["frames"])
            if not 4 <= count <= 48:
                return False
            prof_dir = folder / pid
            files_map = prop.get("files") or {}
            expected_names = [f"{i:02d}.webp" for i in range(count)] + [
                "source.png",
                "diff.png",
                "background.raf.zip",
            ]
            for i in range(count):
                if not (prof_dir / "deluxe_dds" / f"{i:02d}.dds").is_file():
                    return False
                if not (prof_dir / "deluxe_frames" / f"{i:02d}.png").is_file():
                    return False
            for name in expected_names:
                check()
                if engine_media.digest(prof_dir / name, check) != files_map.get(name):
                    return False
        return seen_profiles == {"quality", "balance", "flexibility"}
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _replace_tree_files(src_dir: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        target = dst_dir / item.name
        if item.is_dir():
            _replace_tree_files(item, target)
        else:
            item.replace(target)


def execute(task: Task, calculate) -> None:
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
                data["key"] = key
                data["version"] = OUROBOROS_VERSION
                (stage / "report.json").write_text(json.dumps(data, allow_nan=False), encoding="utf-8")
                with lock:
                    task.check()
                    if not dest.exists():
                        stage.rename(dest)
                    else:
                        for pid in ("quality", "balance", "flexibility"):
                            if (stage / pid).is_dir():
                                _replace_tree_files(stage / pid, dest / pid)
                        (stage / "report.json").replace(dest / "report.json")
        with lock:
            task.check()
            task.state, task.stage, task.percent, task.key = (
                "ready",
                "Concluído",
                100,
                key,
            )
    except engine_media.Cancelled:
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
                if isinstance(exc, engine_media.MotionError)
                else "Falha ao processar no Ouroboros. Confira a fonte e o espaço disponível."
            )
    finally:
        with lock:
            task.finished = time.monotonic()


def task_for(ident: str) -> Task:
    task = tasks.get(ident)
    if not task or task.root != workspace():
        raise HTTPException(404, "Operação indisponível neste projeto.")
    return task


@router.get("/current")
def current():
    root = workspace()
    state = projects.read(root).state
    job = jobs.load("animated", state.anim.jobId) if state and state.anim and state.anim.jobId else None

    active_task = None
    with lock:
        for t in tasks.values():
            if t.root == root and t.state == "working" and not t.stop.is_set():
                active_task = t.public()
                break

    cached_key = None
    cache_dir = root / "ouroboros-v1"
    if cache_dir.is_dir():
        candidates = sorted(
            (p for p in cache_dir.iterdir() if p.is_dir() and re.fullmatch(r"[a-f0-9]{64}", p.name) and (p / "report.json").is_file()),
            key=lambda p: (p / "report.json").stat().st_mtime,
            reverse=True,
        )
        for cand in candidates:
            try:
                rep = json.loads((cand / "report.json").read_bytes())
                if rep.get("type") == "ouroboros_proposals" and rep.get("version") == OUROBOROS_VERSION:
                    cached_key = cand.name
                    if job and (rep.get("source") or {}).get("id") == job.id:
                        break
            except Exception:
                continue

    if not job and not active_task and not cached_key:
        return None

    deluxe_meta = (job.meta.get("deluxe") if job and isinstance(job.meta, dict) else None) or {}
    return {
        "id": job.id if job else None,
        "name": job.original_name if job else None,
        "activeTask": active_task,
        "cachedResultKey": cached_key,
        "appliedRenderKey": deluxe_meta.get("renderKey") or deluxe_meta.get("resultKey"),
        "appliedProfileId": deluxe_meta.get("profileId"),
    }


@router.post("/run", status_code=202)
def unified_run(request: Request, item: UnifiedRunRequest):
    """Single OUROBOROS entrypoint: inspect -> analyze -> search -> 3 independent PS4 proposals (720p, 540p, 360p)."""
    root = workspace()
    path, duration, name = source(request, Source(kind=item.kind, id=item.id))

    resolved_recipe = item.recipe
    if resolved_recipe is None and item.recipeId:
        try:
            resolved_recipe = load_recipe(root, item.recipeId)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "Receita regional não encontrada neste projeto.") from exc

    sig_obj = {
        "kind": item.kind,
        "id": item.id,
        "stabilizeCamera": item.stabilizeCamera,
        "recipeId": item.recipeId,
        "recipe": resolved_recipe.model_dump() if resolved_recipe else None,
    }

    def prepare(task: Task):
        sha = engine_media.digest(path, task.check)
        sig_payload = {
            "kind": item.kind,
            "id": item.id,
            "stabilizeCamera": item.stabilizeCamera,
            "recipe": resolved_recipe.model_dump() if resolved_recipe else None,
        }
        key = engine_media.fingerprint([OUROBOROS_VERSION, "proposals", sha, sig_payload])

        def calculate(folder: Path):
            res = _ENGINE.generate_three_proposals(
                path,
                duration,
                folder,
                filename=name,
                source_sha256=sha,
                recipe=resolved_recipe,
                stabilize_camera=item.stabilizeCamera,
                progress=task.progress,
                check=task.check,
            )
            res["source"] = {"kind": item.kind, "id": item.id}
            return res

        return key, calculate

    return submit(
        root,
        "ouroboros-run:" + json.dumps(sig_obj, sort_keys=True),
        prepare,
        preempt=True,
        force_new=item.forceNew,
    )


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
def report(key: AssetId, full: bool = False):
    data = record(workspace(), key)[1]
    if not full and isinstance(data, dict) and "profileReports" in data:
        slim = dict(data)
        slim.pop("profileReports", None)
        return slim
    return data


@router.get("/artifact/{key}/{profile}/{name}")
def unified_artifact(key: AssetId, profile: Literal["quality", "balance", "flexibility"], name: str):
    folder, data = record(workspace(), key)
    if data.get("type") != "ouroboros_proposals":
        raise HTTPException(404, "Artefato Ouroboros indisponível.")
    prof_dir = folder / profile
    if name in {"source.png", "diff.png", "background.raf.zip"}:
        target = prof_dir / name
        if not target.is_file():
            raise HTTPException(404, "Arquivo não encontrado.")
        media_type = "application/zip" if name.endswith(".zip") else "image/png"
        return FileResponse(target, media_type=media_type, filename=name if name.endswith(".zip") else None)
    if re.fullmatch(r"[0-9]{2}\.webp", name):
        target = prof_dir / name
        if not target.is_file():
            raise HTTPException(404, "Quadro não encontrado.")
        return FileResponse(target, media_type="image/webp")
    raise HTTPException(404, "Artefato inválido.")


@router.post("/apply")
def unified_apply(item: UnifiedApplyRequest):
    """Apply one of the 3 verified Ouroboros proposals directly to the active project with strict render identity."""
    root = workspace()
    folder, data = record(root, item.key)
    if data.get("type") != "ouroboros_proposals":
        raise HTTPException(422, "Execute o Ouroboros antes de aplicar uma proposta ao tema.")

    proposals = data.get("proposals") or []
    prop = next((p for p in proposals if p.get("profileId") == item.profileId), None)
    if prop is None:
        raise HTTPException(404, f"Proposta '{item.profileId}' indisponível neste resultado.")

    if item.expectedRenderKey and prop.get("renderKey") != item.expectedRenderKey:
        raise HTTPException(
            409,
            "A identidade da proposta selecionada não confere com o resultado renderizado. Atualize as propostas antes de aplicar.",
        )

    prof_dir = folder / item.profileId
    prof_report = (data.get("profileReports") or {}).get(item.profileId) or prop
    prof_report = dict(prof_report)
    prof_report.setdefault("profileId", item.profileId)
    prof_report.setdefault("profileTitle", prop.get("title"))
    prof_report.setdefault("renderKey", prop.get("renderKey"))
    prof_report.setdefault("key", prop.get("renderKey"))
    try:
        return _ENGINE.apply_to_project(
            root, prof_dir, prof_report, display_name=item.displayName, acquire_lock=False
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, "Falha ao gravar no projeto. A versão anterior foi preservada.") from exc


@router.get("/recipes")
def unified_list_recipes():
    return {"recipes": list_recipes(workspace())}


@router.get("/recipes/{recipe_id}")
def unified_get_recipe(recipe_id: str):
    try:
        return load_recipe(workspace(), recipe_id).model_dump()
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Receita regional não encontrada.") from exc


@router.put("/recipes/{recipe_id}")
@router.post("/recipes/{recipe_id}")
def unified_save_recipe(recipe_id: str, recipe: RegionalRecipe):
    try:
        save_recipe(workspace(), recipe_id, recipe)
        return {"saved": True, "id": recipe_id}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
