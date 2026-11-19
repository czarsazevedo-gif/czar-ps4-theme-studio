from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import ANIMATED_SIZES, AUDIO_SLOTS, IMAGE_SLOTS, MAX_FRAME_COUNT, RAF_SIZE_LIMIT, dds_bytes_for, max_frames_for
from ..theme_xml import DEFAULTS

VERSION = "0.1.0"
SCHEMA_VERSION = 1
ID_PATTERN = r"^[0-9a-f]{32}$"
AssetId = Annotated[str, Field(pattern=ID_PATTERN)]
Color = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
Argb = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{8}$")]
Seconds = Annotated[float, Field(ge=0, le=600)]
Title = Annotated[str, Field(min_length=1, max_length=127, pattern=r"^[^\x00-\x1f\x7f]+$")]
SIZES = {str(w): (w, h) for w, h in ANIMATED_SIZES}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class Transform(Model):
    fit: Literal["cover", "contain"] = "cover"
    zoom: float = Field(1, ge=0.25, le=4)
    x: float = Field(0, ge=-1, le=1)
    y: float = Field(0, ge=-1, le=1)
    rotation: float = Field(0, ge=-180, le=180)
    flipX: bool = False
    flipY: bool = False
    crop: tuple[float, float, float, float] = (0, 0, 1, 1)
    opacity: float = Field(1, ge=0, le=1)
    brightness: float = Field(1, ge=0, le=2)
    contrast: float = Field(1, ge=0, le=2)
    saturation: float = Field(1, ge=0, le=2)

    @model_validator(mode="after")
    def crop_bounds(self):
        left, top, right, bottom = self.crop
        if not (0 <= left <= right - 0.02 and 0 <= top <= bottom - 0.02 and right <= 1 and bottom <= 1):
            raise ValueError("Recorte fora da imagem ou menor que 2%.")
        return self


class Visual(Model):
    asset: AssetId
    transform: Transform = Field(default_factory=Transform)
    glow: float = Field(0.65, ge=0, le=1)


class Layer(Model):
    name: Title = "Camada"
    visual: Visual | None = None
    color: Color = "#FFFFFF"
    transform: Transform = Field(default_factory=Transform)
    visible: bool = True
    locked: bool = False
    start: Seconds = 0
    end: Seconds | None = None

    @model_validator(mode="after")
    def interval(self):
        if self.end is not None and self.end <= self.start:
            raise ValueError("O fim da camada deve ficar depois do início.")
        return self


class Animation(Model):
    source: Visual | None = None
    width: Literal["1280", "960", "640"] = "960"
    start: Seconds = 0
    end: Seconds | None = None
    frames: int = Field(12, ge=2, le=MAX_FRAME_COUNT)
    wait: float = Field(0.1, ge=0.02, le=5)
    speed: float = Field(1, ge=0.25, le=4)
    reverse: bool = False
    pingPong: bool = False
    sampling: Literal["uniform", "manual"] = "uniform"
    timeline: list[Seconds] = Field(default_factory=list, max_length=MAX_FRAME_COUNT)
    first: int = Field(0, ge=0, lt=MAX_FRAME_COUNT)

    def interval(self, duration: float) -> tuple[float, float]:
        return self.start, duration if self.end is None else self.end

    def timestamps(self, duration: float) -> list[float]:
        start, end = self.interval(duration)
        if not 0 <= start < end <= duration + 0.001:
            raise ValueError("Escolha um trecho dentro da duração da mídia.")
        end = min(end, duration)
        if self.sampling == "manual":
            times = self.timeline.copy()
            if any(not start <= t < end for t in times):
                raise ValueError("Todos os quadros manuais devem ficar dentro do trecho.")
        else:
            count = (self.frames + 2) // 2 if self.pingPong else self.frames
            times = [start + (end - start) * i / count for i in range(count)]
        if self.reverse:
            times.reverse()
        if self.pingPong:
            times += times[-2:0:-1]
            if self.sampling == "uniform":
                if len(times) < self.frames:
                    times.append(times[0])
        if times:
            first = self.first % len(times)
            times = times[first:] + times[:first]
        return times

    def budget(self) -> dict:
        size = SIZES[self.width]
        manual = len(self.timeline)
        count = self.frames if self.sampling == "uniform" else (
            manual + max(0, manual - 2) if self.pingPong else manual
        )
        wait = float(f"{min(5, max(0.02, self.wait / self.speed)):.6g}")
        total = count * dds_bytes_for(*size)
        cap = max_frames_for(size)
        invalid = not 2 <= count <= cap
        return {
            "size": size, "frames": count, "maxFrames": cap, "absoluteFrames": MAX_FRAME_COUNT,
            "fps": 1 / wait, "wait": wait, "duration": count * wait, "actualBytes": None,
            "estimatedBytes": total, "limitBytes": RAF_SIZE_LIMIT, "percent": 100 * total / RAF_SIZE_LIMIT,
            "status": "Inválido" if invalid else ("Próximo do limite" if count / cap >= 0.9 else "Seguro"),
        }


