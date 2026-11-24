"""Private per-session catalog of immutable sources for canonical jobs."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import icon_mapper, media, projects
from .config import (
    IMAGE_EXTS,
    MAX_UPLOAD_BYTES,
    MAX_VIDEO_UPLOAD_BYTES,
    MEDIA_FOLDERS,
    MEDIA_FOLDER_BY_ID,
    MEDIA_FOLDER_IDS,
    SOUND_EXTS,
    VIDEO_EXTS,
)

router = APIRouter(prefix="/api/library")
EXTENSIONS = IMAGE_EXTS | VIDEO_EXTS | SOUND_EXTS
THUMB = "thumb.webp"
NO_CACHE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
ID = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Category = Literal["image", "video", "animation", "audio"]
MediaFolder = Literal["content_icons", "function_icons", "audio", "theme_image", "animated_bg"]

# Match the actual demuxer to an extension already accepted by the editor.
# The demuxer allowlist also keeps playlists and external input indirection
# away from the canonical pipelines when a source comes from the library.
FORMATS = {
    ".png": ("png_pipe apng", "PNG", "image/png"),
    ".jpg": ("jpeg_pipe image2 mjpeg", "JPEG", "image/jpeg"),
    ".jpeg": ("jpeg_pipe image2 mjpeg", "JPEG", "image/jpeg"),
    ".gif": ("gif", "GIF", "image/gif"),
    ".apng": ("apng png_pipe", "APNG", "image/apng"),
    ".bmp": ("bmp_pipe", "BMP", "image/bmp"),
    ".webp": ("webp_pipe", "WebP", "image/webp"),
    ".tif": ("tiff_pipe", "TIFF", "image/tiff"),
    ".tiff": ("tiff_pipe", "TIFF", "image/tiff"),
    ".dds": ("dds_pipe", "DDS", "image/vnd-ms.dds"),
    ".svg": ("svg", "SVG", "image/svg+xml"),
    ".mp4": ("mov", "ISO BMFF", "video/mp4"),
    ".m4v": ("mov m4v", "M4V", "video/mp4"),
    ".mov": ("mov", "QuickTime", "video/quicktime"),
    ".mkv": ("matroska", "Matroska", "video/x-matroska"),
    ".webm": ("matroska", "Matroska/WebM", "video/x-matroska"),
    ".avi": ("avi", "AVI", "video/x-msvideo"),
    ".wmv": ("asf asf_o", "ASF", "video/x-ms-asf"),
    ".flv": ("flv live_flv", "FLV", "video/x-flv"),
    ".wav": ("wav", "WAV", "audio/wav"),
    ".at9": ("wav", "ATRAC9", "audio/wav"),
    ".mp3": ("mp3", "MP3", "audio/mpeg"),
    ".ogg": ("ogg", "Ogg", "audio/ogg"),
    ".opus": ("ogg", "Ogg", "audio/ogg"),
    ".flac": ("flac", "FLAC", "audio/flac"),
    ".m4a": ("mov", "ISO BMFF", "audio/mp4"),
    ".aac": ("aac loas", "AAC", "audio/aac"),
    ".wma": ("asf asf_o", "ASF", "audio/x-ms-asf"),
    ".vag": ("vag svag", "VAG", "application/octet-stream"),
}


def infer_folder(kind: str, original_name: str, requested_folder: str | None = None) -> MediaFolder:
    """Resolve an asset's target folder among the 5 dedicated media folders."""
    req = (requested_folder or "").strip()
    if req:
        if req not in MEDIA_FOLDER_BY_ID:
            raise HTTPException(422, f"Pasta de mídia inválida: {req}")
        spec = MEDIA_FOLDER_BY_ID[req]
        if kind not in spec["accept_kinds"]:
            raise HTTPException(
                415,
                f"Este arquivo ({kind}) não é compatível com a pasta '{spec['label']['pt-BR']}'.",
            )
        return req  # type: ignore[return-value]

    if kind == "audio":
        return "audio"
    if kind in {"video", "animation"}:
        return "animated_bg"
    slot_id, _is_glow = icon_mapper.resolve_icon_filename(original_name or "")
    if slot_id:
        if slot_id.startswith("fn:"):
            return "function_icons"
        if slot_id.startswith("ct:"):
            return "content_icons"
    return "theme_image"


