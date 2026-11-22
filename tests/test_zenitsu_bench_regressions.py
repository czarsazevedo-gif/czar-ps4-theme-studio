"""Permanent regression tests for Stage 4/5 Zenitsu Gold Master bench audit fixes.

Covers:
1. Legacy history without `verification_level` never claims `structure_validated=True`
   or `PACOTE VALIDADO ESTRUTURALMENTE` (API history + report).
2. Same-name PKG builds carry distinct `pkg_sha256` and `project_revision` in history & report.
3. Deterministic order-independent PT-BR/EN/ES icon filename mapper (`app/icon_mapper.py`)
   + `/api/slots/map-names` & `/api/slots/upload-batch` explicit 409 ambiguity rejection.
4. Kinematic loop evaluation distinguishes closed circular orbit vs pause, jump, and ping-pong reversal.
5. Optimized DXT1a encoder preserves opaque alpha (`min_alpha=255`), punchthrough alpha, and PSNR.
"""

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

from app import dxt1, icon_mapper, jobs, main, projects
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


def _reference_compress_dxt1a(rgba: np.ndarray) -> bytes:
    """Unoptimized canonical reference DXT1a implementation (pre-optimization HEAD baseline)."""
    height, width = rgba.shape[:2]
    pad_h = (-height) % 4
    pad_w = (-width) % 4
    if pad_h or pad_w:
        rgba = np.pad(rgba, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
        height, width = rgba.shape[:2]
    bh, bw = height // 4, width // 4
    blocks = rgba.reshape(bh, 4, bw, 4, 4).transpose(0, 2, 1, 3, 4).reshape(-1, 16, 4)
    n = blocks.shape[0]
    colors = blocks[..., :3].astype(np.float32)
    opaque = blocks[..., 3] >= dxt1.ALPHA_THRESHOLD
    weights = opaque.astype(np.float32)
    opaque_count = weights.sum(axis=1)
    needs_alpha = ~opaque.all(axis=1)
    all_clear = opaque_count == 0
    safe_count = np.maximum(opaque_count, 1.0)[:, None]
    mean = (colors * weights[..., None]).sum(axis=1) / safe_count
    centered = colors - mean[:, None, :]

    w = centered * weights[..., None]
    cov = np.einsum("nij,nik->njk", w, w, optimize=True)
    axis = w.max(axis=1) - w.min(axis=1)
    norm = np.linalg.norm(axis, axis=1, keepdims=True)
    axis = np.where(norm > 1e-6, axis / np.maximum(norm, 1e-9), np.array([1.0, 0.0, 0.0], np.float32))
    for _ in range(dxt1._POWER_ITERATIONS):
        axis = np.einsum("nij,nj->ni", cov, axis, optimize=True)
        norm = np.linalg.norm(axis, axis=1, keepdims=True)
        axis = np.where(norm > 1e-9, axis / np.maximum(norm, 1e-12), np.array([1.0, 0.0, 0.0], np.float32))
    axis = axis.astype(np.float32)

    proj = np.einsum("nij,nj->ni", centered, axis, optimize=True)
    big = np.float32(np.inf)
    t_min = np.where(opaque, proj, big).min(axis=1)
    t_max = np.where(opaque, proj, -big).max(axis=1)
    t_min = np.where(all_clear, 0.0, t_min)
    t_max = np.where(all_clear, 0.0, t_max)

    end_a = mean + t_max[:, None] * axis
    end_b = mean + t_min[:, None] * axis
    packed_a, color_a = dxt1._quantize_565(end_a)
    packed_b, color_b = dxt1._quantize_565(end_b)

    want_greater = ~needs_alpha
    is_greater = packed_a > packed_b
    swap = np.where(want_greater, ~is_greater, is_greater)
    packed0 = np.where(swap, packed_b, packed_a)
    packed1 = np.where(swap, packed_a, packed_b)
    color0 = np.where(swap[:, None], color_b, color_a)
    color1 = np.where(swap[:, None], color_a, color_b)
    four_color = packed0 > packed1

    palette = np.empty((n, 4, 3), dtype=np.float32)
    palette[:, 0] = color0
    palette[:, 1] = color1
    palette[:, 2] = np.where(four_color[:, None], (2.0 * color0 + color1) / 3.0, (color0 + color1) / 2.0)
    palette[:, 3] = np.where(four_color[:, None], (color0 + 2.0 * color1) / 3.0, 0.0)

    diff = colors[:, :, None, :] - palette[:, None, :, :]
    dist = np.einsum("nikc,nikc->nik", diff, diff, optimize=True)
    dist[:, :, 3] = np.where(four_color[:, None], dist[:, :, 3], np.inf)
    indices = np.argmin(dist, axis=2).astype(np.uint32)
    indices = np.where(opaque, indices, np.uint32(3))
    if all_clear.any():
        indices[all_clear] = 3

    shifts = (2 * np.arange(16, dtype=np.uint32))[None, :]
    bits = np.bitwise_or.reduce(indices << shifts, axis=1).astype(np.uint32)
    out = np.empty((n, 8), dtype=np.uint8)
    out[:, 0] = packed0 & 0xFF
    out[:, 1] = (packed0 >> 8) & 0xFF
    out[:, 2] = packed1 & 0xFF
    out[:, 3] = (packed1 >> 8) & 0xFF
    out[:, 4] = bits & 0xFF
    out[:, 5] = (bits >> 8) & 0xFF
    out[:, 6] = (bits >> 16) & 0xFF
    out[:, 7] = (bits >> 24) & 0xFF
    return out.tobytes()


def test_dxt1_optimized_encoder_byte_for_byte_parity_resolutions_alpha_and_layers():
    """Optimized DXT1a encoder must match canonical reference byte-for-byte across 720p/540p/360p, alpha, odd dims, and compositor layers."""
    import hashlib
    from app import compositor

    rng = np.random.default_rng(20261122)

    def _frame(w: int, h: int, alpha_mode: str) -> np.ndarray:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        r = np.clip((xx / max(w - 1, 1)) * 220 + 25 * np.sin(yy * 0.05), 0, 255)
        g = np.clip((yy / max(h - 1, 1)) * 210 + 30 * np.cos(xx * 0.04), 0, 255)
        b = np.clip(128 + 90 * np.sin((xx + yy) * 0.03), 0, 255)
        noise = rng.integers(-18, 19, size=(h, w, 3), dtype=np.int16)
        rgb = np.clip(np.stack([r, g, b], axis=-1).astype(np.int16) + noise, 0, 255).astype(np.uint8)
        rgb[16:48, 16:48] = [120, 120, 120]  # degenerate flat grey
        rgb[16:48, 64:96] = [0, 0, 0]
        if alpha_mode == "opaque":
            a = np.full((h, w, 1), 255, dtype=np.uint8)
        elif alpha_mode == "all_clear":
            a = np.zeros((h, w, 1), dtype=np.uint8)
        else:
            a = np.full((h, w, 1), 255, dtype=np.uint8)
            a[48:80, 48:80] = 0
            a[80:112, 80:112] = rng.choice([0, 64, 127, 128, 200, 255], size=(32, 32, 1)).astype(np.uint8)
            a[min(h - 1, 120), min(w - 1, 120)] = 10
        return np.concatenate([rgb, a], axis=-1)

    ov_img = Image.new("RGBA", (400, 240), (0, 0, 0, 0))
    dr = ImageDraw.Draw(ov_img)
    dr.rounded_rectangle([10, 10, 390, 230], radius=24, fill=(255, 0, 220, 180), outline=(255, 255, 255, 255), width=6)
    dr.ellipse([140, 40, 260, 160], fill=(30, 220, 255, 110))
    h_ov = hashlib.sha256(b"dxt1-layer-parity").hexdigest()

    for w, h in [(1280, 720), (960, 540), (640, 360)]:
        op = _frame(w, h, "opaque")
        tr = _frame(w, h, "transparent_mix")
        layer_spec = compositor.Layer(
            id="l1", name="overlay", asset=h_ov,
            x=960.0, y=540.0, width=640.0, height=360.0, angle=14.0, opacity=0.85,
        )
        ov_canvas = compositor.layer_pixels(ov_img, layer_spec, compositor.TARGETS["animated"], (w, h))
        composed_op = next(compositor.compose_frames([op], ov_canvas))
        composed_tr = next(compositor.compose_frames([tr], ov_canvas))

        for label, sample in [
            (f"opaque_{w}x{h}", op),
            (f"transparent_{w}x{h}", tr),
            (f"layers_opaque_{w}x{h}", composed_op),
            (f"layers_trans_{w}x{h}", composed_tr),
            (f"layers_only_{w}x{h}", np.asarray(ov_canvas, dtype=np.uint8)),
        ]:
            got = dxt1.encode_dds(sample)
            ref = dxt1.dds_header(w, h, len(got) - 128) + _reference_compress_dxt1a(sample)
            assert got == ref, f"Byte-for-byte mismatch on {label}"

    # Odd non-multiple-of-4 dimensions and all-clear alpha frame
    for label, sample in [
        ("odd_129x127_alpha", _frame(129, 127, "transparent_mix")),
        ("all_clear_64x64", _frame(64, 64, "all_clear")),
    ]:
        h_s, w_s = sample.shape[:2]
        got = dxt1.encode_dds(sample)
        ref = dxt1.dds_header(w_s, h_s, len(got) - 128) + _reference_compress_dxt1a(sample)
        assert got == ref, f"Byte-for-byte mismatch on {label}"

    # Verify opaque alpha, punchthrough alpha, and smooth-gradient PSNR sanity on decoded DDS
    x = np.linspace(0, 255, 160, dtype=np.uint8)[None, :]
    y = np.linspace(0, 255, 96, dtype=np.uint8)[:, None]
    grad = np.empty((96, 160, 4), dtype=np.uint8)
    grad[..., 0] = x
    grad[..., 1] = y
    grad[..., 2] = ((x.astype(np.uint16) + y.astype(np.uint16)) // 2).astype(np.uint8)
    grad[..., 3] = 255
    dec = dxt1.decode_dds(dxt1.encode_dds(grad))
    assert int(dec[..., 3].min()) == 255
    mse = float(np.mean((grad[..., :3].astype(np.float32) - dec[..., :3].astype(np.float32)) ** 2))
    assert 10.0 * math.log10((255.0 ** 2) / max(mse, 1e-9)) > 36.0

    grad_a = grad.copy()
    grad_a[8:16, 8:16, 3] = 0
    dec_a = dxt1.decode_dds(dxt1.encode_dds(grad_a))
    assert int(dec_a[8:16, 8:16, 3].max()) == 0
    assert int(dec_a[0:8, 0:8, 3].min()) == 255

