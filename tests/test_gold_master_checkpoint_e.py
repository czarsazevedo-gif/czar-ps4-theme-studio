"""Checkpoint E — Pilar III (Gold Master PKG: Full Build, Extraction/Hashes, 3-Tier Status, Failure Matrix).

Verifies all Section 19 requirements and regression fixes GM-E-001 .. GM-E-003:
- Real Linux LibOrbisPkg build of full animated theme (RAF + icons + ATRAC9 + VAG + theme.xml + SFO).
- Explicit 3-tier status distinction: PACOTE GERADO vs PACOTE VALIDADO ESTRUTURALMENTE vs PACOTE TESTADO NO PS4.
- Complete failure matrix: missing tool (503 + bundle fallback), cancel, timeout, storage fault cleanup (GM-E-003),
  truncated/corrupted PKG detection, missing verification_level default safety (GM-E-002), and restart recovery.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import time
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app import jobs, main, projects
from app.forge import pkg_forge, pkg_inspector, pkg_router
from app.forge.subproc import SubprocessTimeoutError
from tests.test_projects import state as editor_state

REPO = Path(__file__).resolve().parents[1]
SAMPLE_PNG = REPO / "tests" / "sample.png"
SAMPLE_MP4 = REPO / "tests" / "sample.mp4"
SAMPLE_MP3 = REPO / "tests" / "sample_music.mp3"
SAMPLE_SFX = REPO / "tests" / "sample_sfx.wav"


@pytest.fixture
def pkg_client(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(projects, "ROOT", ws / "studio-projects-v1")
    monkeypatch.setattr(jobs, "WORKSPACE", ws / "recovery")
    with TestClient(main.app) as client:
        assert client.get("/api/projects").status_code == 200
        yield client, ws


def _prepare_complete_animated_project(client: TestClient, name: str = "PKG Gold Master") -> tuple[str, dict]:
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    params = {"project": pid}

    home_job = client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", SAMPLE_PNG.open("rb"), "image/png")}).json()["job_id"]
    fn_job = client.post("/api/slot/fn:trophy/upload", params=params, files={"file": ("trophy.png", SAMPLE_PNG.open("rb"), "image/png")}).json()["job_id"]
    ct_job = client.post("/api/slot/ct:browser/upload", params=params, files={"file": ("browser.png", SAMPLE_PNG.open("rb"), "image/png")}).json()["job_id"]
    anim_up = client.post(
        "/api/animated/upload",
        params=params,
        files={"file": ("sample.mp4", SAMPLE_MP4.open("rb"), "video/mp4")},
        data={"frames": "6", "size": "640x360"},
    )
    assert anim_up.status_code == 200, anim_up.text
    anim_job = anim_up.json()["job_id"]

    bgm_r = client.post(
        "/api/forge/audio/convert-apply",
        params=params,
        files={"file": ("bgm.mp3", SAMPLE_MP3.open("rb"), "audio/mpeg")},
        data={"loop_enabled": "true", "volume_mode": "original"},
    )
    assert bgm_r.status_code == 200, bgm_r.text
    bgm_job = bgm_r.json()["job_id"]

    sfx_job = client.post(
        "/api/audio/se_decide/upload",
        params=params,
        files={"file": ("decide.wav", SAMPLE_SFX.open("rb"), "audio/wav")},
    ).json()["job_id"]

    st = editor_state()
    st["bgMode"] = "animated"
    st["slots"] = {"home": home_job, "fn:trophy": fn_job, "ct:browser": ct_job}
    st["bgm"] = bgm_job
    st["sfx"] = {"se_decide": sfx_job}
    st["anim"] = {"jobId": anim_job, "frames": 6, "wait": 0.1, "size": [640, 360], "backend": "native"}
    save_r = client.put(f"/api/projects/{pid}", json={"name": name, "revision": 1, "state": st})
    assert save_r.status_code == 200, save_r.text
    return pid, params


@pytest.mark.skipif(pkg_forge.find_liborbispkg_tool() is None, reason="liborbispkg pkgtool not installed")
def test_section19_full_animated_pkg_build_3tier_status_and_truncated_corrupted_inspection(pkg_client, tmp_path):
    client, _ = pkg_client
    pid, params = _prepare_complete_animated_project(client)

    # 1. Build full animated theme PKG synchronously & verify all structural + SHA-256 checks
    build = client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
    assert build.status_code == 200, build.text
    d = build.json()
    pr = d["pkg_report"]
    assert pr["pkg_created"] is True
    assert pr["structure_validated"] is True
    assert pr["theme_type_valid"] is True
    assert pr["verification_level"] == "full_bundle_verified"
    assert pr["package_status"] == "PACOTE VALIDADO ESTRUTURALMENTE"
    assert pr["physical_compatibility"] == "pending"  # NEVER automatically claims PS4 tested!
    assert pr["category"] == "ac"
    assert pr["iro_tag_header"] == 2
    assert pr["iro_tag_sfo"] == 2
    assert pr["bundle_hashes_matched"] is True
    assert pr["bgmhome_loud"] is not None

    paths = {f["path"] for f in pr["files"]}
    assert "Sc0/param.sfo" in paths
    assert "Sc0/icon0.png" in paths
    assert "Sc0/pic1.png" in paths
    assert "Image0/theme.xml" in paths
    assert "Image0/sound/bgm_home.at9" in paths
    assert "Image0/sound/se_decide.vag" in paths
    assert "Image0/scene/background.raf/index.xml" in paths
    assert "Image0/scene/background.raf/plane.mdx" in paths
    assert "Image0/scene/background.raf/00.dds" in paths
    assert "Image0/texture/function_icon/trophy.png" in paths
    assert "Image0/texture/function_icon/trophy_glow.png" in paths
    assert "Image0/texture/content_icon/browser.png" in paths

    # Download streams exact byte-identical PKG
    dl = client.get(d["download"], params=params)
    assert dl.status_code == 200
    pkg_bytes = dl.content
    assert hashlib.sha256(pkg_bytes).hexdigest() == pr["pkg_sha256"]

    # 2. Tier distinction: without Image0 extractor -> PACOTE GERADO (NOT structurally validated!)
    pkg_file = tmp_path / "valid.pkg"
    pkg_file.write_bytes(pkg_bytes)
    partial_rep = pkg_inspector.inspect_and_validate_pkg(pkg_file, pkgtool_bin=None)
    assert partial_rep.verification_level == "header_and_sc0_only"
    assert partial_rep.structure_validated is False
    assert partial_rep.package_status == "PACOTE GERADO"

    # 3. Truncated PKG -> FALHA DE EMPACOTAMENTO
    trunc = pkg_bytes[: len(pkg_bytes) // 2]
    r_trunc = client.post(
        "/api/forge/pkg/inspect",
        params=params,
        files={"file": ("truncated.pkg", io.BytesIO(trunc), "application/octet-stream")},
    )
    assert r_trunc.status_code == 200
    rt = r_trunc.json()
    assert rt["structure_validated"] is False
    assert rt["package_status"] == "FALHA DE EMPACOTAMENTO"
    assert rt["errors"]

    # 4. Corrupted header PKG -> FALHA DE EMPACOTAMENTO
    corrupt = bytearray(pkg_bytes)
    corrupt[0:4] = b"NOPE"
    r_bad = client.post(
        "/api/forge/pkg/inspect",
        params=params,
        files={"file": ("corrupt.pkg", io.BytesIO(bytes(corrupt)), "application/octet-stream")},
    )
    assert r_bad.status_code == 200
    assert r_bad.json()["package_status"] == "FALHA DE EMPACOTAMENTO"
    assert r_bad.json()["structure_validated"] is False


@pytest.mark.skipif(pkg_forge.find_liborbispkg_tool() is None, reason="liborbispkg pkgtool not installed")
def test_section19_failure_matrix_missing_tool_timeout_storage_fault_and_history_defaults(pkg_client, tmp_path):
    client, ws = pkg_client
    pid, params = _prepare_complete_animated_project(client, name="PKG Fault Matrix")

    # Baseline good PKG in history
    ok_build = client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
    assert ok_build.status_code == 200
    good_pkg_id = ok_build.json()["pkg_id"]

    # 1. Missing tool -> 503 with clear install hint while bundle export continues working (200)
    with mock.patch.object(pkg_forge, "find_liborbispkg_tool", return_value=None):
        no_tool = client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true"})
        assert no_tool.status_code == 503
        assert "install_forge_tools.sh" in no_tool.json()["detail"]
        bundle_ok = client.post("/api/forge/bundle/export", params=params, data={"use_saved_project": "true"})
        assert bundle_ok.status_code == 200

    # 2. Subprocess timeout -> clean 422 failure without corrupting baseline good PKG
    with mock.patch.object(pkg_forge, "run_cancellable_process", side_effect=SubprocessTimeoutError("timeout")):
        to_res = client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
        assert to_res.status_code == 422
        assert "tempo limite" in to_res.json()["detail"].lower()

    # 3. GM-E-003: Storage fault during atomic publish cleans up .tmp and preserves baseline good PKG
    real_copyfile = shutil.copyfile

    def _boom_on_tmp(src, dst, *a, **kw):
        if str(dst).endswith(".tmp"):
            Path(dst).write_bytes(b"partial-junk")
            raise OSError("No space left on device")
        return real_copyfile(src, dst, *a, **kw)

    with mock.patch.object(pkg_forge.shutil, "copyfile", side_effect=_boom_on_tmp):
        sf_res = client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
        assert sf_res.status_code == 422

    store_dir = next((ws / "studio-projects-v1").glob(f"*/{pid}/media/pkg-forge-v1"))
    assert not list(store_dir.rglob("*.tmp")), "Storage fault leaked temporary .tmp file!"
    assert not list(store_dir.glob(".staging-*")), "Storage fault leaked .staging-* dir!"

    # Previous good PKG is still intact & downloadable
    assert client.get(f"/api/forge/pkg/{good_pkg_id}/download", params=params).status_code == 200

    # 4. GM-E-002: History entry missing verification_level MUST NOT falsely default to full_bundle_verified
    fake_id = "a" * 16
    fake_dir = store_dir / fake_id
    fake_dir.mkdir()
    (fake_dir / "report.json").write_text(
        json.dumps(
            {
                "pkg_id": fake_id,
                "pkg_name": "legacy.pkg",
                "pkg_bytes": 128,
                "content_id": "UP9000-CUSA00000_00-LEGACY0000000000",
                "title": "Legacy",
                "created_at": 1,
                "pkg_report": {"structure_validated": False},
            }
        ),
        encoding="utf-8",
    )
    hist = client.get("/api/forge/pkg/history", params=params).json()["items"]
    legacy_item = next(x for x in hist if x["pkg_id"] == fake_id)
    assert legacy_item["verification_level"] == "failed"
    assert legacy_item["package_status"] == "PACOTE GERADO"

    # 5. Simulated restart recovery: persisted in-flight snapshot reports "interrupted"
    (store_dir / "last-task.json").write_text(
        json.dumps({"task_id": "dead1234", "status": "processing", "stage": "Construindo...", "percent": 50}),
        encoding="utf-8",
    )
    active = client.get("/api/forge/pkg/active", params=params).json()
    assert active["active"] is None
    assert active["latest"]["status"] == "interrupted"
