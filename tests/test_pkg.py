"""Prueba del empaquetado a .pkg via HTTP."""
import io, json, sys, time, urllib.request, urllib.error, mimetypes, uuid, subprocess
from pathlib import Path

BASE = "http://127.0.0.1:8000"
HERE = Path(__file__).resolve().parent

# Las mismas herramientas que usa la app, buscadas igual que ella: asi el test
# no depende de donde este clonado el repo.
sys.path.insert(0, str(HERE.parent))
from app.config import PKG_TOOLS_DIR as TOOLS

def multipart(fields, files):
    b = uuid.uuid4().hex; out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, path in files.items():
        ct = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{path.name}\"\r\nContent-Type: {ct}\r\n\r\n".encode())
        out.write(path.read_bytes()); out.write(b"\r\n")
    out.write(f"--{b}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={b}"

def req(method, path, fields=None, files=None, raw=False, timeout=1800):
    url = BASE + path
    if fields is not None or files is not None:
        body, ct = multipart(fields or {}, files or {})
        r = urllib.request.Request(url, data=body, headers={"Content-Type": ct}, method=method)
    else:
        r = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            d = resp.read(); return resp.status, (d if raw else json.loads(d))
    except urllib.error.HTTPError as e:
        d = e.read()
        try: return e.code, json.loads(d)
        except Exception: return e.code, d[:400]

for _ in range(60):
    try: urllib.request.urlopen(BASE + "/api/system", timeout=2); break
    except Exception: time.sleep(0.5)

fails = []
def check(label, cond, extra=""):
    print(("  OK  " if cond else " FAIL ") + label + (f"   {extra}" if extra else ""))
    if not cond: fails.append(label)

print("=== disponibilidad ===")
s, sysinfo = req("GET", "/api/system")
has_tools = sysinfo.get("pkg_tools") is True
print(f"        pkg_tools: {has_tools}")
s, d = req("GET", "/api/package/defaults")
check("defaults 200", s == 200)
check("modo por defecto theme", d["defaults"]["mode"] == "theme", d["defaults"]["mode"])
check("category por defecto ac", d["defaults"]["category"] == "ac")

print("        rutas fijas    :", d["fixed_paths"])

print("\n=== content id sugerido ===")
s, c = req("GET", "/api/package/content-id?title_id=CUSA12345&title=Mi%20Tema%20Chido")
check("content id 36 chars", s == 200 and len(c["content_id"]) == 36, c.get("content_id"))
s, c2 = req("GET", "/api/package/content-id?title_id=nope")
check("title id invalido -> 422", s == 422, str(c2)[:70])

print("\n=== subir los 3 assets ===")
s, anim = req("POST", "/api/animated/upload", {"frames": "10"}, {"file": HERE/"sample.mp4"})
check("fondo animado", s == 200, str(anim)[:80]); ajob = anim["job_id"]
s, st = req("POST", "/api/slot/home/upload", {}, {"file": HERE/"sample.png"})
check("fondo de la home", s == 200); sjob = st["job_id"]
s, ic = req("POST", "/api/slot/fn:power/upload", {}, {"file": HERE/"sample.png"})
check("icono de funcion", s == 200); ijob = ic["job_id"]

print("\n=== validaciones de empaquetado ===")
base = {"title": "Mi Tema", "title_id": "CUSA00000",
        "content_id": "UP9000-CUSA00000_00-MITEMA0000000000",
        "category": "ac", "passcode": "0"*32,
        "animated_job": ajob, "slots": json.dumps({"home": sjob, "fn:power": ijob}),
        "frames": "10", "wait": "2", "backend": "native"}
s, e = req("POST", "/api/package/build", {**base, "slots": json.dumps({})})
check("sin fondo de home -> 400", s == 400, str(e)[:70])
s, e = req("POST", "/api/package/build", {**base, "title_id": "BAD"})
check("title id malo -> 422", s == 422, str(e)[:70])
s, e = req("POST", "/api/package/build", {**base, "category": "gd"})
check("category que no es 'ac' -> rechazada", s in (400, 422), f"{s}: {str(e)[:70]}")

print("\n=== BUILD REAL ===")
t0 = time.time()
s, r = req("POST", "/api/package/build", base)
if not has_tools:
    check("sin herramientas, build indisponible", s == 422 and "orbis-pub-cmd.exe" in r.get("detail", ""))
    print("        generación e inspección del PKG omitidas")
    sys.exit(1 if fails else 0)
check("build 200", s == 200, str(r)[:220])
if s != 200:
    print("\n".join(f"{len(fails)} FALLOS"), fails); sys.exit(1)
print(f"        tardo {r['elapsed']}s (round trip {time.time()-t0:.1f}s)")
check("pkg generado", r["pkg_bytes"] > 1_000_000, f'{r["pkg_name"]}  {r["pkg_bytes"]:,} B')
check("nombre lleva content id", r["content_id"] in r["pkg_name"], r["pkg_name"])
print("        incluido:")
for i in r["included"]:
    print(f"          {i['asset']:20} {i['path']:34} {i['bytes']:>10,} B")
print("        pasos:")
for stp in r["log"]:
    print(f"          {stp['label']:14} {stp['seconds']:>6.2f}s  {stp['cmd'][:64]}")

print("\n=== descargar y verificar el pkg ===")
s, blob = req("GET", r["download"], raw=True)
check("descarga 200", s == 200, f"{len(blob):,} B")
check("tamano coincide", len(blob) == r["pkg_bytes"])
out = HERE / "built.pkg"; out.write_bytes(blob)

lst = subprocess.run([str(TOOLS/"orbis-pub-cmd.exe"), "img_file_list", "--passcode", "0"*32, str(out)],
                     capture_output=True, text=True, timeout=600).stdout
names = [l.strip() for l in lst.splitlines() if l.strip().startswith(("Image0", "Sc0"))]
print(f"        {len(names)} entradas dentro del pkg")
# La herramienta mueve param.sfo / icon0.png / pic1.png de sce_sys a la
# particion Sc0, que es donde la PS4 los espera. keystone si queda en Image0.
check("param.sfo en Sc0", "Sc0/param.sfo" in names)
check("icon0 512 en Sc0", "Sc0/icon0.png" in names)
check("pic1 en Sc0", "Sc0/pic1.png" in names)
check("10 .dds del fondo animado", sum(1 for n in names if "background.raf/" in n and n.endswith(".dds")) == 10)
check("index.xml del raf", any(n.endswith("background.raf/index.xml") for n in names))
# Con escena animada los fondos fijos quedan tapados: no se incluyen.
check("sin texture/background/ muerto",
      not any(n.startswith("Image0/texture/background/") for n in names),
      str([n for n in names if "texture/background/" in n]))
check("miniatura", "Image0/texture/preview.png" in names)
check("theme.xml", "Image0/theme.xml" in names)
for n in names[:6]: print("         ", n)

print("\n" + ("PKG OK" if not fails else f"{len(fails)} FALLOS: {fails}"))
sys.exit(1 if fails else 0)