class Asset(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schemaVersion: Literal[1] = 1
    id: ID
    sha256: ID
    originalName: str
    displayName: str
    extension: str
    kind: Category
    folder: MediaFolder | None = None
    format: str
    mime: str
    bytes: int
    importedAt: datetime
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    fps: float | None = None
    codec: str
    validation: Literal["verified"] = "verified"
    hasThumbnail: bool
    normalized: dict | None = None

    def effective_folder(self) -> MediaFolder:
        if self.folder in MEDIA_FOLDER_BY_ID:
            return self.folder  # type: ignore[return-value]
        return infer_folder(self.kind, self.originalName)

    def public(self) -> dict:
        eff_folder = self.effective_folder()
        data = self.model_dump(mode="json")
        data["folder"] = eff_folder
        data["folderSubdir"] = MEDIA_FOLDER_BY_ID[eff_folder]["subdir"]
        return {
            **data,
            "original": f"/api/library/{self.id}/original",
            "thumbnail": f"/api/library/{self.id}/thumbnail" if self.hasThumbnail else None,
        }


class FolderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder: MediaFolder


def catalog(request: Request) -> Path:
    return projects.owner_path(request) / "library-v1"


def storage_error():
    return HTTPException(503, "Não foi possível acessar o armazenamento da biblioteca.")


def _ensure_library_folder_mirrors(root: Path, asset: Asset, source: Path) -> None:
    """Maintain organized hardlink view in `<owner>/library-v1/folders/<subdir>/`."""
    try:
        folders_root = root / "folders"
        for spec in MEDIA_FOLDERS:
            (folders_root / spec["subdir"]).mkdir(parents=True, exist_ok=True)
        # Remove stale mirrors of this asset ID in other folders (e.g. after folder change)
        prefix = f"{asset.id[:12]}-"
        for spec in MEDIA_FOLDERS:
            sub = folders_root / spec["subdir"]
            for existing in sub.glob(f"{prefix}*"):
                if existing.is_file():
                    existing.unlink(missing_ok=True)
        eff = asset.effective_folder()
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(asset.originalName or f"media{asset.extension}").name).strip("._")
        if not safe.lower().endswith(asset.extension):
            safe = f"{Path(safe).stem or 'media'}{asset.extension}"
        target = folders_root / MEDIA_FOLDER_BY_ID[eff]["subdir"] / f"{prefix}{safe[:80]}"
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)
    except OSError:
        pass


def read_asset(root: Path, ident: str) -> tuple[Asset, Path]:
    if not re.fullmatch(r"[0-9a-f]{64}", ident) or not (root / ident).is_dir():
        raise HTTPException(404, "Mídia não encontrada nesta sessão.")
    directory = root / ident
    asset = Asset.model_validate_json((directory / "asset.json").read_bytes())
    if asset.id != ident or asset.sha256 != ident or asset.extension not in EXTENSIONS:
        raise ValueError("Invalid asset reference")
    source = directory / f"source{asset.extension}"
    if not source.is_file() or source.stat().st_size != asset.bytes:
        raise ValueError("Missing or incomplete source")
    return asset, source


def get_asset(request: Request, ident: str) -> tuple[Asset, Path]:
    try:
        return read_asset(catalog(request), ident)
    except (OSError, ValueError) as exc:
        raise storage_error() from exc


def run_checked(args: list[str], *, capture: bool = False) -> bytes:
    try:
        return subprocess.run(
            args, check=True, timeout=90, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).stdout or b""
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(422, "A verificação da mídia excedeu o tempo disponível.") from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(422, "Não foi possível decodificar a mídia. Confira o formato e a integridade.") from exc


