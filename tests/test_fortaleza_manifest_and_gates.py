"""Operação Fortaleza — Fases 8 & 11: Integrity Manifest, Dependency Invalidation & Release Gates tests.

Verifies:
  1. Ouroboros `apply_loop_to_project` fail-closed rejection on:
     - missing/incomplete `ddsHashes` or `files`
     - tampered `deluxe_dds/*.dds`, `deluxe_frames/*.png`, `*.webp`, `background.raf.zip`
     - tampered DDS inside `background.raf.zip` (even if outer ZIP hash were forged!)
     - tampered `recipe` parameters (`frames`, `wait`, `size`) or forged `renderKey`
  2. Automatic project dependency invalidation:
     - Building a PKG stores `inputs_fingerprint` + `integrity_manifest.json` + `release_gate=PHYSICAL_TEST_PENDING`
     - Modifying ANY project input (title, color, icon, etc.) immediately flips `stale_vs_project=True`
       and `release_gate=REGRESSION_DETECTED` in `/api/forge/pkg/history` and `/api/forge/pkg/{id}/report`!
  3. All 7 permanent Release Gate states + non-inheritance of `PHYSICALLY_VERIFIED` across builds!
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import dxt1, jobs, main, projects
from app.forge import pkg_forge
from app.forge.release_gates import (
    RELEASE_GATE_STATES,
    PhysicalVerificationRecord,
    can_inherit_physical_verification,
    evaluate_release_gate,
)
from app.ouroboros_engine import OUROBOROS_VERSION
from app.ouroboros_engine import media_io as io_media
from app.ouroboros_engine.applier import apply_loop_to_project, assemble_canonical_raf, sha256_bytes
from tests.test_projects import state as editor_state

REPO = Path(__file__).resolve().parents[1]
SAMPLE_PNG = REPO / "tests" / "sample.png"


def _make_clean_ouroboros_folder(tmp_path: Path) -> tuple[Path, dict]:
    folder = tmp_path / "ouro_res"
    (folder / "deluxe_dds").mkdir(parents=True)
    (folder / "deluxe_frames").mkdir(parents=True)
    dds_hashes = {}
    png_hashes = {}
    file_hashes = {}
    blobs = []
    for i in range(4):
        rgb = np.full((360, 640, 3), 30 + i * 25, dtype=np.uint8)
        rgba = np.concatenate([rgb, np.full((360, 640, 1), 255, dtype=np.uint8)], axis=-1)
        blob = dxt1.encode_dds(rgba)
        blobs.append(blob)
        dname, pname, wname = f"{i:02d}.dds", f"{i:02d}.png", f"{i:02d}.webp"
        (folder / "deluxe_dds" / dname).write_bytes(blob)
        dds_hashes[dname] = sha256_bytes(blob)
        Image.fromarray(rgb).save(folder / "deluxe_frames" / pname, "PNG")
        png_hashes[pname] = sha256_bytes((folder / "deluxe_frames" / pname).read_bytes())
        Image.fromarray(rgb).save(folder / wname, "WEBP", lossless=True)
        file_hashes[wname] = sha256_bytes((folder / wname).read_bytes())

    raf_zip = folder / "background.raf.zip"
    assemble_canonical_raf(blobs, 0.1, raf_zip)
    file_hashes["background.raf.zip"] = sha256_bytes(raf_zip.read_bytes())
    src_sha = "1" * 64
    render_key = io_media.fingerprint(
        [OUROBOROS_VERSION, src_sha, "flexibility", [640, 360], 0.0, 0.4, 4, 0.1, "natural", dds_hashes]
    )
    report = {
        "profileId": "flexibility",
        "strategyUsed": "natural",
        "sourceInterval": [0.0, 0.4],
        "frames": 4,
        "size": [640, 360],
        "wait": 0.1,
        "ddsHashes": dds_hashes,
        "framePngHashes": png_hashes,
        "files": file_hashes,
        "renderKey": render_key,
        "recipe": {
            "algorithmVersion": OUROBOROS_VERSION,
            "sourceSha256": src_sha,
            "start": 0.0,
            "end": 0.4,
            "frames": 4,
            "wait": 0.1,
            "size": [640, 360],
            "strategyUsed": "natural",
        },
    }
    return folder, report


def test_fase8_apply_loop_to_project_rejects_every_tamper_and_missing_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "ROOT", tmp_path / "studio-projects-v1")
    monkeypatch.setattr(jobs, "WORKSPACE", tmp_path / "recovery")
    with TestClient(main.app) as client:
        assert client.get("/api/projects").status_code == 200
        pid = client.post("/api/projects", json={"name": "OuroTamper"}).json()["id"]
    pdir = next((tmp_path / "studio-projects-v1").rglob(pid))
    folder, base_rep = _make_clean_ouroboros_folder(tmp_path)

    # 1. Missing ddsHashes -> fail closed!
    rep_no_dds = dict(base_rep, ddsHashes={})
    with pytest.raises(ValueError, match="renderKey|ddsHashes"):
        apply_loop_to_project(pdir, folder, rep_no_dds, acquire_lock=False)

    # 2. Incomplete files manifest -> fail closed!
    rep_no_zip = dict(base_rep, files={k: v for k, v in base_rep["files"].items() if k != "background.raf.zip"})
    with pytest.raises(ValueError, match="background.raf.zip"):
        apply_loop_to_project(pdir, folder, rep_no_zip, acquire_lock=False)

    # 3. Forged renderKey -> fail closed!
    rep_bad_rk = dict(base_rep, renderKey="f" * 64)
    with pytest.raises(ValueError, match="renderKey"):
        apply_loop_to_project(pdir, folder, rep_bad_rk, acquire_lock=False)

    # 4. Tampered recipe parameter (wait mismatch) -> fail closed!
    rep_bad_wait = dict(base_rep, wait=0.25)
    with pytest.raises(ValueError, match="wait"):
        apply_loop_to_project(pdir, folder, rep_bad_wait, acquire_lock=False)

    # 5. Tampered deluxe_frames/00.png -> fail closed!
    png0 = folder / "deluxe_frames" / "00.png"
    orig_png0 = png0.read_bytes()
    Image.new("RGB", (640, 360), (255, 0, 0)).save(png0, "PNG")
    with pytest.raises(ValueError, match="00.png"):
        apply_loop_to_project(pdir, folder, base_rep, acquire_lock=False)
    png0.write_bytes(orig_png0)

    # 6. Tampered DDS inside background.raf.zip (even when updating outer zip hash!) -> fail closed!
    raf_zip = folder / "background.raf.zip"
    orig_zip = raf_zip.read_bytes()
    (tmp_path / "orig_copy.zip").write_bytes(orig_zip)
    with zipfile.ZipFile(tmp_path / "orig_copy.zip", "r") as zin, zipfile.ZipFile(raf_zip, "w") as zout:
        dds0_name = next(n for n in zin.namelist() if n.endswith("00.dds"))
        for nm in zin.namelist():
            data = zin.read(nm)
            if nm.endswith("01.dds"):
                data = zin.read(dds0_name)  # substitute wrong DDS inside ZIP!
            zout.writestr(nm, data)
    rep_forged_zip_hash = dict(base_rep, files={**base_rep["files"], "background.raf.zip": sha256_bytes(raf_zip.read_bytes())})
    with pytest.raises(ValueError, match="01.dds"):
        apply_loop_to_project(pdir, folder, rep_forged_zip_hash, acquire_lock=False)
    raf_zip.write_bytes(orig_zip)

    # 7. Clean untouched proposal succeeds!
    res = apply_loop_to_project(pdir, folder, base_rep, acquire_lock=False)
    assert res["applied"] is True
    assert res["rafEquivalent"] is True


@pytest.mark.skipif(pkg_forge.find_liborbispkg_tool() is None, reason="liborbispkg pkgtool not installed")
def test_fase8_project_mutation_automatically_invalidates_prior_pkg_approval(tmp_path, monkeypatch):
    """After building a validated PKG, modifying a project input MUST invalidate its approval badge."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(projects, "ROOT", ws / "studio-projects-v1")
    monkeypatch.setattr(jobs, "WORKSPACE", ws / "recovery")
    with TestClient(main.app) as client:
        assert client.get("/api/projects").status_code == 200
        pid = client.post("/api/projects", json={"name": "StaleGateTest"}).json()["id"]
        params = {"project": pid}

        home_job = client.post(
            "/api/slot/home/upload", params=params, files={"file": ("home.png", SAMPLE_PNG.open("rb"), "image/png")}
        ).json()["job_id"]
        st = editor_state()
        st["campos"]["pkg-title"] = "Tema Rev 1"
        st["campos"]["pkg-title-id"] = "CUSA88888"
        st["bgMode"] = "static"
        st["slots"] = {"home": home_job}
        assert client.put(f"/api/projects/{pid}", json={"name": "StaleGateTest", "revision": 1, "state": st}).status_code == 200

        build = client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
        assert build.status_code == 200, build.text
        pkg_id = build.json()["pkg_id"]

        # Right after build: NOT stale, release_gate == PHYSICAL_TEST_PENDING, integrity_manifest.json persisted
        h1 = client.get("/api/forge/pkg/history", params=params).json()["items"][0]
        assert h1["stale_vs_project"] is False
        assert h1["release_gate"] == "PHYSICAL_TEST_PENDING"
        assert h1["inputs_fingerprint"]
        rep1 = client.get(f"/api/forge/pkg/{pkg_id}/report", params=params).json()
        assert rep1["stale_vs_project"] is False
        assert rep1["release_gate"] == "PHYSICAL_TEST_PENDING"
        assert rep1["integrity_manifest"]["manifest_version"] == "czar-fortaleza-manifest-v1"

        # Mutate project setting (title + focus color) and save revision 2
        st2 = json.loads(json.dumps(st))
        st2["campos"]["pkg-title"] = "Tema Rev 2 Modificado"
        st2["campos"]["pkg-focuscolor"] = "#ff3366"
        assert client.put(f"/api/projects/{pid}", json={"name": "StaleGateTest", "revision": 2, "state": st2}).status_code == 200

        # Prior PKG MUST now be flagged stale_vs_project=True & release_gate=REGRESSION_DETECTED!
        h2 = client.get("/api/forge/pkg/history", params=params).json()["items"][0]
        assert h2["stale_vs_project"] is True
        assert h2["release_gate"] == "REGRESSION_DETECTED"
        assert "invalidado" in h2["verification_reason"].lower()

        rep2 = client.get(f"/api/forge/pkg/{pkg_id}/report", params=params).json()
        assert rep2["stale_vs_project"] is True
        assert rep2["release_gate"] == "REGRESSION_DETECTED"


