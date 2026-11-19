"""Sessões persistentes de upload fragmentado, exclusivas do proprietário do projeto.

Cada bloco é confirmado apenas depois de gravado. Em caso de queda, um bloco
parcial é descartado na retomada; o SHA-256 final é calculado sobre os bytes
originais recebidos, sem redimensionamento ou recompressão.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from . import storage
from .storage import StudioError

CHUNK_SIZE = 512 * 1024
MAX_PENDING = 12
TTL_SECONDS = 24 * 60 * 60


def base_path() -> Path:
    path = storage.ROOT / "staging" / "chunked"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def atomic_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _cleanup(base: Path) -> None:
    now = time.time()
    for entry in base.iterdir():
        if not entry.is_dir() or not re.fullmatch(storage.ID_PATTERN if hasattr(storage, "ID_PATTERN") else r"[a-f0-9]{32}", entry.name):
            continue
        try:
            if now - entry.stat().st_mtime > TTL_SECONDS:
                shutil.rmtree(entry)
        except FileNotFoundError:
            pass


def initiate(owner: str, project_id: str, name: str, size: int, checksum: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise StudioError(422, "upload_checksum", "SHA-256 inválido.")
    with storage.LOCK:
        _, project = storage.load(owner, project_id)
        if not 0 < size <= storage.UPLOAD_LIMIT:
            raise StudioError(413, "upload_size", "O arquivo excede o limite de upload.")
        if len(project.assets) >= storage.MAX_ASSETS or sum(a.bytes for a in project.assets.values()) + size > storage.PROJECT_QUOTA:
            raise StudioError(413, "quota", "O projeto não possui espaço para esse arquivo.")
        base = base_path()
        _cleanup(base)
        pending = []
        for folder in base.iterdir():
            try:
                row = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
                if row.get("owner") == owner:
                    pending.append(row)
            except (OSError, ValueError):
                continue
        # A lost creation acknowledgment must not allocate duplicate sessions.
        for pending_row in pending:
            if (pending_row.get("project") == project_id and
                pending_row.get("name") == name and pending_row.get("size") == size and
                pending_row.get("sha256") == checksum and not pending_row.get("finalizing")):
                try:
                    folder, resumed = locate(owner, project_id, pending_row["id"])
                    return {"id": resumed["id"], "size": size, "offset": resumed["offset"],
                            "chunkSize": CHUNK_SIZE, "name": name, "sha256": checksum}
                except StudioError:
                    pass
        if len(pending) >= MAX_PENDING or sum(r["size"] for r in pending) + size > storage.PROJECT_QUOTA:
            raise StudioError(413, "pending_quota", "Muitos uploads pendentes. Conclua os anteriores ou espere 24 horas.")
        key = storage.new_id()
        folder = base / key
        folder.mkdir(mode=0o700)
        asset_id = storage.new_id()
        row = {"id": key, "asset": asset_id, "owner": owner, "project": project_id,
               "name": name, "size": size, "sha256": checksum, "offset": 0, "created": time.time(),
               "finalizing": False}
        (folder / asset_id).touch(mode=0o600)
        atomic_json(folder / "meta.json", row)
        return {"id": key, "size": size, "offset": 0, "chunkSize": CHUNK_SIZE, "name": name, "sha256": checksum}


def locate(owner: str, project_id: str, key: str) -> tuple[Path, dict]:
    storage.project_path(owner, project_id)
    if not re.fullmatch(r"[a-f0-9]{32}", key):
        raise StudioError(404, "upload_missing", "Upload não encontrado.")
    folder = base_path() / key
    try:
        row = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        if row.get("owner") != owner or row.get("project") != project_id:
            raise ValueError("proprietário incorreto")
        if time.time() - folder.stat().st_mtime > TTL_SECONDS:
            shutil.rmtree(folder)
            raise FileNotFoundError("sessão expirada")
        if not 0 <= row["offset"] <= row["size"] <= storage.UPLOAD_LIMIT:
            raise ValueError("metadados inválidos")
        if not (folder / row["asset"]).is_file():
            raise ValueError("arquivo ausente")
    except (OSError, ValueError, KeyError, TypeError):
        raise StudioError(404, "upload_missing", "Upload inexistente ou expirado. Selecione o arquivo novamente.") from None
    return folder, row


def status(owner: str, project_id: str, key: str) -> dict:
    with storage.LOCK:
        _, row = locate(owner, project_id, key)
        return {k: row[k] for k in ("id", "offset", "size", "name")} | {"chunkSize": CHUNK_SIZE, "sha256": row["sha256"]}


def append(owner: str, project_id: str, key: str, offset: int, block: Path) -> dict:
    length = block.stat().st_size
    with storage.LOCK:
        folder, row = locate(owner, project_id, key)
        if row["finalizing"]:
            raise StudioError(409, "upload_finalizing", "A mídia está sendo processada.")
        if offset != row["offset"]:
            raise StudioError(409, "upload_offset", "O deslocamento mudou. A importação pode ser retomada.",
                              {"offset": row["offset"]})
        if not 0 < length <= CHUNK_SIZE or offset + length > row["size"]:
            raise StudioError(413, "upload_chunk_size", "O bloco excede os limites esperados.")
        dest = folder / row["asset"]
        # Se o processo caiu depois de gravar um bloco, mas antes de atualizar
        # o manifesto, o bloco incompleto é descartado com truncate(offset).
        with dest.open("r+b") as target, block.open("rb") as source:
            target.seek(offset)
            target.truncate(offset)
            shutil.copyfileobj(source, target, 1024 * 64)
            target.flush()
            os.fsync(target.fileno())
        row["offset"] += length
        atomic_json(folder / "meta.json", row)
        return {"offset": row["offset"], "size": row["size"]}


def reserve_finalize(owner: str, project_id: str, key: str) -> tuple[Path, dict]:
    with storage.LOCK:
        folder, row = locate(owner, project_id, key)
        if row["offset"] != row["size"] or (folder / row["asset"]).stat().st_size != row["size"]:
            raise StudioError(409, "upload_incomplete", "O envio ainda não terminou.")
        if row["finalizing"]:
            raise StudioError(409, "upload_finalizing", "A mídia já está sendo verificada.")
        row["finalizing"] = True
        atomic_json(folder / "meta.json", row)
        return folder, row


def finish(owner: str, project_id: str, folder: Path, row: dict, asset) -> dict:
    with storage.LOCK:
        current_folder, current = locate(owner, project_id, row["id"])
        if current_folder != folder or not current["finalizing"]:
            raise StudioError(409, "upload_invalid", "O estado do upload mudou.")
        if asset.sha256 != current["sha256"]:
            raise StudioError(422, "upload_checksum", "O vídeo recebido não corresponde ao original. Reenvie o arquivo.")
        root, project = storage.load(owner, project_id)
        if len(project.assets) >= storage.MAX_ASSETS or (
            sum(a.bytes for a in project.assets.values()) + asset.bytes > storage.PROJECT_QUOTA):
            raise StudioError(413, "quota", "O projeto atingiu a cota de mídia.")
        target, thumb = root / "assets" / asset.id, root / "previews" / asset.id
        try:
            (folder / row["asset"]).replace(target)
            preview = folder / "preview.webp"
            if preview.is_file():
                preview.replace(thumb)
            project.assets[asset.id] = asset
            project.updatedAt = storage.now()
            storage.save(root, project)
        except OSError:
            target.unlink(missing_ok=True)
            thumb.unlink(missing_ok=True)
            raise
        shutil.rmtree(folder, ignore_errors=True)
        return asset.model_dump(mode="json")


def release_finalize(owner: str, project_id: str, key: str) -> None:
    with storage.LOCK:
        try:
            folder, row = locate(owner, project_id, key)
            row["finalizing"] = False
            atomic_json(folder / "meta.json", row)
        except StudioError:
            pass


def remove(owner: str, project_id: str, key: str) -> None:
    with storage.LOCK:
        folder, row = locate(owner, project_id, key)
        if row["finalizing"]:
            raise StudioError(409, "upload_finalizing", "Aguarde o processamento terminar.")
        shutil.rmtree(folder)
