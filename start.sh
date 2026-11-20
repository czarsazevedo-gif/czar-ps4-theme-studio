#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ -x .venv-motion/bin/python ]]; then export PATH="$PWD/.venv-motion/bin:$PATH"; fi
for tool in python ffmpeg ffprobe; do
  command -v "$tool" >/dev/null || { echo "Dependência ausente: $tool" >&2; exit 1; }
done
python -c 'import cv2, fastapi, multipart, numpy, PIL, uvicorn' || { echo "Execute ./.devcontainer/install-motion.sh no terminal do Codespace." >&2; exit 1; }
port="${CZAR_PORT:-8000}"
echo "Czar PS4 Theme Studio · abra a porta privada $port no painel Portas."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$port"
