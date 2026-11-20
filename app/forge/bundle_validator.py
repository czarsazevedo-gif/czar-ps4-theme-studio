"""CZAR Bundle Validator: comprehensive pre-PKG validation of canonical PS4 themes.

Validates every artifact against Imxnxl/PS4-Ultimate-Theme-Creator and Sony PS4
theme specifications before packaging:
- Image0/theme.xml (syntax, format-version 4.0, themecolor 0-7, ARGB, homebgm-enable consistency)
- Image0/texture/ (static vs animated exclusion, preview 740x416, function icons 128x128 + glow 152x152, content icons 512x512)
- Image0/scene/background.raf/ (DDS DXT1a headers/resolutions, <= 48 frames, <= 6 MiB DDS budget, plane.mdx, index.xml)
- Image0/sound/ (independent ATRAC9 validation for bgm_home.at9, VAGp + FFprobe adpcm_psx validation for se_{decide,cursor,cancel}.vag)
- Sc0/param.sfo (CATEGORY='ac', IRO_TAG=2, no APP_TYPE/APP_VER, IDs, bgmhome_loud consistency)
- Sc0/icon0.png (512x512) and Sc0/pic1.png (1920x1080)
- GP4 project XML (volume_type=pkg_ps4_theme, no chunk_info/storage_type, file list parity)
"""

from __future__ import annotations

import hashlib
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

from .. import mdx, media, pkg, theme_xml
from ..config import (
    ANIMATED_SIZES,
    CONTENT_ICONS,
    CONTENT_ICON_SIZE,
    FUNCTION_ICONS,
    FUNCTION_ICON_GLOW_SIZE,
    FUNCTION_ICON_SIZE,
    MAX_FRAME_COUNT,
    PIC1_SIZE,
    PREVIEW_SIZE,
    RAF_SIZE_LIMIT,
    SCE_ICON_SIZE,
    STATIC_SIZE,
    dds_bytes_for,
)
from .audio_validation import validate_at9
from .contracts import (
    PS4_PKG_DEFAULT_PASSCODE,
    PS4_THEME_CATEGORY,
    PS4_THEME_IRO_TAG,
    PS4_THEME_VOLUME_TYPE,
    BundleCheckItem,
    BundleValidationReport,
)
from .sfo import parse_param_sfo

ALLOWED_SFX_NAMES = {"se_decide.vag", "se_cursor.vag", "se_cancel.vag"}
ALLOWED_FUNCTION_ICON_FILES = {f"{n}.png" for n in FUNCTION_ICONS} | {f"{n}_glow.png" for n in FUNCTION_ICONS}
ALLOWED_CONTENT_ICON_FILES = {f"{n}.png" for n in CONTENT_ICONS}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _check_png(
    path: Path,
    expected_size: tuple[int, int],
    *,
    require_alpha_channel: bool = False,
) -> tuple[bool, str]:
    if not path.is_file():
        return False, f"Arquivo ausente: {path.name}"
    raw = path.read_bytes()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return False, f"{path.name}: assinatura PNG inválida."
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            w, h = im.size
            mode = im.mode
    except Exception as exc:
        return False, f"{path.name}: PNG corrompido ({exc})."
    if (w, h) != expected_size:
        return False, f"{path.name}: dimensão {w}×{h} diverge do exigido {expected_size[0]}×{expected_size[1]}."
    if require_alpha_channel and "A" not in mode:
        return False, f"{path.name}: modo de cor '{mode}' sem canal alfa (esperado RGBA/LA)."
    if mode not in {"RGB", "RGBA", "P", "L", "LA"}:
        return False, f"{path.name}: modo de cor inesperado '{mode}'."
    return True, f"PNG íntegro {w}×{h} ({mode}, {len(raw):,} B)"


def _check_dds_dxt1(path: Path) -> tuple[bool, tuple[int, int] | None, str]:
    raw = path.read_bytes()
    if len(raw) < 128 or raw[:4] != b"DDS ":
        return False, None, f"{path.name}: cabeçalho DDS ausente ou truncado."
    dw_size, _flags, height, width = struct.unpack_from("<IIII", raw, 4)
    fourcc = raw[84:88]
    if dw_size != 124:
        return False, None, f"{path.name}: dwSize={dw_size} (esperado 124)."
    if fourcc != b"DXT1":
        return False, (width, height), f"{path.name}: FourCC={fourcc!r} (esperado b'DXT1')."
    if (width, height) not in ANIMATED_SIZES:
        return False, (width, height), f"{path.name}: resolução {width}×{height} fora das resoluções PS4."
    expected_len = dds_bytes_for(width, height)
    if len(raw) != expected_len:
        return False, (width, height), f"{path.name}: tamanho {len(raw)} B != esperado {expected_len} B."
    return True, (width, height), f"DDS DXT1 {width}×{height} ({len(raw):,} B)"


