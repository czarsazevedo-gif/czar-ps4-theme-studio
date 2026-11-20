#!/usr/bin/env bash
# Wrapper compatível de provisionamento Codespaces — delega ao bootstrap integral único.
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${1:-}" == "--quick" ]]; then
    if bash "${ROOT_DIR}/scripts/doctor_czar_full.sh" >/dev/null 2>&1; then
        echo "[CZAR Setup] Toolchain completa já está pronta."
        exit 0
    fi
fi
exec bash "${ROOT_DIR}/scripts/bootstrap_czar_full.sh" --install
