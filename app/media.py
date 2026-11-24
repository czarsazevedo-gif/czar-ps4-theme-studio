"""Capa de ffmpeg / ffprobe.

Todo lo que toca video o imagenes pasa por aqui. Dos reglas que se repiten
en cada llamada:

* El escalado usa `force_original_aspect_ratio=increase` + `crop` centrado,
  o sea llena el frame completo recortando lo que sobra. Nunca deforma la
  imagen ni mete barras negras.
* El fps de extraccion se calcula con la duracion real del archivo
  (ffprobe), nunca con un valor asumido, para sacar exactamente los frames
  que pidio el usuario.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

from .config import (
    PREVIEW_WEBP_QUALITY,
    PROJECT_ROOT,
    VIDEO_NORMALIZE_FPS_THRESHOLD,
    VIDEO_NORMALIZE_SIZE,
    VIDEO_PASSTHROUGH_MAX_BYTES,
)

#: Keeps Windows from opening a console window on every ffmpeg call.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def _is_imagemagick_binary(binary: str | Path) -> bool:
    """Confirm that an executable is genuine ImageMagick (never Windows FAT->NTFS convert.exe)."""
    path = Path(binary)
    if not path.is_file() or not os.access(path, os.X_OK):
        return False
    try:
        proc = subprocess.run(
            [str(path), "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=4,
            creationflags=NO_WINDOW,
        )
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        return proc.returncode == 0 and "imagemagick" in out.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def find_magick() -> str | None:
    """Locate ImageMagick safely across Linux and Windows.

    Priority:
    1. `magick` on PATH (ImageMagick 7 or symlink)
    2. `tools/bin/magick` (user-space link created by CZAR setup)
    3. On POSIX (non-Windows), `convert` on PATH ONLY when `-version` confirms ImageMagick.
       On Windows `convert` is the system FAT->NTFS converter and is never probed.
    """
    on_path = shutil.which("magick")
    if on_path:
        return on_path
    local_bin = PROJECT_ROOT / "tools" / "bin" / "magick"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)
    if os.name != "nt":
        conv = shutil.which("convert")
        if conv and _is_imagemagick_binary(conv):
            return conv
    return None


def _find_rsvg_convert() -> str | None:
    local_bin = PROJECT_ROOT / "tools" / "bin" / "rsvg-convert"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)
    return shutil.which("rsvg-convert")


def _find_resvg_cli() -> str | None:
    local_bin = PROJECT_ROOT / "tools" / "bin" / "resvg"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)
    return shutil.which("resvg")


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
MAGICK = find_magick()


def refresh_tools() -> None:
    """Re-probe ffmpeg, ffprobe and ImageMagick paths live without restarting the server."""
    global FFMPEG, FFPROBE, MAGICK
    FFMPEG = shutil.which("ffmpeg")
    FFPROBE = shutil.which("ffprobe")
    MAGICK = find_magick()


class MediaError(RuntimeError):
    """Fallo de ffmpeg/ffprobe con el stderr util ya extraido."""


@dataclass(frozen=True)
class ProbeResult:
    duration: float
    width: int
    height: int
    frame_count: int | None
    codec: str
    is_animated: bool
    fps: float | None = None



def _run(cmd: list[str], *, capture_stdout: bool = False) -> bytes:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise MediaError("\n".join(tail[-6:]) or f"{cmd[0]} exited with code {proc.returncode}")
    return proc.stdout or b""


def require_tools() -> None:
    if not FFMPEG or not FFPROBE:
        raise MediaError("ffmpeg/ffprobe are not on the PATH.")


def scale_crop_filter(width: int, height: int) -> str:
    """Llena WxH exactos sin deformar: escala hacia arriba y recorta al centro."""
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={width}:{height},setsar=1"
    )


def _parse_fps(value) -> float | None:
    if not value:
        return None
    try:
        fps = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError, TypeError, OverflowError):
        return None
    return fps if math.isfinite(fps) and fps > 0 else None


def _parse_svg_length(raw: str | None) -> float | None:
    if not raw:
        return None
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt|pc|mm|cm|in)?\s*$", raw, flags=re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "px").lower()
    scale = {"px": 1.0, "pt": 96.0 / 72.0, "pc": 16.0, "in": 96.0, "cm": 96.0 / 2.54, "mm": 96.0 / 25.4}.get(unit, 1.0)
    out = val * scale
    return out if math.isfinite(out) and out > 0 else None


def _parse_svg_dimensions(svg_text: str) -> tuple[float, float]:
    """Extract intrinsic (width, height) from an SVG's viewBox or width/height attributes."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError:
        return (512.0, 512.0)

    view_box = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if view_box:
        parts = [p for p in re.split(r"[\s,]+", view_box.strip()) if p]
        if len(parts) == 4:
            try:
                vw, vh = float(parts[2]), float(parts[3])
                if math.isfinite(vw) and math.isfinite(vh) and vw > 0 and vh > 0:
                    return (vw, vh)
            except ValueError:
                pass

    w = _parse_svg_length(root.attrib.get("width"))
    h = _parse_svg_length(root.attrib.get("height"))
    if w and h:
        return (w, h)
    if w:
        return (w, w)
    if h:
        return (h, h)
    return (512.0, 512.0)


