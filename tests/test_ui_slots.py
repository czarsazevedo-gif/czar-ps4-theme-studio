"""UI de slots: rejillas, subida por hueco, aplicar a todos, empaquetado."""
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

# La consola de Windows va en cp1252 y el checklist trae un "check" unicode:
# sin esto el test revienta al imprimir, no por el fallo que este midiendo.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "shots"; SHOTS.mkdir(exist_ok=True)

fails = []
def check(label, cond, extra=""):
    print(("  OK  " if cond else " FAIL ") + label + (f"   {extra}" if extra else ""))
    if not cond: fails.append(label)

def pin_lang(pg, code="en"):
    """Deja la interfaz en un idioma conocido.

    Los textos dependen del idioma activo, asi que sin esto una suite pasaria
    o fallaria segun el navegador de quien la corra. Se fija por localStorage,
    que es de donde lo lee i18n.js, y se recarga para que aplique.
    """
    pg.evaluate(f"localStorage.setItem('ps4theme.lang', '{code}')")
    pg.reload(wait_until="networkidle")
    pg.wait_for_timeout(700)


with sync_playwright() as pw:
    b = pw.chromium.launch(executable_path=os.environ.get("CZAR_CHROMIUM"))
    page = b.new_page(viewport={"width": 1500, "height": 1100})
    errs = []
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto("http://127.0.0.1:8000", wait_until="networkidle")
    pin_lang(page)
    errs.clear()

    check("pestañas nuevas", page.locator('.tab[data-tab="backgrounds"]').count() == 1
          and page.locator('.tab[data-tab="icons"]').count() == 1)

    page.click('.tab[data-tab="backgrounds"]'); page.wait_for_timeout(300)
    check("4 huecos de fondo", page.locator("#grid-background .slot").count() == 4,
          str(page.locator("#grid-background .slot").count()))
    check("home marcado obligatorio", "required" in page.inner_text("#grid-background"))
    check("los demás dicen que copian", "copies home" in page.inner_text("#grid-background"))
    page.screenshot(path=str(SHOTS / "14-fondos-vacio.png"))

    # Al pasar a fondo animado, el del menu superior sobra (la escena lo tapa)
    # y el de la home cambia de nombre porque pasa a servir para otra cosa.
    page.click('label:has(#bgmode-animated)'); page.wait_for_timeout(300)
    visibles = page.eval_on_selector_all(
        "#grid-background .slot:not([hidden]) .slot-name", "e => e.map(x => x.textContent)")
    check("en animado quedan 3 huecos", len(visibles) == 3, str(visibles))
    check("el del menu superior desaparece",
          not any("Top menu" in v for v in visibles), str(visibles))
    check("la home pasa a ser la imagen del tema",
          "Theme image" in visibles, str(visibles))
    check("avisa de que la escena tapa los fijos", page.is_visible("#slots-raf-note"))
    page.screenshot(path=str(SHOTS / "14b-fondo-animado.png"))

    # y al volver, todo como estaba: cambiar de idea no cuesta nada
    page.click('label:has(#bgmode-static)'); page.wait_for_timeout(300)
    vuelta = page.eval_on_selector_all(
        "#grid-background .slot:not([hidden]) .slot-name", "e => e.map(x => x.textContent)")
    check("al volver reaparecen los 4", len(vuelta) == 4, str(vuelta))
    check("y con su nombre original", "Home background" in vuelta, str(vuelta))

    page.click('.tab[data-tab="icons"]'); page.wait_for_timeout(300)
    check("9 iconos de función", page.locator("#grid-function_icon .slot").count() == 9)
    check("10 iconos de contenido", page.locator("#grid-content_icon .slot").count() == 10)

    # empaquetar aun bloqueado
    page.click('.tab[data-tab="package"]'); page.wait_for_timeout(300)
    check("build bloqueado sin home", page.is_disabled("#pkg-build"))
    check("checklist pide la home", "needed" in page.inner_text("#pkg-checklist"))

    # subir dos fondos DISTINTOS
    page.click('.tab[data-tab="backgrounds"]'); page.wait_for_timeout(200)
    page.set_input_files('.slot[data-slot="home"] input[type=file]', str(HERE / "sample.png"))
    page.wait_for_selector('.slot[data-slot="home"].filled', timeout=60_000)
    page.set_input_files('.slot[data-slot="function"] input[type=file]', str(HERE / "sample.gif"))
    page.wait_for_selector('.slot[data-slot="function"].filled', timeout=60_000)
    page.wait_for_timeout(400)
    check("dos fondos con imagen", page.locator("#grid-background .slot.filled").count() == 2)
    check("miniatura sigue vacía (copia de home)",
          not page.locator('.slot[data-slot="preview"]').get_attribute("class").__contains__("filled"))
    page.screenshot(path=str(SHOTS / "15-fondos-dos.png"))

    # aplicar a todos los de funcion
    page.click('.tab[data-tab="icons"]'); page.wait_for_timeout(200)
    page.set_input_files("#bulk-fn", str(HERE / "sample.png"))
    page.wait_for_function(
        "() => document.querySelectorAll('#grid-function_icon .slot.filled').length === 9",
        timeout=120_000)
    check("aplicar a todos llenó los 9", page.locator("#grid-function_icon .slot.filled").count() == 9)

    # uno individual distinto
    page.set_input_files('.slot[data-slot="ct:disc"] input[type=file]', str(HERE / "sample.png"))
    page.wait_for_selector('.slot[data-slot="ct:disc"].filled', timeout=60_000)
    check("un icono de contenido suelto", page.locator("#grid-content_icon .slot.filled").count() == 1)
    page.screenshot(path=str(SHOTS / "16-iconos.png"))

    # quitar uno
    page.click('.slot[data-slot="fn:power"] .slot-clear')
    page.wait_for_timeout(200)
    check("quitar deja 8", page.locator("#grid-function_icon .slot.filled").count() == 8)

    # empaquetar
    page.click('.tab[data-tab="package"]'); page.wait_for_timeout(400)
    txt = page.inner_text("#pkg-checklist")
    check("checklist cuenta los iconos", "8 of 9" in txt, txt.replace("\n", " / "))
    check("checklist cuenta los fondos propios", "custom" in txt)

    # Sin las herramientas de Sony en tools/ el boton se desactiva solo: es el
    # comportamiento correcto, no un fallo. El resto de la suite si corre.
    hay_tools = page.evaluate("pkg.defaults?.available === true")
    if not hay_tools:
        check("sin tools, el boton de empaquetar se desactiva",
              page.is_disabled("#pkg-build"))
        print("        empaquetado real omitido: faltan las tools en tools/")
    else:
        check("build habilitado", not page.is_disabled("#pkg-build"))
        page.fill("#pkg-title", "Tema Personalizado")
        page.wait_for_timeout(600)
        page.click("#pkg-build")
        page.wait_for_selector("#pkg-result:not([hidden])", timeout=300_000)
        page.wait_for_timeout(400)
        res = page.inner_text("#pkg-result")
        check("empaquetado ok", "Done" in res, res.splitlines()[0])
        check("lista los dos fondos",
              "Home background" in res and "Top menu background" in res)
        check("lista los iconos",
              "Function icons" in res and "Content icons" in res)
        with page.expect_download(timeout=120_000) as dl:
            page.click("#pkg-result .dl-btn")
        check("descarga .pkg", dl.value.suggested_filename.endswith(".pkg"),
              dl.value.suggested_filename)
        page.screenshot(path=str(SHOTS / "17-empaquetado.png"), full_page=True)

    check("sin errores de consola", not errs, str(errs[:3]))
    b.close()

print("\n" + ("UI SLOTS OK" if not fails else f"{len(fails)} FALLOS: {fails}"))
sys.exit(1 if fails else 0)