class Track(Model):
    asset: AssetId
    start: Seconds = 0
    end: Seconds | None = None
    gain: float = Field(0, ge=-24, le=12)
    fadeIn: float = Field(0, ge=0, le=30)
    fadeOut: float = Field(0, ge=0, le=30)
    normalize: bool = False


class Appearance(Model):
    background: Color = "#101421"
    themecolor: int = Field(DEFAULTS["themecolor"], ge=0, le=7)
    fontcolor: Argb = DEFAULTS["fontcolor"]
    focuscolor: Argb = DEFAULTS["focuscolor"]


class ConsoleTest(Model):
    firmware: str = Field("12.52", max_length=20)
    console: str = Field("PS4 Slim", max_length=50)
    buildId: str = Field("", max_length=64)
    installed: bool = False
    listed: bool = False
    applied: bool = False
    animation: bool = False
    music: bool = False
    sfx: bool = False
    icons: bool = False
    reboot: bool = False
    error: str = Field("", max_length=50)
    notes: str = Field("", max_length=2000)


class Settings(Model):
    name: Title
    mode: Literal["static", "animated"] = "static"
    background: dict[str, Visual] = Field(default_factory=dict, max_length=4)
    animation: Animation = Field(default_factory=Animation)
    layers: list[Layer] = Field(default_factory=list, max_length=8)
    icons: dict[str, Visual] = Field(default_factory=dict, max_length=19)
    audio: dict[str, Track] = Field(default_factory=dict, max_length=4)
    appearance: Appearance = Field(default_factory=Appearance)
    titleId: str = Field("CUSA00000", pattern=r"^[A-Z]{4}[0-9]{5}$")
    contentId: str = Field("", max_length=36, pattern=r"^(|[A-Z0-9]{6}-[A-Z]{4}[0-9]{5}_00-[A-Z0-9]{16})$")
    consoleTests: list[ConsoleTest] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def slots(self):
        if self.background.keys() - {k for k, v in IMAGE_SLOTS.items() if v["group"] == "background"}:
            raise ValueError("Slot de fundo desconhecido.")
        if self.icons.keys() - {k for k, v in IMAGE_SLOTS.items() if v["group"] != "background"}:
            raise ValueError("Slot de ícone desconhecido.")
        if self.audio.keys() - AUDIO_SLOTS.keys():
            raise ValueError("Slot de áudio desconhecido.")
        return self


class Asset(Model):
    id: AssetId
    name: str = Field(min_length=1, max_length=180)
    kind: Literal["image", "animation", "video", "audio", "at9", "vag"]
    mime: str = Field(max_length=60)
    bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = 0
    height: int = 0
    duration: Seconds = 0
    fps: float = 0
    codec: str = ""
    peak: list[float] = Field(default_factory=list, max_length=100)
    loudness: float | None = None


class Project(Model):
    schemaVersion: Literal[1] = SCHEMA_VERSION
    id: AssetId
    createdAt: str
    updatedAt: str
    revision: int = Field(0, ge=0)
    settings: Settings
    assets: dict[AssetId, Asset] = Field(default_factory=dict, max_length=128)
    exports: list[dict] = Field(default_factory=list, max_length=12)

    @property
    def thumbnail(self) -> str | None:
        home = self.settings.background.get("home")
        source = self.settings.animation.source
        return home.asset if home else source.asset if source else None

    def check_references(self, settings: Settings):
        visual_assets = [*settings.background.values(), *settings.icons.values()]
        visual_assets.extend(layer.visual for layer in settings.layers if layer.visual)
        for item in visual_assets:
            if item.asset not in self.assets or self.assets[item.asset].kind not in {"image", "animation"}:
                raise ValueError("O fundo, os ícones e as camadas devem usar imagens ou GIFs do projeto.")
        source = settings.animation.source
        if source and (source.asset not in self.assets or self.assets[source.asset].kind not in {"video", "animation"}):
            raise ValueError("A fonte RAF deve ser um GIF animado ou vídeo do projeto.")
        for slot, track in settings.audio.items():
            asset = self.assets.get(track.asset)
            supported = {"audio", "at9"} if slot == "bgm" else {"audio", "at9", "vag"}
            if not asset or asset.kind not in supported:
                raise ValueError("Escolha um arquivo de áudio do projeto para este slot.")


def read_manifest(raw: dict) -> Project:
    if not isinstance(raw, dict) or raw.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("Versão do projeto incompatível com este editor.")
    return Project.model_validate(raw)


def safe_name(name: str) -> str:
    leaf = name.replace("\\", "/").rsplit("/", 1)[-1]
    leaf = re.sub(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]", "", leaf).strip(" .")
    return leaf[:180] or "mídia"
