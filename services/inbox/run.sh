#!/usr/bin/env bash
# Alfred Inbox Assistant — wrapper.
#   ./run.sh                 → ciclo completo
#   ./run.sh scan            → só classifica (dry-run)
#   ./run.sh jornal          → só a edição do dia
#   ./run.sh semanal [--semana AAAA-Www] → podcast semanal (VAULT_NOTICIAS/podcast)
#   ./run.sh pendencias      → fila vou/não vou
#   ./run.sh responder ID vou|nao-vou
#   ./run.sh ouvir ID
#   ./run.sh responder-texto ID "texto"   → pendência de WhatsApp (envia pela Evolution API)
#   ./run.sh whatsapp status|qrcode|webhook URL|simular payload.json|notificar ID|fila|processar-fila
#   ./run.sh limpeza [candidatos|propor|lotes|ver ID|aprovar ID|recusar ID] → limpeza pós-podcast (LIMPEZA.md)
#   ./run.sh serve           → API HTTP (FastAPI) na porta PORT_INBOX, para o N8N / Evolution
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

if [[ -f "$ENV_FILE" ]]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        export "$key"="$value"
    done < <(grep -E '^[A-Z_]+=.' "$ENV_FILE")
fi

cd "$SCRIPT_DIR"

if [[ ! -d "venv" ]]; then
    echo "[inbox] Criando venv..."
    python3 -m venv venv
    venv/bin/pip install --quiet -r requirements.txt
fi

if [[ "${1:-}" == "serve" ]]; then
    exec venv/bin/uvicorn main:app --host 0.0.0.0 --port "${PORT_INBOX:-7072}"
fi

exec venv/bin/python inbox-assistant.py "$@"
