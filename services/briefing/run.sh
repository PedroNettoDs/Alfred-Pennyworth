#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

# ── modo jogo (services/scripts/alfred-switch.sh) ─────────────
# Desligado: nada que venha das units do systemd roda (elas exportam ALFRED_AUTOMACAO=1: timers,
# API no boot). Manual no terminal continua liberado; ALFRED_FORCAR=1 passa por cima.
# `serve` e `vigia` nunca rodam desligados.
ALFRED_FLAG="$SCRIPT_DIR/../../logs/alfred_desligado"
if [[ -f "$ALFRED_FLAG" && "${ALFRED_FORCAR:-0}" != "1" ]]; then
    if [[ "${ALFRED_AUTOMACAO:-0}" == "1" || "${1:-}" == "serve" || "${1:-}" == "vigia" ]]; then
        echo "[alfred] desligado (modo jogo desde $(head -1 "$ALFRED_FLAG")) — '${1:-}' não executado. Religue: services/scripts/alfred-switch.sh on"
        exit 0
    fi
fi

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
