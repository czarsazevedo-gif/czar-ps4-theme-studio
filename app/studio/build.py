from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageColor

from .. import dds, mdx, pkg, raf, theme, theme_xml, vag
from ..config import AUDIO_SLOTS, IMAGE_SLOTS, MAX_SFX_SECONDS, PIC1_SIZE, THEME_PATHS, dds_bytes_for
from . import media, sfo
from .schema import Project, Transform, Visual
from .storage import StudioError, sha256
from .validate import build_report, item, validate_tree

Progress = Callable[[str, int], None]


def json_file(path: Path, content: dict) -> None:
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_project(project: Project) -> list[dict]:
    settings = project.settings
    notes = []
    try:
        project.check_references(settings)
    except ValueError as exc:
        return [item("Estrutura", "error", str(exc))]
    if settings.mode == "static" and "home" not in settings.background and not any(
            layer.visible and layer.start == 0 for layer in settings.layers):
        notes.append(item("Fundo", "error", "Adicione uma imagem ou camada visível para o fundo inicial."))
    if settings.mode == "animated":
        animation = settings.animation
        if not animation.source:
            notes.append(item("RAF", "error", "Escolha um GIF animado ou vídeo como fonte."))
        else:
            if animation.budget()["status"] == "Inválido":
                notes.append(item("RAF", "error", "A composição ultrapassa o orçamento RAF ou tem menos de dois quadros."))
            try:
                animation.timestamps(project.assets[animation.source.asset].duration)
            except ValueError as exc:
                notes.append(item("RAF", "error", str(exc)))
            if "home" not in settings.background:
                notes.append(item("Fundo", "warning", "A capa será derivada do primeiro quadro da fonte."))
    for key, track in settings.audio.items():
        asset = project.assets[track.asset]
        try:
            media.audio_interval(asset, track, MAX_SFX_SECONDS if key != "bgm" else media.MAX_DURATION)
        except StudioError as exc:
            notes.append(item("Áudio", "error", exc.message))
        if key == "bgm":
            if asset.kind != "at9":
                notes.append(item("Áudio", "warning", "A música será exportada em WAV para conversão externa. O tema está sem música personalizada até reimportar o AT9."))
            elif track.start or track.end is not None or track.gain or track.fadeIn or track.fadeOut or track.normalize:
                notes.append(item("Áudio", "error", "Restaure os ajustes do AT9 ou importe PCM para preparar uma nova conversão."))
    try:
        package_meta(project).validate()
        sfo.encode(package_meta(project))
    except (ValueError, pkg.PackageError):
        notes.append(item("param.sfo", "error", "Verifique Title ID, Content ID e o comprimento do título em UTF-8."))
    return notes


def package_meta(project: Project, loudness=None) -> pkg.PackageMeta:
    settings = project.settings
    return pkg.PackageMeta(settings.name, settings.titleId,
                           settings.contentId or pkg.default_content_id(settings.titleId, settings.name),
                           "ac", "0" * 32, bgm_loudness=loudness)


def visual_image(root: Path, visual: Visual) -> Image.Image:
    return media.image_at(root / "assets" / visual.asset)


def compose(project: Project, root: Path, size: tuple[int, int], base: Image.Image | None,
            edit: Transform | None, position=0, layers: dict[int, Image.Image] | None = None) -> Image.Image:
    rgba = (*ImageColor.getrgb(project.settings.appearance.background), 255)
    canvas = media.transform(base, size, edit, rgba) if base is not None else Image.new("RGBA", size, rgba)
    for index, layer in enumerate(project.settings.layers):
        if not layer.visible or position < layer.start or (layer.end is not None and position >= layer.end):
            continue
        image = layers[index] if layers is not None else (
            visual_image(root, layer.visual) if layer.visual else Image.new("RGBA", (16, 9), layer.color))
        canvas.alpha_composite(media.transform(image, size, layer.transform))
    return canvas


