"""Compare every unzipped entry with the published 3.3 base, including overlays."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
BASE = "8a9ef7f2ca990cea2ed01439285a90f0770876ea"


def hashes(tree, video):
    sys.path.insert(0, str(tree))
    from fastapi.testclient import TestClient
    from PIL import Image
    from app import projects
    from app.main import app

    with (
        tempfile.TemporaryDirectory() as root,
        patch.object(projects, "ROOT", Path(root)),
        TestClient(app) as client,
    ):
        client.get("/api/projects")
        project = client.post("/api/projects", json={"name": "Comparação"}).json()["id"]
        q = {"project": project}
        swatch = io.BytesIO()
        Image.new("RGBA", (16, 12), (40, 238, 80, 169)).save(swatch, "PNG")
        asset = client.post("/api/library", files={"file": ("marca.png", swatch.getvalue())}).json()["id"]
        composition = {
            "surfaces": {
                "animated": {
                    "layers": [
                        {
                            "id": "marca",
                            "name": "marca",
                            "asset": asset,
                            "x": 971,
                            "y": 445,
                            "width": 600,
                            "height": 310,
                            "angle": 17,
                            "opacity": 0.75,
                        }
                    ]
                }
            }
        }
        results = {}
        for size in ["640x360", "960x540", "1280x720"]:
            with Path(video).open("rb") as fp:
                r = client.post(
                    "/api/animated/upload",
                    params=q,
                    data={"frames": 3, "size": size},
                    files={"file": ("teste.mp4", fp)},
                )
            r.raise_for_status()
            job = r.json()["job_id"]
            for overlay in [False, True]:
                data = {"frames": 3, "wait_seconds": 0.14, "backend": "native", "animation_size": size}
                if overlay:
                    data["composition"] = json.dumps(composition)
                r = client.post("/api/animated/" + job + "/export", params=q, data=data)
                r.raise_for_status()
                blob = client.get(r.json()["download"], params=q)
                blob.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(blob.content)) as archive:
                    results[f"{size}-{overlay}"] = {
                        n: hashlib.sha256(archive.read(n)).hexdigest() for n in archive.namelist()
                    }
        return results


class Compatibility(unittest.TestCase):
    def test_contents_against_published_base(self):
        video = os.environ.get("CZAR_ACCEPTANCE_VIDEO", REPO / "tests/sample.mp4")
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old"
            old.mkdir()
            archive = subprocess.check_output(["git", "archive", BASE, "app"], cwd=REPO, timeout=15)
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(old, filter="data")
                else:
                    tar.extractall(old)

            def run(tree):
                return json.loads(
                    subprocess.check_output(
                        [sys.executable, __file__, "--hashes", str(tree), str(video)], cwd=tmp, timeout=160
                    )
                )

            expected = run(old)
            actual = run(REPO)
            self.assertEqual(actual, expected)
            print(
                f"{len(actual)} variações; {sum(len(entries) for entries in actual.values())} entradas canônicas idênticas."
            )


if __name__ == "__main__":
    if "--hashes" in sys.argv:
        print(json.dumps(hashes(Path(sys.argv[2]), sys.argv[3])))
    else:
        unittest.main()
