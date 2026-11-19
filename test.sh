#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

mode="${1:-all}"
case "$mode" in all|api|ui|legacy) ;; *) printf 'Uso: ./test.sh [all|api|ui|legacy]\n' >&2; exit 2 ;; esac
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONIOENCODING=utf-8
python tests/make_fixtures.py

work="$(mktemp -d)"
server=""
cleanup() {
  if [[ -n "$server" ]]; then kill "$server" 2>/dev/null || true; wait "$server" 2>/dev/null || true; fi
  rm -rf -- "$work"
}
trap cleanup EXIT

start_server() {
  local port="$1" old="$2"
  # Never reuse an existing server as it might have real projects open.
  python - "$port" <<'PY'
import socket
import sys

with socket.socket() as probe:
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", int(sys.argv[1])))
    except OSError as exc:
        sys.exit(f"Porta de testes indisponível: {exc}")
PY
  CZAR_LEGACY="$old" CZAR_WORKSPACE="$work/studio" CZAR_LEGACY_WORKSPACE="$work/legacy" \
    python -m uvicorn app.main:app --host 127.0.0.1 --port "$port" >"$work/server.log" 2>&1 &
  server=$!
  if ! python - "$port" "$server" <<'PY'
import os
import sys
import time
from urllib.request import urlopen

for _ in range(50):
    try:
        os.kill(int(sys.argv[2]), 0)
        with urlopen(f"http://127.0.0.1:{sys.argv[1]}/healthz", timeout=0.5):
            break
    except (OSError, TimeoutError):
        time.sleep(0.1)
else:
    sys.exit("Falha ao iniciar o servidor de testes.")
PY
  then cat "$work/server.log" >&2; exit 1; fi
}

if [[ "$mode" == all || "$mode" == api ]]; then
  python tests/test_dxt1.py
  python -m unittest discover -s tests -p 'studio_api.py' -v
  python -m unittest discover -s tests -p 'test_chunked_upload.py' -v
fi
if [[ "$mode" == all || "$mode" == ui ]]; then
  start_server "${CZAR_TEST_PORT:-8174}" 0
  CZAR_TEST_URL="http://127.0.0.1:${CZAR_TEST_PORT:-8174}" python tests/studio_browser.py
  kill "$server"
  wait "$server" || true
  server=""
fi
if [[ "$mode" == all || "$mode" == legacy ]]; then
  start_server 8000 1
  for suite in http slots theme pkg audio convert ui ui_slots ui_preview; do python "tests/test_$suite.py"; done
  printf 'As verificações de construção real de PKG e de AT9 dependem de ferramentas e fixtures externas.\n'
fi
