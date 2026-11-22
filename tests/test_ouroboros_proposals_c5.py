"""Checkpoint 5 — Unified OUROBOROS 3 Proposals (720p, 540p, 360p), Direct Apply & Chromium Touch E2E."""

from __future__ import annotations

import hashlib
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import dxt1, jobs, main, projects
from app.config import RAF_SIZE_LIMIT


def _make_periodic_mp4(path: Path, *, width: int = 320, height: int = 180, fps: int = 12, seconds: float = 3.0) -> None:
    total = int(round(fps * seconds))
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    frames = []
    for i in range(total):
        phase = 2.0 * math.pi * (i / float(fps))
        wave = 0.5 + 0.5 * np.sin(xx * 0.08 - phase)
        ripple = 0.5 + 0.5 * np.cos(yy * 0.06 + phase)
        r = np.clip(45 + 150 * wave, 0, 255).astype(np.uint8)
        g = np.clip(80 + 120 * ripple, 0, 255).astype(np.uint8)
        b = np.clip(140 + 80 * (0.5 * wave + 0.5 * ripple), 0, 255).astype(np.uint8)
        img = np.stack([r, g, b], axis=-1)
        # Sharp crisp center emblem for clear visual fidelity metrics
        cv2.circle(img, (width // 2, height // 2), 26, (240, 220, 120), 2)
        frames.append(img)
    arr = np.stack(frames, axis=0)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(fps),
            "-i",
            "pipe:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "16",
            str(path),
        ],
        input=arr.tobytes(),
        check=True,
        timeout=60,
    )


def _make_variable_delay_gif(path: Path, *, width: int = 240, height: int = 136) -> None:
    frames: list[Image.Image] = []
    durations: list[int] = []
    n = 12
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    for i in range(n):
        ph = 2.0 * math.pi * i / float(n)
        wave = 0.5 + 0.5 * np.sin(xx * 0.07 - ph)
        img = np.zeros((height, width, 3), dtype=np.uint8)
        img[..., 0] = np.clip(50 + 140 * wave, 0, 255).astype(np.uint8)
        img[..., 1] = np.clip(90 + 100 * (1.0 - wave), 0, 255).astype(np.uint8)
        img[..., 2] = 170
        frames.append(Image.fromarray(img, "RGB"))
        durations.append(50 if i % 2 == 0 else 150)
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
    )


