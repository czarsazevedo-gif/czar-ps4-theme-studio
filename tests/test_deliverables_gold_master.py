"""Comprehensive unit + API tests for the 4 Gold Master deliverables:
1. Streaming 2 MiB chunked upload (HTTP 413 fix) + 4K 60fps video normalization up to 120 MiB
2. High-quality auto-conversion for Function (128x128 + native 152x152 glow) & Content (512x512) icons + SVG
3. 5 separate media folders (Library + Project subfolders) with full backward compatibility
4. Continuous >= 5h operation (keepalive, >= 6h TTLs, SIGHUP-proof supervisor in start.sh)
"""

from __future__ import annotations

import hashlib
import io
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, jobs, library, main, media, ouroboros, projects, uploads
from app.forge import audio_router as forge_audio
from app.forge import pkg_router as forge_pkg

REPO = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
SAMPLE_MP4 = HERE / "sample.mp4"
SAMPLE_GIF = HERE / "sample.gif"
SAMPLE_WAV = HERE / "sample_sfx.wav"


@pytest.fixture()
def studio_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(jobs, "WORKSPACE", ws)
    monkeypatch.setattr(projects, "ROOT", ws / "projects")
    origin = "http://127.0.0.1:8000"
    with (
        TestClient(main.app, base_url=origin, headers={"Origin": origin}) as c1,
        TestClient(main.app, base_url=origin, headers={"Origin": origin}) as c2,
    ):
        assert c1.get("/api/projects").status_code == 200
        assert c2.get("/api/projects").status_code == 200
        yield c1, c2, ws


