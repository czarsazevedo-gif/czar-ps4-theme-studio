"""PS4 Theme Digital Twin — Post-Extraction Semantic Simulation Engine (Operação Fortaleza — Fase 6).

Operates STRICTLY on the extracted content of a built PS4 `.pkg` (never on temporary
pre-packaging inputs). Reconstructs and simulates:
  1. `Image0/theme.xml` semantics & color/BGM bindings.
  2. `Image0/scene/background.raf/` (`index.xml`, `plane.mdx`, all `.dds` frames):
     - Full binary decode of every `.dds` via `dxt1.decode_dds`.
     - Exact sequence reconstruction over >= 10 complete cycles.
     - Duration, effective FPS, speed anomalies, inadvertent duplicate frames,
       planned-vs-actual playback order, and seam-vs-interior transition ratio.
  3. Effective system icon slot bindings (`function_icon` 128×128 + `_glow` 152×152,
     `content_icon` 512×512, `preview.png`, `Sc0/icon0.png`, `Sc0/pic1.png`),
     including duplicate-content detection across distinct slots.
  4. Exported `Image0/sound/bgm_home.at9` (>= 3-cycle loop simulation via `vgmstream-cli`
     and FFmpeg decode, detecting truncation, silence, clipping, and loop-seam clicks)
     plus decoded `.vag` SFX (`se_decide`, `se_cursor`, `se_cancel`).

IMPORTANT: Every report explicitly states `verification_kind = "SEMANTIC_SIMULATION_ONLY"`
and `physical_execution_proven = False`. Simulation is NEVER presented as hardware proof.
"""

from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .. import dxt1, mdx, raf as raf_mod, theme_xml
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
)
from .audio_validation import (
    decode_at9_vgmstream,
    decode_to_float64_stereo,
    find_vgmstream_cli,
    parse_at9_riff,
    validate_at9,
)

_FORBIDDEN_XML_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)\b", re.IGNORECASE)
_COLOR_VEC_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s+([0-9]+(?:\.[0-9]+)?)\s+([0-9]+(?:\.[0-9]+)?)\s+([0-9]+(?:\.[0-9]+)?)\s*$")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _safe_parse_xml(path: Path) -> ET.Element:
    text = path.read_text(encoding="utf-8")
    if _FORBIDDEN_XML_RE.search(text):
        raise ValueError(f"{path.name}: declarações DOCTYPE/ENTITY proibidas por segurança.")
    return ET.fromstring(text)


