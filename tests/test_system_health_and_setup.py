"""Tests for 3-state (green/gray/red) live tool health lights, one-tap setup API, and Codespaces provisioning."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app import at9, dds, main, media, system_health
from app.forge import pkg_forge

REPO = Path(__file__).resolve().parents[1]


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


def _cdp(sock: socket.socket, rid: int, method: str, params: dict | None = None):
    _ws_send(sock, json.dumps({"id": rid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(_ws_recv(sock))
        if msg.get("id") == rid:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result", {})


def _eval(sock: socket.socket, rid: int, expr: str):
    res = _cdp(sock, rid, "Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    if "exceptionDetails" in res:
        raise RuntimeError(res["exceptionDetails"])
    return res["result"].get("value")


def test_scripts_and_devcontainer_provisioning_integrity():
    for rel in ("start.sh", "scripts/install_forge_tools.sh", "scripts/setup_codespace.sh"):
        p = REPO / rel
        assert p.is_file(), f"Missing {rel}"
        res = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
        assert res.returncode == 0, f"Syntax error in {rel}: {res.stderr}"

    install_txt = (REPO / "scripts" / "install_forge_tools.sh").read_text(encoding="utf-8")
    assert 'TOOLS_DIR="${ROOT_DIR}/tools"' in install_txt
    assert "install_magick" in install_txt
    assert "/etc/apt/sources.list.d/yarn.list" in install_txt

    dev = json.loads((REPO / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8"))
    assert "setup_codespace.sh" in dev["postCreateCommand"]
    assert "setup_codespace.sh" in dev["postStartCommand"]


def test_three_state_health_ok_off_and_defective_err(tmp_path):
    """Verify real 3-state transitions: ok (verde), off (cinza), and err (vermelho)."""
    system_health.invalidate_health_cache()
    client = TestClient(main.app)

    # 1. Real environment baseline: ffmpeg, encoder, at9, pkg are healthy (ok)
    r = client.get("/api/system/health?refresh=1")
    assert r.status_code == 200
    body = r.json()
    assert body["tools"]["ffmpeg"]["state"] == "ok"
    assert body["tools"]["encoder"]["state"] == "ok"
    if at9.available():
        assert body["tools"]["at9"]["state"] == "ok"
    if pkg_forge.pkg_forge_info()["available"]:
        assert body["tools"]["pkg"]["state"] == "ok"

    # 2. Absent tools -> state == "off" (cinza)
    with (
        mock.patch.object(system_health, "sync_runtime_tool_bindings"),
        mock.patch.object(media, "MAGICK", None),
        mock.patch.object(at9, "encoder_info", return_value={"available": False, "backend": None, "path": None}),
        mock.patch.object(pkg_forge, "pkg_forge_info", return_value={"available": False, "backend": None, "path": None}),
    ):
        r_off = client.get("/api/system/health?refresh=1")
        assert r_off.status_code == 200
        d_off = r_off.json()
        assert d_off["tools"]["magick"]["state"] == "off"
        assert d_off["tools"]["at9"]["state"] == "off"
        assert d_off["tools"]["pkg"]["state"] == "off"
        assert d_off["needs_activation"] is True

    # 3. Defective binaries -> state == "err" (vermelho)
    broken = tmp_path / "broken_tool.sh"
    broken.write_text("#!/usr/bin/env bash\necho 'simulated crash' >&2\nexit 1\n", encoding="utf-8")
    broken.chmod(0o755)
    with (
        mock.patch.object(system_health, "sync_runtime_tool_bindings"),
        mock.patch.object(media, "MAGICK", str(broken)),
        mock.patch.object(
            pkg_forge,
            "pkg_forge_info",
            return_value={"available": True, "backend": "liborbispkg-linux", "path": str(broken)},
        ),
        mock.patch.object(
            at9,
            "encoder_info",
            return_value={"available": True, "backend": "psgfx-at9", "path": str(broken)},
        ),
        mock.patch.object(at9, "encode", side_effect=RuntimeError("broken encoder")),
    ):
        r_err = client.get("/api/system/health?refresh=1")
        assert r_err.status_code == 200
        d_err = r_err.json()
        assert d_err["tools"]["magick"]["state"] == "err"
        assert d_err["tools"]["magick"]["status_label"] == "Defeituosa"
        assert d_err["tools"]["pkg"]["state"] == "err"
        assert d_err["tools"]["pkg"]["status_label"] == "Defeituosa"
        assert d_err["tools"]["at9"]["state"] == "err"
        assert d_err["tools"]["at9"]["status_label"] == "Defeituosa"
        assert d_err["any_defective"] is True

    # 4. Working synthetic magick wrapper returning valid DXT1 header -> state == "ok" (verde)
    good_magick = tmp_path / "good_magick.py"
    good_magick.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "hdr = bytearray(136)\n"
        "hdr[0:4] = b'DDS '\n"
        "hdr[84:88] = b'DXT1'\n"
        "sys.stdout.buffer.write(bytes(hdr))\n",
        encoding="utf-8",
    )
    good_magick.chmod(0o755)
    with (
        mock.patch.object(system_health, "sync_runtime_tool_bindings"),
        mock.patch.object(media, "MAGICK", str(good_magick)),
    ):
        r_ok = client.get("/api/system/health?refresh=1")
        assert r_ok.status_code == 200
        assert r_ok.json()["tools"]["magick"]["state"] == "ok"

    system_health.invalidate_health_cache()


def test_setup_endpoint_validation_and_origin_protection():
    client = TestClient(main.app)
    # Invalid target rejected with 422
    r_bad = client.post("/api/system/tools/setup", json={"target": "; rm -rf /"})
    assert r_bad.status_code == 422

    # Cross-origin request blocked with 403
    r_csrf = client.post(
        "/api/system/tools/setup",
        json={"target": "pkg"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert r_csrf.status_code == 403

    # Status endpoint always readable
    r_st = client.get("/api/system/tools/setup/status")
    assert r_st.status_code == 200
    assert "setup" in r_st.json() and "health" in r_st.json()


@pytest.mark.skipif(shutil.which("chromium") is None, reason="chromium not installed")
def test_chromium_live_traffic_lights_green_gray_red_and_tools_modal(tmp_path):
    """Verify traffic-light pills (ok/off/err), >=44px touch target, 0 overflow, and Tools modal in Chromium."""
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
            raise AssertionError("Uvicorn did not boot")

        cdp_port = _free_port()
        chrome = subprocess.Popen(
            [
                shutil.which("chromium"),
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                f"--remote-debugging-port={cdp_port}",
                f"--user-data-dir={tmp_path / 'prof'}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            ws_path = None
            for _ in range(60):
                try:
                    _, raw = _http_local(f"http://127.0.0.1:{cdp_port}/json")
                    pages = [p for p in json.loads(raw) if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
                    if pages:
                        ws_path = "/" + pages[0]["webSocketDebuggerUrl"].split("/", 3)[3]
                        break
                except Exception:
                    time.sleep(0.1)
            assert ws_path
            sock = _ws_connect("127.0.0.1", cdp_port, ws_path)
            rid = 1
            for method, params in (
                ("Page.enable", {}),
                ("Runtime.enable", {}),
                ("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}),
                ("Emulation.setDeviceMetricsOverride", {"width": 1480, "height": 924, "deviceScaleFactor": 1, "mobile": False}),
            ):
                _cdp(sock, rid, method, params)
                rid += 1

            _cdp(sock, rid, "Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
            rid += 1
            for _ in range(80):
                ready = _eval(sock, rid, "Boolean(document.querySelector('[data-tool-pill=\"pkg\"]'))")
                rid += 1
                if ready:
                    break
                time.sleep(0.1)
            assert ready, "Traffic light pills did not render"

            # Verify >= 44px pills, 0 overflow, and 3-state CSS transitions (ok, off, err)
            audit = _eval(
                sock,
                rid,
                """(() => {
                  const pills = [...document.querySelectorAll('#sysinfo .pill')];
                  const minH = Math.min(...pills.map(p => p.getBoundingClientRect().height));
                  const overflow = document.documentElement.scrollWidth - window.innerWidth;
                  const ids = pills.map(p => p.dataset.toolPill).filter(Boolean);
                  // Open modal by clicking PKG pill
                  document.querySelector('[data-tool-pill="pkg"]').click();
                  const modalOpen = document.getElementById('tools-dialog').open;
                  const legendText = document.querySelector('.tools-legend')?.textContent || '';
                  // Simulate defective state transition -> must turn red (.err)
                  applySystemHealthSnapshot({
                    ok: false, any_defective: true, needs_activation: true, backends: ['native'],
                    tools: {
                      ffmpeg: { id: 'ffmpeg', label: 'ffmpeg', state: 'ok', status_label: 'Funcionando', detail: 'ok', installable: false },
                      magick: { id: 'magick', label: 'ImageMagick', state: 'off', status_label: 'Ausente', detail: 'ausente', installable: true, setup_target: 'magick' },
                      encoder: { id: 'encoder', label: 'encoder native', state: 'ok', status_label: 'Funcionando', detail: 'ok', installable: false },
                      at9: { id: 'at9', label: 'ATRAC9', state: 'ok', status_label: 'Funcionando', detail: 'ok', installable: true, setup_target: 'at9' },
                      pkg: { id: 'pkg', label: 'PKG Tools', state: 'err', status_label: 'Defeituosa', detail: 'falha simulada', installable: true, setup_target: 'pkg' },
                    }
                  });
                  const clsOk = document.querySelector('[data-tool-pill="ffmpeg"]').className;
                  const clsOff = document.querySelector('[data-tool-pill="magick"]').className;
                  const clsErr = document.querySelector('[data-tool-pill="pkg"]').className;
                  const errText = document.querySelector('[data-tool-pill="pkg"]').textContent;
                  const activateBtnVisible = Boolean(document.querySelector('#sysinfo .pill-activate'));
                  return { minH, overflow, ids, modalOpen, legendText, clsOk, clsOff, clsErr, errText, activateBtnVisible };
                })()""",
            )
            rid += 1
            assert audit["minH"] >= 44.0, f"Pill height < 44px: {audit}"
            assert audit["overflow"] <= 1, f"Horizontal overflow: {audit}"
            assert audit["ids"] == ["ffmpeg", "magick", "encoder", "at9", "pkg"]
            assert audit["modalOpen"] is True
            assert "Verde" in audit["legendText"] and "Cinza" in audit["legendText"] and "Vermelho" in audit["legendText"]
            assert "ok" in audit["clsOk"]
            assert "off" in audit["clsOff"]
            assert "err" in audit["clsErr"]
            assert "defeituosa" in audit["errText"].lower()
            assert audit["activateBtnVisible"] is True
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
