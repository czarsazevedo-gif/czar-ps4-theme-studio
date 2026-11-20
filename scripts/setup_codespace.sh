#!/usr/bin/env bash
# Provisionamento completo e idempotente para GitHub Codespaces / Linux.
# Garante ffmpeg, ImageMagick (magick), g++, dependências Python, ATRAC9 e PKG Tools prontos.
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

QUICK=0
if [[ "${1:-}" == "--quick" ]]; then
    QUICK=1
fi

has_passwordless_sudo() {
    command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1
}

need_apt=()
command -v ffmpeg >/dev/null 2>&1 || need_apt+=(ffmpeg)
command -v g++ >/dev/null 2>&1 || need_apt+=(g++)
command -v curl >/dev/null 2>&1 || need_apt+=(curl)
command -v git >/dev/null 2>&1 || need_apt+=(git)
if ! command -v magick >/dev/null 2>&1 && ! command -v convert >/dev/null 2>&1; then
    need_apt+=(imagemagick)
fi

if (( ${#need_apt[@]} > 0 )); then
    if command -v apt-get >/dev/null 2>&1 && has_passwordless_sudo; then
        if [[ -f /etc/apt/sources.list.d/yarn.list ]]; then
            echo "[CZAR Setup] Removendo yarn.list com chave GPG expirada..."
            sudo -n rm -f /etc/apt/sources.list.d/yarn.list || true
        fi
        echo "[CZAR Setup] Instalando pacotes do sistema: ${need_apt[*]}..."
        sudo -n apt-get update -qq
        sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${need_apt[@]}"
    else
        echo "[CZAR Setup] Aviso: pacotes ausentes (${need_apt[*]}), sem apt-get/sudo automático." >&2
    fi
fi

# Detecta Python ativo (.venv local tem prioridade se existir)
PY_BIN="python3"
if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    PY_BIN="${ROOT_DIR}/.venv/bin/python"
elif command -v python >/dev/null 2>&1; then
    PY_BIN="python"
fi

if [[ "${QUICK}" == "0" ]] || ! "${PY_BIN}" -c 'import fastapi, multipart, numpy, PIL, uvicorn, cv2, scipy, skimage' >/dev/null 2>&1; then
    echo "[CZAR Setup] Instalando dependências Python (requirements.txt)..."
    "${PY_BIN}" -m pip install -q -r requirements.txt
fi

chmod +x "${ROOT_DIR}/start.sh" "${ROOT_DIR}/scripts/install_forge_tools.sh" "${ROOT_DIR}/scripts/setup_codespace.sh"

# Garante ATRAC9, PKG Tools e ImageMagick prontos
if [[ ! -x "${ROOT_DIR}/tools/bin/psgfx-at9" ]]; then
    echo "[CZAR Setup] Preparando ATRAC9 nativo..."
    bash "${ROOT_DIR}/scripts/install_forge_tools.sh" at9
fi

if [[ ! -x "${ROOT_DIR}/tools/bin/liborbispkg/pkgtool" ]]; then
    echo "[CZAR Setup] Preparando PKG Tools nativo (LibOrbisPkg + IRO_TAG=2)..."
    bash "${ROOT_DIR}/scripts/install_forge_tools.sh" pkg
fi

if [[ ! -x "${ROOT_DIR}/tools/bin/magick" ]] && ! command -v magick >/dev/null 2>&1; then
    echo "[CZAR Setup] Preparando ImageMagick (magick)..."
    bash "${ROOT_DIR}/scripts/install_forge_tools.sh" magick || true
fi

if [[ "${QUICK}" == "0" ]]; then
    echo "[CZAR Setup] Verificando integridade de todas as ferramentas..."
    "${PY_BIN}" "${ROOT_DIR}/scripts/diagnose_forge.py" || true
fi

echo "[CZAR Setup] Ambiente pronto! Inicie com: ./start.sh"
