"""CZAR Forge theme bundle assembly, validation, and export service (/api/forge/*)."""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .. import compositor, jobs, pkg, projects, theme, theme_xml
from ..config import (
    MAX_WAIT_SECONDS,
    MIN_WAIT_SECONDS,
    PKG_DEFAULTS,
    dds_bytes_for,
)
from ..dds import DEFAULT_BACKEND
from .bundle_validator import validate_theme_bundle
from .contracts import BundleValidationReport
from .sfo import build_param_sfo_bytes

router = APIRouter(prefix="/api/forge")


def _hex_to_argb(value: str, fallback: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return fallback
    if len(raw) == 7 and raw.startswith("#"):
        return ("#FF" + raw[1:]).upper()
    return raw.upper()


def resolve_theme_inputs_from_saved_project() -> dict:
    """Read the effective saved project state from disk so exports always use persisted state."""
    ws = jobs.project_workspace.get()
    if ws is None:
        raise HTTPException(400, "Abra e salve um projeto antes de validar ou exportar pelo estado salvo.")
    root = ws.parent
    try:
        manifest = projects.read(root)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, "Não foi possível ler o manifesto salvo do projeto.") from exc
    if manifest.state is None:
        raise HTTPException(400, "O projeto ainda não possui estado salvo. Salve o projeto primeiro.")
    st = manifest.state
    title = (st.campos.title or manifest.name).strip()
    title_id = (st.campos.title_id or PKG_DEFAULTS["title_id"]).strip().upper()
    content_id = (st.campos.content_id or pkg.default_content_id(title_id, title)).strip().upper()
    anim_active = st.bgMode == "animated" and st.anim is not None and bool(st.anim.jobId)
    anim_size = f"{st.anim.size[0]}x{st.anim.size[1]}" if (st.anim and st.anim.size) else ""
    return {
        "title": title,
        "title_id": title_id,
        "content_id": content_id,
        "category": PKG_DEFAULTS["category"],
        "passcode": PKG_DEFAULTS["passcode"],
        "mode": PKG_DEFAULTS["mode"],
        "slots": dict(st.slots),
        "animated_job": st.anim.jobId if anim_active else "",
        "audio_job": st.bgm or "",
        "sfx": dict(st.sfx),
        "frames": st.anim.frames if st.anim else 24,
        "wait_seconds": st.anim.wait if st.anim else 0.1,
        "backend": st.anim.backend if st.anim else DEFAULT_BACKEND,
        "themecolor": int(st.campos.theme_color),
        "fontcolor": _hex_to_argb(st.campos.font, theme_xml.DEFAULTS["fontcolor"]),
        "fontshadowcolor": _hex_to_argb(st.campos.shadow, theme_xml.DEFAULTS["fontshadowcolor"]),
        "focuscolor": _hex_to_argb(st.campos.focus, theme_xml.DEFAULTS["focuscolor"]),
        "composition": st.composition,
        "composition_mode": st.bgMode,
        "animation_size": anim_size,
        "project_name": manifest.name,
        "project_revision": manifest.revision,
    }