def positive(value) -> float | None:
    try:
        number = float(Fraction(value))
    except (ValueError, ZeroDivisionError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def webm_header(source: Path) -> bool:
    with source.open("rb") as stream:
        header = stream.read(4096)
    if not header.startswith(b"\x1a\x45\xdf\xa3"):
        return False

    def vint(offset, *, strip_marker):
        first = header[offset]
        if not first:
            raise ValueError("Invalid EBML integer")
        length = 9 - first.bit_length()
        end = offset + length
        if end > len(header):
            raise ValueError("Incomplete EBML integer")
        value = int.from_bytes(header[offset:end], "big")
        return (value ^ (1 << (7 * length)) if strip_marker else value), end

    try:
        length, offset = vint(4, strip_marker=True)
        end = offset + length
        while offset < min(end, len(header)):
            tag, offset = vint(offset, strip_marker=False)
            length, offset = vint(offset, strip_marker=True)
            if offset + length > min(end, len(header)):
                return False
            if tag == 0x4282:  # EBML DocType
                return header[offset:offset + length] == b"webm"
            offset += length
    except (ValueError, IndexError):
        pass
    return False


def inspect_source(source: Path, thumb: Path) -> dict:
    extension = source.suffix.lower()
    if extension == ".svg":
        try:
            probed = media.probe(source)
            media.render_image(source, thumb, (320, 180))
        except media.MediaError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not thumb.is_file():
            raise HTTPException(422, "O arquivo SVG não produziu uma imagem legível.")
        return {
            "kind": "image",
            "format": "SVG",
            "mime": "image/svg+xml",
            "codec": "svg",
            "width": probed.width or 512,
            "height": probed.height or 512,
            "fps": None,
            "duration": None,
            "hasThumbnail": True,
        }
    if not media.FFPROBE or not media.FFMPEG:
        raise HTTPException(503, "Instale FFmpeg e FFprobe no Codespace.")
    allowed, format_name, mime = FORMATS[extension]
    names = allowed.split()
    if "mov" in names:
        names.extend(("mp4", "m4a", "3gp", "3g2", "mj2"))
    if "matroska" in names:
        names.append("webm")
    # All data is read from the fixed private path. No playlist demuxers,
    # network protocols, external MOV data references or image patterns.
    input_options = ["-protocol_whitelist", "file", "-format_whitelist", ",".join(names)]
    raw = run_checked([
        media.FFPROBE, "-v", "error", *input_options,
        "-show_entries",
        "format=format_name,duration:format_tags=major_brand:"
        "stream=index,codec_name,codec_type,width,height,duration,avg_frame_rate,r_frame_rate:"
        "stream_disposition=attached_pic",
        "-of", "json", str(source),
    ], capture=True)
    info = json.loads(raw)
    container = info.get("format", {})
    demuxer = container.get("format_name", "").split(",")[0]
    if demuxer not in allowed.split():
        raise HTTPException(415, "A extensão não corresponde ao contêiner detectado.")
    audio = extension in SOUND_EXTS
    stream = next((item for item in info.get("streams", [])
                   if item.get("codec_type") == ("audio" if audio else "video")
                   and not item.get("disposition", {}).get("attached_pic")), None)
    if not stream or not stream.get("codec_name"):
        raise HTTPException(422, "A mídia não possui um fluxo compatível.")
    codec = stream["codec_name"]
    if extension in {".jpg", ".jpeg"} and codec not in {"mjpeg", "ljpeg"}:
        raise HTTPException(415, "O arquivo não contém uma imagem JPEG.")
    if extension == ".at9" and codec != "atrac9":
        raise HTTPException(415, "O arquivo .at9 não contém áudio ATRAC9.")
    kind = ("audio" if audio else "animation" if extension in {".gif", ".apng"} else
            "image" if extension in IMAGE_EXTS else "video")
    if demuxer == "mov":
        brand = container.get("tags", {}).get("major_brand", "").strip()
        if brand == "qt":
            format_name, mime = "QuickTime", "video/quicktime" if not audio else "application/octet-stream"
        elif brand:
            format_name, mime = "ISO BMFF", "audio/mp4" if audio else "video/mp4"
    if demuxer == "m4v":
        format_name, mime = "MPEG-4 Visual", "application/octet-stream"
    if demuxer == "matroska" and webm_header(source):
        format_name, mime = "WebM", "video/webm"
    if extension == ".apng" and demuxer == "png_pipe":
        format_name, mime = "PNG", "image/png"
    elif demuxer == "apng":
        format_name, mime = "APNG", "image/apng"
    command = [
        media.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
        "-abort_on", "empty_output",
        *input_options, "-i", str(source), "-map", f"0:{stream['index']}",
    ]
    if audio:
        run_checked([*command, "-frames:a", "1", "-f", "null", "-"])
    else:
        run_checked([
            *command, "-frames:v", "1", "-vf", media.scale_crop_filter(320, 180),
            "-c:v", "libwebp", "-quality", "70", str(thumb),
        ])
        if not thumb.is_file():
            raise HTTPException(422, "A mídia não produziu um quadro legível.")
    return {
        "kind": kind, "format": format_name, "mime": mime, "codec": codec,
        "width": stream.get("width"), "height": stream.get("height"),
        "fps": (positive(stream.get("avg_frame_rate")) or positive(stream.get("r_frame_rate")))
        if kind in {"video", "animation"} else None,
        "duration": (positive(container.get("duration")) or positive(stream.get("duration")))
        if kind != "image" else None,
        "hasThumbnail": not audio,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _max_bytes_for_extension(extension: str) -> int:
    if extension in (VIDEO_EXTS - {".gif", ".apng"}):
        return MAX_VIDEO_UPLOAD_BYTES
    return MAX_UPLOAD_BYTES


def _write_asset_manifest(directory: Path, asset: Asset) -> None:
    tmp = directory / f".asset-{os.getpid()}.tmp"
    try:
        with tmp.open("w", encoding="utf-8") as manifest:
            manifest.write(asset.model_dump_json(indent=2))
            manifest.flush()
            os.fsync(manifest.fileno())
        tmp.replace(directory / "asset.json")
    finally:
        tmp.unlink(missing_ok=True)


def store_file_path(
    src_path: Path,
    original_name: str,
    root: Path,
    *,
    folder: str | None = None,
    delete_source: bool = False,
    cancel_event=None,
    register_proc=None,
    progress_cb=None,
) -> tuple[Asset, bool]:
    """Import an on-disk file into the content-addressed library (streaming, zero full-file RAM buffer).

    Automatically normalizes >720p or >16 MiB videos (including 4K 60fps up to 120 MiB) to PS4-optimal
    1280x720 high-quality H.264 MP4 and deletes the raw 4K temporary file immediately afterwards.
    Preserves GIF/APNG and light videos (<=720p and <=16 MiB) byte-for-byte intact.
    """
    name = (original_name or "").replace("\\", "/").split("/")[-1].strip()
    extension = Path(name).suffix.lower()
    if extension not in EXTENSIONS:
        raise HTTPException(415, "Formato não aceito pelo editor.")
    limit = _max_bytes_for_extension(extension)
    size = src_path.stat().st_size if src_path.is_file() else 0
    if size <= 0:
        raise HTTPException(400, "O arquivo está vazio.")
    if size > limit:
        raise HTTPException(
            413,
            f"O arquivo excede o limite de {limit // 1048576} MiB.",
            headers={"X-Czar-Limit-Error": "1"},
        )

    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".import-", dir=root) as incoming:
        entry = Path(incoming) / "entry"
        entry.mkdir(mode=0o700)
        source = entry / f"source{extension}"
        digest = hashlib.sha256()
        count = 0
        with src_path.open("rb") as inp, source.open("xb") as output:
            while block := inp.read(1024 * 1024):
                count += len(block)
                if count > limit:
                    raise HTTPException(
                        413,
                        f"O arquivo excede o limite de {limit // 1048576} MiB.",
                        headers={"X-Czar-Limit-Error": "1"},
                    )
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())

        if delete_source:
            src_path.unlink(missing_ok=True)

        if progress_cb:
            progress_cb("Verificando formato e integridade da mídia…", 35)
        metadata = inspect_source(source, entry / THUMB)

        norm_meta: dict | None = None
        if metadata["kind"] == "video":
            needs_norm, probed_v = media.video_needs_normalization(source)
            if needs_norm:
                if progress_cb:
                    progress_cb(
                        f"Normalizando vídeo {probed_v.width}×{probed_v.height} para 1280×720 H.264 alta qualidade…",
                        60,
                    )
                norm_dest = entry / "source_normalized.mp4"
                try:
                    norm_res = media.normalize_video_for_ps4(
                        source,
                        norm_dest,
                        keep_audio=True,
                        delete_source=True,
                        cancel_event=cancel_event,
                        register_proc=register_proc,
                    )
                except media.MediaError as exc:
                    raise HTTPException(422, str(exc)) from exc
                source = entry / "source.mp4"
                norm_dest.replace(source)
                extension = ".mp4"
                if not name.lower().endswith(".mp4"):
                    name = f"{Path(name).stem}.mp4"
                count = source.stat().st_size
                with source.open("rb") as nf:
                    os.fsync(nf.fileno())
                if progress_cb:
                    progress_cb("Gerando miniatura e validando vídeo normalizado…", 88)
                (entry / THUMB).unlink(missing_ok=True)
                metadata = inspect_source(source, entry / THUMB)
                norm_meta = {
                    k: v for k, v in norm_res.items() if k != "path"
                }
                ident = _sha256_file(source)
            else:
                ident = digest.hexdigest()
        else:
            ident = digest.hexdigest()

        resolved_folder = infer_folder(metadata["kind"], name, folder)

        if (root / ident).is_dir():
            existing, existing_src = read_asset(root, ident)
            if existing.extension == extension:
                if folder and existing.effective_folder() != resolved_folder:
                    updated = existing.model_copy(update={"folder": resolved_folder})
                    _write_asset_manifest(root / ident, updated)
                    _ensure_library_folder_mirrors(root, updated, existing_src)
                    return updated, False
                _ensure_library_folder_mirrors(root, existing, existing_src)
                return existing, False

        asset = Asset(
            id=ident,
            sha256=ident,
            originalName=name,
            displayName=name,
            extension=extension,
            folder=resolved_folder,
            bytes=count,
            importedAt=datetime.now(timezone.utc),
            normalized=norm_meta,
            **metadata,
        )
        with (entry / "asset.json").open("x", encoding="utf-8") as manifest:
            manifest.write(asset.model_dump_json(indent=2))
            manifest.flush()
            os.fsync(manifest.fileno())
        source.chmod(0o400)
        try:
            entry.rename(root / ident)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY} or not (root / ident).is_dir():
                raise
            existing, existing_src = read_asset(root, ident)
            _ensure_library_folder_mirrors(root, existing, existing_src)
            return existing, False
        _ensure_library_folder_mirrors(root, asset, root / ident / f"source{extension}")
        return asset, True


