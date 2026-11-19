"""Musica del menu: validacion del .at9, colocacion y homebgm-enable."""
import io, json, sys, time, urllib.request, urllib.error, mimetypes, uuid, subprocess
from pathlib import Path

BASE = "http://127.0.0.1:8000"
HERE = Path(__file__).resolve().parent
CMD = str(HERE.parent / "tools" / "orbis-pub-cmd.exe")
Z = "0" * 32


def multipart(fields, files):
    b = uuid.uuid4().hex; out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, path in files.items():
        out.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
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


print("=== validacion ===")
s, e = req("POST", "/api/audio/bgm/upload", {}, {"file": HERE / "sample.png"})
check("png rechazado por extension (415)", s == 415, str(e)[:80])
s, e = req("POST", "/api/audio/inventado/upload", {}, {"file": HERE / "sample_sfx.wav"})
check("hueco de audio desconocido (404)", s == 404, str(e)[:60])

s, sysinfo = req("GET", "/api/system")
HAS_AT9TOOL = bool(sysinfo.get("at9tool"))
HAS_PACKAGER = bool(sysinfo.get("pkg_tools"))
print(f"        at9tool disponible: {HAS_AT9TOOL}")

if (HERE / "notaudio.at9").exists():
    s, e = req("POST", "/api/audio/bgm/upload", {}, {"file": HERE / "notaudio.at9"})
    if HAS_AT9TOOL:
        # Con el conversor, un wav mal llamado .at9 se transcodifica: la
        # extension da igual porque lo reencodamos de todos modos.
        check("wav disfrazado -> se convierte", s == 200 and e.get("codec") == "atrac9",
              f"{s} {str(e)[:80]}")
    else:
        check("wav disfrazado de .at9 rechazado (422)", s == 422, str(e)[:90])

AT9 = HERE / "sample.at9"
# make_fixtures lo genera con at9tool. Sin at9tool no existe, y no hay forma
# de fabricar un .at9 valido, asi que esta parte se salta entera.
if not AT9.is_file():
    print(chr(10) + "=== musica: OMITIDO ===")
    print("  falta tests/sample.at9; lo genera make_fixtures.py si hay at9tool")
else:
    print("\n=== subir un at9 de verdad ===")
    s, a = req("POST", "/api/audio/bgm/upload", {}, {"file": HERE / "sample.at9"})
    check("at9 aceptado", s == 200, str(a)[:140])
    if s != 200:
        print(fails); sys.exit(1)
    check("detecta atrac9", a["codec"] == "atrac9", a["codec"])
    check("48 kHz", a["rate"] == 48000, str(a["rate"]))
    check("estereo", a["channels"] == 2, str(a["channels"]))
    check("mide el loudness", a["loudness"] is not None, f'{a["loudness"]} LKFS')
    check("destino correcto", a["path"] == "sound/bgm_home.at9", a["path"])
    print(f"        {a['name']} — {a['duration']}s, {a['bytes']:,} B, {a['loudness']} LKFS")

    print("\n=== empaquetar con musica ===")
    s, home = req("POST", "/api/slot/home/upload", {}, {"file": HERE / "sample.png"})
    base = {"title": "Tema Con Musica", "title_id": "CUSA00000",
            "content_id": "UP9000-CUSA00000_00-CONMUSICA0000000",
            "category": "ac", "passcode": Z, "mode": "theme", "themecolor": "0",
            "slots": json.dumps({"home": home["job_id"]})}

    s, sin = req("POST", "/api/package/build", base)
    check("build sin musica", s == 200, str(sin)[:90])
    s, con = req("POST", "/api/package/build", {**base, "audio_job": a["job_id"]})
    check("build con musica", s == 200, str(con)[:90])
    if s != 200:
        print(fails); sys.exit(1)
    check("la musica aparece listada", any("Menu music" in i["asset"] for i in con["included"]),
          str([i["asset"] for i in con["included"]]))

    s, blob = req("GET", con["download"], raw=True)
    out = HERE / "audio.pkg"; out.write_bytes(blob)
    names = [l.strip() for l in subprocess.run(
        [CMD, "img_file_list", "--passcode", Z, str(out)],
        capture_output=True, text=True, timeout=600).stdout.splitlines()
        if l.strip().startswith(("Image0", "Sc0"))]
    check("sound/bgm_home.at9 dentro del pkg",
          "Image0/sound/bgm_home.at9" in names, str([n for n in names if "sound" in n]))

    print("\n=== homebgm-enable sigue a la musica ===")
    ex = HERE / "sx3"; subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(ex)], capture_output=True); ex.mkdir()
    subprocess.run([CMD, "img_extract", "--passcode", Z, str(out), str(ex)], capture_output=True, timeout=900)
    tx = next((p for p in ex.rglob("theme.xml")), None)
    check("theme.xml extraido", tx is not None)
    if tx:
        txt = tx.read_text(encoding="utf-8")
        check("con musica -> True", "<homebgm-enable>True</homebgm-enable>" in txt,
              [l.strip() for l in txt.splitlines() if "homebgm" in l])
        at9 = next((p for p in ex.rglob("bgm_home.at9")), None)
        check("el at9 sale byte a byte igual", at9 is not None
              and at9.read_bytes() == (HERE / "sample.at9").read_bytes(),
              f"{at9.stat().st_size:,} B" if at9 else "ausente")

    # y sin musica debe quedar en False
    out2 = HERE / "audio-sin.pkg"
    s, blob2 = req("GET", sin["download"], raw=True); out2.write_bytes(blob2)
    ex2 = HERE / "sx4"; subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(ex2)], capture_output=True); ex2.mkdir()
    subprocess.run([CMD, "img_extract", "--passcode", Z, str(out2), str(ex2)], capture_output=True, timeout=900)
    tx2 = next((p for p in ex2.rglob("theme.xml")), None)
    if tx2:
        check("sin musica -> False", "<homebgm-enable>False</homebgm-enable>" in tx2.read_text(encoding="utf-8"))
    names2 = [l.strip() for l in subprocess.run(
        [CMD, "img_file_list", "--passcode", Z, str(out2)],
        capture_output=True, text=True, timeout=600).stdout.splitlines()]
    check("sin musica no hay carpeta sound/", not any("sound" in n for n in names2))

