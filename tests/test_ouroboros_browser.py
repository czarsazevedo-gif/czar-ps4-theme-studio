"""Touch diagnostic, real background job, DDS playback and disk persistence."""

import os
import socket
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from browser_server import server
from test_circulo import film
from test_sentinela import signals

with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
    root = Path(tmp)
    path = root / "Textura.mp4"
    film(path, signals()["periódica"] * 2)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    browser = pw.chromium.launch(executable_path=os.environ.get("CZAR_CHROMIUM"))
    ctx = browser.new_context(viewport={"width": 1480, "height": 924}, has_touch=True, is_mobile=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    with server(root / "projects", port) as base:
        page.goto(base)
        page.locator("#project-new").tap()
        page.locator("#project-dialog-name").fill("Ouroboros")
        page.get_by_role("button", name="Confirmar", exact=True).tap()
        expect(page.locator("#project-name")).to_have_value("Ouroboros")
        page.locator("label:has(#bgmode-animated)").tap()
        page.locator("#anim-file").set_input_files(path)
        expect(page.locator("#anim-src-name")).to_have_text(path.name, timeout=45_000)
        expect(page.locator("#project-status")).to_have_text("Salvo", timeout=20_000)
        page.locator("#ouroboros-link").tap()
        expect(page.locator("#video")).to_contain_text(path.name)
        diagnostic_url = page.url
        page.locator("#run").tap()
        expect(page.locator("#groups h3")).to_have_count(3, timeout=120_000)
        expect(page.locator("#summary")).to_contain_text("movimento estrutural")
        page.locator("#groups button").last.tap()
        expect(page.locator("#player")).to_be_visible(timeout=120_000)
        page.wait_for_function(
            "Number(document.querySelector('#counter').textContent.match(/(\\d+) voltas/)?.[1]) >= 10",
            timeout=85000,
        )
        page.locator("#play").tap()
        page.set_viewport_size({"width": 924, "height": 1480})
        page.locator("#seam").tap()
        expect(page.locator("#counter")).to_have_text("Inspeção da junção")
        page.locator("#step").tap()
        expect(page.locator("#counter")).to_contain_text("Quadro")
        assert page.evaluate("document.querySelector('canvas').width") == 640
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
        page.locator("#back").tap()
        expect(page.locator("#anim-src-name")).to_have_text(path.name, timeout=30_000)
    with server(root / "projects", port):
        page.goto(diagnostic_url)
        page.locator("#run").tap()
        expect(page.locator("#groups h3")).to_have_count(3, timeout=15_000)
    assert not errors, errors
    browser.close()
print("Chromium touch em duas orientações, prévia DDS, volta ao editor e reinício OK")
