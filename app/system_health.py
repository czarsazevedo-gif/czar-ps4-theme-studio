"""Real-time 3-state health monitoring (ok/off/err) and one-tap tool activation for CZAR Studio.

States for every tool:
- ``ok``  (verde)    : present and verified working by a real functional self-check
- ``off`` (cinza)    : absent / not installed on this server
- ``err`` (vermelho) : present/configured, but failing execution or validation
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from . import at9, dds, dxt1, media, pkg
from .config import PROJECT_ROOT
from .forge import pkg_forge

VALID_SETUP_TARGETS = ("all", "pkg", "at9", "magick")
HEALTH_CACHE_TTL_S = 10.0

_HEALTH_LOCK = threading.Lock()
_HEALTH_CACHE: dict | None = None
_HEALTH_CACHE_TS: float = 0.0

_SETUP_LOCK = threading.Lock()
_SETUP_STATE: dict = {
    "status": "idle",  # idle | running | done | error
    "target": None,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "message": "",
    "logs": [],
}


def sync_runtime_tool_bindings() -> None:
    """Refresh tool paths in-process without modifying canonical generators."""
    media.refresh_tools()
    # Sync dds.MAGICK dynamically without editing canonical app/dds.py
    setattr(dds, "MAGICK", media.MAGICK)


def invalidate_health_cache() -> None:
    global _HEALTH_CACHE, _HEALTH_CACHE_TS
    with _HEALTH_LOCK:
        _HEALTH_CACHE = None
        _HEALTH_CACHE_TS = 0.0


def _probe_ffmpeg() -> dict:
    if not media.FFMPEG:
        return {
            "id": "ffmpeg",
            "label": "ffmpeg",
            "state": "off",
            "status_label": "Ausente",
            "detail": "Binário ffmpeg não encontrado no PATH.",
            "path": None,
            "installable": False,
            "setup_target": None,
        }
    try:
        proc = subprocess.run(
            [media.FFMPEG, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            creationflags=media.NO_WINDOW,
        )
        out = (proc.stdout or proc.stderr).decode("utf-8", "replace").strip()
        first = out.splitlines()[0] if out else ""
        if proc.returncode == 0 and first:
            return {
                "id": "ffmpeg",
                "label": "ffmpeg",
                "state": "ok",
                "status_label": "Funcionando",
                "detail": first,
                "path": media.FFMPEG,
                "installable": False,
                "setup_target": None,
            }
        return {
            "id": "ffmpeg",
            "label": "ffmpeg",
            "state": "err",
            "status_label": "Defeituosa",
            "detail": f"ffmpeg retornou código {proc.returncode}: {first or 'sem saída'}",
            "path": media.FFMPEG,
            "installable": False,
            "setup_target": None,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "id": "ffmpeg",
            "label": "ffmpeg",
            "state": "err",
            "status_label": "Defeituosa",
            "detail": f"Falha ao executar ffmpeg: {exc}",
            "path": media.FFMPEG,
            "installable": False,
            "setup_target": None,
        }


def _probe_magick() -> dict:
    magick_bin = media.MAGICK
    if not magick_bin:
        return {
            "id": "magick",
            "label": "ImageMagick",
            "state": "off",
            "status_label": "Ausente",
            "detail": "Não instalado (opcional — toque em Ativar ou use o encoder nativo).",
            "path": None,
            "installable": True,
            "setup_target": "magick",
        }
    try:
        rgba = bytes([255, 128, 64, 255] * 16)
        proc = subprocess.run(
            [
                magick_bin,
                "-size", "4x4", "-depth", "8", "rgba:-",
                "-alpha", "off",
                "-define", "dds:compression=dxt1a",
                "-define", "dds:mipmaps=0",
                "dds:-",
            ],
            input=rgba,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            creationflags=media.NO_WINDOW,
        )
        if (
            proc.returncode == 0
            and len(proc.stdout) >= 136
            and proc.stdout[:4] == b"DDS "
            and proc.stdout[84:88] == b"DXT1"
        ):
            return {
                "id": "magick",
                "label": "ImageMagick",
                "state": "ok",
                "status_label": "Funcionando",
                "detail": f"DXT1a validado ({magick_bin})",
                "path": magick_bin,
                "installable": True,
                "setup_target": "magick",
            }
        err_tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = err_tail[-1] if err_tail else f"código {proc.returncode} (saída DDS inválida)"
        return {
            "id": "magick",
            "label": "ImageMagick",
            "state": "err",
            "status_label": "Defeituosa",
            "detail": f"ImageMagick falhou no teste DXT1a: {msg}",
            "path": magick_bin,
            "installable": True,
            "setup_target": "magick",
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "id": "magick",
            "label": "ImageMagick",
            "state": "err",
            "status_label": "Defeituosa",
            "detail": f"Falha ao executar ImageMagick: {exc}",
            "path": magick_bin,
            "installable": True,
            "setup_target": "magick",
        }


def _probe_encoder_native() -> dict:
    try:
        block = np.full((4, 4, 4), 255, dtype=np.uint8)
        out = dxt1.encode_dds(block)
        if len(out) == 136 and out[:4] == b"DDS " and out[84:88] == b"DXT1":
            return {
                "id": "encoder",
                "label": f"encoder {dds.DEFAULT_BACKEND}",
                "state": "ok",
                "status_label": "Funcionando",
                "detail": "Codificador DXT1a nativo validado (4×4 -> DDS DXT1).",
                "path": "app/dxt1.py",
                "installable": False,
                "setup_target": None,
            }
        return {
            "id": "encoder",
            "label": f"encoder {dds.DEFAULT_BACKEND}",
            "state": "err",
            "status_label": "Defeituosa",
            "detail": "Saída DXT1a inválida do encoder nativo.",
            "path": "app/dxt1.py",
            "installable": False,
            "setup_target": None,
        }
    except Exception as exc:
        return {
            "id": "encoder",
            "label": f"encoder {dds.DEFAULT_BACKEND}",
            "state": "err",
            "status_label": "Defeituosa",
            "detail": f"Erro no encoder nativo: {exc}",
            "path": "app/dxt1.py",
            "installable": False,
            "setup_target": None,
        }


def _probe_at9() -> dict:
    info = at9.encoder_info()
    if not info.get("available"):
        return {
            "id": "at9",
            "label": "ATRAC9",
            "state": "off",
            "status_label": "Ausente",
            "detail": "Codificador ATRAC9 não instalado. Toque em Ativar.",
            "path": None,
            "installable": True,
            "setup_target": "at9",
        }
    backend = info.get("backend") or "at9"
    path = info.get("path")
    try:
        with tempfile.TemporaryDirectory(prefix="czar-at9-probe-") as td:
            wav_p = Path(td) / "probe.wav"
            at9_p = Path(td) / "probe.at9"
            n_samples = 2400  # 0.05 s @ 48 kHz
            t = np.arange(n_samples, dtype=np.float32) / 48000.0
            tone = (np.sin(2.0 * np.pi * 440.0 * t) * 8000.0).astype(np.int16)
            stereo = np.column_stack([tone, tone]).ravel()
            with wave.open(str(wav_p), "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(48000)
                wf.writeframes(stereo.tobytes())
            at9.encode(wav_p, at9_p, bitrate=144, loop=True, validate=False)
            raw = at9_p.read_bytes()
            if len(raw) < 64 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
                raise RuntimeError("Cabeçalho RIFF/WAVE inválido no autoteste.")
        return {
            "id": "at9",
            "label": "ATRAC9",
            "state": "ok",
            "status_label": "Funcionando",
            "detail": f"{backend} · 48 kHz estéreo 144 kbps validado.",
            "path": path,
            "installable": True,
            "setup_target": "at9",
        }
    except Exception as exc:
        return {
            "id": "at9",
            "label": "ATRAC9",
            "state": "err",
            "status_label": "Defeituosa",
            "detail": f"Codificador ATRAC9 presente mas falhou no autoteste: {exc}",
            "path": path,
            "installable": True,
            "setup_target": "at9",
        }


def _probe_pkg() -> dict:
    info = pkg_forge.pkg_forge_info()
    if not info.get("available"):
        # Check if a broken non-executable file exists at the expected location
        expected = PROJECT_ROOT / "tools" / "bin" / "liborbispkg" / "pkgtool"
        if expected.exists() and not os.access(expected, os.X_OK):
            return {
                "id": "pkg",
                "label": "PKG Tools",
                "state": "err",
                "status_label": "Defeituosa",
                "detail": "pkgtool encontrado sem permissão de execução. Toque em Reparar.",
                "path": str(expected),
                "installable": True,
                "setup_target": "pkg",
            }
        return {
            "id": "pkg",
            "label": "PKG Tools",
            "state": "off",
            "status_label": "Ausente",
            "detail": "Empacotador PKG não instalado. Toque em Ativar.",
            "path": None,
            "installable": True,
            "setup_target": "pkg",
        }
    backend = info.get("backend") or "pkg"
    bin_path = info.get("path")
    try:
        if backend == "liborbispkg-linux" and bin_path:
            proc = subprocess.run(
                [bin_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                creationflags=media.NO_WINDOW,
            )
            combined = (proc.stdout + proc.stderr).decode("utf-8", "replace")
            if proc.returncode == 0 and "pkg_build" in combined and "pkg_validate" in combined:
                return {
                    "id": "pkg",
                    "label": "PKG Tools",
                    "state": "ok",
                    "status_label": "Funcionando",
                    "detail": "LibOrbisPkg Linux (IRO_TAG=2 · pkg_build/pkg_validate) pronto.",
                    "path": bin_path,
                    "installable": True,
                    "setup_target": "pkg",
                }
            tail = combined.strip().splitlines()
            reason = tail[-1] if tail else f"código {proc.returncode}"
            return {
                "id": "pkg",
                "label": "PKG Tools",
                "state": "err",
                "status_label": "Defeituosa",
                "detail": f"pkgtool falhou ao iniciar: {reason}",
                "path": bin_path,
                "installable": True,
                "setup_target": "pkg",
            }
        if backend == "orbis-pub-cmd" and bin_path and Path(bin_path).is_file():
            return {
                "id": "pkg",
                "label": "PKG Tools",
                "state": "ok",
                "status_label": "Funcionando",
                "detail": "orbis-pub-cmd pronto.",
                "path": bin_path,
                "installable": False,
                "setup_target": None,
            }
        return {
            "id": "pkg",
            "label": "PKG Tools",
            "state": "err",
            "status_label": "Defeituosa",
            "detail": f"Backend PKG inválido ({backend}).",
            "path": bin_path,
            "installable": True,
            "setup_target": "pkg",
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "id": "pkg",
            "label": "PKG Tools",
            "state": "err",
            "status_label": "Defeituosa",
            "detail": f"Falha ao executar PKG Tools: {exc}",
            "path": bin_path,
            "installable": True,
            "setup_target": "pkg",
        }


def get_system_health(*, force: bool = False) -> dict:
    """Return cached or freshly probed 3-state health for all 5 Studio tools."""
    global _HEALTH_CACHE, _HEALTH_CACHE_TS
    now = time.monotonic()
    with _HEALTH_LOCK:
        if not force and _HEALTH_CACHE is not None and (now - _HEALTH_CACHE_TS) < HEALTH_CACHE_TTL_S:
            return dict(_HEALTH_CACHE)

    sync_runtime_tool_bindings()
    tools = {
        "ffmpeg": _probe_ffmpeg(),
        "magick": _probe_magick(),
        "encoder": _probe_encoder_native(),
        "at9": _probe_at9(),
        "pkg": _probe_pkg(),
    }
    any_err = any(t["state"] == "err" for t in tools.values())
    any_missing_installable = any(
        t["installable"] and t["state"] in ("off", "err") for t in tools.values()
    )
    snapshot = {
        "ok": not any_err and tools["ffmpeg"]["state"] == "ok" and tools["encoder"]["state"] == "ok",
        "checked_at": time.time(),
        "any_defective": any_err,
        "needs_activation": any_missing_installable,
        "tools": tools,
        "pkg_tools_ready": tools["pkg"]["state"] == "ok",
        "at9_ready": tools["at9"]["state"] == "ok",
        "magick_ready": tools["magick"]["state"] == "ok",
        "backends": dds.available_backends(),
    }
    with _HEALTH_LOCK:
        _HEALTH_CACHE = snapshot
        _HEALTH_CACHE_TS = time.monotonic()
    return dict(snapshot)


def _setup_snapshot_unlocked() -> dict:
    return {
        "status": _SETUP_STATE["status"],
        "target": _SETUP_STATE["target"],
        "started_at": _SETUP_STATE["started_at"],
        "finished_at": _SETUP_STATE["finished_at"],
        "returncode": _SETUP_STATE["returncode"],
        "message": _SETUP_STATE["message"],
        "logs": list(_SETUP_STATE["logs"]),
    }


def get_tool_setup_status() -> dict:
    with _SETUP_LOCK:
        return _setup_snapshot_unlocked()


def _run_setup_worker(target: str) -> None:
    script = PROJECT_ROOT / "scripts" / "install_forge_tools.sh"
    try:
        proc = subprocess.Popen(
            ["bash", str(script), target],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = line.rstrip("\r\n")
            if clean:
                with _SETUP_LOCK:
                    _SETUP_STATE["logs"].append(clean)
                    if len(_SETUP_STATE["logs"]) > 200:
                        _SETUP_STATE["logs"] = _SETUP_STATE["logs"][-200:]
        rc = proc.wait(timeout=600)
        sync_runtime_tool_bindings()
        invalidate_health_cache()
        health = get_system_health(force=True)
        with _SETUP_LOCK:
            _SETUP_STATE["returncode"] = rc
            _SETUP_STATE["finished_at"] = time.time()
            if rc == 0:
                _SETUP_STATE["status"] = "done"
                _SETUP_STATE["message"] = (
                    "Ferramentas ativadas e verificadas com sucesso!"
                    if not health["any_defective"]
                    else "Ativação concluída com avisos; confira as luzes de status."
                )
            else:
                _SETUP_STATE["status"] = "error"
                tail = _SETUP_STATE["logs"][-1] if _SETUP_STATE["logs"] else f"código {rc}"
                _SETUP_STATE["message"] = f"Falha na ativação ({target}): {tail}"
    except Exception as exc:
        invalidate_health_cache()
        with _SETUP_LOCK:
            _SETUP_STATE["returncode"] = -1
            _SETUP_STATE["finished_at"] = time.time()
            _SETUP_STATE["status"] = "error"
            _SETUP_STATE["message"] = f"Erro inesperado ao ativar ferramentas: {exc}"
            _SETUP_STATE["logs"].append(str(exc))


def start_tool_setup(target: str = "all") -> dict:
    """Start non-blocking background installation/repair of allowlisted external tools."""
    target = (target or "all").strip().lower()
    if target not in VALID_SETUP_TARGETS:
        raise ValueError(f"Alvo inválido: {target!r}. Use um de: {', '.join(VALID_SETUP_TARGETS)}.")
    script = PROJECT_ROOT / "scripts" / "install_forge_tools.sh"
    if not script.is_file():
        raise FileNotFoundError("Script scripts/install_forge_tools.sh não encontrado.")

    with _SETUP_LOCK:
        if _SETUP_STATE["status"] == "running":
            return {
                "started": False,
                "already_running": True,
                "setup": _setup_snapshot_unlocked(),
            }
        _SETUP_STATE["status"] = "running"
        _SETUP_STATE["target"] = target
        _SETUP_STATE["started_at"] = time.time()
        _SETUP_STATE["finished_at"] = None
        _SETUP_STATE["returncode"] = None
        _SETUP_STATE["message"] = f"Ativando ferramentas ({target})…"
        _SETUP_STATE["logs"] = [f"[CZAR Studio] Iniciando ativação: {target}…"]
        snapshot = _setup_snapshot_unlocked()

    t = threading.Thread(target=_run_setup_worker, args=(target,), daemon=True, name=f"czar-setup-{target}")
    t.start()
    return {
        "started": True,
        "already_running": False,
        "setup": snapshot,
    }
