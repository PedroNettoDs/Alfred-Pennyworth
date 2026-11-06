#!/usr/bin/env bash
# Avaliação de Pesquisa — roda as perguntas nos modos pedidos, uma execução por vez dentro do flock da GPU
# compartilhada (as outras avaliações e a produção usam o mesmo Ollama). Para cada execução anota em
# resultados/lock.jsonl quanto ela esperou pelo lock e a memória livre na hora (ruído das outras avaliações), e o
# bench acrescenta o resumo numérico em resultados/metricas.jsonl. Os brutos (relatório, notas, texto das páginas)
# vão para <saida>, fora do git.
#
#   docs/avaliacoes/scripts/rodar_bench.sh <saida> [modos...] [-- perguntas...]
#   ex.: docs/avaliacoes/scripts/rodar_bench.sh docs/avaliacoes/scripts/resultados/brutos rapido profundo -- produtos
#
# PAUSA_S (padrão 0): espera entre execuções, fora do lock — espaça as buscas no SearXNG (os motores de `general`
#   suspendem a instância por 1 h com "too many requests").
# MEM_MIN_MB (padrão 4000): para o lote se a memória disponível cair abaixo disso.
# MEM_PROFUNDO_MB (padrão 10000): modos profundo* esperam até 30 min por essa memória disponível (o
#   qwen3.6:35b-a3b da síntese tem ~14 GB fora da VRAM); se não aparecer, a execução é pulada e anotada.
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAIZ="$(cd "$AQUI/../../.." && pwd)"
# venv do researcher (só leitura): o deste checkout ou, num worktree sem venv, o do checkout principal
PY="${PY_RESEARCHER:-$RAIZ/services/researcher/venv/bin/python}"
[[ -x "$PY" ]] || PY="$(git -C "$RAIZ" rev-parse --path-format=absolute --git-common-dir)/../services/researcher/venv/bin/python"
[[ -x "$PY" ]] || { echo "venv do researcher não encontrado (defina PY_RESEARCHER)" >&2; exit 2; }
LOCK=/tmp/alfred-bench-gpu.lock
RESULTADOS="$AQUI/resultados"
SAIDA="$1"; shift
MODOS=(); PERGUNTAS=(tecnico noticia produtos academico brasil)
while [[ $# -gt 0 && "$1" != "--" ]]; do MODOS+=("$1"); shift; done
[[ "${1:-}" == "--" ]] && { shift; PERGUNTAS=("$@"); }
mkdir -p "$SAIDA" "$RESULTADOS"

for p in "${PERGUNTAS[@]}"; do
  for m in "${MODOS[@]}"; do
    livre=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
    swap=$(awk '/SwapTotal/ {t=$2} /SwapFree/ {f=$2} END {print int((t-f)/1024)}' /proc/meminfo)
    if (( livre < ${MEM_MIN_MB:-4000} )); then
      echo "memória disponível ${livre} MB < ${MEM_MIN_MB:-4000} MB — lote parado antes de $m/$p" >&2
      exit 2
    fi
    if [[ "$m" == profundo* ]]; then
      for _ in $(seq 60); do
        livre=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
        (( livre >= ${MEM_PROFUNDO_MB:-10000} )) && break
        sleep 30
      done
      if (( livre < ${MEM_PROFUNDO_MB:-10000} )); then
        echo "{\"modo\": \"$m\", \"pergunta\": \"$p\", \"status\": \"pulado_memoria\", \"mem_disponivel_mb\": $livre}" >> "$RESULTADOS/lock.jsonl"
        continue
      fi
    fi
    pedido=$(date +%s.%N)
    flock "$LOCK" bash -c "echo \$(date +%s.%N) > '$SAIDA/.lock-$m-$p'; '$PY' '$AQUI/bench_pesquisa.py' --modo '$m' --pergunta '$p' --saida '$SAIDA' --metricas '$RESULTADOS/metricas.jsonl'"
    status=$?
    obtido=$(cat "$SAIDA/.lock-$m-$p")
    echo "{\"modo\": \"$m\", \"pergunta\": \"$p\", \"espera_lock_s\": $(python3 -c "print(round($obtido-$pedido,1))"), \"status\": $status, \"mem_disponivel_mb\": $livre, \"swap_usado_mb\": $swap}" >> "$RESULTADOS/lock.jsonl"
    sleep "${PAUSA_S:-0}"
  done
done
