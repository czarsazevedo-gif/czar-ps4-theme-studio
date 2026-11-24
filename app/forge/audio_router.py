"""CZAR ATRAC9 Studio additive FastAPI router (/api/forge/audio/*).

Provides audio inspection, loop analysis/finishing, loudness/volume previewing,
and end-to-end "CONVERTER E APLICAR MÚSICA" ATRAC9 conversion & persistence.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from .. import at9, jobs, library, media, projects
from ..config import AUDIO_SLOTS, MAX_UPLOAD_BYTES, SESSION_MEDIA_TTL_SECONDS, SOUND_EXTS
from .audio_loop import (
    analyze_audio_loop,
    evaluate_post_at9_loop,
    finish_audio_loop,
    render_loop_seam_preview_wav,
)
from .audio_validation import (
    VGMSTREAM_CLI,
    compare_roundtrip_audio,
    decode_to_float64_stereo,
    measure_loudness,
    validate_at9,
    write_pcm16_wav,
)
from .audio_volume import (
    BROWSER_VS_PS4_LOUDNESS_NOTE,
    apply_volume_mode,
    clamp_gain_db,
)
from .contracts import (
    PS4_ATRAC9_BITRATE_KBPS,
    PS4_ATRAC9_SAMPLE_RATE,
    PS4_BGM_REFERENCE_LUFS,
    VolumeMode,
)

router = APIRouter(prefix="/api/forge/audio")

MUSIC_INPUT_EXTS = SOUND_EXTS - {".vag"}
TOKEN_RE = re.compile(r"^[0-9a-f]{24}$")
ALLOWED_PREVIEW_FILES = {"source.wav", "processed.wav", "seam.wav"}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _staging_root() -> Path:
    ws = jobs.project_workspace.get() or jobs.WORKSPACE
    root = ws / "_forge_audio_stage"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _prune_old_stages(root: Path, max_age_sec: float = float(SESSION_MEDIA_TTL_SECONDS)) -> None:
    now = time.time()
    try:
        for child in root.iterdir():
            if not child.is_dir() or not TOKEN_RE.fullmatch(child.name):
                continue
            try:
                if now - child.stat().st_mtime > max_age_sec:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def touch_audio_stages(ws: Path | None = None) -> int:
    """Refresh mtime of active audio staging folders during keep-alive so active sessions never expire."""
    base = ws or jobs.project_workspace.get() or jobs.WORKSPACE
    root = base / "_forge_audio_stage"
    if not root.is_dir():
        return 0
    touched = 0
    now = time.time()
    try:
        for child in root.iterdir():
            if child.is_dir() and TOKEN_RE.fullmatch(child.name):
                try:
                    os.utime(child, (now, now))
                    touched += 1
                except OSError:
                    pass
    except OSError:
        pass
    return touched


def _stage_dir(token: str) -> Path:
    if not TOKEN_RE.fullmatch(token or ""):
        raise HTTPException(404, "Prévia de áudio não encontrada.")
    folder = _staging_root() / token
    if not folder.is_dir():
        raise HTTPException(404, "Prévia de áudio expirada ou inexistente.")
    try:
        os.utime(folder, None)
    except OSError:
        pass
    return folder


def _stage_source_file(folder: Path) -> tuple[Path, str]:
    meta_file = folder / "meta.txt"
    if not meta_file.is_file():
        raise HTTPException(404, "Fonte de áudio da prévia não encontrada.")
    lines = meta_file.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        raise HTTPException(404, "Metadados da prévia corrompidos.")
    src_name, original_name = lines[0], lines[1]
    if Path(src_name).name != src_name:
        raise HTTPException(404, "Caminho de prévia inválido.")
    src_path = folder / src_name
    if not src_path.is_file():
        raise HTTPException(404, "Arquivo de áudio de origem ausente.")
    return src_path, original_name


def _validate_music_filename(filename: str | None) -> tuple[str, str]:
    raw_name = (filename or "audio.wav").replace("\\", "/").split("/")[-1].strip() or "audio.wav"
    suffix = Path(raw_name).suffix.lower()
    if suffix not in MUSIC_INPUT_EXTS:
        raise HTTPException(
            415,
            f"Formato não suportado para música: {suffix or '(sem extensão)'}. "
            f"Aceitos: {', '.join(sorted(MUSIC_INPUT_EXTS))}",
        )
    return raw_name, suffix


async def _stream_music_upload(file: UploadFile, dest_dir: Path) -> tuple[str, str, Path]:
    raw_name, suffix = _validate_music_filename(file.filename)
    dest = dest_dir / f"original{suffix}"
    await jobs.stream_upload_to_path(file, dest, MAX_UPLOAD_BYTES)
    return raw_name, suffix, dest



def _resolve_input_source(
    request: Request,
    *,
    staging_id: str | None = None,
    library_id: str | None = None,
    job_id: str | None = None,
) -> tuple[Path, str, bool]:
    """Return (source_path, original_name, is_immutable_library_source)."""
    if staging_id:
        folder = _stage_dir(staging_id)
        src, name = _stage_source_file(folder)
        return src, name, False
    if library_id:
        asset, source = library.get_asset(request, library_id)
        if asset.kind != "audio" or asset.extension not in MUSIC_INPUT_EXTS:
            raise HTTPException(415, "A mídia selecionada na biblioteca não é um áudio compatível com música.")
        return source, asset.originalName, True
    if job_id:
        job = jobs.load("audio", job_id)
        if job is None:
            raise HTTPException(404, "Trabalho de áudio não encontrado neste projeto.")
        return job.source, job.original_name, False
    raise HTTPException(400, "Envie um arquivo de áudio ou informe staging_id, library_id ou job_id.")


def ensure_audio_job_previews(job: jobs.Job) -> tuple[Path, Path | None]:
    """Generate cached Chrome-playable WAV preview (and seam preview for BGM) for an audio job."""
    converted = job.meta.get("converted")
    blob = (job.out_dir / str(converted)) if converted else job.source
    if not blob.is_file():
        raise HTTPException(404, "O arquivo de áudio processado não existe mais.")
    job.preview_dir.mkdir(parents=True, exist_ok=True)
    preview_wav = job.preview_dir / "preview.wav"
    seam_wav = job.preview_dir / "seam.wav"
    slot_id = str(job.meta.get("slot") or "bgm")

    # Regenerate if missing or older than the audio blob
    blob_mtime = blob.stat().st_mtime
    if not preview_wav.is_file() or preview_wav.stat().st_mtime < blob_mtime:
        pcm = decode_to_float64_stereo(blob, sample_rate=PS4_ATRAC9_SAMPLE_RATE, max_seconds=at9.MAX_SECONDS)
        write_pcm16_wav(pcm, preview_wav, sample_rate=PS4_ATRAC9_SAMPLE_RATE)
        if slot_id == "bgm":
            render_loop_seam_preview_wav(pcm, seam_wav, sr=PS4_ATRAC9_SAMPLE_RATE)
    elif slot_id == "bgm" and (not seam_wav.is_file() or seam_wav.stat().st_mtime < blob_mtime):
        pcm = decode_to_float64_stereo(blob, sample_rate=PS4_ATRAC9_SAMPLE_RATE, max_seconds=at9.MAX_SECONDS)
        render_loop_seam_preview_wav(pcm, seam_wav, sr=PS4_ATRAC9_SAMPLE_RATE)

    return preview_wav, (seam_wav if slot_id == "bgm" and seam_wav.is_file() else None)


@router.get("/status")
def forge_audio_status() -> dict:
    enc = at9.encoder_info()
    return {
        "encoder": enc,
        "at9_available": enc["available"],
        "validator": "ffmpeg+vgmstream+riff" if VGMSTREAM_CLI else "ffmpeg+riff",
        "ffmpeg": bool(media.FFMPEG),
        "ffprobe": bool(media.FFPROBE),
        "vgmstream": bool(VGMSTREAM_CLI),
        "vag_available": True,
        "accepted_music_exts": sorted(MUSIC_INPUT_EXTS),
        "ps4_spec": {
            "sample_rate": PS4_ATRAC9_SAMPLE_RATE,
            "channels": 2,
            "bitrate_kbps": PS4_ATRAC9_BITRATE_KBPS,
            "byte_rate": 18000,
            "block_align": 384,
            "config_data": "fe740bf0",
            "reference_lufs": PS4_BGM_REFERENCE_LUFS,
            "max_seconds": at9.MAX_SECONDS,
        },
        "browser_vs_ps4_note": BROWSER_VS_PS4_LOUDNESS_NOTE,
    }


def inspect_audio_file_path(src_file: Path, original_name: str, *, move_source: bool = False) -> dict:
    """Inspect an on-disk audio file, stage playable WAV & seam previews, and return metrics."""
    clean_name, suffix = _validate_music_filename(original_name)
    root = _staging_root()
    _prune_old_stages(root)
    token = uuid.uuid4().hex[:24]
    stage = root / token
    stage.mkdir(parents=True, exist_ok=True)
    try:
        src_path = stage / f"original{suffix}"
        if move_source:
            shutil.move(str(src_file), str(src_path))
        else:
            shutil.copyfile(src_file, src_path)
        return _finish_audio_inspect_stage(stage, token, src_path, clean_name)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _finish_audio_inspect_stage(stage: Path, token: str, src_path: Path, original_name: str) -> dict:
    try:
        (stage / "meta.txt").write_text(f"{src_path.name}\n{original_name}\n", encoding="utf-8")

        # Measure immutable source identity BEFORE generating any derived PCM preview!
        original_bytes = src_path.stat().st_size
        original_sha256 = _sha256_file(src_path)
        info = media.probe_audio(src_path)
        at9_validation = None
        if src_path.suffix.lower() == ".at9" or info.codec == "atrac9":
            at9_validation = validate_at9(src_path, require_ps4_profile=True, require_loop=False).model_dump()
            if src_path.suffix.lower() == ".at9" and info.codec != "atrac9":
                raise HTTPException(
                    422,
                    f"Arquivo .at9 inválido: codec detectado '{info.codec}' em vez de ATRAC9 real.",
                )

        pcm = decode_to_float64_stereo(src_path, sample_rate=PS4_ATRAC9_SAMPLE_RATE, max_seconds=at9.MAX_SECONDS)
        preview_orig = stage / "preview_original.wav"
        write_pcm16_wav(pcm, preview_orig, sample_rate=PS4_ATRAC9_SAMPLE_RATE)
        preview_sha256 = _sha256_file(preview_orig)
        render_loop_seam_preview_wav(pcm, stage / "seam.wav", sr=PS4_ATRAC9_SAMPLE_RATE)

        loud = measure_loudness(src_path)
        loop_analysis = analyze_audio_loop(pcm, sr=PS4_ATRAC9_SAMPLE_RATE)
        stamp = int(time.time() * 1000)
        return {
            "staging_id": token,
            "name": original_name,
            "extension": src_path.suffix.lower(),
            "bytes": original_bytes,
            "original_bytes": original_bytes,
            "sha256": original_sha256,
            "original_sha256": original_sha256,
            "preview_sha256": preview_sha256,
            "codec": info.codec,
            "sample_rate": info.rate,
            "channels": info.channels,
            "bitrate_bps": info.bitrate,
            "duration": round(info.duration or (pcm.shape[0] / float(PS4_ATRAC9_SAMPLE_RATE)), 4),
            "at9_validation": at9_validation,
            "loudness": loud.model_dump(),
            "loop_analysis": loop_analysis,
            "preview_url": f"/api/forge/audio/preview/{token}/preview_original.wav?v={stamp}",
            "seam_preview_url": f"/api/forge/audio/preview/{token}/seam.wav?v={stamp}",
            "can_convert": at9.available(),
            "browser_vs_ps4_note": BROWSER_VS_PS4_LOUDNESS_NOTE,
        }
    except HTTPException:
        raise
    except media.MediaError as exc:
        raise HTTPException(422, f"Não foi possível decodificar o áudio: {exc}") from exc
    except OSError as exc:
        raise HTTPException(503, "Não foi possível gravar os dados temporários do áudio.") from exc


@router.post("/inspect")
async def forge_audio_inspect(
    request: Request,
    file: UploadFile | None = File(None),
    library_id: str | None = Form(None),
    job_id: str | None = Form(None),
) -> dict:
    """Inspect real format, integrity, loudness, and loop suitability; stage playable preview."""
    root = _staging_root()
    _prune_old_stages(root)
    token = uuid.uuid4().hex[:24]
    stage = root / token
    stage.mkdir(parents=True, exist_ok=True)
    try:
        if file is not None:
            original_name, _suffix, src_path = await _stream_music_upload(file, stage)
        else:
            resolved, original_name, _is_lib = _resolve_input_source(
                request, library_id=library_id, job_id=job_id
            )
            suffix = resolved.suffix.lower() or ".wav"
            src_path = stage / f"original{suffix}"
            shutil.copyfile(resolved, src_path)

        return _finish_audio_inspect_stage(stage, token, src_path, original_name)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise



@router.post("/preview-process")
def forge_audio_preview_process(
    request: Request,
    staging_id: str | None = Form(None),
    library_id: str | None = Form(None),
    job_id: str | None = Form(None),
    loop_mode: Literal["whole", "manual_excerpt", "auto_refined"] = Form("whole"),
    start_sec: float | None = Form(None),
    end_sec: float | None = Form(None),
    start_sample: int | None = Form(None),
    end_sample: int | None = Form(None),
    trim_silence: bool = Form(False),
    crossfade_ms: float = Form(0.0),
    allow_fade: bool = Form(False),
    fade_in_ms: float = Form(0.0),
    fade_out_ms: float = Form(0.0),
    volume_mode: VolumeMode = Form("original"),
    gain_db: float = Form(0.0),
    target_lufs: float | None = Form(None),
) -> dict:
    """Process a non-destructive preview of loop finishing + volume leveling for listening before applying."""
    src_path, original_name, _is_lib = _resolve_input_source(
        request, staging_id=staging_id, library_id=library_id, job_id=job_id
    )
    if staging_id:
        token = staging_id
        stage = _stage_dir(token)
    else:
        root = _staging_root()
        _prune_old_stages(root)
        token = uuid.uuid4().hex[:24]
        stage = root / token
        stage.mkdir(parents=True, exist_ok=True)
        copy_src = stage / f"original{src_path.suffix.lower() or '.wav'}"
        shutil.copyfile(src_path, copy_src)
        (stage / "meta.txt").write_text(f"{copy_src.name}\n{original_name}\n", encoding="utf-8")
        src_path = copy_src

    pcm_raw = decode_to_float64_stereo(src_path, sample_rate=PS4_ATRAC9_SAMPLE_RATE, max_seconds=at9.MAX_SECONDS)
    preview_orig = stage / "preview_original.wav"
    if not preview_orig.is_file():
        write_pcm16_wav(pcm_raw, preview_orig, sample_rate=PS4_ATRAC9_SAMPLE_RATE)

    try:
        looped, loop_report = finish_audio_loop(
            pcm_raw,
            sr=PS4_ATRAC9_SAMPLE_RATE,
            mode=loop_mode,
            start_sec=start_sec,
            end_sec=end_sec,
            start_sample=start_sample,
            end_sample=end_sample,
            trim_silence=trim_silence,
            crossfade_ms=crossfade_ms,
            allow_fade=allow_fade,
            fade_in_ms=fade_in_ms,
            fade_out_ms=fade_out_ms,
        )
        leveled, vol_params, before_loud, after_loud = apply_volume_mode(
            looped,
            mode=volume_mode,
            gain_db=gain_db,
            target_lufs=target_lufs,
            sr=PS4_ATRAC9_SAMPLE_RATE,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    write_pcm16_wav(leveled, stage / "processed.wav", sample_rate=PS4_ATRAC9_SAMPLE_RATE)
    render_loop_seam_preview_wav(leveled, stage / "seam.wav", sr=PS4_ATRAC9_SAMPLE_RATE)
    stamp = int(time.time() * 1000)

    return {
        "staging_id": token,
        "name": original_name,
        "original_sha256": _sha256_file(src_path),
        "loop_report": loop_report.model_dump(),
        "volume_params": vol_params,
        "before_loudness": before_loud.model_dump(),
        "after_loudness": after_loud.model_dump(),
        "original_preview_url": f"/api/forge/audio/preview/{token}/preview_original.wav?v={stamp}",
        "preview_url": f"/api/forge/audio/preview/{token}/processed.wav?v={stamp}",
        "seam_preview_url": f"/api/forge/audio/preview/{token}/seam.wav?v={stamp}",
        "browser_vs_ps4_note": BROWSER_VS_PS4_LOUDNESS_NOTE,
    }


@router.get("/preview/{token}/{name}")
def forge_audio_preview_file(token: str, name: str) -> FileResponse:
    if name not in (ALLOWED_PREVIEW_FILES | {"preview_original.wav"}):
        raise HTTPException(404, "Arquivo de prévia inválido.")
    folder = _stage_dir(token)
    target = folder / name
    if name == "source.wav" and not target.is_file():
        target = folder / "preview_original.wav"
    if not target.is_file():
        raise HTTPException(404, "Prévia ainda não gerada.")
    return FileResponse(target, media_type="audio/wav", headers={"Cache-Control": "private, no-store"})


def convert_apply_from_job(
    job: jobs.Job,
    *,
    library_id: str | None = None,
    loop_enabled: bool = True,
    loop_mode: Literal["whole", "manual_excerpt", "auto_refined"] = "whole",
    start_sec: float | None = None,
    end_sec: float | None = None,
    start_sample: int | None = None,
    end_sample: int | None = None,
    trim_silence: bool = False,
    crossfade_ms: float = 0.0,
    allow_fade: bool = False,
    fade_in_ms: float = 0.0,
    fade_out_ms: float = 0.0,
    volume_mode: VolumeMode = "original",
    gain_db: float = 0.0,
    target_lufs: float | None = None,
) -> dict:
    """Process an already-created audio job through Forge loop finishing, loudness, and ATRAC9 encoding."""
    spec = AUDIO_SLOTS["bgm"]
    src_info = media.probe_audio(job.source)
    orig_sha256 = _sha256_file(job.source)
    job.meta["slot"] = "bgm"
    job.meta["original_sha256"] = orig_sha256
    job.meta["source_codec"] = src_info.codec
    if library_id:
        job.meta["library_id"] = library_id

    wants_processing = (
        loop_mode != "whole"
        or start_sec is not None
        or end_sec is not None
        or start_sample is not None
        or end_sample is not None
        or trim_silence
        or crossfade_ms > 0.0
        or (allow_fade and (fade_in_ms > 0.0 or fade_out_ms > 0.0))
        or volume_mode != "original"
        or abs(clamp_gain_db(gain_db)) > 0.01
    )

    # Pass-through if input is already a strictly valid PS4 48kHz 144kbps ATRAC9 and untouched
    passthrough = False
    if src_info.codec == "atrac9" and not wants_processing:
        pre_val = validate_at9(job.source, require_ps4_profile=True, require_loop=loop_enabled)
        if pre_val.valid:
            passthrough = True
            target_at9 = job.source
            validation = pre_val
            pcm_decoded = decode_to_float64_stereo(
                target_at9, sample_rate=PS4_ATRAC9_SAMPLE_RATE, max_seconds=at9.MAX_SECONDS
            )
            _, loop_report = finish_audio_loop(pcm_decoded, sr=PS4_ATRAC9_SAMPLE_RATE, mode="whole")
            vol_params = {
                "mode": "original",
                "requested_gain_db": 0.0,
                "applied_gain_db": 0.0,
                "target_lufs": None,
                "reference_lufs": PS4_BGM_REFERENCE_LUFS,
                "limiter_applied": False,
                "browser_vs_ps4_note": BROWSER_VS_PS4_LOUDNESS_NOTE,
            }
            roundtrip = None
            job.meta.pop("converted", None)

    if not passthrough:
        if not at9.available():
            raise HTTPException(
                422,
                "Codificador ATRAC9 não disponível neste servidor. Execute "
                "./scripts/install_forge_tools.sh at9 ou envie um arquivo .at9 PS4 válido (48 kHz, 144 kbps).",
            )
        pcm_raw = decode_to_float64_stereo(
            job.source, sample_rate=PS4_ATRAC9_SAMPLE_RATE, max_seconds=at9.MAX_SECONDS
        )
        looped, loop_report = finish_audio_loop(
            pcm_raw,
            sr=PS4_ATRAC9_SAMPLE_RATE,
            mode=loop_mode,
            start_sec=start_sec,
            end_sec=end_sec,
            start_sample=start_sample,
            end_sample=end_sample,
            trim_silence=trim_silence,
            crossfade_ms=crossfade_ms,
            allow_fade=allow_fade,
            fade_in_ms=fade_in_ms,
            fade_out_ms=fade_out_ms,
        )
        leveled, vol_params, _before_loud, _after_loud = apply_volume_mode(
            looped,
            mode=volume_mode,
            gain_db=gain_db,
            target_lufs=target_lufs,
            sr=PS4_ATRAC9_SAMPLE_RATE,
        )

        target_at9 = job.out_dir / "bgm.at9"
        at9.encode_pcm(
            leveled,
            target_at9,
            bitrate=PS4_ATRAC9_BITRATE_KBPS,
            loop=loop_enabled,
            validate=True,
        )
        validation = validate_at9(target_at9, require_ps4_profile=True, require_loop=loop_enabled)
        if not validation.valid:
            raise HTTPException(
                422,
                "Validação independente do ATRAC9 falhou: " + "; ".join(validation.errors),
            )
        roundtrip = compare_roundtrip_audio(leveled, target_at9, sample_rate=PS4_ATRAC9_SAMPLE_RATE)
        job.meta["converted"] = target_at9.name

    post_at9_loop = evaluate_post_at9_loop(target_at9, sr=PS4_ATRAC9_SAMPLE_RATE)
    loud_report = measure_loudness(target_at9)
    conv_sha256 = _sha256_file(target_at9)
    derived_id = conv_sha256[:16]
    enc_info = at9.encoder_info()

    effective_gain = float(vol_params.get("applied_gain_db") or 0.0)
    job.meta.update(
        {
            "slot": "bgm",
            "gain_db": clamp_gain_db(effective_gain),
            "volume_mode": vol_params["mode"],
            "target_lufs": vol_params.get("target_lufs"),
            "loudness": loud_report.integrated_lufs,
            "loudness_report": loud_report.model_dump(),
            "codec": validation.codec,
            "rate": validation.sample_rate,
            "channels": validation.channels,
            "bitrate_kbps": PS4_ATRAC9_BITRATE_KBPS,
            "duration": round(validation.duration_seconds, 2),
            "original_sha256": orig_sha256,
            "converted_sha256": conv_sha256,
            "derived_id": derived_id,
            "loop_enabled": loop_enabled,
            "loop_start_sample": validation.loop_start_sample,
            "loop_end_sample": validation.loop_end_sample,
            "play_count": validation.play_count,
            "loop_report": loop_report.model_dump(),
            "post_at9_loop": post_at9_loop,
            "validation": validation.model_dump(),
            "roundtrip": roundtrip,
            "encoder_backend": "passthrough-at9" if passthrough else enc_info["backend"],
            "conversion_params": {
                "loop_enabled": loop_enabled,
                "loop_mode": loop_mode,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "start_sample": start_sample,
                "end_sample": end_sample,
                "trim_silence": trim_silence,
                "crossfade_ms": crossfade_ms,
                "allow_fade": allow_fade,
                "fade_in_ms": fade_in_ms,
                "fade_out_ms": fade_out_ms,
                "volume_mode": volume_mode,
                "gain_db": gain_db,
                "target_lufs": target_lufs,
            },
        }
    )
    job.save()
    ensure_audio_job_previews(job)
    projects.mirror_job_to_project_folder(job, slot_id="bgm")
    stamp = int(time.time() * 1000)

    return {
        "slot": "bgm",
        "job_id": job.id,
        "path": spec["path"],
        "name": job.original_name,
        "codec": validation.codec,
        "rate": validation.sample_rate,
        "channels": validation.channels,
        "bitrate_kbps": PS4_ATRAC9_BITRATE_KBPS,
        "duration": round(validation.duration_seconds, 2),
        "bytes": target_at9.stat().st_size,
        "source_codec": src_info.codec,
        "loudness": loud_report.integrated_lufs,
        "loudness_report": loud_report.model_dump(),
        "slot_label": spec["label"],
        "gain_db": job.meta["gain_db"],
        "volume_mode": job.meta["volume_mode"],
        "volume_params": vol_params,
        "loop_report": loop_report.model_dump(),
        "post_at9_loop": post_at9_loop,
        "validation": validation.model_dump(),
        "roundtrip": roundtrip,
        "derived_id": derived_id,
        "original_sha256": orig_sha256,
        "converted_sha256": conv_sha256,
        "encoder_backend": job.meta["encoder_backend"],
        "passthrough": passthrough,
        "preview": f"/api/audio/{job.id}/preview?v={stamp}",
        "seam_preview": f"/api/audio/{job.id}/seam-preview?v={stamp}",
    }


def convert_apply_from_path(src_path: Path, original_name: str, *, move_source: bool = False, **kwargs) -> dict:
    clean_name, _suffix = _validate_music_filename(original_name)
    job = jobs.create_from_path("audio", clean_name, src_path, move=move_source)
    try:
        return convert_apply_from_job(job, **kwargs)
    except HTTPException:
        shutil.rmtree(job.root, ignore_errors=True)
        raise
    except (media.MediaError, ValueError) as exc:
        shutil.rmtree(job.root, ignore_errors=True)
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        shutil.rmtree(job.root, ignore_errors=True)
        raise HTTPException(503, "Falha de armazenamento ao salvar o áudio convertido.") from exc


@router.post("/convert-apply")
async def forge_audio_convert_apply(
    request: Request,
    file: UploadFile | None = File(None),
    staging_id: str | None = Form(None),
    library_id: str | None = Form(None),
    job_id: str | None = Form(None),
    loop_enabled: bool = Form(True),
    loop_mode: Literal["whole", "manual_excerpt", "auto_refined"] = Form("whole"),
    start_sec: float | None = Form(None),
    end_sec: float | None = Form(None),
    start_sample: int | None = Form(None),
    end_sample: int | None = Form(None),
    trim_silence: bool = Form(False),
    crossfade_ms: float = Form(0.0),
    allow_fade: bool = Form(False),
    fade_in_ms: float = Form(0.0),
    fade_out_ms: float = Form(0.0),
    volume_mode: VolumeMode = Form("original"),
    gain_db: float = Form(0.0),
    target_lufs: float | None = Form(None),
) -> dict:
    """End-to-end 'CONVERTER E APLICAR MÚSICA': inspect, finish loop, level volume,
    encode real 48 kHz 144 kbps ATRAC9, independently validate, and persist into project workspace."""
    job: jobs.Job | None = None
    created_new = False

    try:
        if file is not None:
            original_name, _suffix = _validate_music_filename(file.filename)
            job = await jobs.create_streaming("audio", original_name, file, MAX_UPLOAD_BYTES)
            created_new = True
        else:
            src_path, original_name, is_lib = _resolve_input_source(
                request, staging_id=staging_id, library_id=library_id, job_id=job_id
            )
            job = jobs.create_from_path("audio", original_name, src_path, move=False)
            created_new = True

        return convert_apply_from_job(
            job,
            library_id=library_id,
            loop_enabled=loop_enabled,
            loop_mode=loop_mode,
            start_sec=start_sec,
            end_sec=end_sec,
            start_sample=start_sample,
            end_sample=end_sample,
            trim_silence=trim_silence,
            crossfade_ms=crossfade_ms,
            allow_fade=allow_fade,
            fade_in_ms=fade_in_ms,
            fade_out_ms=fade_out_ms,
            volume_mode=volume_mode,
            gain_db=gain_db,
            target_lufs=target_lufs,
        )
    except HTTPException:
        if created_new and job is not None:
            shutil.rmtree(job.root, ignore_errors=True)
        raise
    except (media.MediaError, ValueError) as exc:
        if created_new and job is not None:
            shutil.rmtree(job.root, ignore_errors=True)
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        if created_new and job is not None:
            shutil.rmtree(job.root, ignore_errors=True)
        raise HTTPException(503, "Falha de armazenamento ao salvar o áudio convertido.") from exc

