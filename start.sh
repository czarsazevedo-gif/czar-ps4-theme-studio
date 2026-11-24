#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

# Protect the Studio against terminal disconnects / WebSocket drops (SIGHUP).
trap '' HUP

MODE="foreground"
SUPERVISOR="${CZAR_SUPERVISOR:-1}"
for arg in "$@"; do
  case "$arg" in
    --daemon|-d) MODE="daemon" ;;
    --stop) MODE="stop" ;;
    --status) MODE="status" ;;
    --no-supervisor) SUPERVISOR="0" ;;
    --help|-h)
      cat <<'EOF'
Uso: ./start.sh [--daemon | --stop | --status | --no-supervisor]
  (padrão)         Inicia o servidor protegido contra SIGHUP com supervisor auto-restart.
  --daemon, -d     Inicia em segundo plano (desacoplado do terminal) gravando logs em workspace/.
  --status         Exibe o status do processo em segundo plano e testa /healthz.
  --stop           Encerra o servidor/supervisor iniciado em segundo plano.
  --no-supervisor  Executa o uvicorn diretamente (sem loop de auto-restart).

Variáveis opcionais:
  CZAR_PORT=8000                  Porta HTTP (padrão: 8000)
  CZAR_HOST=0.0.0.0               Endereço bind (padrão: 0.0.0.0)
  CZAR_KEEP_ALIVE_TIMEOUT=120     HTTP Keep-Alive timeout do uvicorn em segundos
  CZAR_GRACEFUL_TIMEOUT=30        Timeout de desligamento gracioso em segundos
  CZAR_AUTO_INSTALL_FORGE=0       Se 1, instala ferramentas opcionais Forge automaticamente
EOF
      exit 0
      ;;
  esac
done

export PATH="$PWD/tools/bin:$PWD/tools/dotnet:$PATH"
if [[ -n "${CZAR_VENV:-}" && -x "${CZAR_VENV}/bin/python" ]]; then
  export PATH="${CZAR_VENV}/bin:$PATH"
elif [[ -x ./.venv/bin/python ]]; then
  export PATH="$PWD/.venv/bin:$PATH"
elif [[ -x "$HOME/.venv/bin/python" ]]; then
  export PATH="$HOME/.venv/bin:$PATH"
fi

port="${CZAR_PORT:-8000}"
host="${CZAR_HOST:-0.0.0.0}"
keep_alive="${CZAR_KEEP_ALIVE_TIMEOUT:-120}"
graceful="${CZAR_GRACEFUL_TIMEOUT:-30}"

mkdir -p workspace
PID_FILE="$PWD/workspace/.czar-studio-${port}.pid"
LOG_FILE="$PWD/workspace/czar-studio-${port}.log"

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

if [[ "$MODE" == "status" ]]; then
  if is_running; then
    pid="$(cat "$PID_FILE")"
    echo "Czar PS4 Theme Studio em execução (PID $pid, porta $port, log: $LOG_FILE)."
    if command -v curl >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
      echo "Sonda /healthz: OK"
    else
      echo "Sonda /healthz: indisponível"
    fi
    exit 0
  else
    echo "Czar PS4 Theme Studio não está em execução em segundo plano (porta $port)."
    exit 1
  fi
fi

if [[ "$MODE" == "stop" ]]; then
  if is_running; then
    pid="$(cat "$PID_FILE")"
    echo "Encerrando Czar PS4 Theme Studio (PID $pid)..."
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "Servidor encerrado."
  else
    rm -f "$PID_FILE"
    echo "Nenhum processo ativo encontrado para a porta $port."
  fi
  exit 0
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
svg_ok="ausente"
[[ -x ./tools/bin/psgfx-at9 ]] && at9_ok="disponível"
[[ -x ./tools/bin/liborbispkg/pkgtool ]] && pkg_ok="disponível"
if command -v magick >/dev/null 2>&1 || [[ -x ./tools/bin/magick ]] || (command -v convert >/dev/null 2>&1 && convert -version 2>/dev/null | grep -qi ImageMagick); then
  magick_ok="disponível"
fi
if "$PY_BIN" -c 'import cairosvg' 2>/dev/null; then
  svg_ok="cairosvg"
