"""PS4 Theme Compatibility Contract (Operação Fortaleza — Fase 5).

Encodes conservative, evidence-backed semantic rules derived from:
  - Imxnxl/PS4-Ultimate-Theme-Creator (commit aa595aa6)
  - Sony PS4 Theme XML v4.0 / RAF / MDX / PSF v1.1 specifications
  - Empirical retail/FPKG theme binary inspection (e.g., ZenitsuSoul)

Explicitly documents divergences between community tools rather than treating
any single heuristic as universal truth.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .bundle_validator import validate_theme_bundle
from .contracts import (
    PS4_PKG_CONTENT_TYPE_AC,
    PS4_THEME_CATEGORY,
    PS4_THEME_IRO_TAG,
    PS4_THEME_VOLUME_TYPE,
)


@dataclass(frozen=True)
class ContractRule:
    id: str
    layer: str
    requirement: str
    source_provenance: str
    conservative_policy: str
    divergence_note: str = ""


PS4_COMPATIBILITY_CONTRACT_RULES: tuple[ContractRule, ...] = (
    ContractRule(
        id="PS4-CNT-001",
        layer="container",
        requirement="volume_type=pkg_ps4_theme, content_type=0x1B (AC), IRO_TAG=2 (0x98 e SFO), CATEGORY='ac'",
        source_provenance="Imxnxl/app/pkg.py + Sony PSF/CNT retail themes + LibOrbisPkg IROTag.SS_THEME",
        conservative_policy="Exigir coerência exata entre cabeçalho binário PKG (0x74=0x1B, 0x98=2), param.sfo (CATEGORY='ac', IRO_TAG=2, FORMAT='obs', sem APP_TYPE/APP_VER) e GP4 (volume_type=pkg_ps4_theme).",
        divergence_note="OrbisPkgTool/PkgReader.cs classifica CATEGORY='ac' como 'Dlc' se o prefixo não for IP9100 e o título não contiver 'Theme', ignorando IRO_TAG=2; o contrato segue o cabeçalho binário real (IRO_TAG=2 + CATEGORY='ac').",
    ),
    ContractRule(
        id="PS4-CNT-002",
        layer="container",
        requirement="Coerência estrita de CONTENT_ID (36 chars) e TITLE_ID (9 chars contido em CONTENT_ID)",
        source_provenance="Sony PS4 GP4/SFO specification + Imxnxl/app/pkg.py",
        conservative_policy="Rejeitar divergência entre CNT header, Sc0/param.sfo e GP4; rejeitar TITLE vazio.",
    ),
    ContractRule(
        id="PS4-TREE-001",
        layer="structure",
        requirement="Árvore canônica sem arquivos órfãos, duplicatas, symlinks ou path traversal",
        source_provenance="Sony PS4 Theme filesystem contract + GM-PKG-POST-001",
        conservative_policy="Permitir apenas caminhos canônicos de tema PS4 em Image0/ e Sc0/; rejeitar qualquer link simbólico ou arquivo fora da lista branca.",
    ),
    ContractRule(
        id="PS4-XML-001",
        layer="xml",
        requirement="Image0/theme.xml válido (format-version=4.0, themecolor 0..7, cores ARGB obrigatórias, label não vazio, sem DOCTYPE/ENTITY)",
        source_provenance="Imxnxl/app/theme_xml.py + Sony PS4 Theme XML v4.0",
        conservative_policy="Exigir presença explícita e formato válido de todas as tags obrigatórias (format-version, label, themecolor, fontcolor, fontshadowcolor, focuscolor, dimmers, homebgm-enable) e bloquear DOCTYPE/ENTITY.",
    ),
    ContractRule(
        id="PS4-RAF-001",
        layer="raf",
        requirement="RAF canônico com <= 48 quadros, <= 6 MiB de DDS DXT1a, resoluções permitidas, plane.mdx estrutural e index.xml semanticamente íntegro",
        source_provenance="Imxnxl/app/raf.py, mdx.py, dxt1.py, config.py",
        conservative_policy="Manter teto conservador de 6 MiB (6.291.456 B) e 48 quadros; validar parse binário integral de plane.mdx, referências 1:1 <texture>/<material>/<actor>/<case>, ordem temporal f00..f(N-1)->f00 e wait finito em segundos [0.01, 10.0].",
        divergence_note="Documentações comunitárias divergem sobre tetos de memória acima de 6 MiB e se 'wait' seria em milissegundos ou segundos; o contrato impõe wait estritamente em segundos (confirmado por temas reais e Imxnxl) e teto conservador de 6 MiB.",
    ),
    ContractRule(
        id="PS4-BG-001",
        layer="textures",
        requirement="Exclusividade estático/animado + apresentações obrigatórias (preview 740×416, icon0 512×512, pic1 1920×1080)",
        source_provenance="Imxnxl/app/theme.py + Sony PS4 Theme spec",
        conservative_policy="Temas dinâmicos NÃO podem incluir PNGs mortos em texture/background/; temas estáticos exigem homescreen.png e functionscreen.png em 1920×1080.",
    ),
    ContractRule(
        id="PS4-ICON-001",
        layer="icons",
        requirement="Ícones de função 128×128 RGBA pareados 1:1 com _glow 152×152 RGBA; ícones de conteúdo 512×512 RGBA",
        source_provenance="Imxnxl/app/config.py + Sony PS4 Theme spec",
        conservative_policy="Exigir assinatura PNG, integridade decodificável, dimensões exatas, canal alfa real e nomes estritamente nos 9 slots de função e 10 de conteúdo.",
    ),
    ContractRule(
        id="PS4-AUD-001",
        layer="audio",
        requirement="bgm_home.at9 ATRAC9 real 48 kHz estéreo 144 kbps (configData fe740bf0, fact + smpl playCount=0, loop dentro da faixa) coerente com homebgm-enable e bgmhome_loud",
        source_provenance="Imxnxl/app/at9.py (medido em tema real no console) + psgfx-at9 + vgmstream",
        conservative_policy="Exigir 144 kbps / fe740bf0 (não substituir por 168 kbps), validar loop_start/loop_end dentro do comprimento real decodificado e rejeitar contradição com homebgm-enable.",
        divergence_note="Alguns guias comunitários citam 168 kbps ou ausência de smpl; o contrato usa o perfil conservador 144 kbps + smpl playCount=0 comprovado em temas reais de varejo.",
    ),
    ContractRule(
        id="PS4-SFX-001",
        layer="audio",
        requirement="Efeitos se_decide.vag, se_cursor.vag e se_cancel.vag em Sony PS-ADPCM (VAGp, 48 kHz, decodificável por FFmpeg adpcm_psx)",
        source_provenance="Imxnxl/app/vag.py + Sony PS4 Theme spec",
        conservative_policy="Validar cabeçalho VAGp big-endian, taxa 48000 Hz, tamanho de payload não truncado e decodificação real adpcm_psx.",
    ),
)


def contract_catalog() -> list[dict[str, Any]]:
    """Return the full documented PS4 compatibility contract catalog."""
    return [asdict(r) for r in PS4_COMPATIBILITY_CONTRACT_RULES]


def evaluate_ps4_contract(
    extracted_content_dir: Path,
    *,
    pkg_header_meta: dict[str, Any] | None = None,
    gp4_path: Path | None = None,
) -> dict[str, Any]:
    """Evaluate an extracted PS4 theme directory (plus optional PKG header metadata) against the full contract."""
    bundle_rep = validate_theme_bundle(extracted_content_dir, gp4_path=gp4_path)
    rule_results: list[dict[str, Any]] = []
    errors: list[str] = list(bundle_rep.errors)
    warnings: list[str] = list(bundle_rep.warnings)

    # Container-level checks when PKG binary header metadata is provided
    cnt_ok = True
    cnt_details: list[str] = []
    if pkg_header_meta is not None:
        if pkg_header_meta.get("content_type_header") != PS4_PKG_CONTENT_TYPE_AC:
            cnt_ok = False
            cnt_details.append(f"content_type={pkg_header_meta.get('content_type_header')} != 0x1B")
        if pkg_header_meta.get("iro_tag_header") != PS4_THEME_IRO_TAG:
            cnt_ok = False
            cnt_details.append(f"iro_tag_header={pkg_header_meta.get('iro_tag_header')} != 2")
        if pkg_header_meta.get("iro_tag_sfo") != PS4_THEME_IRO_TAG:
            cnt_ok = False
            cnt_details.append(f"iro_tag_sfo={pkg_header_meta.get('iro_tag_sfo')} != 2")
        if pkg_header_meta.get("category") != PS4_THEME_CATEGORY:
            cnt_ok = False
            cnt_details.append(f"category={pkg_header_meta.get('category')!r} != 'ac'")
        if pkg_header_meta.get("volume_type") and pkg_header_meta.get("volume_type") != PS4_THEME_VOLUME_TYPE:
            cnt_ok = False
            cnt_details.append(f"volume_type={pkg_header_meta.get('volume_type')!r} != 'pkg_ps4_theme'")
        if not cnt_ok:
            errors.append("Contrato PS4-CNT-001 reprovado: " + "; ".join(cnt_details))

    checks_by_cat: dict[str, list[dict[str, Any]]] = {}
    for chk in bundle_rep.checks:
        checks_by_cat.setdefault(chk.category, []).append(chk.model_dump())

    def _cat_ok(*cats: str) -> bool:
        for c in cats:
            for item in checks_by_cat.get(c, []):
                if item["status"] == "fail":
                    return False
        return True

    mapping = {
        "PS4-CNT-001": cnt_ok and _cat_ok("sfo", "gp4", "metadata"),
        "PS4-CNT-002": _cat_ok("sfo", "metadata"),
        "PS4-TREE-001": _cat_ok("structure"),
        "PS4-XML-001": _cat_ok("xml"),
        "PS4-RAF-001": _cat_ok("raf"),
        "PS4-BG-001": _cat_ok("textures"),
        "PS4-ICON-001": _cat_ok("icons"),
        "PS4-AUD-001": all(
            item["status"] != "fail"
            for item in checks_by_cat.get("audio", [])
            if not item["id"].startswith("audio.se_")
        ),
        "PS4-SFX-001": all(
            item["status"] != "fail"
            for item in checks_by_cat.get("audio", [])
            if item["id"].startswith("audio.se_")
        ),
    }

    for rule in PS4_COMPATIBILITY_CONTRACT_RULES:
        ok = bool(mapping.get(rule.id, True))
        rule_results.append(
            {
                "id": rule.id,
                "layer": rule.layer,
                "status": "PASS" if ok else "FAIL",
                "requirement": rule.requirement,
                "source_provenance": rule.source_provenance,
                "divergence_note": rule.divergence_note,
            }
        )

    passed = len(errors) == 0 and all(r["status"] == "PASS" for r in rule_results)
    return {
        "contract_version": "ps4-theme-contract-v1",
        "verification_kind": "SEMANTIC_CONTRACT_VALIDATION",
        "physical_execution_proven": False,
        "passed": passed,
        "theme_kind": bundle_rep.theme_kind,
        "rules": rule_results,
        "errors": errors,
        "warnings": warnings,
        "file_count": len(bundle_rep.files),
    }
