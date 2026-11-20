"""CZAR PKG Forge: stable native Linux PS4 Theme PKG packager adapter with
atomic writes, subprocess cancellation, and independent post-build validation."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from .. import media, pkg
from ..config import PKG_TOOLS_DIR, PROJECT_ROOT
from .bundle_validator import validate_theme_bundle
from .contracts import BundleValidationReport, PkgValidationReport
from .pkg_inspector import inspect_and_validate_pkg
from .subproc import SubprocessTimeoutError, run_cancellable_process


class PkgForgeCancelled(RuntimeError):
    """Raised when an in-flight PKG Forge job is cancelled by the user."""


class PkgForgeError(RuntimeError):
    """Actionable failure during PS4 theme PKG assembly or validation."""


def find_liborbispkg_tool() -> Path | None:
    """Locate externally built native Linux LibOrbisPkg PkgTool wrapper."""
    env_override = os.environ.get("CZAR_PKG_TOOL", "").strip()
    if env_override:
        p = Path(env_override)
        if p.is_file() and os.access(p, os.X_OK):
            return p
    for candidate in (
        PROJECT_ROOT / "tools" / "bin" / "liborbispkg" / "pkgtool",
        PKG_TOOLS_DIR / "bin" / "liborbispkg" / "pkgtool",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("pkgtool")
    return Path(found) if found else None


def pkg_forge_info() -> dict:
    """Report active PKG Forge builder backend, licensing, and independent inspector status."""
    native = find_liborbispkg_tool()
    if native is not None:
        return {
            "available": True,
            "backend": "liborbispkg-linux",
            "path": str(native),
            "license": "LGPL-3.0 (OpenOrbis/LibOrbisPkg + audited PS4 Theme IRO_TAG=2 patch)",
            "iro_tag_support": True,
            "inspector": "czar-python-pkg-inspector+liborbispkg-validate",
            "physical_compatibility_note": "Validação estrutural em Linux; compatibilidade física depende de ensaio no PS4 Slim 12.52.",
        }
    if os.name == "nt" and pkg.tools_available():
        return {
            "available": True,
            "backend": "orbis-pub-cmd",
            "path": str(pkg.ORBIS_CMD),
            "license": "Proprietary (user-supplied Sony SDK binary)",
            "iro_tag_support": True,
            "inspector": "czar-python-pkg-inspector",
            "physical_compatibility_note": "Validação estrutural; compatibilidade física depende de ensaio no PS4 Slim 12.52.",
        }
    return {
        "available": False,
        "backend": None,
        "path": None,
        "license": None,
        "iro_tag_support": False,
        "inspector": "czar-python-pkg-inspector",
        "install_hint": "Execute ./scripts/install_forge_tools.sh pkg para compilar o empacotador nativo Linux.",
        "physical_compatibility_note": "Empacotador PKG não instalado neste servidor; a exportação do bundle validado continua disponível.",
    }


def _run_cancellable(
    cmd: list[str],
    *,
    timeout: float,
    check: Callable[[], None],
    register_proc: Callable[[subprocess.Popen | None], None],
) -> tuple[int, str]:
    try:
        return run_cancellable_process(
            cmd,
            timeout=timeout,
            check=check,
            register_proc=register_proc,
        )
    except SubprocessTimeoutError as exc:
        raise PkgForgeError(f"O empacotador excedeu o tempo limite ({timeout:g}s).") from exc


def build_and_validate_theme_pkg(
    content_dir: Path,
    gp4_path: Path,
    meta: pkg.PackageMeta,
    dest_dir: Path,
    *,
    progress: Callable[[str, int], None] = lambda *_: None,
    check: Callable[[], None] = lambda: None,
    register_proc: Callable[[subprocess.Popen | None], None] = lambda *_: None,
) -> tuple[Path, BundleValidationReport, PkgValidationReport, list[dict]]:
    """Validate bundle -> build PS4 Theme PKG -> independently inspect, verify hashes, and atomically store."""
    log_steps: list[dict] = []
    check()
    progress("Validando bundle completo do tema", 15)
    t0 = time.monotonic()
    bundle_report = validate_theme_bundle(content_dir, meta=meta, gp4_path=gp4_path)
    log_steps.append(
        {
            "label": "bundle_validate",
            "seconds": round(time.monotonic() - t0, 2),
            "detail": f"{len(bundle_report.files)} arquivos · {'OK' if bundle_report.valid else 'FALHOU'}",
        }
    )
    if not bundle_report.valid:
        raise PkgForgeError("Validação do bundle falhou: " + "; ".join(bundle_report.errors))

    info = pkg_forge_info()
    if not info["available"]:
        raise PkgForgeError(
            "Empacotador PKG nativo não está instalado neste servidor. "
            "Execute ./scripts/install_forge_tools.sh pkg no terminal do Codespace."
        )

    backend = str(info["backend"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    final_pkg_name = f"{meta.content_id}.pkg"
    final_pkg_path = dest_dir / final_pkg_name

    with tempfile.TemporaryDirectory(dir=dest_dir, prefix=".pkgbuild-") as staging_raw:
        staging = Path(staging_raw)
        out_dir = staging / "out"
        out_dir.mkdir()

        check()
        progress("Construindo imagem PFS e pacote PKG PS4", 45)
        t_build = time.monotonic()
        pkgtool_bin = find_liborbispkg_tool()

        if backend == "liborbispkg-linux":
            assert pkgtool_bin is not None
            # Write a POSIX-normalized GP4 copy inside staging referencing content_dir reliably
            targets = sorted(
                p.relative_to(content_dir).as_posix()
                for p in content_dir.rglob("*")
                if p.is_file()
            )
            linux_gp4 = gp4_path.parent / "theme-linux.gp4"
            gp4_xml = pkg.build_gp4(meta, targets, content_dir.name)
            # Replace backslashes inside orig_path attributes with POSIX slashes
            gp4_xml = gp4_xml.replace("\\", "/")
            linux_gp4.write_text(gp4_xml, encoding="utf-8")

            rc, out = _run_cancellable(
                [str(pkgtool_bin), "pkg_build", str(linux_gp4), str(out_dir)],
                timeout=600,
                check=check,
                register_proc=register_proc,
            )
            log_steps.append(
                {
                    "label": "pkg_build (liborbispkg-linux)",
                    "seconds": round(time.monotonic() - t_build, 2),
                    "detail": out.strip()[-400:],
                }
            )
            if rc != 0:
                tail = "\n".join(out.strip().splitlines()[-8:])
                raise PkgForgeError(f"pkg_build falhou (código {rc}): {tail}")
            built_files = sorted(out_dir.glob("*.pkg"))
            if not built_files:
                raise PkgForgeError("pkg_build concluiu sem erros, mas não gerou nenhum arquivo .pkg.")
            built_pkg = built_files[0]
        else:
            built_pkg, build_log = pkg.build_package(staging, content_dir, meta)
            for s in build_log.steps:
                log_steps.append({"label": s["label"], "seconds": s["seconds"], "detail": s["output"][:300]})

        check()
        progress("Validando cabeçalho binário, IRO_TAG, digests e extração", 78)
        t_val = time.monotonic()
        pkg_report = inspect_and_validate_pkg(
            built_pkg,
            expected_content_dir=content_dir,
            passcode=meta.passcode,
            builder_backend=backend,
            pkgtool_bin=pkgtool_bin,
            check=check,
            register_proc=register_proc,
        )
        log_steps.append(
            {
                "label": "pkg_independent_validate",
                "seconds": round(time.monotonic() - t_val, 2),
                "detail": (
                    f"level={pkg_report.verification_level} "
                    f"IRO_TAG hdr={pkg_report.iro_tag_header} sfo={pkg_report.iro_tag_sfo} "
                    f"CATEGORY={pkg_report.category} hashes={'OK' if pkg_report.bundle_hashes_matched else 'FAIL'}"
                ),
            }
        )
        if not pkg_report.structure_validated or not pkg_report.theme_type_valid:
            raise PkgForgeError(
                "Validação independente do PKG falhou: " + "; ".join(pkg_report.errors)
            )

        check()
        progress("Publicando PKG validado de forma atômica", 95)
        tmp_final = dest_dir / f".{final_pkg_name}.tmp"
        shutil.copyfile(built_pkg, tmp_final)
        tmp_final.replace(final_pkg_path)

    return final_pkg_path, bundle_report, pkg_report, log_steps
