"""Permanent regression tests for Stage 4/5 Zenitsu Gold Master bench audit fixes."""

from __future__ import annotations

import io
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app import icon_mapper, jobs, main, projects
from app.forge.pkg_router import _normalize_persisted_pkg_report
from app.ouroboros_engine.metrics import evaluate_loop


def _png_bytes(size: tuple[int, int] = (128, 128), rgba: tuple[int, int, int, int] = (200, 120, 40, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, rgba).save(buf, format="PNG")
    return buf.getvalue()


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


def test_icon_mapper_order_independence_variants_and_ambiguity_rejection():
    zenitsu_19 = [
        "AmigosBrancoOutrefinado.png",
        "AoVivoDaPlaystation_refinado.png",
        "BibliotecaBrancoOut_refinado.png",
        "ComunidadeBrancoOutrefinado.png",
        "ConfiguraçoesBrancoOutrefinado.png",
        "DiscoBrancoOut_refinado.png",
        "EnergiaBrancoOutrefinado.png",
        "EventoBrancoOutrefinado.png",
        "GaleriaDeCapturas_refinado.png",
        "GrupoBrancoOutrefinado.png",
        "MensagensBrancoOutrefinado.png",
        "MusicaUSB_refinado.png",
        "NavegadorBrancoOut_refinado.png",
        "NotificaçoesBrancoOutrefinado.png",
        "PastaBrancoOut_refinado.png",
        "SharePlayBrancoOut_refinado.png",
        "SobreposicaoDeDiscoBrancoOut_refinado.png",
        "TVeVideoBrancoOut_refinado.png",
        "TrofeusBrancoOutrefinado.png",
    ]
    fwd = icon_mapper.map_icon_filenames(zenitsu_19)
    rev = icon_mapper.map_icon_filenames(list(reversed(zenitsu_19)))
    assert fwd["mapped"] == rev["mapped"]
    assert len(fwd["mapped"]) == 19
    assert not fwd["conflicts"]
    assert not fwd["unmatched"]
    assert fwd["mapped"]["ct:disc"] == "DiscoBrancoOut_refinado.png"
    assert fwd["mapped"]["ct:discoverlay"] == "SobreposicaoDeDiscoBrancoOut_refinado.png"
    assert fwd["mapped"]["ct:browser"] == "NavegadorBrancoOut_refinado.png"

    # Uppercase, accents, and _glow variants
    var = icon_mapper.map_icon_filenames([
        "CONFIGURAÇÕES.PNG",
        "NOTIFICAÇÕES_GLOW.PNG",
        "TROFÉUS_glow.png",
        "SOBREPOSIÇÃO_DE_DISCO.png",
        "DISCO.png",
    ])
    assert var["mapped"]["fn:setting"] == "CONFIGURAÇÕES.PNG"
    assert var["mapped_glow"]["fn:notification"] == "NOTIFICAÇÕES_GLOW.PNG"
    assert var["mapped_glow"]["fn:trophy"] == "TROFÉUS_glow.png"
    assert var["mapped"]["ct:discoverlay"] == "SOBREPOSIÇÃO_DE_DISCO.png"
    assert var["mapped"]["ct:disc"] == "DISCO.png"

    # Ambiguous duplicates must be explicitly rejected, never silently overwritten
    amb = icon_mapper.map_icon_filenames(["DiscoBrancoOut_refinado.png", "disc_alt.png"])
    assert len(amb["conflicts"]) == 1
    assert amb["conflicts"][0]["slot"] == "ct:disc"
    assert "ct:disc" not in amb["mapped"]


def test_slots_upload_batch_endpoint_and_conflict_409(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "WORKSPACE", tmp_path)
    monkeypatch.setattr(projects, "ROOT", tmp_path / "studio-projects-v1")
    client = TestClient(main.app)
    assert client.get("/api/projects").status_code == 200
    pid = client.post("/api/projects", json={"name": "IconBatch"}).json()["id"]

    # Conflict returns HTTP 409 and writes nothing
    r_conf = client.post(
        "/api/slots/upload-batch",
        params={"project": pid},
        files=[
            ("files", ("DiscoBrancoOut_refinado.png", _png_bytes((512, 512), (255, 0, 0, 255)), "image/png")),
            ("files", ("disc_extra.png", _png_bytes((512, 512), (0, 255, 0, 255)), "image/png")),
        ],
    )
    assert r_conf.status_code == 409

    # Valid distinct files map deterministically into project workspace
    r_ok = client.post(
        "/api/slots/upload-batch",
        params={"project": pid},
        files=[
            ("files", ("NavegadorBrancoOut_refinado.png", _png_bytes((512, 512), (0, 120, 255, 255)), "image/png")),
            ("files", ("DiscoBrancoOut_refinado.png", _png_bytes((512, 512), (255, 180, 0, 255)), "image/png")),
            ("files", ("SobreposicaoDeDiscoBrancoOut_refinado.png", _png_bytes((512, 512), (200, 0, 200, 255)), "image/png")),
        ],
    )
    assert r_ok.status_code == 200, r_ok.text
    slots_uploaded = {item["slot"]: item["source"]["name"] for item in r_ok.json()["uploaded"]}
    assert slots_uploaded == {
        "ct:browser": "NavegadorBrancoOut_refinado.png",
        "ct:disc": "DiscoBrancoOut_refinado.png",
        "ct:discoverlay": "SobreposicaoDeDiscoBrancoOut_refinado.png",
    }


def test_kinematics_distinguishes_closed_orbit_from_pause_jump_and_reversal():
    def _render(angles: list[float]) -> np.ndarray:
        w, h = 320, 180
        frames = []
        for ang in angles:
            im = Image.new("RGB", (w, h), (12, 16, 28))
            dr = ImageDraw.Draw(im)
            for gx in range(0, w, 40):
                dr.line((gx, 0, gx, h), fill=(26, 32, 48), width=1)
            for gy in range(0, h, 40):
                dr.line((0, gy, w, gy), fill=(26, 32, 48), width=1)
            cx = int(round(w * 0.5 + 52.0 * math.cos(ang)))
            cy = int(round(h * 0.5 + 42.0 * math.sin(ang)))
            dr.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), fill=(245, 210, 60), outline=(255, 255, 255), width=3)
            dr.rectangle((cx - 6, cy - 12, cx + 6, cy + 12), fill=(40, 80, 220))
            frames.append(np.asarray(im, dtype=np.uint8))
        return np.stack(frames, axis=0)

    n = 24
    ang_ok = [2.0 * math.pi * i / n for i in range(n)]
    ang_pause = ang_ok[:-4] + [ang_ok[-5]] * 4
    ang_jump = [2.0 * math.pi * 0.65 * i / n for i in range(n)]
    ang_rev = [math.pi * i / (n // 2) if i <= n // 2 else math.pi * (n - i) / (n // 2) for i in range(n)]

    ev_ok = evaluate_loop(_render(ang_ok), 0.05, evaluate_dds=False)
    ev_pause = evaluate_loop(_render(ang_pause), 0.05, evaluate_dds=False)
    ev_jump = evaluate_loop(_render(ang_jump), 0.05, evaluate_dds=False)
    ev_rev = evaluate_loop(_render(ang_rev), 0.05, evaluate_dds=False)

    s_ok = ev_ok["metrics"]["trajectoryKinematics"]["definitiveLoopScore"]
    s_pause = ev_pause["metrics"]["trajectoryKinematics"]["definitiveLoopScore"]
    s_jump = ev_jump["metrics"]["trajectoryKinematics"]["definitiveLoopScore"]
    s_rev = ev_rev["metrics"]["trajectoryKinematics"]["definitiveLoopScore"]

    assert not ev_ok["issues"]
    assert s_ok >= 7.0
    assert ev_pause["issues"] and s_pause < s_ok - 1.5
    assert ev_jump["issues"] and s_jump < s_ok - 1.5
    assert ev_rev["issues"] and s_rev <= 5.2 and s_rev < s_ok - 1.5


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


