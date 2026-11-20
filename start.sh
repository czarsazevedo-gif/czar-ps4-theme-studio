#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
for tool in python ffmpeg ffprobe; do
  command -v "$tool" >/dev/null || { echo "Dependência ausente: $tool" >&2; exit 1; }
done
python -c 'import fastapi, multipart, numpy, PIL, uvicorn' || { echo "Instale requirements.txt no Codespace." >&2; exit 1; }
port="${CZAR_PORT:-8000}"
echo "Czar PS4 Theme Studio · abra a porta privada $port no painel Portas."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$port"
