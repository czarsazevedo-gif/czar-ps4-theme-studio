#!/usr/bin/env bash
# CZAR COMPLETE TOOLCHAIN — Verificador Funcional (wrapper POSIX)
# Uso: ./scripts/doctor_czar_full.sh [--json]
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="${ROOT_DIR}/tools/bin:${ROOT_DIR}/tools/dotnet:${PATH}"

PY_BIN=""
if [[ -n "${CZAR_VENV:-}" && -x "${CZAR_VENV}/bin/python" ]]; then
    PY_BIN="${CZAR_VENV}/bin/python"
elif [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    PY_BIN="${ROOT_DIR}/.venv/bin/python"
elif [[ -x "${HOME}/.venv/bin/python" ]]; then
    PY_BIN="${HOME}/.venv/bin/python"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    PY_BIN="${VIRTUAL_ENV}/bin/python"
else
    PY_BIN="$(command -v python3 || command -v python || true)"
fi

if [[ -z "${PY_BIN}" ]]; then
    echo "PYTHON: FALHA — nenhum interpretador Python encontrado." >&2
    exit 1
fi

exec "${PY_BIN}" "${ROOT_DIR}/scripts/doctor_czar_full.py" "$@"
