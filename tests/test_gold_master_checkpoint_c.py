"""Checkpoint C — Pilar III (Gold Master Integrity: Projects, Library/Import, Icons, Compositor).

Verifies all Section 14, 15, 16, 17 requirements and proves regression fixes GM-C-001 .. GM-C-005:
- GM-C-001: Compositor animation preview honors applied Ouroboros Deluxe loop frames (preview == export parity).
- GM-C-002: Premultiplied Lanczos scaling eliminates dark alpha-fringe halos on transparent icons (128/152/512).
- GM-C-003: Corrupted/truncated uploads to slot/animated/convert leak ZERO orphan job directories on disk.
- GM-C-004: Project duplication preserves Ouroboros Deluxe regional recipes alongside jobs and composition.
- GM-C-005: Composed function icons render native 152x152 glow directly from full-res layers (no 128->152 upscale).
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import at9, dxt1, jobs, library, main, media, projects
from tests.test_projects import state as editor_state

REPO = Path(__file__).resolve().parents[1]
SAMPLE_MP4 = REPO / "tests" / "sample.mp4"
SAMPLE_GIF = REPO / "tests" / "sample.gif"
SAMPLE_PNG = REPO / "tests" / "sample.png"
SAMPLE_WAV = REPO / "tests" / "sample_sfx.wav"
SAMPLE_MP3 = REPO / "tests" / "sample_music.mp3"


@pytest.fixture
def env_clients(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(projects, "ROOT", ws / "studio-projects-v1")
    monkeypatch.setattr(jobs, "WORKSPACE", ws / "recovery")
    with TestClient(main.app) as c1, TestClient(main.app) as c2:
        assert c1.get("/api/projects").status_code == 200
        assert c2.get("/api/projects").status_code == 200
        yield c1, c2, ws


def _white_circle_rgba_png(path: Path, size: int = 256) -> Path:
    arr = np.zeros((size, size, 4), dtype=np.uint8)
    yy, xx = np.mgrid[0:size, 0:size]
    mask = ((xx - size / 2.0) ** 2 + (yy - size / 2.0) ** 2) <= (size * 0.36) ** 2
    arr[mask] = [255, 255, 255, 255]
    Image.fromarray(arr, "RGBA").save(path, "PNG")
    return path


def _make_9_real_formats(td: Path) -> dict[str, tuple[Path, str]]:
    """Build genuine files for all 9 Section 15 formats: PNG, JPEG, GIF, MP4, WebM, WAV, MP3, FLAC, AT9."""
    # PNG & JPEG
    png_p = _white_circle_rgba_png(td / "art.png", 256)
    jpg_p = td / "photo.jpg"
    Image.new("RGB", (320, 180), (40, 110, 190)).save(jpg_p, "JPEG", quality=92)

    # WebM (from sample.mp4 via VP8/VP9)
    webm_p = td / "clip.webm"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(SAMPLE_MP4), "-t", "1.0", "-c:v", "libvpx", "-b:v", "250k", "-an", str(webm_p)],
        check=True,
    )

    # FLAC & ATRAC9 (from genuine 48 kHz stereo PCM)
    wav_48 = td / "music_48k.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(wav_48)],
        check=True,
    )
    flac_p = td / "track.flac"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(wav_48), "-c:a", "flac", str(flac_p)],
        check=True,
    )
    at9_p = td / "theme_bgm.at9"
    at9.encode(wav_48, at9_p)

    return {
        "png": (png_p, "image/png"),
        "jpg": (jpg_p, "image/jpeg"),
        "gif": (SAMPLE_GIF, "image/gif"),
        "mp4": (SAMPLE_MP4, "video/mp4"),
        "webm": (webm_p, "video/webm"),
        "wav": (SAMPLE_WAV, "audio/wav"),
        "mp3": (SAMPLE_MP3, "audio/mpeg"),
        "flac": (flac_p, "audio/flac"),
        "at9": (at9_p, "application/octet-stream"),
    }


def test_section14_projects_lifecycle_isolation_conflict_atomic_failure_and_recipe_duplication(env_clients):
    c1, c2, ws = env_clients
    p1 = c1.post("/api/projects", json={"name": "Projeto Alfa"}).json()
    pid1 = p1["id"]
    assert p1["schemaVersion"] == 1
    assert p1["revision"] == 1

    # Cross-session isolation: c2 MUST NOT read or mutate c1's project
    assert c2.get(f"/api/projects/{pid1}").status_code == 404
    assert c2.post(f"/api/projects/{pid1}/duplicate", json={"name": "Roubo"}).status_code == 404

    # Upload home slot + save Ouroboros regional recipe (GM-C-004)
    with SAMPLE_PNG.open("rb") as fh:
        home_job = c1.post("/api/slot/home/upload", params={"project": pid1}, files={"file": ("home.png", fh, "image/png")}).json()["job_id"]

    rec_payload = {
        "version": 1,
        "name": "Receita Principal",
        "sourceKey": "animated/test",
        "regions": [
            {
                "id": "reg1",
                "name": "Céu",
                "strategy": "protect",
                "featherPx": 8,
                "speedScale": 1.0,
                "shapes": [{"kind": "rect", "bounds": [0.0, 0.0, 1.0, 0.4]}],
            }
        ],
    }
    rec_put = c1.put("/api/ouroboros/deluxe/recipes/principal", params={"project": pid1}, json=rec_payload)
    assert rec_put.status_code == 200, rec_put.text

    # Save revision 1 -> 2 with rename
    st1 = editor_state()
    st1["slots"] = {"home": home_job}
    saved = c1.put(f"/api/projects/{pid1}", json={"name": "Projeto Alfa Renomeado", "revision": 1, "state": st1})
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 2
    assert saved.json()["name"] == "Projeto Alfa Renomeado"

    # Revision conflict (409) when stale revision 1 tries to save different content
    conflict = c1.put(f"/api/projects/{pid1}", json={"name": "Outro Nome", "revision": 1, "state": st1})
    assert conflict.status_code == 409

    # Simulated atomic_write disk failure preserves revision 2 manifest intact!
    with mock.patch.object(projects, "atomic_write", side_effect=OSError("disk full")):
        failed = c1.put(f"/api/projects/{pid1}", json={"name": "Falha Disco", "revision": 2, "state": st1})
        assert failed.status_code == 503
    reopened = c1.get(f"/api/projects/{pid1}").json()
    assert reopened["revision"] == 2
    assert reopened["name"] == "Projeto Alfa Renomeado"

    # Duplicate project -> preserves slots AND Ouroboros regional recipes (GM-C-004)!
    dup = c1.post(f"/api/projects/{pid1}/duplicate", json={"name": "Projeto Alfa Cópia"})
    assert dup.status_code == 201, dup.text
    pid2 = dup.json()["id"]
    assert pid2 != pid1
    assert dup.json()["state"]["slots"]["home"] == home_job
    dup_rec = c1.get("/api/ouroboros/deluxe/recipes/principal", params={"project": pid2})
    assert dup_rec.status_code == 200, dup_rec.text
    assert dup_rec.json()["regions"][0]["name"] == "Céu"


def test_section15_library_all_9_formats_corruption_limits_no_orphan_leaks_and_reuse(env_clients, tmp_path):
    c1, c2, ws = env_clients
    p_a = c1.post("/api/projects", json={"name": "Reuse A"}).json()["id"]
    p_b = c1.post("/api/projects", json={"name": "Reuse B"}).json()["id"]

    formats = _make_9_real_formats(tmp_path)
    ids: dict[str, str] = {}
    shas: dict[str, str] = {}
    for fmt_name, (path, mime) in formats.items():
        raw = path.read_bytes()
        shas[fmt_name] = hashlib.sha256(raw).hexdigest()
        r = c1.post("/api/library", files={"file": (path.name, io.BytesIO(raw), mime)})
        assert r.status_code == 201, f"{fmt_name} failed: {r.status_code} {r.text}"
        asset = r.json()
        assert asset["sha256"] == shas[fmt_name]
        assert asset["validation"] == "verified"
        ids[fmt_name] = asset["id"]

        # Re-importing identical bytes deduplicates with HTTP 200
        r_dup = c1.post("/api/library", files={"file": (path.name, io.BytesIO(raw), mime)})
        assert r_dup.status_code == 200
        assert r_dup.json()["id"] == asset["id"]

    # Cross-session isolation on library originals
    assert c2.get(f"/api/library/{ids['png']}/original").status_code == 404

    # Cross-project reuse without mutating original bytes or read-only permissions
    for pid in (p_a, p_b):
        ap_img = c1.post(f"/api/library/{ids['png']}/apply", params={"project": pid}, json={"slot": "home"})
        assert ap_img.status_code == 200, ap_img.text
        ap_at9 = c1.post(f"/api/library/{ids['at9']}/apply", params={"project": pid}, json={"slot": "bgm"})
        assert ap_at9.status_code == 200, ap_at9.text

    orig_dl = c1.get(f"/api/library/{ids['png']}/original")
    assert orig_dl.status_code == 200
    assert hashlib.sha256(orig_dl.content).hexdigest() == shas["png"]

    # Error handling matrix: empty(400), unsupported ext(415), mismatched ext(415/422), corrupted(422), missing(404), 413
    assert c1.post("/api/library", files={"file": ("empty.png", io.BytesIO(b""), "image/png")}).status_code == 400
    assert c1.post("/api/library", files={"file": ("script.exe", io.BytesIO(b"MZ1234"), "application/octet-stream")}).status_code == 415
    assert c1.post("/api/library", files={"file": ("fake.jpg", io.BytesIO(formats["png"][0].read_bytes()), "image/jpeg")}).status_code in {415, 422}
    assert c1.post("/api/library", files={"file": ("fake.at9", io.BytesIO(SAMPLE_WAV.read_bytes()), "audio/wav")}).status_code in {415, 422}
    assert c1.post("/api/library", files={"file": ("broken.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42broken"), "video/mp4")}).status_code == 422
    assert c1.get(f"/api/library/{'f' * 64}/original").status_code == 404

    with mock.patch.object(library, "MAX_UPLOAD_BYTES", 64):
        assert c1.post("/api/library", files={"file": ("too_big.png", io.BytesIO(b"x" * 128), "image/png")}).status_code == 413

    # GM-C-003: Corrupted uploads to /api/slot, /api/animated, /api/convert MUST NOT leak orphan job dirs!
    media_dirs = list((ws / "studio-projects-v1").glob(f"*/{p_a}/media"))
    assert len(media_dirs) == 1
    before_entries = set(p.name for p in media_dirs[0].iterdir())

    bad_slot = c1.post("/api/slot/fn:trophy/upload", params={"project": p_a}, files={"file": ("corrupt.png", io.BytesIO(b"\x89PNG\r\n\x1a\ncorrupt"), "image/png")})
    assert bad_slot.status_code == 422
    bad_anim = c1.post("/api/animated/upload", params={"project": p_a}, files={"file": ("corrupt.mp4", io.BytesIO(b"not a real video"), "video/mp4")})
    assert bad_anim.status_code == 422
    bad_conv = c1.post("/api/convert/wav", params={"project": p_a}, files={"file": ("corrupt.wav", io.BytesIO(b"RIFFbad"), "audio/wav")})
    assert bad_conv.status_code == 422

    after_entries = set(p.name for p in media_dirs[0].iterdir())
    assert after_entries == before_entries, f"Orphan corrupted job dirs leaked: {after_entries - before_entries}"


def test_section16_icons_alpha_halo_free_dimensions_preview_parity_and_native_composed_glow(env_clients, tmp_path):
    c1, _, _ = env_clients
    pid = c1.post("/api/projects", json={"name": "Icons Gold Master"}).json()["id"]
    params = {"project": pid}

    # White circle RGBA PNG + palette+tRNS PNG
    white_rgba = _white_circle_rgba_png(tmp_path / "white_icon.png", 256)
    pal_png = tmp_path / "pal_icon.png"
    Image.open(white_rgba).convert("P", palette=Image.Palette.ADAPTIVE).save(pal_png)

    # Import high-res white circle to library for Compositor function icon glow sharpness test (GM-C-005)
    lib_white = c1.post("/api/library", files={"file": ("white_icon.png", white_rgba.open("rb"), "image/png")}).json()["id"]

    # Upload slots: home, icon0, fn:trophy (transparent white), fn:power (palette+tRNS), ct:browser (transparent white)
    slots_map = {}
    for slot_id, fpath in (
        ("home", SAMPLE_PNG),
        ("icon0", white_rgba),
        ("fn:trophy", white_rgba),
        ("fn:power", pal_png),
        ("ct:browser", white_rgba),
    ):
        r = c1.post(f"/api/slot/{slot_id}/upload", params=params, files={"file": (fpath.name, fpath.open("rb"), "image/png")})
        assert r.status_code == 200, r.text
        slots_map[slot_id] = r.json()["job_id"]

    # Verify slot preview preserves RGBA alpha and exact target size
    prev_r = c1.get(f"/api/slot/{slots_map['fn:trophy']}/preview", params=params)
    assert prev_r.status_code == 200
    prev_im = Image.open(io.BytesIO(prev_r.content)).convert("RGBA")
    assert prev_im.size == (128, 128)
    assert prev_im.getchannel("A").getextrema()[0] == 0
    assert prev_im.getchannel("A").getextrema()[1] == 255

    # Compose fn:setting in Compositor with high-res white circle layer (no base slot) to test GM-C-005 native 152x152 glow
    comp = {
        "version": 1,
        "surfaces": {
            "fn:setting": {
                "base": False,
                "layers": [
                    {
                        "id": "icon1",
                        "asset": lib_white,
                        "name": "White Circle",
                        "x": 64.0,
                        "y": 64.0,
                        "width": 110.0,
                        "height": 110.0,
                        "angle": 15.0,
                        "opacity": 1.0,
                        "fit": "contain",
                        "proportional": True,
                        "visible": True,
                        "locked": False,
                    }
                ],
            }
        },
    }
    st = editor_state()
    st["slots"] = slots_map
    st["composition"] = comp
    assert c1.put(f"/api/projects/{pid}", json={"name": "Icons Gold Master", "revision": 1, "state": st}).status_code == 200

    # Export validated Forge bundle ZIP and inspect EVERY icon PNG directly!
    exp = c1.post("/api/forge/bundle/export", params=params, data={"use_saved_project": "true"})
    assert exp.status_code == 200, exp.text
    rep = exp.json()["report"]
    assert rep["valid"] is True, rep["errors"]

    dl = c1.get(exp.json()["download"], params=params)
    assert dl.status_code == 200
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        def open_zip_img(member: str) -> Image.Image:
            return Image.open(io.BytesIO(zf.read(member)))

        expected_dims = {
            "Sc0/icon0.png": (512, 512),
            "Sc0/pic1.png": (1920, 1080),
            "Image0/texture/preview.png": (740, 416),
            "Image0/texture/function_icon/trophy.png": (128, 128),
            "Image0/texture/function_icon/trophy_glow.png": (152, 152),
            "Image0/texture/function_icon/power.png": (128, 128),
            "Image0/texture/function_icon/power_glow.png": (152, 152),
            "Image0/texture/function_icon/setting.png": (128, 128),
            "Image0/texture/function_icon/setting_glow.png": (152, 152),
            "Image0/texture/content_icon/browser.png": (512, 512),
        }
        for member, dims in expected_dims.items():
            im = open_zip_img(member)
            assert im.size == dims, f"{member}: {im.size} != {dims}"
            if "icon" in member:
                assert "A" in im.mode, f"{member} missing alpha: {im.mode}"

        # GM-C-002 proof: composite exported transparent white icons onto pure white -> ZERO dark fringe halo!
        for member in (
            "Image0/texture/function_icon/trophy.png",
            "Image0/texture/function_icon/trophy_glow.png",
            "Image0/texture/content_icon/browser.png",
            "Image0/texture/function_icon/setting.png",
            "Image0/texture/function_icon/setting_glow.png",
        ):
            im = open_zip_img(member).convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            bg.alpha_composite(im)
            rgb = np.asarray(bg)[..., :3]
            assert int(rgb.min()) >= 250, f"Dark alpha halo detected in {member}: min RGB={rgb.min()}"

        # Preview <-> export parity for fn:trophy (128x128 WebP vs PNG)
        exp_trophy = np.asarray(open_zip_img("Image0/texture/function_icon/trophy.png").convert("RGBA"), dtype=np.int16)
        prev_arr = np.asarray(prev_im, dtype=np.int16)
        mae_rgb_alpha = float(np.abs(exp_trophy - prev_arr).mean())
        assert mae_rgb_alpha < 3.0, f"Icon preview diverged from exported PNG (MAE={mae_rgb_alpha:.2f})"


def test_section17_compositor_deluxe_preview_export_parity_and_zero_temporal_layer_drift(env_clients, tmp_path):
    """GM-C-001 + Section 17: Compositor animated preview uses Deluxe loop frames and overlay has zero temporal drift."""
    c1, _, _ = env_clients
    pid = c1.post("/api/projects", json={"name": "Compositor Deluxe Parity"}).json()["id"]
    params = {"project": pid}

    # Import sample.mp4 + a bright cyan overlay badge into library
    with SAMPLE_MP4.open("rb") as fh:
        vid_id = c1.post("/api/library", files={"file": ("sample.mp4", fh, "video/mp4")}).json()["id"]

    badge_p = tmp_path / "badge.png"
    badge = np.zeros((120, 120, 4), dtype=np.uint8)
    badge[10:110, 10:110] = [0, 255, 255, 255]  # Unmistakable solid cyan square
    Image.fromarray(badge, "RGBA").save(badge_p)
    badge_id = c1.post("/api/library", files={"file": ("badge.png", badge_p.open("rb"), "image/png")}).json()["id"]

    # Render a Deluxe take on a sub-interval (different from raw full-video uniform extraction!)
    spec = {
        "kind": "library",
        "id": vid_id,
        "start": 0.4,
        "end": 1.4,
        "frames": 6,
        "resolution": 2,  # 640x360
        "wait": 0.10,
        "mode": "natural",
        "deluxeFinish": False,
        "stabilizeCamera": False,
        "speedScale": 1.0,
    }
    tk = c1.post("/api/ouroboros/deluxe/render", params=params, json=spec)
    assert tk.status_code == 202, tk.text
    tid = tk.json()["id"]
    import time
    for _ in range(300):
        st = c1.get(f"/api/ouroboros/jobs/{tid}", params=params).json()
        if st["state"] != "working":
            break
        time.sleep(0.15)
    assert st["state"] == "ready", st
    key = st["key"]

    applied = c1.post("/api/ouroboros/deluxe/apply", params=params, json={"key": key, "expected": spec})
    assert applied.status_code == 200, applied.text
    anim_job_id = applied.json()["jobId"]

    # Build animated overlay surface placing cyan badge at top-left (x=180, y=180, 180x180 in 1920x1080 -> 60x60 in 640x360)
    surface = {
        "base": False,
        "layers": [
            {
                "id": "badge1",
                "asset": badge_id,
                "name": "Badge",
                "x": 180.0,
                "y": 180.0,
                "width": 180.0,
                "height": 180.0,
                "angle": 0.0,
                "opacity": 1.0,
                "fit": "stretch",
                "proportional": True,
                "visible": True,
                "locked": False,
            }
        ],
    }

    # 1. Request Compositor animation preview
    prev_res = c1.post(
        "/api/composition/animation",
        params=params,
        json={"job": anim_job_id, "size": [640, 360], "frames": 6, "surface": surface},
    )
    assert prev_res.status_code == 200, prev_res.text
    prev_urls = prev_res.json()["frames"]
    assert len(prev_urls) == 6
    prev_frames = [np.asarray(Image.open(io.BytesIO(c1.get(u, params=params).content)).convert("RGB")) for u in prev_urls]

    # 2. Export composed RAF ZIP with the exact same surface
    comp_json = json.dumps({"version": 1, "surfaces": {"animated": surface}})
    exp_res = c1.post(
        f"/api/animated/{anim_job_id}/export",
        params=params,
        data={"frames": "6", "wait_seconds": "0.10", "composition": comp_json, "animation_size": "640x360"},
    )
    assert exp_res.status_code == 200, exp_res.text
    raf_zip = c1.get(exp_res.json()["download"], params=params).content

    dds_frames = []
    with zipfile.ZipFile(io.BytesIO(raf_zip)) as zf:
        names = set(zf.namelist())
        prefix = "scene/background.raf/" if "scene/background.raf/00.dds" in names else "background.raf/"
        for i in range(6):
            rgba = dxt1.decode_dds(zf.read(f"{prefix}{i:02d}.dds"))
            dds_frames.append(rgba[..., :3])

    # GM-C-001 Proof: Every Compositor preview frame MUST match its exported composed DDS frame (modulo DXT1 quantization)!
    for i in range(6):
        mae = float(np.abs(prev_frames[i].astype(np.float32) - dds_frames[i].astype(np.float32)).mean())
        assert mae < 4.5, f"Frame {i} compositor preview diverged from exported Deluxe DDS (MAE={mae:.2f})"

    # Section 17 Proof: Zero temporal drift on the cyan overlay patch across all 6 frames!
    # At 640x360, center=(60,60), size=60x60 -> interior [42:78, 42:78] must be locked solid cyan [0, 255, 255]
    patch_0 = prev_frames[0][45:75, 45:75]
    assert float(patch_0[..., 0].mean()) < 2.0 and float(patch_0[..., 1].mean()) > 253.0 and float(patch_0[..., 2].mean()) > 253.0
    for i in range(1, 6):
        drift = float(np.abs(prev_frames[i][45:75, 45:75].astype(np.float32) - patch_0.astype(np.float32)).max())
        assert drift == 0.0, f"Layer drifted temporally on frame {i} (max diff={drift})"
