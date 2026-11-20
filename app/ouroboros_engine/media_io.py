"""Bounded media I/O for Ouroboros: FFmpeg/FFprobe reads, PyAV exact PTS inspection, and safe hashing."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .. import media


class MotionError(Exception):
    pass


class Cancelled(Exception):
    pass


def digest(path: Path, check: Callable[[], None] = lambda: None) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            check()
            result.update(chunk)
    return result.hexdigest()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def command(argv: list[str], check: Callable[[], None] = lambda: None, timeout: float = 75.0) -> bytes:
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


def probe_pyav_timestamps(source: Path, *, max_frames: int = 360) -> dict[str, Any] | None:
    """Use PyAV when available to inspect exact packet/frame timestamps without decoding full frames."""
    try:
        import av  # type: ignore
    except ImportError:
        return None
    try:
        pts_sec: list[float] = []
        with av.open(str(source)) as container:
            if not container.streams.video:
                return None
            vs = container.streams.video[0]
            tb = float(vs.time_base) if vs.time_base else None
            avg_fps = float(vs.average_rate) if vs.average_rate else None
            for packet in container.demux(vs):
                if packet.dts is None and packet.pts is None:
                    continue
                ts = packet.pts if packet.pts is not None else packet.dts
                if ts is not None and tb is not None:
                    pts_sec.append(float(ts) * tb)
                if len(pts_sec) >= max_frames:
                    break
        if len(pts_sec) < 2:
            return {"available": True, "samples": len(pts_sec), "avgFps": avg_fps}
        pts_sorted = np.sort(np.asarray(pts_sec, dtype=np.float64))
        diffs = np.diff(pts_sorted)
        pos = diffs[diffs > 1e-6]
        if pos.size == 0:
            return {"available": True, "samples": len(pts_sec), "avgFps": avg_fps}
        med = float(np.median(pos))
        cv = float(np.std(pos) / max(med, 1e-6))
        max_dev = float(np.max(np.abs(pos / max(med, 1e-6) - 1.0)))
        return {
            "available": True,
            "samples": len(pts_sec),
            "avgFps": round(avg_fps, 4) if avg_fps else round(1.0 / med, 4),
            "medianStepMs": round(med * 1000.0, 3),
            "stepCV": round(cv, 4),
            "maxStepDeviation": round(max_dev, 4),
            "isVFR": bool(max_dev > 0.08 or cv > 0.05),
        }
    except Exception:
        return None


def source_info(source: Path, duration: float, check: Callable[[], None] = lambda: None) -> dict[str, Any]:
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
    pyav_info = probe_pyav_timestamps(Path(source))
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
        "pyav": pyav_info,
    }


def extract(
    source: Path,
    start: float,
    end: float,
    count: int,
    size: tuple[int, int],
    folder: Path,
    check: Callable[[], None] = lambda: None,
) -> np.ndarray:
    """Only the selected window is decoded. Output is a bounded disk-backed RGB array."""
    if not 0 <= start < end or count < 1:
        raise MotionError("Intervalo de amostragem inválido.")
    path = folder / f"samples_{int(time.monotonic_ns())}.rgb"
    fps = count / (end - start)
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
    path.unlink()
    return samples