def render_raf(project: Project, root: Path, folder: Path, progress: Progress) -> None:
    animation = project.settings.animation
    budget = animation.budget()
    count, size = budget["frames"], tuple(budget["size"])
    times = animation.timestamps(project.assets[animation.source.asset].duration)
    if len(times) != count:
        raise ValueError("A amostragem diverge do orçamento.")
    folder.mkdir(parents=True)
    path = root / "assets" / animation.source.asset
    # The source is bounded before composition and only a few frames are in flight.
    layers = {i: visual_image(root, layer.visual) if layer.visual else Image.new("RGBA", (16, 9), layer.color)
              for i, layer in enumerate(project.settings.layers) if layer.visible}
    start, end = animation.interval(project.assets[animation.source.asset].duration)

    def images():
        for i, timestamp in enumerate(times):
            value = round(i / count * 56)
            progress("Extraindo quadros", 15 + value)
            # Near EOF there may be no frame immediately after the requested time.
            try:
                frame = media.extract_frame(path, timestamp, (min(1920, size[0] * 2), min(1080, size[1] * 2)))
            except StudioError as exc:
                if exc.code != "frame_missing":
                    raise
                frame = media.extract_frame(path, max(start, timestamp - min(0.25, (end - start) / 2)), size)
            progress("Compondo e redimensionando", 15 + value)
            yield np.asarray(compose(project, root, size, frame, animation.source.transform,
                                     i * budget["wait"], layers), dtype=np.uint8)

    names = []
    for i, blob in dds.encode_sequence(images()):
        progress("Comprimindo DDS", 15 + round((i + 1) / count * 56))
        if len(blob) != dds_bytes_for(*size):
            raise ValueError("Tamanho DDS inválido.")
        name = theme.frame_name(i)
        (folder / name).write_bytes(blob)
        names.append(name)
    theme.check_frame_count(len(names))
    theme.check_budget(sum(p.stat().st_size for p in folder.iterdir()))
    progress("Gerando MDX", 74)
    half_w, half_h = raf.plane_half_size()
    (folder / raf.MODEL_REAL_FILE).write_bytes(mdx.quad_model(node_name=raf.MODEL_ID, half_w=half_w, half_h=half_h))
    progress("Gerando RAF", 77)
    theme.write_index_xml(folder / "index.xml", raf.build_scene(names, budget["wait"]))


def write_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", compress_level=4)


def render_images(project: Project, root: Path, folder: Path) -> None:
    settings = project.settings
    home = settings.background.get("home")
    source = settings.animation.source if settings.mode == "animated" else None
    home_image = visual_image(root, home) if home else (
        media.extract_frame(root / "assets" / source.asset,
                            settings.animation.timestamps(project.assets[source.asset].duration)[0], PIC1_SIZE)
        if source else None)
    cover_edit = home.transform if home else source.transform if source else Transform()
    cover = compose(project, root, PIC1_SIZE, home_image, cover_edit)
    write_png(cover, folder / "Sc0/pic1.png")
    for key, spec in IMAGE_SLOTS.items():
        if settings.mode == "animated" and spec.get("hidden_by_raf"):
            continue
        background = spec["group"] == "background"
        visual = (settings.background if background else settings.icons).get(key)
        if not background and visual is None:
            continue
        target = folder / ("Sc0/icon0.png" if key == "icon0" else "Image0/" + spec["path"])
        if key == "home":
            output = cover
        elif visual:
            source_image = visual_image(root, visual)
            output = (compose(project, root, spec["size"], source_image, visual.transform)
                      if background else media.transform(source_image, spec["size"], visual.transform))
        else:
            output = cover if spec["size"] == cover.size else media.transform(cover, spec["size"], Transform())
        write_png(output, target)
        if "glow_path" in spec:
            write_png(media.icon_glow(output, spec["glow_size"], visual.glow),
                      folder / "Image0" / spec["glow_path"])


