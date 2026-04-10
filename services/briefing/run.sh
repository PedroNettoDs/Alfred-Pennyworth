#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

# Carregar .env filtrando só linhas KEY=VALUE
if [[ -f "$ENV_FILE" ]]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        export "$key"="$value"
    done < <(grep -E '^[A-Z_]+=.' "$ENV_FILE")
fi

cd "$SCRIPT_DIR"

if [[ ! -d "venv" ]]; then
    echo "[briefing] Criando venv..."
    python3 -m venv venv
    venv/bin/pip install --quiet -r requirements.txt
fi

exec venv/bin/python tech-briefing.py "$@"
