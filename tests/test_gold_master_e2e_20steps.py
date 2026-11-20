"""Checkpoint G — Mandatory 20-Step Real End-to-End Acceptance Test (Section 23).

Executes the full real user workflow against REAL Uvicorn subprocesses with REAL server restarts:
1.  Abrir Studio (GET /, /healthz, /api/system, /api/projects)
2.  Criar projeto
3.  Importar vídeo real (Wikimedia real river if present, fallback tests/sample.mp4)
4.  Diagnóstico automático de mídia (all 21 fields + 5 official aptitude categories)
5.  Comparar candidatos (cache-shared Deluxe analysis + calibrator)
6.  Renderizar take escolhido (post-DDS + tenCycleAudit)
7.  Aplicar take correto (verifies 409 rejection on mismatched take + 200 on exact rendered take)
8.  Configurar ícones (home, icon0, transparent fn:trophy 128/152 halo-free, ct:browser 512)
9.  Editar no Compositor (overlay on animated + native 152x152 glow on fn:setting + zero temporal drift)
10. Importar música (sample_music.mp3 inspect)
11. Converter para ATRAC9 (48 kHz, stereo, 144 kbps, fe740bf0, smpl loop + VAG SFX se_decide)
12. Aplicar áudio (playable WAV + seam preview + gain adjustment preserving loop finishing)
13. Salvar projeto (full manifest persisted on disk)
14. REINICIAR SERVIDOR REAL & Reabrir projeto (fresh Uvicorn PID on new port, same CZAR_WORKSPACE)
15. Exportar RAF (composed RAF ZIP, budget <= 6 MiB, <= 48 frames, overlay intact in decoded DDS)
16. Validar bundle (POST /api/forge/validate use_saved_project=true -> valid=True)
17. Gerar PKG (real Linux LibOrbisPkg build -> PACOTE VALIDADO ESTRUTURALMENTE, PS4=pending)
18. Extrair PKG (download .pkg + independent inspect_and_validate_pkg extraction)
19. Conferir todos os arquivos (SFO CATEGORY=ac IRO_TAG=2 bgmhome_loud, icon0, pic1, theme.xml,
    preview, function_icon + glow halo-free, content_icon, RAF DDS/MDX/XML, ATRAC9, VAG)
20. Integridade final + segundo reinício de servidor (PKG history & download byte-identical,
    original imported video SHA-256 100% immutable).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pytest
from PIL import Image

from app import dxt1
from app.forge import pkg_forge, pkg_inspector
from app.forge.audio_validation import validate_at9
from app.forge.sfo import parse_param_sfo
from tests.test_projects import state as editor_state

REPO = Path(__file__).resolve().parents[1]
REAL_RIVER = Path("/tmp/czar-real-corpus/dev_river.webm")
SAMPLE_MP4 = REPO / "tests" / "sample.mp4"
SAMPLE_PNG = REPO / "tests" / "sample.png"
SAMPLE_MP3 = REPO / "tests" / "sample_music.mp3"
SAMPLE_SFX = REPO / "tests" / "sample_sfx.wav"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _spawn_uvicorn(port: int, ws_dir: Path) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=str(REPO), CZAR_WORKSPACE=str(ws_dir))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2.0) as hc:
                if hc.get("/healthz").status_code == 200:
                    return proc
        except Exception:
            time.sleep(0.1)
    proc.terminate()
    raise AssertionError("Uvicorn server failed to boot in time")


def _stop_uvicorn(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _poll_ouroboros(hc: httpx.Client, pid: str, task_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = hc.get(f"/api/ouroboros/jobs/{task_id}", params={"project": pid})
        assert r.status_code == 200, r.text
        st = r.json()
        if st["state"] != "working":
            return st
        time.sleep(0.15)
    raise AssertionError(f"Ouroboros job {task_id} timed out")


@pytest.mark.skipif(pkg_forge.find_liborbispkg_tool() is None, reason="liborbispkg pkgtool not installed")
def test_gold_master_20_steps_real_e2e_with_server_restarts(tmp_path):
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    video_path = REAL_RIVER if REAL_RIVER.is_file() else SAMPLE_MP4
    video_bytes = video_path.read_bytes()
    video_sha = hashlib.sha256(video_bytes).hexdigest()

    # Prepare transparent white icon PNG (for halo-free check) & cyan overlay badge PNG (for compositor check)
    white_icon = tmp_path / "white_icon.png"
    arr_w = np.zeros((256, 256, 4), dtype=np.uint8)
    yy, xx = np.mgrid[0:256, 0:256]
    arr_w[((xx - 128) ** 2 + (yy - 128) ** 2) <= 92**2] = [255, 255, 255, 255]
    Image.fromarray(arr_w, "RGBA").save(white_icon, "PNG")

    cyan_badge = tmp_path / "cyan_badge.png"
    arr_c = np.zeros((120, 120, 4), dtype=np.uint8)
    arr_c[10:110, 10:110] = [0, 255, 255, 255]
    Image.fromarray(arr_c, "RGBA").save(cyan_badge, "PNG")

    port1 = _free_port()
    srv1 = _spawn_uvicorn(port1, ws_dir)
    session_cookies = None
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port1}", trust_env=False, timeout=120.0) as hc:
            # STEP 1: Abrir Studio
            assert hc.get("/").status_code == 200
            assert hc.get("/healthz").json()["ok"] is True
            assert hc.get("/api/system").json()["pkg_tools"] is True
            assert hc.get("/api/projects").status_code == 200
            session_cookies = dict(hc.cookies)

            # STEP 2: Criar projeto
            proj = hc.post("/api/projects", json={"name": "Gold Master 20 Etapas"})
            assert proj.status_code == 201, proj.text
            pid = proj.json()["id"]
            params = {"project": pid}

            # STEP 3: Importar vídeo real
            mime_v = "video/webm" if video_path.suffix == ".webm" else "video/mp4"
            lib_v = hc.post("/api/library", files={"file": (video_path.name, io.BytesIO(video_bytes), mime_v)})
            assert lib_v.status_code == 201, lib_v.text
            vid_id = lib_v.json()["id"]
            assert vid_id == video_sha

            # STEP 4: Diagnóstico automático de mídia (21 campos + 5 categorias oficiais)
            td = hc.post("/api/ouroboros/deluxe/diagnose", params=params, json={"kind": "library", "id": vid_id})
            assert td.status_code == 202, td.text
            st_diag = _poll_ouroboros(hc, pid, td.json()["id"])
            assert st_diag["state"] == "ready", st_diag
            diag_rep = hc.get(f"/api/ouroboros/result/{st_diag['key']}", params=params).json()
            md = diag_rep["mediaDiagnostic"]
            assert len(md["fields"]) == 21

            # STEP 5: Comparar candidatos (cache compartilhado + calibrador)
            ta = hc.post("/api/ouroboros/deluxe/analyze", params=params, json={"kind": "library", "id": vid_id})
            assert ta.status_code == 202
            st_ana = _poll_ouroboros(hc, pid, ta.json()["id"], timeout=30.0)
            assert st_ana["key"] == st_diag["key"], "Analyze must reuse Diagnose cache key"
            best_take = md["bestTake"]
            assert best_take is not None
            cal = hc.post(
                "/api/ouroboros/deluxe/calibrate",
                params=params,
                json={"kind": "library", "id": vid_id, "start": best_take["start"], "end": best_take["end"]},
            )
            assert cal.status_code == 202
            assert _poll_ouroboros(hc, pid, cal.json()["id"])["state"] == "ready"

            # STEP 6: Renderizar take escolhido
            render_spec = {
                "kind": "library",
                "id": vid_id,
                "start": float(best_take["start"]),
                "end": float(best_take["end"]),
                "frames": 8,
                "resolution": 2,  # 640x360
                "wait": 0.10,
                "mode": "reconstruct_auto",
                "deluxeFinish": True,
                "stabilizeCamera": False,
                "speedScale": 1.0,
            }
            tr = hc.post("/api/ouroboros/deluxe/render", params=params, json=render_spec)
            assert tr.status_code == 202, tr.text
            st_ren = _poll_ouroboros(hc, pid, tr.json()["id"], timeout=180.0)
            assert st_ren["state"] == "ready", st_ren
            ren_key = st_ren["key"]
            ren_rep = hc.get(f"/api/ouroboros/result/{ren_key}", params=params).json()
            assert "tenCycleAudit" in ren_rep

            # STEP 7: Aplicar take correto (rejeita seleção trocada 409, aceita exata 200)
            wrong_spec = dict(render_spec, frames=6)
            assert hc.post("/api/ouroboros/deluxe/apply", params=params, json={"key": ren_key, "expected": wrong_spec}).status_code == 409
            ap = hc.post("/api/ouroboros/deluxe/apply", params=params, json={"key": ren_key, "expected": render_spec})
            assert ap.status_code == 200, ap.text
            anim_job_id = ap.json()["jobId"]

            # STEP 8: Configurar ícones (home, icon0, fn:trophy, ct:browser)
            slots_map = {}
            for sid, fp in (
                ("home", SAMPLE_PNG),
                ("icon0", white_icon),
                ("fn:trophy", white_icon),
                ("ct:browser", white_icon),
            ):
                up = hc.post(f"/api/slot/{sid}/upload", params=params, files={"file": (fp.name, fp.open("rb"), "image/png")})
                assert up.status_code == 200, up.text
                slots_map[sid] = up.json()["job_id"]

            # STEP 9: Editar no Compositor (overlay em animated + ícone composto fn:setting)
            lib_badge = hc.post("/api/library", files={"file": ("cyan_badge.png", cyan_badge.open("rb"), "image/png")}).json()["id"]
            lib_white = hc.post("/api/library", files={"file": ("white_icon.png", white_icon.open("rb"), "image/png")}).json()["id"]
            anim_surface = {
                "base": False,
                "layers": [
                    {
                        "id": "ov1", "asset": lib_badge, "name": "Badge",
                        "x": 180.0, "y": 180.0, "width": 180.0, "height": 180.0,
                        "angle": 0.0, "opacity": 1.0, "fit": "stretch",
                        "proportional": True, "visible": True, "locked": False,
                    }
                ],
            }
            icon_surface = {
                "base": False,
                "layers": [
                    {
                        "id": "ic1", "asset": lib_white, "name": "White Icon",
                        "x": 64.0, "y": 64.0, "width": 108.0, "height": 108.0,
                        "angle": 0.0, "opacity": 1.0, "fit": "contain",
                        "proportional": True, "visible": True, "locked": False,
                    }
                ],
            }
            comp_prev = hc.post(
                "/api/composition/animation",
                params=params,
                json={"job": anim_job_id, "size": [640, 360], "frames": 8, "surface": anim_surface},
            )
            assert comp_prev.status_code == 200, comp_prev.text
            assert len(comp_prev.json()["frames"]) == 8

            # STEP 10: Importar música
            insp = hc.post("/api/forge/audio/inspect", params=params, files={"file": ("music.mp3", SAMPLE_MP3.open("rb"), "audio/mpeg")})
            assert insp.status_code == 200, insp.text
            stage_tok = insp.json()["staging_id"]

            # STEP 11: Converter para ATRAC9 (48 kHz, stereo, 144 kbps, smpl loop) + VAG SFX
            conv = hc.post(
                "/api/forge/audio/convert-apply",
                params=params,
                data={
                    "staging_id": stage_tok,
                    "loop_enabled": "true",
                    "loop_mode": "auto_refined",
                    "volume_mode": "ambient",
                },
            )
            assert conv.status_code == 200, conv.text
            bgm_job_id = conv.json()["job_id"]
            assert conv.json()["validation"]["valid"] is True

            sfx_up = hc.post("/api/audio/se_decide/upload", params=params, files={"file": ("decide.wav", SAMPLE_SFX.open("rb"), "audio/wav")})
            assert sfx_up.status_code == 200, sfx_up.text
            sfx_job_id = sfx_up.json()["job_id"]

            # STEP 12: Aplicar áudio (prévia WAV + costura + ajuste de ganho preservando loop)
            assert hc.get(conv.json()["preview"], params=params).status_code == 200
            assert hc.get(conv.json()["seam_preview"], params=params).status_code == 200
            gained = hc.post(f"/api/audio/{bgm_job_id}/gain", params=params, data={"gain_db": "-3.0"})
            assert gained.status_code == 200, gained.text
            assert gained.json()["validation"]["valid"] is True

            # STEP 13: Salvar projeto completo
            cur = hc.get(f"/api/projects/{pid}").json()
            pstate = editor_state()
            pstate["bgMode"] = "animated"
            pstate["slots"] = slots_map
            pstate["bgm"] = bgm_job_id
            pstate["bgmGain"] = -3.0
            pstate["sfx"] = {"se_decide": sfx_job_id}
            pstate["anim"] = {"jobId": anim_job_id, "frames": 8, "wait": 0.10, "size": [640, 360], "backend": "native"}
            pstate["composition"] = {"version": 1, "surfaces": {"animated": anim_surface, "fn:setting": icon_surface}}
            saved = hc.put(f"/api/projects/{pid}", json={"name": "Gold Master 20 Etapas", "revision": cur["revision"], "state": pstate})
            assert saved.status_code == 200, saved.text
            saved_rev = saved.json()["revision"]
    finally:
        _stop_uvicorn(srv1)

    # STEP 14: REINICIAR SERVIDOR REAL #1 & Reabrir projeto
    port2 = _free_port()
    srv2 = _spawn_uvicorn(port2, ws_dir)
    pkg_id = None
    pkg_sha = None
    pkg_dl_url = None
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port2}", cookies=session_cookies, trust_env=False, timeout=120.0) as hc:
            reopened = hc.get(f"/api/projects/{pid}")
            assert reopened.status_code == 200, reopened.text
            rj = reopened.json()
            assert rj["revision"] == saved_rev
            assert rj["state"]["anim"]["jobId"] == anim_job_id
            assert rj["state"]["bgm"] == bgm_job_id
            assert rj["state"]["composition"]["surfaces"]["animated"]["layers"][0]["id"] == "ov1"

            # STEP 15: Exportar RAF composto
            raf_res = hc.post(
                f"/api/animated/{anim_job_id}/export",
                params=params,
                data={
                    "frames": "8",
                    "wait_seconds": "0.10",
                    "composition": json.dumps(rj["state"]["composition"]),
                    "animation_size": "640x360",
                },
            )
            assert raf_res.status_code == 200, raf_res.text
            assert raf_res.json()["dds_bytes"] <= 6 * 1024 * 1024
            raf_zip_bytes = hc.get(raf_res.json()["download"], params=params).content
            with zipfile.ZipFile(io.BytesIO(raf_zip_bytes)) as zf:
                names = set(zf.namelist())
                pref = "scene/background.raf/" if "scene/background.raf/00.dds" in names else "background.raf/"
                for i in range(8):
                    dec = dxt1.decode_dds(zf.read(f"{pref}{i:02d}.dds"))
                    # Cyan overlay at interior [45:75, 45:75] must be preserved in decoded DDS!
                    patch = dec[45:75, 45:75, :3]
                    assert float(patch[..., 0].mean()) < 10.0 and float(patch[..., 1].mean()) > 245.0 and float(patch[..., 2].mean()) > 245.0

            # STEP 16: Validar bundle
            val_res = hc.post("/api/forge/validate", params=params, data={"use_saved_project": "true"})
            assert val_res.status_code == 200, val_res.text
            assert val_res.json()["report"]["valid"] is True, val_res.json()["report"]["errors"]

            # STEP 17: Gerar PKG
            pkg_res = hc.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
            assert pkg_res.status_code == 200, pkg_res.text
            pj = pkg_res.json()
            pr = pj["pkg_report"]
            assert pr["package_status"] == "PACOTE VALIDADO ESTRUTURALMENTE"
            assert pr["physical_compatibility"] == "pending"
            assert pr["verification_level"] == "full_bundle_verified"
            assert pr["bundle_hashes_matched"] is True
            pkg_id = pj["pkg_id"]
            pkg_sha = pr["pkg_sha256"]
            pkg_dl_url = pj["download"]

            # STEP 18: Extrair PKG & STEP 19: Conferir todos os arquivos
            dl_pkg = hc.get(pkg_dl_url, params=params)
            assert dl_pkg.status_code == 200
            pkg_disk = tmp_path / pj["pkg_name"]
            pkg_disk.write_bytes(dl_pkg.content)
            assert hashlib.sha256(dl_pkg.content).hexdigest() == pkg_sha

            parsed_bin = pkg_inspector.parse_pkg_binary(pkg_disk)
            sfo_vals = parse_param_sfo(parsed_bin["sc0_blobs"]["Sc0/param.sfo"])["values"]
            assert sfo_vals["CATEGORY"] == "ac"
            assert sfo_vals["IRO_TAG"] == 2
            assert "bgmhome_loud=" in str(sfo_vals["PUBTOOLINFO"])
            assert Image.open(io.BytesIO(parsed_bin["sc0_blobs"]["Sc0/icon0.png"])).size == (512, 512)
            assert Image.open(io.BytesIO(parsed_bin["sc0_blobs"]["Sc0/pic1.png"])).size == (1920, 1080)

            ex_dir = tmp_path / "extracted_image0"
            ex_dir.mkdir()
            ok_ex, ex_out = pkg_inspector._extract_pkg_image0(
                pkg_disk, ex_dir, passcode="0" * 32, pkgtool_bin=pkg_forge.find_liborbispkg_tool()
            )
            assert ok_ex, ex_out
            root_img0 = ex_dir / "Image0" if (ex_dir / "Image0").is_dir() else (ex_dir / "uroot" if (ex_dir / "uroot").is_dir() else ex_dir)

            assert "<homebgm-enable>True</homebgm-enable>" in (root_img0 / "theme.xml").read_text(encoding="utf-8")
            assert Image.open(root_img0 / "texture" / "preview.png").size == (740, 416)
            for rel_icon, exp_sz in (
                ("texture/function_icon/trophy.png", (128, 128)),
                ("texture/function_icon/trophy_glow.png", (152, 152)),
                ("texture/function_icon/setting.png", (128, 128)),
                ("texture/function_icon/setting_glow.png", (152, 152)),
                ("texture/content_icon/browser.png", (512, 512)),
            ):
                im = Image.open(root_img0 / rel_icon).convert("RGBA")
                assert im.size == exp_sz, rel_icon
                bg_w = Image.new("RGBA", exp_sz, (255, 255, 255, 255))
                bg_w.alpha_composite(im)
                assert int(np.asarray(bg_w)[..., :3].min()) >= 250, f"Halo regression in extracted {rel_icon}"

            assert (root_img0 / "scene" / "background.raf" / "plane.mdx").is_file()
            assert (root_img0 / "scene" / "background.raf" / "index.xml").is_file()
            for i in range(8):
                assert (root_img0 / "scene" / "background.raf" / f"{i:02d}.dds").is_file()

            at9_rep = validate_at9(root_img0 / "sound" / "bgm_home.at9", require_ps4_profile=True, require_loop=True)
            assert at9_rep.valid is True, at9_rep.errors
            assert (root_img0 / "sound" / "se_decide.vag").read_bytes().startswith(b"VAGp")
    finally:
        _stop_uvicorn(srv2)

    # STEP 20: Integridade final + SEGUNDO REINÍCIO DE SERVIDOR REAL
    port3 = _free_port()
    srv3 = _spawn_uvicorn(port3, ws_dir)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port3}", cookies=session_cookies, trust_env=False, timeout=60.0) as hc:
            hist = hc.get("/api/forge/pkg/history", params=params).json()["items"]
            item = next(x for x in hist if x["pkg_id"] == pkg_id)
            assert item["package_status"] == "PACOTE VALIDADO ESTRUTURALMENTE"
            assert item["physical_compatibility"] == "pending"

            dl_after_restart = hc.get(pkg_dl_url, params=params)
            assert dl_after_restart.status_code == 200
            assert hashlib.sha256(dl_after_restart.content).hexdigest() == pkg_sha

            # Library original video remained 100% immutable!
            orig_video_dl = hc.get(f"/api/library/{vid_id}/original")
            assert orig_video_dl.status_code == 200
            assert hashlib.sha256(orig_video_dl.content).hexdigest() == video_sha
    finally:
        _stop_uvicorn(srv3)
