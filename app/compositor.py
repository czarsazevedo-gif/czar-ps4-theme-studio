"""Optional raster composition in front of canonical image and DDS encoders."""

from __future__ import annotations

import hashlib
import math
import re
import tempfile
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from PIL import Image
from pydantic import Field, ValidationError

from . import jobs, library, media
from .composition_model import AssetId, Composition, IMAGE_TARGETS, Layer, Model, Surface, TARGETS
from .config import ANIMATED_SIZES, IMAGE_EXTS, IMAGE_SLOTS, STATIC_SIZE, max_frames_for

router = APIRouter(prefix="/api/composition")
RASTER_EDGE = max(STATIC_SIZE)
VERSION = "raster-v1"


def workspace() -> Path:
    root = jobs.project_workspace.get()
    if root is None:
        raise HTTPException(400, "Abra um projeto para usar a composição.")
    return root.parent


def lookup(root: Path, ident: str):
    try:
        asset, source = library.read_asset(root.parent / "library-v1", ident)
    except HTTPException as exc:
        raise HTTPException(422, "Uma camada aponta para uma mídia ausente na biblioteca.") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(422, "O original de uma camada está incompleto ou inválido.") from exc
    if asset.extension not in IMAGE_EXTS or asset.kind not in {"image", "animation"}:
        raise HTTPException(422, "As camadas aceitam somente imagens raster da biblioteca.")
    return asset, source


def validate_references(root: Path, state: Composition | None) -> None:
    if state:
        for ident in {layer.asset for surface in state.surfaces.values() for layer in surface.layers}:
            lookup(root, ident)


def load_composition(raw: str | None) -> Composition | None:
    root = jobs.project_workspace.get()
    if raw is None:
        if root is None:
            return None
        from .projects import read

        try:
            state = read(root.parent).state
        except (ValueError, OSError) as exc:
            raise HTTPException(422, "O projeto não pôde ser lido para exportação.") from exc
        return state.composition if state else None
    workspace()
    try:
        return Composition.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(422, "Configuração de composição inválida.") from exc


def raster(root: Path, ident: str) -> Path:
    _, source = lookup(root, ident)
    cache = root / "composition-cache" / VERSION
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{ident}.png"
    if not target.is_file():
        if source.suffix.lower() == ".svg":
            with tempfile.TemporaryDirectory(dir=cache) as stage:
                output = Path(stage) / "raster.png"
                try:
                    media.render_image(source, output, (512, 512))
                except media.MediaError as exc:
                    raise HTTPException(422, str(exc)) from exc
                output.replace(target)
            return target
        if not media.FFMPEG:
            raise HTTPException(503, "FFmpeg indisponível no servidor.")
        with tempfile.TemporaryDirectory(dir=cache) as stage:
            output = Path(stage) / "raster.png"
            factor = f"min(1,{RASTER_EDGE}/max(iw,ih))"
            scale = f"scale=w='max(1,round(iw*{factor}))':h='max(1,round(ih*{factor}))',setsar=1"
            library.run_checked([
                media.FFMPEG, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file",
                "-i", str(source), "-map", "0:v:0", "-frames:v", "1", "-vf", scale,
                "-c:v", "png", "-threads:v", "1", "-pix_fmt", "rgba", str(output),
            ])
            if not output.is_file():
                raise HTTPException(422, "A imagem da camada não pôde ser decodificada.")
            output.replace(target)
    return target


