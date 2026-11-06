#!/usr/bin/env bash
# Bench de tokens retomável: um flock por pedido; braço já gravado em tokens.jsonl é pulado.
cd "$(dirname "$0")/../../../../.."
R=docs/avaliacoes/scripts/skills_proto/resultados
PY=${PY:-/home/netto/dev/Alfred-Pennyworth/services/shell-executor/venv/bin/python}
T=docs/avaliacoes/scripts/skills_tokens.py
k=0
for i in 1 4 7 9 11 14 17 19 22 25; do
    flock /tmp/alfred-bench-gpu.lock sh -c "date +%T; cat /proc/loadavg; curl -s http://127.0.0.1:11434/api/ps | head -c 300; echo; $PY $T rodar --id $i --rotacao $k --saida $R/tokens.jsonl"
    k=$(( (k+1) % 3 ))
done
date +%T; echo FIM
