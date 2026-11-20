"""Cinematic Content Editor — regional masks, strategies, diff comparison, and atomic project persistence."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Annotated, Literal, Sequence

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import OUROBOROS_VERSION as DELUXE_VERSION
from .metrics import compare_before_after, evaluate_loop, evaluate_post_dds
from .reconstruction import level6_multi_region_reconstruct

RegionId = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")]
RecipeId = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")]
StrategyKind = Literal["protect", "animate", "remove_transient", "fix_seam", "adjust_motion"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RegionShape(_Model):
    """Resolution-independent normalized [0..1] geometric or touch-brush mask primitive."""

    kind: Literal["rect", "ellipse", "polygon", "brush"]
    mode: Literal["add", "erase"] = "add"
    # rect: [x0, y0, x1, y1]; ellipse: [cx, cy, rx, ry]
    bounds: list[float] | None = None
    # polygon or brush stroke points: [[x, y], ...]
    points: list[list[float]] | None = None
    radiusNorm: float = Field(default=0.04, ge=0.005, le=0.35)


class RegionEdit(_Model):
    id: RegionId
    name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    strategy: StrategyKind
    shapes: list[RegionShape] = Field(min_length=1, max_length=64)
    featherPx: int = Field(default=6, ge=0, le=32)
    speedScale: float = Field(default=1.0, ge=0.1, le=3.0)
    directionXY: tuple[float, float] | None = None
    enabled: bool = True


class RegionalRecipe(_Model):
    version: Literal[1] = 1
    algorithmVersion: str = DELUXE_VERSION
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)] = "Receita Regional"
    sourceKey: str = ""
    regions: list[RegionEdit] = Field(default_factory=list, max_length=16)


def rasterize_region_mask(region: RegionEdit, size: tuple[int, int]) -> np.ndarray:
    """Rasterize normalized shapes (rect, ellipse, polygon, brush add/erase) into a (H, W) bool mask."""
    w, h = size
    canvas = np.zeros((h, w), dtype=np.uint8)
    for shape in region.shapes:
        val = 255 if shape.mode == "add" else 0
        if shape.kind == "rect" and shape.bounds and len(shape.bounds) == 4:
            x0, y0, x1, y1 = shape.bounds
            p0 = (int(np.clip(round(min(x0, x1) * w), 0, w - 1)), int(np.clip(round(min(y0, y1) * h), 0, h - 1)))
            p1 = (int(np.clip(round(max(x0, x1) * w), 0, w - 1)), int(np.clip(round(max(y0, y1) * h), 0, h - 1)))
            cv2.rectangle(canvas, p0, p1, val, thickness=-1)
        elif shape.kind == "ellipse" and shape.bounds and len(shape.bounds) == 4:
            cx, cy, rx, ry = shape.bounds
            center = (int(np.clip(round(cx * w), 0, w - 1)), int(np.clip(round(cy * h), 0, h - 1)))
            axes = (max(1, int(round(abs(rx) * w))), max(1, int(round(abs(ry) * h))))
            cv2.ellipse(canvas, center, axes, 0, 0, 360, val, thickness=-1)
        elif shape.kind == "polygon" and shape.points and len(shape.points) >= 3:
            pts = np.array(
                [[int(np.clip(round(pt[0] * w), 0, w - 1)), int(np.clip(round(pt[1] * h), 0, h - 1))] for pt in shape.points],
                dtype=np.int32,
            )
            cv2.fillPoly(canvas, [pts], val)
        elif shape.kind == "brush" and shape.points and len(shape.points) >= 1:
            rad = max(1, int(round(shape.radiusNorm * min(w, h))))
            pts = [
                (int(np.clip(round(pt[0] * w), 0, w - 1)), int(np.clip(round(pt[1] * h), 0, h - 1)))
                for pt in shape.points
            ]
            if len(pts) == 1:
                cv2.circle(canvas, pts[0], rad, val, thickness=-1)
            else:
                for a, b in zip(pts, pts[1:]):
                    cv2.line(canvas, a, b, val, thickness=rad * 2, lineType=cv2.LINE_AA)
    return canvas > 127


def compute_diff_stats(original: np.ndarray, edited: np.ndarray) -> dict:
    """Measure real pixel differences between original and regionally edited sequence."""
    diff = np.abs(edited.astype(np.float32) - original.astype(np.float32)).mean(axis=-1)  # (N, H, W)
    max_map = diff.max(axis=0)
    changed_frac = float((max_map > 1.5).mean())
    mean_diff = float(diff.mean())
    p95_diff = float(np.percentile(max_map, 95))
    # Compact visual diff RGB image (amplified 3x heatmap on frame 0 / max diff)
    norm_u8 = np.clip(max_map * 4.0, 0, 255).astype(np.uint8)
    heat_bgr = cv2.applyColorMap(norm_u8, cv2.COLORMAP_INFERNO)
    heat_rgb = cv2.cvtColor(heat_bgr, cv2.COLOR_BGR2RGB)
    return {
        "changedPixelFraction": round(changed_frac, 4),
        "meanAbsDiff": round(mean_diff, 4),
        "p95AbsDiff": round(p95_diff, 4),
        "heatmap": heat_rgb,
    }


def apply_regional_recipe(
    frames: Sequence[np.ndarray] | np.ndarray,
    wait: float,
    recipe: RegionalRecipe,
    *,
    verify_dds: bool = True,
) -> dict:
    """Apply a validated RegionalRecipe to a frame stack and scientifically evaluate the result."""
    t0 = time.perf_counter()
    src = np.asarray(frames, dtype=np.uint8)
    if src.shape[-1] == 4:
        src = src[..., :3]
    n, h, w, _ = src.shape

    specs = []
    protected_union = np.zeros((h, w), dtype=bool)
    for reg in recipe.regions:
        if not reg.enabled:
            continue
        mask = rasterize_region_mask(reg, (w, h))
        if not mask.any():
            continue
        if reg.strategy == "protect":
            protected_union |= mask
        specs.append(
            {
                "id": reg.id,
                "mask": mask,
                "strategy": reg.strategy,
                "speedScale": reg.speedScale,
                "directionXY": list(reg.directionXY) if reg.directionXY is not None else None,
                "featherPx": reg.featherPx,
            }
        )

    prot_arg = protected_union if protected_union.any() else None
    eval_fn = evaluate_post_dds if verify_dds else evaluate_loop
    before_eval = eval_fn(src, wait, protected_mask=prot_arg)

    if not specs:
        diff_info = compute_diff_stats(src, src)
        return {
            "version": DELUXE_VERSION,
            "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
            "frames": src,
            "meta": {"applied": False, "regionsProcessed": 0},
            "diffStats": {k: v for k, v in diff_info.items() if k != "heatmap"},
            "heatmap": diff_info["heatmap"],
            "baselineEvaluation": before_eval,
            "finalEvaluation": before_eval,
            "comparison": compare_before_after(before_eval, before_eval),
        }

    edited, meta = level6_multi_region_reconstruct(src, wait, region_specs=specs)
    after_eval = eval_fn(edited, wait, protected_mask=prot_arg, reference_frames=src)
    cmp_res = compare_before_after(before_eval, after_eval)
    diff_info = compute_diff_stats(src, edited)

    return {
        "version": DELUXE_VERSION,
        "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
        "frames": edited,
        "meta": meta,
        "diffStats": {k: v for k, v in diff_info.items() if k != "heatmap"},
        "heatmap": diff_info["heatmap"],
        "baselineEvaluation": before_eval,
        "finalEvaluation": after_eval,
        "comparison": cmp_res,
    }


# ==================== ATOMIC PROJECT PERSISTENCE ====================

def _recipes_dir(project_root: Path) -> Path:
    d = project_root / "ouroboros-deluxe" / "recipes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_recipe_path(project_root: Path, recipe_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", recipe_id):
        raise ValueError("Identificador de receita inválido.")
    return _recipes_dir(project_root) / f"{recipe_id}.json"


def save_recipe(project_root: Path, recipe_id: str, recipe: RegionalRecipe) -> Path:
    """Atomically persist a regional editing recipe inside the project directory."""
    dest = _safe_recipe_path(project_root, recipe_id)
    payload = recipe.model_dump_json(indent=2).encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".recipe-", delete=False) as fh:
        tmp = Path(fh.name)
        try:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
            fh.close()
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
    return dest


def load_recipe(project_root: Path, recipe_id: str) -> RegionalRecipe:
    """Load and validate a persisted regional editing recipe from the project directory."""
    path = _safe_recipe_path(project_root, recipe_id)
    if not path.is_file():
        raise FileNotFoundError(f"Receita '{recipe_id}' não encontrada.")
    return RegionalRecipe.model_validate_json(path.read_bytes())


def list_recipes(project_root: Path) -> list[dict]:
    """List persisted regional recipes in a project."""
    d = project_root / "ouroboros-deluxe" / "recipes"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            rec = RegionalRecipe.model_validate_json(p.read_bytes())
            out.append(
                {
                    "id": p.stem,
                    "name": rec.name,
                    "sourceKey": rec.sourceKey,
                    "regionCount": len(rec.regions),
                    "updatedAt": int(p.stat().st_mtime),
                }
            )
        except (OSError, ValueError):
            continue
    return out
