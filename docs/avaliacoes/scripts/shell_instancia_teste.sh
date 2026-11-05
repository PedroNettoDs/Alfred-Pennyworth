#!/usr/bin/env bash
# Sobe uma instância de TESTE do shell-executor em 127.0.0.1:${PORTA:-17071}.
# Não lê o .env: ambiente mínimo (env -i) e um token de teste próprio, passado em TOKEN.
# Uso:
#   TOKEN=teste123 VENV=/caminho/do/venv docs/avaliacoes/scripts/shell_instancia_teste.sh
# VENV: qualquer venv com fastapi + uvicorn (o do executor de produção serve; ele só é lido).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
VENV="${VENV:-$REPO/services/shell-executor/venv}"
PORTA="${PORTA:-17071}"
: "${TOKEN:?defina TOKEN (token de teste, não o do .env)}"
cd "$REPO/services/shell-executor"
exec env -i PATH="$PATH" HOME="$HOME" USER="$USER" LANG="${LANG:-C.UTF-8}" \
    SHELL_EXECUTOR_TOKEN="$TOKEN" \
    "$VENV/bin/uvicorn" main:app --host 127.0.0.1 --port "$PORTA" --log-level warning
