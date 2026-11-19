#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

for tool in python ffmpeg ffprobe; do
  command -v "$tool" >/dev/null || { printf 'Dependência ausente: %s\n' "$tool" >&2; exit 1; }
done
python - <<'PY'
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("É necessário Python 3.11 ou superior.")
try:
    import fastapi
    import multipart
    import numpy
    import PIL
    import pydantic
    import uvicorn
except ImportError:
    sys.exit("Instale as dependências com python -m pip install -r requirements.txt.")
from app.studio.storage import ROOT
for name in ("projects", "staging"):
    (Path(ROOT) / name).mkdir(parents=True, exist_ok=True, mode=0o700)
PY

export CZAR_LEGACY=0
if [[ -n "${CODESPACE_NAME:-}" && -n "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]]; then
  printf 'Czar PS4 Theme Studio · https://%s-8000.%s\n' "$CODESPACE_NAME" "$GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"
  printf 'No painel Portas, mantenha a porta 8000 como Privada.\n'
else
  printf 'Czar PS4 Theme Studio · http://127.0.0.1:8000\n'
fi
printf 'Health check em /healthz. Ctrl+C encerra o servidor.\n'
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --limit-concurrency 64