def store_upload(file: UploadFile, root: Path, *, folder: str | None = None) -> tuple[Asset, bool]:
    name = (file.filename or "").replace("\\", "/").split("/")[-1].strip()
    extension = Path(name).suffix.lower()
    if extension not in EXTENSIONS:
        raise HTTPException(415, "Formato não aceito pelo editor.")
    limit = _max_bytes_for_extension(extension)
    if file.size is not None and file.size > limit:
        raise HTTPException(
            413,
            f"O arquivo excede {limit // 1048576} MiB.",
            headers={"X-Czar-Limit-Error": "1"},
        )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".import-", dir=root) as incoming:
        raw_path = Path(incoming) / f"incoming{extension}"
        count = 0
        with raw_path.open("xb") as output:
            while block := file.file.read(1024 * 1024):
                count += len(block)
                if count > limit:
                    raise HTTPException(
                        413,
                        f"O arquivo excede {limit // 1048576} MiB.",
                        headers={"X-Czar-Limit-Error": "1"},
                    )
                output.write(block)
            if not count:
                raise HTTPException(400, "O arquivo está vazio.")
            if file.size is not None and count != file.size:
                raise HTTPException(400, "A transferência chegou incompleta.")
            output.flush()
            os.fsync(output.fileno())
        return store_file_path(raw_path, name, root, folder=folder, delete_source=True)


