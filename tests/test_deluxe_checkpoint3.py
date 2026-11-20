"""Checkpoint 3 verification: Cinematic Content Editor regional masks, 5 strategies, diff stats & persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.deluxe.regions import (
    RegionEdit,
    RegionShape,
    RegionalRecipe,
    apply_regional_recipe,
    list_recipes,
    load_recipe,
    rasterize_region_mask,
    save_recipe,
)
from tests import fixtures_deluxe as fx


class Checkpoint3RegionalEditorSuite(unittest.TestCase):
    def test_all_shape_primitives_and_brush_add_erase_scale_across_resolutions(self):
        region = RegionEdit(
            id="freeform",
            name="Máscara combinada",
            strategy="protect",
            shapes=[
                RegionShape(kind="rect", mode="add", bounds=[0.0, 0.0, 1.0, 0.5]),
                RegionShape(kind="ellipse", mode="erase", bounds=[0.5, 0.25, 0.15, 0.12]),
                RegionShape(kind="polygon", mode="add", points=[[0.1, 0.6], [0.4, 0.6], [0.25, 0.9]]),
                RegionShape(kind="brush", mode="add", points=[[0.6, 0.7], [0.8, 0.85]], radiusNorm=0.05),
                RegionShape(kind="brush", mode="erase", points=[[0.7, 0.775]], radiusNorm=0.03),
            ],
        )
        for sz in ((640, 360), (960, 540), (1280, 720)):
            mask = rasterize_region_mask(region, sz)
            self.assertEqual(mask.shape, (sz[1], sz[0]))
            # Top-left inside rect -> True
            self.assertTrue(mask[int(sz[1] * 0.1), int(sz[0] * 0.1)])
            # Center of erased ellipse -> False
            self.assertFalse(mask[int(sz[1] * 0.25), int(sz[0] * 0.5)])
            # Inside bottom polygon -> True
            self.assertTrue(mask[int(sz[1] * 0.7), int(sz[0] * 0.25)])

    def test_regional_recipe_protect_animate_adjust_motion_post_dds(self):
        frames, _ = fx.scenario_k_static_dynamic(12, size=(320, 180))
        recipe = RegionalRecipe(
            name="Montanha + Rio",
            sourceKey="a" * 64,
            regions=[
                RegionEdit(
                    id="sky_mount",
                    name="Montanha protegida",
                    strategy="protect",
                    shapes=[RegionShape(kind="rect", bounds=[0.0, 0.0, 1.0, 0.5])],
                ),
                RegionEdit(
                    id="river_flow",
                    name="Rio acelerado",
                    strategy="adjust_motion",
                    speedScale=2.0,
                    directionXY=(3.0, 0.8),
                    featherPx=6,
                    shapes=[RegionShape(kind="rect", bounds=[0.0, 0.5, 1.0, 1.0])],
                ),
            ],
        )
        res = apply_regional_recipe(frames, wait=0.05, recipe=recipe, verify_dds=True)
        self.assertTrue(res["meta"]["applied"])
        self.assertEqual(res["meta"]["regionsProcessed"], 2)
        self.assertEqual(res["finalEvaluation"]["metrics"]["staticIntegrity"]["maxAbsDiff"], 0.0)
        self.assertFalse(res["finalEvaluation"]["metrics"]["reversals"]["detected"])
        self.assertGreater(res["diffStats"]["changedPixelFraction"], 0.15)
        self.assertEqual(res["heatmap"].shape, (180, 320, 3))

    def test_regional_recipe_remove_transient_and_fix_seam(self):
        trans = fx.scenario_i_transient_object(12, size=(240, 136))
        recipe = RegionalRecipe(
            name="Limpar objeto + costura",
            sourceKey="b" * 64,
            regions=[
                RegionEdit(
                    id="rm_obj",
                    name="Remover círculo",
                    strategy="remove_transient",
                    featherPx=4,
                    shapes=[RegionShape(kind="rect", bounds=[0.15, 0.30, 0.95, 0.72])],
                ),
                RegionEdit(
                    id="seam_bg",
                    name="Refinar costura",
                    strategy="fix_seam",
                    featherPx=4,
                    shapes=[RegionShape(kind="rect", bounds=[0.0, 0.0, 1.0, 1.0])],
                ),
            ],
        )
        res = apply_regional_recipe(trans, wait=0.08, recipe=recipe, verify_dds=False)
        self.assertLess(
            res["finalEvaluation"]["metrics"]["visualSimilarity"]["localSeamExcess"],
            res["baselineEvaluation"]["metrics"]["visualSimilarity"]["localSeamExcess"] * 0.65,
        )

    def test_atomic_recipe_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe = RegionalRecipe(
                name="Cena Lua + Nuvens",
                sourceKey="c" * 64,
                regions=[
                    RegionEdit(
                        id="lua",
                        name="Lua fixa",
                        strategy="protect",
                        shapes=[RegionShape(kind="ellipse", bounds=[0.2, 0.2, 0.1, 0.15])],
                    )
                ],
            )
            save_recipe(root, "cena_lua", recipe)
            loaded = load_recipe(root, "cena_lua")
            self.assertEqual(loaded, recipe)
            listed = list_recipes(root)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["id"], "cena_lua")
            self.assertEqual(listed[0]["regionCount"], 1)
            with self.assertRaises(ValueError):
                save_recipe(root, "../escape", recipe)


if __name__ == "__main__":
    unittest.main()