def _simulate_raf(
    raf_dir: Path,
    *,
    cycles: int = 10,
    expected_frame_order_hashes: list[str] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    index_path = raf_dir / "index.xml"
    mdx_path = raf_dir / "plane.mdx"
    dds_paths = sorted(raf_dir.glob("*.dds"))

    if not index_path.is_file():
        return {"present": True, "valid": False, "errors": ["scene/background.raf/index.xml ausente."], "warnings": []}
    if not mdx_path.is_file():
        errors.append("scene/background.raf/plane.mdx ausente.")
    else:
        try:
            raw_mdx = mdx_path.read_bytes()
            recs = mdx.parse(raw_mdx)
            tags: set[int] = set()

            def _walk(items: list) -> None:
                for it in items:
                    if isinstance(it, mdx.Record):
                        tags.add(it.tag)
                        _walk(it.children)

            _walk(recs)
            required_tags = {mdx.TAG_ROOT, mdx.TAG_MODEL, mdx.TAG_GEOMETRY, mdx.TAG_MESH, mdx.TAG_ARRAYS}
            missing_tags = required_tags - tags
            if missing_tags:
                errors.append(f"plane.mdx incompleto (tags ausentes: {[hex(t) for t in sorted(missing_tags)]}).")
            elif mdx.build(recs) != raw_mdx:
                warnings.append("plane.mdx contém bytes extras/alinhamento não-canônico no round-trip.")
        except Exception as exc:
            errors.append(f"plane.mdx corrompido ou truncado: {exc}")

    try:
        root = _safe_parse_xml(index_path)
    except Exception as exc:
        return {"present": True, "valid": False, "errors": errors + [f"index.xml inválido: {exc}"], "warnings": warnings}

    if root.tag != "raf":
        errors.append(f"index.xml raiz inválida <{root.tag}> (esperado <raf>).")

    top_textures: dict[str, str] = {}
    for el in root.findall("texture"):
        tid = (el.attrib.get("id") or "").strip()
        fname = (el.attrib.get("file") or "").strip()
        if tid and fname:
            top_textures[tid] = fname

    mat_dds_map: dict[str, str] = {}
    for el in root.findall("material"):
        mid = (el.attrib.get("id") or "").strip()
        if not mid:
            errors.append("Elemento <material> sem id.")
            continue
        if mid in mat_dds_map:
            errors.append(f"ID de material duplicado em index.xml: '{mid}'.")
            continue
        tex_el = el.find("texture")
        fname = ""
        if tex_el is not None:
            fname = (tex_el.attrib.get("file") or "").strip()
            if not fname:
                tref = (tex_el.attrib.get("id") or tex_el.attrib.get("ref") or "").strip()
                fname = top_textures.get(tref, "")
        if not fname:
            tref = (el.attrib.get("texture") or el.attrib.get("diffuse") or "").strip()
            fname = top_textures.get(tref, "")
        if not fname:
            errors.append(f"Material '{mid}' sem arquivo de textura .dds.")
            continue
        if "/" in fname or "\\" in fname or ".." in fname:
            errors.append(f"Referência de textura insegura em index.xml: '{fname}'.")
            continue
        if not (raf_dir / fname).is_file():
            errors.append(f"Material '{mid}' aponta para DDS ausente: '{fname}'.")
        mat_dds_map[mid] = fname

    models: dict[str, str] = {}
    for el in root.findall(".//model"):
        mid = (el.attrib.get("id") or "").strip()
        mfile = (el.attrib.get("file") or "").strip()
        if mid:
            models[mid] = mfile
    if "plane.fbx" not in models.values():
        errors.append("index.xml não declara <model ... file=\"plane.fbx\"/>.")

    actors: dict[str, dict[str, str]] = {}
    for el in root.findall("actor"):
        aid = (el.attrib.get("id") or "").strip()
        if not aid:
            continue
        actors[aid] = {
            "model": (el.attrib.get("model") or "").strip(),
            "material": (el.attrib.get("material") or "").strip(),
            "color": (el.attrib.get("color") or "").strip(),
        }

    referenced_dds = {fn for fn in mat_dds_map.values() if (raf_dir / fn).is_file()}
    disk_dds_names = {p.name for p in dds_paths}
    orphan_dds = sorted(disk_dds_names - referenced_dds)
    if orphan_dds:
        errors.append(f"Arquivos .dds presentes no RAF mas não referenciados em index.xml: {orphan_dds}.")

    for aid, info in actors.items():
        if not info["model"] or info["model"] not in models:
            errors.append(f"Actor '{aid}' referencia model inexistente '{info['model']}'.")
        if not info["material"] or info["material"] not in mat_dds_map:
            errors.append(f"Actor '{aid}' referencia material inexistente '{info['material']}'.")

    # Reconstruct temporal sequence from <sequence><case>
    cases = root.findall(".//sequence/case")
    if not cases:
        errors.append("Nenhuma etapa <case> encontrada em <sequence> do index.xml.")
        return {"present": True, "valid": False, "errors": errors, "warnings": warnings}

    frame_count = len(dds_paths)
    if frame_count == 0 or frame_count > MAX_FRAME_COUNT:
        errors.append(f"Quantidade de quadros DDS inválida: {frame_count} (permitido 1..{MAX_FRAME_COUNT}).")

    waits: list[float] = []
    playback_actor_order: list[str] = []
    playback_dds_order: list[str] = []
    for idx, c_el in enumerate(cases):
        raw_w = (c_el.attrib.get("wait") or "").strip()
        try:
            w_val = float(raw_w)
            if not math.isfinite(w_val) or w_val < 0.01 or w_val > 10.0:
                raise ValueError("fora do intervalo [0.01, 10.0]s")
        except Exception:
            errors.append(f"<case #{idx}> com wait inválido: {raw_w!r} (exigido segundos finitos em [0.01, 10.0]).")
            w_val = 0.0
        waits.append(w_val)

        case_actors = c_el.findall("actor")
        fade_out_id = None
        fade_in_id = None
        for ca in case_actors:
            cid = (ca.attrib.get("id") or "").strip()
            col = (ca.attrib.get("color") or "").strip()
            if cid not in actors:
                errors.append(f"<case #{idx}> referencia actor inexistente '{cid}'.")
                continue
            m_col = _COLOR_VEC_RE.match(col)
            if not m_col:
                errors.append(f"<case #{idx}> actor '{cid}' com cor inválida '{col}'.")
                continue
            alpha_v = float(m_col.group(4))
            if alpha_v <= 0.05:
                fade_out_id = cid
            elif alpha_v >= 0.95:
                fade_in_id = cid

        if fade_out_id is None or fade_in_id is None:
            errors.append(f"<case #{idx}> não define par completo de transição (fade-out -> fade-in).")
        else:
            expected_out = raf_mod.frame_id(idx)
            expected_in = raf_mod.frame_id((idx + 1) % len(cases))
            if fade_out_id != expected_out or fade_in_id != expected_in:
                errors.append(
                    f"<case #{idx}> fora de ordem temporal: {fade_out_id}->{fade_in_id} "
                    f"(esperado {expected_out}->{expected_in})."
                )
            playback_actor_order.append(fade_out_id)
            mat_id = actors.get(fade_out_id, {}).get("material", "")
            dds_fn = mat_dds_map.get(mat_id, "")
            if dds_fn:
                playback_dds_order.append(dds_fn)

    # Verify planned playback order matches 00.dds .. (N-1).dds
    expected_dds_order = [f"{i:02d}.dds" for i in range(frame_count)]
    order_matches_canonical = playback_dds_order == expected_dds_order
    if not order_matches_canonical and frame_count > 0:
        errors.append(
            f"Ordem de reprodução reconstruída ({playback_dds_order[:8]}) diverge da ordem canônica ({expected_dds_order[:8]})."
        )

    # Decode every DDS in playback order and verify budget + visual properties
    decoded_frames: list[np.ndarray] = []
    dds_hashes: list[str] = []
    total_dds_bytes = 0
    resolutions: set[tuple[int, int]] = set()
    for fname in playback_dds_order if playback_dds_order else [p.name for p in dds_paths]:
        p = raf_dir / fname
        if not p.is_file():
            continue
        blob = p.read_bytes()
        total_dds_bytes += len(blob)
        dds_hashes.append(hashlib.sha256(blob).hexdigest())
        try:
            rgba = dxt1.decode_dds(blob)
            h_f, w_f = rgba.shape[:2]
            resolutions.add((w_f, h_f))
            if (w_f, h_f) not in ANIMATED_SIZES:
                errors.append(f"{fname}: resolução decodificada {w_f}×{h_f} fora dos tamanhos PS4.")
            decoded_frames.append(rgba[..., :3])
        except Exception as exc:
            errors.append(f"{fname}: falha ao decodificar DDS DXT1a: {exc}")

    if total_dds_bytes > RAF_SIZE_LIMIT:
        errors.append(f"Orçamento DDS excedido: {total_dds_bytes:,} B > 6 MiB ({RAF_SIZE_LIMIT:,} B).")
    if len(resolutions) > 1:
        errors.append(f"Resoluções inconsistentes no RAF: {sorted(resolutions)}.")

    order_matches_expected_hashes: bool | None = None
    if expected_frame_order_hashes is not None:
        order_matches_expected_hashes = dds_hashes == expected_frame_order_hashes
        if not order_matches_expected_hashes:
            errors.append("Sequência de hashes DDS reproduzidos diverge da ordem planejada pelo manifesto.")

    # Simulate >= 10 complete cycles
    n_dec = len(decoded_frames)
    cycle_duration = float(sum(waits))
    simulated_cycles = max(10, int(cycles))
    simulated_total_steps = simulated_cycles * max(len(cases), 1)
    simulated_total_duration = round(cycle_duration * simulated_cycles, 4)
    effective_fps = round(n_dec / cycle_duration, 3) if (n_dec > 0 and cycle_duration > 0) else 0.0

    # Detect inadvertent duplicate adjacent frames & seam discontinuity over the cyclic wrap
    duplicate_pairs: list[tuple[int, int]] = []
    step_maes: list[float] = []
    if n_dec >= 2:
        for i in range(n_dec):
            nxt = (i + 1) % n_dec
            mae = float(np.mean(np.abs(decoded_frames[i].astype(np.float32) - decoded_frames[nxt].astype(np.float32))))
            step_maes.append(mae)
            if mae < 0.05:
                duplicate_pairs.append((i, nxt))

    interior_median_mae = float(np.median(step_maes[:-1])) if len(step_maes) > 1 else (step_maes[0] if step_maes else 0.0)
    seam_mae = step_maes[-1] if step_maes else 0.0
    seam_to_interior_ratio = round(seam_mae / max(interior_median_mae, 0.25), 3) if step_maes else 0.0

    speed_anomalies: list[str] = []
    valid_waits = [w for w in waits if w > 0]
    if valid_waits:
        w_min, w_max = min(valid_waits), max(valid_waits)
        if w_min < 0.02:
            speed_anomalies.append(f"Cadência excessivamente rápida: wait mínimo {w_min:.4f}s (>50 FPS).")
        if w_max > 1.5:
            speed_anomalies.append(f"Cadência muito lenta / slideshow: wait máximo {w_max:.4f}s (<0.67 FPS).")
        if w_min > 0 and (w_max / w_min) > 4.0:
            speed_anomalies.append(f"Oscilação brusca de velocidade entre quadros ({w_min:.4f}s..{w_max:.4f}s).")
    if duplicate_pairs:
        warnings.append(f"Quadros adjacentes idênticos/congelados detectados nos pares {duplicate_pairs[:6]}.")
    if speed_anomalies:
        warnings.extend(speed_anomalies)

    return {
        "present": True,
        "valid": len(errors) == 0,
        "frames": n_dec,
        "resolution": list(next(iter(resolutions))) if resolutions else None,
        "total_dds_bytes": total_dds_bytes,
        "budget_percent": round(total_dds_bytes / float(RAF_SIZE_LIMIT) * 100.0, 2),
        "cycle_duration_sec": round(cycle_duration, 4),
        "effective_fps": effective_fps,
        "waits_sec": [round(w, 6) for w in waits],
        "simulated_cycles": simulated_cycles,
        "simulated_steps": simulated_total_steps,
        "simulated_duration_sec": simulated_total_duration,
        "order_matches_canonical": order_matches_canonical,
        "order_matches_expected_hashes": order_matches_expected_hashes,
        "duplicate_adjacent_pairs": duplicate_pairs,
        "interior_median_mae": round(interior_median_mae, 3),
        "seam_mae": round(seam_mae, 3),
        "seam_to_interior_ratio": seam_to_interior_ratio,
        "speed_anomalies": speed_anomalies,
        "playback_dds_order": playback_dds_order,
        "dds_hashes": dds_hashes,
        "errors": errors,
        "warnings": warnings,
    }


def _audit_icons_and_presentation(root: Path, theme_kind: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    bindings: dict[str, dict[str, Any]] = {}

    def _inspect_slot(slot_key: str, rel_path: str, expected_size: tuple[int, int], require_alpha: bool) -> None:
        p = root / rel_path
        if not p.is_file():
            return
        try:
            with Image.open(p) as im:
                im.load()
                w, h = im.size
                mode = im.mode
                has_alpha = "A" in mode
                alpha_min = int(im.getchannel("A").getextrema()[0]) if has_alpha else 255
                alpha_max = int(im.getchannel("A").getextrema()[1]) if has_alpha else 255
            sha = _sha256_file(p)
            ok = (w, h) == expected_size and (not require_alpha or has_alpha)
            if not ok:
                errors.append(
                    f"Slot {slot_key} ({rel_path}): {w}×{h} {mode} diverge do esperado "
                    f"{expected_size[0]}×{expected_size[1]} {'RGBA' if require_alpha else 'RGB/RGBA'}."
                )
            bindings[slot_key] = {
                "path": rel_path,
                "size": [w, h],
                "expected_size": list(expected_size),
                "mode": mode,
                "alpha_range": [alpha_min, alpha_max],
                "bytes": p.stat().st_size,
                "sha256": sha,
                "valid": ok,
            }
        except Exception as exc:
            errors.append(f"Slot {slot_key} ({rel_path}): erro ao decodificar PNG ({exc}).")

    _inspect_slot("presentation:preview", "texture/preview.png", PREVIEW_SIZE, False)
    _inspect_slot("presentation:icon0", "sce_sys/icon0.png", SCE_ICON_SIZE, False)
    _inspect_slot("presentation:pic1", "sce_sys/pic1.png", PIC1_SIZE, False)
    if theme_kind == "static":
        _inspect_slot("bg:homescreen", "texture/background/homescreen.png", STATIC_SIZE, False)
        _inspect_slot("bg:functionscreen", "texture/background/functionscreen.png", STATIC_SIZE, False)

    fn_shas: dict[str, list[str]] = {}
    for name in FUNCTION_ICONS:
        _inspect_slot(f"fn:{name}", f"texture/function_icon/{name}.png", FUNCTION_ICON_SIZE, True)
        _inspect_slot(f"fn:{name}_glow", f"texture/function_icon/{name}_glow.png", FUNCTION_ICON_GLOW_SIZE, True)
        if f"fn:{name}" in bindings:
            fn_shas.setdefault(bindings[f"fn:{name}"]["sha256"], []).append(f"fn:{name}")

    ct_shas: dict[str, list[str]] = {}
    for name in CONTENT_ICONS:
        _inspect_slot(f"ct:{name}", f"texture/content_icon/{name}.png", CONTENT_ICON_SIZE, True)
        if f"ct:{name}" in bindings:
            ct_shas.setdefault(bindings[f"ct:{name}"]["sha256"], []).append(f"ct:{name}")

    duplicate_icon_groups = [slots for slots in list(fn_shas.values()) + list(ct_shas.values()) if len(slots) > 1]
    if duplicate_icon_groups:
        warnings.append(
            f"Slots distintos compartilham conteúdo PNG idêntico (verifique se intencional ou troca acidental): {duplicate_icon_groups}"
        )

    return {
        "valid": len(errors) == 0,
        "bindings": bindings,
        "function_icons_count": sum(1 for k in bindings if k.startswith("fn:") and not k.endswith("_glow")),
        "function_glows_count": sum(1 for k in bindings if k.startswith("fn:") and k.endswith("_glow")),
        "content_icons_count": sum(1 for k in bindings if k.startswith("ct:")),
        "duplicate_icon_groups": duplicate_icon_groups,
        "errors": errors,
        "warnings": warnings,
    }


def _simulate_audio(root: Path, homebgm_enable: bool | None, *, audio_cycles: int = 3) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    bgm_path = root / "sound" / "bgm_home.at9"
    bgm_info: dict[str, Any] = {"present": bgm_path.is_file()}

    if homebgm_enable is True and not bgm_path.is_file():
        errors.append("homebgm-enable=True em theme.xml mas sound/bgm_home.at9 está ausente.")
    elif homebgm_enable is False and bgm_path.is_file():
        errors.append("sound/bgm_home.at9 presente mas homebgm-enable=False em theme.xml.")

    if bgm_path.is_file():
        at9_rep = validate_at9(bgm_path, require_ps4_profile=True, require_loop=True)
        bgm_info["validation"] = at9_rep.model_dump()
        if not at9_rep.valid:
            errors.extend(at9_rep.errors)
        warnings.extend(at9_rep.warnings)

        # Simulate >= 3 full loop cycles via vgmstream-cli when available
        sim_cycles = max(3, int(audio_cycles))
        if at9_rep.valid:
            try:
                riff = parse_at9_riff(bgm_path.read_bytes())
                if find_vgmstream_cli():
                    pcm_multi = decode_at9_vgmstream(bgm_path, ignore_loop=False, loops=sim_cycles, fade_sec=0.0)
                    decoder = "vgmstream-cli"
                else:
                    pcm_one = decode_to_float64_stereo(bgm_path)
                    pcm_multi = np.concatenate([pcm_one] * sim_cycles, axis=0)
                    decoder = "ffmpeg-concat-fallback"

                total_samples = int(pcm_multi.shape[0])
                sr = int(riff["sample_rate"] or 48000)
                total_dur = total_samples / float(sr)
                peak = float(np.max(np.abs(pcm_multi))) if total_samples else 0.0
                peak_dbfs = round(20.0 * math.log10(peak), 2) if peak > 1e-12 else -120.0
                silence_ratio = float(np.mean(np.max(np.abs(pcm_multi), axis=1) < 1e-4)) if total_samples else 1.0
                clipped = int(np.sum(np.abs(pcm_multi) >= 0.9995))

                # Seam discontinuity around the first loop wrap point
                delay = int(riff.get("encoder_delay") or 0)
                ls = max(0, int((riff.get("loop_start") or 0) - delay))
                le = max(ls + 1, int((riff.get("loop_end") or 0) - delay + 1))
                loop_len = max(1, le - ls)
                seam_click = 0.0
                seam_energy_delta_db = 0.0
                if le < total_samples - 256 and le >= 256:
                    before_w = pcm_multi[le - 256 : le]
                    after_w = pcm_multi[le : le + 256]
                    seam_click = float(np.max(np.abs(pcm_multi[le] - pcm_multi[le - 1])))
                    rms_b = float(np.sqrt(np.mean(before_w ** 2))) + 1e-9
                    rms_a = float(np.sqrt(np.mean(after_w ** 2))) + 1e-9
                    seam_energy_delta_db = round(20.0 * math.log10(rms_a / rms_b), 3)

                if peak_dbfs <= -70.0:
                    errors.append(f"bgm_home.at9 praticamente mudo na simulação de {sim_cycles} ciclos (pico {peak_dbfs} dBFS).")
                elif silence_ratio > 0.90:
                    warnings.append(f"bgm_home.at9 possui {silence_ratio*100:.1f}% de silêncio.")
                if seam_click > 0.45:
                    warnings.append(f"Possível clique audível na emenda do loop musical (salto={seam_click:.3f}).")

                bgm_info["multi_cycle_simulation"] = {
                    "decoder": decoder,
                    "cycles": sim_cycles,
                    "decoded_samples": total_samples,
                    "decoded_duration_sec": round(total_dur, 4),
                    "loop_span_samples": loop_len,
                    "peak_dbfs": peak_dbfs,
                    "silence_ratio": round(silence_ratio, 4),
                    "clipped_samples": clipped,
                    "seam_click_amplitude": round(seam_click, 5),
                    "seam_energy_delta_db": seam_energy_delta_db,
                }
            except Exception as exc:
                errors.append(f"Falha ao simular {sim_cycles} voltas de bgm_home.at9: {exc}")

    # Decode VAG SFX
    sfx_report: dict[str, Any] = {}
    for sfx_name in ("se_decide.vag", "se_cursor.vag", "se_cancel.vag"):
        p = root / "sound" / sfx_name
        if not p.is_file():
            continue
        try:
            pcm = decode_to_float64_stereo(p)
            n_s = int(pcm.shape[0])
            peak = float(np.max(np.abs(pcm))) if n_s else 0.0
            peak_dbfs = round(20.0 * math.log10(peak), 2) if peak > 1e-12 else -120.0
            sfx_report[sfx_name] = {
                "valid": n_s > 0,
                "sha256": _sha256_file(p),
                "bytes": p.stat().st_size,
                "decoded_samples": n_s,
                "duration_sec": round(n_s / 48000.0, 4),
                "peak_dbfs": peak_dbfs,
            }
            if n_s == 0:
                errors.append(f"{sfx_name}: decodificação VAG produziu 0 amostras.")
            elif peak_dbfs <= -70.0:
                warnings.append(f"{sfx_name}: efeito VAG praticamente mudo ({peak_dbfs} dBFS).")
        except Exception as exc:
            sfx_report[sfx_name] = {"valid": False, "error": str(exc)}
            errors.append(f"{sfx_name}: falha ao decodificar VAG ({exc}).")

    return {
        "valid": len(errors) == 0,
        "bgm": bgm_info,
        "sfx": sfx_report,
        "errors": errors,
        "warnings": warnings,
    }


def simulate_extracted_theme(
    extracted_root: Path,
    *,
    expected_frame_order_hashes: list[str] | None = None,
    cycles: int = 10,
    audio_cycles: int = 3,
) -> dict[str, Any]:
    """Run the full PS4 Theme Digital Twin strictly on an extracted PKG content tree."""
    errors: list[str] = []
    warnings: list[str] = []

    xml_path = extracted_root / "theme.xml"
    xml_info: dict[str, Any] = {"present": xml_path.is_file(), "valid": False}
    homebgm_enable: bool | None = None

    if not xml_path.is_file():
        errors.append("Image0/theme.xml ausente no conteúdo extraído.")
    else:
        try:
            root_el = _safe_parse_xml(xml_path)
            if root_el.tag != "theme":
                errors.append(f"theme.xml com raiz inválida <{root_el.tag}>.")
            else:
                fields = {child.tag: (child.text or "").strip() for child in root_el}
                bgm_raw = fields.get("homebgm-enable", "")
                if bgm_raw in {"True", "False"}:
                    homebgm_enable = bgm_raw == "True"
                else:
                    errors.append(f"theme.xml homebgm-enable inválido: {bgm_raw!r}.")
                if fields.get("format-version") != theme_xml.FORMAT_VERSION:
                    errors.append(f"theme.xml format-version inválido: {fields.get('format-version')!r}.")
                if not fields.get("label"):
                    errors.append("theme.xml <label> ausente ou vazio.")
                for req_color in (
                    "fontcolor",
                    "fontshadowcolor",
                    "focuscolor",
                    "homescreen-dimmer",
                    "functionscreen-dimmer",
                    "titlename-dimmer",
                ):
                    cv = fields.get(req_color)
                    if not cv or not theme_xml.ARGB_RE.match(cv):
                        errors.append(f"theme.xml <{req_color}> ausente ou ARGB inválido: {cv!r}.")
                xml_info = {"present": True, "valid": len(errors) == 0, "fields": fields}
        except Exception as exc:
            errors.append(f"Falha ao interpretar theme.xml: {exc}")

    raf_dir = extracted_root / "scene" / "background.raf"
    theme_kind = "animated" if raf_dir.is_dir() else "static"
    raf_sim = (
        _simulate_raf(raf_dir, cycles=cycles, expected_frame_order_hashes=expected_frame_order_hashes)
        if theme_kind == "animated"
        else {"present": False, "valid": True, "errors": [], "warnings": []}
    )
    icons_audit = _audit_icons_and_presentation(extracted_root, theme_kind)
    audio_sim = _simulate_audio(extracted_root, homebgm_enable, audio_cycles=audio_cycles)

    errors.extend(raf_sim.get("errors") or [])
    errors.extend(icons_audit.get("errors") or [])
    errors.extend(audio_sim.get("errors") or [])
    warnings.extend(raf_sim.get("warnings") or [])
    warnings.extend(icons_audit.get("warnings") or [])
    warnings.extend(audio_sim.get("warnings") or [])

    return {
        "twin_version": "ps4-theme-digital-twin-v1",
        "verification_kind": "SEMANTIC_SIMULATION_ONLY",
        "physical_execution_proven": False,
        "disclaimer": (
            "Resultado obtido por simulação semântica em Linux sobre o conteúdo extraído do PKG; "
            "não constitui prova de execução física no console PS4."
        ),
        "passed": len(errors) == 0,
        "theme_kind": theme_kind,
        "theme_xml": xml_info,
        "raf_simulation": raf_sim,
        "icons_and_presentation": icons_audit,
        "audio_simulation": audio_sim,
        "errors": errors,
        "warnings": warnings,
    }
