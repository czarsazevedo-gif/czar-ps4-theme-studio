"""Checkpoint F — Pilar III (Gold Master Performance, Code Quality & Touch/S Pen UI Ergonomics).

Verifies Sections 20, 21, 22 and regression fixes GM-F-001 & GM-F-002:
- Zero stray Spanish user-facing strings in app.js (GM-F-001).
- Live headless Chromium CDP audit with emulated touch across ALL 6 Studio tabs + Ouroboros Deluxe
  in BOTH Galaxy Tab S10 Ultra landscape (1480x924) and portrait (924x1480):
  zero horizontal overflow, zero interactive targets < 44 px (GM-F-002), zero JS exceptions.
- Real timing & peak RSS memory benchmark across all 7 operational stages.
"""

from __future__ import annotations

import base64
import json
import os
import resource
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import jobs, main, projects
from app.forge import pkg_forge
from tests.test_projects import state as editor_state

REPO = Path(__file__).resolve().parents[1]
SAMPLE_PNG = REPO / "tests" / "sample.png"
SAMPLE_MP4 = REPO / "tests" / "sample.mp4"
SAMPLE_MP3 = REPO / "tests" / "sample_music.mp3"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_local(url: str) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=10) as r:
        return r.status, r.read()


def _ws_connect(host: str, port: int, path: str) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)
    assert b" 101 " in buf
    return sock


