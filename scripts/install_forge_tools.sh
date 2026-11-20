#!/usr/bin/env bash
# Installs and builds audited open-source external tools for CZAR Forge under tools/bin/ (gitignored).
# Nothing built here is committed into the MIT repository. See docs/licensing.md.
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="${ROOT_DIR}/tools"
SRC_DIR="${TOOLS_DIR}/src"
BIN_DIR="${TOOLS_DIR}/bin"
DOTNET_DIR="${TOOLS_DIR}/dotnet"

PSGFX_REPO="https://github.com/elripalda/psgfx.git"
PSGFX_COMMIT="5ee7aa9e7d91c9f518322847d13a6bda5ba97837"

LIBORBISPKG_REPO="https://github.com/OpenOrbis/LibOrbisPkg.git"
LIBORBISPKG_COMMIT="92a4e7f8e5914cb06b52ba3ae6b0675da438a051"

mkdir -p "${SRC_DIR}" "${BIN_DIR}"

has_passwordless_sudo() {
    command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1
}

apt_install_if_sudo() {
    if ! command -v apt-get >/dev/null 2>&1 || ! has_passwordless_sudo; then
        return 1
    fi
    # GitHub Codespaces Debian Bookworm images ship an expired Yarn GPG repo
    # that breaks apt-get update; remove it safely before installing packages.
    if [[ -f /etc/apt/sources.list.d/yarn.list ]]; then
        echo "[CZAR Forge] Removendo /etc/apt/sources.list.d/yarn.list (chave GPG expirada no Codespaces)..."
        sudo -n rm -f /etc/apt/sources.list.d/yarn.list || true
    fi
    sudo -n apt-get update -qq
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

is_real_imagemagick() {
    local bin="$1"
    [[ -n "${bin}" && -x "${bin}" ]] && "${bin}" -version 2>/dev/null | grep -qi "ImageMagick"
}

probe_magick_dxt1a() {
    local bin="$1"
    python3 - "${bin}" <<'PY'
import subprocess, sys
bin_path = sys.argv[1]
rgba = bytes([255, 128, 64, 255] * 16)
p = subprocess.run(
    [bin_path, "-size", "4x4", "-depth", "8", "rgba:-", "-alpha", "off",
     "-define", "dds:compression=dxt1a", "-define", "dds:mipmaps=0", "dds:-"],
    input=rgba, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15
)
ok = p.returncode == 0 and len(p.stdout) >= 136 and p.stdout[:4] == b"DDS " and p.stdout[84:88] == b"DXT1"
sys.exit(0 if ok else 1)
PY
}

install_magick() {
    local target="${BIN_DIR}/magick"
    local found=""

    if command -v magick >/dev/null 2>&1 && is_real_imagemagick "$(command -v magick)"; then
        found="$(command -v magick)"
    elif [[ -x "${target}" ]] && is_real_imagemagick "${target}"; then
        found="${target}"
    elif command -v convert >/dev/null 2>&1 && is_real_imagemagick "$(command -v convert)"; then
        found="$(command -v convert)"
    else
        echo "[CZAR Forge] ImageMagick não encontrado; tentando instalar via apt-get..."
        if apt_install_if_sudo imagemagick; then
            if command -v magick >/dev/null 2>&1 && is_real_imagemagick "$(command -v magick)"; then
                found="$(command -v magick)"
            elif command -v convert >/dev/null 2>&1 && is_real_imagemagick "$(command -v convert)"; then
                found="$(command -v convert)"
            fi
        fi
    fi

    if [[ -z "${found}" ]]; then
        echo "[AVISO] ImageMagick não pôde ser instalado automaticamente (sem apt-get/sudo sem senha)." >&2
        echo "        O encoder nativo DXT1a continua 100% funcional sem ImageMagick." >&2
        return 1
    fi

    if [[ "${found}" != "${target}" ]]; then
        ln -sf "${found}" "${target}"
    fi
    if has_passwordless_sudo && [[ ! -e /usr/local/bin/magick ]]; then
        sudo -n ln -sf "${found}" /usr/local/bin/magick || true
    fi

    if probe_magick_dxt1a "${target}"; then
        echo "[CZAR Forge] ImageMagick validado (DXT1a OK): ${target} -> ${found}"
        return 0
    else
        echo "[ERRO] ImageMagick encontrado em ${found}, mas falhou no teste real DXT1a." >&2
        return 1
    fi
}

install_psgfx_at9() {
    local target="${BIN_DIR}/psgfx-at9"
    local repo_dir="${SRC_DIR}/psgfx"

    if ! command -v g++ >/dev/null 2>&1 && ! command -v clang++ >/dev/null 2>&1; then
        echo "[CZAR Forge] Compilador C++17 ausente; tentando instalar g++..."
        apt_install_if_sudo g++ || true
    fi
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

ensure_dotnet8() {
    if command -v dotnet >/dev/null 2>&1; then
        if dotnet --list-sdks 2>/dev/null | grep -q '^8\.'; then
            echo "$(command -v dotnet)"
            return 0
        fi
    fi
    if [[ -x "${DOTNET_DIR}/dotnet" ]] && "${DOTNET_DIR}/dotnet" --list-sdks 2>/dev/null | grep -q '^8\.'; then
        echo "${DOTNET_DIR}/dotnet"
        return 0
    fi
    echo "[CZAR Forge] Instalando .NET 8 SDK em espaço de usuário (${DOTNET_DIR})..." >&2
    mkdir -p "${DOTNET_DIR}"
    local installer="${DOTNET_DIR}/dotnet-install.sh"
    curl -fsSL https://dot.net/v1/dotnet-install.sh -o "${installer}"
    chmod +x "${installer}"
    bash "${installer}" --channel 8.0 --install-dir "${DOTNET_DIR}" --no-path >&2
    echo "${DOTNET_DIR}/dotnet"
}

install_liborbispkg() {
    local repo_dir="${SRC_DIR}/LibOrbisPkg"
    local out_dir="${BIN_DIR}/liborbispkg"
    local dotnet_bin
    dotnet_bin="$(ensure_dotnet8)"

    if [[ ! -d "${repo_dir}/.git" ]]; then
        echo "[CZAR Forge] Clonando OpenOrbis/LibOrbisPkg (LGPL-3.0) em ${repo_dir}..."
        git clone --filter=blob:none --no-checkout "${LIBORBISPKG_REPO}" "${repo_dir}"
    fi
    git -C "${repo_dir}" fetch --depth 1 origin "${LIBORBISPKG_COMMIT}" >/dev/null 2>&1 || git -C "${repo_dir}" fetch origin
    git -C "${repo_dir}" checkout --force "${LIBORBISPKG_COMMIT}"

    echo "[CZAR Forge] Aplicando patch auditável PS4 Theme (IRO_TAG=2 + bgmhome_loud + Linux GP4 paths)..."
    python3 "${ROOT_DIR}/scripts/patches/liborbispkg-ps4-theme-iro-tag.py" "${repo_dir}"

    echo "[CZAR Forge] Compilando PkgTool.Core (.NET 8 Linux)..."
    local dotnet_home="${TOOLS_DIR}/dotnet-home"
    mkdir -p "${dotnet_home}/.nuget/packages" "${dotnet_home}/.config" "${dotnet_home}/.local/share"
    rm -rf "${out_dir}.tmp"
    DOTNET_CLI_HOME="${dotnet_home}" \
    NUGET_PACKAGES="${dotnet_home}/.nuget/packages" \
    XDG_CONFIG_HOME="${dotnet_home}/.config" \
    XDG_DATA_HOME="${dotnet_home}/.local/share" \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    "${dotnet_bin}" publish \
        "${repo_dir}/PkgTool.Core/PkgTool.Core.csproj" \
        -c Release -o "${out_dir}.tmp" --nologo

    cat > "${out_dir}.tmp/pkgtool" <<WRAP
#!/usr/bin/env bash
set -euo pipefail
HERE="\$(cd -- "\$(dirname -- "\${BASH_SOURCE[0]}")" && pwd)"
DOTNET_BIN="${dotnet_bin}"
if [[ ! -x "\${DOTNET_BIN}" ]]; then
    DOTNET_BIN="\$(command -v dotnet || true)"
fi
exec "\${DOTNET_BIN}" "\${HERE}/PkgTool.Core.dll" "\$@"
WRAP
    chmod 755 "${out_dir}.tmp/pkgtool"
    rm -rf "${out_dir}"
    mv "${out_dir}.tmp" "${out_dir}"
    cp -f "${repo_dir}/LICENSE.txt" "${BIN_DIR}/LIBORBISPkg-LICENSE.txt"
    echo "[CZAR Forge] Instalado: ${out_dir}/pkgtool"
}

case "${1:-all}" in
    at9)
        install_psgfx_at9
        ;;
    pkg)
        install_liborbispkg
        ;;
    magick)
        install_magick
        ;;
    all)
        install_psgfx_at9
        install_liborbispkg
        install_magick || true
        ;;
    *)
        echo "Uso: $0 [at9|pkg|magick|all]" >&2
        exit 2
        ;;
esac
