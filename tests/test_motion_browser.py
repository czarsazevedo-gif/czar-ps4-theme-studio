"""Chromium touch, forward playback, applied PS4 preview and disk restart."""

import os
import socket
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from browser_server import server
from test_motion_api import film
from test_motion_experiments import cases


def tab(p, name):
    p.locator(f'.tab[data-tab="{name}"]').tap()


def change(p, name, value):
    field = p.locator("#motion-" + name)
    field.fill(str(value))
    field.press("Tab")


def ready(page):
    expect(page.locator("#motion-position")).to_contain_text("Quadro 1/", timeout=90_000)


with tempfile.TemporaryDirectory() as temp, sync_playwright() as pw:
    root = Path(temp)
    video = root / "textura.mp4"
    film(video, cases()["periódica"] * 3)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    browser = pw.chromium.launch(executable_path=os.environ.get("CZAR_CHROMIUM"))
    context = browser.new_context(
        has_touch=True, is_mobile=True, device_scale_factor=1.5, viewport={"width": 1480, "height": 924}
    )
    p = context.new_page()
    errors = []
    p.on("pageerror", lambda exc: errors.append(str(exc)))
    with server(root / "projects", port) as base:
        p.goto(base)
        p.locator("#project-new").tap()
        p.locator("#project-dialog-name").fill("Rio")
        p.get_by_role("button", name="Confirmar", exact=True).tap()
        expect(p.locator("#project-name")).to_have_value("Rio")
        url = p.url
        tab(p, "backgrounds")
        p.locator("label:has(#bgmode-animated)").tap()
        p.locator("#anim-file").set_input_files(video)
        expect(p.locator("#anim-src-name")).to_have_text(video.name, timeout=30_000)
        tab(p, "motion")
        expect(p.locator("#motion-editor")).to_be_visible()
        p.locator("#motion-analyze").tap()
        expect(p.locator("#motion-candidates button").first).to_be_visible(timeout=60_000)
        p.locator("#motion-candidates button").first.tap()
        p.locator("#motion-zoom").select_option("2")
        p.locator("#motion-strip button").nth(2).tap()
        p.locator("#motion-mark-in").tap()
        p.locator("#motion-undo").tap()
        p.locator("#motion-redo").tap()
        p.locator("#motion-undo").tap()
        change(p, "start", 0)
        change(p, "end", 1)
        change(p, "frames", 16)
        p.locator("#motion-render").tap()
        ready(p)
        expect(p.locator("#motion-download")).to_be_visible()
        expect(p.locator("#motion-apply")).to_be_enabled()
        p.locator("#motion-count").check()
        p.locator("#motion-play").tap()
        p.wait_for_function(
            "parseInt(document.querySelector('#motion-turns').textContent) >= 10", timeout=30_000
        )
        p.locator("#motion-play").tap()
        p.locator("#motion-count").uncheck()
        expect(p.locator("#motion-turns")).to_be_hidden()
        p.locator("#motion-view").select_option("cut")
        ready(p)
        p.locator("#motion-next").tap()
        expect(p.locator("#motion-position")).to_contain_text("Quadro 2/")
        p.locator("#motion-view").select_option("opt")
        ready(p)
        p.locator("#motion-seam").tap()
        expect(p.locator("#motion-play")).to_have_text("Pausar")
        p.locator("#motion-view").select_option("original")
        expect(p.locator("#motion-original")).to_be_visible()
        p.locator("#motion-view").select_option("raf")
        ready(p)
        p.locator("#motion-apply").tap()
        expect(p.locator("#motion-off")).to_be_visible()
        expect(p.locator("#project-status")).to_have_text("Salvo", timeout=20_000)
        with p.expect_download() as event:
            p.locator("#motion-download").tap()
        assert event.value.suggested_filename == "background.raf.zip"
        tab(p, "preview")
        expect(p.locator("#pv-stage")).to_be_visible(timeout=40_000)
        p.wait_for_function(
            "document.querySelector('#pv-canvas').width === 640 && document.querySelector('#pv-canvas').getContext('2d').getImageData(100,100,1,1).data[3] === 255"
        )
        p.set_viewport_size({"width": 924, "height": 1480})
        tab(p, "motion")
        ready(p)
        assert p.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
        if os.environ.get("CZAR_MOTION_SCREENSHOT"):
            p.locator("#panel-motion").screenshot(path=os.environ["CZAR_MOTION_SCREENSHOT"])
        p.reload()
        tab(p, "motion")
        ready(p)
        expect(p.locator("#motion-off")).to_be_visible()
    with server(root / "projects", port):
        p.goto(url)
        expect(p.locator("#project-name")).to_have_value("Rio")
        tab(p, "motion")
        ready(p)
        expect(p.locator("#motion-frames")).to_have_value("16")
        expect(p.locator("#motion-off")).to_be_visible()
        p.locator("#motion-off").tap()
        expect(p.locator("#motion-off")).to_be_hidden()
        expect(p.locator("#project-status")).to_have_text("Salvo")
    assert not errors, errors
    browser.close()
print("Touch paisagem e retrato, dez voltas, quatro vistas, prévia PS4, recarga e reinício OK")
