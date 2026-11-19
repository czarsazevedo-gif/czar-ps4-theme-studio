from __future__ import annotations

import struct
import wave
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

from .. import dxt1, mdx, raf, theme, theme_xml
from ..config import ANIMATED_SIZES, AUDIO_SLOTS, IMAGE_SLOTS, PIC1_SIZE, RAF_SIZE_LIMIT, THEME_PATHS, dds_bytes_for
from . import sfo
from .media import at9_loop, detected, probe, vag_data
from .schema import SCHEMA_VERSION, VERSION, Project
from .storage import StudioError, now, sha256


def item(category: str, status: str, message: str) -> dict:
    return {"category": category, "status": status, "message": message}


def raf_metadata(folder: Path, progress=lambda: None) -> dict:
    files = sorted(folder.glob("*.dds"))
    if len(files) < 2 or any(f.name != theme.frame_name(i) for i, f in enumerate(files)):
        raise ValueError("A numeração sequencial dos quadros DDS está incorreta.")
    theme.check_frame_count(len(files))
    total = sum(f.stat().st_size for f in files)
    theme.check_budget(total)
    size = None
    for f in files:
        progress()
        blob = f.read_bytes()
        if len(blob) < 128 or blob[:4] != b"DDS " or blob[84:88] != b"DXT1":
            raise ValueError("O cabeçalho DDS DXT1 está inválido.")
        height, width = struct.unpack_from("<II", blob, 12)
        if ((width, height) not in ANIMATED_SIZES or len(blob) != dds_bytes_for(width, height)
                or blob[:128] != dxt1.dds_header(width, height, len(blob) - 128)):
            raise ValueError("O tamanho DDS não corresponde à resolução RAF.")
        if size and size != (width, height):
            raise ValueError("Os quadros DDS têm resoluções diferentes.")
        size = (width, height)
        dxt1.decode_dds(blob)
    plane = folder / raf.MODEL_REAL_FILE
    if not mdx.parse(plane.read_bytes()):
        raise ValueError("O modelo MDX está vazio.")
    scene = ET.parse(folder / "index.xml").getroot()
    weights = [float(case.attrib["wait"]) for case in scene.findall("./sequence/case")]
    names = [f.name for f in files]
    if len(weights) != len(files) or not 0.02 <= weights[0] <= 5 or (
            ET.tostring(scene) != ET.tostring(ET.fromstring(raf.build_scene(names, weights[0])))):
        raise ValueError("A sequência index.xml diverge do formato da cena RAF.")
    if {p.name for p in folder.iterdir()} != {*names, "index.xml", raf.MODEL_REAL_FILE}:
        raise ValueError("A cena RAF contém arquivos inesperados.")
    return {"size": size, "frames": len(files), "ddsBytes": total, "limitBytes": RAF_SIZE_LIMIT,
            "wait": weights[0], "fps": 1 / weights[0], "duration": sum(weights)}


def build_report(project: Project, build_id: str, checks: list[dict], files: dict | None = None,
                 raf_info: dict | None = None, details: dict | None = None) -> dict:
    files = files or {}
    errors = [check["message"] for check in checks if check["status"] == "error"]
    warnings = [check["message"] for check in checks if check["status"] == "warning"]
    return {"appVersion": VERSION, "schemaVersion": SCHEMA_VERSION, "timestamp": now(), "buildId": build_id,
            "projectId": project.id, "revision": project.revision, "assetHashes": {
                key: asset.sha256 for key, asset in project.assets.items()},
            "files": files, "totalBytes": sum(f["bytes"] for f in files.values()), "raf": raf_info,
            "checks": checks, "status": "error" if errors else "valid",
            "warnings": warnings, "errors": errors, "consoleTests": project.settings.consoleTests
                and [test.model_dump() for test in project.settings.consoleTests] or [],
            "audio": (details or {}).get("audio", {}), "xmlValidation": (details or {}).get("xml", {}),
            "packageValidation": {"built": False, "status": "unverified"}, "packagingBackend": "external",
            "testMatrix": {"unit": "unverified", "structural": "failed" if errors else "passed" if files else "unverified",
                           "parser": (details or {}).get("parser", "unverified"),
                           "package": "unverified", "physical": "manual"}}


