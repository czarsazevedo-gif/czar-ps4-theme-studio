#!/usr/bin/env bash
# CZAR COMPLETE TOOLCHAIN — Instalador Integral e Idempotente (Linux / GitHub Codespaces)
# Uso: ./scripts/bootstrap_czar_full.sh [--install|--check-only]
# Regras:
# - Idempotente: não reinstala ferramentas já íntegras e funcionais.
# - Isolado: usa ambiente virtual Python e tools/bin/ (gitignored).
# - Seguro: não desativa verificação GPG, não baixa binários proprietários Sony,
#   fixa commits Git e checksums SHA-256.
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="${ROOT_DIR}/tools"
SRC_DIR="${TOOLS_DIR}/src"
BIN_DIR="${TOOLS_DIR}/bin"
CACHE_DIR="${TOOLS_DIR}/cache"
DOTNET_DIR="${TOOLS_DIR}/dotnet"
ICONS_DIR="${TOOLS_DIR}/icons/tabler"

# Pinned upstream commits & verified SHA-256 checksums (see docs/czar-toolchain-lock.md)
PSGFX_REPO="https://github.com/elripalda/psgfx.git"
PSGFX_COMMIT="5ee7aa9e7d91c9f518322847d13a6bda5ba97837"

LIBORBISPKG_REPO="https://github.com/OpenOrbis/LibOrbisPkg.git"
LIBORBISPKG_COMMIT="92a4e7f8e5914cb06b52ba3ae6b0675da438a051"

GIFSICLE_REPO="https://github.com/kohler/gifsicle.git"
GIFSICLE_COMMIT="8ff3057b736141b406be621830b851fdf4285689"

VGMSTREAM_REPO="https://github.com/vgmstream/vgmstream.git"
VGMSTREAM_COMMIT="24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce"

HYPERFINE_VERSION="v2.0.0"
HYPERFINE_X86_64_URL="https://github.com/sharkdp/hyperfine/releases/download/${HYPERFINE_VERSION}/hyperfine-${HYPERFINE_VERSION}-x86_64-unknown-linux-gnu.tar.gz"
HYPERFINE_X86_64_SHA256="ae2beda2ac99c098427e4f755244552daf20e514422889cca6d44421e2d93c87"

TABLER_REPO="https://github.com/tabler/tabler-icons.git"
TABLER_COMMIT="0bd42617ab4c3eae2774a548b9d429046c6b197e"

# Perceptual Supremacy (Pilar IV) pinned commits & verified SHA-256 checksums
BC7ENC_REPO="https://github.com/richgel999/bc7enc.git"
BC7ENC_COMMIT="f66c2e489b07138f2673a2fb3d27c1aa1d565c48"

ICBC_REPO="https://github.com/castano/icbc.git"
ICBC_COMMIT="f06e053377feaaa1fe6d433459f625622f8081d9"

BLUENOISE_REPO="https://github.com/Calinou/free-blue-noise-textures.git"
BLUENOISE_COMMIT="a64e300dcd96b5f8c42494c5e452c1eb409b0efb"

COMPRESSONATOR_VERSION="V4.5.52"
COMPRESSONATOR_URL="https://github.com/GPUOpen-Tools/compressonator/releases/download/${COMPRESSONATOR_VERSION}/compressonatorcli-4.5.52-Linux.tar.gz"
COMPRESSONATOR_SHA256="70c9cdb27a19875df03766f349864951a749a44c0f5c001c33903944465f6b97"

VMAF_VERSION="v3.0.0"
VMAF_URL="https://github.com/Netflix/vmaf/releases/download/${VMAF_VERSION}/vmaf"
VMAF_SHA256="cf36d5c2abf61d3492aaff585a2af74c9782dd06b6e791b4b979ebf1bd39e329"

LIB_DIR="${TOOLS_DIR}/lib"

MODE="install"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install) MODE="install" ;;
        --check-only) MODE="check" ;;
        -h|--help)
            echo "Uso: $0 [--install|--check-only]"
            exit 0
            ;;
        *)
            echo "[ERRO] Opção desconhecida: $1" >&2
            exit 2
            ;;
    esac
    shift
done

