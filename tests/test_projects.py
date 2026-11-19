"""Persistence, revision checks and isolation against the real media routes."""

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import projects
from app.main import app

HERE = Path(__file__).parent


def state():
    return {
        "slots": {}, "bgm": None, "bgmGain": 0, "sfx": {}, "bgMode": "static",
        "contentIdTouched": True,
        "anim": {"jobId": None, "size": [640, 360], "frames": 2, "wait": 0.12, "backend": "native"},
        "campos": {
            "pkg-title": "Tema de teste", "pkg-title-id": "CUSA00123", "pkg-content-id": "",
            "pkg-themecolor": "3", "pkg-fontcolor": "#ffffff",
            "pkg-fontshadow": "#101010", "pkg-focuscolor": "#0066ff",
        },
    }


class ProjectsTest(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temp)
        self.enterContext(patch.object(projects, "ROOT", self.root))
        self.client = self.enterContext(TestClient(app))
        self.assertEqual(self.client.get("/api/projects").json(), [])
        self.id = self.create("Primeiro")
        self.url = f"/api/projects/{self.id}"

    def create(self, name):
        result = self.client.post("/api/projects", json={"name": name})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()["id"]

    def save(self, editor, name="Primeiro", revision=1):
        return self.client.put(self.url, json={"revision": revision, "name": name, "state": editor})

    def upload(self, route, filename, data=None):
        with (HERE / filename).open("rb") as source:
            result = self.client.post(route, params={"project": self.id}, data=data, files={
                "file": (filename, source),
            })
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_create_open_rename_conflict_and_restart(self):
        first = self.client.get(self.url).json()
        self.assertEqual(first["schemaVersion"], 1)
        self.assertEqual(first["createdAt"], first["updatedAt"])
        edited = state()
        self.assertEqual(self.save(edited, "Renomeado").status_code, 200)
        self.assertEqual(self.save(edited, "Nome concorrente").status_code, 409)
        loaded = self.client.get(self.url).json()
        self.assertEqual(loaded["name"], "Renomeado")
        self.assertEqual(loaded["state"], edited)
        self.assertEqual(loaded["revision"], 2)
        self.assertEqual(loaded["createdAt"], first["createdAt"])
        self.assertGreater(loaded["updatedAt"], first["updatedAt"])
        self.assertEqual(self.client.get("/api/projects").json()[0]["name"], "Renomeado")
        # A fresh server client reads all state from disk using the persisted cookie.
        with TestClient(app, cookies=self.client.cookies) as reopened:
            self.assertEqual(reopened.get(self.url).json(), loaded)

    def test_atomic_write_failure_leaves_last_revision(self):
        self.assertEqual(self.save(state()).status_code, 200)
        before = self.client.get(self.url).json()
        with patch.object(projects.os, "replace", side_effect=OSError("disk failure")):
            self.assertEqual(self.save(state(), "Falha", revision=2).status_code, 503)
        self.assertEqual(self.client.get(self.url).json(), before)
        self.assertEqual(list(self.root.rglob(".manifest-*")), [])

    def test_manifest_validation(self):
        bad = state()
        bad["slots"]["home"] = "../source"
        self.assertEqual(self.save(bad).status_code, 422)
        bad["slots"]["home"] = "a" * 12
        self.assertEqual(self.save(bad).status_code, 422)
        bad = state()
        bad["anim"]["frames"] = 49
        self.assertEqual(self.save(bad).status_code, 422)
        self.assertEqual(self.client.get("/api/projects/invalid-id").status_code, 404)
        forbidden = self.client.post("/api/projects", json={"name": "Outro"},
                                     headers={"Origin": "https://other.example"})
        self.assertEqual(forbidden.status_code, 403)
        self.assertIsNone(self.client.get(self.url).json()["state"])

    def test_duplicate_preserves_originals_and_isolates_jobs(self):
        image = self.upload("/api/slot/home/upload", "sample.png")
        video = self.upload("/api/animated/upload", "sample.gif", {"frames": 2, "size": "640x360"})
        sound = self.upload("/api/audio/se_cursor/upload", "sample_sfx.wav")
        editor = state()
        editor["slots"]["home"] = image["job_id"]
        editor["anim"]["jobId"] = video["job_id"]
        editor["sfx"]["se_cursor"] = sound["job_id"]
        self.assertEqual(self.save(editor).status_code, 200)
        duplicate = self.client.post(f"{self.url}/duplicate", json={"name": "Cópia"})
        self.assertEqual(duplicate.status_code, 201, duplicate.text)
        duplicate = duplicate.json()
        self.assertEqual(duplicate["state"], editor)
        self.assertNotEqual(duplicate["id"], self.id)
        source = next(self.root.glob(f"*/{self.id}/media/animated-*/source.gif"))
        copied = next(self.root.glob(f"*/{duplicate['id']}/media/animated-*/source.gif"))
        self.assertEqual(source.stat().st_ino, copied.stat().st_ino)
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(),
                         hashlib.sha256((HERE / "sample.gif").read_bytes()).digest())
        preview = image["preview"]
        self.assertEqual(self.client.get(preview, params={"project": self.id}).status_code, 200)
        self.assertEqual(self.client.get(preview).status_code, 404)
        other = self.create("Outro")
        self.assertEqual(self.client.get(preview, params={"project": other}).status_code, 404)
        with TestClient(app) as stranger:
            self.assertEqual(stranger.get("/api/projects").json(), [])
            self.assertEqual(stranger.get(self.url).status_code, 404)
            self.assertEqual(stranger.get(preview, params={"project": self.id}).status_code, 404)
        reframed = self.client.post(f"/api/animated/{video['job_id']}/frames",
                                   params={"project": duplicate["id"]},
                                   data={"frames": 3, "size": "640x360"})
        self.assertEqual(reframed.status_code, 200, reframed.text)
        parent_meta = (source.parent / "job.json").read_text()
        child_meta = (copied.parent / "job.json").read_text()
        self.assertNotEqual(parent_meta, child_meta)
        self.assertIn('"frames": 2', parent_meta)
        animated = f"/api/animated/{video['job_id']}"
        export = self.client.post(f"{animated}/export", params={"project": self.id},
                                  data={"frames": 2, "wait_seconds": 0.12, "backend": "native"})
        self.assertEqual(export.status_code, 200, export.text)
        download = export.json()["download"]
        self.assertEqual(self.client.get(download, params={"project": duplicate["id"]}).status_code, 404)
        archive = self.client.get(download, params={"project": self.id})
        self.assertEqual(archive.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
            prefix = "scene/background.raf/"
            self.assertEqual(set(bundle.namelist()), {
                prefix + name for name in ["00.dds", "01.dds", "plane.mdx", "index.xml"]
            })
            frame = bundle.read(prefix + "00.dds")
            self.assertEqual(frame[:4], b"DDS ")
            self.assertEqual(frame[84:88], b"DXT1")
            self.assertIn(b'wait="0.12"', bundle.read(prefix + "index.xml"))


if __name__ == "__main__":
    unittest.main()
