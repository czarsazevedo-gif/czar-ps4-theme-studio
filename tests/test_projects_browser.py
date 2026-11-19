"""Touch viewport, real uploads, reload, browser context and Uvicorn restart."""

import os
import socket
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from browser_server import server

HERE = Path(__file__).resolve().parent


def name_dialog(page, button, name):
    page.locator(button).tap()
    page.locator("#project-dialog-name").fill(name)
    page.get_by_role("button", name="Confirmar", exact=True).tap()
    expect(page.locator("#project-name")).to_have_value(name, timeout=30_000)
    expect(page.locator("#project-name")).to_be_enabled()


def saved(page):
    expect(page.locator("#project-status")).to_have_text("Salvo", timeout=20_000)


with tempfile.TemporaryDirectory() as root, sync_playwright() as playwright:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    browser = playwright.chromium.launch(executable_path=os.environ.get("CZAR_CHROMIUM"))
    context = browser.new_context(viewport={"width": 1480, "height": 924}, has_touch=True, is_mobile=True)
    page = context.new_page()
    with server(root, port) as base:
        page.goto(base)
        name_dialog(page, "#project-new", "Tema de bancada")
        original_url = page.url
        page.locator('.slot[data-slot="home"] input').set_input_files(HERE / "sample.png")
        expect(page.locator('.slot[data-slot="home"].filled')).to_be_visible(timeout=20_000)
        page.locator('label:has(#bgmode-animated)').tap()
        page.locator("#anim-file").set_input_files(HERE / "sample.gif")
        expect(page.locator("#anim-src-name")).to_have_text("sample.gif", timeout=30_000)
        page.locator("#anim-size").select_option("640x360")
        expect(page.locator("#anim-frames")).to_be_enabled(timeout=30_000)
        page.locator("#anim-frames").evaluate("""(el) => {
            el.value = 2;
            el.dispatchEvent(new Event('input'));
            el.dispatchEvent(new Event('change'));
        }""")
        expect(page.locator("#anim-frames")).to_be_enabled(timeout=30_000)
        page.locator("#anim-wait").evaluate("(el) => { el.value = 0.25; el.dispatchEvent(new Event('input')); }")
        saved(page)
        page.locator("#project-name").fill("Tema renomeado")
        saved(page)
        page.reload()
        saved(page)
        expect(page.locator("#anim-wait")).to_have_value("0.25")
        expect(page.locator("#anim-frames")).to_have_value("2")
        expect(page.locator("#project-name")).to_have_value("Tema renomeado")
        name_dialog(page, "#project-copy", "Cópia independente")
        saved(page)
        copy_url = page.url
        assert copy_url != original_url

        page.route("**/api/projects/*", lambda route: route.fulfill(
            status=503, json={"detail": "Falha simulada."},
        ) if route.request.method == "PUT" else route.continue_())
        page.locator("#project-name").fill("Rascunho após falha")
        expect(page.locator("#project-status")).to_contain_text("Falha simulada")
        expect(page.locator("#project-name")).to_have_value("Rascunho após falha")
        page.unroute_all()
        page.locator("#project-save").tap()
        saved(page)
        page.locator("#project-close").tap()
        expect(page.locator("#project-status")).to_contain_text("Sessão avulsa")
        page.locator("#project-list").select_option(value=original_url.split("project=")[-1])
        page.locator("#project-open").tap()
        saved(page)
        expect(page.locator("#project-name")).to_have_value("Tema renomeado")
        second = context.new_page()
        second.goto(original_url)
        saved(second)
        page.locator("#project-name").fill("Versão confirmada")
        saved(page)
        second.locator("#project-name").fill("Rascunho concorrente")
        expect(second.locator("#project-status")).to_contain_text("Outra aba")
        expect(second.locator("#project-name")).to_have_value("Rascunho concorrente")
        second.close()
        browser_state = context.storage_state()
        context.close()

    with server(root, port):
        with browser.new_context(
            viewport={"width": 924, "height": 1480}, has_touch=True, is_mobile=True,
            storage_state=browser_state,
        ) as reopened:
            page = reopened.new_page()
            page.goto(copy_url)
            saved(page)
            expect(page.locator("#project-name")).to_have_value("Rascunho após falha")
            expect(page.locator("#anim-src-name")).to_have_text("sample.gif")
            expect(page.locator("#anim-wait")).to_have_value("0.25")
            page.locator(".tab[data-tab=preview]").tap()
            expect(page.locator("#pv-home")).to_be_visible()
    browser.close()

print("Projetos: painel por toque, PNG/GIF, autosave, duplicação, falha de gravação e reinício OK.")
