"""Permanent regression tests for Stage 4/5 Zenitsu Gold Master bench audit fixes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app import jobs, main, projects
from app.forge.pkg_router import _normalize_persisted_pkg_report


def test_legacy_pkg_history_without_verification_level_never_claims_validated():
    """Legacy report missing verification_level (even if structure_validated=True) must NOT report validated."""
    norm = _normalize_persisted_pkg_report({"structure_validated": True, "pkg_created": True})
    assert norm["verification_level"] == "failed"
    assert norm["structure_validated"] is False
    assert norm["package_status"] == "PACOTE GERADO"
    assert norm["legacy_unverified"] is True
    assert "verification_level" in norm["verification_reason"]

    # Partial header_and_sc0_only must also force structure_validated=False
    partial = _normalize_persisted_pkg_report({"verification_level": "header_and_sc0_only", "structure_validated": True})
    assert partial["structure_validated"] is False
    assert partial["legacy_unverified"] is False

    # Full verified stays validated
    full = _normalize_persisted_pkg_report({"verification_level": "full_bundle_verified", "structure_validated": True})
    assert full["structure_validated"] is True
    assert full["package_status"] == "PACOTE VALIDADO ESTRUTURALMENTE"


def test_forge_pkg_history_and_report_http_legacy_and_same_name_provenance(tmp_path, monkeypatch):
    """HTTP /api/forge/pkg/history and /report differentiate same-name builds by SHA-256/revision and reject legacy."""
    monkeypatch.setattr(jobs, "WORKSPACE", tmp_path)
    monkeypatch.setattr(projects, "ROOT", tmp_path / "studio-projects-v1")
    client = TestClient(main.app)
    assert client.get("/api/projects").status_code == 200
    pid = client.post("/api/projects", json={"name": "ForgeHistoryAudit"}).json()["id"]

    proj_dir = next(p for p in projects.ROOT.rglob(pid) if p.is_dir())
    pkg_dir = proj_dir / "media" / "pkg-forge-v1"
    id_leg = "0000000000000001"
    id_v1 = "0000000000000002"
    id_v2 = "0000000000000003"
    for idx, (pkg_id, sha, rev, report_payload) in enumerate(
        [
            (
                id_leg,
                "a" * 64,
                None,
                {"structure_validated": True, "pkg_created": True, "iro_tag_header": 2, "iro_tag_sfo": 2, "category": "ac"},
            ),
            (
                id_v1,
                "b" * 64,
                3,
                {
                    "structure_validated": True,
                    "pkg_created": True,
                    "verification_level": "full_bundle_verified",
                    "package_status": "PACOTE VALIDADO ESTRUTURALMENTE",
                    "pkg_sha256": "b" * 64,
                    "iro_tag_header": 2,
                    "iro_tag_sfo": 2,
                    "category": "ac",
                },
            ),
            (
                id_v2,
                "c" * 64,
                4,
                {
                    "structure_validated": True,
                    "pkg_created": True,
                    "verification_level": "full_bundle_verified",
                    "package_status": "PACOTE VALIDADO ESTRUTURALMENTE",
                    "pkg_sha256": "c" * 64,
                    "iro_tag_header": 2,
                    "iro_tag_sfo": 2,
                    "category": "ac",
                },
            ),
        ]
    ):
        d = pkg_dir / pkg_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "UP9000-CUSA00000_00-SAME000000000000.pkg").write_bytes(b"\x7fCNT" + bytes([idx]) * 32)
        rec = {
            "pkg_id": pkg_id,
            "pkg_name": "UP9000-CUSA00000_00-SAME000000000000.pkg",
            "pkg_bytes": 36,
            "pkg_sha256": sha,
            "project_revision": rev,
            "content_id": "UP9000-CUSA00000_00-SAME000000000000",
            "title": "SameNameTheme",
            "created_at": 1700000000.0 + idx * 10.0,
            "pkg_report": report_payload,
        }
        (d / "report.json").write_text(json.dumps(rec), encoding="utf-8")

    hist = client.get(f"/api/forge/pkg/history?project={pid}")
    assert hist.status_code == 200, hist.text
    by_id = {it["pkg_id"]: it for it in hist.json()["items"]}

    assert by_id[id_v1]["pkg_sha256"] == "b" * 64
    assert by_id[id_v1]["project_revision"] == 3
    assert by_id[id_v2]["pkg_sha256"] == "c" * 64
    assert by_id[id_v2]["project_revision"] == 4
    assert by_id[id_v1]["pkg_sha256"] != by_id[id_v2]["pkg_sha256"]

    leg = by_id[id_leg]
    assert leg["verification_level"] == "failed"
    assert leg["structure_validated"] is False
    assert leg["package_status"] == "PACOTE GERADO"
    assert leg["legacy_unverified"] is True

    rep_leg = client.get(f"/api/forge/pkg/{id_leg}/report?project={pid}")
    assert rep_leg.status_code == 200, rep_leg.text
    pr_leg = rep_leg.json()["pkg_report"]
    assert pr_leg["verification_level"] == "failed"
    assert pr_leg["structure_validated"] is False
    assert pr_leg["legacy_unverified"] is True


