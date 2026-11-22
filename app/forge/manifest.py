"""Build & Validation Integrity Manifest + Dependency Fingerprinting (Operação Fortaleza — Fase 8).

Tracks deterministic SHA-256 fingerprints across ALL theme inputs, configurations,
build artifacts, tool versions/commits, and verification results so that ANY
modification to an input or setting automatically invalidates stale approvals.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import at9, jobs, media
from ..config import PROJECT_ROOT
from .audio_validation import find_vgmstream_cli
from .contracts import EXTERNAL_TOOLS
from .pkg_forge import find_liborbispkg_tool, pkg_forge_info


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _stable_json_sha256(obj: Any) -> str:
    encoded = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _collect_source_fingerprints(params: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Collect exact SHA-256 digests of every source job/asset feeding the PKG build."""
    sources: dict[str, Any] = {"slots": {}, "sfx": {}, "audio": None, "animated": None}
    token = jobs.project_workspace.set(workspace)
    try:
        for slot_id, jid in sorted((params.get("slots") or {}).items()):
            if not jid:
                continue
            j = jobs.load("slot", str(jid))
            if j is None:
                sources["slots"][slot_id] = {"job_id": jid, "missing": True}
                continue
            sources["slots"][slot_id] = {
                "job_id": jid,
                "original_name": j.original_name,
                "source_sha256": _sha256_file(j.source),
                "out_png_sha256": _sha256_file(j.out_dir / f"{slot_id.replace(':', '-')}.png"),
            }

        audio_jid = str(params.get("audio_job") or "")
        if audio_jid:
            aj = jobs.load("audio", audio_jid)
            if aj is not None:
                conv = aj.meta.get("converted")
                out_blob = (aj.out_dir / str(conv)) if conv else aj.source
                sources["audio"] = {
                    "job_id": audio_jid,
                    "original_name": aj.original_name,
                    "source_sha256": _sha256_file(aj.source),
                    "converted_sha256": _sha256_file(out_blob),
                    "loudness": aj.meta.get("loudness"),
                }

        for sfx_slot, jid in sorted((params.get("sfx") or {}).items()):
            if not jid:
                continue
            sj = jobs.load("sfx", str(jid))
            if sj is None:
                sources["sfx"][sfx_slot] = {"job_id": jid, "missing": True}
                continue
            vag_out = sj.out_dir / f"{sfx_slot}.vag"
            sources["sfx"][sfx_slot] = {
                "job_id": jid,
                "original_name": sj.original_name,
                "source_sha256": _sha256_file(sj.source),
                "vag_sha256": _sha256_file(vag_out),
            }

        anim_jid = str(params.get("animated_job") or "")
        if anim_jid:
            mj = jobs.load("animated", anim_jid)
            if mj is not None:
                deluxe = mj.meta.get("deluxe") if isinstance(mj.meta, dict) else None
                dds_dir = mj.root / "deluxe_dds"
                dds_shas = (
                    {p.name: _sha256_file(p) for p in sorted(dds_dir.glob("*.dds"))}
                    if dds_dir.is_dir()
                    else {}
                )
                sources["animated"] = {
                    "job_id": anim_jid,
                    "original_name": mj.original_name,
                    "source_sha256": _sha256_file(mj.source),
                    "deluxe_result_key": deluxe.get("resultKey") if isinstance(deluxe, dict) else None,
                    "deluxe_dds_hashes": dds_shas,
                    "frames": params.get("frames"),
                    "wait_seconds": params.get("wait_seconds"),
                    "backend": params.get("backend"),
                    "animation_size": params.get("animation_size"),
                }
    finally:
        jobs.project_workspace.reset(token)
    return sources


def compute_theme_inputs_fingerprint(params: dict[str, Any], workspace: Path) -> tuple[str, dict[str, Any]]:
    """Compute a deterministic SHA-256 over all effective inputs and settings of a PS4 theme build."""
    sources = _collect_source_fingerprints(params, workspace)
    comp = params.get("composition")
    comp_dump = comp.model_dump() if hasattr(comp, "model_dump") else comp
    payload = {
        "title": str(params.get("title") or "").strip(),
        "title_id": str(params.get("title_id") or "").strip().upper(),
        "content_id": str(params.get("content_id") or "").strip().upper(),
        "category": str(params.get("category") or "ac").strip().lower(),
        "mode": str(params.get("mode") or "theme").strip().lower(),
        "themecolor": int(params.get("themecolor", 0)),
        "fontcolor": str(params.get("fontcolor") or "").upper(),
        "fontshadowcolor": str(params.get("fontshadowcolor") or "").upper(),
        "focuscolor": str(params.get("focuscolor") or "").upper(),
        "composition_mode": str(params.get("composition_mode") or ""),
        "composition": comp_dump,
        "frames": int(params.get("frames") or 24) if params.get("animated_job") else 0,
        "wait_seconds": round(float(params.get("wait_seconds") or 0.1), 6) if params.get("animated_job") else 0.0,
        "backend": str(params.get("backend") or "") if params.get("animated_job") else "",
        "animation_size": str(params.get("animation_size") or "") if params.get("animated_job") else "",
        "sources": sources,
    }
    return _stable_json_sha256(payload), payload


