"""Validated composition state. Existing project manifests remain version 1."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .config import IMAGE_SLOTS, STATIC_SIZE

LIMIT_PER_SURFACE = 16
LIMIT_TOTAL = 48
IMAGE_TARGETS = {
    key: tuple(spec["size"]) for key, spec in IMAGE_SLOTS.items()
    if key in {"home", "function"} or spec["group"] in {"function_icon", "content_icon"}
}
TARGETS = {**IMAGE_TARGETS, "animated": STATIC_SIZE}
AssetId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Layer(Model):
    id: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")]
    asset: AssetId
    name: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    x: float = Field(strict=True, ge=-7680, le=7680)
    y: float = Field(strict=True, ge=-7680, le=7680)
    width: float = Field(strict=True, ge=1, le=7680)
    height: float = Field(strict=True, ge=1, le=7680)
    angle: float = Field(strict=True, ge=-360, le=360)
    opacity: float = Field(strict=True, ge=0, le=1)
    fit: Literal["contain", "cover", "stretch"] = "contain"
    proportional: bool = True
    visible: bool = True
    locked: bool = False


class Surface(Model):
    base: bool = True
    layers: list[Layer] = Field(max_length=LIMIT_PER_SURFACE)

    @model_validator(mode="after")
    def unique_layers(self):
        if len({layer.id for layer in self.layers}) != len(self.layers):
            raise ValueError("IDs de camada repetidos.")
        return self


class Composition(Model):
    version: Literal[1] = 1
    surfaces: dict[str, Surface] = Field(default_factory=dict, max_length=len(TARGETS))

    @model_validator(mode="after")
    def validate_surfaces(self):
        if set(self.surfaces) - TARGETS.keys():
            raise ValueError("Superfície de composição desconhecida.")
        if sum(len(surface.layers) for surface in self.surfaces.values()) > LIMIT_TOTAL:
            raise ValueError("A composição aceita até 48 camadas no total.")
        return self

    def surface(self, target: str) -> Surface | None:
        surface = self.surfaces.get(target)
        return surface if surface and surface.layers else None
