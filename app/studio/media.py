from __future__ import annotations

import io
import json
import math
import struct
import subprocess
import warnings
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError

from .. import media, vag
from ..at9 import DEFAULT_BITRATE
from .schema import Asset, Track, Transform, safe_name
from .storage import StudioError, sha256

MAX_DIMENSION = 4096
MAX_PIXELS = 4096 * 2304
MAX_DURATION = 300
MEDIA_TIMEOUT = 90  # Includes first-frame decoding of 4K/60FPS source media.
STILL = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp",
         "GIF": "image/gif", "BMP": "image/bmp"}
FORMATS = {
    "mov": ("video/mp4", "audio/mp4"), "matroska": ("video/webm", "audio/webm"),
    "wav": ("", "audio/wav"), "ogg": ("", "audio/ogg"), "mp3": ("", "audio/mpeg"),
    "flac": ("", "audio/flac"), "aac": ("", "audio/aac"), "gif": ("image/gif", ""),
}


def reject(message="A mídia está corrompida ou usa um formato incompatível."):
    return StudioError(415, "invalid_media", message)


def demuxer(header: bytes) -> str | None:
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "wav"
    if header[4:8] == b"ftyp":
        return "mov"
    for signature, kind in ((b"\x1a\x45\xdf\xa3", "matroska"), (b"OggS", "ogg"),
                            (b"fLaC", "flac"), (b"ID3", "mp3"), (b"GIF8", "gif")):
        if header.startswith(signature):
            return kind
    if len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0:
        return "aac" if header[1] & 0xf6 == 0xf0 else "mp3"
    return None


def run(program: str, args: list[str], *, binary=False, capture_log=False) -> bytes:
    if not program:
        raise StudioError(503, "media_dependency", "FFmpeg e ffprobe são necessários.")
    try:
        proc = subprocess.run(
            [program, *args], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if binary else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_log else subprocess.DEVNULL, timeout=MEDIA_TIMEOUT, shell=False,
            creationflags=media.NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise StudioError(422, "media_timeout", "O processamento excedeu o tempo permitido.") from None
    except OSError:
        raise StudioError(503, "media_dependency", "O processador de mídia está indisponível.") from None
    if proc.returncode:
        raise reject()
    return (proc.stderr if capture_log else proc.stdout) or b""


def input_args(path: Path, kind: str) -> list[str]:
    return ["-protocol_whitelist", "file,pipe", "-max_alloc", "134217728",
            "-f", kind, "-i", str(path)]


def ffmpeg(path: Path, kind: str, output: list[str], *, seek=0, binary=False) -> bytes:
    return run(media.FFMPEG, [
        "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-threads", "2", "-filter_threads", "1", "-filter_complex_threads", "1",
        *(["-ss", f"{seek:.6f}"] if seek else []), *input_args(path, kind),
        *output,
    ], binary=binary)


def probe(path: Path, kind: str) -> tuple[list[dict], float]:
    result = run(media.FFPROBE, [
        "-v", "error", "-probesize", "5000000", "-analyzeduration", "5000000",
        *input_args(path, kind), "-show_entries",
        "stream=codec_type,codec_name,width,height,duration,avg_frame_rate,sample_rate,channels,bit_rate:stream_disposition=attached_pic:format=duration",
        "-of", "json",
    ], binary=True)
    payload = json.loads(result)
    return payload.get("streams", []), number(payload.get("format", {}).get("duration"))


def number(value) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else 0
    except (ValueError, TypeError):
        return 0


def dimensions(width: int, height: int) -> None:
    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION and width * height <= MAX_PIXELS):
        raise reject("A imagem ou o vídeo excede o limite de dimensões (4096 px por eixo e 9,4 MP).")