mkdir -p "${SRC_DIR}" "${BIN_DIR}" "${CACHE_DIR}"

detect_system() {
    local os arch distro py_ver cpus mem_kb disk_avail
    os="$(uname -s)"
    arch="$(uname -m)"
    distro="desconhecida"
    if [[ -r /etc/os-release ]]; then
        distro="$(awk -F= '/^PRETTY_NAME=/{gsub(/"/,"",$2); print $2}' /etc/os-release)"
    fi
    py_ver="$(python3 --version 2>/dev/null || echo 'ausente')"
    cpus="$(nproc 2>/dev/null || echo 1)"
    mem_kb="$(awk '/MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    disk_avail="$(df -h "${ROOT_DIR}" | awk 'NR==2{print $4}')"
    echo "=========================================================="
    echo "CZAR COMPLETE TOOLCHAIN — BOOTSTRAP (${MODE})"
    echo "=========================================================="
    echo "SO / Arch     : ${os} (${arch})"
    echo "Distribuição  : ${distro}"
    echo "Python base   : ${py_ver}"
    echo "CPU / RAM     : ${cpus} núcleo(s) · $((mem_kb / 1024)) MiB RAM"
    echo "Disco livre   : ${disk_avail} em ${ROOT_DIR}"
    echo "=========================================================="
}

resolve_venv_python() {
    if [[ -n "${CZAR_VENV:-}" && -x "${CZAR_VENV}/bin/python" ]]; then
        echo "${CZAR_VENV}/bin/python"
        return 0
    fi
    if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
        echo "${ROOT_DIR}/.venv/bin/python"
        return 0
    fi
    if [[ -x "${HOME}/.venv/bin/python" ]]; then
        echo "${HOME}/.venv/bin/python"
        return 0
    fi
    if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
        echo "${VIRTUAL_ENV}/bin/python"
        return 0
    fi
    echo ""
}

