#!/usr/bin/env bash
# Avaliação da memória — roda todas as medições que usam o Ollama, uma etapa por vez, com o amostrador de
# ruído ligado durante cada etapa. O lock da GPU compartilhada (/tmp/alfred-bench-gpu.lock) é tomado uma vez
# por modelo (cinco etapas seguidas, ~10 min) e solto entre um modelo e outro para as demais avaliações.
#
# Uso: memoria_rodar.sh <pasta-de-trabalho> <clone-do-odysseus> [rodadas]
#   pasta-de-trabalho: scratchpad (bancos temporários, venvs e resultados) — nunca o cofre real
#   venvs esperados na pasta: venv-bench (httpx, pyyaml, numpy, faiss-cpu) e venv-odysseus (vazio)
# O .env do Alfred não é lido: as variáveis abaixo imitam o .env.example com os modelos desta máquina.
set -uo pipefail

PASTA=$(realpath "$1"); ODY=$(realpath "$2"); RODADAS=${3:-3}
AQUI=$(cd "$(dirname "$0")" && pwd)
SERVICES=$(realpath "$AQUI/../../../services")
LOCK=/tmp/alfred-bench-gpu.lock
RES=$(realpath -m "${RES:-$PASTA/res}")   # RES=<pasta> grava os resultados fora da pasta de trabalho
mkdir -p "$RES"

export OLLAMA_BASE_URL=http://localhost:11434 OLLAMA_THINK=false OLLAMA_NUM_CTX=8192
export MODEL_EMBED=bge-m3 OLLAMA_EMBED_NUM_GPU=0 MEMORIA_DB="$PASTA/memoria_bench.db"
unset VAULT_ALFRED VAULT_PEDRO MODEL_CLASSIFY || true

etapa() {  # etapa <nome> <comando...> — com amostrador de ruído; falha de uma etapa não para as outras
  local nome=$1; shift
  python3 "$AQUI/memoria_ruido.py" --saida "$RES/ruido_$nome.jsonl" & local amostrador=$!
  local inicio; inicio=$(date +%s)
  "$@" > "$RES/log_$nome.txt" 2>&1; local status=$?
  kill "$amostrador"; wait "$amostrador" 2>/dev/null
  echo "$(date +%T) $nome: $(( $(date +%s) - inicio ))s status=$status"
}

exec 9>>"$LOCK"
for M in gemma4:12b gemma4:e4b; do
  T=${M#gemma4:}
  echo "$(date +%T) esperando o lock da GPU para $M"; flock 9; echo "$(date +%T) lock tomado"
  etapa "alfred_extracao_$T" "$PASTA/venv-bench/bin/python" "$AQUI/memoria_extracao.py" --services "$SERVICES" \
    --trechos "$AQUI/memoria_trechos.json" --modelo "$M" --rodadas "$RODADAS" --saida "$RES/alfred_$T.json"
  etapa "odysseus_extracao_$T" env -i PATH=/usr/bin:/bin HOME="$PASTA" "$PASTA/venv-odysseus/bin/python" -I \
    "$AQUI/memoria_odysseus.py" extrair --odysseus "$ODY" --trechos "$AQUI/memoria_trechos.json" --modelo "$M" \
    --rodadas "$RODADAS" --saida "$RES/odysseus_$T.json"
  etapa "alfred_dedup_$T" "$PASTA/venv-bench/bin/python" "$AQUI/memoria_dedup.py" --services "$SERVICES" \
    --dados "$AQUI/memoria_dedup.json" --modelo "$M" --saida "$RES/alfred_dedup_$T.json"
  etapa "odysseus_dedup_$T" env -i PATH=/usr/bin:/bin HOME="$PASTA" "$PASTA/venv-odysseus/bin/python" -I \
    "$AQUI/memoria_odysseus.py" deduplicar --odysseus "$ODY" --dados "$AQUI/memoria_dedup.json" --modelo "$M" \
    --pasta "$PASTA/ody_dedup_$T" --saida "$RES/odysseus_dedup_$T.json"
  # Mesmo fluxo com o limiar do Alfred (0,85): separa o efeito do limiar (ajustado para o MiniLM) do resto
  etapa "odysseus_dedup085_$T" env -i PATH=/usr/bin:/bin HOME="$PASTA" "$PASTA/venv-odysseus/bin/python" -I \
    "$AQUI/memoria_odysseus.py" deduplicar --odysseus "$ODY" --dados "$AQUI/memoria_dedup.json" --modelo "$M" \
    --limiar 0.85 --pasta "$PASTA/ody_dedup085_$T" --saida "$RES/odysseus_dedup085_$T.json"
  flock -u 9
done
echo "pronto: $RES"
