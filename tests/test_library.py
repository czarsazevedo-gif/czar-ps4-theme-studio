"""Library import, persistence, project reuse, and isolation."""

import hashlib
import io
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import library, media, projects
from app.main import app
from test_projects import state as editor_state

HERE = Path(__file__).resolve().parent


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(projects, "ROOT", self.root / "workspace"))
        self.client = self.enterContext(TestClient(app))
        self.assertEqual(self.client.get("/api/projects").status_code, 200)

    def upload(self, source, name=None):
        source = Path(source)
        with source.open("rb") as stream:
            return self.client.post("/api/library", files={"file": (name or source.name, stream, "text/plain")})

    def project(self, name):
        response = self.client.post("/api/projects", json={"name": name})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def apply(self, asset, project, slot):
        return self.client.post(f"/api/library/{asset}/apply?project={project}",
                                json={"slot": slot, "frames": 2, "size": "640x360"})

    def test_catalog_formats_search_and_persistence(self):
        jpeg = self.root / "TEMA.jpg"
        self.make_media(HERE / "sample.png", jpeg, ["-frames:v", "1"])
        webm = self.root / "movimento.webm"
        self.make_media(HERE / "sample.mp4", webm, ["-t", "1", "-an", "-c:v", "libvpx-vp9"])
        for source, kind, mime in [
            (HERE.parent / "docs/03-preview-home.png", "image", "image/png"),
            (jpeg, "image", "image/jpeg"),
            (HERE / "sample.gif", "animation", "image/gif"),
            (HERE / "sample.mp4", "video", "video/mp4"),
            (webm, "video", "video/webm"),
            (HERE / "sample_sfx.wav", "audio", "audio/wav"),
        ]:
            with self.subTest(source=source.name):
                response = self.upload(source)
                self.assertEqual(response.status_code, 201, response.text)
                asset = response.json()
                self.assertEqual(asset["kind"], kind)
                self.assertEqual(asset["mime"], mime)
                self.assertEqual(asset["validation"], "verified")
                self.assertEqual(asset["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
                self.assertEqual(asset["bytes"], source.stat().st_size)
                self.assertTrue(asset["codec"])
                if kind != "audio":
                    self.assertGreater(asset["width"], 0)
                    self.assertEqual(self.client.get(asset["thumbnail"]).headers["content-type"], "image/webp")
                if kind in {"video", "animation"}:
                    self.assertGreater(asset["fps"], 0)
                    self.assertGreater(asset["duration"], 0)
                self.assertEqual(self.client.get(asset["original"]).content, source.read_bytes())
                self.assertEqual(self.upload(source).status_code, 200)
        assets = self.client.get("/api/library", params={"kind": "image", "q": "tema"}).json()["items"]
        self.assertEqual([a["originalName"] for a in assets], ["TEMA.jpg"])
        assets = self.client.get("/api/library", params={"sort": "size"}).json()["items"]
        self.assertEqual(len(assets), 6)
        self.assertEqual([a["bytes"] for a in assets], sorted((a["bytes"] for a in assets), reverse=True))
        self.assertEqual(self.client.get("/api/library?sort=oldest").json()["items"][0]["mime"], "image/png")
        self.assertEqual(len(self.client.get("/api/library?offset=3").json()["items"]), 3)
        with TestClient(app, cookies=self.client.cookies) as reopened:
            self.assertEqual(reopened.get("/api/library?sort=size").json()["items"], assets)

    def make_media(self, source, target, flags):
        subprocess.run([media.FFMPEG, "-v", "error", "-threads", "1", "-i", str(source),
                        *flags, "-threads", "1", str(target)], check=True, timeout=40)

    def test_reuse_and_duplicate_keep_derivatives_separate(self):
        image = self.upload(HERE / "sample.png").json()
        clip = self.upload(HERE / "sample.mp4").json()
        sound = self.upload(HERE / "sample_sfx.wav").json()
        one, two = self.project("Um"), self.project("Dois")
        used = {}
        for project in [one, two]:
            editor = editor_state()
            for asset, slot in [(image, "home"), (image, "fn:friend"), (clip, "animated"), (sound, "se_cursor")]:
                response = self.apply(asset["id"], project, slot)
                self.assertEqual(response.status_code, 200, response.text)
                job = response.json()["job_id"]
                if slot == "animated":
                    editor["anim"]["jobId"] = job
                elif slot == "se_cursor":
                    editor["sfx"][slot] = job
                else:
                    editor["slots"][slot] = job
            response = self.client.put(f"/api/projects/{project}",
                                       json={"revision": 1, "name": project, "state": editor})
            self.assertEqual(response.status_code, 200, response.text)
            used[project] = editor["anim"]["jobId"]
        duplicate = self.client.post(f"/api/projects/{one}/duplicate", json={"name": "Cópia"})
        self.assertEqual(duplicate.status_code, 201, duplicate.text)
        third = duplicate.json()["id"]
        used[third] = used[one]
        owner = next((self.root / "workspace").iterdir())
        original = owner / "library-v1" / clip["id"] / "source.mp4"
        job_roots = {project: owner / project / "media" / f"animated-{ident}" for project, ident in used.items()}
        for job in job_roots.values():
            self.assertEqual((job / "source.mp4").stat().st_ino, original.stat().st_ino)
        self.assertEqual(self.client.post(
            f"/api/animated/{used[third]}/frames?project={third}", data={"frames": 3, "size": "640x360"},
        ).status_code, 200)
        self.assertEqual(len(list((job_roots[third] / "preview").glob("*.webp"))), 3)
        for project in [one, two]:
            self.assertEqual(len(list((job_roots[project] / "preview").glob("*.webp"))), 2)
        export = self.client.post(f"/api/animated/{used[one]}/export?project={one}",
                                  data={"frames": 2, "wait_seconds": 0.12, "backend": "native"})
        self.assertEqual(export.status_code, 200, export.text)
        archive = self.client.get(export.json()["download"], params={"project": one})
        with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
            prefix = "scene/background.raf/"
            self.assertEqual(set(bundle.namelist()), {prefix + name for name in
                             ["00.dds", "01.dds", "plane.mdx", "index.xml"]})
            self.assertEqual(bundle.read(prefix + "00.dds")[84:88], b"DXT1")
            self.assertIn(b'wait="0.12"', bundle.read(prefix + "index.xml"))
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), clip["id"])

    def test_invalid_files_references_and_failed_writes(self):
        asset = self.upload(HERE / "sample.png").json()
        project = self.project("Privado")
        self.assertEqual(self.apply(asset["id"], project, "animated").status_code, 415)
        self.assertEqual(self.apply("f" * 64, project, "home").status_code, 404)
        self.assertEqual(self.apply(asset["id"], "not-a-project", "home").status_code, 404)
        self.assertEqual(self.client.get("/api/library/invalid/thumbnail").status_code, 404)
        with TestClient(app) as stranger:
            stranger.get("/api/projects")
            self.assertEqual(stranger.get("/api/library").json()["items"], [])
            self.assertEqual(stranger.get(asset["original"]).status_code, 404)
            self.assertEqual(stranger.get(asset["thumbnail"]).status_code, 404)
            alien = stranger.post("/api/projects", json={"name": "Outro dono"}).json()["id"]
            self.assertEqual(stranger.post(f"/api/library/{asset['id']}/apply?project={alien}",
                                          json={"slot": "home"}).status_code, 404)
        broken = self.client.post("/api/library", files={"file": ("inválido.png", b"broken", "image/png")})
        self.assertEqual(broken.status_code, 422)
        disguised = self.upload(HERE / "sample.png", "falso.mp4")
        self.assertIn(disguised.status_code, {415, 422})
        partial = self.client.post("/api/library", headers={"Content-Type": "multipart/form-data; boundary=cut"},
                                   content=b'--cut\r\nContent-Disposition: form-data; name="file"; filename="x.gif"\r\n\r\nGIF89a')
        self.assertIn(partial.status_code, {400, 422})
        with patch.object(library.os, "fsync", side_effect=OSError("disk full")):
            self.assertEqual(self.upload(HERE / "sample.gif").status_code, 503)
        self.assertEqual(self.client.get("/api/library").json()["items"], [asset])
        self.assertEqual(list(self.root.rglob(".import-*")), [])


if __name__ == "__main__":
    unittest.main()
