#!/usr/bin/env python3
"""Deterministic, auditable patch for OpenOrbis/LibOrbisPkg (commit 92a4e7f8e5914cb06b52ba3ae6b0675da438a051).

License notice: OpenOrbis/LibOrbisPkg is licensed under LGPL-3.0. This patch
script modifies only the externally cloned LGPL-3.0 source under tools/src/
before compiling the standalone CLI executable in tools/bin/. No LGPL code is
copied into the MIT repository.

Exact fixes applied for PS4 Theme (pkg_ps4_theme) packaging on Linux:
1. LibOrbisPkg/PKG/PkgBuilder.cs:
   - Set pkg.Header.iro_tag = IROTag.SS_THEME (2) for pkg_ps4_theme and
     IROTag.SF_THEME (1) for pkg_ps4_sf_theme BEFORE HeaderDigest / HeaderSignature
     computation so the binary header (offset 0x98) and all hashes/signatures
     are structurally consistent.
   - Preserve bgmhome_loud=... from input param.sfo PUBTOOLINFO when appending
     packaging size metadata.
2. LibOrbisPkg/GP4/Gp4Validator.cs:
   - Accept VolumeType.pkg_ps4_theme and VolumeType.pkg_ps4_sf_theme in
     checkPkgVolumeType (matching checkCategoryMatchesPkgType in the same file).
   - Normalize Windows backslashes in GP4 orig_path on Linux.
3. LibOrbisPkg/PFS/PfsProperties.cs:
   - Normalize Windows backslashes in GP4 orig_path when building the PFS tree
     on Linux.
"""

from __future__ import annotations

import sys
from pathlib import Path


def patch_file(path: Path, old: str, new: str, label: str) -> None:
    raw = path.read_bytes()
    eol = b"\r\n" if b"\r\n" in raw else b"\n"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if new in text:
        print(f"  [ok] {label} (já aplicado)")
        return
    if old not in text:
        raise RuntimeError(f"Trecho esperado não encontrado em {path} para '{label}'")
    updated = text.replace(old, new, 1)
    path.write_bytes(updated.replace("\n", eol.decode("ascii")).encode("utf-8"))
    print(f"  [patch] {label}")


def main() -> int:
    if len(sys.argv) != 2:
        print("Uso: liborbispkg-ps4-theme-iro-tag.py <path-to-LibOrbisPkg>", file=sys.stderr)
        return 2
    repo = Path(sys.argv[1])

    pkg_builder = repo / "LibOrbisPkg" / "PKG" / "PkgBuilder.cs"
    patch_file(
        pkg_builder,
        "        iro_tag = IROTag.None,",
        "        iro_tag = volType == GP4.VolumeType.pkg_ps4_theme ? IROTag.SS_THEME\n"
        "                : volType == GP4.VolumeType.pkg_ps4_sf_theme ? IROTag.SF_THEME\n"
        "                : IROTag.None,",
        "PkgBuilder.cs: set IROTag.SS_THEME (2) for pkg_ps4_theme before header digest/signing",
    )

    patch_file(
        pkg_builder,
        '        sfo["PUBTOOLINFO"] = new SFO.Utf8Value("PUBTOOLINFO", date+time+sizeInfo, 0x200);',
        '        var bgmLoud = "";\n'
        '        if (sfo["PUBTOOLINFO"] is SFO.Utf8Value existingPub)\n'
        '        {\n'
        '          foreach (var tok in (existingPub.Value ?? "").Split(\',\'))\n'
        '          {\n'
        '            if (tok.Trim().StartsWith("bgmhome_loud="))\n'
        '            {\n'
        '              bgmLoud = "," + tok.Trim();\n'
        '              break;\n'
        '            }\n'
        '          }\n'
        '        }\n'
        '        sfo["PUBTOOLINFO"] = new SFO.Utf8Value("PUBTOOLINFO", date+time+bgmLoud+sizeInfo, 0x200);',
        "PkgBuilder.cs: preserve bgmhome_loud in PUBTOOLINFO",
    )

    gp4_val = repo / "LibOrbisPkg" / "GP4" / "Gp4Validator.cs"
    patch_file(
        gp4_val,
        "        case VolumeType.pkg_ps4_ac_nodata:\n          break;",
        "        case VolumeType.pkg_ps4_ac_nodata:\n"
        "        case VolumeType.pkg_ps4_theme:\n"
        "        case VolumeType.pkg_ps4_sf_theme:\n"
        "          break;",
        "Gp4Validator.cs: accept pkg_ps4_theme and pkg_ps4_sf_theme",
    )
    patch_file(
        gp4_val,
        "Path.Combine(dir, f.OrigPath)",
        "Path.Combine(dir, f.OrigPath.Replace('\\\\', Path.DirectorySeparatorChar))",
        "Gp4Validator.cs: normalize Windows backslashes on Linux (checkAllFilesExist)",
    )
    patch_file(
        gp4_val,
        "Path.Combine(projDir, sfoFile.OrigPath)",
        "Path.Combine(projDir, sfoFile.OrigPath.Replace('\\\\', Path.DirectorySeparatorChar))",
        "Gp4Validator.cs: normalize Windows backslashes on Linux (param.sfo)",
    )

    pfs_props = repo / "LibOrbisPkg" / "PFS" / "PfsProperties.cs"
    patch_file(
        pfs_props,
        "var source = Path.Combine(projDir, f.OrigPath);",
        "var source = Path.Combine(projDir, f.OrigPath.Replace('\\\\', Path.DirectorySeparatorChar));",
        "PfsProperties.cs: normalize Windows backslashes on Linux",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