def _tool_versions_snapshot() -> dict[str, Any]:
    git_head = ""
    try:
        git_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True, stderr=subprocess.DEVNULL, timeout=5
        ).strip()
    except Exception:
        pass
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "repo_head": git_head,
        "ffmpeg": media.FFMPEG or None,
        "ffprobe": media.FFPROBE or None,
        "vgmstream_cli": find_vgmstream_cli(),
        "atrac9_encoder": at9.encoder_info(),
        "pkg_forge": pkg_forge_info(),
        "liborbispkg_bin": str(find_liborbispkg_tool()) if find_liborbispkg_tool() else None,
        "pinned_external_references": EXTERNAL_TOOLS,
    }


def build_fortaleza_manifest(
    *,
    params: dict[str, Any],
    workspace: Path,
    bundle_report: dict[str, Any],
    pkg_report: dict[str, Any],
    digital_twin: dict[str, Any] | None = None,
    external_verifiers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build complete Fase 8 integrity & provenance manifest for a PS4 Theme PKG build."""
    inputs_fp, inputs_detail = compute_theme_inputs_fingerprint(params, workspace)
    raf_sim = (digital_twin or {}).get("raf_simulation") or {}
    audio_sim = (digital_twin or {}).get("audio_simulation") or {}
    return {
        "manifest_version": "czar-fortaleza-manifest-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs_fingerprint": inputs_fp,
        "project_revision": params.get("project_revision"),
        "theme_identity": {
            "title": params.get("title"),
            "title_id": pkg_report.get("title_id") or params.get("title_id"),
            "content_id": pkg_report.get("content_id") or params.get("content_id"),
            "category": pkg_report.get("category"),
            "iro_tag_header": pkg_report.get("iro_tag_header"),
            "iro_tag_sfo": pkg_report.get("iro_tag_sfo"),
            "volume_type": pkg_report.get("volume_type"),
        },
        "outputs": {
            "pkg_sha256": pkg_report.get("pkg_sha256"),
            "pkg_bytes": pkg_report.get("pkg_bytes"),
            "extracted_files": pkg_report.get("files") or [],
            "bundle_files": bundle_report.get("files") or [],
        },
        "sources_and_settings": inputs_detail,
        "raf_properties": {
            "present": bool(raf_sim.get("present")),
            "frames": raf_sim.get("frames"),
            "resolution": raf_sim.get("resolution"),
            "total_dds_bytes": raf_sim.get("total_dds_bytes"),
            "cycle_duration_sec": raf_sim.get("cycle_duration_sec"),
            "effective_fps": raf_sim.get("effective_fps"),
            "simulated_cycles": raf_sim.get("simulated_cycles"),
        },
        "audio_properties": {
            "has_music": bool(bundle_report.get("has_music")),
            "bgmhome_loud": pkg_report.get("bgmhome_loud"),
            "bgm_validation": (audio_sim.get("bgm") or {}).get("validation"),
            "bgm_multi_cycle": (audio_sim.get("bgm") or {}).get("multi_cycle_simulation"),
            "sfx": audio_sim.get("sfx") or {},
        },
        "verification": {
            "verification_level": pkg_report.get("verification_level"),
            "package_status": pkg_report.get("package_status"),
            "release_gate": pkg_report.get("release_gate"),
            "release_gate_reasons": pkg_report.get("release_gate_reasons") or [],
            "bundle_valid": bool(bundle_report.get("valid")),
            "bundle_checks": bundle_report.get("checks") or [],
            "digital_twin_passed": (digital_twin or {}).get("passed"),
            "digital_twin_mode": (digital_twin or {}).get("verification_kind"),
            "external_verifiers": list(external_verifiers or []),
            "physical_compatibility": pkg_report.get("physical_compatibility", "pending"),
            "physical_evidence_sha256": pkg_report.get("physical_evidence_sha256"),
        },
        "tools_and_provenance": _tool_versions_snapshot(),
    }