def render_audio(project: Project, root: Path, folder: Path, progress: Progress) -> float | None:
    loudness = None
    for key, track in project.settings.audio.items():
        progress("Processando áudio", 12)
        asset = project.assets[track.asset]
        source = root / "assets" / asset.id
        dest = folder / "Image0" / AUDIO_SLOTS[key]["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if key == "bgm" and asset.kind == "at9":
            shutil.copyfile(source, dest)
            loudness = asset.loudness
        elif key == "bgm":
            pcm = media.audio_pcm(source, asset, track, media.MAX_DURATION, 2)
            prepared = folder / "audio-preparado"
            prepared.mkdir()
            (prepared / "bgm_home.wav").write_bytes(media.wav_bytes(pcm, 2))
        else:
            pcm = media.audio_pcm(source, asset, track, MAX_SFX_SECONDS, 1)
            dest.write_bytes(vag.encode(pcm, 48000, key))
    return loudness


def zip_entries(output: Path, entries: dict[str, Path], *, stored=False, progress=None) -> None:
    method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(output, "w", method, compresslevel=None if stored else 4) as archive:
        for name, file in sorted(entries.items()):
            if progress:
                progress("Criando ZIP", 92)
            archive.write(file, name)


def execute(kind: str, project: Project, root: Path, work: Path, build_id: str, progress: Progress) -> dict:
    progress("Preparando projeto", 3)
    report_file = work / "validation-report.json"
    manifest = work / "manifest.json"
    json_file(manifest, project.model_dump(mode="json"))
    notes = check_project(project)
    report = build_report(project, build_id, notes)
    zip_name = None
    if kind in {"project", "diagnostic"}:
        json_file(report_file, report)
        entries = {"manifest.json": manifest, "validation-report.json": report_file}
        if kind == "project":
            entries.update({f"assets/{key}": root / "assets" / key for key in project.assets})
        else:
            last = [entry for entry in project.exports if entry.get("id") and entry.get("kind") in
                    {"validate", "raf", "tree", "assets", "bundle"}]
            if last:
                past = work.parent / last[-1]["id"]
                for name in ("validation-report.json", "logs.json"):
                    if (past / name).is_file():
                        entries[f"ultima-geracao/{name}"] = past / name
        zip_name = "projeto.czar.zip" if kind == "project" else "diagnostico.zip"
        zip_entries(work / zip_name, entries, stored=True, progress=progress)
    elif report["status"] != "error":
        progress("Renderizando fundos e ícones", 6)
        render_images(project, root, work)
        loudness = render_audio(project, root, work, progress)
        has_bgm = (work / "Image0" / AUDIO_SLOTS["bgm"]["path"]).is_file()
        if has_bgm and loudness is None:
            notes.append(item("Áudio", "warning", "O FFmpeg não retornou loudness para o AT9."))
        appearance = project.settings.appearance
        xml = theme_xml.build_theme_xml(label=project.settings.name, themecolor=appearance.themecolor,
                                       fontcolor=appearance.fontcolor, focuscolor=appearance.focuscolor,
                                       homebgm_enable=has_bgm)
        theme.write_index_xml(work / "Image0/theme.xml", xml)
        scene_dir = work / "Image0" / THEME_PATHS["raf_dir"]
        if project.settings.mode == "animated":
            progress("Preparando vídeo", 14)
            render_raf(project, root, scene_dir, progress)
        elif kind == "raf":
            notes.append(item("RAF", "error", "Selecione fundo animado para exportar RAF."))
        meta = package_meta(project, loudness)
        (work / "Sc0/param.sfo").write_bytes(sfo.encode(meta))
        mappings = {}
        for prefix, target_prefix in (("Image0", ""), ("Sc0", "sce_sys/")):
            for path in (work / prefix).rglob("*"):
                if path.is_file():
                    mappings[target_prefix + path.relative_to(work / prefix).as_posix()] = path.relative_to(work).as_posix()
        theme.write_index_xml(work / "theme.gp4", pkg.build_gp4(meta, sorted(mappings), ".", source_paths=mappings))
        progress("Validando", 80)
        expected_meta = {"CATEGORY": "ac", "IRO_TAG": 2, "TITLE": meta.title,
                         "TITLE_ID": meta.title_id, "CONTENT_ID": meta.content_id, "CONTENT_VER": "01.00"}
        decoded = sfo.parse((work / "Sc0/param.sfo").read_bytes())
        expected_meta = {**decoded, **expected_meta}
        report = validate_tree(work, project, build_id, expected_meta, notes, lambda: progress("Validando", 82))
        json_file(report_file, report)
        if report["status"] != "error" and kind != "validate":
            entries = {"validation-report.json": report_file}
            if kind == "raf":
                entries.update({f"{THEME_PATHS['raf_dir']}/{p.name}": p for p in scene_dir.iterdir()})
                zip_name = "background.raf.zip"
            else:
                for prefix in ("Image0", "Sc0", "audio-preparado"):
                    entries.update({p.relative_to(work).as_posix(): p for p in (work / prefix).rglob("*") if p.is_file()})
                if kind == "assets":
                    entries.pop("Image0/theme.xml", None)
                    entries.pop("Sc0/param.sfo", None)
                if kind == "bundle":
                    entries.update({"theme.gp4": work / "theme.gp4", "manifest.json": manifest})
                    howto = work / "EMPACOTAMENTO.md"
                    howto.write_text(PACKAGING, encoding="utf-8")
                    entries[howto.name] = howto
                    hashes = work / "SHA256SUMS"
                    hashes.write_text("".join(f"{sha256(file)}  {name}\n" for name, file in sorted(entries.items())), encoding="utf-8")
                    entries[hashes.name] = hashes
                zip_name = {"tree": "arvore-do-tema.zip", "assets": "assets-processados.zip",
                            "bundle": "bundle-empacotamento.zip"}[kind]
            zip_entries(work / zip_name, entries, progress=progress)
    json_file(report_file, report)
    for name in ("Image0", "Sc0", "audio-preparado"):
        shutil.rmtree(work / name, ignore_errors=True)
    return {"report": report, "download": zip_name, "bytes": (work / zip_name).stat().st_size if zip_name else 0}


PACKAGING = """# Empacotamento externo

Este bundle contém os arquivos do tema, o GP4 e o relatório de validação estrutural.
Confira os hashes em SHA256SUMS. Image0 contém as entradas do volume e Sc0 é
mapeado para sce_sys no GP4. Preserve os caminhos relativos ao extrair no Windows.

Abra theme.gp4 em uma ferramenta de empacotamento compatível fornecida pelo
proprietário, com acesso legítimo. Confira volume_type=pkg_ps4_theme,
CATEGORY=ac, IRO_TAG=2 e o Content ID no pacote produzido. Valide o inventário
Image0/Sc0 e registre o SHA-256 do PKG antes do teste em console.

Se houver audio-preparado/bgm_home.wav, converta-o externamente para ATRAC9
a 48 kHz, 144 kbps e em loop completo. Importe o AT9 em Música do menu
no Studio e gere um novo bundle para incluir a música e seus metadados.

O relatório físico deve vincular o resultado ao Build ID e à versão do firmware.
"""