def assemble_theme_tree(
    work_dir: Path,
    params: dict,
) -> tuple[Path, Path, pkg.PackageMeta, list[dict], BundleValidationReport]:
    """Assemble full PS4 theme tree + binary param.sfo + theme.gp4 and run CZAR Bundle Validator."""
    from .. import main as app_main

    raw_slots = params["slots"]
    parsed_sfx = params["sfx"]
    state = params.get("composition")
    animated_job = params.get("animated_job") or ""
    audio_job = params.get("audio_job") or ""
    frames = int(params.get("frames") or 24)
    wait_seconds = float(params.get("wait_seconds") or 0.1)
    backend = str(params.get("backend") or DEFAULT_BACKEND)
    has_overlay = bool(state and state.surface("animated"))
    composition_mode = str(params.get("composition_mode") or "")
    animation_size = str(params.get("animation_size") or "")

    if animated_job and has_overlay and not math.isfinite(wait_seconds):
        raise HTTPException(422, "A temporização da composição é inválida.")
    if composition_mode == "animated" and has_overlay and not animated_job:
        raise HTTPException(422, "Selecione uma fonte animada para exportar as sobreposições.")

    slot_jobs = app_main._resolve_slot_jobs({str(k): str(v) for k, v in raw_slots.items()})
    if "home" not in slot_jobs and not (state and state.surface("home")):
        raise HTTPException(
            400,
            "Falta a imagem obrigatória do tema (fundo da tela inicial).",
        )

    frame_count = frames
    effective_anim_size = animation_size if has_overlay else ""
    if animated_job:
        anim_job_obj = app_main._get_job("animated", animated_job)
        anim_size = app_main._composed_animation_size(anim_job_obj, effective_anim_size)
        frame_count = app_main._clamp_frames(frames, anim_size)
        if has_overlay and frame_count != frames:
            raise HTTPException(422, "A composição ultrapassa o orçamento RAF da resolução selecionada.")
        try:
            theme.check_frame_count(frame_count)
            theme.check_budget(frame_count * dds_bytes_for(*anim_size))
        except ValueError as exc:
            raise HTTPException(413, str(exc)) from exc

    theme_settings = {
        "label": str(params["title"]),
        "themecolor": int(params.get("themecolor", 0)),
        "fontcolor": str(params.get("fontcolor") or theme_xml.DEFAULTS["fontcolor"]).strip(),
        "fontshadowcolor": str(params.get("fontshadowcolor") or theme_xml.DEFAULTS["fontshadowcolor"]).strip(),
        "focuscolor": str(params.get("focuscolor") or theme_xml.DEFAULTS["focuscolor"]).strip(),
    }
    try:
        theme_xml.build_theme_xml(**theme_settings)
    except theme_xml.ThemeXmlError as exc:
        raise HTTPException(422, str(exc)) from exc

    bgm_loudness = None
    if audio_job:
        audio_obj = app_main._get_job("audio", audio_job)
        bgm_loudness = audio_obj.meta.get("loudness")
        if bgm_loudness is None:
            conv = audio_obj.meta.get("converted")
            blob = (audio_obj.out_dir / str(conv)) if conv else audio_obj.source
            if blob.is_file():
                bgm_loudness = pkg.calc_loudness(blob)

    meta = pkg.PackageMeta(
        title=str(params["title"]).strip(),
        title_id=str(params["title_id"]).strip().upper(),
        content_id=str(params["content_id"]).strip().upper(),
        category=str(params.get("category") or PKG_DEFAULTS["category"]).strip().lower(),
        passcode=str(params.get("passcode") or PKG_DEFAULTS["passcode"]).strip(),
        mode=str(params.get("mode") or PKG_DEFAULTS["mode"]).strip().lower(),
        bgm_loudness=bgm_loudness,
    )
    try:
        meta.validate()
    except pkg.PackageError as exc:
        raise HTTPException(422, str(exc)) from exc

    content_dir = work_dir / "content"
    content_dir.mkdir(parents=True, exist_ok=True)

    included = app_main._collect_theme_assets(
        slot_jobs,
        theme_settings,
        content_dir,
        audio_job=audio_job,
        sfx_jobs=parsed_sfx,
        animated_job=animated_job,
        frames=frame_count,
        wait_seconds=max(
            MIN_WAIT_SECONDS if has_overlay else 0.01,
            min(MAX_WAIT_SECONDS if has_overlay else 10.0, wait_seconds),
        ),
        backend=backend,
        composition=state,
        direct_slots={key for key, value in raw_slots.items() if value},
        animation_size=effective_anim_size,
    )

    # Generate canonical binary sce_sys/param.sfo + param.sfx + theme.gp4
    sce_sys = content_dir / "sce_sys"
    sce_sys.mkdir(parents=True, exist_ok=True)
    sfo_bytes = build_param_sfo_bytes(meta)
    (sce_sys / "param.sfo").write_bytes(sfo_bytes)
    (work_dir / "param.sfx").write_text(pkg.build_param_sfx(meta), encoding="utf-8")

    targets = sorted(
        p.relative_to(content_dir).as_posix()
        for p in content_dir.rglob("*")
        if p.is_file()
    )
    gp4_path = work_dir / "theme.gp4"
    gp4_path.write_text(pkg.build_gp4(meta, targets, content_dir.name), encoding="utf-8")

    report = validate_theme_bundle(content_dir, meta=meta, gp4_path=gp4_path)
    return content_dir, gp4_path, meta, included, report


