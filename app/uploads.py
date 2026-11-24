"""Streaming chunked upload engine (/api/uploads/*) — definitive HTTP 413 fix.

Transfers files in 2 MiB binary blocks (`PUT /api/uploads/{id}/chunk`) streamed
directly to disk via `request.stream()` with zero full-file RAM buffering.
Supports `background=true` completion so long 4K->720p normalizations never hit
reverse-proxy gateway timeouts (e.g. GitHub Codespaces `*.app.github.dev`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import at9, jobs, library, media, pkg, projects
from .config import (
    AUDIO_SLOTS,
    IMAGE_EXTS,
    IMAGE_SLOTS,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_CHUNK_BYTES,
    MAX_VIDEO_UPLOAD_BYTES,
    MEDIA_FOLDER_BY_ID,
    SESSION_MEDIA_TTL_SECONDS,
    SOUND_EXTS,
    UPLOAD_CHUNK_BYTES,
    VIDEO_EXTS,
)
from .forge import audio_router as forge_audio
from .forge import pkg_router as forge_pkg
from .forge.contracts import PS4_PKG_DEFAULT_PASSCODE
from .forge.subproc import kill_process_tree

router = APIRouter(prefix="/api/uploads")

NO_CACHE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{24}$")
CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$", re.IGNORECASE)

Purpose = Literal[
    "animated",
    "library",
    "slot",
    "audio",
    "forge_audio_inspect",
    "forge_audio_convert",
    "forge_pkg_inspect",
    "convert",
]

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="czar-upload-final")
_lock = threading.Lock()


@dataclass
class UploadRuntime:
    upload_id: str
    session: str
    root: Path
    cancel_event: threading.Event = field(default_factory=threading.Event)
    active_proc: subprocess.Popen | None = None
    updated_at: float = field(default_factory=time.time)

    def register_proc(self, proc: subprocess.Popen | None) -> None:
        with _lock:
            self.active_proc = proc


_runtime: dict[str, UploadRuntime] = {}


class UploadInitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    purpose: Purpose
    mime: str = Field(default="", max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    chunk_size: int | None = Field(default=None, ge=65536, le=MAX_UPLOAD_CHUNK_BYTES)


class UploadCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    background: bool = True


def _upload_store_root(session: str) -> Path:
    root = projects.ROOT / session / "_uploads"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _prune_old_uploads(store_root: Path, max_age_sec: float = float(SESSION_MEDIA_TTL_SECONDS)) -> None:
    now = time.time()
    try:
        for child in store_root.iterdir():
            if not child.is_dir() or not UPLOAD_ID_RE.fullmatch(child.name):
                continue
            try:
                st_file = child / "state.json"
                mtime = st_file.stat().st_mtime if st_file.is_file() else child.stat().st_mtime
                if now - mtime > max_age_sec:
                    with _lock:
                        rt = _runtime.get(child.name)
                        if rt is not None and rt.active_proc is not None:
                            continue
                        _runtime.pop(child.name, None)
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def touch_session_uploads(session: str | None) -> int:
    """Refresh upload session timestamps on keep-alive so long active sessions never expire."""
    if not session:
        return 0
    store_root = projects.ROOT / session / "_uploads"
    if not store_root.is_dir():
        return 0
    now = time.time()
    touched = 0
    try:
        for child in store_root.iterdir():
            if not child.is_dir() or not UPLOAD_ID_RE.fullmatch(child.name):
                continue
            for p in (child, child / "state.json"):
                if p.exists():
                    try:
                        os.utime(p, (now, now))
                    except OSError:
                        pass
            touched += 1
    except OSError:
        pass
    return touched


def _read_state(upload_dir: Path) -> dict:
    st_file = upload_dir / "state.json"
    if not st_file.is_file():
        raise HTTPException(404, "Sessão de upload não encontrada.")
    try:
        data = json.loads(st_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid state")
        return data
    except (OSError, ValueError) as exc:
        raise HTTPException(500, "Não foi possível ler o estado do upload.") from exc


def _write_state(upload_dir: Path, state: dict) -> None:
    state["updated_at"] = time.time()
    tmp = upload_dir / f".state-{uuid.uuid4().hex[:8]}.tmp"
    try:
        payload = json.dumps(state, indent=2).encode("utf-8")
        with tmp.open("wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(upload_dir / "state.json")
    finally:
        tmp.unlink(missing_ok=True)


def _public_state(state: dict) -> dict:
    total = int(state.get("total_bytes") or 0)
    rec = int(state.get("received_bytes") or 0)
    upload_pct = round(min(100.0, (rec / total) * 100.0), 2) if total > 0 else 0.0
    status = state.get("status", "uploading")
    pct = state.get("percent")
    if pct is None:
        pct = upload_pct if status == "uploading" else (100.0 if status == "completed" else upload_pct)
    return {
        "upload_id": state["upload_id"],
        "purpose": state["purpose"],
        "filename": state["filename"],
        "chunk_size": state["chunk_size"],
        "chunk_bytes": state["chunk_size"],
        "total_bytes": total,
        "received_bytes": rec,
        "status": status,
        "stage": state.get("stage") or "",
        "percent": pct,
        "http_status": state.get("http_status"),
        "result": state.get("result"),
        "error": state.get("error"),
        "error_status": state.get("error_status"),
        "expires_in_seconds": SESSION_MEDIA_TTL_SECONDS,
    }


def _clean_filename(raw: str) -> tuple[str, str]:
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    if not name:
        raise HTTPException(400, "Nome de arquivo inválido.")
    ext = Path(name).suffix.lower()
    return name, ext


def _validate_init(purpose: Purpose, filename: str, size: int, params: dict) -> tuple[str, str, int]:
    """Validate filename, extension, purpose params, and size limits before receiving any bytes."""
    name, ext = _clean_filename(filename)

    if purpose == "animated":
        if ext not in VIDEO_EXTS:
            raise HTTPException(
                415,
                f"Unsupported extension: {ext or '(no extension)'}. Accepted: {', '.join(sorted(VIDEO_EXTS))}",
            )
        max_bytes = MAX_VIDEO_UPLOAD_BYTES if ext not in {".gif", ".apng"} else MAX_UPLOAD_BYTES
    elif purpose == "library":
        if ext not in library.EXTENSIONS:
            raise HTTPException(415, "Formato não aceito pelo editor.")
        folder = str(params.get("folder") or "").strip()
        if folder and folder not in MEDIA_FOLDER_BY_ID:
            raise HTTPException(422, f"Pasta de mídia inválida: {folder}")
        max_bytes = MAX_VIDEO_UPLOAD_BYTES if ext in (VIDEO_EXTS - {".gif", ".apng"}) else MAX_UPLOAD_BYTES
    elif purpose == "slot":
        slot_id = str(params.get("slot") or "")
        if slot_id not in IMAGE_SLOTS:
            raise HTTPException(404, f"Unknown slot: {slot_id}")
        if ext not in IMAGE_EXTS:
            raise HTTPException(
                415,
                f"Unsupported extension: {ext or '(no extension)'}. Accepted: {', '.join(sorted(IMAGE_EXTS))}",
            )
        max_bytes = MAX_UPLOAD_BYTES
    elif purpose == "audio":
        slot_id = str(params.get("slot") or "")
        spec = AUDIO_SLOTS.get(slot_id)
        if spec is None:
            raise HTTPException(404, f"Unknown audio slot: {slot_id}")
        allowed = spec["exts"] or SOUND_EXTS
        if not spec["convert"]:
            if at9.available():
                allowed = SOUND_EXTS
            elif ext in SOUND_EXTS and ext not in allowed:
                raise HTTPException(
                    415,
                    "O PS4 exige ATRAC9 (.at9) para a música do menu. Instale o codificador "
                    "nativo (./scripts/install_forge_tools.sh at9) ou envie um .at9 válido.",
                )
        if ext not in allowed:
            raise HTTPException(
                415,
                f"Unsupported extension: {ext or '(no extension)'}. Accepted: {', '.join(sorted(allowed))}",
            )
        max_bytes = MAX_UPLOAD_BYTES
    elif purpose in {"forge_audio_inspect", "forge_audio_convert"}:
        if ext not in forge_audio.MUSIC_INPUT_EXTS:
            raise HTTPException(
                415,
                f"Formato não suportado para música: {ext or '(sem extensão)'}. "
                f"Aceitos: {', '.join(sorted(forge_audio.MUSIC_INPUT_EXTS))}",
            )
        max_bytes = MAX_UPLOAD_BYTES
    elif purpose == "forge_pkg_inspect":
        if ext != ".pkg":
            raise HTTPException(415, "Envie um arquivo com extensão .pkg.")
        passcode = str(params.get("passcode") or PS4_PKG_DEFAULT_PASSCODE).strip()
        if not pkg.PASSCODE_RE.match(passcode):
            raise HTTPException(422, "O passcode deve ter exatamente 32 caracteres alfanuméricos.")
        max_bytes = MAX_UPLOAD_BYTES
    elif purpose == "convert":
        target = str(params.get("target") or "")
        if target not in {"wav", "at9", "vag"}:
            raise HTTPException(404, f"Unknown format: {target}")
        if target == "at9" and not at9.available():
            raise HTTPException(422, "at9tool was not found, so ATRAC9 cannot be generated.")
        if ext not in SOUND_EXTS:
            raise HTTPException(
                415,
                f"Unsupported extension: {ext or '(no extension)'}. Accepted: {', '.join(sorted(SOUND_EXTS))}",
            )
        max_bytes = MAX_UPLOAD_BYTES
    else:
        raise HTTPException(422, f"Finalidade de upload inválida: {purpose}")

    if size > max_bytes:
        raise HTTPException(
            413,
            f"O arquivo excede o limite máximo permitido de {max_bytes // 1048576} MiB.",
            headers={"X-Czar-Limit-Error": "1"},
        )
    return name, ext, max_bytes


def _require_upload(request: Request, upload_id: str) -> tuple[str, Path, dict]:
    if not UPLOAD_ID_RE.fullmatch(upload_id or ""):
        raise HTTPException(404, "Sessão de upload não encontrada.")
    session = projects.owner(request)
    if not session:
        raise HTTPException(403, "Sessão indisponível.")
    upload_dir = _upload_store_root(session) / upload_id
    if not upload_dir.is_dir():
        raise HTTPException(404, "Sessão de upload não encontrada.")
    state = _read_state(upload_dir)
    if state.get("session") != session:
        raise HTTPException(404, "Sessão de upload não encontrada.")
    return session, upload_dir, state


def _get_or_create_runtime(upload_id: str, session: str, upload_dir: Path) -> UploadRuntime:
    with _lock:
        rt = _runtime.get(upload_id)
        if rt is None:
            rt = UploadRuntime(upload_id=upload_id, session=session, root=upload_dir)
            _runtime[upload_id] = rt
        rt.updated_at = time.time()
        return rt


def _parse_chunk_offset(request: Request, offset_q: int | None, total_bytes: int) -> int:
    if offset_q is not None:
        return offset_q
    hdr = request.headers.get("x-upload-offset")
    if hdr is not None and hdr.strip():
        try:
            return int(hdr.strip())
        except ValueError as exc:
            raise HTTPException(400, "Cabeçalho X-Upload-Offset inválido.") from exc
    cr = request.headers.get("content-range")
    if cr:
        m = CONTENT_RANGE_RE.match(cr.strip())
        if not m:
            raise HTTPException(400, "Cabeçalho Content-Range inválido.")
        start, _end, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if total != total_bytes:
            raise HTTPException(400, "Total de Content-Range diverge do tamanho declarado.")
        return start
    return 0


def _finalize_upload_sync(upload_dir: Path, rt: UploadRuntime) -> dict:
    """Execute the target canonical media pipeline on the assembled file and clean up payload.part."""
    from . import main as app_main

    state = _read_state(upload_dir)
    part_path = upload_dir / "payload.part"
    purpose: Purpose = state["purpose"]
    filename: str = state["filename"]
    params: dict = dict(state.get("params") or {})
    session: str = state["session"]
    project_ws_str: str | None = state.get("project_workspace")

    def set_progress(stage_txt: str, pct: float) -> None:
        cur = _read_state(upload_dir)
        if cur.get("status") == "cancelled" or rt.cancel_event.is_set():
            return
        cur["stage"] = stage_txt
        cur["percent"] = round(pct, 1)
        _write_state(upload_dir, cur)

    ws_token = None
    try:
        if rt.cancel_event.is_set():
            raise HTTPException(499, "Upload cancelado pelo usuário.")

        if project_ws_str:
            ws_token = jobs.project_workspace.set(Path(project_ws_str))
        elif purpose in {"forge_audio_inspect", "forge_audio_convert"}:
            standalone_ws = projects.ROOT / session / "_standalone_forge"
            standalone_ws.mkdir(parents=True, exist_ok=True)
            ws_token = jobs.project_workspace.set(standalone_ws)

        if not part_path.is_file():
            raise HTTPException(400, "Arquivo temporário do upload ausente.")

        http_status = 200
        if purpose == "animated":
            set_progress("Analisando vídeo e normalizando para resolução PS4 (1280×720)…", 40)
            frames = int(params.get("frames") or 12)
            size_str = str(params.get("size") or "")
            result = app_main._ingest_animated_from_path(
                part_path,
                filename,
                frames=frames,
                size=size_str,
                delete_source=True,
                cancel_event=rt.cancel_event,
                register_proc=rt.register_proc,
                progress_cb=set_progress,
            )
        elif purpose == "library":
            set_progress("Importando mídia para a Biblioteca…", 25)
            folder = str(params.get("folder") or "").strip() or None
            lib_root = projects.ROOT / session / "library-v1"
            asset, created = library.store_file_path(
                part_path,
                filename,
                lib_root,
                folder=folder,
                delete_source=True,
                cancel_event=rt.cancel_event,
                register_proc=rt.register_proc,
                progress_cb=set_progress,
            )
            http_status = 201 if created else 200
            result = asset.public()
        elif purpose == "slot":
            slot_id = str(params.get("slot") or "")
            spec = IMAGE_SLOTS.get(slot_id)
            if spec is None:
                raise HTTPException(404, f"Unknown slot: {slot_id}")
            set_progress("Convertendo imagem/ícone com alta qualidade…", 50)
            job = jobs.create_from_path("slot", filename, part_path, move=True)
            try:
                result = app_main._prepare_slot_upload(job, slot_id, spec)
            except BaseException:
                shutil.rmtree(job.root, ignore_errors=True)
                raise
        elif purpose == "audio":
            slot_id = str(params.get("slot") or "")
            spec = AUDIO_SLOTS.get(slot_id)
            if spec is None:
                raise HTTPException(404, f"Unknown audio slot: {slot_id}")
            gain_db = float(params.get("gain_db") or 0.0)
            set_progress("Processando áudio para o formato PS4…", 50)
            job = jobs.create_from_path("audio", filename, part_path, move=True)
            try:
                result = app_main._prepare_audio_upload(job, slot_id, spec, gain_db)
            except BaseException:
                shutil.rmtree(job.root, ignore_errors=True)
                raise
        elif purpose == "forge_audio_inspect":
            set_progress("Analisando loudness EBU R128 e pontos de loop…", 50)
            result = forge_audio.inspect_audio_file_path(part_path, filename, move_source=True)
        elif purpose == "forge_audio_convert":
            set_progress("Convertendo áudio para ATRAC9 PS4 (48 kHz · 144 kbps)…", 50)
            conv_kwargs = {
                k: params[k]
                for k in (
                    "loop_enabled", "loop_mode", "start_sec", "end_sec",
                    "start_sample", "end_sample", "trim_silence", "crossfade_ms",
                    "allow_fade", "fade_in_ms", "fade_out_ms", "volume_mode",
                    "gain_db", "target_lufs",
                )
                if k in params and params[k] is not None
            }
            result = forge_audio.convert_apply_from_path(
                part_path, filename, move_source=True, **conv_kwargs
            )
        elif purpose == "forge_pkg_inspect":
            set_progress("Inspecionando cabeçalho, Sc0 e Image0 do PKG…", 50)
            passcode = str(params.get("passcode") or PS4_PKG_DEFAULT_PASSCODE)
            result = forge_pkg.inspect_pkg_file_path(part_path, filename, passcode=passcode)
        elif purpose == "convert":
            target = str(params.get("target") or "")
            set_progress(f"Convertendo áudio para {target.upper()}…", 50)
            result = app_main._convert_audio_from_path(target, part_path, filename, move_source=True)
        else:
            raise HTTPException(422, f"Finalidade não suportada: {purpose}")

        if rt.cancel_event.is_set():
            raise HTTPException(499, "Upload cancelado pelo usuário.")

        cur = _read_state(upload_dir)
        cur.update({
            "status": "completed",
            "stage": "Concluído",
            "percent": 100.0,
            "http_status": http_status,
            "result": result,
            "error": None,
            "error_status": None,
            "finished_at": time.time(),
        })
        _write_state(upload_dir, cur)
        return cur
    except HTTPException as exc:
        cur = _read_state(upload_dir)
        if rt.cancel_event.is_set() or exc.status_code == 499:
            cur.update({
                "status": "cancelled",
                "stage": "Cancelado",
                "error": "Upload cancelado pelo usuário.",
                "error_status": 499,
                "finished_at": time.time(),
            })
        else:
            cur.update({
                "status": "failed",
                "stage": "Falhou",
                "error": str(exc.detail),
                "error_status": exc.status_code,
                "finished_at": time.time(),
            })
        _write_state(upload_dir, cur)
        return cur
    except media.MediaError as exc:
        cur = _read_state(upload_dir)
        cur.update({
            "status": "failed",
            "stage": "Falhou",
            "error": app_main._sanitize_media_error(str(exc)),
            "error_status": 422,
            "finished_at": time.time(),
        })
        _write_state(upload_dir, cur)
        return cur
    except Exception as exc:
        cur = _read_state(upload_dir)
        cur.update({
            "status": "failed",
            "stage": "Falhou",
            "error": str(exc) or "Erro interno ao finalizar upload.",
            "error_status": 500,
            "finished_at": time.time(),
        })
        _write_state(upload_dir, cur)
        return cur
    finally:
        # Always erase the raw temporary payload file after processing/failure/cancel!
        part_path.unlink(missing_ok=True)
        if ws_token is not None:
            jobs.project_workspace.reset(ws_token)


@router.post("/init", status_code=201)
def upload_init(request: Request, payload: UploadInitRequest):
    """Initialize a streaming 2 MiB chunked upload session."""
    session = projects.owner(request)
    if not session:
        raise HTTPException(403, "Sessão indisponível.")
    name, ext, _max_bytes = _validate_init(payload.purpose, payload.filename, payload.size, payload.params)

    store_root = _upload_store_root(session)
    _prune_old_uploads(store_root)

    upload_id = uuid.uuid4().hex[:24]
    upload_dir = store_root / upload_id
    upload_dir.mkdir(parents=True, mode=0o700)
    (upload_dir / "payload.part").touch()

    chunk_size = min(payload.chunk_size or UPLOAD_CHUNK_BYTES, MAX_UPLOAD_CHUNK_BYTES)
    ws = jobs.project_workspace.get()
    state = {
        "upload_id": upload_id,
        "session": session,
        "purpose": payload.purpose,
        "filename": name,
        "extension": ext,
        "params": payload.params,
        "project_workspace": str(ws) if ws is not None else None,
        "chunk_size": chunk_size,
        "total_bytes": int(payload.size),
        "received_bytes": 0,
        "chunks_received": 0,
        "status": "uploading",
        "stage": "Aguardando blocos",
        "percent": 0.0,
        "created_at": time.time(),
        "updated_at": time.time(),
        "finished_at": None,
        "http_status": None,
        "result": None,
        "error": None,
        "error_status": None,
    }
    _write_state(upload_dir, state)
    _get_or_create_runtime(upload_id, session, upload_dir)
    return JSONResponse(_public_state(state), status_code=201, headers=NO_CACHE)


@router.put("/{upload_id}/chunk")
async def upload_chunk(
    request: Request,
    upload_id: str,
    offset: int | None = Query(None, ge=0),
):
    """Stream a single binary chunk (<= 2 MiB default) directly to disk without full-file RAM buffering."""
    session, upload_dir, state = _require_upload(request, upload_id)
    rt = _get_or_create_runtime(upload_id, session, upload_dir)

    if state["status"] != "uploading" or rt.cancel_event.is_set():
        raise HTTPException(409, f"Upload não está aceitando blocos (estado: {state['status']}).")

    total_bytes = int(state["total_bytes"])
    received = int(state["received_bytes"])
    req_offset = _parse_chunk_offset(request, offset, total_bytes)

    if req_offset != received:
        return JSONResponse(
            {
                "detail": f"Deslocamento de bloco inesperado: esperado {received}, recebido {req_offset}.",
                "expected_offset": received,
                "total_bytes": total_bytes,
            },
            status_code=409,
            headers=NO_CACHE,
        )

    part_path = upload_dir / "payload.part"
    if not part_path.is_file():
        raise HTTPException(404, "Arquivo parcial do upload ausente.")

    chunk_written = 0
    with part_path.open("r+b") as fh:
        fh.seek(req_offset)
        async for piece in request.stream():
            if not piece:
                continue
            if rt.cancel_event.is_set():
                raise HTTPException(499, "Upload cancelado pelo usuário.")
            chunk_written += len(piece)
            if chunk_written > MAX_UPLOAD_CHUNK_BYTES:
                raise HTTPException(
                    413,
                    f"Bloco excede o máximo por requisição ({MAX_UPLOAD_CHUNK_BYTES // 1048576} MiB).",
                )
            if req_offset + chunk_written > total_bytes:
                raise HTTPException(
                    413,
                    "Os blocos enviados ultrapassam o tamanho total declarado.",
                    headers={"X-Czar-Limit-Error": "1"},
                )
            fh.write(piece)
        fh.flush()

    if chunk_written == 0:
        raise HTTPException(400, "Bloco vazio enviado.")

    state["received_bytes"] = req_offset + chunk_written
    state["chunks_received"] = int(state.get("chunks_received") or 0) + 1
    pct = round((state["received_bytes"] / total_bytes) * 100.0, 2) if total_bytes else 100.0
    state["percent"] = pct
    state["stage"] = f"Enviando blocos ({state['chunks_received']})"
    _write_state(upload_dir, state)
    return JSONResponse(_public_state(state), status_code=200, headers=NO_CACHE)


@router.post("/{upload_id}/complete")
async def upload_complete(
    request: Request,
    upload_id: str,
    background: bool | None = Query(None),
):
    """Finalize a completed chunked upload (defaults to background=true -> HTTP 202 + status polling)."""
    session, upload_dir, state = _require_upload(request, upload_id)
    rt = _get_or_create_runtime(upload_id, session, upload_dir)

    use_background = True if background is None else bool(background)
    if background is None:
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            try:
                body = await request.json()
                if isinstance(body, dict) and "background" in body:
                    use_background = bool(body["background"])
            except Exception:
                pass

    if state["status"] == "completed":
        return JSONResponse(_public_state(state), status_code=200, headers=NO_CACHE)
    if state["status"] == "processing":
        return JSONResponse(_public_state(state), status_code=202, headers=NO_CACHE)
    if state["status"] in {"cancelled", "failed"}:
        raise HTTPException(409, state.get("error") or f"Upload em estado {state['status']}.")

    if int(state["received_bytes"]) != int(state["total_bytes"]):
        raise HTTPException(
            400,
            f"Upload incompleto: {state['received_bytes']} de {state['total_bytes']} bytes recebidos.",
        )

    state["status"] = "processing"
    state["stage"] = "Validando e processando mídia no servidor…"
    state["percent"] = 10.0
    _write_state(upload_dir, state)

    if use_background:
        _pool.submit(_finalize_upload_sync, upload_dir, rt)
        return JSONResponse(_public_state(state), status_code=202, headers=NO_CACHE)

    final_state = _finalize_upload_sync(upload_dir, rt)
    if final_state["status"] == "failed":
        code = int(final_state.get("error_status") or 422)
        return JSONResponse(
            {"detail": final_state.get("error") or "Falha ao processar upload.", **_public_state(final_state)},
            status_code=code,
            headers=NO_CACHE,
        )
    if final_state["status"] == "cancelled":
        return JSONResponse(_public_state(final_state), status_code=409, headers=NO_CACHE)
    return JSONResponse(_public_state(final_state), status_code=200, headers=NO_CACHE)


@router.get("/{upload_id}/status")
def upload_status(request: Request, upload_id: str):
    """Poll streaming upload and background normalization/processing progress."""
    _session, upload_dir, state = _require_upload(request, upload_id)
    try:
        os.utime(upload_dir / "state.json", None)
    except OSError:
        pass
    return JSONResponse(_public_state(state), status_code=200, headers=NO_CACHE)


def _do_cancel(request: Request, upload_id: str) -> JSONResponse:
    session, upload_dir, state = _require_upload(request, upload_id)
    rt = _get_or_create_runtime(upload_id, session, upload_dir)
    rt.cancel_event.set()
    with _lock:
        proc = rt.active_proc
    if proc is not None:
        kill_process_tree(proc)

    (upload_dir / "payload.part").unlink(missing_ok=True)
    if state["status"] not in {"completed"}:
        state["status"] = "cancelled"
        state["stage"] = "Cancelado"
        state["error"] = "Upload cancelado pelo usuário."
        state["finished_at"] = time.time()
        _write_state(upload_dir, state)
    return JSONResponse(_public_state(state), status_code=200, headers=NO_CACHE)


@router.post("/{upload_id}/cancel")
def upload_cancel_post(request: Request, upload_id: str):
    return _do_cancel(request, upload_id)


@router.delete("/{upload_id}")
def upload_cancel_delete(request: Request, upload_id: str):
    return _do_cancel(request, upload_id)
