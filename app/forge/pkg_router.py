"""CZAR PKG Forge async job manager and FastAPI router (/api/forge/status, /api/forge/pkg/*).

Features:
- Bounded concurrency (ThreadPoolExecutor max_workers=2)
- Per-workspace single-flight protection (no duplicate simultaneous builds)
- Stage-by-stage progress reporting & cancellation (terminates child pkgtool process)
- Atomic publishing into persistent pkg-forge-v1/<pkg_id>/ (survives server restarts,
  never overwrites a valid PKG on failure)
- Streaming FileResponse downloads (zero browser RAM bloat on Chrome Android)
- Standalone independent PKG inspection endpoint (/api/forge/pkg/inspect)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .. import at9, jobs, pkg, theme_xml
from ..config import MAX_UPLOAD_BYTES, PKG_DEFAULTS
from ..dds import DEFAULT_BACKEND
from .bundle_service import _parse_params, assemble_theme_tree
from .contracts import PS4_PKG_DEFAULT_PASSCODE
from .pkg_forge import (
    PkgForgeCancelled,
    PkgForgeError,
    build_and_validate_theme_pkg,
    find_liborbispkg_tool,
    pkg_forge_info,
)
from .pkg_inspector import inspect_and_validate_pkg
from .subproc import kill_process_tree

router = APIRouter(prefix="/api/forge")

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="czar-pkg-forge")
_lock = Lock()
PKG_ID_RE = re.compile(r"^[0-9a-f]{16,32}$")


@dataclass
class PkgTask:
    id: str
    workspace: Path
    status: str = "processing"       # processing | validating | completed | failed | cancelled
    stage: str = "Na fila para empacotamento"
    percent: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    pkg_id: str | None = None
    pkg_name: str | None = None
    pkg_bytes: int | None = None
    download: str | None = None
    report_url: str | None = None
    error: str | None = None
    bundle_report: dict | None = None
    pkg_report: dict | None = None
    log: list[dict] = field(default_factory=list)
    stop: Event = field(default_factory=Event)
    active_proc: subprocess.Popen | None = None

    def check(self) -> None:
        if self.stop.is_set():
            raise PkgForgeCancelled("Empacotamento cancelado pelo usuário.")

    def progress(self, stage: str, percent: int) -> None:
        self.check()
        with _lock:
            self.stage = stage
            self.percent = percent
            if percent >= 70:
                self.status = "validating"

    def register_proc(self, proc: subprocess.Popen | None) -> None:
        with _lock:
            self.active_proc = proc

    def public(self) -> dict:
        elapsed = round((self.finished_at or time.time()) - self.started_at, 2)
        return {
            "task_id": self.id,
            "status": self.status,
            "stage": self.stage,
            "percent": self.percent,
            "elapsed": elapsed,
            "pkg_id": self.pkg_id,
            "pkg_name": self.pkg_name,
            "pkg_bytes": self.pkg_bytes,
            "download": self.download,
            "report_url": self.report_url,
            "error": self.error,
            "bundle_report": self.bundle_report,
            "pkg_report": self.pkg_report,
            "log": self.log,
            "physical_compatibility": (
                self.pkg_report.get("physical_compatibility", "pending")
                if self.pkg_report
                else "pending"
            ),
        }


_tasks: dict[str, PkgTask] = {}


def _forge_store_root(ws: Path | None = None, *, create: bool = True) -> Path:
    base = ws or jobs.project_workspace.get() or jobs.WORKSPACE
    root = base / "pkg-forge-v1"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _persist_task_snapshot(task: PkgTask) -> None:
    try:
        store_root = _forge_store_root(task.workspace, create=True)
        snap = task.public()
        snap["updated_at"] = time.time()
        tmp = store_root / f".last-task-{uuid.uuid4().hex[:8]}.tmp"
        tmp.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        tmp.replace(store_root / "last-task.json")
    except OSError:
        pass


def _load_last_task_snapshot(ws: Path) -> dict | None:
    path = _forge_store_root(ws, create=False) / "last-task.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _prune_old_tasks() -> None:
    now = time.time()
    for tid, t in list(_tasks.items()):
        if t.finished_at and (now - t.finished_at) > 3600:
            del _tasks[tid]


def _execute_pkg_task(task: PkgTask, params: dict) -> None:
    token = jobs.project_workspace.set(task.workspace)
    pkg_id = uuid.uuid4().hex[:16]
    store_root = _forge_store_root(task.workspace)
    stage_dir = store_root / f".staging-{pkg_id}"
    final_dir = store_root / pkg_id
    try:
        _persist_task_snapshot(task)
        stage_dir.mkdir(parents=True, exist_ok=True)
        task.progress("Montando árvore canônica do tema e param.sfo", 10)
        _persist_task_snapshot(task)
        content_dir, gp4_path, meta, included, _pre_report = assemble_theme_tree(stage_dir, params)

        task.check()
        pkg_out_dir = stage_dir / "pkg"

        def _on_progress(stage_txt: str, pct: int) -> None:
            task.progress(stage_txt, pct)
            _persist_task_snapshot(task)

        pkg_path, bundle_report, pkg_report, log_steps = build_and_validate_theme_pkg(
            content_dir,
            gp4_path,
            meta,
            pkg_out_dir,
            progress=_on_progress,
            check=task.check,
            register_proc=task.register_proc,
        )

        # Persist only final validated .pkg + reports atomically (clean heavy intermediate staging)
        pub_stage = store_root / f".publish-{pkg_id}"
        pub_stage.mkdir(parents=True, exist_ok=True)
        final_pkg = pub_stage / pkg_path.name
        shutil.move(str(pkg_path), str(final_pkg))

        record = {
            "pkg_id": pkg_id,
            "pkg_name": final_pkg.name,
            "pkg_bytes": final_pkg.stat().st_size,
            "content_id": meta.content_id,
            "title_id": meta.title_id,
            "title": meta.title,
            "created_at": time.time(),
            "included": included,
            "bundle_report": bundle_report.model_dump(),
            "pkg_report": pkg_report.model_dump(),
            "log": log_steps,
        }
        (pub_stage / "report.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        pub_stage.rename(final_dir)

        with _lock:
            task.status = "completed"
            task.stage = "PKG construído e validado (pendente de teste físico no PS4)"
            task.percent = 100
            task.pkg_id = pkg_id
            task.pkg_name = record["pkg_name"]
            task.pkg_bytes = record["pkg_bytes"]
            task.download = f"/api/forge/pkg/{pkg_id}/download"
            task.report_url = f"/api/forge/pkg/{pkg_id}/report"
            task.bundle_report = record["bundle_report"]
            task.pkg_report = record["pkg_report"]
            task.log = log_steps
            task.finished_at = time.time()
        _persist_task_snapshot(task)
    except PkgForgeCancelled as exc:
        with _lock:
            task.status = "cancelled"
            task.stage = "Cancelado"
            task.error = str(exc)
            task.finished_at = time.time()
        _persist_task_snapshot(task)
    except (PkgForgeError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        with _lock:
            task.status = "failed"
            task.stage = "Falhou"
            task.error = str(detail)
            task.finished_at = time.time()
        _persist_task_snapshot(task)
    except Exception as exc:
        with _lock:
            task.status = "failed"
            task.stage = "Falhou"
            task.error = f"Erro interno ao empacotar: {exc}"
            task.finished_at = time.time()
        _persist_task_snapshot(task)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
        shutil.rmtree(store_root / f".publish-{pkg_id}", ignore_errors=True)
        jobs.project_workspace.reset(token)


@router.get("/status")
def forge_overall_status() -> dict:
    return {
        "atrac9": at9.encoder_info(),
        "pkg_forge": pkg_forge_info(),
        "bundle_validator": True,
        "physical_compatibility_default": "pending",
    }


@router.post("/pkg/build")
def forge_pkg_build(
    use_saved_project: bool = Form(True),
    sync: bool = Form(False),
    title: str | None = Form(None),
    title_id: str | None = Form(None),
    content_id: str | None = Form(None),
    category: str = Form(PKG_DEFAULTS["category"]),
    passcode: str = Form(PKG_DEFAULTS["passcode"]),
    mode: str = Form(PKG_DEFAULTS["mode"]),
    slots: str | None = Form(None),
    animated_job: str = Form(""),
    audio_job: str = Form(""),
    sfx: str | None = Form(None),
    frames: int = Form(24),
    wait_seconds: float = Form(0.1),
    backend: str = Form(DEFAULT_BACKEND),
    themecolor: int = Form(0),
    fontcolor: str = Form(theme_xml.DEFAULTS["fontcolor"]),
    fontshadowcolor: str = Form(theme_xml.DEFAULTS["fontshadowcolor"]),
    focuscolor: str = Form(theme_xml.DEFAULTS["focuscolor"]),
    composition: str | None = Form(None),
    composition_mode: str = Form(""),
    animation_size: str = Form(""),
):
    """Launch an async (202) or synchronous (sync=true) PS4 Theme PKG Forge build & independent validation."""
    if not pkg_forge_info()["available"]:
        raise HTTPException(
            503,
            "Empacotador PKG não instalado neste servidor. Execute ./scripts/install_forge_tools.sh pkg "
            "ou utilize 'Exportar recursos' para baixar o bundle validado.",
        )

    params = _parse_params(
        use_saved_project=use_saved_project,
        title=title,
        title_id=title_id,
        content_id=content_id,
        category=category,
        passcode=passcode,
        mode=mode,
        slots=slots,
        animated_job=animated_job,
        audio_job=audio_job,
        sfx=sfx,
        frames=frames,
        wait_seconds=wait_seconds,
        backend=backend,
        themecolor=themecolor,
        fontcolor=fontcolor,
        fontshadowcolor=fontshadowcolor,
        focuscolor=focuscolor,
        composition=composition,
        composition_mode=composition_mode,
        animation_size=animation_size,
    )

    ws = jobs.project_workspace.get() or jobs.WORKSPACE
    ws.mkdir(parents=True, exist_ok=True)

    with _lock:
        _prune_old_tasks()
        active = [t for t in _tasks.values() if t.status in {"processing", "validating"}]
        for t in active:
            if t.workspace == ws:
                raise HTTPException(409, "Já existe um empacotamento PKG em andamento para este projeto.")
        if len(active) >= 2:
            raise HTTPException(429, "O servidor está empacotando o limite máximo de 2 temas simultâneos.")
        task = PkgTask(id=uuid.uuid4().hex[:24], workspace=ws)
        _tasks[task.id] = task
        _persist_task_snapshot(task)

    if sync:
        _execute_pkg_task(task, params)
        pub = task.public()
        if task.status != "completed":
            return JSONResponse(status_code=422, content={"detail": task.error or "Falhou", **pub})
        return pub

    _pool.submit(_execute_pkg_task, task, params)
    return JSONResponse(status_code=202, content=task.public())


@router.get("/pkg/active")
def forge_pkg_active() -> dict:
    """Return any in-flight PKG task for the current project/session, or the latest persisted terminal snapshot."""
    ws = jobs.project_workspace.get() or jobs.WORKSPACE
    with _lock:
        _prune_old_tasks()
        for t in _tasks.values():
            if t.workspace == ws and t.status in {"processing", "validating"}:
                pub = t.public()
                return {"active": pub, "latest": pub}

    snap = _load_last_task_snapshot(ws)
    if snap and snap.get("status") in {"processing", "validating"}:
        # Process restarted while a prior job was running; never pretend it is still running.
        snap = dict(snap)
        snap["status"] = "interrupted"
        snap["stage"] = "Interrompido após reinício do servidor"
        snap["error"] = "O empacotamento em andamento foi interrompido por reinício do servidor."
    return {"active": None, "latest": snap}


@router.get("/pkg/jobs/{task_id}")
def forge_pkg_job_status(task_id: str) -> dict:
    ws = jobs.project_workspace.get() or jobs.WORKSPACE
    with _lock:
        task = _tasks.get(task_id)
        if task is not None and task.workspace == ws:
            return task.public()
    snap = _load_last_task_snapshot(ws)
    if snap and snap.get("task_id") == task_id:
        if snap.get("status") in {"processing", "validating"}:
            snap = dict(snap)
            snap["status"] = "interrupted"
            snap["stage"] = "Interrompido após reinício do servidor"
            snap["error"] = "O empacotamento foi interrompido por reinício do servidor."
        return snap
    raise HTTPException(404, "Tarefa de empacotamento não encontrada neste projeto.")


@router.post("/pkg/jobs/{task_id}/cancel")
def forge_pkg_job_cancel(task_id: str) -> dict:
    ws = jobs.project_workspace.get() or jobs.WORKSPACE
    with _lock:
        task = _tasks.get(task_id)
        if task is None or task.workspace != ws:
            raise HTTPException(404, "Tarefa de empacotamento não encontrada neste projeto.")
        task.stop.set()
        kill_process_tree(task.active_proc)
        return task.public()


@router.get("/pkg/history")
def forge_pkg_history() -> dict:
    """List persisted validated PKG builds for the active project (survives server restarts)."""
    root = _forge_store_root(create=False)
    if not root.is_dir():
        return {"items": []}
    items = []
    for child in root.iterdir():
        if not child.is_dir() or not PKG_ID_RE.fullmatch(child.name):
            continue
        rep_file = child / "report.json"
        if not rep_file.is_file():
            continue
        try:
            rec = json.loads(rep_file.read_text(encoding="utf-8"))
            pr = rec.get("pkg_report", {})
            items.append(
                {
                    "pkg_id": rec["pkg_id"],
                    "pkg_name": rec["pkg_name"],
                    "pkg_bytes": rec["pkg_bytes"],
                    "content_id": rec["content_id"],
                    "title": rec.get("title", ""),
                    "created_at": rec.get("created_at", 0),
                    "download": f"/api/forge/pkg/{rec['pkg_id']}/download",
                    "report_url": f"/api/forge/pkg/{rec['pkg_id']}/report",
                    "iro_tag_header": pr.get("iro_tag_header"),
                    "iro_tag_sfo": pr.get("iro_tag_sfo"),
                    "category": pr.get("category", ""),
                    "verification_level": pr.get("verification_level", "failed"),
                    "package_status": pr.get(
                        "package_status",
                        "PACOTE VALIDADO ESTRUTURALMENTE" if pr.get("structure_validated") else "PACOTE GERADO",
                    ),
                    "structure_validated": pr.get("structure_validated", False),
                    "theme_type_valid": pr.get("theme_type_valid", False),
                    "physical_compatibility": pr.get("physical_compatibility", "pending"),
                }
            )
        except (OSError, ValueError, KeyError):
            continue
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"items": items}


def _load_persisted_pkg(pkg_id: str) -> tuple[Path, dict]:
    if not PKG_ID_RE.fullmatch(pkg_id or ""):
        raise HTTPException(404, "Identificador de PKG inválido.")
    folder = _forge_store_root(create=False) / pkg_id
    rep_file = folder / "report.json"
    if not rep_file.is_file():
        raise HTTPException(404, "PKG não encontrado neste projeto.")
    try:
        rec = json.loads(rep_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(500, "Não foi possível ler o relatório do PKG.") from exc
    pkg_name = str(rec.get("pkg_name") or "")
    pkg_path = folder / pkg_name
    if not pkg_name or Path(pkg_name).name != pkg_name or not pkg_path.is_file():
        raise HTTPException(404, "Arquivo .pkg não encontrado em disco.")
    return pkg_path, rec


@router.get("/pkg/{pkg_id}/report")
def forge_pkg_report(pkg_id: str) -> dict:
    _pkg_path, rec = _load_persisted_pkg(pkg_id)
    return rec


@router.get("/pkg/{pkg_id}/download")
def forge_pkg_download(pkg_id: str) -> FileResponse:
    pkg_path, rec = _load_persisted_pkg(pkg_id)
    return FileResponse(
        pkg_path,
        media_type="application/octet-stream",
        filename=rec["pkg_name"],
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/pkg/inspect")
async def forge_pkg_inspect(
    file: UploadFile = File(...),
    passcode: str = Form(PS4_PKG_DEFAULT_PASSCODE),
) -> dict:
    """Independently inspect and validate any uploaded PS4 .pkg file."""
    raw_name = (file.filename or "theme.pkg").replace("\\", "/").split("/")[-1].strip()
    if Path(raw_name).suffix.lower() != ".pkg":
        raise HTTPException(415, "Envie um arquivo com extensão .pkg.")
    if not pkg.PASSCODE_RE.match(passcode.strip()):
        raise HTTPException(422, "O passcode deve ter exatamente 32 caracteres alfanuméricos.")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(400, "O arquivo .pkg chegou vazio.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"O arquivo excede {MAX_UPLOAD_BYTES // 1048576} MiB.")

    with tempfile.TemporaryDirectory(prefix="czar-inspect-pkg-") as td:
        tmp_pkg = Path(td) / "uploaded.pkg"
        tmp_pkg.write_bytes(data)
        report = inspect_and_validate_pkg(
            tmp_pkg,
            passcode=passcode.strip(),
            builder_backend="external-upload",
            pkgtool_bin=find_liborbispkg_tool(),
        )
    return report.model_dump()