def probe(path: Path) -> ProbeResult:
    """Duracion y geometria reales del archivo.

    La duracion se busca en varios lados porque los GIF y algunos webm no la
    traen en el contenedor; si todo falla, se cuentan los frames a mano.
    """
    if path.suffix.lower() == ".svg":
        validate_svg_safe(path)
        vw, vh = _parse_svg_dimensions(path.read_text(encoding="utf-8"))
        return ProbeResult(
            duration=0.0,
            width=max(1, int(round(vw))),
            height=max(1, int(round(vh))),
            frame_count=1,
            codec="svg",
            is_animated=False,
            fps=None,
        )

    require_tools()
    out = _run(
        [
            FFPROBE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,duration,nb_frames,avg_frame_rate,r_frame_rate,codec_name:format=duration",
            "-of", "json", str(path),
        ],
        capture_stdout=True,
    )
    data = json.loads(out or b"{}")
    streams = data.get("streams") or []
    if not streams:
        raise MediaError("The file has no video stream (check that it is not corrupt).")
    st = streams[0]

    def as_float(value) -> float:
        try:
            f = float(value)
            return f if f > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    duration = as_float(data.get("format", {}).get("duration")) or as_float(st.get("duration"))
    fps = _parse_fps(st.get("avg_frame_rate")) or _parse_fps(st.get("r_frame_rate"))

    nb_frames = None
    try:
        nb_frames = int(st["nb_frames"])
    except (KeyError, TypeError, ValueError):
        pass

    # Fallback 1: nb_frames / declared fps.
    if not duration and nb_frames:
        rate = st.get("avg_frame_rate") or "0/0"
        try:
            num, den = (int(x) for x in rate.split("/"))
            if den and num:
                duration = nb_frames * den / num
        except ValueError:
            pass

    # Fallback 2: count packets. Slow, but only happens with odd files.
    if not duration:
        counted = _run(
            [
                FFPROBE, "-v", "error", "-select_streams", "v:0",
                "-count_packets", "-show_entries", "stream=nb_read_packets,avg_frame_rate",
                "-of", "json", str(path),
            ],
            capture_stdout=True,
        )
        cst = (json.loads(counted or b"{}").get("streams") or [{}])[0]
        nb_frames = nb_frames or int(cst.get("nb_read_packets") or 0) or None
        fps = fps or _parse_fps(cst.get("avg_frame_rate"))
        try:
            num, den = (int(x) for x in (cst.get("avg_frame_rate") or "0/1").split("/"))
            if nb_frames and den and num:
                duration = nb_frames * den / num
        except ValueError:
            pass

    if not duration:
        raise MediaError("Could not determine the file duration.")

    return ProbeResult(
        duration=duration,
        width=int(st.get("width") or 0),
        height=int(st.get("height") or 0),
        frame_count=nb_frames,
        codec=str(st.get("codec_name") or "?"),
        is_animated=bool(nb_frames is None or nb_frames > 1),
        fps=fps,
    )


def compute_normalized_fps(src_fps: float | None) -> float | None:
    """Return integer-divisor target FPS when src_fps > 36 fps; None to preserve 24/25/30 fps intact.

    Examples:
    - 60 fps -> 30 fps (divisor 2)
    - 59.94 fps -> 29.97 fps (divisor 2)
    - 50 fps -> 25 fps (divisor 2)
    - 48 fps -> 24 fps (divisor 2)
    - 120 fps -> 30 fps (divisor 4)
    - 24 / 25 / 30 fps -> None (preserved untouched)
    """
    if src_fps is None or not math.isfinite(src_fps) or src_fps <= 0:
        return None
    if src_fps <= VIDEO_NORMALIZE_FPS_THRESHOLD:
        return None
    divisor = max(2, math.ceil(src_fps / 30.05))
    return round(src_fps / divisor, 6)