def layer_pixels(source: Image.Image, layer: Layer, logical: tuple[int, int], output: tuple[int, int]):
    """Resample directly into output pixels, with no unbounded scaled intermediate."""
    iw, ih = source.size
    width, height = layer.width, layer.height
    if layer.fit == "contain":
        ratio = min(width / iw, height / ih)
        width, height = iw * ratio, ih * ratio
    elif layer.fit == "cover":
        if iw * height > ih * width:
            crop = min(iw, max(1, math.floor(ih * width / height + 0.5)))
            left = (iw - crop) // 2
            source = source.crop((left, 0, left + crop, ih))
        else:
            crop = min(ih, max(1, math.floor(iw * height / width + 0.5)))
            top = (ih - crop) // 2
            source = source.crop((0, top, iw, top + crop))
        iw, ih = source.size
    # Invert CSS-style clockwise rotation in logical coordinates, then map
    # local layer coordinates back into the source raster.
    theta = math.radians(layer.angle)
    c, s = math.cos(theta), math.sin(theta)
    sx, sy = logical[0] / output[0], logical[1] / output[1]
    ax, ay = iw / width, ih / height
    affine = (
        c * sx * ax, s * sy * ax, iw / 2 - (c * layer.x + s * layer.y) * ax,
        -s * sx * ay, c * sy * ay, ih / 2 - (-s * layer.x + c * layer.y) * ay,
    )
    image = source.transform(output, Image.Transform.AFFINE, affine, Image.Resampling.BICUBIC)
    if layer.opacity != 1:
        image.putalpha(image.getchannel("A").point(lambda alpha: round(alpha * layer.opacity)))
    return image


def render(root: Path, target: str, surface: Surface, size: tuple[int, int], base: Path | None = None):
    opaque = target in {"home", "function"}
    result = Image.new("RGBA", size, (0, 0, 0, 255 if opaque else 0))
    if surface.base and base is not None:
        result.alpha_composite(Image.fromarray(media.image_to_rgba(base, size)))
    for layer in surface.layers:
        # Check the reference even if it is hidden, so incomplete state cannot
        # turn into a seemingly complete export after toggling visibility.
        if not layer.visible or layer.opacity == 0:
            lookup(root, layer.asset)
            continue
        with Image.open(raster(root, layer.asset)) as image:
            result.alpha_composite(layer_pixels(image.convert("RGBA"), layer, TARGETS[target], size))
    return result


def animation_overlay(composition: Composition | None, size: tuple[int, int]):
    surface = composition.surface("animated") if composition else None
    return render(workspace(), "animated", surface, size) if surface else None


def compose_frames(frames, overlay: Image.Image | None):
    if overlay is None:
        yield from frames
        return
    for frame in frames:
        yield np.asarray(Image.alpha_composite(Image.fromarray(frame), overlay))


def composed_slots(composition, original: dict[str, Path], direct: set[str], staging: Path):
    """Materialize only edited slots, then apply the upstream home fallbacks."""
    sources = dict(original)
    for target, size in IMAGE_TARGETS.items():
        surface = composition.surface(target) if composition else None
        if surface:
            staging.mkdir(parents=True, exist_ok=True)
            path = staging / f"{target.replace(':', '-')}.png"
            render(workspace(), target, surface, size, original.get(target)).save(path)
            sources[target] = path
    if composition and composition.surface("home"):
        for target, spec in IMAGE_SLOTS.items():
            if spec.get("fallback") == "home" and target not in direct and not composition.surface(target):
                sources[target] = sources["home"]
    return sources


def composed_glow_slots(composition, original: dict[str, Path], staging: Path) -> dict[str, Path]:
    """Render composed function-icon glow variants natively at 152x152 without 128->152 upscaling."""
    glows: dict[str, Path] = {}
    if not composition:
        return glows
    for target in IMAGE_TARGETS:
        spec = IMAGE_SLOTS.get(target)
        if not spec or "glow_size" not in spec:
            continue
        surface = composition.surface(target)
        if surface:
            staging.mkdir(parents=True, exist_ok=True)
            path = staging / f"{target.replace(':', '-')}_glow.png"
            render(workspace(), target, surface, tuple(spec["glow_size"]), original.get(target)).save(path)
            glows[target] = path
    return glows


class ImagePreview(Model):
    target: str
    surface: Surface
    base: str | None = None


class AnimatedPreview(Model):
    job: str
    size: tuple[int, int]
    frames: int = Field(strict=True, ge=2, le=48)
    surface: Surface


@router.get("/raster/{ident}")
def view_raster(ident: AssetId):
    try:
        return FileResponse(raster(workspace(), ident), media_type="image/png")
    except OSError as exc:
        raise HTTPException(503, "Não foi possível preparar a imagem da camada.") from exc


