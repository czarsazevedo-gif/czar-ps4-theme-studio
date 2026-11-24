"""Headless Chromium E2E test for the 4 Gold Master deliverables:
1. Transparent reverse-proxy HTTP 413 fallback to 2 MiB streaming chunks across
   Animated Background, Media Library, Compositor, Icons/Slots, Batch-by-name,
   Audio SFX, Forge Audio Inspect, and Forge PKG Inspect.
2. Full SVG upload & high-quality auto-conversion in fn:*, ct:*, batch-by-name,
   Library, and Compositor.
3. 5 separate Media Library folder tabs, counts, direct folder upload, filtering,
   and folder reassignment.
4. Active /api/keepalive heartbeat, connection status indicator (#conn-status),
   offline -> auto-reconnect recovery, and visibilitychange recovery.
"""

from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from browser_server import server

HERE = Path(__file__).resolve().parent
SAMPLE_MP4 = HERE / "sample.mp4"
SAMPLE_GIF = HERE / "sample.gif"
SAMPLE_WAV = HERE / "sample_sfx.wav"
SAMPLE_PNG = HERE.parent / "docs" / "03-preview-home.png"

WHITE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
    'fill="none" stroke="#ffffff" stroke-width="2"><circle cx="12" cy="12" r="8"/></svg>'
).encode("utf-8")

WHITE_SVG_2 = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32" '
    'fill="none" stroke="#ffffff" stroke-width="2"><rect x="4" y="4" width="24" height="24" rx="4"/></svg>'
).encode("utf-8")


def create_project(page, name: str):
    page.locator("#project-new").click()
    page.locator("#project-dialog-name").fill(name)
    page.get_by_role("button", name="Confirmar", exact=True).click()
    expect(page.locator("#project-name")).to_have_value(name)