elif command -v rsvg-convert >/dev/null 2>&1; then
  svg_ok="rsvg-convert"
elif "$PY_BIN" -c 'import resvg_py' 2>/dev/null; then
  svg_ok="resvg-py"
elif [[ "$magick_ok" == "disponível" ]]; then
  svg_ok="imagemagick"
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

if [[ "$MODE" == "daemon" ]]; then
  if is_running; then
    echo "Czar PS4 Theme Studio já está em execução (PID $(cat "$PID_FILE"), porta $port)."
    exit 0
  fi
  echo "Iniciando Czar PS4 Theme Studio em segundo plano (desacoplado de SIGHUP)..."
  nohup env CZAR_SUPERVISOR="$SUPERVISOR" CZAR_PORT="$port" CZAR_HOST="$host" \
    CZAR_KEEP_ALIVE_TIMEOUT="$keep_alive" CZAR_GRACEFUL_TIMEOUT="$graceful" \
    bash "$PWD/start.sh" </dev/null >>"$LOG_FILE" 2>&1 &
  daemon_pid=$!
  disown "$daemon_pid" 2>/dev/null || true
  echo "$daemon_pid" >"$PID_FILE"
  if command -v curl >/dev/null 2>&1; then
    for _ in $(seq 1 40); do
      if curl -fsS "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
        echo "Servidor ativo (PID $daemon_pid) · porta $port · sonda /healthz OK · log: $LOG_FILE"
        exit 0
      fi
      kill -0 "$daemon_pid" 2>/dev/null || break
      sleep 0.25
    done
  fi
  echo "Supervisor iniciado (PID $daemon_pid) · log: $LOG_FILE"
  exit 0
fi

echo "CZAR FORGE · ATRAC9: ${at9_ok} · PKG: ${pkg_ok} · ImageMagick: ${magick_ok} · SVG: ${svg_ok}"
echo "Czar PS4 Theme Studio · abra a porta privada $port no painel Portas (sonda: /healthz)."
echo "Sessão contínua (>= 5h): SIGHUP bloqueado · keep-alive HTTP ${keep_alive}s · heartbeat /api/keepalive."
echo "Dica GitHub Codespaces: em github.com/settings/codespaces, ajuste 'Default idle timeout' para 240 min (máximo)."

UVICORN_ARGS=(
  -m uvicorn app.main:app
  --host "$host"
  --port "$port"
  --timeout-keep-alive "$keep_alive"
  --timeout-graceful-shutdown "$graceful"
  --proxy-headers
  --forwarded-allow-ips="*"
)

if [[ "$SUPERVISOR" != "1" ]]; then
  exec "$PY_BIN" "${UVICORN_ARGS[@]}"
fi

echo "$$" >"$PID_FILE"
STOPPING=0
CHILD_PID=""

cleanup_supervisor() {
  STOPPING=1
  if [[ -n "$CHILD_PID" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
    kill -TERM "$CHILD_PID" 2>/dev/null || true
    wait "$CHILD_PID" 2>/dev/null || true
  fi
  if [[ -f "$PID_FILE" ]] && [[ "$(cat "$PID_FILE" 2>/dev/null || true)" == "$$" ]]; then
    rm -f "$PID_FILE"
  fi
  exit 0
}
trap cleanup_supervisor INT TERM

backoff=1
while [[ "$STOPPING" -eq 0 ]]; do
  started_at="$(date +%s)"
  "$PY_BIN" "${UVICORN_ARGS[@]}" &
  CHILD_PID=$!
  set +e
  wait "$CHILD_PID"
  rc=$?
  set -e
  CHILD_PID=""
  if [[ "$STOPPING" -ne 0 ]]; then
    break
  fi
  elapsed=$(( $(date +%s) - started_at ))
  if [[ "$elapsed" -ge 60 ]]; then
    backoff=1
  fi
  echo "[supervisor] uvicorn encerrou (código ${rc}, uptime ${elapsed}s). Reiniciando em ${backoff}s..." >&2
  sleep "$backoff"
  if [[ "$backoff" -lt 15 ]]; then
    backoff=$(( backoff * 2 ))
    [[ "$backoff" -gt 15 ]] && backoff=15
  fi
done