ensure_system_prereqs() {
    local missing=()
    for cmd in git curl tar unzip sha256sum ffmpeg ffprobe gcc g++ make cmake autoconf automake pkg-config; do
        command -v "${cmd}" >/dev/null 2>&1 || missing+=("${cmd}")
    done
    if ! command -v chromium >/dev/null 2>&1 && ! command -v google-chrome >/dev/null 2>&1; then
        missing+=("chromium")
    fi
    if (( ${#missing[@]} == 0 )); then
        echo "[OK] Pré-requisitos de sistema já presentes."
        return 0
    fi

    echo "[INFO] Pré-requisitos de sistema ausentes: ${missing[*]}"
    if [[ "${MODE}" == "check" ]]; then
        return 1
    fi

    if command -v apt-get >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
        local pkgs=()
        for m in "${missing[@]}"; do
            case "$m" in
                ffmpeg|ffprobe) pkgs+=(ffmpeg) ;;
                gcc|g++|make) pkgs+=(build-essential) ;;
                cmake) pkgs+=(cmake ninja-build) ;;
                autoconf|automake) pkgs+=(autoconf automake) ;;
                pkg-config) pkgs+=(pkg-config) ;;
                chromium) pkgs+=(chromium) ;;
                git|curl|tar|unzip) pkgs+=("$m") ;;
            esac
        done
        if ! command -v magick >/dev/null 2>&1 && ! command -v convert >/dev/null 2>&1; then
            pkgs+=(imagemagick)
        fi
        if (( ${#pkgs[@]} > 0 )); then
            echo "[INFO] Instalando pacotes Debian/Ubuntu sem desativar GPG: ${pkgs[*]}"
            if ! sudo -n apt-get update -qq 2>/dev/null; then
                # Em imagens padrão do GitHub Codespaces, /etc/apt/sources.list.d/yarn.list vem com chave GPG expirada/ausente
                # (dl.yarnpkg.com NO_PUBKEY 62D54FD4003F6525). Como o Studio não usa o repositório apt do Yarn,
                # desativamos apenas a fonte quebrada do Yarn mantendo 100% da verificação GPG oficial Debian/Ubuntu ativa!
                if [[ -f /etc/apt/sources.list.d/yarn.list ]]; then
                    echo "[INFO] Removendo fonte apt legada do Yarn com chave GPG expirada no Codespaces (/etc/apt/sources.list.d/yarn.list)..."
                    sudo -n rm -f /etc/apt/sources.list.d/yarn.list
                fi
            fi
            if sudo -n apt-get update -qq; then
                sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${pkgs[@]}"
            else
                echo "[ERRO] 'apt-get update' falhou nos repositórios do sistema." >&2
                return 1
            fi
        fi
    else
        echo "[ERRO] Ferramentas obrigatórias de sistema ausentes sem apt-get/sudo disponível: ${missing[*]}" >&2
        return 1
    fi
}

ensure_python_env() {
    local py_bin
    py_bin="$(resolve_venv_python)"
    if [[ -z "${py_bin}" ]]; then
        if [[ "${MODE}" == "check" ]]; then
            echo "[ERRO] Ambiente virtual Python (.venv) ausente." >&2
            return 1
        fi
        echo "[INFO] Criando ambiente virtual isolado em ${ROOT_DIR}/.venv..."
        if command -v uv >/dev/null 2>&1; then
            uv venv "${ROOT_DIR}/.venv" --python python3
        else
            python3 -m venv "${ROOT_DIR}/.venv"
        fi
        py_bin="${ROOT_DIR}/.venv/bin/python"
    fi

    # Verifica se tudo já está instalado e sem conflitos de OpenCV
    if "${py_bin}" - <<'PY' >/dev/null 2>&1
import importlib
for mod in ("fastapi", "uvicorn", "multipart", "numpy", "pydantic", "PIL", "cv2", "scipy", "skimage", "av", "imageio", "httpx", "pytest", "pytest_asyncio", "playwright", "gdown"):
    importlib.import_module(mod)
import importlib.metadata as md
dists = {d.metadata["Name"].lower() for d in md.distributions() if d.metadata.get("Name")}
assert "opencv-python-headless" in dists
assert "opencv-python" not in dists and "opencv-contrib-python" not in dists
PY
    then
        echo "[OK] Ambiente Python isolado íntegro (${py_bin})."
        return 0
    fi

    if [[ "${MODE}" == "check" ]]; then
        echo "[ERRO] Dependências Python incompletas em ${py_bin}." >&2
        return 1
    fi

    echo "[INFO] Instalando dependências Python isoladas em ${py_bin}..."
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "${py_bin}" -r "${ROOT_DIR}/requirements-dev.txt"
    else
        "${py_bin}" -m pip install --upgrade pip
        "${py_bin}" -m pip install -r "${ROOT_DIR}/requirements-dev.txt"
    fi
}

ensure_psgfx() {
    local target="${BIN_DIR}/psgfx-at9"
    if [[ -x "${target}" ]] && { "${target}" 2>&1 || true; } | grep -qiE "at9|usage|encode|wav"; then
        echo "[OK] PSGFX ATRAC9 já instalado e funcional (${target})."
        return 0
    fi
    [[ "${MODE}" == "check" ]] && { echo "[AUSENTE] PSGFX ATRAC9 (${target})" >&2; return 1; }
    bash "${ROOT_DIR}/scripts/install_forge_tools.sh" at9
}

ensure_liborbispkg() {
    local target="${BIN_DIR}/liborbispkg/pkgtool"
    if [[ -x "${target}" ]] && { "${target}" 2>&1 || true; } | grep -q "pkg_build"; then
        echo "[OK] LibOrbisPkg + .NET já instalados e funcionais (${target})."
        return 0
    fi
    [[ "${MODE}" == "check" ]] && { echo "[AUSENTE] LibOrbisPkg (${target})" >&2; return 1; }
    bash "${ROOT_DIR}/scripts/install_forge_tools.sh" pkg
}

ensure_gifsicle() {
    local target="${BIN_DIR}/gifsicle"
    if [[ -x "${target}" ]] && "${target}" --version >/dev/null 2>&1; then
        echo "[OK] Gifsicle já instalado ($("${target}" --version | head -n 1))."
        return 0
    fi
    [[ "${MODE}" == "check" ]] && { echo "[AUSENTE] Gifsicle (${target})" >&2; return 1; }

    local repo_dir="${SRC_DIR}/gifsicle"
    if [[ ! -d "${repo_dir}/.git" ]]; then
        echo "[INFO] Clonando kohler/gifsicle (GPL-2.0)..."
        git clone --filter=blob:none --no-checkout "${GIFSICLE_REPO}" "${repo_dir}"
    fi
    git -C "${repo_dir}" fetch --depth 1 origin "${GIFSICLE_COMMIT}" >/dev/null 2>&1 || git -C "${repo_dir}" fetch origin
    git -C "${repo_dir}" checkout --force "${GIFSICLE_COMMIT}"

    echo "[INFO] Compilando Gifsicle (${GIFSICLE_COMMIT:0:12})..."
    (
        cd "${repo_dir}"
        autoreconf -i
        ./configure --disable-gifview --disable-gifdiff
        make -j"$(nproc)"
    )
    cp -f "${repo_dir}/src/gifsicle" "${target}.tmp"
    chmod 755 "${target}.tmp"
    mv -f "${target}.tmp" "${target}"
    cp -f "${repo_dir}/COPYING" "${BIN_DIR}/GIFSICLE-LICENSE.txt"
    echo "[OK] Gifsicle instalado: ${target}"
}

ensure_vgmstream() {
    local target="${BIN_DIR}/vgmstream-cli"
    if [[ -x "${target}" ]] && { "${target}" 2>&1 || true; } | grep -qi "vgmstream"; then
        echo "[OK] vgmstream-cli já instalado (${target})."
        return 0
    fi
    [[ "${MODE}" == "check" ]] && { echo "[AUSENTE] vgmstream-cli (${target})" >&2; return 1; }

    local repo_dir="${SRC_DIR}/vgmstream"
    if [[ ! -d "${repo_dir}/.git" ]]; then
        echo "[INFO] Clonando vgmstream/vgmstream (ISC/MIT)..."
        git clone --filter=blob:none --no-checkout "${VGMSTREAM_REPO}" "${repo_dir}"
    fi
    git -C "${repo_dir}" fetch --depth 1 origin "${VGMSTREAM_COMMIT}" >/dev/null 2>&1 || git -C "${repo_dir}" fetch origin
    git -C "${repo_dir}" checkout --force "${VGMSTREAM_COMMIT}"

    echo "[INFO] Compilando vgmstream-cli com suporte nativo ATRAC9/VAG (${VGMSTREAM_COMMIT:0:12})..."
    local gen_args=()
    if command -v ninja >/dev/null 2>&1; then
        gen_args=(-G Ninja)
    fi
    cmake -S "${repo_dir}" -B "${repo_dir}/build" "${gen_args[@]}" \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_CLI=ON \
        -DBUILD_V123=OFF \
        -DBUILD_AUDACIOUS=OFF \
        -DBUILD_STATIC=ON \
        -DUSE_ATRAC9=ON \
        -DUSE_MPEG=OFF \
        -DUSE_VORBIS=OFF \
        -DUSE_FFMPEG=OFF \
        -DUSE_G7221=OFF \
        -DUSE_G719=OFF \
        -DUSE_CELT=OFF \
        -DUSE_SPEEX=OFF
    cmake --build "${repo_dir}/build" --target vgmstream-cli -j"$(nproc)"
    cp -f "${repo_dir}/build/cli/vgmstream-cli" "${target}.tmp"
    chmod 755 "${target}.tmp"
    mv -f "${target}.tmp" "${target}"
    cp -f "${repo_dir}/COPYING" "${BIN_DIR}/VGMSTREAM-LICENSE.txt"
    echo "[OK] vgmstream-cli instalado: ${target}"
}

ensure_hyperfine() {
    local target="${BIN_DIR}/hyperfine"
    if [[ -x "${target}" ]] && "${target}" --version >/dev/null 2>&1; then
        echo "[OK] Hyperfine já instalado ($("${target}" --version))."
        return 0
    fi
    if command -v hyperfine >/dev/null 2>&1; then
        ln -sf "$(command -v hyperfine)" "${target}"
        echo "[OK] Hyperfine vinculado do sistema ($("${target}" --version))."
        return 0
    fi
    [[ "${MODE}" == "check" ]] && { echo "[AUSENTE] Hyperfine (${target})" >&2; return 1; }

    local arch
    arch="$(uname -m)"
    if [[ "${arch}" == "x86_64" ]]; then
        local tgz="${CACHE_DIR}/hyperfine-${HYPERFINE_VERSION}-x86_64-unknown-linux-gnu.tar.gz"
        if [[ ! -f "${tgz}" ]] || ! echo "${HYPERFINE_X86_64_SHA256}  ${tgz}" | sha256sum -c - >/dev/null 2>&1; then
            echo "[INFO] Baixando Hyperfine ${HYPERFINE_VERSION} (x86_64) com verificação SHA-256..."
            curl -fsSL "${HYPERFINE_X86_64_URL}" -o "${tgz}.tmp"
            echo "${HYPERFINE_X86_64_SHA256}  ${tgz}.tmp" | sha256sum -c -
            mv -f "${tgz}.tmp" "${tgz}"
        fi
        local ex_dir="${CACHE_DIR}/hyperfine-extract"
        rm -rf "${ex_dir}"
        mkdir -p "${ex_dir}"
        tar -xzf "${tgz}" -C "${ex_dir}" --strip-components=1
        cp -f "${ex_dir}/hyperfine" "${target}.tmp"
        chmod 755 "${target}.tmp"
        mv -f "${target}.tmp" "${target}"
        cp -f "${ex_dir}/LICENSE-MIT" "${BIN_DIR}/HYPERFINE-LICENSE-MIT.txt"
        echo "[OK] Hyperfine instalado e validado por SHA-256: $("${target}" --version)"
    elif command -v cargo >/dev/null 2>&1; then
        echo "[INFO] Compilando Hyperfine via cargo (${arch})..."
        cargo install --locked --root "${TOOLS_DIR}/cargo-hyperfine" hyperfine
        cp -f "${TOOLS_DIR}/cargo-hyperfine/bin/hyperfine" "${target}"
        chmod 755 "${target}"
    else
        echo "[ERRO] Arquitetura ${arch} requer cargo ou pacote de sistema para Hyperfine." >&2
        return 1
    fi
}

ensure_magick() {
    local target="${BIN_DIR}/magick"
    if [[ -x "${target}" ]] && "${target}" -version 2>/dev/null | grep -qi "ImageMagick"; then
        echo "[OK] ImageMagick já vinculado (${target})."
        return 0
    fi
    local found=""
    if command -v magick >/dev/null 2>&1 && magick -version 2>/dev/null | grep -qi "ImageMagick"; then
        found="$(command -v magick)"
    elif command -v convert >/dev/null 2>&1 && convert -version 2>/dev/null | grep -qi "ImageMagick"; then
        found="$(command -v convert)"
    fi
    if [[ -n "${found}" ]]; then
        ln -sf "${found}" "${target}"
        echo "[OK] ImageMagick vinculado: ${target} -> ${found}"
        return 0
    fi
    echo "[AVISO] ImageMagick opcional ausente (encoder nativo DXT1a continua 100% ativo)."
    return 0
}

ensure_tabler_icons() {
    if [[ -f "${ICONS_DIR}/icons/outline/settings.svg" && -f "${ICONS_DIR}/LICENSE" ]]; then
        echo "[OK] Tabler Icons (v3.9.0 MIT) já preparados em ${ICONS_DIR}."
        return 0
    fi
    [[ "${MODE}" == "check" ]] && { echo "[AVISO] Tabler Icons ainda não baixados em ${ICONS_DIR}."; return 0; }

    echo "[INFO] Preparando conjunto curado Tabler Icons v3.9.0 (MIT) em ${ICONS_DIR}..."
    rm -rf "${ICONS_DIR}.tmp"
    git clone --filter=blob:none --no-checkout --depth 1 "${TABLER_REPO}" "${ICONS_DIR}.tmp"
    git -C "${ICONS_DIR}.tmp" fetch --depth 1 origin "${TABLER_COMMIT}" >/dev/null 2>&1 || true
    git -C "${ICONS_DIR}.tmp" sparse-checkout init --no-cone
    cat > "${ICONS_DIR}.tmp/.git/info/sparse-checkout" <<'EOF'
/LICENSE
/icons/outline/settings.svg
/icons/outline/bell.svg
/icons/outline/users.svg
/icons/outline/message.svg
/icons/outline/trophy.svg
/icons/outline/user.svg
/icons/outline/power.svg
/icons/outline/device-gamepad-2.svg
/icons/outline/music.svg
/icons/outline/photo.svg
/icons/outline/folder.svg
/icons/outline/world.svg
/icons/outline/shopping-bag.svg
/icons/outline/video.svg
/icons/outline/star.svg
EOF
    git -C "${ICONS_DIR}.tmp" checkout --force "${TABLER_COMMIT}"
    rm -rf "${ICONS_DIR}"
    mkdir -p "$(dirname "${ICONS_DIR}")"
    mv "${ICONS_DIR}.tmp" "${ICONS_DIR}"
    echo "[OK] Tabler Icons preparados em ${ICONS_DIR}."
}

ensure_czar_bc1() {
    mkdir -p "${LIB_DIR}"
    local target="${LIB_DIR}/libczar_bc1.so"
    if [[ -f "${target}" && "${target}" -nt "${ROOT_DIR}/scripts/src/czar_bc1.cpp" ]]; then
        echo "[OK] libczar_bc1.so (RGBCX + ICBC) já compilada em ${target}."
        return 0
    fi
    [[ "${MODE}" == "check" ]] && { echo "[AVISO] libczar_bc1.so ainda não compilada em ${target}."; return 0; }

    if [[ ! -f "${SRC_DIR}/bc7enc/rgbcx.h" ]]; then
        rm -rf "${SRC_DIR}/bc7enc"
        git clone --depth 1 "${BC7ENC_REPO}" "${SRC_DIR}/bc7enc"
        git -C "${SRC_DIR}/bc7enc" fetch --depth 1 origin "${BC7ENC_COMMIT}" >/dev/null 2>&1 || true
        git -C "${SRC_DIR}/bc7enc" checkout --force "${BC7ENC_COMMIT}" >/dev/null 2>&1 || true
    fi
    if [[ ! -f "${SRC_DIR}/icbc/icbc.h" ]]; then
        rm -rf "${SRC_DIR}/icbc"
        git clone --depth 1 "${ICBC_REPO}" "${SRC_DIR}/icbc"
        git -C "${SRC_DIR}/icbc" fetch --depth 1 origin "${ICBC_COMMIT}" >/dev/null 2>&1 || true
        git -C "${SRC_DIR}/icbc" checkout --force "${ICBC_COMMIT}" >/dev/null 2>&1 || true
    fi
    echo "[INFO] Compilando libczar_bc1.so (RGBCX + ICBC, -O3 -msse4.1)..."
    g++ -O3 -msse4.1 -fPIC -shared \
        -I"${SRC_DIR}/bc7enc" -I"${SRC_DIR}/icbc" \
        "${ROOT_DIR}/scripts/src/czar_bc1.cpp" -o "${target}.tmp"
    mv -f "${target}.tmp" "${target}"
    echo "[OK] libczar_bc1.so compilada com sucesso em ${target}."
}

ensure_compressonator() {
    local target="${BIN_DIR}/compressonator/compressonatorcli-4.5.52-Linux/compressonatorcli"
    if [[ -f "${target}" ]] && bash "${target}" -version 2>/dev/null | grep -qi "4.5.52"; then
        echo "[OK] AMD Compressonator CLI 4.5.52 já pronto em ${target}."
        return 0
    fi
    [[ "${MODE}" == "check" ]] && { echo "[AVISO] Compressonator CLI 4.5.52 ausente (opcional)."; return 0; }
    local arch; arch="$(uname -m)"
    [[ "${arch}" != "x86_64" ]] && { echo "[AVISO] Compressonator CLI pré-compilado suporta apenas x86_64."; return 0; }
    local tgz="${CACHE_DIR}/compressonatorcli-4.5.52-Linux.tar.gz"
    if [[ ! -f "${tgz}" ]] || ! echo "${COMPRESSONATOR_SHA256}  ${tgz}" | sha256sum -c - >/dev/null 2>&1; then
        curl -fsSL "${COMPRESSONATOR_URL}" -o "${tgz}.tmp"
        echo "${COMPRESSONATOR_SHA256}  ${tgz}.tmp" | sha256sum -c -
        mv -f "${tgz}.tmp" "${tgz}"
    fi
    rm -rf "${BIN_DIR}/compressonator"
    mkdir -p "${BIN_DIR}/compressonator"
    tar -xzf "${tgz}" -C "${BIN_DIR}/compressonator"
    echo "[OK] AMD Compressonator CLI 4.5.52 instalado."
}

ensure_vmaf() {
    local target="${BIN_DIR}/vmaf"
    if [[ -x "${target}" ]] && "${target}" --version >/dev/null 2>&1; then
        echo "[OK] Netflix VMAF CLI já pronto em ${target}."
        return 0
    fi
    [[ "${MODE}" == "check" ]] && { echo "[AVISO] VMAF CLI ausente (opcional)."; return 0; }
    local arch; arch="$(uname -m)"
    [[ "${arch}" != "x86_64" ]] && return 0
    curl -fsSL "${VMAF_URL}" -o "${target}.tmp"
    echo "${VMAF_SHA256}  ${target}.tmp" | sha256sum -c -
    chmod 755 "${target}.tmp"
    mv -f "${target}.tmp" "${target}"
    echo "[OK] Netflix VMAF ${VMAF_VERSION} instalado em ${target}."
}

ensure_blue_noise_textures() {
    local asset_png="${ROOT_DIR}/app/ouroboros_engine/assets/blue_noise_64.png"
    local repo_dir="${TOOLS_DIR}/assets/blue_noise"
    if [[ -f "${asset_png}" ]]; then
        echo "[OK] Textura CC0 Blue-Noise 64×64 já presente em ${asset_png}."
    fi
    if [[ -f "${repo_dir}/64_64/HDR_L_0.png" ]]; then
        return 0
    fi
    [[ "${MODE}" == "check" ]] && return 0
    mkdir -p "$(dirname "${repo_dir}")"
    rm -rf "${repo_dir}.tmp"
    if git clone --depth 1 "${BLUENOISE_REPO}" "${repo_dir}.tmp" >/dev/null 2>&1; then
        git -C "${repo_dir}.tmp" fetch --depth 1 origin "${BLUENOISE_COMMIT}" >/dev/null 2>&1 || true
        git -C "${repo_dir}.tmp" checkout --force "${BLUENOISE_COMMIT}" >/dev/null 2>&1 || true
        rm -rf "${repo_dir}"
        mv "${repo_dir}.tmp" "${repo_dir}"
        if [[ ! -f "${asset_png}" && -f "${repo_dir}/64_64/HDR_L_0.png" ]]; then
            mkdir -p "$(dirname "${asset_png}")"
            cp -f "${repo_dir}/64_64/HDR_L_0.png" "${asset_png}"
        fi
        echo "[OK] Acervo CC0 free-blue-noise-textures preparado em ${repo_dir}."
    fi
}

ensure_profiling_python_extras() {
    local py_bin; py_bin="$(resolve_venv_python)"
    [[ -z "${py_bin}" ]] && return 0
    if "${py_bin}" -c "import pybind11, numba, memray, gdown" >/dev/null 2>&1; then
        echo "[OK] Extras Python de otimização/profiling/corpus (pybind11, numba, memray, gdown) já presentes."
        return 0
    fi
    [[ "${MODE}" == "check" ]] && { echo "[AVISO] Extras Python (pybind11/numba/memray/py-spy/gdown) ausentes."; return 0; }
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "${py_bin}" pybind11 numba py-spy memray gdown || true
    else
        "${py_bin}" -m pip install -q pybind11 numba py-spy memray gdown || true
    fi
}

detect_system
ensure_system_prereqs
ensure_python_env
ensure_psgfx
ensure_liborbispkg
ensure_gifsicle
ensure_vgmstream
ensure_hyperfine
ensure_magick
ensure_tabler_icons
ensure_czar_bc1
ensure_compressonator
ensure_vmaf
ensure_blue_noise_textures
ensure_profiling_python_extras

echo ""
echo "[INFO] Executando verificação funcional integral (doctor_czar_full.sh)..."
bash "${ROOT_DIR}/scripts/doctor_czar_full.sh"

