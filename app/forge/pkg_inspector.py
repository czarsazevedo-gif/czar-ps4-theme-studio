"""Independent pure-Python PS4 PKG binary reader, cryptographic verifier, Sc0 extractor,
and bundle hash comparator.

Never relies on the PKG builder alone to self-approve its output: independently parses
the binary CNT header (including offset 0x98 IRO_TAG and 0x74 ContentType), verifies
SHA-256 digests over header/body/PFS/entries, extracts Sc0/param.sfo + icon0.png +
pic1.png directly from raw PKG bytes, and compares extracted Image0+Sc0 file hashes
byte-for-byte against the validated source theme bundle.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from .. import media, pkg
from .contracts import (
    PS4_PKG_CONTENT_TYPE_AC,
    PS4_PKG_DEFAULT_PASSCODE,
    PS4_THEME_CATEGORY,
    PS4_THEME_IRO_TAG,
    PS4_THEME_VOLUME_TYPE,
    PkgValidationReport,
)
from .sfo import parse_param_sfo
from .subproc import SubprocessTimeoutError, run_cancellable_process

CNT_MAGIC = b"\x7fCNT"
ENTRY_DIGESTS = 0x00000001
ENTRY_KEYS = 0x00000010
ENTRY_IMAGE_KEY = 0x00000020
ENTRY_GENERAL_DIGESTS = 0x00000080
ENTRY_METAS = 0x00000100
ENTRY_NAMES = 0x00000200
ENTRY_PARAM_SFO = 0x00001000
ENTRY_PIC1_PNG = 0x00001006
ENTRY_ICON0_PNG = 0x00001200

KNOWN_SC0_ENTRY_NAMES = {
    ENTRY_PARAM_SFO: "Sc0/param.sfo",
    ENTRY_PIC1_PNG: "Sc0/pic1.png",
    ENTRY_ICON0_PNG: "Sc0/icon0.png",
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _sha256_range(path: Path, offset: int, length: int) -> bytes:
    h = hashlib.sha256()
    remaining = length
    with path.open("rb") as fh:
        fh.seek(offset)
        while remaining > 0:
            chunk = fh.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.digest()


def parse_pkg_binary(pkg_path: Path) -> dict[str, Any]:
    """Independently parse raw PS4 PKG header, meta entry table, digests, and unencrypted Sc0 entries."""
    file_size = pkg_path.stat().st_size
    if file_size < 0x1100:
        raise ValueError(f"Arquivo PKG truncado ({file_size} bytes < 0x1100).")

    with pkg_path.open("rb") as fh:
        hdr = fh.read(0x1100)

    if hdr[0:4] != CNT_MAGIC:
        raise ValueError(f"Assinatura PKG inválida: {hdr[0:4]!r} (esperado b'\\x7fCNT').")

    flags = struct.unpack_from(">I", hdr, 0x04)[0]
    entry_count = struct.unpack_from(">I", hdr, 0x10)[0]
    sc_entry_count = struct.unpack_from(">H", hdr, 0x14)[0]
    entry_count_2 = struct.unpack_from(">H", hdr, 0x16)[0]
    entry_table_offset = struct.unpack_from(">I", hdr, 0x18)[0]
    main_ent_data_size = struct.unpack_from(">I", hdr, 0x1C)[0]
    body_offset = struct.unpack_from(">Q", hdr, 0x20)[0]
    body_size = struct.unpack_from(">Q", hdr, 0x28)[0]
    content_id = hdr[0x40:0x64].split(b"\x00", 1)[0].decode("ascii", "replace")
    drm_type = struct.unpack_from(">I", hdr, 0x70)[0]
    content_type = struct.unpack_from(">I", hdr, 0x74)[0]
    content_flags = struct.unpack_from(">I", hdr, 0x78)[0]
    promote_size = struct.unpack_from(">I", hdr, 0x7C)[0]
    version_date = struct.unpack_from(">I", hdr, 0x80)[0]
    version_hash = struct.unpack_from(">I", hdr, 0x84)[0]
    iro_tag = struct.unpack_from(">I", hdr, 0x98)[0]
    ekc_version = struct.unpack_from(">I", hdr, 0x9C)[0]

    sc_entries1_hash = hdr[0x100:0x120]
    sc_entries2_hash = hdr[0x120:0x140]
    digest_table_hash = hdr[0x140:0x160]
    body_digest = hdr[0x160:0x180]

    pfs_image_count = struct.unpack_from(">I", hdr, 0x404)[0]
    pfs_flags = struct.unpack_from(">Q", hdr, 0x408)[0]
    pfs_image_offset = struct.unpack_from(">Q", hdr, 0x410)[0]
    pfs_image_size = struct.unpack_from(">Q", hdr, 0x418)[0]
    mount_image_offset = struct.unpack_from(">Q", hdr, 0x420)[0]
    mount_image_size = struct.unpack_from(">Q", hdr, 0x428)[0]
    package_size = struct.unpack_from(">Q", hdr, 0x430)[0]
    pfs_signed_size = struct.unpack_from(">I", hdr, 0x438)[0]
    pfs_cache_size = struct.unpack_from(">I", hdr, 0x43C)[0]
    pfs_image_digest = hdr[0x440:0x460]
    pfs_signed_digest = hdr[0x460:0x480]

    header_digest_stored = hdr[0xFE0:0x1000]
    header_digest_calc = hashlib.sha256(hdr[0:0xFE0]).digest()
    header_signature = hdr[0x1000:0x1100]

    # Read entry metadata table
    entries: list[dict[str, Any]] = []
    with pkg_path.open("rb") as fh:
        fh.seek(entry_table_offset)
        raw_table = fh.read(entry_count * 32)
        if len(raw_table) < entry_count * 32:
            raise ValueError("Tabela de entradas PKG truncada.")
        for i in range(entry_count):
            eid, name_off, f1, f2, data_off, data_sz, _pad = struct.unpack_from(">IIIIIIQ", raw_table, i * 32)
            encrypted = bool(f2 & 0x80000000) or (eid in {0x400, 0x401} and bool(f1 & 0x80000000))
            entries.append(
                {
                    "index": i,
                    "id": eid,
                    "name_offset": name_off,
                    "flags1": f1,
                    "flags2": f2,
                    "data_offset": data_off,
                    "data_size": data_sz,
                    "encrypted": encrypted,
                }
            )

        # Resolve names from ENTRY_NAMES (0x200)
        names_blob = b""
        digests_blob = b""
        sc0_blobs: dict[str, bytes] = {}
        for ent in entries:
            if ent["data_offset"] + ent["data_size"] > file_size:
                raise ValueError(f"Entrada PKG 0x{ent['id']:04x} ultrapassa o tamanho do arquivo.")
            if ent["id"] == ENTRY_NAMES:
                fh.seek(ent["data_offset"])
                names_blob = fh.read(ent["data_size"])
            elif ent["id"] == ENTRY_DIGESTS:
                fh.seek(ent["data_offset"])
                digests_blob = fh.read(ent["data_size"])
            elif ent["id"] in KNOWN_SC0_ENTRY_NAMES:
                fh.seek(ent["data_offset"])
                sc0_blobs[KNOWN_SC0_ENTRY_NAMES[ent["id"]]] = fh.read(ent["data_size"])

        for ent in entries:
            name = ""
            if names_blob and ent["name_offset"] < len(names_blob):
                end = names_blob.find(b"\x00", ent["name_offset"])
                raw_n = names_blob[ent["name_offset"] : (end if end != -1 else len(names_blob))]
                name = raw_n.decode("ascii", "replace")
            ent["name"] = name or KNOWN_SC0_ENTRY_NAMES.get(ent["id"], f"0x{ent['id']:04x}")

    # Verify cryptographic SHA-256 digests independently
    body_digest_ok = (
        body_offset + body_size <= file_size
        and _sha256_range(pkg_path, body_offset, body_size) == body_digest
    )
    digest_table_ok = bool(digests_blob) and hashlib.sha256(digests_blob).digest() == digest_table_hash

    pfs_image_digest_ok = (
        pfs_image_size > 0
        and pfs_image_offset + pfs_image_size <= file_size
        and _sha256_range(pkg_path, pfs_image_offset, pfs_image_size) == pfs_image_digest
    )
    pfs_signed_digest_ok = (
        pfs_signed_size > 0
        and pfs_image_offset + pfs_signed_size <= file_size
        and _sha256_range(pkg_path, pfs_image_offset, pfs_signed_size) == pfs_signed_digest
    )

    # Verify per-entry SHA-256 digests against DIGESTS table
    entry_digest_mismatches: list[str] = []
    if digests_blob and len(digests_blob) >= entry_count * 32:
        for i, ent in enumerate(entries):
            if ent["id"] == ENTRY_DIGESTS:
                continue
            expected_d = digests_blob[i * 32 : (i + 1) * 32]
            if expected_d == b"\x00" * 32:
                continue
            actual_d = _sha256_range(pkg_path, ent["data_offset"], ent["data_size"])
            if actual_d != expected_d:
                entry_digest_mismatches.append(ent["name"])

    # Verify SC entries 1 & 2 hashes
    by_id = {e["id"]: e for e in entries}
    sc1_ok = False
    sc2_ok = False
    if all(eid in by_id for eid in (ENTRY_KEYS, ENTRY_IMAGE_KEY, ENTRY_GENERAL_DIGESTS, ENTRY_METAS, ENTRY_DIGESTS)):
        with pkg_path.open("rb") as fh:
            buf1 = bytearray()
            for eid in (ENTRY_KEYS, ENTRY_IMAGE_KEY, ENTRY_GENERAL_DIGESTS, ENTRY_METAS, ENTRY_DIGESTS):
                e = by_id[eid]
                fh.seek(e["data_offset"])
                buf1 += fh.read(e["data_size"])
            sc1_ok = hashlib.sha256(buf1).digest() == sc_entries1_hash

            buf2 = bytearray()
            for eid in (ENTRY_KEYS, ENTRY_IMAGE_KEY, ENTRY_GENERAL_DIGESTS, ENTRY_METAS):
                e = by_id[eid]
                sz = sc_entry_count * 32 if eid == ENTRY_METAS else e["data_size"]
                fh.seek(e["data_offset"])
                buf2 += fh.read(sz)
            sc2_ok = hashlib.sha256(buf2).digest() == sc_entries2_hash

    return {
        "file_size": file_size,
        "package_size": package_size,
        "flags": flags,
        "entry_count": entry_count,
        "sc_entry_count": sc_entry_count,
        "entry_count_2": entry_count_2,
        "entry_table_offset": entry_table_offset,
        "main_ent_data_size": main_ent_data_size,
        "body_offset": body_offset,
        "body_size": body_size,
        "content_id": content_id,
        "drm_type": drm_type,
        "content_type": content_type,
        "content_flags": content_flags,
        "promote_size": promote_size,
        "version_date": version_date,
        "version_hash": version_hash,
        "iro_tag": iro_tag,
        "ekc_version": ekc_version,
        "pfs_image_count": pfs_image_count,
        "pfs_flags": pfs_flags,
        "pfs_image_offset": pfs_image_offset,
        "pfs_image_size": pfs_image_size,
        "pfs_signed_size": pfs_signed_size,
        "header_digest_ok": header_digest_stored == header_digest_calc,
        "header_signature_present": any(b != 0 for b in header_signature),
        "body_digest_ok": body_digest_ok,
        "digest_table_ok": digest_table_ok,
        "pfs_image_digest_ok": pfs_image_digest_ok,
        "pfs_signed_digest_ok": pfs_signed_digest_ok,
        "sc_entries1_hash_ok": sc1_ok,
        "sc_entries2_hash_ok": sc2_ok,
        "entry_digest_mismatches": entry_digest_mismatches,
        "entries": entries,
        "sc0_blobs": sc0_blobs,
    }


def _extract_pkg_image0(
    pkg_path: Path,
    dest_dir: Path,
    *,
    passcode: str,
    pkgtool_bin: Path | None,
    check: Callable[[], None] = lambda: None,
    register_proc: Callable[[subprocess.Popen | None], None] = lambda *_: None,
) -> tuple[bool, str]:
    """Extract outer/inner PFS (Image0) files using external pkgtool or orbis-pub-cmd with cancellation support."""
    check()
    if pkgtool_bin is not None and pkgtool_bin.is_file():
        try:
            rc, out = run_cancellable_process(
                [str(pkgtool_bin), "pkg_extract", "--passcode", passcode, str(pkg_path), str(dest_dir)],
                timeout=300,
                check=check,
                register_proc=register_proc,
            )
            return rc == 0, out
        except SubprocessTimeoutError as exc:
            return False, str(exc)
    if pkg.tools_available():
        try:
            rc, out = run_cancellable_process(
                [str(pkg.ORBIS_CMD), "img_extract", "--passcode", passcode, str(pkg_path), str(dest_dir)],
                timeout=300,
                check=check,
                register_proc=register_proc,
            )
            return rc == 0, out
        except SubprocessTimeoutError as exc:
            return False, str(exc)
    return False, "Nenhum extrator PFS disponível no servidor."


def _run_pkgtool_validate(
    pkg_path: Path,
    pkgtool_bin: Path | None,
    *,
    check: Callable[[], None] = lambda: None,
    register_proc: Callable[[subprocess.Popen | None], None] = lambda *_: None,
) -> tuple[bool | None, str]:
    if pkgtool_bin is None or not pkgtool_bin.is_file():
        return None, ""
    check()
    try:
        rc, out = run_cancellable_process(
            [str(pkgtool_bin), "pkg_validate", str(pkg_path)],
            timeout=180,
            check=check,
            register_proc=register_proc,
        )
    except SubprocessTimeoutError as exc:
        return False, str(exc)
    failed = rc != 0 or ("FAIL" in out.upper() and "NOKEY" not in out.upper())
    return (not failed), out


def inspect_and_validate_pkg(
    pkg_path: Path,
    *,
    expected_content_dir: Path | None = None,
    passcode: str = PS4_PKG_DEFAULT_PASSCODE,
    builder_backend: str = "",
    pkgtool_bin: Path | None = None,
    check: Callable[[], None] = lambda: None,
    register_proc: Callable[[subprocess.Popen | None], None] = lambda *_: None,
) -> PkgValidationReport:
    """Full independent PKG inspection, cryptographic digest verification, Sc0/param.sfo check,
    extraction, and byte-for-byte SHA-256 comparison against the source theme bundle."""
    errors: list[str] = []
    warnings: list[str] = []
    files_list: list[dict] = []
    hash_mismatches: list[str] = []

    check()
    if not pkg_path.is_file() or pkg_path.stat().st_size == 0:
        return PkgValidationReport(
            pkg_created=False,
            structure_validated=False,
            theme_type_valid=False,
            verification_level="failed",
            builder_backend=builder_backend,
            validator_backend="czar-python-pkg-inspector",
            errors=["Arquivo .pkg ausente ou vazio."],
        )

    pkg_sha256 = _sha256_file(pkg_path)
    pkg_bytes = pkg_path.stat().st_size

    try:
        parsed = parse_pkg_binary(pkg_path)
    except Exception as exc:
        return PkgValidationReport(
            pkg_created=True,
            structure_validated=False,
            theme_type_valid=False,
            verification_level="failed",
            pkg_bytes=pkg_bytes,
            pkg_sha256=pkg_sha256,
            builder_backend=builder_backend,
            validator_backend="czar-python-pkg-inspector",
            errors=[f"Falha ao ler cabeçalho binário PKG: {exc}"],
        )

    # 1. Verify binary header fields & cryptographic digests independently
    header_errors_start = len(errors)
    if parsed["package_size"] != parsed["file_size"]:
        errors.append(
            f"Tamanho do arquivo ({parsed['file_size']:,} B) difere de package_size ({parsed['package_size']:,} B)."
        )
    if parsed["drm_type"] != 0xF:
        errors.append(f"drm_type inválido: 0x{parsed['drm_type']:X} (esperado 0xF para PS4).")
    if not parsed["header_digest_ok"]:
        errors.append("HeaderDigest (0xFE0) não bate com SHA-256(0x000..0xFE0) — cabeçalho inconsistente/corrompido.")
    if not parsed["header_signature_present"]:
        errors.append("HeaderSignature (0x1000) está zerada.")
    if not parsed["body_digest_ok"]:
        errors.append("body_digest (0x160) não bate com SHA-256 do segmento body.")
    if not parsed["digest_table_ok"]:
        errors.append("digest_table_hash (0x140) não bate com a tabela DIGESTS.")
    if not parsed["pfs_image_digest_ok"]:
        errors.append("pfs_image_digest (0x440) não bate com SHA-256 da imagem PFS.")
    if not parsed["pfs_signed_digest_ok"]:
        errors.append("pfs_signed_digest (0x460) não bate com SHA-256 do bloco inicial da imagem PFS.")
    if not parsed["sc_entries1_hash_ok"]:
        errors.append("sc_entries1_hash (0x100) inválido.")
    if not parsed["sc_entries2_hash_ok"]:
        errors.append("sc_entries2_hash (0x120) inválido.")
    if parsed["entry_digest_mismatches"]:
        errors.append(f"Digests de entradas Sc0 divergiram: {parsed['entry_digest_mismatches']}")

    # 2. Verify PS4 Theme binary identity (content_type=0x1B AC AND iro_tag=2 SS_THEME)
    content_type_hdr = int(parsed["content_type"])
    iro_tag_hdr = int(parsed["iro_tag"])
    content_id = str(parsed["content_id"])
    if content_type_hdr != PS4_PKG_CONTENT_TYPE_AC:
        errors.append(
            f"content_type no cabeçalho PKG é 0x{content_type_hdr:02X} (exigido 0x1B = AC; 0x1A seria aplicativo)."
        )
    if iro_tag_hdr != PS4_THEME_IRO_TAG:
        errors.append(
            f"IRO_TAG no cabeçalho binário PKG (offset 0x98) é {iro_tag_hdr} (exigido 2 = IROTag.SS_THEME). "
            "Sem IRO_TAG=2 o PS4 trata o pacote como aplicativo comum (CE-37695-0)."
        )
    if not pkg.CONTENT_ID_RE.match(content_id):
        errors.append(f"CONTENT_ID inválido no cabeçalho PKG: '{content_id}'.")
    header_and_crypto_valid = len(errors) == header_errors_start

    # 3. Independently parse Sc0/param.sfo extracted directly from raw PKG bytes
    sc0_errors_start = len(errors)
    sc0_blobs: dict[str, bytes] = parsed["sc0_blobs"]
    category = ""
    title_id = ""
    iro_tag_sfo: int | None = None
    bgmhome_loud: str | None = None

    if "Sc0/param.sfo" not in sc0_blobs:
        errors.append("Sc0/param.sfo ausente na tabela de entradas do PKG.")
    else:
        try:
            sfo = parse_param_sfo(sc0_blobs["Sc0/param.sfo"])
            vals = sfo["values"]
            category = str(vals.get("CATEGORY") or "")
            title_id = str(vals.get("TITLE_ID") or "")
            iro_tag_sfo = int(vals["IRO_TAG"]) if "IRO_TAG" in vals else None
            sfo_cid = str(vals.get("CONTENT_ID") or "")
            pub = str(vals.get("PUBTOOLINFO") or "")
            m_loud = re.search(r"bgmhome_loud=(-?\d+\.\d+)", pub)
            if m_loud:
                bgmhome_loud = m_loud.group(1)

            if category != PS4_THEME_CATEGORY:
                errors.append(f"CATEGORY em Sc0/param.sfo é '{category}' (exigido 'ac').")
            if iro_tag_sfo != PS4_THEME_IRO_TAG:
                errors.append(f"IRO_TAG em Sc0/param.sfo é {iro_tag_sfo} (exigido 2).")
            if sfo_cid != content_id:
                errors.append(f"CONTENT_ID em param.sfo ('{sfo_cid}') difere do cabeçalho PKG ('{content_id}').")
            if not pkg.TITLE_ID_RE.match(title_id) or title_id not in content_id:
                errors.append(f"TITLE_ID inválido ou incoerente em param.sfo: '{title_id}'.")
            if "APP_TYPE" in vals or "APP_VER" in vals:
                errors.append("Sc0/param.sfo contém campos de aplicativo (APP_TYPE/APP_VER) proibidos em tema.")
        except Exception as exc:
            errors.append(f"Falha ao decodificar Sc0/param.sfo: {exc}")

    for sc_name in ("Sc0/param.sfo", "Sc0/icon0.png", "Sc0/pic1.png"):
        if sc_name not in sc0_blobs:
            errors.append(f"Arquivo obrigatório ausente em Sc0: {sc_name}.")
        else:
            blob = sc0_blobs[sc_name]
            files_list.append(
                {
                    "path": sc_name,
                    "partition": "Sc0",
                    "bytes": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                }
            )
    sc0_validated = len(errors) == sc0_errors_start

    # 4. Cross-check with pkg_validate when available
    validator_backend = "czar-python-pkg-inspector"
    check()
    pv_ok, pv_out = _run_pkgtool_validate(
        pkg_path, pkgtool_bin, check=check, register_proc=register_proc
    )
    if pv_ok is not None:
        validator_backend = "czar-python-pkg-inspector+liborbispkg-validate"
        if not pv_ok:
            errors.append(f"Validador auxiliar pkg_validate reportou falha: {pv_out[:300]}")

    # 5. Extract Image0 PFS and compare SHA-256 hashes against expected source bundle (when provided)
    image0_extracted = False
    bundle_compared = False
    bundle_hashes_matched = False

    check()
    with tempfile.TemporaryDirectory(prefix="czar-pkg-extract-") as td:
        ex_dir = Path(td)
        ok_ex, ex_out = _extract_pkg_image0(
            pkg_path,
            ex_dir,
            passcode=passcode,
            pkgtool_bin=pkgtool_bin,
            check=check,
            register_proc=register_proc,
        )
        if not ok_ex:
            if expected_content_dir is not None:
                errors.append(f"Falha ao extrair Image0 do PKG para comparação de hashes: {ex_out[:300]}")
            else:
                warnings.append(
                    "Extração de Image0 não executada (extrator indisponível ou passcode diferente); "
                    "apenas cabeçalho, digests e Sc0 foram verificados."
                )
        else:
            # Locate extracted root (orbis-pub-cmd extracts to Image0/, LibOrbisPkg extracts to uroot/)
            if (ex_dir / "Image0").is_dir():
                img0_root = ex_dir / "Image0"
            elif (ex_dir / "uroot").is_dir():
                img0_root = ex_dir / "uroot"
            else:
                img0_root = ex_dir
            extracted_image0: dict[str, tuple[int, str]] = {}
            for p in sorted(img0_root.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(img0_root).as_posix()
                # LibOrbisPkg puts sce_sys inside inner PFS too; skip Sc0 duplicates when listing Image0
                if rel in {"sce_sys/param.sfo", "sce_sys/icon0.png", "sce_sys/pic1.png"}:
                    continue
                sha = _sha256_file(p)
                sz = p.stat().st_size
                extracted_image0[rel] = (sz, sha)
                files_list.append(
                    {
                        "path": f"Image0/{rel}",
                        "partition": "Image0",
                        "bytes": sz,
                        "sha256": sha,
                    }
                )

            image0_extracted = True
            if "theme.xml" not in extracted_image0:
                errors.append("Image0/theme.xml ausente após extração do PKG.")

            # Verify bgmhome_loud consistency with extracted bgm_home.at9
            has_bgm_in_pkg = "sound/bgm_home.at9" in extracted_image0
            if has_bgm_in_pkg and bgmhome_loud is None:
                warnings.append("Image0/sound/bgm_home.at9 está no PKG mas bgmhome_loud não consta em Sc0/param.sfo.")
            if not has_bgm_in_pkg and bgmhome_loud is not None:
                errors.append("Sc0/param.sfo declara bgmhome_loud mas Image0/sound/bgm_home.at9 não existe no PKG.")

            # Compare every file against expected source bundle directory!
            if expected_content_dir is not None and expected_content_dir.is_dir():
                bundle_compared = True
                expected_image0: dict[str, str] = {}
                expected_sc0: dict[str, str] = {}
                for p in sorted(expected_content_dir.rglob("*")):
                    if not p.is_file():
                        continue
                    rel = p.relative_to(expected_content_dir).as_posix()
                    if rel in {"sce_sys/param.sfo", "sce_sys/icon0.png", "sce_sys/pic1.png"}:
                        expected_sc0[f"Sc0/{Path(rel).name}"] = _sha256_file(p)
                    else:
                        expected_image0[rel] = _sha256_file(p)

                # Sc0 icon0.png & pic1.png must match byte-for-byte (param.sfo gets img0_l0_size added by packager)
                for sc_key in ("Sc0/icon0.png", "Sc0/pic1.png"):
                    if sc_key in expected_sc0:
                        actual_sc_sha = (
                            hashlib.sha256(sc0_blobs[sc_key]).hexdigest() if sc_key in sc0_blobs else None
                        )
                        if actual_sc_sha != expected_sc0[sc_key]:
                            hash_mismatches.append(f"{sc_key}: SHA-256 diverge do bundle")

                # Every Image0 file must exist and match SHA-256 byte-for-byte
                for rel, exp_sha in sorted(expected_image0.items()):
                    if rel not in extracted_image0:
                        hash_mismatches.append(f"Image0/{rel}: ausente no PKG extraído")
                    elif extracted_image0[rel][1] != exp_sha:
                        hash_mismatches.append(f"Image0/{rel}: SHA-256 diverge do bundle")

                extra_in_pkg = sorted(set(extracted_image0) - set(expected_image0))
                if extra_in_pkg:
                    for rel in extra_in_pkg:
                        hash_mismatches.append(f"Image0/{rel}: arquivo inesperado no PKG")

                if hash_mismatches:
                    errors.append(f"Divergência entre PKG extraído e bundle ({len(hash_mismatches)} itens).")
                else:
                    bundle_hashes_matched = True

    theme_type_valid = (
        content_type_hdr == PS4_PKG_CONTENT_TYPE_AC
        and iro_tag_hdr == PS4_THEME_IRO_TAG
        and category == PS4_THEME_CATEGORY
        and iro_tag_sfo == PS4_THEME_IRO_TAG
    )

    # CZAR-AUD-004: strict, honest verification tiers — NEVER mark structure_validated=True
    # when Image0 could not be extracted, and NEVER mark extracted_match=True without bundle comparison!
    base_ok = len(errors) == 0 and theme_type_valid and header_and_crypto_valid and sc0_validated
    if not base_ok:
        verification_level = "failed"
        structure_validated = False
    elif not image0_extracted:
        verification_level = "header_and_sc0_only"
        structure_validated = False
    elif bundle_compared and bundle_hashes_matched:
        verification_level = "full_bundle_verified"
        structure_validated = True
    else:
        verification_level = "extracted_uncompared"
        structure_validated = True

    return PkgValidationReport(
        pkg_created=True,
        structure_validated=structure_validated,
        theme_type_valid=theme_type_valid,
        verification_level=verification_level,
        header_and_crypto_valid=header_and_crypto_valid,
        sc0_validated=sc0_validated,
        image0_extracted=image0_extracted,
        bundle_compared=bundle_compared,
        bundle_hashes_matched=bundle_hashes_matched,
        content_id=content_id,
        title_id=title_id,
        volume_type=PS4_THEME_VOLUME_TYPE if theme_type_valid else "invalid",
        category=category,
        iro_tag_header=iro_tag_hdr,
        iro_tag_sfo=iro_tag_sfo,
        content_type_header=content_type_hdr,
        bgmhome_loud=bgmhome_loud,
        pkg_bytes=pkg_bytes,
        pkg_sha256=pkg_sha256,
        files=files_list,
        extracted_match=bundle_hashes_matched,
        hash_mismatches=hash_mismatches,
        physical_compatibility="pending",
        builder_backend=builder_backend,
        validator_backend=validator_backend,
        errors=errors,
        warnings=warnings,
    )
