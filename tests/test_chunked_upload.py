"""Upload fragmentado: retomada, integridade, controles de acesso e validação."""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.main import app
from app.studio import storage

ROOT = "/api/studio"


class ChunkedUploadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        p = patch.object(storage, "ROOT", Path(self.directory.name))
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        session = self.client.get(ROOT + "/session")
        self.auth = {"x-czar-token": session.json()["token"]}
        project = self.client.post(ROOT + "/projects", headers=self.auth,
                                   json={"name": "Teste de upload", "mode": "animated"})
        self.assertEqual(project.status_code, 201, project.text)
        self.base = ROOT + "/projects/" + project.json()["id"]

    def test_parts_resume_checks_and_finish(self):
        data = (Path(__file__).parent / "sample.mp4").read_bytes()
        start = self.client.post(self.base + "/uploads", headers=self.auth,
                                 json={"name": "paisagem.mp4", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.assertEqual(start.status_code, 201, start.text)
        key = start.json()["id"]
        endpoint = self.base + "/uploads/" + key
        split = max(1, len(data) // 3)
        chunk = self.client.put(endpoint, content=data[:split],
                                headers={**self.auth, "x-upload-offset": "0"})
        self.assertEqual(chunk.status_code, 200, chunk.text)
        self.assertEqual(chunk.json()["offset"], split)
        wrong = self.client.put(endpoint, content=b"x",
                                headers={**self.auth, "x-upload-offset": "0"})
        self.assertEqual(wrong.status_code, 409)
        self.assertEqual(wrong.json()["details"]["offset"], split)
        self.assertEqual(self.client.get(endpoint).json()["offset"], split)
        # Simulate client refresh and resume at persisted server offset.
        # Resume across multiple correctly bounded chunks (each <= 512 KiB).
        position = split
        while position < len(data):
            part = data[position:position + 512 * 1024]
            rest = self.client.put(endpoint, content=part,
                                   headers={**self.auth, "x-upload-offset": str(position)})
            self.assertEqual(rest.status_code, 200, rest.text)
            position = rest.json()["offset"]
        self.assertEqual(position, len(data))
        out = self.client.post(endpoint + "/finalize", headers=self.auth)
        self.assertEqual(out.status_code, 201, out.text)
        asset = out.json()
        self.assertEqual(asset["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(asset["bytes"], len(data))
        self.assertEqual(asset["kind"], "video")
        self.assertEqual((storage.ROOT / "projects" / self.base.rsplit("/", 1)[-1] /
                          "assets" / asset["id"]).read_bytes(), data)
        self.assertEqual(self.client.get(endpoint).status_code, 404)

    def test_security_incomplete_cancel(self):
        data = b"not a video"
        r = self.client.post(self.base + "/uploads", headers=self.auth,
                             json={"name": "video.mp4", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.assertEqual(r.status_code, 201)
        url = self.base + "/uploads/" + r.json()["id"]
        self.assertEqual(self.client.post(url + "/finalize", headers=self.auth).status_code, 409)
        self.assertEqual(self.client.put(url, headers={"x-upload-offset": "0"},
                                         content=data).status_code, 403)
        self.assertEqual(self.client.put(url, headers={**self.auth, "x-upload-offset": "0",
                                         "origin": "https://attacker.example"}, content=data).status_code, 403)
        stranger = TestClient(app)
        self.addCleanup(stranger.close)
        stranger.get(ROOT + "/session")
        self.assertEqual(stranger.get(url).status_code, 404)
        self.assertEqual(self.client.delete(url, headers=self.auth).status_code, 204)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_chunk_limits_and_invalid_media(self):
        r = self.client.post(self.base + "/uploads", headers=self.auth,
                             json={"name": "bad.mp4", "size": 4, "sha256": hashlib.sha256(b"abcd").hexdigest()})
        self.assertEqual(r.status_code, 201)
        url = self.base + "/uploads/" + r.json()["id"]
        self.assertEqual(self.client.put(url, headers={**self.auth, "x-upload-offset": "0"},
                                         content=b"abcdef").status_code, 413)
        self.assertEqual(self.client.put(url, headers={**self.auth, "x-upload-offset": "0"},
                                         content=b"abcd").status_code, 200)
        fail = self.client.post(url + "/finalize", headers=self.auth)
        self.assertEqual(fail.status_code, 415)
        self.assertEqual(self.client.get(url).json()["offset"], 4)


if __name__ == "__main__":
    unittest.main()