def vag_data(path: Path, *, require_theme=False) -> tuple[np.ndarray, int]:
    if not vag.HEADER_SIZE + 2 * vag.BLOCK_SIZE <= path.stat().st_size <= 512 * 1024:
        raise reject("Tamanho VAG inválido.")
    blob = path.read_bytes()
    if blob[:4] != b"VAGp":
        raise reject("Cabeçalho VAG inválido.")
    size, rate = struct.unpack_from(">II", blob, 12)
    data = blob[vag.HEADER_SIZE:]
    if size != len(data) or not rate or rate > 96000 or size % vag.BLOCK_SIZE:
        raise reject("Cabeçalho VAG inválido.")
    if any((b >> 4) > 4 or (b & 15) > 12 for b in data[::vag.BLOCK_SIZE]):
        raise reject("Bloco VAG inválido.")
    max_blocks = math.ceil(5 * rate / vag.SAMPLES_PER_BLOCK) + 1
    if require_theme and (rate != 48000 or size > max_blocks * vag.BLOCK_SIZE):
        raise reject("O VAG do tema deve ter 48 kHz e até 5 segundos.")
    return vag.decode(blob)


def at9_loop(path: Path, duration: float, rate: int) -> bool:
    with path.open("rb") as source:
        head = source.read(12)
        size = path.stat().st_size
        if head[:4] != b"RIFF" or head[8:] != b"WAVE" or struct.unpack_from("<I", head, 4)[0] + 8 != size:
            return False
        for _ in range(128):
            chunk = source.read(8)
            if len(chunk) < 8:
                break
            length = struct.unpack_from("<I", chunk, 4)[0]
            if source.tell() + length > size:
                break
            if chunk[:4] == b"smpl" and length >= 60:
                body = source.read(60)
                loop_count = struct.unpack_from("<I", body, 28)[0]
                _, loop_type, start, end, _, repeats = struct.unpack_from("<6I", body, 36)
                return (loop_count == 1 and loop_type == 0 and repeats == 0 and start == 0 and
                        0 < end <= duration * rate + 2048 and end >= duration * rate - 2048)
            source.seek(length + length % 2, 1)
    return False


def audio_points(pcm: np.ndarray) -> list[float]:
    if not pcm.size:
        raise reject("A mídia não contém áudio decodificável.")
    return [round(float(np.max(np.abs(p.astype(np.int32)))) / 32768, 4)
            for p in np.array_split(pcm, min(pcm.size, 96))]


def thumbnail(image: Image.Image, path: Path):
    thumb = image.convert("RGBA")
    thumb.thumbnail((480, 270))
    thumb.save(path, format="WEBP", quality=82)