def video_needs_normalization(src: Path, info: ProbeResult | None = None) -> tuple[bool, ProbeResult]:
    """Determine if a video needs automatic high-quality PS4 720p normalization.

    Preserves GIF/APNG and light videos (<= 1280x720 and <= 16 MiB) byte-for-byte intact.
    """
    probed = info or probe(src)
    ext = src.suffix.lower()
    if ext in {".gif", ".apng"}:
        return False, probed
    size_bytes = src.stat().st_size if src.is_file() else 0
    max_w, max_h = VIDEO_NORMALIZE_SIZE
    if probed.width <= max_w and probed.height <= max_h and size_bytes <= VIDEO_PASSTHROUGH_MAX_BYTES:
        return False, probed
    return True, probed


def normalize_video_for_ps4(
    src: Path,
    dest: Path,
    *,
    keep_audio: bool = False,
    delete_source: bool = True,
    cancel_event=None,
    register_proc=None,
) -> dict:
    """Compress >720p or >16 MiB videos (including 4K 60fps up to 120 MiB) to PS4-optimal 1280x720 MP4.

    Encoding profile:
    - 1280x720 16:9 Lanczos scale + centered crop (setsar=1)
    - libx264 High@4.1 yuv420p CRF 17
    - maxrate 8M (bufsize 16M) for clips <= 45s, 6M (bufsize 12M) for longer clips
    - 1s GOP (-g round(effective_fps)) + -movflags +faststart
    - Integer FPS divisor above 36 fps (60->30, 50->25, 48->24); 24/25/30 fps preserved
    - Deletes temporary raw source file after successful normalization when delete_source=True.
    """
    needs, info = video_needs_normalization(src)
    orig_bytes = src.stat().st_size
    if not needs:
        return {
            "normalized": False,
            "path": src,
            "original_width": info.width,
            "original_height": info.height,
            "original_bytes": orig_bytes,
            "original_fps": round(info.fps, 3) if info.fps else None,
            "width": info.width,
            "height": info.height,
            "bytes": orig_bytes,
            "fps": round(info.fps, 3) if info.fps else None,
            "codec": info.codec,
            "duration": round(info.duration, 3),
        }

    require_tools()
    if cancel_event is not None and cancel_event.is_set():
        raise MediaError("Normalização de vídeo cancelada.")

    target_w, target_h = VIDEO_NORMALIZE_SIZE
    target_fps = compute_normalized_fps(info.fps)
    eff_fps = target_fps or info.fps or 30.0
    gop = max(1, int(round(eff_fps)))
    maxrate = "6M" if info.duration > 45.0 else "8M"
    bufsize = "12M" if maxrate == "6M" else "16M"

    vf = scale_crop_filter(target_w, target_h)
    if target_fps is not None:
        vf = f"fps={target_fps:.6f},{vf}"

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = dest.with_name(f".{dest.stem}.norm.tmp.mp4")

    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-map", "0:v:0",
    ]
    if keep_audio:
        cmd += ["-map", "0:a:0?"]
    else:
        cmd += ["-an"]

    cmd += [
        "-vf", vf,
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level:v", "4.1",
        "-pix_fmt", "yuv420p",
        "-preset", "fast",
        "-crf", "17",
        "-maxrate", maxrate,
        "-bufsize", bufsize,
        "-g", str(gop),
        "-movflags", "+faststart",
    ]
    if keep_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd.append(str(tmp_out))

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
    )
    if register_proc is not None:
        register_proc(proc)
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                raise MediaError("Normalização de vídeo cancelada pelo usuário.")
            try:
                proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                continue
        stderr_data = proc.stderr.read() if proc.stderr else b""
        if proc.returncode != 0 or not tmp_out.is_file() or tmp_out.stat().st_size == 0:
            tail = stderr_data.decode("utf-8", "replace").strip().splitlines()
            raise MediaError(
                "\n".join(tail[-6:]) or f"ffmpeg falhou ao normalizar vídeo (código {proc.returncode})."
            )
        tmp_out.replace(dest)
    except BaseException:
        tmp_out.unlink(missing_ok=True)
        raise
    finally:
        if proc.stderr:
            proc.stderr.close()
        if register_proc is not None:
            register_proc(None)

    out_info = probe(dest)
    out_bytes = dest.stat().st_size
    if delete_source and src.resolve() != dest.resolve():
        src.unlink(missing_ok=True)

    return {
        "normalized": True,
        "path": dest,
        "original_width": info.width,
        "original_height": info.height,
        "original_bytes": orig_bytes,
        "original_fps": round(info.fps, 3) if info.fps else None,
        "width": out_info.width,
        "height": out_info.height,
        "bytes": out_bytes,
        "fps": round(out_info.fps, 3) if out_info.fps else round(eff_fps, 3),
        "target_fps": target_fps,
        "gop": gop,
        "maxrate": maxrate,
        "codec": out_info.codec,
        "duration": round(out_info.duration, 3),
    }


