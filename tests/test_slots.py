"""Slots: cada hueco con su imagen, respaldos, y el pkg resultante."""
import io, json, sys, time, urllib.request, urllib.error, mimetypes, uuid, subprocess, struct
from pathlib import Path

BASE = "http://127.0.0.1:8000"
HERE = Path(__file__).resolve().parent
CMD = str(Path(__file__).resolve().parent.parent / "tools" / "orbis-pub-cmd.exe")
Z = "0" * 32


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


def req(method, path, fields=None, files=None, raw=False, timeout=900):
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
        except Exception: return e.code, d[:300]


for _ in range(60):
    try: urllib.request.urlopen(BASE + "/api/system", timeout=2); break
    except Exception: time.sleep(0.5)

fails = []
def check(label, cond, extra=""):
    print(("  OK  " if cond else " FAIL ") + label + (f"   {extra}" if extra else ""))
    if not cond: fails.append(label)


print("=== catalogo de huecos ===")
s, cat = req("GET", "/api/slots")
check("catalogo 200", s == 200)
by_id = {x["id"]: x for x in cat["slots"]}
check("23 huecos", len(cat["slots"]) == 23, str(len(cat["slots"])))
check("3 grupos", len(cat["groups"]) == 3)
check("home obligatorio", by_id["home"]["required"] is True)
check("function copia de home", by_id["function"]["fallback"] == "home", str(by_id["function"]))
check("home -> homescreen.png", by_id["home"]["path"] == "texture/background/homescreen.png")
check("function -> functionscreen.png", by_id["function"]["path"] == "texture/background/functionscreen.png")
check("fn:power con glow", by_id["fn:power"]["glow"] == [152, 152], str(by_id["fn:power"]["glow"]))
check("ct:disc a 512", by_id["ct:disc"]["size"] == [512, 512])
check("hueco inexistente -> 404", req("POST", "/api/slot/inventado/upload", {}, {"file": HERE/"sample.png"})[0] == 404)

print("\n=== subir a huecos distintos ===")
s, home = req("POST", "/api/slot/home/upload", {}, {"file": HERE / "sample.png"})
check("home 200", s == 200, str(home)[:100])
s, fn = req("POST", "/api/slot/function/upload", {}, {"file": HERE / "sample.gif"})
check("function 200 (imagen distinta)", s == 200)
s, ic = req("POST", "/api/slot/fn:power/upload", {}, {"file": HERE / "sample.png"})
check("fn:power 200", s == 200)
s, ct = req("POST", "/api/slot/ct:folder/upload", {}, {"file": HERE / "sample.png"})
check("ct:folder 200", s == 200)

s, img = req("GET", home["preview"].split("?")[0], raw=True)
check("preview servido", s == 200 and img[8:12] == b"WEBP", f"{len(img)} B")

print("\n=== build: respaldos y huecos propios ===")
chosen = {"home": home["job_id"], "function": fn["job_id"],
          "fn:power": ic["job_id"], "ct:folder": ct["job_id"]}
base = {"title": "Tema Slots", "title_id": "CUSA00000",
        "content_id": "UP9000-CUSA00000_00-SLOTS00000000000",
        "category": "ac", "passcode": Z, "mode": "theme",
        "themecolor": "2", "slots": json.dumps(chosen)}

s, e = req("POST", "/api/package/build", {**base, "slots": json.dumps({"function": fn["job_id"]})})
check("sin home -> 400", s == 400, str(e)[:70])
s, e = req("POST", "/api/package/build", {**base, "slots": json.dumps({"home": home["job_id"], "raro": "x"})})
check("hueco desconocido -> 422", s == 422, str(e)[:70])
s, e = req("POST", "/api/package/build", {**base, "slots": "no-json"})
check("slots no-json -> 422", s == 422, str(e)[:70])

s, r = req("POST", "/api/package/build", base)
if not req("GET", "/api/system")[1].get("pkg_tools"):
    check("sin herramientas, build indisponible", s == 422 and "orbis-pub-cmd.exe" in r.get("detail", ""))
    print("        inspección del PKG omitida")
    sys.exit(1 if fails else 0)
check("build 200", s == 200, str(r)[:170])
if s != 200:
    print(fails); sys.exit(1)
for i in r["included"]:
    print(f"     {i['asset']:24} {i['path']:44} {i['bytes']:>9,} B")

s, blob = req("GET", r["download"], raw=True)
out = HERE / "slots.pkg"; out.write_bytes(blob)
lst = subprocess.run([CMD, "img_file_list", "--passcode", Z, str(out)],
                     capture_output=True, text=True, timeout=600).stdout
names = sorted(l.strip() for l in lst.splitlines() if l.strip().startswith(("Image0", "Sc0")))

print("\n=== arbol ===")
for n in names: print("   ", n)
check("homescreen", "Image0/texture/background/homescreen.png" in names)
check("functionscreen", "Image0/texture/background/functionscreen.png" in names)
check("preview (respaldo de home)", "Image0/texture/preview.png" in names)
check("solo el icono de funcion subido",
      sorted(n for n in names if "/function_icon/" in n) ==
      ["Image0/texture/function_icon/power.png", "Image0/texture/function_icon/power_glow.png"],
      str([n for n in names if "/function_icon/" in n]))
check("solo el icono de contenido subido",
      [n for n in names if "/content_icon/" in n] == ["Image0/texture/content_icon/folder.png"],
      str([n for n in names if "/content_icon/" in n]))

print("\n=== el fondo del menu es OTRA imagen ===")
ex = HERE / "sx2"; subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(ex)], capture_output=True); ex.mkdir()
subprocess.run([CMD, "img_extract", "--passcode", Z, str(out), str(ex)], capture_output=True, timeout=900)
def find(rel):
    return next((p for p in ex.rglob(Path(rel).name) if rel.replace("/", "\\") in str(p)), None)
hs, fs = find("texture/background/homescreen.png"), find("texture/background/functionscreen.png")
check("ambos fondos presentes", hs is not None and fs is not None)
if hs and fs:
    check("contenido distinto", hs.read_bytes() != fs.read_bytes(),
          f"{hs.stat().st_size:,} vs {fs.stat().st_size:,} B")
    def dim(p):
        with open(p, "rb") as f: f.seek(16); return struct.unpack(">II", f.read(8))
    check("los dos a 1920x1080", dim(hs) == (1920, 1080) and dim(fs) == (1920, 1080),
          f"{dim(hs)} / {dim(fs)}")
pv = find("texture/preview.png")
if pv and hs:
    check("miniatura copia de home (mismo origen, otro tamano)",
          pv.read_bytes() != hs.read_bytes(), "740x416 vs 1920x1080")

print("\n" + ("SLOTS OK" if not fails else f"{len(fails)} FALLOS: {fails}"))
sys.exit(1 if fails else 0)
