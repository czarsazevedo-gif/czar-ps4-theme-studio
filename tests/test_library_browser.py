"""Real browser catalog workflow, with optional CC0 source clip."""

import os
import socket
import tempfile
import zipfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from browser_server import server

HERE = Path(__file__).resolve().parent
IMAGE = HERE.parent / "docs/03-preview-home.png"
VIDEO = Path(os.environ.get("CZAR_ACCEPTANCE_VIDEO", HERE / "sample.mp4"))


def create_project(page, name):
    page.locator("#project-new").tap()
    page.locator("#project-dialog-name").fill(name)
    page.get_by_role("button", name="Confirmar", exact=True).tap()
    expect(page.locator("#project-name")).to_have_value(name)


def open_library(page):
    page.locator("#media-library summary").tap()
    expect(page.locator("#library-files")).to_be_visible()


def use_asset(page, name, target):
    page.get_by_role("button", name=f"{name}", exact=False).tap()
    expect(page.locator("#library-title")).to_have_text(name)
    page.locator("#library-target").select_option(target)
    page.locator("#library-apply").tap()
    expect(page.locator("#library-apply-status")).to_have_text("Mídia aplicada ao editor.", timeout=60_000)
    page.locator("#library-asset").get_by_role("button", name="Fechar").tap()
    expect(page.locator("#project-status")).to_have_text("Salvo")


with tempfile.TemporaryDirectory() as root, sync_playwright() as playwright:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    browser = playwright.chromium.launch(executable_path=os.environ.get("CZAR_CHROMIUM"))
    context = browser.new_context(viewport={"width": 1480, "height": 924}, has_touch=True, is_mobile=True)
    page = context.new_page()
    with server(root, port) as base:
        page.goto(base)
        create_project(page, "Biblioteca no tablet")
        first = page.url
        open_library(page)
        page.locator("#library-files").set_input_files([
            str(IMAGE), str(VIDEO), str(HERE / "sample.gif"), str(HERE / "sample_sfx.wav"),
        ])
        expect(page.locator("#library-queue li", has_text="Disponível na biblioteca")).to_have_count(4, timeout=90_000)
        expect(page.locator(".library-card")).to_have_count(4)
        page.locator("#library-files").set_input_files({
            "name": "quebrado.png", "mimeType": "image/png", "buffer": b"broken image",
        })
        expect(page.locator("#library-queue")).to_contain_text("Não foi possível decodificar")
        expect(page.locator(".library-card")).to_have_count(4)
        page.locator("#library-kind").select_option("image")
        expect(page.locator(".library-card")).to_have_count(1)
        page.locator("#library-search").fill("inexistente")
        expect(page.locator("#library-status")).to_have_text("Nenhuma mídia encontrada.")
        page.locator("#library-search").fill("preview")
        page.locator("#library-view").select_option("list")
        expect(page.locator("#library-items")).to_have_class("library-items is-list")
        expect(page.locator(".library-card img")).to_be_visible()
        use_asset(page, IMAGE.name, "home")
        expect(page.locator('.slot[data-slot="home"].filled')).to_be_visible()

        page.locator("#library-search").clear()
        page.locator("#library-kind").select_option("video")
        page.locator("#library-sort").select_option("name")
        use_asset(page, VIDEO.name, "animated")
        expect(page.locator("#anim-src-name")).to_have_text(VIDEO.name)
        page.locator("#anim-size").select_option("640x360")
        expect(page.locator("#anim-frames")).to_be_enabled(timeout=30_000)
        page.locator("#anim-frames").evaluate("""(el) => {
            el.value = 2;
            el.dispatchEvent(new Event('input'));
            el.dispatchEvent(new Event('change'));
        }""")
        expect(page.locator("#anim-frames")).to_be_enabled(timeout=30_000)
        expect(page.locator("#project-status")).to_have_text("Salvo", timeout=30_000)
        page.locator("#anim-export").tap()
        link = page.locator("#anim-result a[download]")
        expect(link).to_be_visible(timeout=30_000)
        with page.expect_download() as downloading:
            link.tap()
        with zipfile.ZipFile(downloading.value.path()) as archive:
            assert archive.read("scene/background.raf/00.dds")[84:88] == b"DXT1"
            assert len(archive.namelist()) == 4

        create_project(page, "Outro tema")
        open_library(page)
        page.locator("#library-kind").select_option("image")
        use_asset(page, IMAGE.name, "fn:friend")
        expect(page.locator("#project-status")).to_have_text("Salvo")
        browser_state = context.storage_state()
        context.close()

    with server(root, port):
        with browser.new_context(
            viewport={"width": 924, "height": 1480}, has_touch=True, is_mobile=True,
            storage_state=browser_state,
        ) as reopened:
            page = reopened.new_page()
            page.goto(first)
            expect(page.locator("#project-status")).to_have_text("Salvo", timeout=30_000)
            expect(page.locator("#anim-src-name")).to_have_text(VIDEO.name)
            open_library(page)
            expect(page.locator(".library-card")).to_have_count(4)
            page.locator("#library-kind").select_option("audio")
            use_asset(page, "sample_sfx.wav", "se_cursor")
    browser.close()

print(f"Biblioteca com toque e reinício do servidor OK. Mídias: {IMAGE.name}, {VIDEO.name}, GIF e WAV.")
