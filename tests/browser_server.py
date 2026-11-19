"""Isolated Uvicorn instance with a caller-owned persistent work directory."""

import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

REPO = Path(__file__).resolve().parent.parent


@contextmanager
def server(root, port):
    command = """
import sys
from pathlib import Path
import uvicorn
from app import projects
from app.main import app
projects.ROOT = Path(sys.argv[1])
uvicorn.run(app, port=int(sys.argv[2]), access_log=False)
"""
    process = subprocess.Popen([sys.executable, "-c", command, root, str(port)], cwd=REPO)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                with urlopen(f"{base}/api/system", timeout=0.5):
                    break
            except (URLError, TimeoutError):
                if process.poll() is not None:
                    raise RuntimeError("Uvicorn encerrou durante a inicialização.")
                time.sleep(0.1)
        else:
            raise RuntimeError("Uvicorn não respondeu.")
        yield base
    finally:
        process.terminate()
        process.wait(timeout=15)