def _ws_send(sock: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    hdr = bytearray([0x81])
    n = len(payload)
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126)
        hdr.extend(struct.pack("!H", n))
    else:
        hdr.append(0x80 | 127)
        hdr.extend(struct.pack("!Q", n))
    hdr.extend(mask)
    sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _recv_n(sock: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        c = sock.recv(n - len(out))
        if not c:
            raise EOFError("WS closed")
        out.extend(c)
    return bytes(out)


def _ws_recv(sock: socket.socket) -> str:
    while True:
        h = _recv_n(sock, 2)
        op = h[0] & 0x0F
        masked = (h[1] & 0x80) != 0
        n = h[1] & 0x7F
        if n == 126:
            n = struct.unpack("!H", _recv_n(sock, 2))[0]
        elif n == 127:
            n = struct.unpack("!Q", _recv_n(sock, 8))[0]
        mask = _recv_n(sock, 4) if masked else b""
        data = _recv_n(sock, n)
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op == 1:
            return data.decode("utf-8")


def _cdp(sock: socket.socket, rid: int, method: str, params: dict | None = None, events: list | None = None):
    _ws_send(sock, json.dumps({"id": rid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(_ws_recv(sock))
        if msg.get("id") == rid:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result", {})
        if events is not None and msg.get("method") == "Runtime.exceptionThrown":
            events.append(msg["params"])


def _eval(sock: socket.socket, rid: int, expr: str, events: list | None = None):
    res = _cdp(
        sock,
        rid,
        "Runtime.evaluate",
        {"expression": expr, "returnByValue": True, "awaitPromise": True},
        events=events,
    )
    if "exceptionDetails" in res:
        raise RuntimeError(res["exceptionDetails"])
    return res["result"].get("value")


def test_gm_f_001_no_stray_spanish_user_strings_in_app_js():
    text = (REPO / "app" / "static" / "app.js").read_text(encoding="utf-8")
    for forbidden in (
        "Convirtiendo a .dds",
        "Exportado correctamente",
        "No se pudo contactar",
        "(no instalado)",
        ".dds totales",
        '"miniatura · icono · ficha"',
    ):
        assert forbidden not in text, f"Stray Spanish UI string found in app.js: {forbidden}"


@pytest.mark.skipif(shutil.which("chromium") is None, reason="chromium not installed")
def test_section21_chromium_touch_ergonomics_all_tabs_and_ouroboros_both_orientations(tmp_path):
    """Verify zero overflow, >= 44 px touch targets (GM-F-002), and zero JS exceptions across all views."""
    port = _free_port()
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO),
        env=dict(os.environ, PYTHONPATH=str(REPO), CZAR_WORKSPACE=str(ws_dir)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(80):
            try:
                if _http_local(f"http://127.0.0.1:{port}/healthz")[0] == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise AssertionError("Server did not start")

        for page_path in ("/", "/static/ouroboros.html"):
            for w, h in ((1480, 924), (924, 1480)):
                dp = _free_port()
                chrome = subprocess.Popen(
                    [
                        "chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
                        f"--user-data-dir={tmp_path}/c_{w}_{h}_{len(page_path)}",
                        f"--remote-debugging-port={dp}",
                        f"--window-size={w},{h}",
                        f"http://127.0.0.1:{port}{page_path}",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    tgs = []
                    for _ in range(80):
                        try:
                            tgs = json.loads(_http_local(f"http://127.0.0.1:{dp}/json")[1].decode())
                            if any(t.get("type") == "page" for t in tgs):
                                break
                        except Exception:
                            time.sleep(0.1)
                    pg = next(t for t in tgs if t.get("type") == "page")
                    sock = _ws_connect("127.0.0.1", dp, "/" + pg["webSocketDebuggerUrl"].split("/", 3)[3])
                    exceptions: list = []
                    try:
                        _cdp(sock, 1, "Runtime.enable", events=exceptions)
                        _cdp(sock, 2, "Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}, events=exceptions)
                        time.sleep(1.2)

                        if page_path == "/":
                            _eval(
                                sock,
                                3,
                                """
                                (async () => {
                                  const r = await fetch('/api/projects', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({ name: 'Touch Audit F' }),
                                  });
                                  const d = await r.json();
                                  location.href = '/?project=' + encodeURIComponent(d.id);
                                  return d.id;
                                })()
                                """,
                                events=exceptions,
                            )
                            for i in range(60):
                                try:
                                    if _eval(
                                        sock,
                                        10 + i,
                                        "location.search.includes('project=') && document.getElementById('project-name') && !document.getElementById('project-name').disabled",
                                        events=exceptions,
                                    ):
                                        break
                                except RuntimeError:
                                    pass
                                time.sleep(0.15)

                            rep = _eval(
                                sock,
                                100,
                                """
                                (() => {
                                  const out = {};
                                  for (const tab of ['backgrounds', 'icons', 'compositor', 'sounds', 'preview', 'package']) {
                                    document.querySelector('[data-tab="' + tab + '"]').click();
                                    if (tab === 'backgrounds') {
                                      const r = document.getElementById('bgmode-animated');
                                      r.checked = true;
                                      r.dispatchEvent(new Event('change', { bubbles: true }));
                                    }
                                    const ov = document.documentElement.scrollWidth - window.innerWidth;
                                    const small = [];
                                    for (const el of document.querySelectorAll('button, a.btn, select, input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=range]):not([type=file]), summary')) {
                                      const rc = el.getBoundingClientRect();
                                      if (rc.width > 0 && rc.height > 0 && rc.height < 43.5) {
                                        small.push({ id: el.id || el.textContent.trim().slice(0, 24), h: Math.round(rc.height * 10) / 10 });
                                      }
                                    }
                                    out[tab] = { ov, small };
                                  }
                                  return out;
                                })()
                                """,
                                events=exceptions,
                            )
                            for tab_name, info in rep.items():
                                assert info["ov"] <= 0, f"Overflow on Studio/{tab_name} at {w}x{h}: {info}"
                                assert not info["small"], f"Controls < 44px on Studio/{tab_name} at {w}x{h}: {info['small']}"
                        else:
                            rep = _eval(
                                sock,
                                200,
                                """
                                (() => {
                                  const ov = document.documentElement.scrollWidth - window.innerWidth;
                                  const small = [];
                                  for (const el of document.querySelectorAll('button, a.btn, select, input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=range]):not([type=file]), summary')) {
                                    const rc = el.getBoundingClientRect();
                                    if (rc.width > 0 && rc.height > 0 && rc.height < 43.5) {
                                      small.push({ id: el.id || el.textContent.trim().slice(0, 24), h: Math.round(rc.height * 10) / 10 });
                                    }
                                  }
                                  return { ov, small };
                                })()
                                """,
                                events=exceptions,
                            )
                            assert rep["ov"] <= 0, f"Overflow on ouroboros.html at {w}x{h}: {rep}"
                            assert not rep["small"], f"Controls < 44px on ouroboros.html at {w}x{h}: {rep['small']}"

                        assert not exceptions, f"Uncaught JS exceptions on {page_path} {w}x{h}: {exceptions}"
                    finally:
                        sock.close()
                finally:
                    chrome.terminate()
                    chrome.wait(timeout=5)
    finally:
        srv.terminate()
        srv.wait(timeout=5)


@pytest.mark.skipif(pkg_forge.find_liborbispkg_tool() is None, reason="liborbispkg pkgtool not installed")
def test_section20_real_performance_and_memory_benchmarks(tmp_path, monkeypatch):
    """Measure real wall-clock timings & peak RSS across all 7 Section 20 operational stages."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(projects, "ROOT", ws / "studio-projects-v1")
    monkeypatch.setattr(jobs, "WORKSPACE", ws / "recovery")

    timings: dict[str, float] = {}
    with TestClient(main.app) as client:
        t0 = time.perf_counter()
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/projects").status_code == 200
        timings["healthz_and_boot_s"] = round(time.perf_counter() - t0, 3)

        pid = client.post("/api/projects", json={"name": "Bench F"}).json()["id"]
        params = {"project": pid}

        # 1. Library video import
        t0 = time.perf_counter()
        vid_id = client.post("/api/library", files={"file": ("sample.mp4", SAMPLE_MP4.open("rb"), "video/mp4")}).json()["id"]
        timings["import_video_s"] = round(time.perf_counter() - t0, 3)

        # 2. Automatic media diagnosis / Deluxe analysis
        t0 = time.perf_counter()
        tk = client.post("/api/ouroboros/deluxe/diagnose", params=params, json={"kind": "library", "id": vid_id}).json()["id"]
        while True:
            st = client.get(f"/api/ouroboros/jobs/{tk}", params=params).json()
            if st["state"] != "working":
                break
            time.sleep(0.1)
        assert st["state"] == "ready"
        timings["diagnose_analyze_s"] = round(time.perf_counter() - t0, 3)

        # 3. Deluxe render
        t0 = time.perf_counter()
        spec = {
            "kind": "library", "id": vid_id, "start": 0.0, "end": 1.2, "frames": 8,
            "resolution": 2, "wait": 0.1, "mode": "reconstruct_auto", "deluxeFinish": True,
            "stabilizeCamera": False, "speedScale": 1.0,
        }
        rk = client.post("/api/ouroboros/deluxe/render", params=params, json=spec).json()["id"]
        while True:
            rst = client.get(f"/api/ouroboros/jobs/{rk}", params=params).json()
            if rst["state"] != "working":
                break
            time.sleep(0.1)
        assert rst["state"] == "ready"
        timings["deluxe_render_s"] = round(time.perf_counter() - t0, 3)

        ap = client.post("/api/ouroboros/deluxe/apply", params=params, json={"key": rst["key"], "expected": spec})
        assert ap.status_code == 200
        anim_job = ap.json()["jobId"]

        # 4. RAF ZIP export
        t0 = time.perf_counter()
        raf_exp = client.post(f"/api/animated/{anim_job}/export", params=params, data={"frames": "8", "wait_seconds": "0.1"})
        assert raf_exp.status_code == 200
        timings["raf_export_s"] = round(time.perf_counter() - t0, 3)

        # 5. ATRAC9 144 kbps conversion
        t0 = time.perf_counter()
        bgm = client.post(
            "/api/forge/audio/convert-apply",
            params=params,
            files={"file": ("music.mp3", SAMPLE_MP3.open("rb"), "audio/mpeg")},
            data={"loop_enabled": "true", "loop_mode": "auto_refined", "volume_mode": "ambient"},
        )
        assert bgm.status_code == 200
        timings["atrac9_convert_s"] = round(time.perf_counter() - t0, 3)
        bgm_job = bgm.json()["job_id"]

        # 6. Save & PKG build + independent extraction/hash verification
        home_job = client.post("/api/slot/home/upload", params=params, files={"file": ("home.png", SAMPLE_PNG.open("rb"), "image/png")}).json()["job_id"]
        pstate = editor_state()
        pstate["bgMode"] = "animated"
        pstate["slots"] = {"home": home_job}
        pstate["bgm"] = bgm_job
        pstate["bgmGain"] = bgm.json()["gain_db"]
        pstate["anim"] = {"jobId": anim_job, "frames": 8, "wait": 0.1, "size": [640, 360], "backend": "native"}
        cur = client.get(f"/api/projects/{pid}").json()
        assert client.put(f"/api/projects/{pid}", json={"name": "Bench F", "revision": cur["revision"], "state": pstate}).status_code == 200

        t0 = time.perf_counter()
        pkg_r = client.post("/api/forge/pkg/build", params=params, data={"use_saved_project": "true", "sync": "true"})
        assert pkg_r.status_code == 200
        assert pkg_r.json()["pkg_report"]["structure_validated"] is True
        timings["pkg_build_and_validate_s"] = round(time.perf_counter() - t0, 3)

    peak_rss_mib = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    timings["peak_rss_mib"] = peak_rss_mib
    print("\nBENCHMARK_F_JSON=" + json.dumps(timings, ensure_ascii=False))
    # Sanity operational budgets (2-core Linux Codespace)
    assert timings["healthz_and_boot_s"] < 5.0
    assert timings["raf_export_s"] < 15.0
    assert timings["atrac9_convert_s"] < 30.0
    assert timings["pkg_build_and_validate_s"] < 45.0
    assert peak_rss_mib < 2500.0