def extraction_fps(frame_count: int, duration: float) -> float:
    """fps necesario para sacar `frame_count` muestras repartidas en `duration`.

    Con este fps el filtro genera frames en t = k*duration/frame_count, o sea
    el ultimo cae justo antes del final del archivo.
    """
    if frame_count < 1:
        raise ValueError("frame_count must be >= 1")
    if duration <= 0:
        raise ValueError("duration must be > 0")
    return frame_count / duration


def extract_preview_frames(
    src: Path, dest_dir: Path, count: int, size: tuple[int, int], duration: float
) -> list[Path]:
    """Saca `count` frames como .webp para mostrarlos en el navegador.

    Los .dds no se pueden pintar en un <img>, asi que el preview corre sobre
    estos. Salen del mismo filtro que la exportacion real, con el mismo fps y
    el mismo recorte, para que lo que ves sea lo que se exporta.
    """
    require_tools()
    dest_dir.mkdir(parents=True, exist_ok=True)
    for stale in dest_dir.glob("*.webp"):
        stale.unlink()

    width, height = size
    fps = extraction_fps(count, duration)
    _run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vf", f"fps={fps:.9f},{scale_crop_filter(width, height)}",
        "-frames:v", str(count),
        "-fps_mode", "passthrough",
        "-c:v", "libwebp", "-quality", str(PREVIEW_WEBP_QUALITY), "-compression_level", "4",
        "-start_number", "0",
        "-f", "image2", str(dest_dir / "%02d.webp"),
    ])

    frames = sorted(dest_dir.glob("*.webp"))
    if not frames:
        raise MediaError("ffmpeg produced no frames. The file may be corrupt.")

    # When the container reports a duration slightly longer than the last
    # decodable frame, fewer frames come out than requested; pad by repeating
    # the last one so index.xml matches the files on disk.
    while len(frames) < count:
        clone = dest_dir / f"{len(frames):02d}.webp"
        clone.write_bytes(frames[-1].read_bytes())
        frames.append(clone)

    return frames[:count]


def iter_raw_frames(
    src: Path, count: int, size: tuple[int, int], duration: float
):
    """Genera los frames como arrays RGBA, listos para comprimir a DXT1a.

    Va leyendo del stdout de ffmpeg en vez de escribir PNGs intermedios:
    a 1280x720 son 3.5MB por frame y nunca hay mas de uno en memoria.
    """
    require_tools()
    width, height = size
    fps = extraction_fps(count, duration)
    frame_bytes = width * height * 4

    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vf", f"fps={fps:.9f},{scale_crop_filter(width, height)}",
        "-frames:v", str(count),
        "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW
    )
    assert proc.stdout is not None

    emitted = 0
    last: np.ndarray | None = None
    try:
        while emitted < count:
            buf = proc.stdout.read(frame_bytes)
            if not buf or len(buf) < frame_bytes:
                break
            last = np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 4)
            emitted += 1
            yield last
    finally:
        if proc.stdout:
            proc.stdout.close()
        stderr = proc.stderr.read() if proc.stderr else b""
        if proc.stderr:
            proc.stderr.close()
        proc.wait()

    if emitted == 0:
        tail = stderr.decode("utf-8", "replace").strip().splitlines()
        raise MediaError("\n".join(tail[-6:]) or "ffmpeg decoded no frames.")

    # Same padding as the preview, so both end up with the same count.
    while emitted < count and last is not None:
        emitted += 1
        yield last


def _find_svg_rasterizer() -> str | None:
    local = PROJECT_ROOT / "tools" / "bin" / "magick"
    if local.is_file():
        return str(local)
    return find_magick()


def svg_rasterizer_info() -> dict:
    """Report available deterministic SVG vector rasterizers on this host."""
    backends: list[str] = []
    try:
        import cairosvg  # noqa: F401
        backends.append("cairosvg")
    except (ImportError, OSError):
        pass
    if _find_rsvg_convert():
        backends.append("rsvg-convert")
    try:
        import resvg_py  # noqa: F401
        backends.append("resvg-py")
    except (ImportError, OSError):
        pass
    if _find_resvg_cli():
        backends.append("resvg")
    if _find_svg_rasterizer():
        backends.append("imagemagick")
    return {
        "available": bool(backends),
        "preferred": backends[0] if backends else None,
        "backends": backends,
    }


