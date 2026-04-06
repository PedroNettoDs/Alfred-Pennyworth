#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

if [[ -f "$ENV_FILE" ]]; then
    set -a; source "$ENV_FILE"; set +a
fi

cd "$SCRIPT_DIR"

if [[ ! -d "venv" ]]; then
    echo "[researcher] Criando venv..."
    python3 -m venv venv
    venv/bin/pip install --quiet -r requirements.txt
fi

PORT="${PORT_RESEARCHER:-7071}"
echo "[researcher] Iniciando na porta $PORT..."
exec venv/bin/uvicorn main:app --host 0.0.0.0 --port "$PORT"
