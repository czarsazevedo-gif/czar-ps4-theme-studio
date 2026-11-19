"""Smoke test contra el servidor real (sin dependencias externas)."""
import io, json, sys, time, urllib.request, urllib.error, zipfile, mimetypes, uuid
from pathlib import Path

import os
BASE = os.environ.get("CZAR_TEST_URL", "http://127.0.0.1:8000")
HERE = Path(__file__).parent

def multipart(fields, files):
    b = uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, path in files.items():
        ct = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{path.name}\"\r\nContent-Type: {ct}\r\n\r\n".encode())
        out.write(path.read_bytes()); out.write(b"\r\n")
    out.write(f"--{b}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={b}"

def req(method, path, fields=None, files=None, raw=False):
    url = BASE + path
    if fields is not None or files is not None:
        body, ct = multipart(fields or {}, files or {})
        r = urllib.request.Request(url, data=body, headers={"Content-Type": ct}, method=method)
    else:
        r = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            data = resp.read()
            return resp.status, (data if raw else json.loads(data)), resp.headers
    except urllib.error.HTTPError as e:
        data = e.read()
        try: return e.code, json.loads(data), e.headers
        except Exception: return e.code, data[:300], e.headers

# wait for boot
for _ in range(60):
    try:
        urllib.request.urlopen(BASE + "/api/system", timeout=2); break
    except Exception: time.sleep(0.5)

fails = []
def check(label, cond, extra=""):
    print(("  OK  " if cond else " FAIL ") + label + (f"   {extra}" if extra else ""))
    if not cond: fails.append(label)

print("=== / y estáticos ===")
s, body, h = req("GET", "/", raw=True)
check("GET / devuelve el html", s == 200 and b"PS4 Ultimate Theme Creator" in body, f"{s}, {len(body)}B")
for asset in ("/static/app.js", "/static/style.css"):
    s, body, _ = req("GET", asset, raw=True)
    check(f"GET {asset}", s == 200 and len(body) > 500, f"{s}, {len(body)}B")

print("\n=== /api/system ===")
s, sysinfo, _ = req("GET", "/api/system")
check("system 200", s == 200)
print("       ", json.dumps({k: sysinfo[k] for k in ("ffmpeg","magick","backends","max_frames","default_wait_seconds","animated_frame_bytes")}))

print("\n=== fondo animado: subida ===")
s, up, _ = req("POST", "/api/animated/upload", {"frames": "12"}, {"file": HERE/"sample.mp4"})
check("upload 200", s == 200, str(up)[:160])
job = up["job_id"]
check("12 urls de preview", len(up["frames"]) == 12)
check("budget correcto", up["budget"]["total_bytes"] == 12 * sysinfo["animated_frame_bytes"],
      f'{up["budget"]["total_bytes"]} bytes, {up["budget"]["percent"]}%')
check("no over_limit con 12", up["budget"]["over_limit"] is False)

s, img, hh = req("GET", up["frames"][0].split("?")[0], raw=True)
check("frame de preview servido", s == 200 and img[:4] == b"RIFF" and img[8:12] == b"WEBP",
      f'{s}, {len(img)}B, ct={hh.get("content-type")}')

print("\n=== reframe sin re-subir ===")
s, re1, _ = req("POST", f"/api/animated/{job}/frames", {"frames": "8"})
check("reframe 200", s == 200, str(re1)[:120])
check("ahora 8 frames", len(re1["frames"]) == 8)
check("fps recalculado", re1["fps_used"] != up["fps_used"], f'{up["fps_used"]} -> {re1["fps_used"]}')

print("\n=== la resolucion pedida es la que sale ===")
# Esto no estaba cubierto y por eso paso desapercibido: la interfaz mandaba
# "640x360" con el signo de multiplicacion U+00D7, el backend partia por la "x"
# ASCII, fallaba, y devolvia 1280x720 SIN AVISAR. El selector de resolucion no
# hacia absolutamente nada.
for etiqueta, size, esperado, tope in (
    ("ascii  640x360", "640x360", [640, 360], 48),
    ("bonito 640×360", "640×360", [640, 360], 48),
    ("ascii  960x540", "960x540", [960, 540], 24),
    ("inventada 999", "999x999", [1280, 720], 13),
):
    s2, r2, _ = req("POST", f"/api/animated/{job}/frames", {"frames": "40", "size": size})
    ok = (s2 == 200 and r2["budget"]["size"] == esperado
          and r2["budget"]["max_frames"] == tope)
    check(f"size={etiqueta} -> {esperado[0]}x{esperado[1]}", ok,
          f'{r2.get("budget", {}).get("size")} tope {r2.get("budget", {}).get("max_frames")}')

s, r40, _ = req("POST", f"/api/animated/{job}/frames", {"frames": "40", "size": "640x360"})
check("a 640x360 si caben 40 frames", len(r40["frames"]) == 40,
      str(len(r40.get("frames", []))))
check("y siguen cabiendo en los 6 MiB", r40["budget"]["over_limit"] is False,
      f'{r40["budget"]["percent"]}%')
# devolver el job a 720p para lo que viene despues
req("POST", f"/api/animated/{job}/frames", {"frames": "12", "size": "1280x720"})

print("\n=== no hay forma de pasarse del limite ===")
# Pedir de mas no da error: se acota. Asi no se puede armar un tema que la
# consola rechace, ni desde la interfaz ni llamando la API a mano.
tope = sysinfo["max_frames"]
s, over, _ = req("POST", f"/api/animated/{job}/frames", {"frames": "999"})
check("pedir 999 frames se acota al tope", s == 200 and len(over["frames"]) == tope,
      f'{len(over.get("frames", []))} (tope {tope})')
check("nunca queda over_limit", over["budget"]["over_limit"] is False, f'{over["budget"]["percent"]}%')
s, ex2, _ = req("POST", f"/api/animated/{job}/export",
                {"frames": "999", "wait_seconds": "0.1", "backend": "native"})
check("export con 999 tambien se acota", s == 200 and ex2["frames"] == tope, f"{s}: {str(ex2)[:100]}")
s, low, _ = req("POST", f"/api/animated/{job}/frames", {"frames": "0"})
check("pedir 0 frames sube al minimo", s == 200 and len(low["frames"]) == 2,
      str(len(low.get("frames", []))))

print("\n=== export real ===")
t0 = time.time()
s, ex, _ = req("POST", f"/api/animated/{job}/export", {"frames": "12", "wait_seconds": "0.25", "backend": "native"})
check("export 200", s == 200, str(ex)[:140])
check("12 frames escritos", ex["frames"] == 12)
check("wait respetado (segundos)", ex["wait"] == 0.25, str(ex["wait"]))
# dds_bytes incluye ahora el plano compartido de la escena.
check("dds_bytes = frames + plano",
      12 * sysinfo["animated_frame_bytes"] < ex["dds_bytes"]
      < 12 * sysinfo["animated_frame_bytes"] + 2000, str(ex["dds_bytes"]))
print(f"        export tardó {ex['elapsed']}s (round trip {time.time()-t0:.2f}s)")

print("\n=== zip descargado ===")
s, zb, hh = req("GET", ex["download"], raw=True)
check("download 200", s == 200, f'{len(zb)}B  ct={hh.get("content-type")}')
zf = zipfile.ZipFile(io.BytesIO(zb))
names = zf.namelist()
check("ruta scene/background.raf/", all(n.startswith("scene/background.raf/") for n in names), names[0])
check("index.xml presente", "scene/background.raf/index.xml" in names)
check("plano compartido en el zip", any(n.endswith("plane.mdx") for n in names))
dds = [n for n in names if n.endswith(".dds")]
check("12 .dds", len(dds) == 12)
check("nombres 00..23", [Path(n).name for n in sorted(dds)][:3] == ["00.dds","01.dds","02.dds"]
                        and Path(sorted(dds)[-1]).name == "11.dds")
sizes = {zf.getinfo(n).file_size for n in dds}
check("todos del mismo tamaño", len(sizes) == 1 and sizes.pop() == sysinfo["animated_frame_bytes"])
first = zf.read(sorted(dds)[0])
check("cabecera DDS/DXT1", first[:4] == b"DDS " and first[84:88] == b"DXT1", f'{first[:4]} {first[84:88]}')

xml = zf.read("scene/background.raf/index.xml").decode()
print("\n--- index.xml (primeras líneas) ---")
print("\n".join(xml.splitlines()[:5]) + "\n  ...\n" + xml.splitlines()[-1])
# Escena RAF real: sin target=, wait en segundos, un material por frame.
check("escena raf", xml.startswith('<?xml version="1.0" encoding="UTF-8"?>'))
check("raiz con angle_unit", '<raf angle_unit="degree">' in xml)
check("sin target=", "target=" not in xml)
check("wait 0.25 en segundos", '<case wait="0.25">' in xml)
check("12 materiales", xml.count("<material ") == 12, str(xml.count("<material ")))
check("12 cases", xml.count("<case ") == 12)
check("modelo como .fbx", 'file="plane.fbx"' in xml)

# Los fondos y los iconos van por huecos: los cubre test_slots.

print("\n=== errores y seguridad ===")
s, e, _ = req("POST", "/api/animated/upload", {"frames": "12"}, {"file": HERE/"sample.png"})
check("png rechazado en tab animado (415)", s == 415, f"{s}: {str(e)[:90]}")
s, e, _ = req("POST", "/api/slot/inventado/upload", {}, {"file": HERE/"sample.png"})
check("hueco inexistente -> 404", s == 404, f"{s}")
s, e, _ = req("POST", "/api/animated/deadbeef1234/frames", {"frames": "10"})
check("job inexistente -> 404", s == 404, f"{s}")
s, e, _ = req("POST", "/api/animated/..%2f..%2fetc/frames", {"frames": "10"})
check("job id con traversal -> 404", s in (404, 400), f"{s}")
s, e, _ = req("GET", f"/api/animated/{job}/preview/..%2f..%2fjob.json", raw=True)
check("preview con traversal -> 404", s == 404, f"{s}")
s, e, _ = req("GET", f"/api/animated/{job}/preview/00.dds", raw=True)
check("preview de no-webp -> 404", s == 404, f"{s}")

print("\n" + ("TODO OK" if not fails else f"{len(fails)} FALLOS: {fails}"))
sys.exit(1 if fails else 0)
