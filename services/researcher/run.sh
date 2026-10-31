#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

if [[ -f "$ENV_FILE" ]]; then
    # leitura literal (como services/inbox/run.sh): `source` executaria o .env como shell — senha com $ ou ( derrubava o serviço
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        export "$key"="$value"
    done < <(grep -E '^[A-Z_][A-Z0-9_]*=' "$ENV_FILE")
fi

cd "$SCRIPT_DIR"

if [[ ! -d "venv" ]]; then
    echo "[researcher] Criando venv..."
    python3 -m venv venv
    venv/bin/pip install --quiet -r requirements.txt
fi

PORT="${PORT_RESEARCHER:-7072}"
echo "[researcher] Iniciando na porta $PORT..."
exec venv/bin/uvicorn main:app --host 0.0.0.0 --port "$PORT"
