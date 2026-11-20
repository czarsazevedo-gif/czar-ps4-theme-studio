"""Checkpoint B — Pilar II (O Guia do Loop Perfeito & Diagnóstico Automático de Mídia).

Verifies:
1. All 21 required fields & 5 official aptitude categories in `/api/ouroboros/deluxe/diagnose`.
2. Zero-motion / static clips are strictly classified as `INADEQUADO AO RESULTADO DESEJADO`.
3. Clean periodic loops vs moving-camera footage receive honest, evidence-backed verdicts.
4. Shared content-addressed cache between `/diagnose` and `/analyze` (same key, zero duplicate compute).
5. Non-blocking job cancellation via `/api/ouroboros/jobs/{id}/cancel`.
6. Live headless Chromium verification (1480x924 & 924x1480 with emulated touch) of the Guide
   ("COMO ESCOLHER UM VÍDEO PARA UM LOOP PERFEITO") and live post-import automatic diagnosis UI.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest

from app import jobs, projects
from app.deluxe.media_diagnostic import (
    APT_CONDITIONAL,
    APT_CORRECTABLE,
    APT_NATURAL,
    APT_SYNTHESIS,
    APT_UNSUITABLE,
)
from app.main import app

OFFICIAL_APTITUDES = {
    APT_NATURAL,
    APT_CORRECTABLE,
    APT_SYNTHESIS,
    APT_CONDITIONAL,
    APT_UNSUITABLE,
}

REQUIRED_21_FIELDS = (
    "material",
    "duracao",
    "resolucao",
    "fps",
    "qualidade",
    "movimento",
    "estabilidade",
    "periodicidade",
    "aptidao",
    "pontos_fortes",
    "pontos_fracos",
    "o_que_falta",
    "o_que_pode_ser_corrigido",
    "o_que_nao_pode_ser_garantido",
    "melhor_trecho",
    "melhor_resolucao",
    "melhor_cadencia",
    "melhor_estrategia",
    "qualidade_esperada",
    "confianca",
    "proximo_passo",
)

REQUIRED_UI_LABELS = (
    "MATERIAL",
    "DURAÇÃO",
    "RESOLUÇÃO",
    "FPS",
    "QUALIDADE",
    "MOVIMENTO",
    "ESTABILIDADE",
    "PERIODICIDADE",
    "APTIDÃO",
    "PONTOS FORTES",
    "PONTOS FRACOS",
    "O QUE FALTA",
    "O QUE PODE SER CORRIGIDO",
    "O QUE NÃO PODE SER GARANTIDO",
    "MELHOR TRECHO",
    "MELHOR RESOLUÇÃO",
    "MELHOR CADÊNCIA",
    "MELHOR ESTRATÉGIA",
    "QUALIDADE ESPERADA",
    "CONFIANÇA",
    "PRÓXIMO PASSO",
)

REPO = Path(__file__).resolve().parents[1]
REAL_RIVER = Path("/tmp/czar-real-corpus/dev_river.webm")
SAMPLE_MP4 = REPO / "tests" / "sample.mp4"


def _make_mp4(path: Path, frames_bgr: list[np.ndarray], fps: float = 20.0) -> Path:
    h, w = frames_bgr[0].shape[:2]
    raw = path.with_suffix(".raw.avi")
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    writer = cv2.VideoWriter(str(raw), fourcc, fps, (w, h))
    for fr in frames_bgr:
        writer.write(fr)
    writer.release()
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(raw),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
            "-movflags", "+faststart", str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    raw.unlink(missing_ok=True)
    return path


def _synth_static_mp4(path: Path, n_frames: int = 40, fps: float = 20.0) -> Path:
    rng = np.random.default_rng(7)
    base = np.zeros((180, 320, 3), dtype=np.uint8)
    base[:, :, 0] = np.linspace(30, 120, 320, dtype=np.uint8)[None, :]
    base[:, :, 1] = np.linspace(40, 160, 180, dtype=np.uint8)[:, None]
    base[:, :, 2] = 75
    cv2.rectangle(base, (40, 40), (140, 130), (200, 180, 100), -1)
    cv2.circle(base, (230, 95), 35, (90, 210, 190), -1)
    # Identical frame repeated: strictly zero motion
    return _make_mp4(path, [base.copy() for _ in range(n_frames)], fps=fps)


def _synth_periodic_wave_mp4(path: Path, n_frames: int = 60, fps: float = 20.0) -> Path:
    """Synthetic seamless periodic river-band flow (exact period = 20 frames = 1.0 s)."""
    h, w = 180, 320
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    frames = []
    period = 20
    for i in range(n_frames):
        phase = 2.0 * np.pi * (i % period) / period
        # Smooth continuous horizontal scroll with rich texture
        val = (
            128.0
            + 55.0 * np.sin(2.0 * np.pi * xx / 64.0 - phase)
            + 30.0 * np.cos(2.0 * np.pi * (xx / 32.0 + yy / 48.0) - phase)
            + 18.0 * np.sin(2.0 * np.pi * xx / 16.0 - 2.0 * phase)
        )
        b = np.clip(val + 30, 0, 255).astype(np.uint8)
        g = np.clip(val * 0.85, 0, 255).astype(np.uint8)
        r = np.clip(val * 0.45, 0, 255).astype(np.uint8)
        fr = np.stack([b, g, r], axis=2)
        # Static top bank so camera is unambiguously fixed
        fr[:48, :, :] = (40, 55, 45)
        cv2.rectangle(fr, (18, 10), (92, 36), (160, 170, 180), -1)
        cv2.rectangle(fr, (210, 8), (295, 38), (120, 140, 150), -1)
        frames.append(fr)
    return _make_mp4(path, frames, fps=fps)


def _synth_camera_pan_mp4(path: Path, n_frames: int = 50, fps: float = 20.0) -> Path:
    """Synthetic continuous horizontal camera pan across a non-repeating textured canvas."""
    h, w = 180, 320
    rng = np.random.default_rng(123)
    big_w = w + n_frames * 6 + 40
    canvas = rng.integers(30, 220, size=(h, big_w, 3), dtype=np.uint8)
    canvas = cv2.GaussianBlur(canvas, (5, 5), 1.2)
    for x in range(20, big_w - 20, 45):
        color = tuple(int(c) for c in rng.integers(50, 240, size=3))
        cv2.rectangle(canvas, (x, 30), (x + 24, 150), color, -1)
        cv2.circle(canvas, (x + 12, 90), 9, (255, 255, 255), -1)
    frames = [canvas[:, i * 6 : i * 6 + w].copy() for i in range(n_frames)]
    return _make_mp4(path, frames, fps=fps)


def _poll_task(client: httpx.Client, pid: str, task_id: str, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/ouroboros/jobs/{task_id}", params={"project": pid})
        assert r.status_code == 200, r.text
        st = r.json()
        if st["state"] != "working":
            return st
        time.sleep(0.15)
    raise AssertionError(f"Task {task_id} timed out")


def _assert_valid_21_fields(diag: dict) -> None:
    assert diag["aptidao"] in OFFICIAL_APTITUDES, diag["aptidao"]
    assert isinstance(diag["resumo"], str) and len(diag["resumo"]) > 10
    fields = diag["fields"]
    for key in REQUIRED_21_FIELDS:
        assert key in fields, f"Missing required field: {key}"
        val = fields[key]
        if isinstance(val, list):
            assert len(val) >= 1 and all(isinstance(x, str) and x.strip() for x in val), f"Empty list field: {key}"
        else:
            assert isinstance(val, str) and val.strip(), f"Empty string field: {key}"


@pytest.fixture
def isolated_client(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(projects, "ROOT", ws / "studio-projects-v1")
    monkeypatch.setattr(jobs, "WORKSPACE", ws / "recovery")
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/api/projects").status_code == 200
        yield client


def test_diagnose_21_fields_and_cache_sharing_on_real_video(isolated_client):
    client = isolated_client
    proj = client.post("/api/projects", json={"name": "Diag Real"}).json()
    pid = proj["id"]

    video_path = REAL_RIVER if REAL_RIVER.is_file() else SAMPLE_MP4
    with video_path.open("rb") as fh:
        up = client.post(
            "/api/animated/upload",
            params={"project": pid},
            files={"file": (video_path.name, fh, "video/webm" if video_path.suffix == ".webm" else "video/mp4")},
            data={"frames": "10", "size": "640x360"},
        )
    assert up.status_code == 200, up.text
    job_id = up.json()["job_id"]

    t_diag = client.post(
        "/api/ouroboros/deluxe/diagnose",
        params={"project": pid},
        json={"kind": "animated", "id": job_id},
    )
    assert t_diag.status_code == 202, t_diag.text
    st_diag = _poll_task(client, pid, t_diag.json()["id"], timeout=120.0)
    assert st_diag["state"] == "ready", st_diag
    key_diag = st_diag["key"]

    rep = client.get(f"/api/ouroboros/result/{key_diag}", params={"project": pid}).json()
    assert "mediaDiagnostic" in rep
    _assert_valid_21_fields(rep["mediaDiagnostic"])
    assert rep["mediaDiagnostic"]["aptidao"] != APT_UNSUITABLE or video_path == SAMPLE_MP4

    # Calling /analyze right afterwards MUST hit the exact same content-addressed cache key!
    t0 = time.monotonic()
    t_ana = client.post(
        "/api/ouroboros/deluxe/analyze",
        params={"project": pid},
        json={"kind": "animated", "id": job_id},
    )
    assert t_ana.status_code == 202, t_ana.text
    st_ana = _poll_task(client, pid, t_ana.json()["id"], timeout=30.0)
    elapsed_cached = time.monotonic() - t0
    assert st_ana["state"] == "ready"
    assert st_ana["key"] == key_diag, "Diagnose and Deluxe Analyze must share the exact same cache key"
    assert elapsed_cached < 3.0, f"Expected cached reuse under 3s, took {elapsed_cached:.2f}s"


def test_diagnose_static_rejected_and_periodic_vs_pan_classification(isolated_client, tmp_path):
    client = isolated_client
    proj = client.post("/api/projects", json={"name": "Diag Classification"}).json()
    pid = proj["id"]

    def upload_and_diagnose(mp4: Path) -> dict:
        with mp4.open("rb") as fh:
            up = client.post(
                "/api/animated/upload",
                params={"project": pid},
                files={"file": (mp4.name, fh, "video/mp4")},
                data={"frames": "10", "size": "640x360"},
            )
        assert up.status_code == 200, up.text
        jid = up.json()["job_id"]
        tk = client.post(
            "/api/ouroboros/deluxe/diagnose",
            params={"project": pid},
            json={"kind": "animated", "id": jid},
        )
        assert tk.status_code == 202, tk.text
        st = _poll_task(client, pid, tk.json()["id"], timeout=90.0)
        assert st["state"] == "ready", st
        rep = client.get(f"/api/ouroboros/result/{st['key']}", params={"project": pid}).json()
        _assert_valid_21_fields(rep["mediaDiagnostic"])
        return rep["mediaDiagnostic"]

    # 1. Static video -> STRICTLY INADEQUADO (zero motion is NEVER a good loop)
    static_diag = upload_and_diagnose(_synth_static_mp4(tmp_path / "static_clip.mp4"))
    assert static_diag["aptidao"] == APT_UNSUITABLE, static_diag
    assert "estátic" in static_diag["resumo"].lower() or "desprezível" in static_diag["resumo"].lower()

    # 2. Periodic continuous flow with fixed camera -> favorable / synthesis / correctable (never unsuitable)
    periodic_diag = upload_and_diagnose(_synth_periodic_wave_mp4(tmp_path / "periodic_wave.mp4"))
    assert periodic_diag["aptidao"] in {APT_NATURAL, APT_SYNTHESIS, APT_CORRECTABLE}, periodic_diag

    # 3. Continuous camera pan -> must detect moving camera and NEVER claim FAVORÁVEL A LOOP NATURAL
    pan_diag = upload_and_diagnose(_synth_camera_pan_mp4(tmp_path / "camera_pan.mp4"))
    assert pan_diag["aptidao"] != APT_NATURAL, pan_diag
    assert "câmera" in " ".join(pan_diag["fields"]["pontos_fracos"]).lower()


def test_diagnose_cancellation_is_non_blocking_and_clean(isolated_client):
    client = isolated_client
    proj = client.post("/api/projects", json={"name": "Diag Cancel"}).json()
    pid = proj["id"]
    video_path = REAL_RIVER if REAL_RIVER.is_file() else SAMPLE_MP4
    with video_path.open("rb") as fh:
        up = client.post(
            "/api/animated/upload",
            params={"project": pid},
            files={"file": (video_path.name, fh, "video/webm" if video_path.suffix == ".webm" else "video/mp4")},
            data={"frames": "8", "size": "640x360"},
        )
    assert up.status_code == 200
    jid = up.json()["job_id"]

    tk = client.post(
        "/api/ouroboros/deluxe/diagnose",
        params={"project": pid},
        json={"kind": "animated", "id": jid},
    )
    assert tk.status_code == 202
    tid = tk.json()["id"]
    cancel_r = client.post(f"/api/ouroboros/jobs/{tid}/cancel", params={"project": pid})
    assert cancel_r.status_code == 200
    st = _poll_task(client, pid, tid, timeout=30.0)
    assert st["state"] in {"cancelled", "ready"}

    # Even after cancelling diagnosis, animated export remains completely unblocked!
    exp = client.post(
        f"/api/animated/{jid}/export",
        params={"project": pid},
        data={"frames": "8", "wait_seconds": "0.1"},
    )
    assert exp.status_code == 200, exp.text


# ---------- Live headless Chromium CDP verification ----------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_local(url: str, data: bytes | None = None, headers: dict | None = None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with opener.open(req, timeout=10) as r:
        return r.status, r.read()


def _ws_connect(host: str, port: int, path: str) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)
    assert b" 101 " in buf
    return sock


def _ws_send(sock: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", n))
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise EOFError("WebSocket closed")
        out.extend(chunk)
    return bytes(out)


def _ws_recv(sock: socket.socket) -> str:
    while True:
        h = _recv_exact(sock, 2)
        op = h[0] & 0x0F
        masked = (h[1] & 0x80) != 0
        n = h[1] & 0x7F
        if n == 126:
            n = struct.unpack("!H", _recv_exact(sock, 2))[0]
        elif n == 127:
            n = struct.unpack("!Q", _recv_exact(sock, 8))[0]
        mask = _recv_exact(sock, 4) if masked else b""
        data = _recv_exact(sock, n)
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op == 1:
            return data.decode("utf-8")
        if op == 8:
            raise EOFError("WS close")


def _cdp_call(sock: socket.socket, req_id: int, method: str, params: dict | None = None):
    _ws_send(sock, json.dumps({"id": req_id, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(_ws_recv(sock))
        if msg.get("id") == req_id:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result", {})


def _cdp_eval(sock: socket.socket, req_id: int, expr: str):
    res = _cdp_call(
        sock,
        req_id,
        "Runtime.evaluate",
        {"expression": expr, "returnByValue": True, "awaitPromise": True},
    )
    if "exceptionDetails" in res:
        raise RuntimeError(res["exceptionDetails"])
    return res["result"].get("value")


@pytest.mark.skipif(shutil.which("chromium") is None, reason="chromium not installed")
def test_chromium_guide_and_live_auto_diagnostic_touch_viewports(tmp_path):
    """Verify Guide + automatic 21-field post-import diagnostic in real headless Chromium."""
    app_port = _free_port()
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    clip = _synth_periodic_wave_mp4(tmp_path / "ui_wave.mp4", n_frames=40, fps=20.0)

    env = dict(os.environ, PYTHONPATH=str(REPO), CZAR_WORKSPACE=str(ws_dir))
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(app_port)],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(80):
            try:
                status, _ = _http_local(f"http://127.0.0.1:{app_port}/healthz")
                if status == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise AssertionError("Uvicorn did not start")

        for idx, (w, h) in enumerate(((1480, 924), (924, 1480))):
            dbg_port = _free_port()
            user_dir = tmp_path / f"chrome_{w}x{h}"
            chrome = subprocess.Popen(
                [
                    "chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
                    f"--user-data-dir={user_dir}",
                    f"--remote-debugging-port={dbg_port}",
                    f"--window-size={w},{h}",
                    f"http://127.0.0.1:{app_port}/",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                targets = []
                for _ in range(80):
                    try:
                        _, raw = _http_local(f"http://127.0.0.1:{dbg_port}/json")
                        targets = json.loads(raw.decode())
                        if any(t.get("type") == "page" for t in targets):
                            break
                    except Exception:
                        time.sleep(0.1)
                page = next(t for t in targets if t.get("type") == "page")
                ws_path = "/" + page["webSocketDebuggerUrl"].split("/", 3)[3]
                sock = _ws_connect("127.0.0.1", dbg_port, ws_path)
                try:
                    _cdp_call(sock, 1, "Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
                    _cdp_call(sock, 2, "DOM.enable")
                    time.sleep(1.2)

                    # Create a project inside the browser's own session cookie jar and navigate to it
                    pid = _cdp_eval(
                        sock,
                        3,
                        """
                        (async () => {
                          const r = await fetch('/api/projects', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ name: 'Chromium Diag B' }),
                          });
                          if (!r.ok) throw new Error('HTTP ' + r.status);
                          const d = await r.json();
                          location.href = '/?project=' + encodeURIComponent(d.id);
                          return d.id;
                        })()
                        """,
                    )
                    assert pid
                    # Wait for full app boot + Projects.boot() completion (#project-name only enables after boot)
                    ready = False
                    for wait_i in range(60):
                        try:
                            ready = bool(
                                _cdp_eval(
                                    sock,
                                    100 + wait_i,
                                    "location.search.includes('project=') && document.getElementById('project-name') && !document.getElementById('project-name').disabled && !document.querySelector('main')?.inert",
                                )
                            )
                        except RuntimeError:
                            ready = False
                        if ready:
                            break
                        time.sleep(0.15)
                    assert ready, "Project page did not finish booting"

                    # Switch to Animated Background, open Guide, verify layout & touch targets
                    guide_check = _cdp_eval(
                        sock,
                        200,
                        """
                        (() => {
                          const r = document.getElementById('bgmode-animated');
                          r.checked = true;
                          r.dispatchEvent(new Event('change', { bubbles: true }));
                          const guide = document.getElementById('loop-guide');
                          const summary = guide.querySelector('summary');
                          const summaryRect = summary.getBoundingClientRect();
                          summary.click();
                          const overflow = document.documentElement.scrollWidth - window.innerWidth;
                          return {
                            title: summary.textContent.trim(),
                            open: guide.open,
                            summaryHeight: Math.round(summaryRect.height * 10) / 10,
                            cards: guide.querySelectorAll('.loop-guide-card').length,
                            overflow,
                          };
                        })()
                        """,
                    )
                    assert guide_check["title"] == "COMO ESCOLHER UM VÍDEO PARA UM LOOP PERFEITO"
                    assert guide_check["open"] is True
                    assert guide_check["summaryHeight"] >= 44.0, guide_check
                    assert guide_check["cards"] >= 8, guide_check
                    assert guide_check["overflow"] <= 0, f"Horizontal overflow in {w}x{h}: {guide_check}"

                    # On landscape pass, upload clip via the REAL #anim-file input and verify automatic 21-field UI!
                    if idx == 0:
                        doc = _cdp_call(sock, 201, "DOM.getDocument", {"depth": -1})
                        inp = _cdp_call(
                            sock,
                            202,
                            "DOM.querySelector",
                            {"nodeId": doc["root"]["nodeId"], "selector": "#anim-file"},
                        )
                        _cdp_call(
                            sock,
                            203,
                            "DOM.setFileInputFiles",
                            {"nodeId": inp["nodeId"], "files": [str(clip)]},
                        )
                        diag_ui = None
                        for poll_i in range(180):
                            diag_ui = _cdp_eval(
                                sock,
                                300 + poll_i,
                                """
                                (() => {
                                  const sec = document.getElementById('anim-diagnostic');
                                  const body = document.getElementById('anim-diag-body');
                                  const status = document.getElementById('anim-diag-status')?.textContent || '';
                                  const dts = [...document.querySelectorAll('#anim-diag-body dt')].map(el => el.textContent.trim());
                                  const h4s = [...document.querySelectorAll('#anim-diag-body h4')].map(el => el.textContent.trim());
                                  const badge = document.querySelector('#anim-diag-body .anim-diag-badge')?.textContent?.trim() || '';
                                  const link = document.getElementById('anim-diag-ouroboros')?.getAttribute('href') || '';
                                  const playDisabled = document.getElementById('anim-play')?.disabled;
                                  const overflow = document.documentElement.scrollWidth - window.innerWidth;
                                  return {
                                    secHidden: sec.hidden,
                                    bodyHidden: body.hidden,
                                    status,
                                    labels: [...dts, ...h4s],
                                    badge,
                                    link,
                                    playDisabled,
                                    overflow,
                                  };
                                })()
                                """,
                            )
                            if not diag_ui["bodyHidden"] and diag_ui["badge"]:
                                break
                            time.sleep(0.35)
                        assert diag_ui and not diag_ui["bodyHidden"], diag_ui
                        assert diag_ui["badge"] in OFFICIAL_APTITUDES, diag_ui
                        assert diag_ui["playDisabled"] is False
                        assert diag_ui["overflow"] <= 0, diag_ui
                        assert f"project={pid}" in diag_ui["link"]
                        for lbl in REQUIRED_UI_LABELS:
                            assert lbl in diag_ui["labels"], f"Missing UI label {lbl}: {diag_ui['labels']}"
                finally:
                    sock.close()
            finally:
                chrome.terminate()
                try:
                    chrome.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    chrome.kill()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
