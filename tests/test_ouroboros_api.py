"""HTTP isolation, disk cache repair, cancellation and recoverable failures."""

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import circulo, projects
from app.main import app
from test_circulo import film
from test_sentinela import signals


class DiagnosticAPI(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(projects, "ROOT", self.root / "workspace"))
        self.client = self.enterContext(TestClient(app))
        self.client.get("/api/projects")
        self.project = self.client.post("/api/projects", json={"name": "Ensaio"}).json()["id"]
        self.params = {"project": self.project}
        self.video = self.root / "fonte.mp4"
        film(self.video, signals()["periódica"])
        with self.video.open("rb") as f:
            asset = self.client.post("/api/library", files={"file": (self.video.name, f)})
        asset.raise_for_status()
        self.item = {"kind": "library", "id": asset.json()["id"]}

    def start(self, route, payload):
        result = self.client.post("/api/ouroboros/" + route, params=self.params, json=payload)
        self.assertEqual(result.status_code, 202, result.text)
        return result.json()["id"]

    def finish(self, job, expected="ready"):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            r = self.client.get(f"/api/ouroboros/jobs/{job}", params=self.params)
            r.raise_for_status()
            state = r.json()
            if state["state"] != "working":
                self.assertEqual(state["state"], expected, state)
                return state
            time.sleep(0.1)
        self.fail("Operação não terminou.")

    def test_project_boundaries_cache_and_repair(self):
        manifest = self.client.get("/api/projects/" + self.project).json()
        job = self.start("analyze", self.item)
        other = {"project": self.client.post("/api/projects", json={"name": "Outro"}).json()["id"]}
        self.assertEqual(self.client.get("/api/ouroboros/jobs/" + job, params=other).status_code, 404)
        key = self.finish(job)["key"]
        path = "/api/ouroboros/result/" + key
        self.assertEqual(self.client.get(path, params=other).status_code, 404)
        stranger = self.enterContext(TestClient(app))
        stranger.get("/api/projects")
        self.assertEqual(stranger.get(path, params=self.params).status_code, 404)
        stranger_project = stranger.post("/api/projects", json={"name": "Isolado"}).json()["id"]
        self.assertEqual(
            stranger.post(
                "/api/ouroboros/analyze", params={"project": stranger_project}, json=self.item
            ).status_code,
            404,
        )
        with patch.object(circulo, "search", side_effect=AssertionError("cache perdido")):
            self.assertEqual(self.finish(self.start("analyze", self.item))["key"], key)

        recipe = {"analysis": key, "resolution": 2, "candidate": 0}
        preview = self.finish(self.start("preview", recipe))["key"]
        uri = "/api/ouroboros/result/" + preview
        frame = self.client.get(uri + "/00.webp", params=self.params)
        self.assertIn("no-store", frame.headers["Cache-Control"])
        good = frame.content
        self.assertEqual(good[:4], b"RIFF")
        derived = next((self.root / "workspace").glob(f"*/{self.project}/ouroboros-v1/{preview}/00.webp"))
        derived.write_bytes(good[:-3])
        with patch.object(circulo, "preview", side_effect=OSError("disco /privado")):
            error = self.finish(self.start("preview", recipe), "error")
            self.assertNotIn("/privado", error["stage"])
            self.assertEqual(self.client.get(path, params=self.params).status_code, 200)
        self.assertEqual(self.finish(self.start("preview", recipe))["key"], preview)
        self.assertEqual(derived.read_bytes(), good)
        self.assertEqual(self.client.get("/api/projects/" + self.project).json(), manifest)
        original = self.client.get("/api/library/" + self.item["id"] + "/original")
        self.assertEqual(hashlib.sha256(original.content).hexdigest(), self.item["id"])

    def test_invalid_and_cancel(self):
        route = "/api/ouroboros/analyze"
        client = self.client
        self.assertEqual(client.post(route, json=self.item).status_code, 400)
        self.assertEqual(client.post(route, params={"project": "../x"}, json=self.item).status_code, 404)
        self.assertEqual(
            client.post(route, params=self.params, json={"kind": "url", "id": "/etc/passwd"}).status_code,
            422,
        )
        self.assertEqual(
            client.post(
                route, params=self.params, json=self.item, headers={"Origin": "https://example.invalid"}
            ).status_code,
            403,
        )
        self.assertEqual(
            client.post(route, params=self.params, json={"kind": "animated", "id": "f" * 12}).status_code,
            422,
        )
        entered = Event()

        def pending(path, duration, folder, progress, check):
            entered.set()
            while True:
                check()
                time.sleep(0.02)

        with patch.object(circulo, "search", pending):
            job = self.start("analyze", self.item)
            self.assertTrue(entered.wait(5))
            self.assertEqual(job, self.start("analyze", self.item))
            self.assertEqual(
                client.post(f"/api/ouroboros/jobs/{job}/cancel", params=self.params).status_code, 200
            )
            self.finish(job, "cancelled")
        self.assertFalse(list(self.root.rglob(".stage-*")))
        self.assertFalse(list(self.root.rglob("report.json")))


if __name__ == "__main__":
    unittest.main()