print("\n=== efectos de sonido (se convierten solos) ===")
s, cat = req("GET", "/api/audio/slots")
check("catalogo de audio", s == 200 and len(cat["slots"]) == 4, str(len(cat.get("slots", []))))
conv = [x for x in cat["slots"] if x["convert"]]
check("3 efectos convertibles", len(conv) == 3, str([x["id"] for x in conv]))
check("la musica NO se convierte", not [x for x in cat["slots"] if x["id"] == "bgm"][0]["convert"])

sfx_jobs = {}
if (HERE / "sample_sfx.wav").exists():
    for sid in ("se_decide", "se_cursor", "se_cancel"):
        s, r = req("POST", f"/api/audio/{sid}/upload", {}, {"file": HERE / "sample_sfx.wav"})
        check(f"{sid}: wav -> vag", s == 200 and r["codec"].startswith("vag"), str(r)[:90])
        if s == 200:
            sfx_jobs[sid] = r["job_id"]
            print(f"        {sid}: {r['source_codec']} -> {r['codec']}, {r['bytes']:,} B")

if sfx_jobs and HAS_PACKAGER:
    s, home = req("POST", "/api/slot/home/upload", {}, {"file": HERE / "sample.png"})
    base_sfx = {"title": "Tema con efectos", "title_id": "CUSA00000",
                "category": "ac", "passcode": Z, "mode": "theme",
                "slots": json.dumps({"home": home["job_id"]})}
    s, r3 = req("POST", "/api/package/build", {**base_sfx, "sfx": json.dumps(sfx_jobs)})
    check("build con efectos", s == 200, str(r3)[:90])
    if s == 200:
        s, blob3 = req("GET", r3["download"], raw=True)
        out3 = HERE / "sfx.pkg"; out3.write_bytes(blob3)
        n3 = [l.strip() for l in subprocess.run([CMD, "img_file_list", "--passcode", Z, str(out3)],
              capture_output=True, text=True, timeout=600).stdout.splitlines()]
        for sid in sfx_jobs:
            check(f"sound/{sid}.vag en el pkg", any(n.endswith(f"{sid}.vag") for n in n3))
        # el vag del pkg lo tiene que leer ffmpeg
        ex3 = HERE / "sx5"; subprocess.run(["cmd","/c","rmdir","/s","/q",str(ex3)], capture_output=True); ex3.mkdir()
        subprocess.run([CMD, "img_extract", "--passcode", Z, str(out3), str(ex3)], capture_output=True, timeout=900)
        v = next((p for p in ex3.rglob("se_decide.vag")), None)
        check("vag extraido", v is not None)
        if v:
            check("empieza por VAGp", v.read_bytes()[:4] == b"VAGp", str(v.read_bytes()[:4]))
            pr = subprocess.run(["ffprobe","-v","error","-show_entries","stream=codec_name,sample_rate",
                                 "-of","csv=p=0", str(v)], capture_output=True, text=True).stdout.strip()
            check("ffmpeg lo reconoce", "adpcm_psx" in pr, pr)

print("\n=== conversion a ATRAC9 (solo si at9tool esta) ===")
if not HAS_AT9TOOL:
    print("  at9tool no disponible: se omite (hay que subir el .at9 ya convertido)")
