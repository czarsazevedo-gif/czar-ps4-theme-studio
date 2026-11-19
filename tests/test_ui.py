"""Maneja la UI real en Chromium: sube archivos, anima, cambia sliders, exporta."""
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
SHOTS = HERE / "shots"; SHOTS.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:8000"

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
    browser = pw.chromium.launch(executable_path=os.environ.get("CZAR_CHROMIUM"))
    page = browser.new_page(viewport={"width": 1500, "height": 980})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

    page.goto(BASE, wait_until="networkidle")
    pin_lang(page)
    errors.clear()
    check("carga sin errores de consola", not errors, str(errors[:3]))
    check("pills de sistema", "ffmpeg" in page.inner_text("#sysinfo"), page.inner_text("#sysinfo").replace("\n", " | "))
    page.screenshot(path=str(SHOTS / "01-inicio.png"))

    # ---- FONDO ANIMADO ----
    # Ya no es una pestana propia: es un modo dentro de "Fondo". El radio esta
    # oculto tras su tarjeta, asi que se pincha donde pincharia una persona.
    check("arranca en imagen fija", page.evaluate("bgMode()") == "static",
          page.evaluate("bgMode()"))
    check("el reproductor empieza oculto", page.is_hidden("#bg-animated"))
    page.click('label:has(#bgmode-animated)')
    page.wait_for_timeout(300)
    check("al elegir animado aparece", page.is_visible("#bg-animated"))

    page.set_input_files("#anim-file", str(HERE / "sample.mp4"))
    page.wait_for_selector("#anim-workspace:not([hidden])")
    page.wait_for_function("() => document.getElementById('anim-loading').hidden", timeout=90_000)
    page.wait_for_timeout(600)

    check("workspace visible", page.is_visible("#anim-workspace"))
    check("dropzone oculta", page.is_hidden("#anim-drop"))
    check("frames cargados en JS", page.evaluate("anim.bitmaps.length") == 13,
          f'bitmaps={page.evaluate("anim.bitmaps.length")}')
    check("reproduciendo", page.evaluate("player.playing") is True)

    # el canvas debe tener pixeles pintados (no negro)
    nonblack = page.evaluate("""() => {
        const c = document.getElementById('anim-canvas');
        const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;
        let n = 0; for (let i=0;i<d.length;i+=4000) if (d[i]+d[i+1]+d[i+2] > 30) n++;
        return n; }""")
    check("canvas pintado", nonblack > 50, f"{nonblack} muestras no-negras")
    check("total de frames en el contador", page.inner_text("#anim-frame-total") == "13",
          page.inner_text(".transport-read"))
    check("selector de resolución", page.locator("#anim-size option").count() == 3,
          page.inner_text("#anim-size").replace("\n", " | "))
    check("el slider no llega mas alla del tope",
          page.get_attribute("#anim-frames", "max") == "13",
          "max=" + str(page.get_attribute("#anim-frames", "max")))
    check("el contador ensena el tope, no solo el numero",
          "/" in page.inner_text("#anim-frames-out"), page.inner_text("#anim-frames-out"))
    page.screenshot(path=str(SHOTS / "02-animado.png"))

    # la animacion debe avanzar sola
    i0 = page.evaluate("player.index")
    page.wait_for_timeout(900)
    i1 = page.evaluate("player.index")
    check("el indice avanza solo", i0 != i1, f"{i0} -> {i1}")

    # Hash del canvas completo por indice: confirma que cada frame pinta algo
    # distinto (no una imagen congelada) y que el repintado es determinista.
    # Muestrear una region chica no sirve: testsrc2 tiene zonas planas iguales
    # en varios frames.
    # Muestrear mas rapido que el avance de frame (100ms) para no aliasar.
    seen = {}
    for _ in range(90):
        idx, h = page.evaluate("""() => {
            const c = document.getElementById('anim-canvas');
            const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;
            let h = 2166136261;
            for (let i=0;i<d.length;i+=997) { h ^= d[i]; h = Math.imul(h, 16777619); }
            return [player.index, h>>>0]; }""")
        seen.setdefault(idx, set()).add(h)
        page.wait_for_timeout(65)
    check("recorre todos los frames", len(seen) == 13, f"{len(seen)} indices distintos")
    check("cada frame pinta algo distinto",
          len({list(v)[0] for v in seen.values()}) == len(seen),
          f'{len({list(v)[0] for v in seen.values()})} hashes únicos')
    check("el repintado es determinista", all(len(v) == 1 for v in seen.values()))

    # play / pausa
    page.click("#anim-play")
    check("pausa funciona", page.evaluate("player.playing") is False)
    page.click("#anim-play")
    check("reanuda", page.evaluate("player.playing") is True)

    # timing con wait
    def set_range(sel, val):
        page.evaluate(
            """([s,v]) => { const e = document.querySelector(s); e.value = v;
                 e.dispatchEvent(new Event('input',{bubbles:true}));
                 e.dispatchEvent(new Event('change',{bubbles:true})); }""",
            [sel, str(val)])

    page.evaluate("""() => { const e = document.querySelector('#anim-wait'); e.value = '0.6';
                     e.dispatchEvent(new Event('input',{bubbles:true})); }""")
    check("wait=0.6s -> 600 ms/frame", page.inner_text("#anim-per-frame") == "600 ms", page.inner_text("#anim-per-frame"))
    check("fps efectivos 1.7", page.inner_text("#anim-eff-fps") == "1.7", page.inner_text("#anim-eff-fps"))
    check("loop 7.80 s", page.inner_text("#anim-loop-secs") == "7.80 s", page.inner_text("#anim-loop-secs"))
    check("el JS usa el nuevo wait", page.evaluate("player.frameMs()") == 600)

    page.evaluate("""() => { const e = document.querySelector('#anim-wait'); e.value = '0.1';
                     e.dispatchEvent(new Event('input',{bubbles:true})); }""")

    # barra de peso: nunca deberia poder ponerse en rojo
    set_range("#anim-frames", 10)
    page.wait_for_function("() => document.getElementById('anim-loading').hidden", timeout=90_000)
    page.wait_for_timeout(400)
    check("contador sigue a los frames", page.inner_text("#anim-frame-total") == "10",
          page.inner_text(".transport-read"))
    check("regenerado a 10 frames sin re-subir", page.evaluate("anim.bitmaps.length") == 10,
          f'bitmaps={page.evaluate("anim.bitmaps.length")}')
    check("barra sin estado de error", "danger" not in page.get_attribute("#budget-fill", "class"),
          page.inner_text("#budget-figure"))
    check("export habilitado", not page.is_disabled("#anim-export"))
    page.screenshot(path=str(SHOTS / "03-34-frames.png"))

    # intentar pasarse: el slider tope en 13 y no hay forma de subir mas
    set_range("#anim-frames", 17)
    page.wait_for_function("() => document.getElementById('anim-loading').hidden", timeout=90_000)
    page.wait_for_timeout(400)
    check("pedir 17 se queda en 13", page.evaluate("anim.frameCount") == 13,
          f'frameCount={page.evaluate("anim.frameCount")}')
    check("la barra sigue sin rojo", "danger" not in page.get_attribute("#budget-fill", "class"),
          page.inner_text("#budget-figure"))
    # Sin depender de si va en mayuscula: el texto de la interfaz se retoca.
    check("aviso de que esta al maximo",
          "maximum" in page.inner_text("#budget-note").lower(),
          page.inner_text("#budget-note"))
    check("export sigue habilitado", not page.is_disabled("#anim-export"))
    page.screenshot(path=str(SHOTS / "04-tope-de-frames.png"))

    # Elegir resolución tiene que cambiar la extracción DE VERDAD. El selector
    # estuvo roto sin que saltara nada: mandaba la etiqueta con "×" (U+00D7),
    # el backend no la parseaba y todo se extraía a 1280×720 con tope 13.
    page.select_option("#anim-size", "640x360")
    page.wait_for_function("() => document.getElementById('anim-loading').hidden", timeout=180_000)
    page.wait_for_timeout(700)
    check("a 640x360 se extrae a 640x360",
          page.evaluate("anim.size.join('x')") == "640x360",
          str(page.evaluate("anim.size && anim.size.join('x')")))
    check("el tope sube a 48", page.get_attribute("#anim-frames", "max") == "48",
          str(page.get_attribute("#anim-frames", "max")))
    check("estando al tope, el contador salta al nuevo maximo",
          page.evaluate("anim.frameCount") == 48, str(page.evaluate("anim.frameCount")))
    check("y se generan los 48 frames de verdad",
          page.evaluate("anim.bitmaps.length") == 48,
          str(page.evaluate("anim.bitmaps.length")))

    # Un valor elegido a mano por debajo del tope se respeta al cambiar.
    set_range("#anim-frames", 20)
    page.wait_for_function("() => document.getElementById('anim-loading').hidden", timeout=180_000)
    page.wait_for_timeout(700)
    page.select_option("#anim-size", "960x540")
    page.wait_for_function("() => document.getElementById('anim-loading').hidden", timeout=180_000)
    page.wait_for_timeout(700)
    check("20 se mantiene al pasar a un tope de 24",
          page.evaluate("anim.frameCount") == 20, str(page.evaluate("anim.frameCount")))

    page.select_option("#anim-size", "1280x720")
    page.wait_for_function("() => document.getElementById('anim-loading').hidden", timeout=180_000)
    page.wait_for_timeout(700)
    check("y se acota al volver a un tope de 13",
          page.evaluate("anim.frameCount") == 13, str(page.evaluate("anim.frameCount")))

    # volver a un valor válido y exportar
    set_range("#anim-frames", 12)
    page.wait_for_function("() => document.getElementById('anim-loading').hidden", timeout=90_000)
    page.wait_for_timeout(400)
    check("export rehabilitado", not page.is_disabled("#anim-export"))

    page.click("#anim-export")
    page.wait_for_selector("#anim-result:not([hidden])", timeout=120_000)
    page.wait_for_timeout(400)
    res = page.inner_text("#anim-result")
    check("resultado con conteo de frames", "12 frames" in res, res.splitlines()[0])
    # Formato RAF real: escena con materiales y actores, sin target=.
    check("escena RAF mostrada", '<raf angle_unit="degree">' in res and "target=" not in res,
          [l for l in res.splitlines() if "raf" in l][:1])
    check("boton de descarga", page.is_visible("#anim-result .dl-btn"))

    with page.expect_download() as dl:
        page.click("#anim-result .dl-btn")
    d = dl.value
    check("zip descargado", d.suggested_filename == "background.raf.zip", d.suggested_filename)
    page.screenshot(path=str(SHOTS / "05-exportado.png"))

    check("sin errores de consola al final", not errors, str(errors[:4]))
    browser.close()

print("\n" + ("UI TODO OK" if not fails else f"{len(fails)} FALLOS: {fails}"))
sys.exit(1 if fails else 0)
