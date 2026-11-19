"""Verifica um bundle Czar antes de entregá-lo ao empacotador externo."""
import argparse
import hashlib
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath

REQUIRED = {"Image0/theme.xml", "Sc0/param.sfo", "Sc0/icon0.png", "Sc0/pic1.png",
            "theme.gp4", "manifest.json", "validation-report.json", "EMPACOTAMENTO.md", "SHA256SUMS"}
MAX_BYTES = 384 * 1024 ** 2


def verify(path):
    with zipfile.ZipFile(path) as bundle:
        infos = bundle.infolist()
        names = {entry.filename for entry in infos}
        if len(names) != len(infos) or len(names) > 150 or not REQUIRED <= names:
            raise ValueError("Inventário incompleto ou duplicado.")
        if sum(e.file_size for e in infos) > MAX_BYTES:
            raise ValueError("Limite de descompressão excedido.")
        for entry in infos:
            name = entry.filename
            path = PurePosixPath(name)
            if (path.is_absolute() or "\\" in name or ":" in name or path.as_posix() != name or
                ".." in path.parts or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)):
                raise ValueError("O bundle contém uma entrada inválida.")
        for name in ("SHA256SUMS", "validation-report.json"):
            if bundle.getinfo(name).file_size > 1024 ** 2:
                raise ValueError("Metadados acima do limite.")
        checks = {}
        for line in bundle.read("SHA256SUMS").decode().splitlines():
            digest, name = line.split("  ", 1)
            if name in checks or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Entrada inválida em SHA256SUMS.")
            checks[name] = digest
        if set(checks) != names - {"SHA256SUMS"}:
            raise ValueError("SHA256SUMS não corresponde ao inventário.")
        for name, digest in sorted(checks.items()):
            with bundle.open(name) as member:
                if hashlib.file_digest(member, "sha256").hexdigest() != digest:
                    raise ValueError(f"SHA-256 divergente em {name}.")
        report = json.loads(bundle.read("validation-report.json"))
        if report["status"] != "valid" or report["packageValidation"]["built"]:
            raise ValueError("O relatório precisa indicar um bundle estruturalmente válido.")
        return report["buildId"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    args = parser.parse_args()
    try:
        build_id = verify(args.bundle)
    except (zipfile.BadZipFile, OSError, ValueError, KeyError, RuntimeError) as exc:
        parser.exit(1, f"Bundle inválido. {exc}\n")
    print(f"Bundle conferido. Build {build_id}. A etapa de .pkg é externa.")


if __name__ == "__main__":
    main()