def _poll_task(client: TestClient, pid: str, task_id: str, timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/ouroboros/jobs/{task_id}?project={pid}")
        assert r.status_code == 200, r.text
        st = r.json()
        if st["state"] != "working":
            return st
        time.sleep(0.25)
    raise AssertionError(f"Ouroboros task {task_id} timed out after {timeout}s")


def test_unified_three_proposals_identity_and_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(projects, "ROOT", tmp_path / "projects")
    monkeypatch.setattr(jobs, "WORKSPACE", tmp_path / "standalone")

    mp4 = tmp_path / "periodic_wave.mp4"
    gif = tmp_path / "variable_delays.gif"
    _make_periodic_mp4(mp4)
    _make_variable_delay_gif(gif)

    client = TestClient(main.app)
    assert client.get("/api/projects").status_code == 200
    resp_p = client.post("/api/projects", json={"name": "Ouroboros C5 API"})
    assert resp_p.status_code == 201, resp_p.text
    proj = resp_p.json()
    pid = proj["id"]

    # 1. Upload MP4 into project
    with mp4.open("rb") as fh:
        up = client.post(
            f"/api/animated/upload?project={pid}",
            files={"file": ("periodic_wave.mp4", fh, "video/mp4")},
            data={"frames": "12", "size": "1280x720"},
        )
    assert up.status_code == 200, up.text
    job_id = up.json()["job_id"]

    # 2. Run unified OUROBOROS -> 3 independent proposals (720p, 540p, 360p)
    run = client.post(
        f"/api/ouroboros/run?project={pid}",
        json={"kind": "animated", "id": job_id},
    )
    assert run.status_code == 202, run.text
    st = _poll_task(client, pid, run.json()["id"])
    assert st["state"] == "ready", st
    key = st["key"]
    assert key

    rep_r = client.get(f"/api/ouroboros/result/{key}?project={pid}")
    assert rep_r.status_code == 200, rep_r.text
    report = rep_r.json()
    assert report["type"] == "ouroboros_proposals"
    assert report["recommendedProfileId"] in {"quality", "balance", "flexibility"}
    assert report.get("mediaDiagnostic", {}).get("fields", {}).get("aptidao")
    props = {p["profileId"]: p for p in report["proposals"]}
    assert set(props.keys()) == {"quality", "balance", "flexibility"}
    assert props["quality"]["size"] == [1280, 720]
    assert props["balance"]["size"] == [960, 540]
    assert props["flexibility"]["size"] == [640, 360]

    # Each proposal must carry real artifacts, distinct renderKey, valid budget, and valid decoded RAF ZIP
    render_keys = set()
    for profile_id, p in props.items():
        assert len(p["renderKey"]) == 64
        render_keys.add(p["renderKey"])
        assert 4 <= p["frames"] <= p["maxFrames"]
        assert p["ddsBytes"] <= RAF_SIZE_LIMIT
        assert p["verdict"] in {"APROVADO", "CONDICIONAL", "REPROVADO"}
        # Check artifacts over HTTP
        w0 = client.get(f"/api/ouroboros/artifact/{key}/{profile_id}/00.webp?project={pid}")
        assert w0.status_code == 200 and len(w0.content) > 100
        diff = client.get(f"/api/ouroboros/artifact/{key}/{profile_id}/diff.png?project={pid}")
        assert diff.status_code == 200 and diff.content[:8] == b"\x89PNG\r\n\x1a\n"
        rz = client.get(f"/api/ouroboros/artifact/{key}/{profile_id}/background.raf.zip?project={pid}")
        assert rz.status_code == 200
        zf_path = tmp_path / f"{profile_id}.raf.zip"
        zf_path.write_bytes(rz.content)
        with zipfile.ZipFile(zf_path) as zf:
            names = set(zf.namelist())
            assert "scene/background.raf/index.xml" in names
            assert "scene/background.raf/plane.mdx" in names
            for i in range(p["frames"]):
                dname = f"scene/background.raf/{i:02d}.dds"
                assert dname in names
                blob = zf.read(dname)
                assert hashlib.sha256(blob).hexdigest() == p["ddsHashes"][f"{i:02d}.dds"]
    assert len(render_keys) == 3

    # 3. Cache hit check: re-running same request must complete rapidly and return identical key
    run_cached = client.post(
        f"/api/ouroboros/run?project={pid}",
        json={"kind": "animated", "id": job_id},
    )
    assert run_cached.status_code == 202
    st2 = _poll_task(client, pid, run_cached.json()["id"], timeout=30.0)
    assert st2["state"] == "ready" and st2["key"] == key

    # 4. Strict renderKey guard: sending quality's renderKey with profileId="balance" MUST fail 409!
    bad_apply = client.post(
        f"/api/ouroboros/apply?project={pid}",
        json={
            "key": key,
            "profileId": "balance",
            "expectedRenderKey": props["quality"]["renderKey"],
        },
    )
    assert bad_apply.status_code == 409, bad_apply.text

    # 5. Apply Proposal 2 (balance 960x540) with its exact renderKey
    bal = props["balance"]
    applied = client.post(
        f"/api/ouroboros/apply?project={pid}",
        json={
            "key": key,
            "profileId": "balance",
            "expectedRenderKey": bal["renderKey"],
        },
    )
    assert applied.status_code == 200, applied.text
    ap = applied.json()
    assert ap["applied"] is True
    assert ap["profileId"] == "balance"
    assert ap["renderKey"] == bal["renderKey"]
    assert ap["size"] == [960, 540]
    assert ap["frames"] == bal["frames"]

    # Verify manifest updated & exported RAF matches Proposal 2 DDS byte-for-byte (NOT Proposal 1!)
    manifest = client.get(f"/api/projects/{pid}").json()
    assert manifest["state"]["bgMode"] == "animated"
    assert manifest["state"]["anim"]["jobId"] == ap["jobId"]
    assert manifest["state"]["anim"]["size"] == [960, 540]
    assert manifest["state"]["anim"]["frames"] == bal["frames"]

    exp = client.post(
        f"/api/animated/{ap['jobId']}/export?project={pid}",
        data={"frames": str(bal["frames"]), "wait_seconds": str(bal["wait"]), "backend": "native"},
    )
    assert exp.status_code == 200, exp.text
    dl = client.get(f"/api/animated/{ap['jobId']}/download?project={pid}")
    assert dl.status_code == 200
    exp_zip = tmp_path / "exported_applied.raf.zip"
    exp_zip.write_bytes(dl.content)
    with zipfile.ZipFile(exp_zip) as zf:
        for i in range(bal["frames"]):
            blob = zf.read(f"scene/background.raf/{i:02d}.dds")
            assert hashlib.sha256(blob).hexdigest() == bal["ddsHashes"][f"{i:02d}.dds"]
            # Confirm it truly decoded as valid 960x540 DXT1
            rgba = dxt1.decode_dds(blob)
            assert rgba.shape[:2] == (540, 960)

    # 6. Upload variable-delay GIF and confirm Gifsicle inspection + Mode B + 3 proposals
    with gif.open("rb") as fh:
        up_g = client.post(
            f"/api/animated/upload?project={pid}",
            files={"file": ("variable_delays.gif", fh, "image/gif")},
            data={"frames": "12", "size": "960x540"},
        )
    assert up_g.status_code == 200, up_g.text
    g_job = up_g.json()["job_id"]
    run_g = client.post(
        f"/api/ouroboros/run?project={pid}",
        json={"kind": "animated", "id": g_job},
    )
    assert run_g.status_code == 202, run_g.text
    stg = _poll_task(client, pid, run_g.json()["id"])
    assert stg["state"] == "ready", stg
    rep_g = client.get(f"/api/ouroboros/result/{stg['key']}?project={pid}").json()
    assert rep_g["gifInfo"]["isGif"] is True
    assert rep_g["gifInfo"]["hasVariableDelays"] is True
    assert len(rep_g["proposals"]) == 3


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_playwright_touch_three_proposals_and_apply(tmp_path: Path) -> None:
    """Real Chromium touch E2E on 1480x924 (landscape) and 924x1480 (portrait): upload -> 3 proposals -> apply."""
    playwright = pytest.importorskip("playwright.sync_api")
    chromium_bin = Path("/usr/bin/chromium")
    if not chromium_bin.is_file():
        pytest.skip("/usr/bin/chromium unavailable")

    mp4 = tmp_path / "touch_wave.mp4"
    _make_periodic_mp4(mp4, width=320, height=180, fps=12, seconds=2.5)

    port = _free_port()
    env = os.environ.copy()
    env["CZAR_WORKSPACE"] = str(tmp_path / "workspace")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        # Wait for server readiness
        deadline = time.monotonic() + 25.0
        ready = False
        import urllib.request

        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{base_url}/api/system", timeout=2) as r:
                    if r.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.2)
        assert ready, "Uvicorn server failed to boot"

        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(chromium_bin), headless=True)
            context = browser.new_context(
                viewport={"width": 1480, "height": 924},
                has_touch=True,
                is_mobile=False,
            )
            page = context.new_page()
            page_errors: list[str] = []
            console_errors: list[str] = []
            page.on("pageerror", lambda err: page_errors.append(str(err)))
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )

            # 1. Open Studio & create a project via UI
            import re as _re

            page.goto(base_url, wait_until="networkidle")
            page.click("#project-new")
            page.fill("#project-dialog-name", "Tablet S10 Ouroboros")
            page.click('#project-dialog button[value="confirm"]')
            page.wait_for_url(_re.compile(r"\?project="), timeout=15000)
            page.wait_for_selector("main:not([inert])", timeout=15000)

            # 2. Switch to Animated mode and upload real MP4
            page.click('label.mode-opt:has(#bgmode-animated)')
            page.set_input_files("#anim-file", str(mp4))

            # 3. Wait for OUROBOROS to render all 3 proposal cards automatically!
            # Checkpoints G/H/I (KLT kinematics, SE(2), Helmholtz-Hodge, Wavelet) take ~112-122s on CPU
            # (measured on Linux VM: upload 1.11s + backend 122.14s + UI render 0.625s = 124.05s total).
            page.wait_for_selector('#ouroboros-proposals .ob-card[data-profile="quality"]', timeout=240000)
            page.wait_for_selector('#ouroboros-proposals .ob-card[data-profile="balance"]', timeout=15000)
            page.wait_for_selector('#ouroboros-proposals .ob-card[data-profile="flexibility"]', timeout=15000)

            # Confirm 3 distinct cards & diagnostic body populated from same run
            cards = page.locator("#ouroboros-proposals .ob-card")
            assert cards.count() == 3
            page.wait_for_selector("#anim-diag-body:not([hidden])", timeout=10000)

            # Confirm no horizontal overflow & touch target sizes >= 44px on landscape 1480x924
            geom_land = page.evaluate(
                """() => {
                    const overflow = document.documentElement.scrollWidth - window.innerWidth;
                    const btns = [...document.querySelectorAll('#ouroboros-panel button:not([hidden])')];
                    const minH = Math.min(...btns.map(b => b.getBoundingClientRect().height));
                    return { overflow, minH, count: btns.length };
                }"""
            )
            assert geom_land["overflow"] <= 1, f"Landscape overflow: {geom_land}"
            assert geom_land["minH"] >= 43.5, f"Touch buttons too small: {geom_land}"

            # Exercise 10-cycle toggle, seam inspection, and fullscreen modal on balance card
            bal_card = page.locator('#ouroboros-proposals .ob-card[data-profile="balance"]')
            bal_card.locator(".ob-btn-10c").click()
            assert bal_card.locator(".ob-btn-10c").get_attribute("aria-pressed") == "true"
            bal_card.locator(".ob-btn-seam").click()
            assert bal_card.locator(".ob-btn-seam").get_attribute("aria-pressed") == "true"
            bal_card.locator(".ob-btn-fs").click()
            page.wait_for_selector("#ouroboros-fs-dialog[open]", timeout=5000)
            page.click('#ouroboros-fs-dialog button[type="submit"]')
            page.wait_for_timeout(150)

            # Check portrait 924x1480 has zero horizontal overflow too
            page.set_viewport_size({"width": 924, "height": 1480})
            page.wait_for_timeout(150)
            overflow_port = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            assert overflow_port <= 1, f"Portrait overflow: {overflow_port}"

            # Back to landscape & click "APLICAR ESTE LOOP AO MEU TEMA" on Proposal 2 (balance)
            page.set_viewport_size({"width": 1480, "height": 924})
            bal_card.locator(".ob-btn-apply").click()
            page.wait_for_selector('#ouroboros-proposals .ob-card[data-profile="balance"].is-applied', timeout=30000)
            assert page.locator("#anim-size").input_value() == "960x540"
            assert page.locator("#project-status").inner_text().strip() == "Salvo"

            # Reload project URL and verify 960x540 animated loop persisted cleanly!
            page.reload(wait_until="networkidle")
            page.wait_for_selector("main:not([inert])", timeout=15000)
            assert page.locator("#bgmode-animated").is_checked()
            assert page.locator("#anim-size").input_value() == "960x540"

            assert not page_errors, f"Page errors: {page_errors}"
            assert not console_errors, f"Console errors: {console_errors}"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