def _check_vag(path: Path) -> tuple[bool, str]:
    raw = path.read_bytes()
    if len(raw) < 64 or raw[:4] != b"VAGp":
        return False, f"{path.name}: cabeçalho VAGp inválido."
    data_len = struct.unpack_from(">I", raw, 12)[0]
    sample_rate = struct.unpack_from(">I", raw, 16)[0]
    if sample_rate != 48000:
        return False, f"{path.name}: taxa {sample_rate} Hz (esperado 48000 Hz)."
    if data_len == 0 or len(raw) < 48 + data_len:
        return False, f"{path.name}: bloco ADPCM truncado ({len(raw)} B)."
    try:
        info = media.probe_audio(path)
        if info.codec != "adpcm_psx":
            return False, f"{path.name}: FFprobe identificou '{info.codec}' em vez de 'adpcm_psx'."
    except media.MediaError as exc:
        return False, f"{path.name}: FFprobe falhou ({exc})."
    return True, f"VAG PS-ADPCM válido (48000 Hz, {len(raw):,} B)"


def validate_theme_bundle(
    content_dir: Path,
    *,
    meta: pkg.PackageMeta | None = None,
    gp4_path: Path | None = None,
) -> BundleValidationReport:
    """Run exhaustive validation on an assembled PS4 theme content directory."""
    checks: list[BundleCheckItem] = []
    errors: list[str] = []
    warnings: list[str] = []

    def add(
        cid: str,
        category: str,
        label: str,
        status: str,
        detail: str,
        rel_path: str | None = None,
    ) -> None:
        checks.append(
            BundleCheckItem(
                id=cid,
                category=category,  # type: ignore[arg-type]
                label=label,
                status=status,      # type: ignore[arg-type]
                detail=detail,
                path=rel_path,
            )
        )
        if status == "fail":
            errors.append(f"[{label}] {detail}")
        elif status == "warn":
            warnings.append(f"[{label}] {detail}")

    if not content_dir.is_dir():
        add("dir", "structure", "Diretório do tema", "fail", "Pasta de conteúdo do tema não encontrada.")
        return BundleValidationReport(
            valid=False,
            theme_kind="static",
            has_music=False,
            has_sfx=False,
            checks=checks,
            errors=errors,
            warnings=warnings,
        )

    # 1. Determine theme kind (static vs animated) & audio presence
    raf_dir = content_dir / "scene" / "background.raf"
    is_animated = raf_dir.is_dir()
    theme_kind = "animated" if is_animated else "static"
    bgm_path = content_dir / "sound" / "bgm_home.at9"
    has_music = bgm_path.is_file()
    sound_dir = content_dir / "sound"
    sfx_files = sorted(p for p in sound_dir.glob("*.vag")) if sound_dir.is_dir() else []
    sfx_slots = [p.stem for p in sfx_files if p.name in ALLOWED_SFX_NAMES]
    has_sfx = len(sfx_slots) > 0

    # 2. Metadata validation
    if meta is not None:
        try:
            meta.validate()
            if meta.mode != "theme":
                add("meta.mode", "metadata", "Modo de pacote", "fail", f"Modo '{meta.mode}' não é 'theme'.")
            elif meta.category != PS4_THEME_CATEGORY:
                add("meta.cat", "metadata", "CATEGORY", "fail", f"CATEGORY='{meta.category}' (exigido 'ac').")
            elif meta.profile.get("volume_type") != PS4_THEME_VOLUME_TYPE:
                add("meta.vol", "metadata", "volume_type", "fail", "volume_type precisa ser pkg_ps4_theme.")
            elif meta.profile.get("iro_tag") != PS4_THEME_IRO_TAG:
                add("meta.iro", "metadata", "IRO_TAG", "fail", "IRO_TAG precisa ser 2.")
            else:
                add(
                    "meta.ok",
                    "metadata",
                    "Metadados do tema",
                    "pass",
                    f"{meta.content_id} · {meta.title_id} · {PS4_THEME_VOLUME_TYPE} · CATEGORY=ac · IRO_TAG=2",
                )
            if meta.passcode != PS4_PKG_DEFAULT_PASSCODE:
                add(
                    "meta.passcode",
                    "metadata",
                    "Passcode",
                    "warn",
                    "Passcode difere dos 32 zeros canônicos esperados por instaladores FPKG no PS4.",
                )
        except pkg.PackageError as exc:
            add("meta.err", "metadata", "Metadados do tema", "fail", str(exc))

    # 3. Image0/theme.xml validation
    xml_path = content_dir / "theme.xml"
    homebgm_xml: bool | None = None
    if not xml_path.is_file():
        add("xml.missing", "xml", "Image0/theme.xml", "fail", "theme.xml ausente (o PS4 falha ao aplicar sem ele).", "Image0/theme.xml")
    else:
        try:
            tree = ET.fromstring(xml_path.read_text(encoding="utf-8"))
            if tree.tag != "theme":
                add("xml.root", "xml", "Image0/theme.xml", "fail", f"Raiz XML inválida: <{tree.tag}>.", "Image0/theme.xml")
            else:
                fv = (tree.findtext("format-version") or "").strip()
                tc_raw = (tree.findtext("themecolor") or "").strip()
                bgm_raw = (tree.findtext("homebgm-enable") or "").strip()
                xml_issues: list[str] = []
                if fv != theme_xml.FORMAT_VERSION:
                    xml_issues.append(f"format-version='{fv}' (esperado '{theme_xml.FORMAT_VERSION}')")
                if not tc_raw.isdigit() or not (0 <= int(tc_raw) <= 7):
                    xml_issues.append(f"themecolor='{tc_raw}' fora de 0..7")
                for color_tag in (
                    "fontcolor", "fontshadowcolor", "focuscolor",
                    "homescreen-dimmer", "functionscreen-dimmer", "titlename-dimmer",
                ):
                    cval = tree.findtext(color_tag)
                    if cval is not None and not theme_xml.ARGB_RE.match(cval.strip()):
                        xml_issues.append(f"{color_tag}='{cval}' não é ARGB #AARRGGBB")
                if bgm_raw not in {"True", "False"}:
                    xml_issues.append(f"homebgm-enable='{bgm_raw}' deve ser 'True' ou 'False'")
                else:
                    homebgm_xml = (bgm_raw == "True")
                if xml_issues:
                    add("xml.fields", "xml", "Image0/theme.xml", "fail", "; ".join(xml_issues), "Image0/theme.xml")
                else:
                    add(
                        "xml.ok",
                        "xml",
                        "Image0/theme.xml",
                        "pass",
                        f"XML válido (format-version={fv}, themecolor={tc_raw}, homebgm-enable={bgm_raw}).",
                        "Image0/theme.xml",
                    )
        except Exception as exc:
            add("xml.parse", "xml", "Image0/theme.xml", "fail", f"Erro ao interpretar XML: {exc}", "Image0/theme.xml")

    # XML <-> BGM consistency (never contradict XML!)
    if homebgm_xml is True and not has_music:
        add(
            "xml.bgm.contradict",
            "audio",
            "Consistência XML ↔ música",
            "fail",
            "theme.xml declara <homebgm-enable>True</homebgm-enable>, mas Image0/sound/bgm_home.at9 está ausente.",
            "Image0/sound/bgm_home.at9",
        )
    elif homebgm_xml is False and has_music:
        add(
            "xml.bgm.disabled",
            "audio",
            "Consistência XML ↔ música",
            "fail",
            "Image0/sound/bgm_home.at9 está presente, mas theme.xml declara <homebgm-enable>False</homebgm-enable>.",
            "Image0/sound/bgm_home.at9",
        )

    # 4. Backgrounds: static vs animated exclusivity
    bg_dir = content_dir / "texture" / "background"
    if is_animated:
        dead_bgs = sorted(bg_dir.glob("*")) if bg_dir.is_dir() else []
        if dead_bgs:
            add(
                "bg.dead",
                "textures",
                "Exclusividade estático/animado",
                "fail",
                f"Tema animado contém fundos estáticos inúteis em texture/background/: {[p.name for p in dead_bgs]}.",
                "Image0/texture/background/",
            )
        else:
            add(
                "bg.anim_clean",
                "textures",
                "Exclusividade estático/animado",
                "pass",
                "Tema animado sem PNGs mortos em texture/background/ (conforme padrão do console).",
            )
    else:
        for fname in ("homescreen.png", "functionscreen.png"):
            ok, msg = _check_png(bg_dir / fname, STATIC_SIZE)
            add(
                f"bg.{fname}",
                "textures",
                f"Fundo estático ({fname})",
                "pass" if ok else "fail",
                msg,
                f"Image0/texture/background/{fname}",
            )

    # 5. Thumbnail, icon0, pic1
    ok_prev, msg_prev = _check_png(content_dir / "texture" / "preview.png", PREVIEW_SIZE)
    add("tex.preview", "textures", "Miniatura (preview.png)", "pass" if ok_prev else "fail", msg_prev, "Image0/texture/preview.png")

    ok_icon0, msg_icon0 = _check_png(content_dir / "sce_sys" / "icon0.png", SCE_ICON_SIZE)
    add("sc0.icon0", "icons", "Sc0/icon0.png (512×512)", "pass" if ok_icon0 else "fail", msg_icon0, "Sc0/icon0.png")

    ok_pic1, msg_pic1 = _check_png(content_dir / "sce_sys" / "pic1.png", PIC1_SIZE)
    add("sc0.pic1", "textures", "Sc0/pic1.png (1920×1080)", "pass" if ok_pic1 else "fail", msg_pic1, "Sc0/pic1.png")

    # 6. Function icons (128x128 + 152x152 glow) & Content icons (512x512)
    fn_dir = content_dir / "texture" / "function_icon"
    if fn_dir.is_dir():
        fn_files = sorted(p for p in fn_dir.iterdir() if p.is_file())
        bad_fn = [p.name for p in fn_files if p.name not in ALLOWED_FUNCTION_ICON_FILES]
        if bad_fn:
            add("icons.fn.unknown", "icons", "Ícones de função", "fail", f"Arquivos fora dos slots canônicos: {bad_fn}", "Image0/texture/function_icon/")
        base_names = sorted({p.stem for p in fn_files if p.name in ALLOWED_FUNCTION_ICON_FILES and not p.stem.endswith("_glow")})
        glow_names = sorted({p.stem[:-5] for p in fn_files if p.name in ALLOWED_FUNCTION_ICON_FILES and p.stem.endswith("_glow")})
        if base_names != glow_names:
            add(
                "icons.fn.pair",
                "icons",
                "Pares ícone + glow",
                "fail",
                f"Divergência entre ícones base ({base_names}) e glow ({glow_names}).",
                "Image0/texture/function_icon/",
            )
        fn_errors = []
        for name in base_names:
            ok_i, msg_i = _check_png(fn_dir / f"{name}.png", FUNCTION_ICON_SIZE, require_alpha_channel=True)
            ok_g, msg_g = _check_png(fn_dir / f"{name}_glow.png", FUNCTION_ICON_GLOW_SIZE, require_alpha_channel=True)
            if not ok_i:
                fn_errors.append(msg_i)
            if not ok_g:
                fn_errors.append(msg_g)
        if fn_errors:
            add("icons.fn.check", "icons", "Ícones de função (128/152)", "fail", "; ".join(fn_errors), "Image0/texture/function_icon/")
        elif base_names:
            add("icons.fn.ok", "icons", "Ícones de função (128/152)", "pass", f"{len(base_names)} ícones + glow válidos (RGBA).", "Image0/texture/function_icon/")

    ct_dir = content_dir / "texture" / "content_icon"
    if ct_dir.is_dir():
        ct_files = sorted(p for p in ct_dir.iterdir() if p.is_file())
        bad_ct = [p.name for p in ct_files if p.name not in ALLOWED_CONTENT_ICON_FILES]
        if bad_ct:
            add("icons.ct.unknown", "icons", "Ícones de conteúdo", "fail", f"Arquivos fora dos slots canônicos: {bad_ct}", "Image0/texture/content_icon/")
        ct_errors = []
        valid_ct = [p for p in ct_files if p.name in ALLOWED_CONTENT_ICON_FILES]
        for p in valid_ct:
            ok_c, msg_c = _check_png(p, CONTENT_ICON_SIZE, require_alpha_channel=True)
            if not ok_c:
                ct_errors.append(msg_c)
        if ct_errors:
            add("icons.ct.check", "icons", "Ícones de conteúdo (512×512)", "fail", "; ".join(ct_errors), "Image0/texture/content_icon/")
        elif valid_ct:
            add("icons.ct.ok", "icons", "Ícones de conteúdo (512×512)", "pass", f"{len(valid_ct)} ícones 512×512 válidos (RGBA).", "Image0/texture/content_icon/")

    # 7. RAF animated scene validation (Section 14)
    if is_animated:
        dds_files = sorted(raf_dir.glob("*.dds"))
        plane_path = raf_dir / "plane.mdx"
        raf_xml_path = raf_dir / "index.xml"
        raf_errors: list[str] = []

        if not dds_files:
            raf_errors.append("Nenhum quadro .dds encontrado em scene/background.raf/.")
        elif len(dds_files) > MAX_FRAME_COUNT:
            raf_errors.append(f"{len(dds_files)} quadros excedem o teto canônico de {MAX_FRAME_COUNT}.")

        total_dds = 0
        resolutions: set[tuple[int, int]] = set()
        for idx, dds_p in enumerate(dds_files):
            expected_name = f"{idx:02d}.dds"
            if dds_p.name != expected_name:
                raf_errors.append(f"Quadro '{dds_p.name}' fora da sequência esperada '{expected_name}'.")
            ok_d, res, msg_d = _check_dds_dxt1(dds_p)
            if not ok_d:
                raf_errors.append(msg_d)
            if res is not None:
                resolutions.add(res)
            total_dds += dds_p.stat().st_size

        if len(resolutions) > 1:
            raf_errors.append(f"Quadros com resoluções misturadas no RAF: {sorted(resolutions)}.")
        if total_dds > RAF_SIZE_LIMIT:
            raf_errors.append(f"Soma dos DDS ({total_dds:,} B) excede o orçamento de 6 MiB ({RAF_SIZE_LIMIT:,} B).")

        if not plane_path.is_file() or not plane_path.read_bytes().startswith(mdx.MAGIC):
            raf_errors.append("plane.mdx ausente ou com cabeçalho XDM/MDX inválido.")

        if not raf_xml_path.is_file():
            raf_errors.append("scene/background.raf/index.xml ausente.")
        else:
            try:
                raf_xml_txt = raf_xml_path.read_text(encoding="utf-8")
                raf_root = ET.fromstring(raf_xml_txt)
                if raf_root.tag != "raf":
                    raf_errors.append(f"index.xml com raiz inválida <{raf_root.tag}>.")
                mats = raf_root.findall(".//material")
                cases = raf_root.findall(".//case")
                if len(mats) != len(dds_files):
                    raf_errors.append(f"index.xml declara {len(mats)} materiais para {len(dds_files)} arquivos .dds.")
                if len(cases) != len(dds_files):
                    raf_errors.append(f"index.xml declara {len(cases)} <case> para {len(dds_files)} arquivos .dds.")
                if 'file="plane.fbx"' not in raf_xml_txt:
                    raf_errors.append("index.xml não referencia o modelo canônico plane.fbx.")
            except Exception as exc:
                raf_errors.append(f"index.xml inválido: {exc}")

        if raf_errors:
            add("raf.check", "raf", "Cena animada (background.raf)", "fail", "; ".join(raf_errors), "Image0/scene/background.raf/")
        else:
            res_str = f"{next(iter(resolutions))[0]}×{next(iter(resolutions))[1]}" if resolutions else "?"
            add(
                "raf.ok",
                "raf",
                "Cena animada (background.raf)",
                "pass",
                f"{len(dds_files)} quadros DXT1a ({res_str}), {total_dds:,} B / 6 MiB, plane.mdx e index.xml válidos.",
                "Image0/scene/background.raf/",
            )

    # 8. Audio validation (sound/)
    if sound_dir.is_dir():
        allowed_sound = {"bgm_home.at9"} | ALLOWED_SFX_NAMES
        extra_sound = [p.name for p in sorted(sound_dir.iterdir()) if p.is_file() and p.name not in allowed_sound]
        if extra_sound:
            add("sound.unknown", "audio", "Pasta Image0/sound/", "fail", f"Arquivos inválidos em sound/: {extra_sound}", "Image0/sound/")
        if not has_music and not sfx_files and not extra_sound:
            add("sound.empty", "audio", "Pasta Image0/sound/", "warn", "Pasta sound/ vazia presente sem arquivos de áudio.", "Image0/sound/")

    if has_music:
        at9_rep = validate_at9(bgm_path, require_ps4_profile=True, require_loop=True)
        if at9_rep.valid:
            add(
                "audio.bgm",
                "audio",
                "Música do menu (bgm_home.at9)",
                "pass",
                f"ATRAC9 validado ({at9_rep.validator}): 48 kHz, estéreo, 144 kbps, "
                f"configData={at9_rep.config_data_hex}, loop smpl playCount={at9_rep.play_count}, "
                f"{at9_rep.duration_seconds:.2f}s.",
                "Image0/sound/bgm_home.at9",
            )
        else:
            add(
                "audio.bgm",
                "audio",
                "Música do menu (bgm_home.at9)",
                "fail",
                "; ".join(at9_rep.errors),
                "Image0/sound/bgm_home.at9",
            )

    for sfx_p in sfx_files:
        if sfx_p.name not in ALLOWED_SFX_NAMES:
            continue
        ok_v, msg_v = _check_vag(sfx_p)
        add(
            f"audio.{sfx_p.stem}",
            "audio",
            f"Efeito sonoro ({sfx_p.name})",
            "pass" if ok_v else "fail",
            msg_v,
            f"Image0/sound/{sfx_p.name}",
        )

    # 9. Sc0/param.sfo validation
    sfo_path = content_dir / "sce_sys" / "param.sfo"
    if not sfo_path.is_file():
        add("sfo.missing", "sfo", "Sc0/param.sfo", "fail", "sce_sys/param.sfo ausente.", "Sc0/param.sfo")
    else:
        try:
            sfo = parse_param_sfo(sfo_path.read_bytes())
            vals = sfo["values"]
            sfo_errs: list[str] = []
            if vals.get("CATEGORY") != PS4_THEME_CATEGORY:
                sfo_errs.append(f"CATEGORY={vals.get('CATEGORY')!r} (exigido 'ac')")
            if vals.get("IRO_TAG") != PS4_THEME_IRO_TAG:
                sfo_errs.append(f"IRO_TAG={vals.get('IRO_TAG')!r} (exigido 2)")
            if vals.get("FORMAT") != "obs":
                sfo_errs.append(f"FORMAT={vals.get('FORMAT')!r} (exigido 'obs')")
            for forbidden in ("APP_TYPE", "APP_VER"):
                if forbidden in vals:
                    sfo_errs.append(f"Chave de aplicativo '{forbidden}' não deve existir em tema PS4")
            cid = str(vals.get("CONTENT_ID") or "")
            tid = str(vals.get("TITLE_ID") or "")
            if not pkg.CONTENT_ID_RE.match(cid):
                sfo_errs.append(f"CONTENT_ID inválido: {cid!r}")
            if not pkg.TITLE_ID_RE.match(tid):
                sfo_errs.append(f"TITLE_ID inválido: {tid!r}")
            if tid and cid and tid not in cid:
                sfo_errs.append(f"CONTENT_ID ({cid}) não contém TITLE_ID ({tid})")
            if meta is not None:
                if cid != meta.content_id:
                    sfo_errs.append(f"CONTENT_ID no SFO ({cid}) difere do manifesto ({meta.content_id})")
                if tid != meta.title_id:
                    sfo_errs.append(f"TITLE_ID no SFO ({tid}) difere do manifesto ({meta.title_id})")
            pub = str(vals.get("PUBTOOLINFO") or "")
            if has_music and "bgmhome_loud=" not in pub:
                add(
                    "sfo.loud.warn",
                    "sfo",
                    "PUBTOOLINFO bgmhome_loud",
                    "warn",
                    "Música presente mas PUBTOOLINFO não declara bgmhome_loud.",
                    "Sc0/param.sfo",
                )
            if not has_music and "bgmhome_loud=" in pub:
                sfo_errs.append("PUBTOOLINFO declara bgmhome_loud mas o tema não possui música")

            if sfo_errs:
                add("sfo.check", "sfo", "Sc0/param.sfo", "fail", "; ".join(sfo_errs), "Sc0/param.sfo")
            else:
                add(
                    "sfo.ok",
                    "sfo",
                    "Sc0/param.sfo",
                    "pass",
                    f"PSF válido: CATEGORY=ac, IRO_TAG=2, CONTENT_ID={cid}, PUBTOOLINFO={pub}.",
                    "Sc0/param.sfo",
                )
        except Exception as exc:
            add("sfo.parse", "sfo", "Sc0/param.sfo", "fail", f"Falha ao ler param.sfo binário: {exc}", "Sc0/param.sfo")

    # 10. GP4 project XML validation (if provided)
    if gp4_path is not None:
        if not gp4_path.is_file():
            add("gp4.missing", "gp4", "Projeto GP4", "fail", "Arquivo .gp4 ausente.", gp4_path.name)
        else:
            try:
                gp4_txt = gp4_path.read_text(encoding="utf-8-sig")
                gp4_root = ET.fromstring(gp4_txt)
                gp4_errs: list[str] = []
                vol = gp4_root.find("volume")
                if vol is None:
                    gp4_errs.append("Elemento <volume> ausente.")
                else:
                    vt = (vol.findtext("volume_type") or "").strip()
                    if vt != PS4_THEME_VOLUME_TYPE:
                        gp4_errs.append(f"volume_type='{vt}' (exigido '{PS4_THEME_VOLUME_TYPE}')")
                    if vol.find("chunk_info") is not None:
                        gp4_errs.append("Temas não devem declarar <chunk_info>.")
                    pkg_el = vol.find("package")
                    if pkg_el is None:
                        gp4_errs.append("Elemento <package> ausente.")
                    else:
                        if "storage_type" in pkg_el.attrib:
                            gp4_errs.append("Temas não devem declarar storage_type no GP4.")
                        if not pkg.CONTENT_ID_RE.match(pkg_el.attrib.get("content_id", "")):
                            gp4_errs.append("content_id inválido no GP4.")
                        if not pkg.PASSCODE_RE.match(pkg_el.attrib.get("passcode", "")):
                            gp4_errs.append("passcode inválido no GP4.")
                files_el = gp4_root.find("files")
                declared_targets: set[str] = set()
                if files_el is None:
                    gp4_errs.append("Elemento <files> ausente.")
                else:
                    for f_el in files_el.findall("file"):
                        tp = f_el.attrib.get("targ_path", "").replace("\\", "/")
                        declared_targets.add(tp)
                        if not (content_dir / tp).is_file():
                            gp4_errs.append(f"Arquivo declarado no GP4 ausente em disco: {tp}")
                disk_targets = {
                    p.relative_to(content_dir).as_posix()
                    for p in content_dir.rglob("*")
                    if p.is_file()
                }
                undeclared = sorted(disk_targets - declared_targets)
                if undeclared:
                    gp4_errs.append(f"Arquivos em disco não listados no GP4: {undeclared}")
                if gp4_errs:
                    add("gp4.check", "gp4", "Projeto GP4", "fail", "; ".join(gp4_errs), gp4_path.name)
                else:
                    add(
                        "gp4.ok",
                        "gp4",
                        "Projeto GP4",
                        "pass",
                        f"GP4 válido ({PS4_THEME_VOLUME_TYPE}, {len(declared_targets)} arquivos sincronizados).",
                        gp4_path.name,
                    )
            except Exception as exc:
                add("gp4.parse", "gp4", "Projeto GP4", "fail", f"Erro ao ler GP4 XML: {exc}", gp4_path.name)

    # 11. Inventory & SHA-256 hashes of all bundle files
    files_manifest: list[dict] = []
    for p in sorted(content_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(content_dir).as_posix()
        pkg_partition = "Sc0" if rel in {"sce_sys/param.sfo", "sce_sys/icon0.png", "sce_sys/pic1.png"} else "Image0"
        pkg_path = (
            f"Sc0/{Path(rel).name}" if pkg_partition == "Sc0" else f"Image0/{rel}"
        )
        files_manifest.append(
            {
                "path": rel,
                "pkg_path": pkg_path,
                "partition": pkg_partition,
                "bytes": p.stat().st_size,
                "sha256": _sha256_file(p),
            }
        )

    return BundleValidationReport(
        valid=len(errors) == 0,
        theme_kind=theme_kind,  # type: ignore[arg-type]
        has_music=has_music,
        has_sfx=has_sfx,
        sfx_slots=sfx_slots,
        checks=checks,
        files=files_manifest,
        errors=errors,
        warnings=warnings,
    )
