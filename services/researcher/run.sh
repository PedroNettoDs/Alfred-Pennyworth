#!/usr/bin/env bash
# Inicia o Alfred Research Service
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$(dirname "$(dirname "$SCRIPT_DIR")")/$(basename "$(dirname "$SCRIPT_DIR")")/../.env"

# Carrega .env do projeto
ENV_FILE="$SCRIPT_DIR/../../.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

PORT="${PORT_RESEARCHER:-7071}"

cd "$SCRIPT_DIR"

if [[ ! -d "venv" ]]; then
    echo "[researcher] Criando venv..."
    python3 -m venv venv
    venv/bin/pip install --quiet -r requirements.txt
fi

echo "[researcher] Iniciando na porta $PORT..."
exec venv/bin/uvicorn main:app --host 0.0.0.0 --port "$PORT"
