#!/usr/bin/env bash
# Installs and builds audited open-source external tools for CZAR Forge under tools/bin/ (gitignored).
# Nothing built here is committed into the MIT repository. See docs/licensing.md.
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="${ROOT_DIR}/tools/src"
BIN_DIR="${ROOT_DIR}/tools/bin"

PSGFX_REPO="https://github.com/elripalda/psgfx.git"
PSGFX_COMMIT="5ee7aa9e7d91c9f518322847d13a6bda5ba97837"

mkdir -p "${SRC_DIR}" "${BIN_DIR}"

install_psgfx_at9() {
    local target="${BIN_DIR}/psgfx-at9"
    local repo_dir="${SRC_DIR}/psgfx"

    if ! command -v g++ >/dev/null 2>&1 && ! command -v clang++ >/dev/null 2>&1; then
        echo "[ERRO] Compilador C++17 (g++ ou clang++) não encontrado." >&2
        exit 1
    fi
    local cxx="${CXX:-$(command -v g++ || command -v clang++)}"

    if [[ ! -d "${repo_dir}/.git" ]]; then
        echo "[CZAR Forge] Clonando elripalda/psgfx (GPL-3.0-or-later) em ${repo_dir}..."
        git clone --filter=blob:none --no-checkout "${PSGFX_REPO}" "${repo_dir}"
    fi
    git -C "${repo_dir}" fetch --depth 1 origin "${PSGFX_COMMIT}" >/dev/null 2>&1 || git -C "${repo_dir}" fetch origin
    git -C "${repo_dir}" checkout --force "${PSGFX_COMMIT}"

    echo "[CZAR Forge] Compilando codificador ATRAC9 nativo Linux (${PSGFX_COMMIT:0:12})..."
    "${cxx}" -std=c++17 -O2 -Wall -Wextra -Wno-unused-function \
        "${repo_dir}/tools/at9check/at9check.cpp" \
        "${repo_dir}/src/lib/at9/at9_encoder.cpp" \
        "${repo_dir}/src/lib/at9/at9_snd0.cpp" \
        "${repo_dir}/src/lib/at9/at9_decode.cpp" \
        "${repo_dir}/src/lib/at9/at9_tables.cpp" \
        -o "${target}.tmp" -pthread
    chmod 755 "${target}.tmp"
    mv -f "${target}.tmp" "${target}"
    cp -f "${repo_dir}/LICENSE" "${BIN_DIR}/PSGFX-LICENSE.txt"
    cp -f "${repo_dir}/src/lib/at9/NOTICES.md" "${BIN_DIR}/PSGFX-AT9-NOTICES.md"
    echo "[CZAR Forge] Instalado: ${target}"
}

case "${1:-at9}" in
    at9|all)
        install_psgfx_at9
        ;;
    *)
        echo "Uso: $0 [at9|all]" >&2
        exit 2
        ;;
esac