else:
    src_music = HERE / "sample_music.mp3"
    if not src_music.exists():
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                        "-filter_complex", "[0][1]amerge=inputs=2",
                        "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame",
                        str(src_music)], check=True)

    s, m = req("POST", "/api/audio/bgm/upload", {}, {"file": src_music})
    check("mp3 aceptado y convertido", s == 200 and m.get("codec") == "atrac9", str(m)[:110])
    if s == 200:
        check("registra el formato de origen", m.get("source_codec") == "mp3", str(m.get("source_codec")))
        check("sale a 48 kHz estereo", m["rate"] == 48000 and m["channels"] == 2)
        check("mide el loudness del convertido", m["loudness"] is not None, str(m["loudness"]))
        print(f"        {m['source_codec']} -> {m['codec']}, {m['duration']}s, "
              f"{m['bytes']:,} B, {m['loudness']} LKFS")

        s, rr = req("POST", "/api/package/build", {**base, "audio_job": m["job_id"]})
        check("empaqueta con la musica convertida", s == 200, str(rr)[:90])
        if s == 200:
            s, bb = req("GET", rr["download"], raw=True)
            o4 = HERE / "conv.pkg"; o4.write_bytes(bb)
            e4 = HERE / "sx7"
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(e4)], capture_output=True)
            e4.mkdir()
            subprocess.run([CMD, "img_extract", "--passcode", Z, str(o4), str(e4)],
                           capture_output=True, timeout=900)
            a4 = next((x for x in e4.rglob("bgm_home.at9")), None)
            check("bgm_home.at9 dentro del pkg", a4 is not None)
            if a4:
                # ffmpeg es implementacion independiente del encoder de Sony.
                pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                     "stream=codec_name,sample_rate,channels",
                                     "-of", "csv=p=0", str(a4)],
                                    capture_output=True, text=True).stdout.strip()
                check("ffmpeg lo lee como atrac9 48k estereo", pr.startswith("atrac9,48000,2"), pr)

                # Igualar al unico bgm que sabemos que suena en la consola
                # (Anime Cyberpunk Girl). Con 168 kbps y sin bucle el archivo
                # se lee igual de bien aqui, pero la PS4 no lo reproduce.
                import struct
                d = a4.read_bytes(); o = 12; ch = {}
                while o + 8 <= len(d):
                    cid = d[o:o+4]; sz = struct.unpack_from("<I", d, o+4)[0]
                    ch[cid] = d[o+8:o+8+sz]; o += 8 + sz + (sz & 1)
                align = struct.unpack_from("<H", ch[b"fmt "], 12)[0]
                bps = struct.unpack_from("<I", ch[b"fmt "], 8)[0]
                cfg = ch[b"fmt "][44:48]
                check("144 kbps como el tema de referencia", bps == 18000 and align == 384,
                      f"{bps} B/s, align {align}")
                check("configData igual al de referencia", cfg.hex() == "fe740bf0", cfg.hex(" "))
                check("lleva bucle infinito (chunk smpl)", b"smpl" in ch,
                      "chunks: " + " ".join(c.decode("latin1") for c in ch))
                if b"smpl" in ch:
                    pc = struct.unpack_from("<I", ch[b"smpl"], 56)[0]
                    check("playCount 0 = repetir siempre", pc == 0, str(pc))

                # Comparar espectros, no ondas: un codec con perdida desfasa la
                # senal y una comparacion muestra a muestra no dice nada.
                import numpy as np
                raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                                      "-i", str(a4), "-af", "pan=mono|c0=c0",
                                      "-f", "s16le", "-acodec", "pcm_s16le",
                                      "-ar", "48000", "-"], capture_output=True).stdout
                x = np.frombuffer(raw, "<i2").astype(float)[:48000 * 4]
                if len(x) > 1000:
                    mag = np.abs(np.fft.rfft(x * np.hanning(len(x))))
                    peak = round(float(np.fft.rfftfreq(len(x), 1 / 48000)[int(np.argmax(mag))]))
                    check("conserva el tono del canal izquierdo (330 Hz)", abs(peak - 330) <= 3, f"{peak} Hz")

print("\n=== volumen y bgmhome_loud ===")
# El formato del tema no tiene ningun campo de volumen, asi que la ganancia se
# aplica AL ARCHIVO. Y el nivel resultante se declara en el param.sfo como
# bgmhome_loud, que es con lo que la consola nivela contra el resto del sistema.
if not HAS_AT9TOOL:
    print("  omitido: recodificar con otra ganancia necesita at9tool")
