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
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import PREVIEW_WEBP_QUALITY

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

# On Windows `convert` is the system's FAT->NTFS converter, not ImageMagick.
# Only the `magick` binary is accepted, which has no such name clash.
MAGICK = shutil.which("magick")

#: Keeps Windows from opening a console window on every ffmpeg call.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


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


def probe(path: Path) -> ProbeResult:
    """Duracion y geometria reales del archivo.

    La duracion se busca en varios lados porque los GIF y algunos webm no la
    traen en el contenedor; si todo falla, se cuentan los frames a mano.
    """
    require_tools()
    out = _run(
        [
            FFPROBE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,duration,nb_frames,avg_frame_rate,codec_name:format=duration",
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
    )


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


def _render_transparent_rgba(src: Path, size: tuple[int, int]) -> "Image.Image | None":
    """Return a premultiplied-alpha Lanczos cover-cropped RGBA image when `src` has real transparency.

    FFmpeg's swscale resamples straight RGBA channels independently, creating dark fringe halos
    around transparent antialiased icon edges. Pillow's Lanczos filter properly premultiplies alpha.
    Returns None for opaque sources or formats Pillow does not decode (e.g. DDS), preserving the
    canonical FFmpeg path.
    """
    from PIL import Image, ImageOps

    if src.suffix.lower() in {".dds", ".gif", ".apng"}:
        return None
    try:
        with Image.open(src) as im:
            has_alpha = "A" in im.getbands() or "transparency" in im.info
            if not has_alpha:
                return None
            im = ImageOps.exif_transpose(im)
            rgba = im.convert("RGBA")
            alpha_min, _ = rgba.getchannel("A").getextrema()
            if alpha_min >= 255:
                return None
            width, height = size
            sw, sh = rgba.size
            if sw <= 0 or sh <= 0:
                return None
            # Match scale_crop_filter: force_original_aspect_ratio=increase + centered crop
            scale = max(width / sw, height / sh)
            nw = max(width, int(round(sw * scale)))
            nh = max(height, int(round(sh * scale)))
            if (nw, nh) != (sw, sh):
                rgba = rgba.resize((nw, nh), Image.Resampling.LANCZOS)
            left = (nw - width) // 2
            top = (nh - height) // 2
            return rgba.crop((left, top, left + width, top + height))
    except Exception:
        return None


def render_image(src: Path, dest: Path, size: tuple[int, int]) -> Path:
    """Escala + recorta una imagen a WxH exactos y la guarda (formato por extension)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    suffix = dest.suffix.lower()
    if suffix in {".png", ".webp"}:
        rgba = _render_transparent_rgba(src, size)
        if rgba is not None:
            tmp = dest.with_name(f".{dest.name}.tmp")
            try:
                if suffix == ".png":
                    rgba.save(tmp, "PNG")
                else:
                    rgba.save(tmp, "WEBP", lossless=True, method=1)
                tmp.replace(dest)
                return dest
            finally:
                tmp.unlink(missing_ok=True)

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