def run_e2e():
    with tempfile.TemporaryDirectory() as root, sync_playwright() as playwright:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        browser = playwright.chromium.launch(
            executable_path=os.environ.get("CZAR_CHROMIUM") or None
        )
        context = browser.new_context(viewport={"width": 1440, "height": 960})
        page = context.new_page()

        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        proxy_413_hits: list[str] = []
        chunk_puts: list[str] = []
        keepalive_hits = {"count": 0}
        simulate_offline = {"on": False}

        def intercept_direct_413(route):
            req = route.request
            if req.method == "POST":
                proxy_413_hits.append(req.url)
                route.fulfill(
                    status=413,
                    content_type="text/html",
                    body="<html><body><h1>413 Request Entity Too Large</h1><p>nginx/codespaces</p></body></html>",
                )
                return
            route.continue_()

        def track_chunks(route):
            if route.request.method == "PUT":
                chunk_puts.append(route.request.url)
            route.continue_()

        def intercept_keepalive(route):
            keepalive_hits["count"] += 1
            if simulate_offline["on"]:
                route.fulfill(status=503, content_type="text/plain", body="Service Unavailable")
                return
            route.continue_()

        # Simulate reverse-proxy HTTP 413 on ALL direct upload routes!
        for pattern in [
            "**/api/animated/upload*",
            "**/api/library",
            "**/api/slot/*/upload*",
            "**/api/slots/upload-batch*",
            "**/api/audio/*/upload*",
            "**/api/forge/audio/inspect*",
            "**/api/forge/pkg/inspect*",
        ]:
            page.route(pattern, intercept_direct_413)
        page.route("**/api/uploads/*/chunk*", track_chunks)
        page.route("**/api/keepalive*", intercept_keepalive)

        with server(root, port) as base:
            page.goto(base)
            # Deliverable 4: #conn-status connects via /api/keepalive
            expect(page.locator("#conn-status")).to_have_attribute("data-state", "connected", timeout=15_000)
            expect(page.locator("#conn-status")).to_have_text("Conectado")
            assert keepalive_hits["count"] >= 1

            create_project(page, "Gold Master E2E")

            # Deliverable 1 + 3: Media Library with 5 dedicated folders + HTTP 413 fallback
            page.locator("#media-library summary").click()
            expect(page.locator("#library-folders .library-folder-tab")).to_have_count(6)  # Todas + 5 folders

            # Upload SVG directly into "Ícones de função" folder (hits simulated 413 -> chunked fallback!)
            page.locator('#library-folders [data-folder="function_icons"]').click()
            page.locator("#library-files").set_input_files({
                "name": "config_icon.svg",
                "mimeType": "image/svg+xml",
                "buffer": WHITE_SVG,
            })
            expect(page.locator("#library-queue li", has_text="Disponível na biblioteca")).to_have_count(1, timeout=30_000)
            expect(page.locator(".library-card")).to_have_count(1)
            expect(page.locator('#library-folders [data-count="function_icons"]')).to_have_text("1")

            # Upload SVG directly into "Ícones de conteúdo"
            page.locator('#library-folders [data-folder="content_icons"]').click()
            page.locator("#library-files").set_input_files({
                "name": "browser_icon.svg",
                "mimeType": "image/svg+xml",
                "buffer": WHITE_SVG_2,
            })
            expect(page.locator("#library-queue li", has_text="Disponível na biblioteca")).to_have_count(1, timeout=30_000)
            expect(page.locator('#library-folders [data-count="content_icons"]')).to_have_text("1")

            # Upload audio into "Áudio"
            page.locator('#library-folders [data-folder="audio"]').click()
            page.locator("#library-files").set_input_files(str(SAMPLE_WAV))
            expect(page.locator("#library-queue li", has_text="Disponível na biblioteca")).to_have_count(1, timeout=30_000)
            expect(page.locator('#library-folders [data-count="audio"]')).to_have_text("1")

            # Upload theme image into "Imagem de tema"
            page.locator('#library-folders [data-folder="theme_image"]').click()
            page.locator("#library-files").set_input_files(str(SAMPLE_PNG))
            expect(page.locator("#library-queue li", has_text="Disponível na biblioteca")).to_have_count(1, timeout=30_000)
            expect(page.locator('#library-folders [data-count="theme_image"]')).to_have_text("1")

            # Upload animated video into "Fundo animado"
            page.locator('#library-folders [data-folder="animated_bg"]').click()
            page.locator("#library-files").set_input_files(str(SAMPLE_GIF))
            expect(page.locator("#library-queue li", has_text="Disponível na biblioteca")).to_have_count(1, timeout=30_000)
            expect(page.locator('#library-folders [data-count="animated_bg"]')).to_have_text("1")

            # Switch back to "Todas" and verify all 5 items appear
            page.locator('#library-folders [data-folder=""]').click()
            expect(page.locator('#library-folders [data-count=""]')).to_have_text("5")
            expect(page.locator(".library-card")).to_have_count(5)

            # Reassign folder inside asset dialog
            page.locator(".library-card", has_text="03-preview-home.png").click()
            expect(page.locator("#library-asset")).to_be_visible()
            page.locator("#library-asset-folder").select_option("content_icons")
            expect(page.locator("#library-apply-status")).to_contain_text("Ícones de conteúdo", timeout=15_000)
            page.locator("#library-asset").get_by_role("button", name="Fechar").click()
            expect(page.locator('#library-folders [data-count="content_icons"]')).to_have_text("2")

            # Collapse library
            page.locator("#media-library summary").click()

            # Deliverable 1: Animated background upload under simulated HTTP 413 -> chunked fallback
            page.locator('label.mode-opt', has=page.locator('#bgmode-animated')).click()
            page.locator("#anim-file").set_input_files(str(SAMPLE_MP4))
            expect(page.locator("#anim-src-name")).to_have_text("sample.mp4", timeout=60_000)
            expect(page.locator("#anim-workspace")).to_be_visible()

            # Deliverable 1 + 2: Icons tab — single SVG upload on fn:setting + batch-by-name SVG upload under HTTP 413!
            page.locator('.tab[data-tab="icons"]').click()
            page.locator('.slot[data-slot="fn:setting"] input[type="file"]').set_input_files({
                "name": "settings_vec.svg",
                "mimeType": "image/svg+xml",
                "buffer": WHITE_SVG,
            })
            expect(page.locator('.slot[data-slot="fn:setting"].filled')).to_be_visible(timeout=30_000)

            page.locator("#bulk-by-name").set_input_files([
                {"name": "trofeus.svg", "mimeType": "image/svg+xml", "buffer": WHITE_SVG},
                {"name": "navegador.svg", "mimeType": "image/svg+xml", "buffer": WHITE_SVG_2},
            ])
            expect(page.locator('.slot[data-slot="fn:trophy"].filled')).to_be_visible(timeout=30_000)
            expect(page.locator('.slot[data-slot="ct:browser"].filled')).to_be_visible(timeout=30_000)

            # Deliverable 1 + 2: Compositor SVG layer import under simulated HTTP 413
            page.locator('.tab[data-tab="compositor"]').click()
            expect(page.locator("#comp-editor")).to_be_visible()
            page.locator("#comp-files").set_input_files({
                "name": "sticker_comp.svg",
                "mimeType": "image/svg+xml",
                "buffer": WHITE_SVG,
            })
            expect(page.locator("#comp-layers li")).to_have_count(1, timeout=30_000)
            # Close library opened by compositor import
            if page.locator("#media-library").evaluate("el => el.open"):
                page.locator("#media-library summary").click()

            # Deliverable 1: Audio SFX + Forge Audio Inspect under simulated HTTP 413
            page.locator('.tab[data-tab="sounds"]').click()
            page.locator("#forge-audio-file").set_input_files(str(SAMPLE_WAV))
            expect(page.locator("#forge-audio-status")).to_contain_text("Áudio inspecionado", timeout=30_000)

            page.locator('#sfx-rows input[type="file"]').first.set_input_files(str(SAMPLE_WAV))
            expect(page.locator("#sfx-rows .bgm-info.on").first).to_contain_text("vag", timeout=30_000)

            # Deliverable 1: Forge PKG Inspect card under simulated HTTP 413 (malformed pkg shows structured BUILD_FAILED report via chunked path)
            page.locator('.tab[data-tab="package"]').click()
            page.locator("#forge-pkg-inspect-card summary").click()
            page.locator("#forge-pkg-inspect-file").set_input_files({
                "name": "fake_inspect.pkg",
                "mimeType": "application/octet-stream",
                "buffer": b"not-a-pkg-header" * 32,
            })
            expect(page.locator("#forge-pkg-inspect-status")).to_contain_text("BUILD_FAILED", timeout=30_000)
            expect(page.locator("#forge-pkg-inspect-output")).to_be_visible()

            # Verify HTTP 413 proxy hits occurred AND chunked PUTs rescued every single one!
            assert len(proxy_413_hits) >= 9, f"Expected >= 9 simulated 413 interceptions, got {len(proxy_413_hits)}"
            assert len(chunk_puts) >= 9, f"Expected >= 9 chunk PUT fallbacks, got {len(chunk_puts)}"

            # Deliverable 4: Simulate connection drop -> Offline -> Auto-reconnect on visibility return
            simulate_offline["on"] = True
            page.evaluate("() => window.CzarKeepAlive.ping('test-drop-1')")
            expect(page.locator("#conn-status")).to_have_attribute("data-state", "reconnecting", timeout=10_000)
            page.evaluate("() => window.CzarKeepAlive.ping('test-drop-2')")
            expect(page.locator("#conn-status")).to_have_attribute("data-state", "offline", timeout=10_000)

            simulate_offline["on"] = False
            # Simulate returning from tablet gallery (visibilitychange -> visible)
            page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
            expect(page.locator("#conn-status")).to_have_attribute("data-state", "connected", timeout=15_000)
            expect(page.locator("#conn-status")).to_have_text("Conectado")

        browser.close()

    assert not page_errors, f"Uncaught page errors in Chromium: {page_errors}"
    print(
        f"Gold Master E2E OK in headless Chromium! "
        f"Simulated 413s rescued: {len(proxy_413_hits)} · Chunk PUTs: {len(chunk_puts)} · Keepalive hits: {keepalive_hits['count']}"
    )


if __name__ == "__main__":
    run_e2e()
