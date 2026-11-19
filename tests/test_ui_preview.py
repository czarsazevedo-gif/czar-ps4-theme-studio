"""Vista previa (maqueta de la PS4) y recuperacion de la sesion tras recargar.

Lo que se comprueba de la maqueta no es que "se vea bonita" sino que diga la
verdad sobre el tema: que respete la cadena de respaldos, que con escena
animada las dos pantallas ensenen lo mismo (que es lo que hace la consola) y
que los colores salgan de los mismos campos que van al theme.xml.
"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "shots"; SHOTS.mkdir(exist_ok=True)
import os
BASE = os.environ.get("CZAR_TEST_URL", "http://127.0.0.1:8000")

fails = []
def check(label, cond, extra=""):
    print(("  OK  " if cond else " FAIL ") + label + (f"   {extra}" if extra else ""))
    if not cond: fails.append(label)


def fondo_visible(page):
    """Que capa pinta el fondo ahora mismo: la imagen fija o la escena."""
    if page.is_visible("#pv-canvas"):
        return "escena"
    if page.is_visible("#pv-bg"):
        return "imagen"
    return "nada"


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
    b = pw.chromium.launch()
    page = b.new_page(viewport={"width": 1440, "height": 950})
    errs = []
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(BASE, wait_until="networkidle")
    pin_lang(page)
    errs.clear()

    print("=== sin nada subido ===")
    page.click('.tab[data-tab="preview"]'); page.wait_for_timeout(300)
    check("avisa de que falta el fondo", page.is_visible("#pv-empty"))
    check("no pinta pantallas vacias", not page.is_visible("#pv-home"))

    print("\n=== solo con el fondo de la home ===")
    page.click('.tab[data-tab="backgrounds"]'); page.wait_for_timeout(200)
    page.set_input_files('.slot[data-slot="home"] input[type=file]', str(HERE / "sample.png"))
    page.wait_for_selector('.slot[data-slot="home"].filled', timeout=60_000)
    page.click('.tab[data-tab="preview"]'); page.wait_for_timeout(500)
    check("ya hay pantalla principal", page.is_visible("#pv-home"))
    check("el fondo es la imagen fija", fondo_visible(page) == "imagen", fondo_visible(page))
    check("9 iconos de funcion arriba", page.locator("#pv-fnrow .pv-fn").count() == 9)
    check("todos son del sistema todavia",
          page.locator("#pv-fnrow .pv-fn.is-default").count() == 9)
    check("hay fila de contenido", page.locator("#pv-tiles .pv-tile").count() == 6)

    # Positions checked against measurements taken from real console
    # captures. If anyone nudges the mock-up by eye, this trips.
    caja = page.evaluate("""() => {
      const st = document.getElementById('pv-stage').getBoundingClientRect();
      const t = [...document.querySelectorAll('#pv-tiles .pv-tile')];
      const sel = t.find(x => x.classList.contains('is-sel')).getBoundingClientRect();
      const n0 = t[0].getBoundingClientRect();
      const fn = document.querySelector('#pv-fnrow .pv-fn').getBoundingClientRect();
      const p = (v, base) => +(v / base * 100).toFixed(1);
      return { nx: p(n0.left-st.left, st.width), ny: p(n0.top-st.top, st.height),
               nw: p(n0.width, st.width), sx: p(sel.left-st.left, st.width),
               sw: p(sel.width, st.width), fy: p(fn.top-st.top+fn.height/2, st.height) };
    }""")
    for clave, ref, etiqueta in (("nx", 5.1, "mosaico normal x"), ("ny", 21.4, "mosaico normal y"),
                                 ("nw", 11.6, "mosaico normal ancho"), ("sx", 17.1, "elegido x"),
                                 ("sw", 19.0, "elegido ancho"), ("fy", 9.4, "iconos de funcion y")):
        check(f"{etiqueta} coincide con la consola", abs(caja[clave] - ref) < 1.2,
              f"{caja[clave]}% vs {ref}%")
    # El elegido es el segundo, como en la consola: uno normal queda a su izq.
    elegido = page.eval_on_selector_all(
        "#pv-tiles .pv-tile",
        "e => e.findIndex(x => x.classList.contains('is-sel'))")
    check("el mosaico elegido es el segundo", elegido == 1, str(elegido))

    # El menu superior sin imagen propia tiene que caer a la de la home, igual
    # que hace la consola; si no, la maqueta ensenaria un hueco que no existe.
    src_home = page.get_attribute("#pv-bg", "src")
    page.click('label:has(#pvview-function)'); page.wait_for_timeout(400)
    check("menu superior visible", page.is_visible("#pv-func"))
    check("sin fondo propio, copia el de la home",
          page.get_attribute("#pv-bg", "src") == src_home,
          f'{src_home} vs {page.get_attribute("#pv-bg", "src")}')
    check("9 iconos grandes con nombre", page.locator("#pv-funcrow .pv-fnbig").count() == 9)
    etiquetas = page.eval_on_selector_all(
        "#pv-funcrow .pv-fnbig > span:last-child", "e => e.map(x => x.textContent.trim())")
    check("los nombres van traducidos", "Trophies" in etiquetas and "Power" in etiquetas,
          str(etiquetas[:4]))
    check("los 9 caben en una fila", len(page.eval_on_selector_all(
        "#pv-funcrow .pv-fnbig",
        "e => [...new Set(e.map(x => Math.round(x.getBoundingClientRect().top)))]")) == 1)

    print("\n=== con fondo propio del menu superior ===")
    page.click('.tab[data-tab="backgrounds"]'); page.wait_for_timeout(200)
    page.set_input_files('.slot[data-slot="function"] input[type=file]', str(HERE / "sample.gif"))
    page.wait_for_selector('.slot[data-slot="function"].filled', timeout=60_000)
    page.click('.tab[data-tab="preview"]'); page.wait_for_timeout(500)
    check("ahora usa el suyo, no el de la home",
          page.get_attribute("#pv-bg", "src") != src_home,
          str(page.get_attribute("#pv-bg", "src")))

    print("\n=== iconos propios ===")
    page.click('.tab[data-tab="icons"]'); page.wait_for_timeout(200)
    page.set_input_files("#bulk-fn", str(HERE / "sample.png"))
    page.wait_for_function(
        "() => document.querySelectorAll('#grid-function_icon .slot.filled').length === 9",
        timeout=120_000)
    page.click('.tab[data-tab="preview"]'); page.wait_for_timeout(500)
    check("ya no queda ninguno del sistema",
          page.locator("#pv-funcrow .pv-fnbig.is-default").count() == 0)
    check("el pie lo cuenta", "9 of 9" in page.inner_text("#pv-note"), page.inner_text("#pv-note"))
    page.screenshot(path=str(SHOTS / "20-vista-previa.png"), full_page=True)

    print("\n=== los colores salen del theme.xml ===")
    page.click('.tab[data-tab="package"]'); page.wait_for_timeout(300)
    page.evaluate("""() => {
        const f = document.getElementById('pkg-focuscolor');
        f.value = '#ff0000'; f.dispatchEvent(new Event('input', {bubbles: true})); }""")
    page.select_option("#pkg-themecolor", "2")
    page.wait_for_timeout(400)
    check("la muestra del selector se tine",
          page.eval_on_selector("#pkg-themecolor-swatch",
                                "e => getComputedStyle(e).backgroundColor") == "rgb(232, 65, 60)",
          page.eval_on_selector("#pkg-themecolor-swatch", "e => getComputedStyle(e).backgroundColor"))
    page.click('.tab[data-tab="preview"]'); page.wait_for_timeout(400)
    check("el foco de la maqueta usa focuscolor",
          page.eval_on_selector("#pv-stage",
                                "e => getComputedStyle(e).getPropertyValue('--pv-focus').trim()") == "#ff0000")

    print("\n=== pantalla de aplicar el tema ===")
    # La unica vista donde aparece texture/preview.png: en las otras dos ese
    # hueco no se ve por ningun lado.
    page.click('.tab[data-tab="package"]'); page.wait_for_timeout(200)
    page.fill("#pkg-title", "Tema De Prueba")
    page.select_option("#pkg-themecolor", "7")
    page.wait_for_timeout(300)
    page.click('.tab[data-tab="preview"]'); page.wait_for_timeout(200)
    page.click('label:has(#pvview-apply)'); page.wait_for_timeout(500)
    check("la vista aparece", page.is_visible("#pv-apply"))
    check("las otras dos se ocultan",
          page.is_hidden("#pv-home") and page.is_hidden("#pv-func"))
    check("lleva el titulo del tema", page.inner_text("#pv-ap-title") == "Tema De Prueba",
          page.inner_text("#pv-ap-title"))
    check("el panel izquierdo es la miniatura",
          "/api/slot/" in (page.get_attribute("#pv-ap-shot", "src") or ""),
          str(page.get_attribute("#pv-ap-shot", "src")))
    # 740x416 sobre 1920x1080 = 38.5% x 38.5%... el ancho manda: mismo ratio.
    caja = page.eval_on_selector("#pv-ap-shot", "e => e.getBoundingClientRect()")
    escena = page.eval_on_selector("#pv-stage", "e => e.getBoundingClientRect()")
    check("el panel ocupa el 38.5% del ancho, como en la consola",
          abs(caja["width"] / escena["width"] - 0.385) < 0.01,
          f'{caja["width"] / escena["width"]:.3f}')
    check("el fondo del tema se atenua (aun no esta aplicado)",
          "pv-en-ajustes" in (page.get_attribute("#pv-stage", "class") or ""))
    check("el panel de ajustes se tine con el color del sistema",
          "rgb(139, 92, 246)" in page.eval_on_selector(
              "#pv-stage", "e => getComputedStyle(e).getPropertyValue('--pv-theme')")
          or page.eval_on_selector(
              "#pv-stage", "e => getComputedStyle(e).getPropertyValue('--pv-theme').trim()") == "#8b5cf6",
          page.eval_on_selector("#pv-stage", "e => getComputedStyle(e).getPropertyValue('--pv-theme')"))
    page.screenshot(path=str(SHOTS / "22-aplicar-tema.png"), full_page=True)
    page.click('label:has(#pvview-home)'); page.wait_for_timeout(300)
    check("al volver a la home se quita el atenuado",
          "pv-en-ajustes" not in (page.get_attribute("#pv-stage", "class") or ""))

    print("\n=== con fondo animado las dos pantallas son la misma ===")
    page.click('.tab[data-tab="backgrounds"]'); page.wait_for_timeout(200)
    page.click('label:has(#bgmode-animated)'); page.wait_for_timeout(300)
    page.set_input_files("#anim-file", str(HERE / "sample.mp4"))
    page.wait_for_selector("#anim-workspace:not([hidden])")
    page.wait_for_function("() => document.getElementById('anim-loading').hidden", timeout=90_000)
    page.wait_for_timeout(500)
    page.click('.tab[data-tab="preview"]'); page.wait_for_timeout(600)
    check("la home pinta la escena", fondo_visible(page) == "escena", fondo_visible(page))
    page.click('label:has(#pvview-home)'); page.wait_for_timeout(300)
    en_home = fondo_visible(page)
    page.click('label:has(#pvview-function)'); page.wait_for_timeout(300)
    check("el menu superior pinta la misma escena",
          fondo_visible(page) == "escena" == en_home, f"{en_home} / {fondo_visible(page)}")
    check("lo dice en el pie", "both screens" in page.inner_text("#pv-note"),
          page.inner_text("#pv-note"))
    page.screenshot(path=str(SHOTS / "21-vista-previa-animada.png"), full_page=True)

    print("\n=== la sesion sobrevive a una recarga ===")
    page.click('.tab[data-tab="package"]'); page.wait_for_timeout(200)
    page.fill("#pkg-title", "Tema Que Sobrevive")
    page.wait_for_timeout(900)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(2500)
    check("el fondo de la home vuelve",
          "filled" in (page.get_attribute('.slot[data-slot="home"]', "class") or ""))
    page.click('.tab[data-tab="icons"]'); page.wait_for_timeout(300)
    check("los 9 iconos vuelven",
          page.locator("#grid-function_icon .slot.filled").count() == 9,
          str(page.locator("#grid-function_icon .slot.filled").count()))
    page.click('.tab[data-tab="package"]'); page.wait_for_timeout(300)
    check("el titulo vuelve", page.input_value("#pkg-title") == "Tema Que Sobrevive",
          page.input_value("#pkg-title"))
    tools_available = page.request.get(BASE + "/api/system").json()["pkg_tools"]
    check("disponibilidad del empaquetador", page.is_enabled("#pkg-build") == tools_available)

    # El Content ID se autogenera desde el Title ID, PERO tras recargar dejaba
    # de hacerlo: la sesion lo marcaba como "editado a mano" solo porque tenia
    # valor, y siempre lo tiene. Se quedaba con el Title ID viejo y el build
    # fallaba con "The Content ID must contain the Title ID".
    page.fill("#pkg-title-id", "CUSA01234")
    page.wait_for_timeout(900)
    cid = page.input_value("#pkg-content-id")
    check("tras recargar, el Content ID sigue al Title ID", "CUSA01234" in cid, cid)

    # Y si lo escribes tu, mandas tu.
    page.fill("#pkg-content-id", "UP9000-CUSA01234_00-MIOMIOMIOMIOMIOO")
    page.wait_for_timeout(300)
    page.fill("#pkg-title-id", "CUSA09999")
    page.wait_for_timeout(900)
    check("un Content ID escrito a mano se respeta",
          page.input_value("#pkg-content-id").endswith("MIOMIOMIOMIOMIOO"),
          page.input_value("#pkg-content-id"))

    # Los nombres de color venian del backend en espanol y salian asi en la
    # interfaz inglesa ("2 — rojo").
    colores = page.eval_on_selector_all("#pkg-themecolor option",
                                        "e => e.map(x => x.textContent)")
    check("los colores del sistema van traducidos",
          any("red" in c for c in colores) and not any("rojo" in c for c in colores),
          str(colores[:3]))

    check("sin errores de consola", not errs, str(errs[:3]))
    b.close()

print("\n" + ("VISTA PREVIA OK" if not fails else f"{len(fails)} FALLOS: {fails}"))
sys.exit(1 if fails else 0)
