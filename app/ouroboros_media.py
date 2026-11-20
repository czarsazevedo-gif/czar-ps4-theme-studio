"""Bounded source reads recovered from the ARCA checkpoint."""

import hashlib
import json
import subprocess
import time
from fractions import Fraction

import numpy as np

from . import media


class MotionError(Exception):
    pass


class Cancelled(Exception):
    pass


def digest(path, check):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            check()
            result.update(chunk)
    return result.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def command(argv, check, timeout=75):
    with subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as proc:
        deadline = time.monotonic() + timeout
        try:
            while True:
                check()
                try:
                    output, _ = proc.communicate(timeout=0.15)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise MotionError(
                            "A leitura do trecho excedeu o tempo disponível. Tente um trecho menor."
                        )
            if proc.returncode:
                raise MotionError("O decodificador não conseguiu ler o trecho da mídia.")
            return output
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def source_info(source, duration, check):
    raw = command(
        [
            media.FFPROBE,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-select_streams",
            "v:0",
            "-read_intervals",
            "%+#240",
            "-show_frames",
            "-show_entries",
            "stream=width,height,duration,avg_frame_rate,codec_name:frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(source),
        ],
        check,
    )
    data = json.loads(raw)
    stream = data["streams"][0]
    pts = [
        float(f["best_effort_timestamp_time"])
        for f in data.get("frames", [])
        if "best_effort_timestamp_time" in f
    ]
    steps = np.diff(pts)
    steps = steps[steps > 0]
    try:
        declared = float(Fraction(stream.get("avg_frame_rate", "0")))
    except (ZeroDivisionError, ValueError):
        declared = 0.0
    measured = float(1 / np.median(steps)) if len(steps) else None
    return {
        "duration": min(duration, float(stream["duration"]))
        if stream.get("duration") not in {None, "N/A", "0"}
        else duration,
        "width": stream["width"],
        "height": stream["height"],
        "codec": stream["codec_name"],
        "fps": declared or measured,
        "sampleFps": measured,
        "timestamps": pts,
        "irregular": bool(len(steps) and np.max(np.abs(steps / np.median(steps) - 1)) > 0.08),
    }


def extract(source, start, end, count, size, folder, check):
    """Only the selected window is decoded. Output is a bounded disk-backed RGB array."""
    if not 0 <= start < end or count < 1:
        raise MotionError("Intervalo de amostragem inválido.")
    path = folder / "samples.rgb"
    fps = count / (end - start)
    # Decode the frame already on screen at the in-point. Seeking directly to
    # a fractional in-point can duplicate the first post-seek frame with fps.
    seek = max(0.0, start - min(1.0, start))
    vf = (
        f"setpts=PTS-{start - seek:.10f}/TB,fps={fps:.10f}:start_time=0:round=up,"
        f"{media.scale_crop_filter(*size)}"
    )
    command(
        [
            media.FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-threads",
            "2",
            "-filter_threads",
            "1",
            "-protocol_whitelist",
            "file",
            "-ss",
            f"{seek:.8f}",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-vf",
            vf,
            "-frames:v",
            str(count),
            "-fps_mode",
            "passthrough",
            "-pix_fmt",
            "rgb24",
            "-threads:v",
            "1",
            "-f",
            "rawvideo",
            str(path),
        ],
        check,
    )
    if path.stat().st_size != count * size[0] * size[1] * 3:
        path.unlink(missing_ok=True)
        raise MotionError("Faltam quadros decodificáveis no fim do trecho. Antecipe a saída.")
    samples = np.memmap(path, dtype=np.uint8, mode="r", shape=(count, size[1], size[0], 3))
    path.unlink()  # Linux keeps mapped pages until the caller releases the array.
    return samples
