"""Touch manipulation, raster preview/download and restart in Chromium."""

import base64
import io
import os
import socket
import tempfile
import zipfile
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright

from browser_server import server

HERE = Path(__file__).resolve().parent
VIDEO = Path(os.environ.get("CZAR_ACCEPTANCE_VIDEO", HERE / "sample.mp4"))


def choose_tab(page, name):
    page.locator(f'.tab[data-tab="{name}"]').tap()


def saved(page):
    expect(page.locator("#project-status")).to_have_text("Salvo", timeout=30_000)


def field(page, key):
    return page.locator(f'#comp-properties [data-field="{key}"]')


def number(page, key, value):
    control = field(page, key)
    control.fill(str(value))
    control.press("Tab")


def add_catalog_layer(page, title):
    page.locator("#comp-library").tap()
    page.locator(".library-card", has_text=title).tap()
    page.locator("#library-layer").tap()
    expect(page.locator(".comp-layer[aria-pressed=true]")).to_have_text(title)


def touch_drag(context, page, x_fraction, y_fraction, dx_fraction, dy_fraction):
    target = page.locator("#comp-canvas")
    target.scroll_into_view_if_needed()
    rect = target.bounding_box()
    x = rect["x"] + rect["width"] * x_fraction
    y = rect["y"] + rect["height"] * y_fraction
    session = context.new_cdp_session(page)
    session.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    session.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{
        "x": x + dx_fraction * rect["width"], "y": y + dy_fraction * rect["height"],
    }]})
    session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    session.detach()


def standard_png():
    output = io.BytesIO()
    Image.new("RGBA", (90, 50), (255, 16, 32, 255)).save(output, "PNG")
    return output.getvalue()


