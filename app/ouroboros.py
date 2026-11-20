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

from .ouroboros_engine import OUROBOROS_VERSION, OuroborosEngine, media_io as engine_media  # noqa: E402
from .ouroboros_engine.regions import RegionalRecipe, list_recipes, load_recipe, save_recipe  # noqa: E402

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ouroboros")
pool = ThreadPoolExecutor(max_workers=3)
lock = Lock()
VERSION = "ouroboros-foundation-1"
_ENGINE = OuroborosEngine()


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
            raise engine_media.Cancelled()

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


def submit(root, signature, calculate, *, preempt: bool = False):
    with lock:
        for ident, t in list(tasks.items()):
            if t.finished and time.monotonic() - t.finished > 600:
                del tasks[ident]
        running = [t for t in tasks.values() if t.state == "working"]
        active_other = 0
        for task in running:
            if task.root == root:
                if task.signature == signature and not task.stop.is_set():
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


def healthy(folder, key, check):
    try:
        header = json.loads((folder / "report.json").read_bytes())
        kind = header.get("type")
        if header["key"] != key:
            return False
        if kind == "ouroboros_proposals":
            if header.get("version") != OUROBOROS_VERSION:
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
        if header.get("version") != VERSION:
            return False
        if kind == "analysis":
            return len(header["groups"]) == 3 and "sentinela" in header
        if kind == "deluxe_analysis":
            return (
                len(header["groups"]) == 3
                and "sentinela" in header
                and "categories" in header
                and "mediaDiagnostic" in header
            )
        if kind == "deluxe_calibrate":
            return "recommendations" in header and "configurations" in header
        count = header["frames"]
        if not 1 <= count <= 48:
            return False
        names = [f"{i:02d}.webp" for i in range(count)]
        if kind == "genesis":
            names.extend(["source.png", "background.raf.zip"])
        elif kind == "deluxe_render":
            names.extend(["source.png", "diff.png", "background.raf.zip"])
            for i in range(count):
                if not (folder / "deluxe_dds" / f"{i:02d}.dds").is_file():
                    return False
                if not (folder / "deluxe_frames" / f"{i:02d}.png").is_file():
                    return False
        for name in names:
            check()
            if engine_media.digest(folder / name, check) != header["files"][name]:
                return False
        return True
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
                data["key"] = key
                if data.get("type") == "ouroboros_proposals":
                    data["version"] = OUROBOROS_VERSION
                else:
                    data["version"] = VERSION
                    if "frames" in data:
                        data["files"] = {p.name: engine_media.digest(p, task.check) for p in stage.glob("*.webp")}
                    if data["type"] == "genesis":
                        for name in ("source.png", "background.raf.zip"):
                            data["files"][name] = engine_media.digest(stage / name, task.check)
                    elif data["type"] == "deluxe_render":
                        for name in ("source.png", "diff.png", "background.raf.zip"):
                            data["files"][name] = engine_media.digest(stage / name, task.check)
                (stage / "report.json").write_text(json.dumps(data, allow_nan=False), encoding="utf-8")
                with lock:
                    task.check()
                    if not dest.exists():
                        stage.rename(dest)
                    else:
                        # Readers see complete files from the same immutable recipe.
                        if data.get("type") == "ouroboros_proposals":
                            for pid in ("quality", "balance", "flexibility"):
                                if (stage / pid).is_dir():
                                    _replace_tree_files(stage / pid, dest / pid)
                        else:
                            for name in data.get("files", {}):
                                (stage / name).replace(dest / name)
                            for sub in ("deluxe_dds", "deluxe_frames"):
                                if (stage / sub).is_dir():
                                    _replace_tree_files(stage / sub, dest / sub)
                        (stage / "report.json").replace(dest / "report.json")
        with lock:
            task.check()
            task.state, task.stage, task.percent, task.key = (
                "ready",
                "Concluído",
                100,
                key,
            )
    except (media.Cancelled, engine_media.Cancelled):
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
                if isinstance(exc, (media.MotionError, engine_media.MotionError))
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


class UnifiedRunRequest(Model):
    kind: Literal["library", "animated"]
    id: AssetId | JobId
    stabilizeCamera: bool = False
    recipeId: str | None = None
    recipe: RegionalRecipe | None = None


class UnifiedApplyRequest(Model):
    key: AssetId
    profileId: Literal["quality", "balance", "flexibility"]
    expectedRenderKey: AssetId | None = None
    displayName: str | None = Field(default=None, max_length=120)


@router.post("/run", status_code=202)
def unified_run(request: Request, item: UnifiedRunRequest):
    """Single Ouroboros entrypoint: inspect -> analyze -> search -> 3 independent PS4 proposals (720p, 540p, 360p)."""
    root = workspace()
    path, duration, name = source(request, Source(kind=item.kind, id=item.id))

    resolved_recipe = item.recipe
    if resolved_recipe is None and item.recipeId:
        try:
            resolved_recipe = load_recipe(root, item.recipeId)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "Receita regional não encontrada neste projeto.") from exc

    def prepare(task):
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

    return submit(root, "ouroboros-run:" + item.model_dump_json(), prepare, preempt=True)


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

    # Strict cryptographic render identity guarantee: NEVER apply Proposal A when user chose Proposal B!
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


@router.post("/recipes/{recipe_id}")
def unified_save_recipe(recipe_id: str, recipe: RegionalRecipe):
    try:
        save_recipe(workspace(), recipe_id, recipe)
        return {"saved": True, "id": recipe_id}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


from .deluxe.router import router as _deluxe_router  # noqa: E402

router.include_router(_deluxe_router)
