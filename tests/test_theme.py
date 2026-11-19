"""Verifica que el modo tema produce un pkg de TEMA (IRO_TAG=2, volumen correcto)."""
import io, json, sys, time, urllib.request, urllib.error, mimetypes, uuid, subprocess, struct
from pathlib import Path

BASE = "http://127.0.0.1:8000"
HERE = Path(__file__).resolve().parent

# Las mismas herramientas que usa la app, buscadas igual que ella: asi el test
# no depende de donde este clonado el repo.
sys.path.insert(0, str(HERE.parent))
from app.config import PKG_TOOLS_DIR as TOOLS
CMD = str(TOOLS / "orbis-pub-cmd.exe")

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

print("=== defaults ===")
s, d = req("GET", "/api/package/defaults")
check("modo por defecto = theme", d["defaults"]["mode"] == "theme", d["defaults"]["mode"])
check("categoria por defecto = ac", d["defaults"]["category"] == "ac", d["defaults"]["category"])
check("gdt ya no se ofrece", "gdt" not in d["categories"], str(d["categories"]))
check("16 categorias validas", len(d["categories"]) == 16, str(len(d["categories"])))
modes = {m["id"]: m for m in d["modes"]}
check("modo theme -> pkg_ps4_theme + IRO 2",
      modes["theme"]["volume_type"] == "pkg_ps4_theme" and modes["theme"]["iro_tag"] == 2, str(modes["theme"]))
check("modo app -> pkg_ps4_app sin IRO",
      modes["app"]["volume_type"] == "pkg_ps4_app" and modes["app"]["iro_tag"] is None, str(modes["app"]))

print("\n=== subir assets ===")
s, anim = req("POST", "/api/animated/upload", {"frames": "10"}, {"file": HERE/"sample.mp4"})
ajob = anim["job_id"]; check("animado", s == 200)
s, st = req("POST", "/api/slot/home/upload", {}, {"file": HERE/"sample.png"}); sjob = st["job_id"]
s, ic = req("POST", "/api/slot/fn:power/upload", {}, {"file": HERE/"sample.png"}); ijob = ic["job_id"]
check("estatico + icono", bool(sjob and ijob))

base = {"title": "Mi Tema", "title_id": "CUSA00000",
        "content_id": "UP9000-CUSA00000_00-MITEMA0000000000",
        "category": "ac", "passcode": "0"*32,
        "animated_job": ajob, "slots": json.dumps({"home": sjob, "fn:power": ijob}),
        "frames": "10", "wait": "2", "backend": "native", }

print("\n=== categoria invalida rechazada ===")
s, e = req("POST", "/api/package/build", {**base, "category": "gdt", "mode": "theme"})
check("gdt -> 422", s == 422, str(e)[:80])
s, e = req("POST", "/api/package/build", {**base, "mode": "inventado"})
check("modo desconocido -> 422", s == 422, str(e)[:80])

print("\n=== BUILD MODO TEMA ===")
s, r = req("POST", "/api/package/build", {**base, "mode": "theme"})
if not req("GET", "/api/system")[1].get("pkg_tools"):
    check("sin herramientas, build indisponible", s == 422 and "orbis-pub-cmd.exe" in r.get("detail", ""))
    print("        inspección del PKG omitida")
    sys.exit(1 if fails else 0)
check("build 200", s == 200, str(r)[:200])
if s != 200: print(fails); sys.exit(1)
check("volume de tema", r["volume_type"] == "pkg_ps4_theme", r["volume_type"])
check("IRO_TAG 2 reportado", r["iro_tag"] == 2, str(r["iro_tag"]))
print(f"        {r['pkg_name']}  {r['pkg_bytes']:,} B  en {r['elapsed']}s")
check("nombre con sufijo A0000 (no es app)", "-A0000-" in r["pkg_name"], r["pkg_name"])
check("sin paso de keystone", not any(x["label"] == "keystone" for x in r["log"]),
      str([x["label"] for x in r["log"]]))

s, blob = req("GET", r["download"], raw=True)
out = HERE / "theme-mode.pkg"; out.write_bytes(blob)
check("descarga ok", s == 200 and len(blob) == r["pkg_bytes"], f"{len(blob):,} B")

print("\n=== leer el param.sfo DE DENTRO del pkg ===")
ex = HERE / "extracted"; subprocess.run(["cmd","/c","rmdir","/s","/q",str(ex)], capture_output=True)
ex.mkdir(exist_ok=True)
subprocess.run([CMD, "img_extract", "--passcode", "0"*32, str(out), str(ex)],
               capture_output=True, text=True, timeout=900)
sfo = next((p for p in ex.rglob("param.sfo")), None)
check("param.sfo extraido del pkg", sfo is not None, str(sfo))
if sfo:
    sfx = HERE / "from-pkg.sfx"
    subprocess.run([CMD, "sfo_export", str(sfo), str(sfx)], capture_output=True, timeout=300)
    txt = sfx.read_text(encoding="utf-8", errors="replace")
    print("        " + "\n        ".join(l for l in txt.splitlines() if "param key" in l))
    check("IRO_TAG=2 dentro del pkg", '<param key="IRO_TAG">2</param>' in txt)
    check("CATEGORY=ac dentro del pkg", '<param key="CATEGORY">ac</param>' in txt)
    check("CONTENT_VER dentro del pkg", '<param key="CONTENT_VER">01.00</param>' in txt)
    check("sin APP_VER (no es app)", "APP_VER" not in txt)

lst = subprocess.run([CMD, "img_file_list", "--passcode", "0"*32, str(out)],
                     capture_output=True, text=True, timeout=600).stdout
names = [l.strip() for l in lst.splitlines() if l.strip().startswith(("Image0","Sc0"))]
check("theme.xml presente", "Image0/theme.xml" in names)
# Este build lleva fondo animado, y la escena tapa las dos pantallas: los PNG
# de texture/background/ no se verian nunca, asi que no se copian. El tema de
# referencia que suena y se ve bien tampoco los trae.
check("sin fondos estaticos muertos",
      not any(n.startswith("Image0/texture/background/") for n in names),
      str([n for n in names if "texture/background/" in n]))
check("la miniatura si sigue", "Image0/texture/preview.png" in names)
check("escena RAF con plano y xml",
      any(n.endswith("plane.mdx") for n in names) and any(n.endswith("background.raf/index.xml") for n in names),
      str([n for n in names if "background.raf/" in n][:3]))
check("sin background.dds suelto", not any(n.endswith("Image0/background.dds") for n in names))
check("Sc0/param.sfo", "Sc0/param.sfo" in names)
check("Sc0/icon0.png", "Sc0/icon0.png" in names)
check("sin playgo (no es app)", not any("playgo" in n for n in names),
      str([n for n in names if "playgo" in n]))

print("\n=== el modo app sigue funcionando ===")
s, r2 = req("POST", "/api/package/build", {**base, "mode": "app"})
check("build app 200", s == 200, str(r2)[:110])
if s == 200:
    check("volume de app", r2["volume_type"] == "pkg_ps4_app", r2["volume_type"])
    check("app sin IRO_TAG", r2["iro_tag"] is None)
    check("app con keystone", any(x["label"] == "keystone" for x in r2["log"]))

print("\n" + ("TEMA TODO OK" if not fails else f"{len(fails)} FALLOS: {fails}"))
sys.exit(1 if fails else 0)