@router.post("/image")
def image_preview(payload: ImagePreview):
    import io

    size = IMAGE_TARGETS.get(payload.target)
    if not size:
        raise HTTPException(422, "Destino de imagem inválido.")
    root = workspace()
    base_job = jobs.load("slot", payload.base) if payload.base and payload.surface.base else None
    if payload.base and payload.surface.base and base_job is None:
        raise HTTPException(422, "A imagem base do projeto está ausente.")
    try:
        output = io.BytesIO()
        render(root, payload.target, payload.surface, size, base_job.source if base_job else None).save(output, "PNG")
        return Response(output.getvalue(), media_type="image/png")
    except (OSError, media.MediaError) as exc:
        raise HTTPException(422, "Não foi possível renderizar a composição da imagem.") from exc


def _iter_animation_base_frames(job: jobs.Job, frame_count: int, size: tuple[int, int]):
    """Yield the exact base RGBA frames used on RAF/PKG export (honoring applied Deluxe loops)."""
    from . import dxt1, theme

    deluxe = job.meta.get("deluxe")
    deluxe_match = (
        isinstance(deluxe, dict)
        and deluxe.get("frames") == frame_count
        and list(deluxe.get("size") or []) == list(size)
    )
    png_dir = job.root / "deluxe_frames"
    dds_dir = job.root / "deluxe_dds"
    if deluxe_match and png_dir.is_dir() and all((png_dir / f"{i:02d}.png").is_file() for i in range(frame_count)):
        for index in range(frame_count):
            with Image.open(png_dir / f"{index:02d}.png") as im:
                yield np.asarray(im.convert("RGBA"))
        return
    if deluxe_match and dds_dir.is_dir() and all((dds_dir / theme.frame_name(i)).is_file() for i in range(frame_count)):
        for index in range(frame_count):
            yield dxt1.decode_dds((dds_dir / theme.frame_name(index)).read_bytes())
        return
    info = media.probe(job.source)
    yield from media.iter_raw_frames(job.source, frame_count, size, info.duration)


@router.post("/animation")
def animation_preview(payload: AnimatedPreview):
    root = workspace()
    if payload.size not in ANIMATED_SIZES or payload.frames > max_frames_for(payload.size):
        raise HTTPException(422, "Resolução ou orçamento RAF inválido.")
    job = jobs.load("animated", payload.job)
    if job is None:
        raise HTTPException(422, "Selecione uma fonte animada deste projeto.")
    validate_references(root, Composition(surfaces={"animated": payload.surface}))
    deluxe = job.meta.get("deluxe")
    deluxe_sig = ""
    if (
        isinstance(deluxe, dict)
        and deluxe.get("frames") == payload.frames
        and list(deluxe.get("size") or []) == list(payload.size)
    ):
        deluxe_sig = str(deluxe.get("key") or deluxe.get("appliedAt") or "deluxe")
    key = hashlib.sha256((VERSION + "|" + deluxe_sig + "|" + payload.model_dump_json()).encode()).hexdigest()
    cache = root / "composition-cache" / "frames"
    target = cache / key
    names = [f"{index:02d}.webp" for index in range(payload.frames)]
    if not all((target / name).is_file() for name in names):
        try:
            cache.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=cache) as temporary:
                stage = Path(temporary) / key
                stage.mkdir()
                overlay = render(root, "animated", payload.surface, payload.size)
                raw = _iter_animation_base_frames(job, payload.frames, payload.size)
                for name, frame in zip(names, compose_frames(raw, overlay), strict=True):
                    Image.fromarray(frame).save(stage / name, "WEBP", lossless=True, method=1)
                stage.rename(target)
        except (OSError, ValueError, media.MediaError) as exc:
            raise HTTPException(422, "Não foi possível preparar os quadros compostos.") from exc
    return {"frames": [f"/api/composition/frame/{key}/{name}" for name in names]}


@router.get("/frame/{key}/{name}")
def animation_frame(key: str, name: str):
    if not re.fullmatch(r"[0-9a-f]{64}", key) or not re.fullmatch(r"[0-9]{2}\.webp", name):
        raise HTTPException(404, "Quadro não encontrado.")
    file = workspace() / "composition-cache" / "frames" / key / name
    if not file.is_file():
        raise HTTPException(404, "Quadro não encontrado.")
    return FileResponse(file, media_type="image/webp")