def _parse_params(
    *,
    use_saved_project: bool,
    title: str | None,
    title_id: str | None,
    content_id: str | None,
    category: str,
    passcode: str,
    mode: str,
    slots: str | None,
    animated_job: str,
    audio_job: str,
    sfx: str | None,
    frames: int,
    wait_seconds: float,
    backend: str,
    themecolor: int,
    fontcolor: str,
    fontshadowcolor: str,
    focuscolor: str,
    composition: str | None,
    composition_mode: str,
    animation_size: str,
) -> dict:
    if use_saved_project and jobs.project_workspace.get() is not None:
        return resolve_theme_inputs_from_saved_project()
    try:
        raw_slots = json.loads(slots or "{}")
        if not isinstance(raw_slots, dict):
            raise ValueError
    except ValueError as exc:
        raise HTTPException(422, "O campo `slots` deve ser um objeto JSON.") from exc
    try:
        parsed_sfx = {str(k): str(v) for k, v in (json.loads(sfx or "{}") or {}).items()}
    except ValueError as exc:
        raise HTTPException(422, "O campo `sfx` deve ser um objeto JSON.") from exc
    if not title or not title_id or not content_id:
        raise HTTPException(422, "Informe title, title_id e content_id (ou use_saved_project=true).")
    state = compositor.load_composition(composition)
    return {
        "title": title,
        "title_id": title_id,
        "content_id": content_id,
        "category": category,
        "passcode": passcode,
        "mode": mode,
        "slots": raw_slots,
        "animated_job": animated_job,
        "audio_job": audio_job,
        "sfx": parsed_sfx,
        "frames": frames,
        "wait_seconds": wait_seconds,
        "backend": backend,
        "themecolor": themecolor,
        "fontcolor": fontcolor,
        "fontshadowcolor": fontshadowcolor,
        "focuscolor": focuscolor,
        "composition": state,
        "composition_mode": composition_mode,
        "animation_size": animation_size,
    }


def _write_validated_bundle_zip(
    content_dir: Path,
    gp4_path: Path,
    report: BundleValidationReport,
    dest_zip: Path,
) -> Path:
    """Write canonical Image0/ + Sc0/ + theme.gp4 + forge-bundle-report.json atomically."""
    tmp = dest_zip.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in sorted(content_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(content_dir).as_posix()
            if rel in {"sce_sys/param.sfo", "sce_sys/icon0.png", "sce_sys/pic1.png"}:
                zf.write(p, f"Sc0/{Path(rel).name}")
            else:
                zf.write(p, f"Image0/{rel}")
        zf.write(gp4_path, "theme.gp4")
        zf.writestr("forge-bundle-report.json", report.model_dump_json(indent=2))
    tmp.replace(dest_zip)
    return dest_zip


@router.post("/validate")
def forge_validate_theme(
    use_saved_project: bool = Form(True),
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
) -> dict:
    """Assemble into a temp staging dir and validate the complete PS4 theme bundle."""
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
    started = time.time()
    ws = jobs.project_workspace.get() or jobs.WORKSPACE
    ws.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ws, prefix=".validate-") as tmp:
        _cdir, _gp4, meta, included, report = assemble_theme_tree(Path(tmp), params)
    return {
        "status": "completed" if report.valid else "failed",
        "elapsed": round(time.time() - started, 2),
        "content_id": meta.content_id,
        "title_id": meta.title_id,
        "title": meta.title,
        "included": included,
        "report": report.model_dump(),
    }


@router.post("/bundle/export")
def forge_export_bundle(
    use_saved_project: bool = Form(True),
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
    """Assemble, validate, and produce an atomic PS4 theme bundle ZIP (Image0/ + Sc0/ + GP4 + report)."""
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
    started = time.time()
    build_job = jobs.create("package", "bundle.bin", b"\0")
    work_dir = build_job.root / "build"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        content_dir, gp4_path, meta, included, report = assemble_theme_tree(work_dir, params)
        if not report.valid:
            shutil.rmtree(build_job.root, ignore_errors=True)
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "O bundle do tema falhou na validação: " + "; ".join(report.errors),
                    "report": report.model_dump(),
                },
            )
        archive_name = f"{meta.content_id}-bundle.zip"
        archive_path = _write_validated_bundle_zip(content_dir, gp4_path, report, work_dir / archive_name)
        build_job.meta.update(
            {
                "bundle_zip": archive_name,
                "content_id": meta.content_id,
                "report": report.model_dump(),
            }
        )
        build_job.save()
        return {
            "status": "completed",
            "job_id": build_job.id,
            "download": f"/api/forge/bundle/{build_job.id}/download",
            "filename": archive_name,
            "bytes": archive_path.stat().st_size,
            "elapsed": round(time.time() - started, 2),
            "content_id": meta.content_id,
            "included": included,
            "report": report.model_dump(),
        }
    except HTTPException:
        shutil.rmtree(build_job.root, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(build_job.root, ignore_errors=True)
        raise HTTPException(503, "Não foi possível gravar o bundle validado no disco.") from exc


@router.get("/bundle/{job_id}/download")
def forge_download_bundle(job_id: str) -> FileResponse:
    job = jobs.load("package", job_id)
    if job is None:
        raise HTTPException(404, "Bundle não encontrado.")
    name = str(job.meta.get("bundle_zip") or "")
    path = job.root / "build" / name
    if not name or Path(name).name != name or not path.is_file():
        raise HTTPException(404, "O arquivo do bundle não está mais disponível.")
    return FileResponse(path, media_type="application/zip", filename=name, headers={"Cache-Control": "private, no-store"})