@router.post("")
def import_asset(
    request: Request,
    file: UploadFile = File(...),
    folder: str | None = Form(None),
    folder_query: str | None = Query(None, alias="folder"),
):
    effective_folder = (folder or folder_query or "").strip() or None
    try:
        asset, created = store_upload(file, catalog(request), folder=effective_folder)
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise storage_error() from exc
    return JSONResponse(asset.public(), status_code=201 if created else 200, headers=NO_CACHE)


@router.get("")
def list_assets(
    request: Request,
    q: str = "",
    kind: Category | None = None,
    folder: MediaFolder | None = None,
    sort: Literal["recent", "oldest", "name", "size"] = "recent",
    offset: Annotated[int, Query(ge=0)] = 0,
):
    root = catalog(request)
    try:
        all_assets = [read_asset(root, item.name)[0] for item in root.iterdir()
                      if re.fullmatch(r"[0-9a-f]{64}", item.name)] if root.is_dir() else []
    except (OSError, ValueError) as exc:
        raise storage_error() from exc

    counts: dict[str, int] = {"all": len(all_assets), **{fid: 0 for fid in MEDIA_FOLDER_IDS}}
    for asset in all_assets:
        counts[asset.effective_folder()] += 1

    query = q.strip().casefold()
    assets = [
        asset for asset in all_assets
        if (kind is None or kind == asset.kind)
        and (folder is None or folder == asset.effective_folder())
        and (query in asset.displayName.casefold() or query in asset.originalName.casefold())
    ]
    if sort in {"recent", "oldest"}:
        assets.sort(key=lambda a: (a.importedAt, a.id), reverse=sort == "recent")
    elif sort == "size":
        assets.sort(key=lambda a: (-a.bytes, a.displayName.casefold(), a.id))
    else:
        assets.sort(key=lambda a: (a.displayName.casefold(), a.id))
    end = offset + 48
    return JSONResponse({
        "items": [asset.public() for asset in assets[offset:end]],
        "next": end if end < len(assets) else None,
        "counts": counts,
        "folders": [
            {
                "id": f["id"],
                "subdir": f["subdir"],
                "label": f["label"],
                "count": counts[f["id"]],
            }
            for f in MEDIA_FOLDERS
        ],
    }, headers=NO_CACHE)


