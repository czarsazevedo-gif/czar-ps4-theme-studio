from __future__ import annotations

import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import build, storage
from .schema import Project
from .storage import StudioError
from .validate import build_report, item

LOG = logging.getLogger("czar.jobs")
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="czar")
GUARD = threading.RLock()
ACTIVE: dict[str, dict] = {}
KINDS = {"validate", "project", "assets", "raf", "tree", "bundle", "diagnostic"}


class Cancelled(Exception):
    pass


def update_file(root: Path, payload: dict) -> None:
    temporary = root / ".status.json"
    build.json_file(temporary, payload)
    temporary.replace(root / "status.json")


def submit(owner: str, root: Path, project: Project, kind: str) -> dict:
    with GUARD:
        if kind not in KINDS:
            raise StudioError(422, "export_kind", "Tipo de exportação desconhecido.")
        if len(ACTIVE) >= 6 or any(job["projectId"] == project.id for job in ACTIVE.values()):
            raise StudioError(409, "queue_busy", "Já há uma tarefa neste projeto ou a fila está cheia.")
        ident = storage.new_id()
        jobs = root / "jobs"
        jobs.mkdir(exist_ok=True)
        work = jobs / ident
        work.mkdir()
        payload = {"id": ident, "kind": kind, "projectId": project.id, "revision": project.revision,
                   "state": "queued", "stage": "Na fila", "progress": 0, "createdAt": storage.now()}
        update_file(work, payload)
        flag = threading.Event()
        ACTIVE[ident] = {**payload, "cancel": flag}
        EXECUTOR.submit(worker, owner, root, project, work, payload, flag)
        return payload.copy()


def worker(owner, root, project, work, payload, cancel):
    started = time.monotonic()
    events = []

    def progress(stage, percent):
        if cancel.is_set():
            raise Cancelled()
        payload["progress"] = max(payload["progress"], percent)
        if payload["stage"] != stage or not events or payload["progress"] >= events[-1]["progress"] + 4:
            payload["stage"] = stage
            event = {"jobId": payload["id"], "projectId": project.id, "stage": stage,
                     "elapsed": round(time.monotonic() - started, 3), "progress": payload["progress"]}
            # Bound stored log size even if extraction/compression alternate.
            events.append(event)
            update_file(work, payload)
            LOG.info("studio_job %s", event)

    def failure(message, code):
        payload.update(state="failed", stage="Falhou", error={"code": code, "message": message})
        report = build_report(project, payload["id"], [item("Estrutura", "error", message)])
        payload["report"] = report
        build.json_file(work / "validation-report.json", report)

    try:
        payload["state"] = "running"
        progress("Iniciando", 1)
        result = build.execute(payload["kind"], project, root, work, payload["id"], progress)
        progress("Concluído", 100)
        payload.update(state="done", **result)
        if result["report"]["status"] == "error":
            payload["stage"] = "Validação encontrou erros"
    except Cancelled:
        payload.update(state="cancelled", stage="Cancelado")
    except StudioError as exc:
        failure(exc.message, exc.code)
    except Exception as exc:
        LOG.error("studio_job_failed id=%s class=%s", payload["id"], type(exc).__name__)
        failure("Não foi possível concluir a geração. Consulte os dados do projeto e tente novamente.", "build_failed")
    finally:
        payload["elapsed"] = round(time.monotonic() - started, 3)
        try:
            if payload["state"] != "done":
                for path in work.iterdir():
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    elif path.name not in {"status.json", "validation-report.json"}:
                        path.unlink(missing_ok=True)
            build.json_file(work / "logs.json", events[-180:])
            with GUARD, storage.LOCK:
                if payload["state"] == "done":
                    _, current = storage.load(owner, project.id)
                    current.exports = [*current.exports[-11:], {k: payload[k] for k in (
                        "id", "kind", "revision", "createdAt", "bytes")}]  # Metadata does not change edit revision.
                    storage.save(root, current)
                update_file(work, payload)
                completed = sorted((d for d in work.parent.iterdir() if d.is_dir() and d != work),
                                   key=lambda d: d.stat().st_mtime, reverse=True)
                for stale in completed[5:]:
                    shutil.rmtree(stale, ignore_errors=True)
        finally:
            with GUARD:
                ACTIVE.pop(payload["id"], None)


def lookup(root: Path, ident: str) -> tuple[Path, dict]:
    import json
    import re

    from .schema import ID_PATTERN

    if not re.fullmatch(ID_PATTERN, ident):
        raise StudioError(404, "job_missing", "Tarefa indisponível.")
    work = root / "jobs" / ident
    with GUARD:
        try:
            payload = json.loads((work / "status.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise StudioError(404, "job_missing", "Tarefa indisponível.") from None
        if payload["state"] in {"queued", "running"} and ident not in ACTIVE:
            payload.update(state="failed", stage="Interrompido pelo reinício")
    return work, payload


def cancel(root: Path, ident: str) -> dict:
    with GUARD:
        _, payload = lookup(root, ident)
        active = ACTIVE.get(ident)
        if active:
            active["cancel"].set()
        return payload