def validate_tree(root: Path, project: Project, build_id: str, expected_meta: dict,
                  notes: list[dict], progress=lambda: None) -> dict:
    base = root / "Image0"
    animated = project.settings.mode == "animated"
    checks = notes.copy()
    files, audio = {}, {}
    xml = {}
    raf_info = None

    def check(category, success, function):
        try:
            result = function()
        except (KeyError, OSError, ValueError, IndexError, StopIteration, AttributeError, EOFError, struct.error, ET.ParseError, wave.Error, StudioError):
            checks.append(item(category, "error", f"Falha na verificação de {success}."))
            return None
        checks.append(item(category, "valid", success))
        return result

    expected = {"Image0/theme.xml", "Sc0/param.sfo"}
    pngs = {"Sc0/pic1.png": PIC1_SIZE}
    for slot, spec in IMAGE_SLOTS.items():
        if animated and spec.get("hidden_by_raf"):
            continue
        if spec["group"] != "background" and slot not in project.settings.icons:
            continue
        path = spec["path"]
        pngs[f"Sc0/{Path(path).name}" if slot == "icon0" else f"Image0/{path}"] = spec["size"]
        if "glow_path" in spec:
            pngs[f"Image0/{spec['glow_path']}"] = spec["glow_size"]
    expected.update(pngs)
    for rel, size in pngs.items():
        progress()
        def inspect_png():
            with Image.open(root / rel, formats=["PNG"]) as image:
                assert_png = image.size == size and image.mode == "RGBA"
                if not assert_png:
                    raise ValueError("PNG")
                image.load()
                return {"dimensions": list(image.size), "alpha": image.getchannel("A").getextrema()[0] < 255}
        kind = "Ícones" if "_icon/" in rel else "Fundo"
        metrics = check(kind, rel, inspect_png)
        if metrics:
            files[rel] = metrics

    if not project.settings.icons:
        checks.append(item("Ícones", "valid", "Slots vazios mantêm os ícones do sistema."))

    if animated:
        scene_path = base / THEME_PATHS["raf_dir"]
        raf_info = check("RAF", "DDS, plane.mdx e index.xml", lambda: raf_metadata(scene_path, progress))
        if raf_info:
            expected.update(f"Image0/{THEME_PATHS['raf_dir']}/{name}" for name in
                            [*(theme.frame_name(i) for i in range(raf_info["frames"])), "plane.mdx", "index.xml"])
    else:
        checks.append(item("RAF", "valid", "Tema estático, sem cena RAF."))

    paths = {key: spec["path"] for key, spec in AUDIO_SLOTS.items()}
    for key in project.settings.audio:
        relative = paths[key]
        if key == "bgm" and project.assets[project.settings.audio[key].asset].kind != "at9":
            prepared = "audio-preparado/bgm_home.wav"

            def inspect_prepared():
                path = root / prepared
                with wave.open(str(path)) as pcm:
                    if (pcm.getnchannels(), pcm.getsampwidth(), pcm.getframerate()) != (2, 2, 48000):
                        raise ValueError("PCM")
                    return {"codec": "PCM", "sampleRate": 48000, "duration": pcm.getnframes() / 48000,
                            "pendingAtrac9": True, "bytes": path.stat().st_size, "sha256": sha256(path)}

            details = check("Áudio", "WAV estéreo para conversão externa", inspect_prepared)
            if details:
                audio[key] = details
                files[prepared] = {k: details[k] for k in ("bytes", "sha256")}
            continue
        expected.add("Image0/" + relative)
        def inspect_audio():
            path = base / relative
            if key != "bgm":
                pcm, rate = vag_data(path, require_theme=True)
                return {"codec": "VAG", "sampleRate": rate, "duration": len(pcm) / rate}
            streams, duration = probe(path, detected(path))
            stream = next(s for s in streams if s["codec_type"] == "audio")
            rate = int(stream["sample_rate"])
            duration = float(stream.get("duration") or duration)
            if (stream["codec_name"] != "atrac9" or rate != 48000 or
                not 139000 <= int(stream.get("bit_rate", 0)) <= 147000 or not at9_loop(path, duration, rate)):
                raise ValueError("AT9")
            return {"codec": "ATRAC9", "sampleRate": rate, "duration": duration, "wholeLoop": True}
        details = check("Áudio", relative, inspect_audio)
        if details:
            audio[key] = details
    if not paths.keys() & project.settings.audio.keys():
        checks.append(item("Áudio", "valid", "Nenhuma faixa personalizada."))

    def check_xml():
        node = ET.parse(base / "theme.xml").getroot()
        bgm = "Image0/" + paths["bgm"] in expected
        appearance = project.settings.appearance
        values = theme_xml.build_theme_xml(label=project.settings.name, themecolor=appearance.themecolor,
                                           fontcolor=appearance.fontcolor, focuscolor=appearance.focuscolor,
                                           homebgm_enable=bgm)
        if ET.tostring(node) != ET.tostring(ET.fromstring(values)):
            raise ValueError("theme.xml")
        return True
    xml["theme.xml"] = bool(check("theme.xml", "versão, cores e homebgm-enable", check_xml))

    def check_sfo():
        if sfo.parse((root / "Sc0/param.sfo").read_bytes()) != expected_meta:
            raise ValueError("param.sfo")
        return True
    check("param.sfo", "metadados do tema e IRO_TAG=2", check_sfo)
    actual = {f.relative_to(root).as_posix(): f for directory in ("Image0", "Sc0")
              for f in (root / directory).rglob("*") if f.is_file()}
    if set(actual) != expected:
        checks.append(item("Estrutura", "error", "Há arquivos ausentes ou inesperados em Image0/Sc0."))
    else:
        checks.append(item("Estrutura", "valid", "Caminhos literais e inventário da árvore."))
    for name, path in sorted(actual.items()):
        files.setdefault(name, {}).update(bytes=path.stat().st_size, sha256=sha256(path))

    def check_gp4():
        doc = ET.parse(root / "theme.gp4").getroot()
        sources = {n.attrib["targ_path"]: n.attrib["orig_path"].replace(chr(92), "/")
                   for n in doc.findall("./files/file")}
        mappings = {f"sce_sys/{name[4:]}" if name.startswith("Sc0/") else name[7:]: name for name in actual}
        if (doc.findtext("./volume/volume_type") != "pkg_ps4_theme" or
                doc.find("./volume/package").get("content_id") != expected_meta["CONTENT_ID"] or sources != mappings):
            raise ValueError("GP4")
        return True
    xml["theme.gp4"] = bool(check("Estrutura", "GP4 e caminhos de origem", check_gp4))
    if animated:
        xml["index.xml"] = bool(raf_info)
    checks.append(item("Pacote", "unverified", "O bundle está separado da etapa que cria o .pkg."))
    return build_report(project, build_id, checks, files, raf_info,
                        {"audio": audio, "xml": xml, "parser": "passed" if all(xml.values()) and
                         not any(c["status"] == "error" for c in checks) else "failed"})
