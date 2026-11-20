"""Project-local, forward-only clip settings. Times refer to the source clock."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .composition_model import AssetId, Model, Surface
from .config import ANIMATED_SIZES, MAX_WAIT_SECONDS, MIN_WAIT_SECONDS, max_frames_for

JobId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12}$")]
Profile = Literal["cinema", "smooth", "long", "custom"]
Strategy = Literal["cut", "align", "texture"]
PROFILES = {"cinema": ((1280, 720), 12), "smooth": ((640, 360), 24), "long": ((960, 540), 12)}


class Clip(Model):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    size: tuple[int, int]
    frames: int = Field(strict=True, ge=3, le=48)
    strategy: Strategy = "cut"
    profile: Profile = "custom"
    # Fractions of the output raster. This area can never be warped.
    protect: tuple[float, float, float, float] | None = None

    @property
    def wait(self) -> float:
        return float(f"{(self.end - self.start) / self.frames:.6g}")  # raf.build_scene uses :g

    @model_validator(mode="after")
    def limits(self):
        if self.size not in ANIMATED_SIZES or self.frames > max_frames_for(self.size):
            raise ValueError("O trecho ultrapassa o orçamento RAF.")
        if not MIN_WAIT_SECONDS <= self.wait <= MAX_WAIT_SECONDS:
            raise ValueError("Intervalo de reprodução fora da faixa do RAF.")
        if self.protect:
            x, y, right, bottom = self.protect
            if not (0 <= x < right <= 1 and 0 <= y < bottom <= 1):
                raise ValueError("Região protegida inválida.")
        return self


class MotionState(Model):
    version: Literal[1] = 1
    job: JobId
    clip: Clip
    analysis: AssetId | None = None
    rendered: AssetId | None = None
    applied: AssetId | None = None


class Scan(Model):
    job: JobId
    profile: Profile = "smooth"
    strategy: Strategy = "cut"


class Build(Model):
    job: JobId
    clip: Clip
    surface: Surface | None = None
