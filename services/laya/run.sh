#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

if [[ -f "$ENV_FILE" ]]; then
    # leitura literal (como services/inbox/run.sh): `source` executaria o .env como shell
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        export "$key"="$value"
    done < <(grep -E '^[A-Z_][A-Z0-9_]*=' "$ENV_FILE")
fi

cd "$SCRIPT_DIR"

if [[ ! -d "venv" ]]; then
    echo "[laya] Criando venv..."
    python3 -m venv venv
    venv/bin/pip install --quiet -r requirements.txt
fi

# Só local: decisões de máquina não saem do host
export LAYA_HOST="127.0.0.1"
export LAYA_PORT="${PORT_LAYA:-7074}"
export LAYA_PRELOAD="${LAYA_PRELOAD:-1}"
export LAYA_MODELS="${LAYA_MODELS:-multilingual}"
export LAYA_DEFAULT_MODEL="${LAYA_DEFAULT_MODEL:-multilingual}"
export LAYA_MAX_LOADED="${LAYA_MAX_LOADED:-1}"
# Checkpoint treinado da porta da GPU (treino/gerar_porta.py + laya-train, ver about.md): se existe, é servido
# como "porta" e fica na memória junto com o multilingual (~650 MB de RAM cada; trocar a cada pedido custa segundos).
if [[ -d "treino/dados/modelo-porta" && -z "${LAYA_EXTRA_MODELS:-}" ]]; then
    export LAYA_EXTRA_MODELS="{\"porta\": \"$SCRIPT_DIR/treino/dados/modelo-porta\"}"
    export LAYA_MODELS="$LAYA_MODELS,porta"
    [[ "$LAYA_MAX_LOADED" -lt 2 ]] && export LAYA_MAX_LOADED=2
fi
# CPU por padrão: a GPU de 12 GB fica para o Ollama (gemma4:12b + bge-m3 não cabem com o Laya ao lado).
# LAYA_THREADS ≤ núcleos físicos (6 no Ryzen 5 4500); passar disso piora muito (ver docstring de laya.serve).
export LAYA_DEVICE="${LAYA_DEVICE:-cpu}"
export LAYA_THREADS="${LAYA_THREADS:-6}"
# Pesos baixados uma vez do Hugging Face (~/.cache/huggingface); depois roda offline.
# Sem isto o Router consulta o Hub a cada recarga do checkpoint (~17 s por requisição).
if ls "${HF_HOME:-$HOME/.cache/huggingface}"/hub/models--convaiinnovations--laya* >/dev/null 2>&1; then
    export HF_HUB_OFFLINE=1
fi
echo "[laya] Iniciando em $LAYA_HOST:$LAYA_PORT (modelos: $LAYA_MODELS)..."
exec venv/bin/laya-serve
