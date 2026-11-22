#!/usr/bin/env python3
"""Reproducible end-to-end PS4 animated-theme PKG build using a real user video.

Runs the actual upload / project-save / Forge PKG API, downloads the result,
and records input hashes and independently extracted-bundle evidence.

Requires the Forge toolchain and development dependencies. Set CZAR_WORKSPACE
to a disposable directory to avoid modifying anyone's existing projects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.forge.pkg_forge import find_liborbispkg_tool
from tests import test_gold_master_checkpoint_e as fixture


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _peak_dbfs(path: Path) -> float:
    """Reject silent benchmark inputs that would give false confidence in BGM tests."""
    run = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if run.returncode != 0:
        raise RuntimeError(f"Cannot decode benchmark music: {run.stderr[-700:]}")
    peaks = re.findall(r"max_volume:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dB", run.stderr)
    if not peaks:
        raise RuntimeError("FFmpeg did not report a music peak level")
    return float(peaks[-1])


def main_cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True, help="Video input (a short seamless segment is recommended)")
    parser.add_argument("--music", type=Path, required=True, help="Source music to encode into looped ATRAC9")
    parser.add_argument("--icon", type=Path, required=True, help="Custom image used for icons and wallpaper")
    parser.add_argument("--icons-source", type=Path, help="Extracted reference Image0 root: exercise every custom icon slot")
    parser.add_argument("--output", type=Path, required=True, help="Destination .pkg")
    parser.add_argument("--frames", type=int, default=24)
    parser.add_argument("--size", choices=("640x360", "960x540", "1280x720"), default="960x540")
    args = parser.parse_args()

    if not os.environ.get("CZAR_WORKSPACE"):
        parser.error("Set CZAR_WORKSPACE to an isolated QA directory before running this script")
    for path in (args.video, args.music, args.icon):
        if not path.is_file():
            parser.error(f"Missing input: {path}")
    peak_dbfs = _peak_dbfs(args.music)
    if peak_dbfs < -60:
        parser.error(
            f"Benchmark music is effectively silent (peak {peak_dbfs:.1f} dBFS). "
            "Provide audible music to actually exercise playback and loop correctness."
        )

    fixture.SAMPLE_MP4 = args.video.resolve()
    fixture.SAMPLE_MP3 = args.music.resolve()
    fixture.SAMPLE_PNG = args.icon.resolve()

    with TestClient(main.app) as client:
        session = client.get("/api/projects")
        if session.status_code != 200:
            raise RuntimeError(f"Failed to initialize project session: {session.text[:500]}")
        project_id, params = fixture._prepare_complete_animated_project(
            client,
            name="Crown of Thorns — independent Gold Master audit",
            frames=args.frames,
            size=args.size,
            theme_title="Coroa de Espinhos - QA Gold Master",
            theme_title_id="CUSA08652",
        )
        if args.icons_source is not None:
            # Exercise every icon slot, not just a single representative icon.
            # The trophy and browser slots retain the Crown custom art; the
            # remaining icons come from the user's extracted reference theme.
            manifest_resp = client.get(f"/api/projects/{project_id}")
            if manifest_resp.status_code != 200:
                raise RuntimeError(f"Failed to load saved project: {manifest_resp.text[:500]}")
            manifest = manifest_resp.json()
            state = manifest["state"]
            for folder, slot_prefix, skip in (
                ("function_icon", "fn", "trophy"),
                ("content_icon", "ct", "browser"),
            ):
                folder_path = args.icons_source / "texture" / folder
                if not folder_path.is_dir():
                    parser.error(f"Missing reference icon folder: {folder_path}")
                for icon in sorted(folder_path.glob("*.png")):
                    if icon.stem.endswith("_glow") or icon.stem == skip:
                        continue
                    key = f"{slot_prefix}:{icon.stem}"
                    with icon.open("rb") as uploaded:
                        upload = client.post(
                            f"/api/slot/{key}/upload", params=params,
                            files={"file": (icon.name, uploaded, "image/png")},
                        )
                    if upload.status_code != 200:
                        raise RuntimeError(f"Failed icon upload {key}: {upload.text[:500]}")
                    state["slots"][key] = upload.json()["job_id"]
            saved = client.put(
                f"/api/projects/{project_id}",
                json={"name": manifest["name"], "revision": manifest["revision"], "state": state},
            )
            if saved.status_code != 200:
                raise RuntimeError(f"Failed to persist all icon slots: {saved.text[:500]}")

        res = client.post(
            "/api/forge/pkg/build", params=params,
            data={"use_saved_project": "true", "sync": "true"},
        )
        if res.status_code != 200:
            raise RuntimeError(f"Forge build rejected: HTTP {res.status_code}: {res.text[:2500]}")
        data = res.json()
        report = data["pkg_report"]
        if not (
            report["theme_type_valid"]
            and report["verification_level"] == "full_bundle_verified"
            and report["bundle_hashes_matched"]
            and report["physical_compatibility"] == "pending"
        ):
            raise RuntimeError(f"Gold Master PKG audit FAILED: {json.dumps(report, indent=2)[:4000]}")

        required = {
            "Sc0/param.sfo", "Sc0/icon0.png", "Sc0/pic1.png",
            "Image0/theme.xml", "Image0/sound/bgm_home.at9",
            "Image0/sound/se_decide.vag",
            "Image0/scene/background.raf/index.xml",
            "Image0/scene/background.raf/plane.mdx",
            "Image0/scene/background.raf/00.dds",
            "Image0/texture/function_icon/trophy.png",
            "Image0/texture/function_icon/trophy_glow.png",
            "Image0/texture/content_icon/browser.png",
        }
        contained = {item["path"] for item in report["files"]}
        if args.icons_source is not None:
            for folder in ("function_icon", "content_icon"):
                required.update(
                    f"Image0/texture/{folder}/{p.name}"
                    for p in (args.icons_source / "texture" / folder).glob("*.png")
                )
        if missing := sorted(required - contained):
            raise RuntimeError(f"Required files missing from PKG: {missing}")

        dl = client.get(data["download"], params=params)
        if dl.status_code != 200:
            raise RuntimeError(f"PKG download failed: HTTP {dl.status_code}")
        if hashlib.sha256(dl.content).hexdigest() != report["pkg_sha256"]:
            raise RuntimeError("Downloaded PKG SHA-256 differs from the independently inspected build")

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(dl.content)

        pkgtool = find_liborbispkg_tool()
        if pkgtool is None:
            raise RuntimeError("Cannot perform the independent post-download extraction")
        with tempfile.TemporaryDirectory(prefix="czar-audio-audit-") as scratch:
            ex = Path(scratch)
            extraction = subprocess.run(
                [str(pkgtool), "pkg_extract", "--passcode", "0" * 32,
                 str(args.output), str(ex)],
                capture_output=True, text=True, timeout=300, check=False,
            )
            if extraction.returncode != 0:
                raise RuntimeError(f"Independent extraction failed: {extraction.stderr[-500:]}")
            root = ex / "uroot" if (ex / "uroot").is_dir() else ex / "Image0"
            output_peak_dbfs = _peak_dbfs(root / "sound" / "bgm_home.at9")
            if output_peak_dbfs < -60:
                raise RuntimeError(
                    f"PKG music decodes as effectively silent ({output_peak_dbfs:.1f} dBFS)"
                )

        audit = {
            "source_video": str(args.video.resolve()),
            "source_video_sha256": _hash_file(args.video),
            "source_music": str(args.music.resolve()),
            "source_music_sha256": _hash_file(args.music),
            "source_music_peak_dbfs": peak_dbfs,
            "extracted_at9_peak_dbfs": output_peak_dbfs,
            "source_icon": str(args.icon.resolve()),
            "source_icon_sha256": _hash_file(args.icon),
            "icons_source": str(args.icons_source.resolve()) if args.icons_source else None,
            "frames": args.frames,
            "size": args.size,
            "project_id": project_id,
            "output_pkg": str(args.output.resolve()),
            "pkg_report": report,
            "bundle_report": data.get("bundle_report", {}),
            "physical_test": "NOT PERFORMED — PS4 Slim hardware required",
        }
        report_path = args.output.with_suffix(".audit.json")
        report_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"pkg": str(args.output), "audit": str(report_path),
                          "sha256": report["pkg_sha256"],
                          "verified_files": len(report["files"]),
                          "verification_level": report["verification_level"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
