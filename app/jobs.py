"""Almacen de trabajos en disco.

Cada archivo que sube el usuario abre un job con su propia carpeta dentro de
`workspace/`. Ahi vive el original, los frames de preview y lo exportado, asi
que cambiar la cantidad de frames o el wait no obliga a volver a subir nada.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .config import WORKSPACE as DATA_ROOT

WORKSPACE = DATA_ROOT / "recovery"
project_workspace: ContextVar[Path | None] = ContextVar("project_workspace", default=None)


@dataclass
class Job:
    id: str
    kind: str                      # "animated" | "static" | "icon"
    root: Path
    source: Path
    original_name: str
    meta: dict = field(default_factory=dict)

    @property
    def preview_dir(self) -> Path:
        return self.root / "preview"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    @property
    def meta_path(self) -> Path:
        return self.root / "job.json"

    def save(self) -> None:
        payload = json.dumps(
            {
                "id": self.id,
                "kind": self.kind,
                "source": self.source.name,
                "original_name": self.original_name,
                "meta": self.meta,
            },
            indent=2,
        ).encode("utf-8")
        tmp = self.root / f".job-{uuid.uuid4().hex[:8]}.tmp"
        try:
            with tmp.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            tmp.replace(self.meta_path)
        finally:
            tmp.unlink(missing_ok=True)


def _safe_suffix(filename: str) -> str:
    """Solo conserva la extension del nombre subido; el resto se descarta.

    El nombre original nunca toca el sistema de archivos, asi se evita que
    algo como `..\\..\\x.mp4` escriba fuera del workspace.
    """
    suffix = Path(filename).suffix.lower()
    if len(suffix) > 10 or not suffix.lstrip(".").isalnum():
        return ".bin"
    return suffix or ".bin"


def create(kind: str, filename: str, data: bytes) -> Job:
    job = _new_job(kind, filename, _safe_suffix(filename))
    job.source.write_bytes(data)
    job.save()
    return job


def _new_job(kind: str, filename: str, suffix: str) -> Job:
    workspace = project_workspace.get() or WORKSPACE
    workspace.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4().hex[:12]
    root = workspace / f"{kind}-{job_id}"
    root.mkdir(parents=True)

    job = Job(id=job_id, kind=kind, root=root, source=root / f"source{suffix}", original_name=filename)
    job.preview_dir.mkdir(exist_ok=True)
    job.out_dir.mkdir(exist_ok=True)
    return job


@contextmanager
def from_library(kind: str, filename: str, source: Path):
    """Give the canonical processor an independent job with a read-only original."""
    job = _new_job(kind, filename, _safe_suffix(source.name))
    try:
        os.link(source, job.source)
        job.save()
        yield job
    except BaseException:
        # Only this newly created job is rolled back. The library link survives.
        shutil.rmtree(job.root, ignore_errors=True)
        raise


def load(kind: str, job_id: str) -> Job | None:
    """Recupera un job por id.

    El id se valida como hex antes de tocar el disco, para que no pueda
    escaparse de `workspace/` por mas que venga manipulado desde el cliente.
    """
    if not job_id or len(job_id) > 32 or not all(c in "0123456789abcdef" for c in job_id):
        return None

    root = (project_workspace.get() or WORKSPACE) / f"{kind}-{job_id}"
    meta_path = root / "job.json"
    if not meta_path.is_file():
        return None

    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    source_name = str(raw.get("source", ""))
    if not source_name.startswith("source.") or Path(source_name).name != source_name:
        return None
    source = root / source_name
    if not source.is_file():
        return None

    return Job(
        id=job_id,
        kind=kind,
        root=root,
        source=source,
        original_name=str(raw.get("original_name", "")),
        meta=raw.get("meta") or {},
    )


def touch(job: Job) -> None:
    """Registra o último acesso ao trabalho."""
    try:
        job.root.touch()
    except OSError:
        pass
    job.meta["last_used"] = time.time()
