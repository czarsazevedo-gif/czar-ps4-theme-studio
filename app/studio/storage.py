from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ..config import WORKSPACE
from .schema import ID_PATTERN, Project, Settings, read_manifest

ROOT = Path(os.environ.get("CZAR_WORKSPACE", WORKSPACE)).resolve()
UPLOAD_LIMIT = int(os.environ.get("CZAR_UPLOAD_BYTES", 128 * 1024 ** 2))
PROJECT_QUOTA = int(os.environ.get("CZAR_PROJECT_BYTES", 512 * 1024 ** 2))
MAX_ASSETS = 128
MAX_PROJECTS = 40
MANIFEST_LIMIT = 1024 ** 2
LOCK = threading.RLock()


class StudioError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details
        super().__init__(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def project_path(owner: str, ident: str) -> Path:
    if not re.fullmatch(ID_PATTERN, ident):
        raise StudioError(404, "project_missing", "Projeto indisponível.")
    root = ROOT / "projects" / ident
    try:
        valid = (root / ".owner").read_text(encoding="ascii") == owner
    except OSError:
        valid = False
    if not valid:
        raise StudioError(404, "project_missing", "Projeto indisponível.")
    return root


def load(owner: str, ident: str) -> tuple[Path, Project]:
    root = project_path(owner, ident)
    with LOCK:
        try:
            return root, read_manifest(json.loads((root / "manifest.json").read_bytes()))
        except FileNotFoundError:
            raise StudioError(404, "project_missing", "Projeto indisponível.") from None


def save(root: Path, project: Project) -> None:
    dest = root / "manifest.json"
    temporary = root / f".{new_id()}.json"
    try:
        temporary.write_text(project.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(dest)
    finally:
        temporary.unlink(missing_ok=True)


def list_projects(owner: str) -> list[Project]:
    projects = []
    with LOCK:
        for root in (ROOT / "projects").glob("*"):
            if not re.fullmatch(ID_PATTERN, root.name):
                continue
            try:
                projects.append(load(owner, root.name)[1])
            except StudioError:
                continue
    return sorted(projects, key=lambda project: project.updatedAt, reverse=True)


def create(owner: str, settings: Settings, source: Path | None = None,
           original: Project | None = None) -> Project:
    with LOCK:
        if len(list_projects(owner)) >= MAX_PROJECTS:
            raise StudioError(409, "project_limit", "Limite de projetos atingido.")
        ident = new_id()
        root = ROOT / "projects" / ident
        root.mkdir(parents=True, mode=0o700)
        project = Project(id=ident, createdAt=now(), updatedAt=now(), settings=settings)
        try:
            (root / "assets").mkdir()
            (root / "previews").mkdir()
            if source is not None and original is not None:
                for key, asset in original.assets.items():
                    path = root / "assets" / key
                    shutil.copyfile(source / "assets" / key, path)
                    prev = source / "previews" / key
                    if prev.is_file():
                        shutil.copyfile(prev, root / "previews" / key)
                    project.assets[key] = asset
                project.check_references(settings)
            save(root, project)
            (root / ".owner").write_text(owner, encoding="ascii")
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
        return project


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def import_zip(owner: str, archive: Path, stage: Path) -> Project:
    from .media import inspect_asset

    try:
        with zipfile.ZipFile(archive) as zipped:
            infos = zipped.infolist()
            sizes = sum(info.file_size for info in infos)
            if len(infos) > MAX_ASSETS + 2 or sizes > PROJECT_QUOTA + MANIFEST_LIMIT * 2:
                raise ValueError("ZIP excede o limite de descompressão.")
            entries = {}
            for info in infos:
                name = info.filename
                if name in entries or not (name in {"manifest.json", "validation-report.json"} or
                    re.fullmatch(r"assets/[a-f0-9]{32}", name)):
                    raise ValueError("O ZIP contém caminho inválido ou duplicado.")
                mode = stat.S_IFMT(info.external_attr >> 16)
                limit = MANIFEST_LIMIT if name.endswith(".json") else UPLOAD_LIMIT
                if mode not in (0, stat.S_IFREG) or info.flag_bits & 1 or info.file_size > limit:
                    raise ValueError("O ZIP contém um arquivo inválido ou acima do limite.")
                if info.file_size > 2 * 1024 ** 2 and info.file_size > 250 * max(1, info.compress_size):
                    raise ValueError("Taxa de descompressão do ZIP acima do limite.")
                entries[name] = info
            project = read_manifest(json.loads(zipped.read(entries["manifest.json"])))
            expected = {f"assets/{key}" for key in project.assets}
            if expected != set(entries) - {"manifest.json", "validation-report.json"}:
                raise ValueError("Os arquivos do ZIP não correspondem ao manifesto.")
            (stage / "assets").mkdir()
            (stage / "previews").mkdir()
            for name in sorted(expected):
                size = 0
                with zipped.open(entries[name]) as source, (stage / name).open("wb") as dest:
                    while chunk := source.read(1024 ** 2):
                        size += len(chunk)
                        if size > entries[name].file_size:
                            raise ValueError("O arquivo expandido excede o tamanho declarado.")
                        dest.write(chunk)
        for key, original in project.assets.items():
            path = stage / "assets" / key
            if path.stat().st_size != original.bytes or sha256(path) != original.sha256:
                raise ValueError("O conteúdo do ZIP diverge do manifesto.")
            project.assets[key] = inspect_asset(path, original.name, stage / "previews" / key)
        project.check_references(project.settings)
        return create(owner, project.settings, stage, project)
    except (zipfile.BadZipFile, KeyError, NotImplementedError, RuntimeError, ValueError) as exc:
        raise StudioError(422, "bad_project_zip", "ZIP de projeto inválido.") from exc