def validate_svg_safe(src: Path) -> None:
    """Validate that an SVG file is clean UTF-8 XML without XXE, external links, or active scripts."""
    raw = src.read_bytes()
    if not raw or len(raw) > 4 * 1024 * 1024 or b"\x00" in raw:
        raise MediaError("Arquivo SVG inválido ou vazio.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MediaError("Arquivo SVG precisa estar codificado em UTF-8.") from exc
    low = text.lower()
    if "<svg" not in low:
        raise MediaError("Arquivo .svg não contém um elemento <svg> válido.")
    for forbidden in ("<!entity", "<!doctype", "<script", "javascript:", "href=\"http", "href='http", "href=\"file:", "href='file:"):
        if forbidden in low:
            raise MediaError(f"SVG contém recurso externo ou script não permitido ({forbidden}).")

    # Attribute syntax can contain whitespace, namespaces, and CSS url(...)
    # references. Reject all non-local resource URLs before passing the SVG to
    # either renderer, so neither CairoSVG nor ImageMagick can fetch external
    # files or URLs through a crafted icon.
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise MediaError(f"SVG inválido: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise MediaError("SVG precisa ter o elemento <svg> como raiz.")
    if "@import" in low:
        raise MediaError("SVG contém importação CSS externa não permitida.")
    for target in re.findall(r"url\(\s*([^)]*)\)", text, flags=re.IGNORECASE):
        if not target.strip(" \t\r\n'\"").startswith("#"):
            raise MediaError("SVG contém URL de recurso externo não permitida.")
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() in {"script", "foreignobject"}:
            raise MediaError("SVG contém elemento ativo não permitido.")
        for attribute, value in element.attrib.items():
            name = attribute.rsplit("}", 1)[-1].lower()
            if name.startswith("on") or (name in {"href", "src"} and not value.strip().startswith("#")):
                raise MediaError(f"SVG contém atributo inseguro ({name}).")


def _resize_rgba_premultiplied(rgba: "Image.Image", target_size: tuple[int, int]) -> "Image.Image":
    """Exact float32 premultiplied-alpha Lanczos resize that eliminates dark fringe halos."""
    from PIL import Image

    if rgba.size == target_size:
        return rgba.copy()
    arr = np.asarray(rgba.convert("RGBA"), dtype=np.float32)
    alpha = arr[..., 3] / 255.0
    # Zero RGB of fully transparent pixels so invisible garbage never bleeds into edges.
    rgb = np.where(alpha[..., None] > 0.0, arr[..., :3], 0.0)
    premul = rgb * alpha[..., None]

    out_a = np.asarray(
        Image.fromarray(alpha, mode="F").resize(target_size, Image.Resampling.LANCZOS),
        dtype=np.float32,
    )
    out_a = np.clip(out_a, 0.0, 1.0)

    out_rgb = np.empty((target_size[1], target_size[0], 3), dtype=np.float32)
    safe_a = np.where(out_a > 1e-4, out_a, 1.0)
    for ch in range(3):
        resized_ch = np.asarray(
            Image.fromarray(premul[..., ch], mode="F").resize(target_size, Image.Resampling.LANCZOS),
            dtype=np.float32,
        )
        out_rgb[..., ch] = np.where(out_a > 1e-4, resized_ch / safe_a, 0.0)

    out = np.empty((target_size[1], target_size[0], 4), dtype=np.uint8)
    out[..., :3] = np.clip(np.rint(out_rgb), 0, 255).astype(np.uint8)
    out[..., 3] = np.clip(np.rint(out_a * 255.0), 0, 255).astype(np.uint8)
    return Image.fromarray(out, mode="RGBA")


def _rasterize_svg_rgba(src: Path, size: tuple[int, int]) -> "Image.Image":
    """Rasterize SVG to crisp premultiplied-alpha RGBA without dark halos.

    Uses a deterministic vector renderer cascade so Linux/Codespaces always has
    a functional rasterizer: CairoSVG -> rsvg-convert -> resvg-py -> resvg -> ImageMagick.
    """
    import tempfile
    from PIL import Image

    validate_svg_safe(src)
    width, height = size
    vw, vh = _parse_svg_dimensions(src.read_text(encoding="utf-8"))
    supersample = min(2048, max(width, height) * 4)
    if vw >= vh:
        rw = supersample
        rh = max(1, int(round(supersample * vh / vw)))
    else:
        rh = supersample
        rw = max(1, int(round(supersample * vw / vh)))

    with tempfile.TemporaryDirectory(prefix="czar-svg-") as td:
        out_png = Path(td) / "raster.png"
        errors: list[str] = []

        # 1. CairoSVG (preferred when system libcairo2 is available)
        if not out_png.is_file():
            try:
                import cairosvg
            except (ImportError, OSError):
                pass
            else:
                try:
                    cairosvg.svg2png(
                        url=str(src),
                        write_to=str(out_png),
                        output_width=rw,
                        output_height=rh,
                        unsafe=False,
                    )
                except Exception as exc:
                    errors.append(f"CairoSVG: {exc}")
                    out_png.unlink(missing_ok=True)

        # 2. rsvg-convert (native librsvg CLI)
        if not out_png.is_file():
            rsvg_bin = _find_rsvg_convert()
            if rsvg_bin:
                try:
                    _run([
                        rsvg_bin,
                        "--keep-aspect-ratio",
                        "-w", str(rw),
                        "-h", str(rh),
                        "-f", "png",
                        "-o", str(out_png),
                        str(src),
                    ])
                except MediaError as exc:
                    errors.append(f"rsvg-convert: {exc}")
                    out_png.unlink(missing_ok=True)

        # 3. resvg-py (pure user-space Rust vector renderer, zero system lib deps)
        if not out_png.is_file():
            try:
                import resvg_py
            except (ImportError, OSError):
                pass
            else:
                try:
                    png_bytes = resvg_py.svg_to_bytes(svg_path=str(src), width=rw, height=rh)
                    if png_bytes:
                        out_png.write_bytes(png_bytes)
                except Exception as exc:
                    errors.append(f"resvg-py: {exc}")
                    out_png.unlink(missing_ok=True)

        # 4. resvg CLI
        if not out_png.is_file():
            resvg_bin = _find_resvg_cli()
            if resvg_bin:
                try:
                    _run([resvg_bin, "-w", str(rw), "-h", str(rh), str(src), str(out_png)])
                except MediaError as exc:
                    errors.append(f"resvg: {exc}")
                    out_png.unlink(missing_ok=True)

        # 5. ImageMagick fallback
        if not out_png.is_file():
            magick = _find_svg_rasterizer()
            if magick:
                try:
                    _run([
                        magick,
                        "-background", "none",
                        "-density", "384",
                        str(src),
                        "-resize", f"{rw}x{rh}",
                        f"PNG32:{out_png}",
                    ])
                except MediaError as exc:
                    errors.append(f"ImageMagick: {exc}")
                    out_png.unlink(missing_ok=True)

        if not out_png.is_file():
            detail = f" ({'; '.join(errors)})" if errors else ""
            raise MediaError(
                "Rasterizador SVG ausente ou indisponível: instale resvg-py, CairoSVG/libcairo2 ou librsvg2-bin."
                + detail
            )

        with Image.open(out_png) as im:
            rgba = im.convert("RGBA")
            sw, sh = rgba.size
            if sw <= 0 or sh <= 0:
                raise MediaError("SVG rasterizado possui dimensões inválidas.")
            # Preserve aspect ratio cleanly inside target box without cropping strokes or darkening edges.
            if width == height and sw == sh:
                return _resize_rgba_premultiplied(rgba, (width, height))
            scale = min(width / sw, height / sh)
            nw = max(1, int(round(sw * scale)))
            nh = max(1, int(round(sh * scale)))
            resized = _resize_rgba_premultiplied(rgba, (nw, nh))
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            # Direct copy (NO alpha mask) preserves straight RGBA without premultiplying RGB twice!
            canvas.paste(resized, ((width - nw) // 2, (height - nh) // 2))
            return canvas


def _render_transparent_rgba(
    src: Path,
    size: tuple[int, int],
    *,
    force_rgba: bool = False,
    contain_transparent: bool = False,
) -> "Image.Image | None":
    """Return a premultiplied-alpha Lanczos RGBA image when `src` is SVG, has transparency, or `force_rgba=True`.

    FFmpeg's swscale resamples straight RGBA channels independently, creating dark fringe halos
    around transparent antialiased icon edges. Our float32 premultiplied-alpha Lanczos resizer
    prevents dark fringes completely. Returns None for opaque non-icon sources or formats Pillow
    does not decode (e.g. DDS), preserving the canonical FFmpeg path.
    """
    from PIL import Image, ImageOps

    ext = src.suffix.lower()
    if ext == ".svg":
        return _rasterize_svg_rgba(src, size)
    if ext in {".dds", ".gif", ".apng"}:
        return None
    try:
        with Image.open(src) as im:
            has_alpha = "A" in im.getbands() or "transparency" in im.info
            if not has_alpha and not force_rgba:
                return None
            im = ImageOps.exif_transpose(im)
            rgba = im.convert("RGBA")
            alpha_min, _ = rgba.getchannel("A").getextrema()
            has_real_alpha = alpha_min < 255
            if not has_real_alpha and not force_rgba:
                return None
            width, height = size
            sw, sh = rgba.size
            if sw <= 0 or sh <= 0:
                return None
            if contain_transparent and has_real_alpha and sw != sh:
                scale = min(width / sw, height / sh)
                nw = max(1, int(round(sw * scale)))
                nh = max(1, int(round(sh * scale)))
                resized = _resize_rgba_premultiplied(rgba, (nw, nh))
                canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                canvas.paste(resized, ((width - nw) // 2, (height - nh) // 2))
                return canvas
            # Match scale_crop_filter: force_original_aspect_ratio=increase + centered crop
            scale = max(width / sw, height / sh)
            nw = max(width, int(round(sw * scale)))
            nh = max(height, int(round(sh * scale)))
            if (nw, nh) != (sw, sh):
                rgba = _resize_rgba_premultiplied(rgba, (nw, nh))
            left = (nw - width) // 2
            top = (nh - height) // 2
            return rgba.crop((left, top, left + width, top + height))
    except Exception:
        return None


def _save_pil_atomic(im: "Image.Image", dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    suffix = dest.suffix.lower()
    tmp = dest.with_name(f".{dest.name}.tmp")
    try:
        if suffix == ".png":
            im.convert("RGBA").save(tmp, "PNG")
        else:
            im.convert("RGBA").save(tmp, "WEBP", lossless=True, method=1)
        tmp.replace(dest)
        return dest
    finally:
        tmp.unlink(missing_ok=True)


def render_image(
    src: Path,
    dest: Path,
    size: tuple[int, int],
    *,
    force_rgba: bool = False,
    contain_transparent: bool = False,
) -> Path:
    """Escala + recorta una imagen a WxH exactos y la guarda (formato por extension)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    suffix = dest.suffix.lower()
    if suffix in {".png", ".webp"}:
        rgba = _render_transparent_rgba(
            src, size, force_rgba=force_rgba, contain_transparent=contain_transparent
        )
        if rgba is not None:
            return _save_pil_atomic(rgba, dest)

    require_tools()
    width, height = size
    args = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vf", scale_crop_filter(width, height),
        "-frames:v", "1",
    ]
    if suffix == ".webp":
        args += ["-c:v", "libwebp", "-quality", str(PREVIEW_WEBP_QUALITY), "-compression_level", "4"]
    elif suffix == ".png":
        # The icon carries alpha; without this ffmpeg would flatten it to rgb24.
        args += ["-c:v", "png", "-pix_fmt", "rgba"]
    args.append(str(dest))
    _run(args)
    if not dest.exists():
        raise MediaError("ffmpeg produced no output image.")
    return dest


def render_icon_assets(src: Path, out_dir: Path, preview_dir: Path, spec: dict) -> dict:
    """Automatically convert uploaded raster/SVG icons into canonical high-quality PS4 RGBA PNGs.

    - Function icons (fn:*): 128x128 RGBA PNG + native 152x152 glow RGBA PNG rendered DIRECTLY
      from the high-res/SVG source (never upscaling 128 -> 152).
    - Content icons (ct:*): 512x512 RGBA PNG rendered directly from the source.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)

    file_stem = str(spec.get("file") or "icon")
    target_size = tuple(spec["size"])
    icon_png = out_dir / f"{file_stem}.png"
    render_image(src, icon_png, target_size, force_rgba=True, contain_transparent=True)

    preview_webp = preview_dir / "preview.webp"
    render_image(icon_png, preview_webp, target_size, force_rgba=True)

    result: dict = {
        "icon_name": icon_png.name,
        "icon_path": icon_png,
        "icon_size": list(target_size),
        "icon_bytes": icon_png.stat().st_size,
        "preview_path": preview_webp,
        "preview_bytes": preview_webp.stat().st_size,
        "format": "PNG",
        "mode": "RGBA",
        "source_is_svg": src.suffix.lower() == ".svg",
        "glow_name": None,
        "glow_path": None,
        "glow_size": None,
        "glow_bytes": None,
        "glow_preview_path": None,
        "native_glow_from_source": False,
    }

    if "glow_size" in spec:
        glow_size = tuple(spec["glow_size"])
        glow_png = out_dir / f"{file_stem}_glow.png"
        # Render native 152x152 directly from high-res/SVG `src`, NEVER from 128x128 `icon_png`.
        render_image(src, glow_png, glow_size, force_rgba=True, contain_transparent=True)
        glow_webp = preview_dir / "glow.webp"
        render_image(glow_png, glow_webp, glow_size, force_rgba=True)
        result.update({
            "glow_name": glow_png.name,
            "glow_path": glow_png,
            "glow_size": list(glow_size),
            "glow_bytes": glow_png.stat().st_size,
            "glow_preview_path": glow_webp,
            "native_glow_from_source": True,
        })

    return result


def image_to_rgba(src: Path, size: tuple[int, int]) -> np.ndarray:
    """Imagen -> array RGBA (H, W, 4) ya escalado y recortado."""
    rgba = _render_transparent_rgba(src, size)
    if rgba is not None:
        return np.asarray(rgba, dtype=np.uint8)
    require_tools()
    width, height = size
    raw = _run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-vf", scale_crop_filter(width, height),
            "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgba", "-",
        ],
        capture_stdout=True,
    )
    expected = width * height * 4
    if len(raw) < expected:
        raise MediaError("ffmpeg returned an incomplete frame.")
    return np.frombuffer(raw[:expected], dtype=np.uint8).reshape(height, width, 4)



@dataclass(frozen=True)
class AudioInfo:
    codec: str
    rate: int
    channels: int
    duration: float
    bitrate: int


def probe_audio(path: Path) -> AudioInfo:
    """Datos del stream de audio. Sirve para validar que un .at9 lo es."""
    require_tools()
    out = _run(
        [
            FFPROBE, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,sample_rate,channels,duration,bit_rate",
            "-of", "json", str(path),
        ],
        capture_stdout=True,
    )
    streams = (json.loads(out or b"{}").get("streams") or [])
    if not streams:
        raise MediaError("The file has no audio stream.")
    st = streams[0]

    def num(key, cast=int, default=0):
        try:
            return cast(st[key])
        except (KeyError, TypeError, ValueError):
            return default

    return AudioInfo(
        codec=str(st.get("codec_name") or "?"),
        rate=num("sample_rate"),
        channels=num("channels"),
        duration=num("duration", float, 0.0),
        bitrate=num("bit_rate"),
    )


def pcm_mono(src: Path, rate: int, max_seconds: float | None = None,
             gain_db: float = 0.0) -> np.ndarray:
    """Decodifica cualquier audio a PCM mono de 16 bits al ritmo pedido.

    `gain_db` se aplica aqui, antes de que vag.encode comprima: subirlo despues
    sobre PS-ADPCM solo anadiria ruido de cuantizacion.
    """
    require_tools()
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if max_seconds:
        args += ["-t", f"{max_seconds:g}"]
    if abs(gain_db) > 0.01:
        filtro = f"volume={gain_db:+.2f}dB"
        if gain_db > 0:
            filtro += ",alimiter=limit=0.98"
        args += ["-af", filtro]
    args += ["-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(rate), "-"]
    raw = _run(args, capture_stdout=True)
    if not raw:
        raise MediaError("ffmpeg extracted no audio from the file.")
    return np.frombuffer(raw, dtype="<i2")


def to_wav(src: Path, dest: Path, rate: int, max_seconds: float | None = None,
           gain_db: float = 0.0) -> Path:
    """Pasa cualquier audio a WAV PCM 16 bits estereo, que es lo que come at9tool.

    `gain_db` ajusta el volumen ANTES de codificar, que es el unico sitio donde
    se puede: el formato del tema no lleva ningun campo de volumen, asi que la
    unica forma de que suene mas fuerte o mas flojo es cambiar el audio.

    Al subir volumen se pasa por un limitador en vez de recortar en seco: con
    `volume` a secas, un archivo ya alto satura y se oye a distorsion.
    """
    require_tools()
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if max_seconds:
        args += ["-t", f"{max_seconds:g}"]
    if abs(gain_db) > 0.01:
        filtro = f"volume={gain_db:+.2f}dB"
        if gain_db > 0:
            filtro += ",alimiter=limit=0.98"
        args += ["-af", filtro]
    args += ["-c:a", "pcm_s16le", "-ac", "2", "-ar", str(rate), str(dest)]
    _run(args)
    if not dest.is_file():
        raise MediaError("ffmpeg produced no intermediate WAV.")
    return dest


def flatten_alpha(rgba: np.ndarray, background=(0, 0, 0)) -> np.ndarray:
    """Compone sobre un fondo opaco y deja alpha en 255.

    Los fondos de la PS4 no usan transparencia, y dejar alpha suelto hace que
    algunos encoders se pasen a DXT5 -- que ocupa el doble y rompe el
    presupuesto de 16MB. Aplanar aqui lo vuelve deterministico.
    """
    if rgba.shape[2] != 4:
        return rgba
    alpha = rgba[..., 3:4].astype(np.float32) / 255.0
    bg = np.array(background, dtype=np.float32)
    rgb = rgba[..., :3].astype(np.float32) * alpha + bg * (1.0 - alpha)
    out = np.empty_like(rgba)
    out[..., :3] = np.rint(rgb).astype(np.uint8)
    out[..., 3] = 255
    return out