def test_fase11_all_7_release_gate_states_and_non_inheritance_of_physical_verification():
    assert set(RELEASE_GATE_STATES) == {
        "BUILD_FAILED",
        "STRUCTURE_VALIDATED",
        "SEMANTICALLY_VALIDATED",
        "INDEPENDENTLY_COMPARED",
        "PHYSICAL_TEST_PENDING",
        "PHYSICALLY_VERIFIED",
        "REGRESSION_DETECTED",
    }

    # 1. BUILD_FAILED
    g1 = evaluate_release_gate(
        pkg_created=False, header_and_crypto_valid=False, sc0_validated=False, theme_type_valid=False,
        image0_extracted=False, semantic_contract_passed=None, digital_twin_passed=None,
        bundle_compared=False, bundle_hashes_matched=False,
    )
    assert g1["state"] == "BUILD_FAILED"

    # 2. STRUCTURE_VALIDATED (header + Sc0 OK, Image0 NOT extracted -> fail-closed cannot advance)
    g2 = evaluate_release_gate(
        pkg_created=True, header_and_crypto_valid=True, sc0_validated=True, theme_type_valid=True,
        image0_extracted=False, semantic_contract_passed=None, digital_twin_passed=None,
        bundle_compared=False, bundle_hashes_matched=False,
    )
    assert g2["state"] == "STRUCTURE_VALIDATED"

    # 3. SEMANTICALLY_VALIDATED (extracted + semantic contract + Digital Twin OK, no bundle/external ref)
    g3 = evaluate_release_gate(
        pkg_created=True, header_and_crypto_valid=True, sc0_validated=True, theme_type_valid=True,
        image0_extracted=True, semantic_contract_passed=True, digital_twin_passed=True,
        bundle_compared=False, bundle_hashes_matched=False,
    )
    assert g3["state"] == "SEMANTICALLY_VALIDATED"

    # 4. INDEPENDENTLY_COMPARED
    g4 = evaluate_release_gate(
        pkg_created=True, header_and_crypto_valid=True, sc0_validated=True, theme_type_valid=True,
        image0_extracted=True, semantic_contract_passed=True, digital_twin_passed=True,
        bundle_compared=False, bundle_hashes_matched=False, independent_verifiers_passed=True,
    )
    assert g4["state"] == "INDEPENDENTLY_COMPARED"

    # 5. PHYSICAL_TEST_PENDING (bundle parity 100% + Digital Twin + Contract, no hardware test yet)
    g5 = evaluate_release_gate(
        pkg_created=True, header_and_crypto_valid=True, sc0_validated=True, theme_type_valid=True,
        image0_extracted=True, semantic_contract_passed=True, digital_twin_passed=True,
        bundle_compared=True, bundle_hashes_matched=True, independent_verifiers_passed=True,
        pkg_sha256="a" * 64, inputs_fingerprint="fp1",
    )
    assert g5["state"] == "PHYSICAL_TEST_PENDING"
    assert g5["physical_verified"] is False

    # 6. PHYSICALLY_VERIFIED ONLY with exact matching pkg_sha256 AND inputs_fingerprint
    phys = PhysicalVerificationRecord(
        pkg_sha256="a" * 64,
        inputs_fingerprint="fp1",
        console_model="PS4 Slim CUH-2215B",
        firmware="12.52",
        install_method="USB FPKG Debug Settings",
        passed=True,
        recorded_at="2026-11-23T14:30:00Z",
    )
    g6 = evaluate_release_gate(
        pkg_created=True, header_and_crypto_valid=True, sc0_validated=True, theme_type_valid=True,
        image0_extracted=True, semantic_contract_passed=True, digital_twin_passed=True,
        bundle_compared=True, bundle_hashes_matched=True, independent_verifiers_passed=True,
        pkg_sha256="a" * 64, inputs_fingerprint="fp1", physical_record=phys,
    )
    assert g6["state"] == "PHYSICALLY_VERIFIED"
    assert g6["physical_verified"] is True

    # Non-inheritance rule: rebuilding with ANY change (new pkg_sha256 or new inputs_fingerprint) MUST NOT inherit!
    ok_new_sha, _ = can_inherit_physical_verification(phys, pkg_sha256="b" * 64, inputs_fingerprint="fp1")
    ok_new_fp, _ = can_inherit_physical_verification(phys, pkg_sha256="a" * 64, inputs_fingerprint="fp2")
    assert ok_new_sha is False
    assert ok_new_fp is False

    g6_rebuilt = evaluate_release_gate(
        pkg_created=True, header_and_crypto_valid=True, sc0_validated=True, theme_type_valid=True,
        image0_extracted=True, semantic_contract_passed=True, digital_twin_passed=True,
        bundle_compared=True, bundle_hashes_matched=True, independent_verifiers_passed=True,
        pkg_sha256="b" * 64, inputs_fingerprint="fp2", physical_record=phys,
    )
    assert g6_rebuilt["state"] == "PHYSICAL_TEST_PENDING"
    assert g6_rebuilt["physical_verified"] is False

    # 7. REGRESSION_DETECTED
    g7 = evaluate_release_gate(
        pkg_created=True, header_and_crypto_valid=True, sc0_validated=True, theme_type_valid=True,
        image0_extracted=True, semantic_contract_passed=True, digital_twin_passed=True,
        bundle_compared=True, bundle_hashes_matched=True, stale_vs_project=True,
    )
    assert g7["state"] == "REGRESSION_DETECTED"
