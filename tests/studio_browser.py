"""Tablet emulation against a running Studio with a disposable workspace."""
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

HERE = Path(__file__).resolve().parent
URL = os.environ.get("CZAR_TEST_URL", "http://127.0.0.1:8174")
SHOTS = HERE / "studio-shots"
SHOTS.mkdir(exist_ok=True)


def navigate(page, panel):
    page.locator(f'[data-panel="{panel}"]').tap()


def choose_image(page):
    page.get_by_role("button", name="Escolher mídia", exact=True).tap()
    page.get_by_role("dialog").get_by_role("button", name="Importar mídia", exact=True).tap()
    page.locator("#media-picker").set_input_files(HERE / "sample.png")
    expect(page.get_by_role("dialog")).not_to_be_visible(timeout=10000)


def edit_field(page, field, value):
    target = page.get_by_label(field, exact=True)
    target.fill(str(value))
    target.dispatch_event("change")


def read_manifest(page):
    key = page.evaluate("localStorage.getItem('czar.current-project')")
    return page.request.get(f"{URL}/api/studio/projects/{key}").json()


def create(page, title):
    page.goto(URL)
    expect(page.get_by_role("button", name="Novo tema")).to_be_enabled()
    page.get_by_role("button", name="Novo tema").tap()
    page.get_by_label("Nome do projeto").fill(title)
    page.get_by_role("button", name="Confirmar").tap()
    expect(page.locator("#project-title")).to_have_text(title)


def landscape(context):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    create(page, "Nebulosa")
    choose_image(page)
    expect(page.locator("#canvas-empty")).not_to_be_visible()
    page.get_by_text("Recorte e ajustes", exact=True).tap()
    edit_field(page, "Esquerda (%)", 16)
    if not page.get_by_label("Topo (%)", exact=True).is_visible():
        page.get_by_text("Recorte e ajustes", exact=True).tap()
    edit_field(page, "Topo (%)", 10)
    zoom = page.get_by_label("Zoom", exact=False).first
    zoom.evaluate("""input => {
      input.value = '1.4';
      input.dispatchEvent(new InputEvent('input', {bubbles: true}));
      input.dispatchEvent(new Event('change', {bubbles: true}));
    }""")
    expect(page.locator("#save-state")).to_have_text("Salvo", timeout=10000)
    state = read_manifest(page)
    transform = state["settings"]["background"]["home"]["transform"]
    assert transform["crop"][:2] == [.16, .1], transform
    assert transform["zoom"] == 1.4, transform
    page.locator("#undo").tap()
    page.locator("#redo").tap()
    expect(page.locator("#save-state")).to_have_text("Salvo", timeout=10000)
    bounds = page.locator("#canvas").bounding_box()
    pen = context.new_cdp_session(page)
    for event, offset, pressed, clicks in (("mousePressed", 70, 1, 1), ("mouseMoved", 140, 1, 0),
                                           ("mouseReleased", 140, 0, 1)):
        pen.send("Input.dispatchMouseEvent", {"type": event, "x": bounds["x"] + offset,
                 "y": bounds["y"] + 80, "button": "left", "buttons": pressed,
                 "clickCount": clicks, "pointerType": "pen"})
    pen.detach()
    expect(page.locator("#save-state")).to_have_text("Salvo", timeout=10000)
    assert read_manifest(page)["settings"]["background"]["home"]["transform"]["x"] > 0

    navigate(page, "icons")
    page.get_by_role("button", name="Energia", exact=True).tap()
    page.get_by_role("button", name="Substituir", exact=True).tap()
    page.get_by_role("dialog").get_by_role("button", name="sample.png", exact=False).tap()
    page.get_by_role("button", name="Preview normal e glow").tap()
    expect(page.get_by_role("dialog").get_by_label("Preview glow")).to_be_visible()
    page.locator("#close-dialog").tap()
    navigate(page, "audio")
    page.get_by_label("Slot", exact=True).select_option("se_cursor")
    page.locator("#properties").get_by_role("button", name="Importar", exact=True).tap()
    page.locator("#media-picker").set_input_files(HERE / "sample_sfx.wav")
    expect(page.get_by_role("img", name="Forma de onda", exact=False)).to_be_visible(timeout=10000)
    page.get_by_role("button", name="Escutar", exact=True).tap()

    navigate(page, "project")
    page.get_by_label("Controles", exact=True).select_option("advanced")
    navigate(page, "animation")
    page.locator("#properties").get_by_role("button", name="Importar", exact=True).tap()
    page.locator("#media-picker").set_input_files(HERE / "sample.mp4")
    expect(page.locator("#playhead")).to_be_visible(timeout=15000)
    edit_field(page, "Início (s)", .5)
    edit_field(page, "Fim (s)", 2)
    page.get_by_label("Preset", exact=True).select_option("640")
    page.get_by_label("Amostragem", exact=True).select_option("manual")
    page.locator(".frame").first.tap()
    page.get_by_role("button", name="Duplicar quadro", exact=True).tap()
    page.get_by_role("button", name="Remover quadro", exact=True).tap()
    expect(page.locator("#raf-summary")).to_contain_text("640 × 360")
    page.locator("#preview-mode").tap()
    expect(page.locator("#screen-overlay")).to_be_visible()
    page.locator('[data-view="function"]').tap()
    expect(page.locator(".sim-function")).to_be_visible()
    expect(page.locator("#save-state")).to_have_text("Salvo", timeout=10000)
    assert read_manifest(page)["settings"]["mode"] == "animated"
    page.wait_for_function("document.querySelector('.frame img')?.naturalWidth > 0", timeout=12000)
    page.screenshot(path=SHOTS / "tablet-landscape.png")
    page.reload()
    expect(page.locator("#project-title")).to_have_text("Nebulosa")
    navigate(page, "animation")
    expect(page.get_by_label("Início (s)", exact=True)).to_have_value("0.5")
    page.locator("#export-button").tap()
    page.get_by_role("button", name="Bundle para empacotamento", exact=True).tap()
    link = page.get_by_role("dialog").get_by_role("link", name="Baixar ZIP")
    expect(link).to_be_visible(timeout=45000)
    with page.expect_download() as result:
        link.click()
    assert result.value.suggested_filename == "bundle-empacotamento.zip"
    assert not errors, errors


def portrait(context):
    page = context.new_page()
    create(page, "Vertical")
    navigate(page, "background")
    expect(page.locator("#inspector")).to_be_visible()
    choose_image(page)
    page.locator("#close-panel").tap()
    expect(page.locator("#inspector")).not_to_be_visible()
    page.locator("#preview-mode").tap()
    expect(page.locator("#screen-overlay")).to_be_visible()
    page.screenshot(path=SHOTS / "tablet-portrait.png", full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 412, "height": 860})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    navigate(page, "layers")
    page.get_by_role("button", name="Cor", exact=True).tap()
    expect(page.get_by_text("Camada 1", exact=True).first).to_be_visible()


with sync_playwright() as playwright:
    local = os.environ.get("CZAR_CHROMIUM")
    browser = playwright.chromium.launch(**({"executable_path": local} if local else {}))
    landscape(browser.new_context(viewport={"width": 1512, "height": 945}, has_touch=True))
    print("Tablet landscape: editor, pointer, autosave, timeline, áudio e download OK")
    portrait(browser.new_context(viewport={"width": 900, "height": 1440}, has_touch=True))
    print("Tablet portrait e tela dividida: painéis por toque OK")
    browser.close()