else:
    src_m = HERE / "sample_music.mp3"
    s, base = req("POST", "/api/audio/bgm/upload", {}, {"file": src_m})
    check("sube sin ganancia", s == 200 and base.get("gain_db") == 0, str(base)[:90])
    l0 = base["loudness"]
    check("mide el loudness de partida", l0 is not None, str(l0))

    for pedido, esperado in ((-12.0, -12.0), (-6.0, -6.0)):
        s, g = req("POST", f"/api/audio/{base['job_id']}/gain", {"gain_db": str(pedido)})
        ok = s == 200 and abs((g["loudness"] - l0) - esperado) < 0.6
        check(f"bajar {pedido:g} dB baja el loudness otro tanto", ok,
              f'{l0} -> {g.get("loudness")} ({(g.get("loudness", 0) - l0):+.2f})')

    # Al subir se pasa por limitador, asi que sube MENOS de lo pedido: es lo
    # que se quiere, saturar sonaria a distorsion.
    s, up = req("POST", f"/api/audio/{base['job_id']}/gain", {"gain_db": "6"})
    subida = up["loudness"] - l0
    check("subir 6 dB sube algo pero el limitador lo contiene",
          s == 200 and 0 < subida <= 6.2, f"{subida:+.2f} dB")

    s, tope = req("POST", f"/api/audio/{base['job_id']}/gain", {"gain_db": "999"})
    check("la ganancia se acota", s == 200 and tope["gain_db"] <= 12, str(tope.get("gain_db")))

    # Dejarla donde un tema real (-32 LKFS) y comprobarla DENTRO del pkg.
    s, fin = req("POST", f"/api/audio/{base['job_id']}/gain", {"gain_db": "-13"})
    print(f"        a -13 dB queda en {fin['loudness']} LKFS "
          f"(el tema de referencia declara -32.00)")

    s, home2 = req("POST", "/api/slot/home/upload", {}, {"file": HERE / "sample.png"})
    base2 = {"title": "Tema Con Volumen", "title_id": "CUSA00000",
             "content_id": "UP9000-CUSA00000_00-VOLUMEN000000000",
             "category": "ac", "passcode": Z, "mode": "theme", "themecolor": "0",
             "slots": json.dumps({"home": home2["job_id"]})}
    s, con = req("POST", "/api/package/build", {**base2, "audio_job": base["job_id"]})
    check("empaqueta con la musica nivelada", s == 200, str(con)[:90])
    if s == 200:
        s, blob = req("GET", con["download"], raw=True)
        o5 = HERE / "vol.pkg"; o5.write_bytes(blob)
        e5 = HERE / "sx8"
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(e5)], capture_output=True)
        e5.mkdir()
        subprocess.run([CMD, "img_extract", "--passcode", Z, str(o5), str(e5)],
                       capture_output=True, timeout=900)
        sfo5 = next((p for p in e5.rglob("param.sfo")), None)
        check("param.sfo en el pkg", sfo5 is not None)
        if sfo5:
            sfx5 = HERE / "vol.sfx"
            subprocess.run([CMD, "sfo_export", str(sfo5), str(sfx5)],
                           capture_output=True, timeout=300)
            txt5 = sfx5.read_text(encoding="utf-8", errors="replace")
            check("el pkg declara bgmhome_loud", "bgmhome_loud=" in txt5,
                  [l.strip() for l in txt5.splitlines() if "PUBTOOLINFO" in l][:1])
            import re as _re
            m5 = _re.search(r"bgmhome_loud=(-?\d+\.\d+)", txt5)
            check("y es el loudness REAL del archivo, no uno inventado",
                  m5 is not None and abs(float(m5.group(1)) - fin["loudness"]) < 0.01,
                  f'{m5.group(1) if m5 else "?"} vs {fin["loudness"]}')

    # Sin musica no hay nada que nivelar: el campo no debe aparecer.
    s, sinm = req("POST", "/api/package/build", base2)
    if s == 200:
        s, blob = req("GET", sinm["download"], raw=True)
        o6 = HERE / "sinvol.pkg"; o6.write_bytes(blob)
        e6 = HERE / "sx9"
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(e6)], capture_output=True)
        e6.mkdir()
        subprocess.run([CMD, "img_extract", "--passcode", Z, str(o6), str(e6)],
                       capture_output=True, timeout=900)
        sfo6 = next((p for p in e6.rglob("param.sfo")), None)
        if sfo6:
            sfx6 = HERE / "sinvol.sfx"
            subprocess.run([CMD, "sfo_export", str(sfo6), str(sfx6)],
                           capture_output=True, timeout=300)
            t6 = sfx6.read_text(encoding="utf-8", errors="replace")
            check("sin musica no se declara bgmhome_loud", "bgmhome_loud=" not in t6,
                  [l.strip() for l in t6.splitlines() if "PUBTOOLINFO" in l][:1])

print("\n" + ("AUDIO OK" if not fails else f"{len(fails)} FALLOS: {fails}"))
sys.exit(1 if fails else 0)
