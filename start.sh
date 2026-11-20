#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

export PATH="$PWD/tools/bin:$PWD/tools/dotnet:$PATH"
if [[ -n "${CZAR_VENV:-}" && -x "${CZAR_VENV}/bin/python" ]]; then
  export PATH="${CZAR_VENV}/bin:$PATH"
elif [[ -x ./.venv/bin/python ]]; then
  export PATH="$PWD/.venv/bin:$PATH"
elif [[ -x "$HOME/.venv/bin/python" ]]; then
  export PATH="$HOME/.venv/bin:$PATH"
fi

PY_BIN="$(command -v python || command -v python3 || true)"
if [[ -z "$PY_BIN" ]]; then
  echo "Dependência ausente: python" >&2
  exit 1
fi
for tool in ffmpeg ffprobe; do
  command -v "$tool" >/dev/null || { echo "Dependência ausente: $tool (execute ./scripts/setup_codespace.sh)" >&2; exit 1; }
done
"$PY_BIN" -c 'import fastapi, multipart, numpy, PIL, uvicorn, cv2, scipy, skimage' || {
  echo "Dependências Python ausentes. Execute: ./scripts/setup_codespace.sh" >&2
  exit 1
}

at9_ok="ausente (ative pela barra de status ou ./scripts/install_forge_tools.sh at9)"
pkg_ok="ausente (ative pela barra de status ou ./scripts/install_forge_tools.sh pkg)"
magick_ok="ausente (opcional; ative pela barra de status ou ./scripts/install_forge_tools.sh magick)"
[[ -x ./tools/bin/psgfx-at9 ]] && at9_ok="disponível"
[[ -x ./tools/bin/liborbispkg/pkgtool ]] && pkg_ok="disponível"
if command -v magick >/dev/null 2>&1 || [[ -x ./tools/bin/magick ]] || (command -v convert >/dev/null 2>&1 && convert -version 2>/dev/null | grep -qi ImageMagick); then
  magick_ok="disponível"
fi

if [[ "${CZAR_AUTO_INSTALL_FORGE:-0}" == "1" ]] && [[ -x ./scripts/install_forge_tools.sh ]]; then
  if [[ ! -x ./tools/bin/psgfx-at9 || ! -x ./tools/bin/liborbispkg/pkgtool ]]; then
    echo "CZAR_AUTO_INSTALL_FORGE=1 ativo: preparando ferramentas opcionais..."
    ./scripts/install_forge_tools.sh all || echo "Aviso: preparação opcional do Forge falhou; o editor continuará disponível." >&2
    [[ -x ./tools/bin/psgfx-at9 ]] && at9_ok="disponível"
    [[ -x ./tools/bin/liborbispkg/pkgtool ]] && pkg_ok="disponível"
    if command -v magick >/dev/null 2>&1 || [[ -x ./tools/bin/magick ]]; then magick_ok="disponível"; fi
  fi
fi

echo "CZAR FORGE · ATRAC9: ${at9_ok} · PKG: ${pkg_ok} · ImageMagick: ${magick_ok}"
port="${CZAR_PORT:-8000}"
echo "Czar PS4 Theme Studio · abra a porta privada $port no painel Portas (sonda: /healthz)."
exec "$PY_BIN" -m uvicorn app.main:app --host 0.0.0.0 --port "$port"
