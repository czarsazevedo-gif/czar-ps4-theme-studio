#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
for tool in python ffmpeg ffprobe; do
  command -v "$tool" >/dev/null || { echo "Dependência ausente: $tool" >&2; exit 1; }
done
python -c 'import fastapi, multipart, numpy, PIL, uvicorn, cv2, scipy, skimage' || { echo "Instale requirements.txt no Codespace: python -m pip install -r requirements.txt" >&2; exit 1; }
at9_ok="ausente (opcional: ./scripts/install_forge_tools.sh at9)"
pkg_ok="ausente (opcional: ./scripts/install_forge_tools.sh pkg)"
[[ -x ./tools/bin/psgfx-at9 ]] && at9_ok="disponível"
[[ -x ./tools/bin/liborbispkg/pkgtool ]] && pkg_ok="disponível"
if [[ "${CZAR_AUTO_INSTALL_FORGE:-0}" == "1" ]] && [[ -x ./scripts/install_forge_tools.sh ]]; then
  if [[ ! -x ./tools/bin/psgfx-at9 || ! -x ./tools/bin/liborbispkg/pkgtool ]]; then
    echo "CZAR_AUTO_INSTALL_FORGE=1 ativo: preparando ferramentas opcionais..."
    ./scripts/install_forge_tools.sh all || echo "Aviso: preparação opcional do Forge falhou; o editor continuará disponível." >&2
    [[ -x ./tools/bin/psgfx-at9 ]] && at9_ok="disponível"
    [[ -x ./tools/bin/liborbispkg/pkgtool ]] && pkg_ok="disponível"
  fi
fi
echo "CZAR FORGE · ATRAC9: ${at9_ok} · PKG: ${pkg_ok}"
port="${CZAR_PORT:-8000}"
echo "Czar PS4 Theme Studio · abra a porta privada $port no painel Portas (sonda: /healthz)."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$port"
