"""Checkpoint 5 integration suite: Deluxe HTTP API, transactional apply & rollback, persistence, and preview<->RAF byte equivalence."""

from __future__ import annotations

import hashlib
import io
import tempfile
import time
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from app import dxt1, projects
from app.main import app
from tests import fixtures_deluxe as fx


class Checkpoint5IntegrationSuite(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(projects, "ROOT", self.root / "workspace"))
        self.client = self.enterContext(TestClient(app))
        self.client.get("/api/projects")
        self.project = self.client.post("/api/projects", json={"name": "Deluxe Studio"}).json()["id"]
        self.params = {"project": self.project}

        # Real quasi-periodic video with static top + moving river bottom
        frames, _ = fx.scenario_k_static_dynamic(16, size=(320, 180))
        # Add slight tonal drift so Deluxe Finish / reconstructor actually improves it
        drifted = np.stack([
            np.clip(frames[i].astype(np.float32) + 8.0 * (i / 15.0), 0, 255).astype(np.uint8)
            for i in range(len(frames))
        ])
        self.video = self.root / "scene.mp4"
        fx.write_mp4(self.video, drifted, fps=10.0)
        with self.video.open("rb") as fh:
            up = self.client.post("/api/library", files={"file": (self.video.name, fh)})
        up.raise_for_status()
        self.item = {"kind": "library", "id": up.json()["id"]}

    def _wait_job(self, job_id: str, expected: str = "ready") -> dict:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            r = self.client.get(f"/api/ouroboros/jobs/{job_id}", params=self.params)
            r.raise_for_status()
            state = r.json()
            if state["state"] != "working":
                self.assertEqual(state["state"], expected, state)
                return state
            time.sleep(0.1)
        self.fail("Job Deluxe não concluiu no tempo limite.")

    def test_full_end_to_end_deluxe_apply_persistence_duplicate_and_raf_equivalence(self):
        # 1. Analyze with Sentinela Supreme + Círculo Supreme
        r_an = self.client.post("/api/ouroboros/deluxe/analyze", params=self.params, json=self.item)
        self.assertEqual(r_an.status_code, 202, r_an.text)
        an_key = self._wait_job(r_an.json()["id"])["key"]
        an_rep = self.client.get(f"/api/ouroboros/result/{an_key}", params=self.params).json()
        self.assertEqual(an_rep["type"], "deluxe_analysis")
        self.assertEqual(len(an_rep["groups"]), 3)
        self.assertIn("levelE", an_rep["sentinela"])

        # 2. Calibrate cadence
        r_cal = self.client.post(
            "/api/ouroboros/deluxe/calibrate",
            params=self.params,
            json={**self.item, "start": 0.0, "end": 1.6, "verifyDds": False},
        )
        self.assertEqual(r_cal.status_code, 202, r_cal.text)
        cal_key = self._wait_job(r_cal.json()["id"])["key"]
        cal_rep = self.client.get(f"/api/ouroboros/result/{cal_key}", params=self.params).json()
        self.assertIn("best_balance", cal_rep["recommendations"])

        # 3. Save regional recipe to project & verify persistence
        recipe = {
            "version": 1,
            "name": "Montanha Fixa + Rio",
            "sourceKey": f"library/{self.item['id']}",
            "regions": [
                {
                    "id": "sky",
                    "name": "Céu protegido",
                    "strategy": "protect",
                    "shapes": [{"kind": "rect", "mode": "add", "bounds": [0.0, 0.0, 1.0, 0.5]}],
                    "featherPx": 6,
                    "speedScale": 1.0,
                    "enabled": True,
                }
            ],
        }
        r_save = self.client.put("/api/ouroboros/deluxe/recipes/principal", params=self.params, json=recipe)
        self.assertEqual(r_save.status_code, 200, r_save.text)
        r_list = self.client.get("/api/ouroboros/deluxe/recipes", params=self.params).json()
        self.assertEqual(len(r_list["recipes"]), 1)

        # 4. Render Deluxe with Deluxe Finish + saved recipe
        r_ren = self.client.post(
            "/api/ouroboros/deluxe/render",
            params=self.params,
            json={
                **self.item,
                "start": 0.0,
                "end": 1.6,
                "frames": 12,
                "resolution": 2,  # 640x360
                "wait": 0.066667,
                "mode": "reconstruct_auto",
                "deluxeFinish": True,
                "recipeId": "principal",
            },
        )
        self.assertEqual(r_ren.status_code, 202, r_ren.text)
        ren_key = self._wait_job(r_ren.json()["id"])["key"]
        ren_rep = self.client.get(f"/api/ouroboros/result/{ren_key}", params=self.params).json()
        self.assertEqual(ren_rep["type"], "deluxe_render")
        self.assertEqual(ren_rep["frames"], 12)
        self.assertEqual(ren_rep["size"], [640, 360])

        # Verify preview artifacts (decoded DDS WebP + diff.png + source.png + background.raf.zip)
        webp0 = self.client.get(f"/api/ouroboros/deluxe/artifact/{ren_key}/00.webp", params=self.params)
        self.assertEqual(webp0.status_code, 200)
        self.assertIn("no-store", webp0.headers["Cache-Control"])
        diff_png = self.client.get(f"/api/ouroboros/deluxe/artifact/{ren_key}/diff.png", params=self.params)
        self.assertEqual(diff_png.status_code, 200)
        deluxe_zip = self.client.get(f"/api/ouroboros/deluxe/artifact/{ren_key}/background.raf.zip", params=self.params)
        self.assertEqual(deluxe_zip.status_code, 200)

        # 5. Transactional rollback check: simulate manifest write failure -> prior manifest stays untouched!
        before_manifest = self.client.get(f"/api/projects/{self.project}").json()
        with patch.object(projects, "atomic_write", side_effect=OSError("disk full simulated")):
            r_fail = self.client.post("/api/ouroboros/deluxe/apply", params=self.params, json={"key": ren_key})
            self.assertEqual(r_fail.status_code, 503)
        after_fail_manifest = self.client.get(f"/api/projects/{self.project}").json()
        self.assertEqual(after_fail_manifest, before_manifest)

        # 6. Real transactional apply to active project
        r_app = self.client.post("/api/ouroboros/deluxe/apply", params=self.params, json={"key": ren_key})
        self.assertEqual(r_app.status_code, 200, r_app.text)
        applied = r_app.json()
        self.assertTrue(applied["applied"])
        self.assertTrue(applied["rafEquivalent"])
        job_id = applied["jobId"]

        # 7. Reload project manifest & verify persistence
        manifest = self.client.get(f"/api/projects/{self.project}").json()
        self.assertEqual(manifest["revision"], before_manifest["revision"] + 1)
        self.assertEqual(manifest["state"]["bgMode"], "animated")
        self.assertEqual(manifest["state"]["anim"]["jobId"], job_id)
        self.assertEqual(manifest["state"]["anim"]["frames"], 12)
        self.assertEqual(manifest["state"]["anim"]["size"], [640, 360])
        self.assertAlmostEqual(manifest["state"]["anim"]["wait"], 0.066667, places=5)

        # 8. Main editor preview reload returns the exact decoded DDS WebP frames!
        r_ref = self.client.post(
            f"/api/animated/{job_id}/frames",
            params=self.params,
            data={"frames": 12, "size": "640x360"},
        )
        self.assertEqual(r_ref.status_code, 200, r_ref.text)
        ed_webp0 = self.client.get(r_ref.json()["frames"][0], params=self.params)
        self.assertEqual(ed_webp0.content, webp0.content)

        # 9. Main editor RAF export matches Deluxe preview RAF **BYTE-FOR-BYTE** on every DDS, MDX, and XML!
        r_exp = self.client.post(
            f"/api/animated/{job_id}/export",
            params=self.params,
            data={"frames": 12, "wait_seconds": 0.066667, "backend": "native", "animation_size": "640x360"},
        )
        self.assertEqual(r_exp.status_code, 200, r_exp.text)
        ed_zip = self.client.get(r_exp.json()["download"], params=self.params)
        self.assertEqual(ed_zip.status_code, 200)

        with zipfile.ZipFile(io.BytesIO(deluxe_zip.content)) as z1, zipfile.ZipFile(io.BytesIO(ed_zip.content)) as z2:
            self.assertEqual(sorted(z1.namelist()), sorted(z2.namelist()))
            for entry in z1.namelist():
                self.assertEqual(
                    hashlib.sha256(z1.read(entry)).hexdigest(),
                    hashlib.sha256(z2.read(entry)).hexdigest(),
                    f"Divergência em {entry}",
                )
            # Also confirm decoded DDS frame 0 matches WebP preview pixel-for-pixel!
            dec0 = dxt1.decode_dds(z2.read("scene/background.raf/00.dds"))[..., :3]
            prev0 = np.asarray(Image.open(io.BytesIO(ed_webp0.content)).convert("RGB"))
            self.assertTrue(np.array_equal(dec0, prev0))

            # Confirm index.xml wait & case count
            cases = ET.fromstring(z2.read("scene/background.raf/index.xml")).findall("sequence/case")
            self.assertEqual(len(cases), 12)
            self.assertAlmostEqual(float(cases[0].get("wait")), 0.066667, places=5)

        # 10. Project duplication preserves the applied Deluxe background & RAF equivalence!
        dup = self.client.post(f"/api/projects/{self.project}/duplicate", json={"name": "Deluxe Cópia"})
        self.assertEqual(dup.status_code, 201, dup.text)
        dup_id = dup.json()["id"]
        dup_exp = self.client.post(
            f"/api/animated/{job_id}/export",
            params={"project": dup_id},
            data={"frames": 12, "wait_seconds": 0.066667, "backend": "native", "animation_size": "640x360"},
        )
        self.assertEqual(dup_exp.status_code, 200, dup_exp.text)
        dup_zip = self.client.get(dup_exp.json()["download"], params={"project": dup_id})
        with zipfile.ZipFile(io.BytesIO(ed_zip.content)) as z1, zipfile.ZipFile(io.BytesIO(dup_zip.content)) as z2:
            for entry in z1.namelist():
                self.assertEqual(z1.read(entry), z2.read(entry))


if __name__ == "__main__":
    unittest.main()
