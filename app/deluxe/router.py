"""Additive FastAPI router for Ouroboros Cinematic Deluxe under /api/ouroboros/deluxe."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field

from .. import ouroboros_media as io_media
from ..composition_model import AssetId, Model
from ..config import ANIMATED_SIZES, MAX_WAIT_SECONDS, MIN_WAIT_SECONDS
from ..projects import JobId
from . import DELUXE_VERSION
from .calibrator import calibrate_interval
from .circulo_supreme import search_supreme
from .media_diagnostic import build_media_diagnostic
from .pipeline import RenderMode, apply_deluxe_to_project, render_deluxe_loop
from .regions import RegionalRecipe, list_recipes, load_recipe, save_recipe

router = APIRouter(prefix="/deluxe")


class DeluxeSource(Model):
    kind: Literal["library", "animated"]
    id: AssetId | JobId


class CalibrateRequest(DeluxeSource):
    start: float = Field(ge=0.0)
    end: float = Field(gt=0.0)
    verifyDds: bool = False


class RenderRequest(DeluxeSource):
    start: float = Field(ge=0.0)
    end: float = Field(gt=0.0)
    frames: int = Field(ge=4, le=48)
    resolution: int = Field(ge=0, le=2)
    wait: float = Field(ge=MIN_WAIT_SECONDS, le=MAX_WAIT_SECONDS)
    mode: RenderMode = "reconstruct_auto"
    deluxeFinish: bool = True
    stabilizeCamera: bool = False
    speedScale: float = Field(default=1.0, ge=0.25, le=2.5)
    recipeId: str | None = None
    recipe: RegionalRecipe | None = None


class ApplyRequest(Model):
    key: AssetId
    displayName: str | None = Field(default=None, max_length=120)
    expected: RenderRequest | None = None


def _render_fingerprint(sha: str, item: RenderRequest, resolved_recipe: RegionalRecipe | None) -> str:
    sig_payload = item.model_dump()
    sig_payload["resolvedRecipe"] = resolved_recipe.model_dump() if resolved_recipe else None
    return io_media.fingerprint([DELUXE_VERSION, "render", sha, sig_payload])


def _submit_deluxe_analysis(request: Request, item: DeluxeSource):
    """Run Sentinela + Círculo Supreme + 21-field Media Diagnostic under one shared cache key."""
    from .. import ouroboros as core

    root = core.workspace()
    path, duration, name = core.source(request, core.Source(kind=item.kind, id=item.id))

    def prepare(task):
        sha = io_media.digest(path, task.check)
        key = io_media.fingerprint([DELUXE_VERSION, "analyze_v2", sha, item.model_dump()])

        def calculate(folder: Path):
            result = search_supreme(
                path,
                duration,
                folder,
                source_sha256=sha,
                progress=task.progress,
                check=task.check,
            )
            result.update(
                type="deluxe_analysis",
                source=item.model_dump(),
                name=name,
                sha256=sha,
            )
            result["mediaDiagnostic"] = build_media_diagnostic(result, filename=name)
            return result

        return key, calculate

    return core.submit(root, "deluxe-analyze:" + item.model_dump_json(), prepare)


@router.post("/analyze", status_code=202)
def deluxe_analyze(request: Request, item: DeluxeSource):
    return _submit_deluxe_analysis(request, item)


@router.post("/diagnose", status_code=202)
def deluxe_diagnose(request: Request, item: DeluxeSource):
    """Non-blocking, cancellable automatic post-import media diagnostic sharing the analysis cache."""
    return _submit_deluxe_analysis(request, item)


@router.post("/calibrate", status_code=202)
def deluxe_calibrate(request: Request, item: CalibrateRequest):
    from .. import ouroboros as core

    root = core.workspace()
    path, duration, name = core.source(request, core.Source(kind=item.kind, id=item.id))
    if item.end <= item.start + 0.05:
        raise HTTPException(422, "Intervalo de calibração inválido.")

    def prepare(task):
        sha = io_media.digest(path, task.check)
        key = io_media.fingerprint([DELUXE_VERSION, "calibrate", sha, item.model_dump()])

        def calculate(folder: Path):
            info = io_media.source_info(path, duration, task.check)
            fps = info["sampleFps"] or info["fps"] or 10.0
            task.progress("Calibrando cadência temporal nas resoluções PS4", 35)
            res = calibrate_interval(
                path,
                item.start,
                min(item.end, info["duration"]),
                source_fps=fps,
                folder=folder,
                verify_dds=item.verifyDds,
                check=task.check,
            )
            res.update(type="deluxe_calibrate", source=item.model_dump(), name=name, sha256=sha)
            return res

        return key, calculate

    return core.submit(root, "deluxe-calibrate:" + item.model_dump_json(), prepare)


@router.post("/render", status_code=202)
def deluxe_render(request: Request, item: RenderRequest):
    from .. import ouroboros as core

    root = core.workspace()
    path, duration, name = core.source(request, core.Source(kind=item.kind, id=item.id))
    if item.end <= item.start + 0.05:
        raise HTTPException(422, "Intervalo de renderização inválido.")

    resolved_recipe = item.recipe
    if resolved_recipe is None and item.recipeId:
        try:
            resolved_recipe = load_recipe(root, item.recipeId)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "Receita regional não encontrada neste projeto.") from exc

    size = ANIMATED_SIZES[item.resolution]

    def prepare(task):
        sha = io_media.digest(path, task.check)
        key = _render_fingerprint(sha, item, resolved_recipe)

        def calculate(folder: Path):
            res = render_deluxe_loop(
                path,
                duration,
                folder,
                start=item.start,
                end=item.end,
                frames=item.frames,
                size=size,
                wait=item.wait,
                mode=item.mode,
                recipe=resolved_recipe,
                deluxe_finish=item.deluxeFinish,
                stabilize_camera=item.stabilizeCamera,
                speed_scale=item.speedScale,
                source_sha256=sha,
                progress=task.progress,
                check=task.check,
            )
            res.update(key=key, source=item.model_dump(), name=name, sha256=sha)
            return res

        return key, calculate

    return core.submit(root, "deluxe-render:" + item.model_dump_json(), prepare)


@router.post("/apply")
def deluxe_apply(request: Request, item: ApplyRequest):
    from .. import ouroboros as core

    root = core.workspace()
    folder, report = core.record(root, item.key)
    if report.get("type") != "deluxe_render":
        raise HTTPException(422, "Renderize o resultado Deluxe antes de aplicar ao projeto.")

    # CZAR-AUD-001: verify that the rendered key actually matches the active selection when provided.
    if item.expected is not None:
        exp = item.expected
        path, _duration, _name = core.source(request, core.Source(kind=exp.kind, id=exp.id))
        resolved_recipe = exp.recipe
        if resolved_recipe is None and exp.recipeId:
            try:
                resolved_recipe = load_recipe(root, exp.recipeId)
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(404, "Receita regional não encontrada neste projeto.") from exc
        sha = io_media.digest(path, lambda: None)
        expected_key = _render_fingerprint(sha, exp, resolved_recipe)
        expected_size = list(ANIMATED_SIZES[exp.resolution])
        if (
            expected_key != item.key
            or int(report.get("frames", -1)) != exp.frames
            or list(report.get("size") or []) != expected_size
            or abs(float(report.get("wait", -1.0)) - float(exp.wait)) > 1e-4
        ):
            raise HTTPException(
                409,
                "A seleção atual não corresponde ao take renderizado. Renderize esta configuração antes de aplicar ao projeto.",
            )

    report_with_key = dict(report)
    report_with_key.setdefault("key", item.key)
    try:
        return apply_deluxe_to_project(
            root, folder, report_with_key, display_name=item.displayName, acquire_lock=False
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, "Falha ao gravar no projeto. A versão anterior foi preservada.") from exc


@router.get("/artifact/{key}/{name}")
def deluxe_artifact(key: AssetId, name: str):
    from .. import ouroboros as core

    folder, data = core.record(core.workspace(), key)
    if data.get("type") != "deluxe_render":
        raise HTTPException(404, "Artefato Deluxe indisponível.")
    if name in {"source.png", "diff.png", "background.raf.zip"}:
        target = folder / name
        if not target.is_file():
            raise HTTPException(404, "Arquivo não encontrado.")
        media_type = "application/zip" if name.endswith(".zip") else "image/png"
        return FileResponse(target, media_type=media_type, filename=name if name.endswith(".zip") else None)
    if re.fullmatch(r"[0-9]{2}\.webp", name):
        target = folder / name
        if not target.is_file():
            raise HTTPException(404, "Quadro indisponível.")
        return FileResponse(target, media_type="image/webp")
    raise HTTPException(404, "Artefato inválido.")


@router.get("/recipes")
def deluxe_list_recipes():
    from .. import ouroboros as core

    return {"recipes": list_recipes(core.workspace())}


@router.get("/recipes/{recipe_id}")
def deluxe_get_recipe(recipe_id: str):
    from .. import ouroboros as core

    try:
        return load_recipe(core.workspace(), recipe_id).model_dump()
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Receita regional indisponível.") from exc


@router.put("/recipes/{recipe_id}")
def deluxe_save_recipe(recipe_id: str, recipe: RegionalRecipe):
    from .. import ouroboros as core

    try:
        save_recipe(core.workspace(), recipe_id, recipe)
        return {"saved": True, "id": recipe_id, "regionCount": len(recipe.regions)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, "Não foi possível salvar a receita no disco.") from exc