@router.patch("/{ident}/folder")
def update_asset_folder(request: Request, ident: str, payload: FolderUpdate):
    """Move an asset to another compatible dedicated media folder."""
    root = catalog(request)
    asset, source = get_asset(request, ident)
    spec = MEDIA_FOLDER_BY_ID[payload.folder]
    if asset.kind not in spec["accept_kinds"]:
        raise HTTPException(
            415,
            f"Este arquivo ({asset.kind}) não é compatível com a pasta '{spec['label']['pt-BR']}'.",
        )
    updated = asset.model_copy(update={"folder": payload.folder})
    try:
        _write_asset_manifest(root / ident, updated)
        _ensure_library_folder_mirrors(root, updated, source)
    except OSError as exc:
        raise storage_error() from exc
    return JSONResponse(updated.public(), headers=NO_CACHE)


@router.get("/{ident}/thumbnail")
def thumbnail(request: Request, ident: str):
    asset, source = get_asset(request, ident)
    thumb = source.parent / THUMB
    if not asset.hasThumbnail or not thumb.is_file():
        raise HTTPException(404, "Miniatura indisponível.")
    return FileResponse(thumb, media_type="image/webp", headers=NO_CACHE)


@router.get("/{ident}/original")
def original(request: Request, ident: str, play: bool = False):
    asset, source = get_asset(request, ident)
    inline = play and asset.mime in {
        "audio/wav", "audio/mpeg", "audio/ogg", "audio/flac", "audio/mp4", "video/mp4", "video/webm",
        "image/gif", "image/apng",
    } and asset.extension != ".at9"
    return FileResponse(
        source, media_type=asset.mime, filename=asset.originalName,
        content_disposition_type="inline" if inline else "attachment", headers=NO_CACHE,
    )

