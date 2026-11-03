#!/usr/bin/env bash
# Avaliação de Robustez — cobertura de gravar_atomico.
# Lista todo ponto de services/ e tools/ que grava arquivo, separando quem já usa gravação atômica.
# Só lê o código (nada de dados). Rode da raiz do repositório:
#   bash docs/avaliacoes/scripts/robustez_cobertura.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

FILTRO='/venv/|/tests/|site-packages'
ESCRITA="write_text\(|write_bytes\(|open\([^)]*['\"](w|wb|a|ab|x|w\+|r\+)['\"]|\.open\(['\"](w|a)['\"]|os\.fdopen\(|json\.dump\(|\.save\(str"
ATOMICO="gravar_atomico\(|_gravar_atomico\(|_gravar_json_atomico\(|os\.replace\(|\.replace\((tmp|FILA_FILE|IDS_ENVIADOS_FILE)"

echo "== Pontos que gravam arquivo (todas as formas)"
grep -rnE "$ESCRITA" --include=*.py services tools | grep -vE "$FILTRO" | sort
echo
echo "== Pontos que já fazem troca atômica (gravar_atomico, implementações próprias, os.replace)"
grep -rnE "$ATOMICO" --include=*.py services tools | grep -vE "$FILTRO" | grep -v "def " | sort
echo
echo "== Contagem"
printf "gravações de arquivo: %s\n" "$(grep -rnE "$ESCRITA" --include=*.py services tools | grep -vE "$FILTRO" | wc -l)"
printf "chamadas atômicas:    %s\n" "$(grep -rnE "$ATOMICO" --include=*.py services tools | grep -vE "$FILTRO" | grep -v "def " | wc -l)"