def _chunked_upload(
    client: TestClient,
    *,
    purpose: str,
    filename: str,
    payload: bytes,
    params: dict | None = None,
    project: str | None = None,
    chunk_size: int = 2 * 1024 * 1024,
    background: bool = True,
) -> dict:
    q = {"project": project} if project else {}
    init_r = client.post(
        "/api/uploads/init",
        params=q,
        json={
            "purpose": purpose,
            "filename": filename,
            "size": len(payload),
            "mime": "application/octet-stream",
            "params": params or {},
        },
    )
    assert init_r.status_code in (200, 201), init_r.text
    uid = init_r.json()["upload_id"]

    offset = 0
    while offset < len(payload):
        chunk = payload[offset : offset + chunk_size]
        put_q = {**q, "offset": str(offset)}
        put_r = client.put(
            f"/api/uploads/{uid}/chunk",
            params=put_q,
            content=chunk,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert put_r.status_code == 200, put_r.text
        offset = put_r.json()["received_bytes"]
    assert offset == len(payload)

    comp_q = {**q, "background": "true" if background else "false"}
    comp_r = client.post(f"/api/uploads/{uid}/complete", params=comp_q)
    assert comp_r.status_code in (200, 202), comp_r.text
    state = comp_r.json()
    deadline = time.time() + 120
    while state["status"] in ("uploading", "processing"):
        assert time.time() < deadline, f"Upload timed out: {state}"
        time.sleep(0.1)
        st_r = client.get(f"/api/uploads/{uid}/status", params=q)
        assert st_r.status_code == 200, st_r.text
        state = st_r.json()
    return state


def _make_video(dest: Path, *, width: int, height: int, fps: int, duration: float = 1.0) -> Path:
    cmd = [
        media.FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    subprocess.run(cmd, check=True)
    return dest


def _moov_before_mdat(mp4_path: Path) -> bool:
    data = mp4_path.read_bytes()
    moov = data.find(b"moov")
    mdat = data.find(b"mdat")
    return moov != -1 and mdat != -1 and moov < mdat


def _make_white_svg(width: int = 24, height: int = 24) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" fill="none" stroke="#ffffff" stroke-width="2">'
        f'<circle cx="{width / 2}" cy="{height / 2}" r="{min(width, height) * 0.34}"/>'
        f'<path d="M{width * 0.25} {height * 0.5} L{width * 0.75} {height * 0.5}"/>'
        f"</svg>"
    ).encode("utf-8")


def _make_detailed_png(size: tuple[int, int] = (512, 512)) -> bytes:
    w, h = size
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    # Crisp white concentric rings & diagonal spokes on transparent canvas (high-freq detail)
    yy, xx = np.ogrid[:h, :w]
    cx, cy = w / 2.0, h / 2.0
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    mask = ((dist > w * 0.14) & (dist < w * 0.18)) | ((dist > w * 0.32) & (dist < w * 0.36))
    mask |= (np.abs((xx - cx) - (yy - cy)) < 5) & (dist < w * 0.42)
    mask |= (np.abs((xx - cx) + (yy - cy)) < 5) & (dist < w * 0.42)
    arr[mask] = [255, 255, 255, 255]
    buf = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Deliverable 1: Streaming 2 MiB chunked upload + HTTP 413 fix + 4K 60fps
# ---------------------------------------------------------------------------

def test_fps_integer_divisor_rules():
    assert media.compute_normalized_fps(60.0) == 30.0
    assert media.compute_normalized_fps(59.94) == pytest.approx(29.97, abs=0.01)
    assert media.compute_normalized_fps(50.0) == 25.0
    assert media.compute_normalized_fps(48.0) == 24.0
    assert media.compute_normalized_fps(120.0) == 30.0
    assert media.compute_normalized_fps(36.0) is None
    assert media.compute_normalized_fps(30.0) is None
    assert media.compute_normalized_fps(25.0) is None
    assert media.compute_normalized_fps(24.0) is None
    assert media.compute_normalized_fps(None) is None


def test_chunked_upload_multi_chunk_409_recovery_cancel_and_isolation(studio_env, tmp_path: Path):
    c1, c2, ws = studio_env
    pid = c1.post("/api/projects", json={"name": "Chunked Project"}).json()["id"]

    # 1. Multi-chunk upload with forced small chunk size (256 KiB) + 409 offset self-heal
    payload = SAMPLE_MP4.read_bytes()
    init_r = c1.post(
        "/api/uploads/init",
        params={"project": pid},
        json={
            "purpose": "animated",
            "filename": "clip.mp4",
            "size": len(payload),
            "params": {"frames": 4, "size": "640x360"},
        },
    )
    assert init_r.status_code == 201, init_r.text
    uid = init_r.json()["upload_id"]

    # Session isolation: c2 cannot access c1's upload
    assert c2.get(f"/api/uploads/{uid}/status").status_code == 404
    assert c2.put(f"/api/uploads/{uid}/chunk?offset=0", content=b"x").status_code == 404
    assert c2.post(f"/api/uploads/{uid}/cancel").status_code == 404

    # Send first 256 KiB chunk
    first_chunk = payload[: 256 * 1024]
    r0 = c1.put(f"/api/uploads/{uid}/chunk", params={"project": pid, "offset": 0}, content=first_chunk)
    assert r0.status_code == 200
    assert r0.json()["received_bytes"] == len(first_chunk)

    # Wrong offset triggers 409 with expected_offset for client self-heal
    r_bad = c1.put(f"/api/uploads/{uid}/chunk", params={"project": pid, "offset": 999999}, content=b"abc")
    assert r_bad.status_code == 409
    assert r_bad.json()["expected_offset"] == len(first_chunk)

    # Complete remaining bytes in 256 KiB chunks
    offset = len(first_chunk)
    while offset < len(payload):
        chunk = payload[offset : offset + 256 * 1024]
        r = c1.put(f"/api/uploads/{uid}/chunk", params={"project": pid, "offset": offset}, content=chunk)
        assert r.status_code == 200, r.text
        offset = r.json()["received_bytes"]

    comp = c1.post(f"/api/uploads/{uid}/complete", params={"project": pid, "background": "true"})
    assert comp.status_code == 202, comp.text
    state = comp.json()
    for _ in range(200):
        if state["status"] not in ("uploading", "processing"):
            break
        time.sleep(0.05)
        state = c1.get(f"/api/uploads/{uid}/status", params={"project": pid}).json()
    assert state["status"] == "completed", state
    assert state["result"]["budget"]["frames"] == 4
    # Light <=720p <=16 MiB video preserved byte-for-byte intact!
    assert not (state["result"].get("normalized") or {}).get("normalized")
    stored_src = next((ws / "projects").rglob(f"animated-{state['result']['job_id']}/source.mp4"))
    assert hashlib.sha256(stored_src.read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()
    # payload.part deleted after completion
    assert not list((ws / "projects").rglob("payload.part"))

    # 2. Cancel removes payload.part cleanly
    init2 = c1.post(
        "/api/uploads/init",
        json={"purpose": "library", "filename": "cancel_me.mp4", "size": 4096},
    ).json()
    uid2 = init2["upload_id"]
    c1.put(f"/api/uploads/{uid2}/chunk?offset=0", content=b"0" * 1024)
    assert list((ws / "projects").rglob("payload.part"))
    can = c1.post(f"/api/uploads/{uid2}/cancel")
    assert can.status_code == 200
    assert can.json()["status"] == "cancelled"
    assert not list((ws / "projects").rglob("payload.part"))

    # 3. Video > 120 MiB rejected at init with 413 + X-Czar-Limit-Error: 1
    r_big = c1.post(
        "/api/uploads/init",
        json={
            "purpose": "animated",
            "filename": "huge.mp4",
            "size": config.MAX_VIDEO_UPLOAD_BYTES + 1,
        },
    )
    assert r_big.status_code == 413
    assert r_big.headers.get("x-czar-limit-error") == "1"


def test_4k_60fps_video_normalizes_to_720p_30fps_high41_faststart_and_cleans_raw(studio_env, tmp_path: Path):
    c1, _, ws = studio_env
    pid = c1.post("/api/projects", json={"name": "4K Normalize"}).json()["id"]

    raw_4k = _make_video(tmp_path / "uhd_60fps.mp4", width=3840, height=2160, fps=60, duration=1.0)
    raw_bytes = raw_4k.read_bytes()

    # Upload 4K 60fps via chunked upload to animated background
    state = _chunked_upload(
        c1,
        purpose="animated",
        filename="uhd_60fps.mp4",
        payload=raw_bytes,
        params={"frames": 6, "size": "1280x720"},
        project=pid,
        chunk_size=256 * 1024,
    )
    assert state["status"] == "completed", state
    res = state["result"]
    assert res["normalized"]["normalized"] is True
    assert res["normalized"]["original_width"] == 3840
    assert res["normalized"]["original_height"] == 2160
    assert res["normalized"]["width"] == 1280
    assert res["normalized"]["height"] == 720
    assert res["normalized"]["fps"] == pytest.approx(30.0, abs=0.5)

    # Inspect actual stored source.mp4 with ffprobe
    job_src = next((ws / "projects").rglob(f"animated-{res['job_id']}/source.mp4"))
    probe_json = subprocess.check_output(
        [
            media.FFPROBE, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,profile,level,pix_fmt,width,height,avg_frame_rate",
            "-of", "json", str(job_src),
        ]
    )
    import json
    st = json.loads(probe_json)["streams"][0]
    assert st["codec_name"] == "h264"
    assert st["profile"] == "High"
    assert int(st["level"]) == 41
    assert st["pix_fmt"] == "yuv420p"
    assert (st["width"], st["height"]) == (1280, 720)
    num, den = (int(x) for x in st["avg_frame_rate"].split("/"))
    assert num / den == pytest.approx(30.0, abs=0.5)
    assert _moov_before_mdat(job_src), "Normalized MP4 must have +faststart (moov before mdat)"

    # Ensure NO temporary raw 4K files or payload.part remain on disk
    assert not list((ws / "projects").rglob("payload.part"))
    assert not list((ws / "projects").rglob("*raw-video*"))
    assert not list((ws / "projects").rglob("*.norm.mp4"))

    # Also test 4K normalization via Library chunked upload
    lib_state = _chunked_upload(
        c1,
        purpose="library",
        filename="uhd_lib.mp4",
        payload=raw_bytes,
        params={"folder": "animated_bg"},
        chunk_size=256 * 1024,
    )
    assert lib_state["status"] == "completed", lib_state
    asset = lib_state["result"]
    assert (asset["width"], asset["height"]) == (1280, 720)
    assert asset["fps"] == pytest.approx(30.0, abs=0.5)
    assert asset["folder"] == "animated_bg"
    assert asset["normalized"]["normalized"] is True


def test_gif_and_light_720p_video_remain_byte_for_byte_intact(studio_env, tmp_path: Path):
    c1, _, ws = studio_env

    # 1. GIF stays byte-for-byte identical
    gif_bytes = SAMPLE_GIF.read_bytes()
    gif_sha = hashlib.sha256(gif_bytes).hexdigest()
    r_gif = c1.post("/api/library", files={"file": ("anim.gif", gif_bytes, "image/gif")})
    assert r_gif.status_code == 201, r_gif.text
    assert r_gif.json()["id"] == gif_sha
    assert r_gif.json()["folder"] == "animated_bg"

    # 2. Light <=720p <=16 MiB 30fps video stays byte-for-byte identical
    vid_bytes = SAMPLE_MP4.read_bytes()
    vid_sha = hashlib.sha256(vid_bytes).hexdigest()
    r_vid = c1.post("/api/library", files={"file": ("light.mp4", vid_bytes, "video/mp4")})
    assert r_vid.status_code == 201, r_vid.text
    assert r_vid.json()["id"] == vid_sha


def test_chunked_upload_supports_audio_forge_inspect_and_convert(studio_env):
    c1, _, ws = studio_env
    pid = c1.post("/api/projects", json={"name": "Audio & Forge Chunked"}).json()["id"]
    wav_bytes = SAMPLE_WAV.read_bytes()

    # Audio SFX via chunked upload
    sfx_st = _chunked_upload(
        c1,
        purpose="audio",
        filename="cursor.wav",
        payload=wav_bytes,
        params={"slot": "se_cursor", "gain_db": 0.0},
        project=pid,
        chunk_size=16 * 1024,
    )
    assert sfx_st["status"] == "completed", sfx_st
    assert sfx_st["result"]["slot"] == "se_cursor"
    assert "vag" in sfx_st["result"]["codec"]

    # Forge audio inspect via chunked upload
    insp_st = _chunked_upload(
        c1,
        purpose="forge_audio_inspect",
        filename="bgm_inspect.wav",
        payload=wav_bytes,
        project=pid,
        chunk_size=16 * 1024,
    )
    assert insp_st["status"] == "completed", insp_st
    assert insp_st["result"]["staging_id"]
    assert "loudness" in insp_st["result"]

    # Standalone audio converter via chunked upload
    conv_st = _chunked_upload(
        c1,
        purpose="convert",
        filename="sfx.wav",
        payload=wav_bytes,
        params={"target": "vag"},
        chunk_size=16 * 1024,
    )
    assert conv_st["status"] == "completed", conv_st
    assert conv_st["result"]["name"].endswith(".vag")

    # Forge PKG inspect via chunked upload (malformed .pkg returns structured BUILD_FAILED report & cleans payload.part)
    pkg_st = _chunked_upload(
        c1,
        purpose="forge_pkg_inspect",
        filename="bad.pkg",
        payload=b"not-a-valid-pkg" * 64,
        chunk_size=256,
    )
    assert pkg_st["status"] == "completed", pkg_st
    assert pkg_st["result"]["structure_validated"] is False
    assert pkg_st["result"]["release_gate"] == "BUILD_FAILED"
    assert pkg_st["result"]["errors"]
    assert not list((ws / "projects").rglob("payload.part"))


# ---------------------------------------------------------------------------
# Deliverable 2: High-quality auto-conversion for fn:* / ct:* + full SVG
# ---------------------------------------------------------------------------

def test_svg_rasterizer_backends_and_zero_dark_halo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    info = media.svg_rasterizer_info()
    assert info["available"] is True
    assert len(info["backends"]) >= 2, f"Expected redundant SVG backends, got {info}"

    # Both square and non-square (rectangular) white SVGs must have ZERO dark halo
    for name, svg_bytes in [
        ("square.svg", _make_white_svg(24, 24)),
        ("wide.svg", _make_white_svg(48, 24)),
        ("tall.svg", _make_white_svg(24, 48)),
    ]:
        svg_path = tmp_path / name
        svg_path.write_bytes(svg_bytes)
        out = tmp_path / f"{name}.png"
        media.render_image(svg_path, out, (128, 128), force_rgba=True, contain_transparent=True)
        arr = np.asarray(Image.open(out))
        assert arr.shape == (128, 128, 4)
        vis = arr[:, :, 3] > 10
        assert int(vis.sum()) > 100
        # Zero dark halo: every non-transparent pixel must stay pure white (>= 248)
        assert int(arr[vis, :3].min()) >= 248, f"Dark halo detected in {name}: min RGB={arr[vis, :3].min()}"

    # Verify fallback to rsvg-convert and resvg-py when cairosvg is unavailable
    svg_path = tmp_path / "fallback.svg"
    svg_path.write_bytes(_make_white_svg(32, 32))
    import builtins
    real_import = builtins.__import__

    def no_cairosvg(name, *args, **kwargs):
        if name == "cairosvg":
            raise ImportError("simulated missing cairosvg")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_cairosvg)
    im_rsvg = media._rasterize_svg_rgba(svg_path, (152, 152))
    assert im_rsvg.size == (152, 152) and im_rsvg.mode == "RGBA"
    arr_rsvg = np.asarray(im_rsvg)
    assert int(arr_rsvg[arr_rsvg[:, :, 3] > 10, :3].min()) >= 248

    # Also disable rsvg-convert so resvg-py runs
    monkeypatch.setattr(media, "_find_rsvg_convert", lambda: None)
    im_resvg = media._rasterize_svg_rgba(svg_path, (152, 152))
    assert im_resvg.size == (152, 152) and im_resvg.mode == "RGBA"
    arr_resvg = np.asarray(im_resvg)
    assert int(arr_resvg[arr_resvg[:, :, 3] > 10, :3].min()) >= 248


def test_icon_auto_conversion_fn_128_native_152_glow_and_ct_512_raster_and_svg(studio_env, tmp_path: Path):
    c1, _, ws = studio_env
    pid = c1.post("/api/projects", json={"name": "Icons HQ"}).json()["id"]
    q = {"project": pid}

    # 1. Function icon from high-res 512x512 raster PNG -> 128x128 RGBA PNG + native 152x152 glow
    hi_png = _make_detailed_png((512, 512))
    r_fn = c1.post(
        "/api/slot/fn:setting/upload",
        params=q,
        files={"file": ("settings_hq.png", hi_png, "image/png")},
    )
    assert r_fn.status_code == 200, r_fn.text
    d_fn = r_fn.json()
    assert d_fn["converted"]["size"] == [128, 128]
    assert d_fn["converted"]["glow_size"] == [152, 152]
    assert d_fn["converted"]["native_glow_from_source"] is True

    icon_r = c1.get(d_fn["asset"], params=q)
    glow_r = c1.get(d_fn["glow"], params=q)
    assert icon_r.status_code == 200 and glow_r.status_code == 200
    im_128 = Image.open(io.BytesIO(icon_r.content))
    im_152 = Image.open(io.BytesIO(glow_r.content))
    assert im_128.size == (128, 128) and im_128.mode == "RGBA"
    assert im_152.size == (152, 152) and im_152.mode == "RGBA"

    # Prove native 152x152 was rendered from 512x512 source, NOT upscaled from 128x128!
    upscaled = im_128.resize((152, 152), Image.Resampling.LANCZOS)
    diff = np.abs(np.asarray(im_152, dtype=np.int16) - np.asarray(upscaled, dtype=np.int16))
    assert float(diff.mean()) > 0.25, "152x152 glow must come from high-res source, not 128->152 upscale"

    # 2. Function icon from SVG -> 128x128 RGBA PNG + native 152x152 glow RGBA PNG (zero halo)
    svg_bytes = _make_white_svg(24, 24)
    r_fn_svg = c1.post(
        "/api/slot/fn:friend/upload",
        params=q,
        files={"file": ("friend.svg", svg_bytes, "image/svg+xml")},
    )
    assert r_fn_svg.status_code == 200, r_fn_svg.text
    d_svg = r_fn_svg.json()
    assert d_svg["converted"]["source_is_svg"] is True
    assert d_svg["converted"]["size"] == [128, 128]
    assert d_svg["converted"]["glow_size"] == [152, 152]
    im_svg_152 = Image.open(io.BytesIO(c1.get(d_svg["glow"], params=q).content))
    arr_svg_152 = np.asarray(im_svg_152)
    assert arr_svg_152.shape == (152, 152, 4)
    assert int(arr_svg_152[arr_svg_152[:, :, 3] > 10, :3].min()) >= 248

    # 3. Content icon from SVG and raster -> exact 512x512 RGBA PNG
    r_ct_svg = c1.post(
        "/api/slot/ct:browser/upload",
        params=q,
        files={"file": ("browser.svg", svg_bytes, "image/svg+xml")},
    )
    assert r_ct_svg.status_code == 200, r_ct_svg.text
    d_ct = r_ct_svg.json()
    assert d_ct["converted"]["size"] == [512, 512]
    assert d_ct["converted"]["glow_size"] is None
    im_ct = Image.open(io.BytesIO(c1.get(d_ct["asset"], params=q).content))
    assert im_ct.size == (512, 512) and im_ct.mode == "RGBA"

    # 4. Batch upload by name accepts SVG icons and converts them automatically
    r_batch = c1.post(
        "/api/slots/upload-batch",
        params=q,
        files=[
            ("files", ("trofeus.svg", svg_bytes, "image/svg+xml")),
            ("files", ("navegador.svg", svg_bytes, "image/svg+xml")),
        ],
    )
    assert r_batch.status_code == 200, r_batch.text
    by_slot = {u["slot"]: u for u in r_batch.json()["uploaded"]}
    assert "fn:trophy" in by_slot and by_slot["fn:trophy"]["converted"]["glow_size"] == [152, 152]
    assert "ct:browser" in by_slot and by_slot["ct:browser"]["converted"]["size"] == [512, 512]

    # 5. Unsafe SVG rejected in chunked upload too
    bad_svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    bad_st = _chunked_upload(
        c1,
        purpose="slot",
        filename="xss.svg",
        payload=bad_svg,
        params={"slot": "fn:message"},
        project=pid,
    )
    assert bad_st["status"] == "failed"
    assert bad_st["error_status"] == 422


# ---------------------------------------------------------------------------
# Deliverable 3: 5 Separate Media Folders (Library + Project Subfolders)
# ---------------------------------------------------------------------------

def test_five_separate_media_folders_in_library_and_project(studio_env, tmp_path: Path):
    c1, _, ws = studio_env
    pid = c1.post("/api/projects", json={"name": "Pastas 5"}).json()["id"]
    q = {"project": pid}

    # Prepare 5 distinct assets (distinct bytes so dedup keeps them separate)
    svg_fn = _make_white_svg(24, 24)
    svg_ct = _make_white_svg(32, 32)
    img_bg = _make_detailed_png((320, 180))
    wav_bytes = SAMPLE_WAV.read_bytes()
    gif_bytes = SAMPLE_GIF.read_bytes()

    uploads_plan = [
        ("content_icons", "ct_icon.svg", svg_ct, "image/svg+xml"),
        ("function_icons", "fn_icon.svg", svg_fn, "image/svg+xml"),
        ("audio", "sfx.wav", wav_bytes, "audio/wav"),
        ("theme_image", "bg_home.png", img_bg, "image/png"),
        ("animated_bg", "loop.gif", gif_bytes, "image/gif"),
    ]
    created_ids = {}
    for folder, fname, data, mime in uploads_plan:
        r = c1.post("/api/library", data={"folder": folder}, files={"file": (fname, data, mime)})
        assert r.status_code == 201, f"{folder}: {r.text}"
        body = r.json()
        assert body["folder"] == folder
        created_ids[folder] = body["id"]

    # Check GET /api/library counts and per-folder filtering
    all_r = c1.get("/api/library").json()
    assert len(all_r["items"]) == 5
    for fid in ("content_icons", "function_icons", "audio", "theme_image", "animated_bg"):
        assert all_r["counts"][fid] == 1
        sub_r = c1.get("/api/library", params={"folder": fid}).json()
        assert len(sub_r["items"]) == 1
        assert sub_r["items"][0]["id"] == created_ids[fid]

    # Verify library-v1/folders/<subdir>/ hardlinks exist on disk
    lib_folders_root = next((ws / "projects").rglob("library-v1/folders"))
    for fdef in config.MEDIA_FOLDERS:
        sub_dir = lib_folders_root / fdef["subdir"]
        assert sub_dir.is_dir()
        assert len(list(sub_dir.iterdir())) == 1

    # Reassign image folder via PATCH /api/library/{id}/folder
    patch_r = c1.patch(
        f"/api/library/{created_ids['theme_image']}/folder",
        json={"folder": "content_icons"},
    )
    assert patch_r.status_code == 200, patch_r.text
    assert patch_r.json()["folder"] == "content_icons"
    assert c1.get("/api/library", params={"folder": "content_icons"}).json()["counts"]["content_icons"] == 2
    assert c1.get("/api/library", params={"folder": "theme_image"}).json()["counts"]["theme_image"] == 0

    # Incompatible folder rejected with 415
    bad_folder = c1.patch(
        f"/api/library/{created_ids['audio']}/folder",
        json={"folder": "function_icons"},
    )
    assert bad_folder.status_code == 415

    # Upload directly to project slots & verify <project>/folders/<5 subdirs>/ + /api/projects/{id}/folders
    assert c1.post("/api/slot/home/upload", params=q, files={"file": ("home.png", img_bg, "image/png")}).status_code == 200
    assert c1.post("/api/slot/fn:setting/upload", params=q, files={"file": ("set.svg", svg_fn, "image/svg+xml")}).status_code == 200
    assert c1.post("/api/slot/ct:browser/upload", params=q, files={"file": ("brw.svg", svg_ct, "image/svg+xml")}).status_code == 200
    assert c1.post("/api/audio/se_cursor/upload", params=q, files={"file": ("cur.wav", wav_bytes, "audio/wav")}).status_code == 200
    assert c1.post("/api/animated/upload", params=q, data={"frames": "4", "size": "640x360"}, files={"file": ("bg.gif", gif_bytes, "image/gif")}).status_code == 200

    pf = c1.get(f"/api/projects/{pid}/folders")
    assert pf.status_code == 200, pf.text
    folder_map = {f["id"]: f for f in pf.json()["folders"]}
    assert set(folder_map.keys()) == {"content_icons", "function_icons", "audio", "theme_image", "animated_bg"}
    for fid, info in folder_map.items():
        assert len(info["files"]) >= 1, f"Project subfolder {fid} should have mirrored files: {info}"


# ---------------------------------------------------------------------------
# Deliverable 4: Continuous >= 5h operation (keepalive, >=6h TTL, start.sh)
# ---------------------------------------------------------------------------

def test_keepalive_and_extended_ttls_over_6_hours(studio_env):
    c1, _, ws = studio_env
    pid = c1.post("/api/projects", json={"name": "Long Session"}).json()["id"]

    # TTLs must be >= 6 hours (configured to 8 hours = 28800s)
    assert config.SESSION_MEDIA_TTL_SECONDS >= 6 * 3600
    assert config.KEEPALIVE_INTERVAL_MS == 25_000

    # Create an upload session and verify keepalive refreshes its updated_at / mtime
    init_r = c1.post(
        "/api/uploads/init",
        params={"project": pid},
        json={"purpose": "library", "filename": "keep.png", "size": 100},
    ).json()
    uid = init_r["upload_id"]
    state_file = next((ws / "projects").rglob(f"{uid}/state.json"))
    # Age the state file by 2 hours
    old_ts = time.time() - 7200
    os.utime(state_file.parent, (old_ts, old_ts))

    ka_post = c1.post("/api/keepalive", params={"project": pid})
    assert ka_post.status_code == 200, ka_post.text
    body = ka_post.json()
    assert body["ok"] is True
    assert body["ttl_seconds"] >= 6 * 3600
    assert body["keepalive_interval_ms"] == 25_000
    assert body["refreshed"]["uploads"] >= 1
    assert state_file.parent.stat().st_mtime > old_ts + 3600

    ka_get = c1.get("/api/keepalive", params={"project": pid})
    assert ka_get.status_code == 200 and ka_get.json()["ok"] is True

    # /api/system exposes upload chunk size, 120 MiB video limit, 5 folders, SVG info & keepalive TTL
    sys_info = c1.get("/api/system").json()
    assert sys_info["chunk_size"] == 2 * 1024 * 1024
    assert sys_info["max_video_bytes"] == 120 * 1024 * 1024
    assert sys_info["keepalive_interval_ms"] == 25_000
    assert sys_info["ttl_seconds"] >= 6 * 3600
    assert len(sys_info["media_folders"]) == 5
    assert sys_info["svg_rasterizer"]["available"] is True


def test_start_sh_sighup_hardening_and_daemon_lifecycle():
    script = REPO / "start.sh"
    text = script.read_text(encoding="utf-8")
    assert "trap '' HUP" in text
    assert "--timeout-keep-alive" in text
    assert "CZAR_AUTO_INSTALL_FORGE:-0" in text
    assert "/healthz" in text

    # Syntax check
    subprocess.run(["bash", "-n", str(script)], check=True)
    # Help flag works cleanly
    help_out = subprocess.check_output(["bash", str(script), "--help"], text=True)
    assert "--daemon" in help_out and "--stop" in help_out

    # Real daemon startup + SIGHUP immunity + status + stop on an ephemeral port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    env = {**os.environ, "CZAR_PORT": str(port), "CZAR_HOST": "127.0.0.1"}
    pid_file = REPO / "workspace" / f".czar-studio-{port}.pid"
    log_file = REPO / "workspace" / f"czar-studio-{port}.log"
    try:
        subprocess.run(["bash", str(script), "--daemon"], cwd=REPO, env=env, check=True, timeout=30)
        assert pid_file.is_file()
        sup_pid = int(pid_file.read_text().strip())

        # Send SIGHUP directly to the supervisor — it MUST survive!
        os.kill(sup_pid, signal.SIGHUP)
        time.sleep(0.8)
        status_r = subprocess.run(
            ["bash", str(script), "--status"], cwd=REPO, env=env, capture_output=True, text=True
        )
        assert status_r.returncode == 0, status_r.stdout + status_r.stderr
        assert "Sonda /healthz: OK" in status_r.stdout
    finally:
        subprocess.run(["bash", str(script), "--stop"], cwd=REPO, env=env, check=False, timeout=20)
        pid_file.unlink(missing_ok=True)
        log_file.unlink(missing_ok=True)