with tempfile.TemporaryDirectory() as root, sync_playwright() as playwright:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    browser = playwright.chromium.launch(executable_path=os.environ.get("CZAR_CHROMIUM"))
    context = browser.new_context(viewport={"width": 1480, "height": 924}, has_touch=True, is_mobile=True,
                                  device_scale_factor=2)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    with server(root, port) as base:
        page.goto(base)
        page.locator("#project-new").tap()
        page.locator("#project-dialog-name").fill("Compositor")
        page.get_by_role("button", name="Confirmar", exact=True).tap()
        expect(page.locator("#project-name")).to_have_value("Compositor")
        url = page.url
        choose_tab(page, "compositor")
        page.locator("#comp-files").set_input_files([
            {"name": "Marca.png", "mimeType": "image/png", "buffer": standard_png()},
            {"name": "03-preview-home.png", "mimeType": "image/png",
             "buffer": (HERE.parent / "docs/03-preview-home.png").read_bytes()},
        ])
        expect(page.locator(".comp-layer")).to_have_count(2, timeout=30_000)
        page.locator("#media-library summary").tap()
        page.locator(".comp-layer", has_text="Marca.png").tap()
        page.locator('[data-action="front"]').tap()
        expect(page.locator(".comp-layer").first).to_have_text("Marca.png")
        number(page, "width", 640)
        number(page, "x", 960)
        number(page, "y", 540)
        page.locator("#comp-snap").uncheck()
        touch_drag(context, page, 0.5, 0.5, 0.05, 0.06)
        assert abs(float(field(page, "x").input_value()) - 1056) < 8
        assert abs(float(field(page, "y").input_value()) - 604.8) < 8
        page.locator("#comp-undo").tap()
        expect(field(page, "x")).to_have_value("960")
        page.locator("#comp-redo").tap()
        assert abs(float(field(page, "x").input_value()) - 1056) < 8
        number(page, "angle", 22)
        number(page, "opacity", 75)
        page.locator('[data-action="duplicate"]').tap()
        expect(page.locator(".comp-layer")).to_have_count(3)
        page.locator('[data-action="lock"]').tap()
        expect(field(page, "angle")).to_be_disabled()
        page.locator('[data-action="lock"]').tap()
        expect(field(page, "angle")).to_be_enabled()
        page.locator('[data-action="visibility"]').tap()
        expect(page.locator(".comp-layer").first).to_contain_text("oculta")
        page.locator('[data-action="remove"]').tap()
        expect(page.locator(".comp-layer")).to_have_count(2)
        saved(page)

        with page.expect_response(lambda response: "/api/composition/image?" in response.url) as rendering:
            page.locator("#comp-ps4").tap()
        assert rendering.value.ok and rendering.value.headers.get("content-type") == "image/png"
        expect(page.locator("#pv-home")).to_be_visible(timeout=30_000)
        assert page.locator("#pv-bg").get_attribute("src").startswith("blob:")
        page.wait_for_function("document.getElementById('pv-bg').naturalWidth === 1920")
        data_url = page.locator("#pv-bg").evaluate("""async (image) => {
            const blob = await (await fetch(image.src)).blob();
            return new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result);
                reader.onerror = reject;
                reader.readAsDataURL(blob);
            });
        }""")
        authoritative_png = base64.b64decode(data_url.split(",", 1)[1])
        choose_tab(page, "package")
        page.locator("#pkg-assets").tap()
        link = page.locator("#pkg-result a[download]")
        expect(link).to_be_visible(timeout=30_000)
        with page.expect_download() as download:
            link.tap()
        with zipfile.ZipFile(download.value.path()) as archive:
            exported_png = archive.read("texture/background/homescreen.png")
            assert exported_png.startswith(b"\x89PNG\r\n\x1a\n"), exported_png[:120]
            pngs = [
                Image.open(io.BytesIO(blob)).convert("RGBA").tobytes()
                for blob in [authoritative_png, exported_png]
            ]
            assert pngs[0] == pngs[1]

        choose_tab(page, "compositor")
        page.locator("#comp-target").select_option("fn:friend")
        add_catalog_layer(page, "Marca.png")
        assert page.locator("#comp-canvas").get_attribute("width") == "128"
        saved(page)

        choose_tab(page, "backgrounds")
        page.locator('label:has(#bgmode-animated)').tap()
        page.locator("#anim-file").set_input_files(VIDEO)
        expect(page.locator("#anim-src-name")).to_have_text(VIDEO.name, timeout=60_000)
        choose_tab(page, "compositor")
        page.locator("#comp-target").select_option("animated")
        add_catalog_layer(page, "Marca.png")
        page.locator("#comp-ps4").tap()
        expect(page.locator("#pv-canvas")).to_be_visible(timeout=60_000)
        page.wait_for_function("""() => {
            const c = document.getElementById('pv-canvas');
            const p = c.getContext('2d').getImageData(c.width / 2, c.height / 2, 1, 1).data;
            return p[0] > 245 && p[1] < 22 && p[2] < 40;
        }""", timeout=30_000)
        saved(page)
        assert not errors, errors
        persist = context.storage_state()
        context.close()

    with server(root, port):
        with browser.new_context(viewport={"width": 924, "height": 1480}, has_touch=True, is_mobile=True,
                                 storage_state=persist) as reopened:
            page = reopened.new_page()
            page.goto(url)
            saved(page)
            choose_tab(page, "compositor")
            expect(page.locator(".comp-layer")).to_have_count(2)
            page.locator(".comp-layer", has_text="Marca.png").tap()
            expect(field(page, "angle")).to_have_value("22")
            expect(field(page, "opacity")).to_have_value("75")
            page.locator("#comp-target").select_option("fn:friend")
            expect(page.locator(".comp-layer")).to_have_count(1)
            page.locator("#comp-target").select_option("animated")
            expect(page.locator(".comp-layer")).to_have_count(1)
            page.locator("#comp-plus").tap()
            expect(page.locator("#comp-zoom")).to_have_text("125%")
            page.locator("#comp-zoom").tap()
            expect(page.locator("#comp-zoom")).to_have_text("100%")
    browser.close()

print(f"Compositor em Chromium touch, ZIP pixel a pixel e reinício OK; vídeo: {VIDEO.name}.")
