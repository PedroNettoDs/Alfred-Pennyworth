#!/usr/bin/env bash
# Abre o Pestinha, o pet de tela que mostra a fila e o trabalho do Alfred (services/inbox/PESTINHA.md).
# Ele mora fora deste repositório: PESTINHA_DIR, ou ~/dev/pestinha. Quem instala é o Painel (Integrações)
# ou `services/inbox/run.sh integracoes instalar`. É o ExecStart da unit alfred-pestinha.service.
set -euo pipefail
DIR="${PESTINHA_DIR:-$HOME/dev/pestinha}"
if [[ ! -x "$DIR/venv/bin/python" || ! -f "$DIR/main.py" ]]; then
    echo "[pestinha] não instalado em $DIR — use o Painel (Integrações) ou: services/inbox/run.sh integracoes instalar" >&2
    exit 1
fi
exec "$DIR/venv/bin/python" "$DIR/main.py" "$@"