def inspect_asset(path: Path, name: str, preview: Path) -> Asset:
    with path.open("rb") as source:
        head = source.read(64)
    base = {"id": path.name, "name": safe_name(name), "bytes": path.stat().st_size, "sha256": sha256(path)}
    suffix = Path(name.lower()).suffix
    if suffix == ".vag" and not head.startswith(b"VAGp"):
        raise reject("O arquivo .vag não tem cabeçalho VAG.")
    if head.startswith(b"VAGp"):
        pcm, rate = vag_data(path)
        duration = len(pcm) / rate
        if duration > MAX_DURATION:
            raise reject("O áudio excede a duração máxima.")
        return Asset(**base, kind="vag", mime="application/octet-stream", duration=duration,
                     codec="PS-ADPCM", peak=audio_points(pcm))

    # A recognized container is required before any demuxing.
    container = demuxer(head)
    if container in {None, "gif"}:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path, formats=list(STILL)) as image:
                    dimensions(*image.size)
                    fmt = image.format
                    animated = bool(getattr(image, "is_animated", False))
                    if not animated:
                        thumbnail(ImageOps.exif_transpose(image), preview)
                        return Asset(**base, kind="image", mime=STILL[fmt], codec=fmt,
                                     width=image.width, height=image.height)
                    if fmt != "GIF":
                        raise reject("Use GIF, MP4 ou WebM para animação.")
        except (Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError, OSError):
            raise reject() from None
    if container is None:
        raise reject()
    streams, duration = probe(path, container)
    visual = next((s for s in streams if s.get("codec_type") == "video" and
                   not s.get("disposition", {}).get("attached_pic")), None)
    sound = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not visual and not sound:
        raise reject()
    stream = visual or sound
    duration = number(stream.get("duration")) or duration
    if not 0 < duration <= MAX_DURATION:
        raise reject(f"A mídia precisa ter duração conhecida e até {MAX_DURATION} segundos.")
    codec = stream.get("codec_name")
    if not codec:
        raise reject()
    if suffix == ".at9" and codec != "atrac9":
        raise reject("O arquivo .at9 não contém áudio ATRAC9.")
    if visual:
        size = (int(stream.get("width") or 0), int(stream.get("height") or 0))
        dimensions(*size)
        mimetype = FORMATS[container][0]
        if not mimetype:
            raise reject()
        thumb = extract_frame(path, 0, (480, 270))
        thumbnail(thumb, preview)
        try:
            fps = float(Fraction(stream.get("avg_frame_rate", "0/1")))
        except (ValueError, ZeroDivisionError):
            fps = 0
        return Asset(**base, kind="animation" if container == "gif" else "video", mime=mimetype,
                     codec=codec, duration=duration, width=size[0], height=size[1], fps=fps)

    if not 1 <= int(sound.get("channels", 0)) <= 8 or not 0 < int(sound.get("sample_rate", 0)) <= 192000:
        raise reject("O áudio excede os limites de canais ou frequência.")
    raw = ffmpeg(path, container, ["-vn", "-t", str(MAX_DURATION), "-ar", "4000", "-ac", "1",
                                  "-f", "s16le", "-"], binary=True)
    peak = audio_points(np.frombuffer(raw, dtype="<i2"))
    if codec == "atrac9":
        rate = int(sound.get("sample_rate", 0))
        bitrate = int(sound.get("bit_rate", 0))
        if container != "wav" or rate != 48000 or not 139000 <= bitrate <= 147000 or not at9_loop(path, duration, rate):
            raise reject(f"O AT9 precisa de ATRAC9 a 48 kHz, {DEFAULT_BITRATE} kbps e loop completo.")
        return Asset(**base, kind="at9", mime="application/octet-stream", codec=codec,
                     duration=duration, peak=peak, loudness=loudness(path))
    return Asset(**base, kind="audio", mime=FORMATS[container][1], codec=codec, duration=duration,
                 peak=peak, loudness=loudness(path))


def loudness(path: Path) -> float | None:
    try:
        output = run(media.FFMPEG, [
            "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
            "-threads", "2", "-filter_threads", "1", *input_args(path, detected(path)),
            "-map", "0:a:0", "-vn", "-t", str(MAX_DURATION), "-af", "loudnorm=print_format=json",
            "-f", "null", "-",
        ], capture_log=True).decode("utf-8", "replace")
        payload = json.loads(output[output.rfind("{"):output.rfind("}") + 1])
        value = float(payload["input_i"])
        return value if math.isfinite(value) else None
    except (StudioError, KeyError, ValueError):
        return None


def detected(path: Path) -> str:
    with path.open("rb") as source:
        kind = demuxer(source.read(64))
    if not kind:
        raise reject()
    return kind


def extract_frame(path: Path, timestamp: float, bounds: tuple[int, int]) -> Image.Image:
    w, h = bounds
    data = ffmpeg(path, detected(path), [
        "-map", "0:V:0", "-an", "-frames:v", "1", "-vf",
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,setsar=1",
        "-threads", "1", "-c:v", "png", "-f", "image2pipe", "-",
    ], seek=timestamp, binary=True)
    if not data:
        raise StudioError(422, "frame_missing", "O corte selecionado não contém um quadro decodificável.")
    with Image.open(io.BytesIO(data), formats=["PNG"]) as frame:
        return frame.convert("RGBA")


def image_at(path: Path) -> Image.Image:
    with Image.open(path, formats=list(STILL)) as image:
        return ImageOps.exif_transpose(image).convert("RGBA")


