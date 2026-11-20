"""Private per-session catalog of immutable sources for canonical jobs."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import media, projects
from .config import IMAGE_EXTS, MAX_UPLOAD_BYTES, SOUND_EXTS, VIDEO_EXTS

router = APIRouter(prefix="/api/library")
EXTENSIONS = IMAGE_EXTS | VIDEO_EXTS | SOUND_EXTS
THUMB = "thumb.webp"
NO_CACHE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
ID = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Category = Literal["image", "video", "animation", "audio"]

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


class Asset(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schemaVersion: Literal[1] = 1
    id: ID
    sha256: ID
    originalName: str
    displayName: str
    extension: str
    kind: Category
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

    def public(self) -> dict:
        return {
            **self.model_dump(mode="json"),
            "original": f"/api/library/{self.id}/original",
            "thumbnail": f"/api/library/{self.id}/thumbnail" if self.hasThumbnail else None,
        }


def catalog(request: Request) -> Path:
    return projects.owner_path(request) / "library-v1"


def storage_error():
    return HTTPException(503, "Não foi possível acessar o armazenamento da biblioteca.")


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
    extension = source.suffix
    if extension == ".svg":
        try:
            media.validate_svg_safe(source)
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
            "width": 512,
            "height": 512,
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


def store_upload(file: UploadFile, root: Path) -> tuple[Asset, bool]:
    name = (file.filename or "").replace("\\", "/").split("/")[-1].strip()
    extension = Path(name).suffix.lower()
    if extension not in EXTENSIONS:
        raise HTTPException(415, "Formato não aceito pelo editor.")
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "O arquivo excede 512 MiB.")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".import-", dir=root) as incoming:
        entry = Path(incoming) / "entry"
        entry.mkdir(mode=0o700)
        source = entry / f"source{extension}"
        digest = hashlib.sha256()
        count = 0
        with source.open("xb") as output:
            while block := file.file.read(1024 * 1024):
                count += len(block)
                if count > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "O arquivo excede 512 MiB.")
                output.write(block)
                digest.update(block)
            if not count:
                raise HTTPException(400, "O arquivo está vazio.")
            if file.size is not None and count != file.size:
                raise HTTPException(400, "A transferência chegou incompleta.")
            output.flush()
            os.fsync(output.fileno())
        ident = digest.hexdigest()
        if (root / ident).is_dir():
            existing, _ = read_asset(root, ident)
            if existing.extension == extension:
                return existing, False
        metadata = inspect_source(source, entry / THUMB)
        asset = Asset(
            id=ident, sha256=ident, originalName=name, displayName=name, extension=extension,
            bytes=count, importedAt=datetime.now(timezone.utc), **metadata,
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
            return read_asset(root, ident)[0], False
        return asset, True


@router.post("")
def import_asset(request: Request, file: UploadFile = File(...)):
    try:
        asset, created = store_upload(file, catalog(request))
    except (OSError, ValueError) as exc:
        raise storage_error() from exc
    return JSONResponse(asset.public(), status_code=201 if created else 200, headers=NO_CACHE)


@router.get("")
def list_assets(
    request: Request, q: str = "", kind: Category | None = None,
    sort: Literal["recent", "oldest", "name", "size"] = "recent",
    offset: Annotated[int, Query(ge=0)] = 0,
):
    root = catalog(request)
    try:
        assets = [read_asset(root, folder.name)[0] for folder in root.iterdir()
                  if re.fullmatch(r"[0-9a-f]{64}", folder.name)] if root.is_dir() else []
    except (OSError, ValueError) as exc:
        raise storage_error() from exc
    query = q.strip().casefold()
    assets = [asset for asset in assets if (kind is None or kind == asset.kind) and
              (query in asset.displayName.casefold() or query in asset.originalName.casefold())]
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
    }, headers=NO_CACHE)


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
