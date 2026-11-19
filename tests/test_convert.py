"""Conversor suelto: mp3 -> wav / at9 / vag, y descarga del resultado."""
import io, json, sys, time, urllib.request, urllib.error, uuid, subprocess
from pathlib import Path

import os
BASE = os.environ.get("CZAR_TEST_URL", "http://127.0.0.1:8000")
HERE = Path(__file__).resolve().parent


def post(path, p):
    b = uuid.uuid4().hex; o = io.BytesIO()
    o.write(f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{p.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
    o.write(p.read_bytes()); o.write(b"\r\n"); o.write(f"--{b}--\r\n".encode())
    r = urllib.request.Request(BASE + path, data=o.getvalue(),
                               headers={"Content-Type": f"multipart/form-data; boundary={b}"},
                               method="POST")
    try:
        with urllib.request.urlopen(r, timeout=900) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        d = e.read()
        try: return e.code, json.loads(d)
        except Exception: return e.code, {}


def get(path, raw=False):
    try:
        with urllib.request.urlopen(BASE + path, timeout=900) as resp:
            d = resp.read(); return resp.status, (d if raw else json.loads(d))
    except urllib.error.HTTPError as e:
        return e.code, b"" if raw else {}


for _ in range(60):
    try: urllib.request.urlopen(BASE + "/api/system", timeout=2); break
    except Exception: time.sleep(0.5)

fails = []
def check(label, cond, extra=""):
    print(("  OK  " if cond else " FAIL ") + label + (f"   {extra}" if extra else ""))
    if not cond: fails.append(label)


print("=== formatos ofrecidos ===")
s, t = get("/api/convert/targets")
check("catalogo 200", s == 200)
by = {x["id"]: x for x in t["targets"]}
check("wav, at9 y vag", set(by) == {"wav", "at9", "vag"}, str(sorted(by)))
check("wav siempre disponible", by["wav"]["available"])
check("vag siempre disponible", by["vag"]["available"])
print(f"        at9 disponible: {by['at9']['available']} (depende de at9tool)")

src = HERE / "sample_music.mp3"
if not src.exists():
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=5",
                    "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", str(src)], check=True)

print("\n=== conversiones ===")
for target, magic in (("wav", b"RIFF"), ("vag", b"VAGp"), ("at9", b"RIFF")):
    if not by[target]["available"]:
        print(f"  {target}: omitido (falta at9tool)")
        continue
    s, r = post(f"/api/convert/{target}", src)
    check(f"mp3 -> {target}", s == 200, str(r)[:100])
    if s != 200:
        continue
    print(f"        {r['name']}  {r['bytes']:,} B   {r['source']['codec']} -> {r['result']['codec']}")
    check(f"{target}: nombre con la extension", r["name"].endswith("." + target), r["name"])
    sd, blob = get(r["download"], raw=True)
    check(f"{target}: descarga", sd == 200 and len(blob) == r["bytes"], f"{len(blob)} B")
    check(f"{target}: cabecera correcta", blob[:4] == magic, str(blob[:4]))
    out = HERE / f"conv_out{'.'+target}"
    out.write_bytes(blob)
    # que lo lea ffmpeg, implementacion independiente
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                         "stream=codec_name,sample_rate", "-of", "csv=p=0", str(out)],
                        capture_output=True, text=True).stdout.strip()
    esperado = {"wav": "pcm_s16le", "vag": "adpcm_psx", "at9": "atrac9"}[target]
    check(f"{target}: ffmpeg lo lee como {esperado}", esperado in pr, pr)

print("\n=== errores ===")
s, e = post("/api/convert/inventado", src)
check("formato desconocido -> 404", s == 404, str(e)[:60])
s, e = post("/api/convert/wav", HERE / "sample.png")
check("imagen rechazada -> 415", s == 415, str(e)[:70])
s, e = get("/api/convert/deadbeef1234/download")
check("descarga inexistente -> 404", s == 404)

print("\n" + ("CONVERSOR OK" if not fails else f"{len(fails)} FALLOS: {fails}"))
sys.exit(1 if fails else 0)