def transform(image: Image.Image, size: tuple[int, int], edit: Transform,
              background=(0, 0, 0, 0)) -> Image.Image:
    w, h = size
    a, b, c, d = edit.crop
    image = image.crop((int(a * image.width), int(b * image.height),
                        math.ceil(c * image.width), math.ceil(d * image.height)))
    if edit.flipX:
        image = ImageOps.mirror(image)
    if edit.flipY:
        image = ImageOps.flip(image)
    fit = min if edit.fit == "contain" else max
    scale = fit(w / image.width, h / image.height) * edit.zoom
    alpha = image.getchannel("A")
    rgb = image.convert("RGB")
    for enhancer, factor in ((ImageEnhance.Brightness, edit.brightness), (ImageEnhance.Contrast, edit.contrast),
                             (ImageEnhance.Color, edit.saturation)):
        if factor != 1:
            rgb = enhancer(rgb).enhance(factor)
    if edit.opacity != 1:
        alpha = alpha.point(lambda p: round(p * edit.opacity))
    rgb.putalpha(alpha)
    # Inverse affine mapping keeps memory bounded even for extreme crops and zoom.
    radians = math.radians(edit.rotation)
    cosine, sine = math.cos(radians) / scale, math.sin(radians) / scale
    cx, cy = w * (0.5 + edit.x), h * (0.5 + edit.y)
    matrix = (cosine, sine, image.width / 2 - cosine * cx - sine * cy,
              -sine, cosine, image.height / 2 + sine * cx - cosine * cy)
    styled = rgb.transform(size, Image.Transform.AFFINE, matrix, resample=Image.Resampling.BICUBIC)
    canvas = Image.new("RGBA", size, background)
    canvas.alpha_composite(styled)
    return canvas


def icon_glow(icon: Image.Image, size: tuple[int, int], intensity: float) -> Image.Image:
    canvas = Image.new("RGBA", size)
    canvas.alpha_composite(icon, ((size[0] - icon.width) // 2, (size[1] - icon.height) // 2))
    if intensity == 0:
        return canvas
    alpha = canvas.getchannel("A").filter(ImageFilter.GaussianBlur(7))
    glow = Image.new("RGBA", size, "white")
    glow.putalpha(alpha.point(lambda p: min(255, round(p * intensity * 2))))
    glow.alpha_composite(canvas)
    return glow


def audio_interval(asset: Asset, track: Track, max_length: float) -> float:
    end = asset.duration if track.end is None else track.end
    if not track.start < end <= asset.duration + 0.001:
        raise StudioError(422, "audio_trim", "O corte de áudio precisa estar dentro da faixa.")
    return min(end - track.start, max_length)


def audio_pcm(path: Path, asset: Asset, track: Track, limit: float, channels: int) -> np.ndarray:
    duration = audio_interval(asset, track, limit)
    filters = []
    if track.fadeIn:
        filters.append(f"afade=t=in:d={min(duration, track.fadeIn):.6f}")
    if track.fadeOut:
        fade = min(duration, track.fadeOut)
        filters.append(f"afade=t=out:st={duration - fade:.6f}:d={fade:.6f}")
    if asset.kind == "vag":
        pcm, rate = vag_data(path)
        raw = pcm[round(track.start * rate):round((track.start + duration) * rate)]
        raw = np.interp(np.arange(round(len(raw) * 48000 / rate)) * rate / 48000,
                        np.arange(len(raw)), raw) if raw.size else raw
        ramp_in = min(raw.size, round(track.fadeIn * 48000))
        ramp_out = min(raw.size, round(track.fadeOut * 48000))
        if ramp_in:
            raw[:ramp_in] = raw[:ramp_in] * np.linspace(0, 1, ramp_in)
        if ramp_out:
            raw[-ramp_out:] = raw[-ramp_out:] * np.linspace(1, 0, ramp_out)
    else:
        args = ["-map", "0:a:0", "-vn", "-t", f"{duration:.6f}", "-ar", "48000", "-ac", str(channels)]
        if filters:
            args += ["-af", ",".join(filters)]
        decoded = ffmpeg(path, detected(path), [*args, "-f", "s16le", "-"], seek=track.start, binary=True)
        raw = np.frombuffer(decoded, dtype="<i2")
    if not raw.size:
        raise reject("O corte não contém amostras de áudio.")
    signal = raw.astype(np.float32)
    peak = float(np.max(np.abs(signal)))
    scale = 10 ** (track.gain / 20) * (0.98 * 32767 / peak if track.normalize and peak else 1)
    return np.clip(signal * scale, -32768, 32767).astype("<i2")


def wav_bytes(pcm: np.ndarray, channels: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((channels, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(pcm.tobytes())
    return output.getvalue()
