#!/usr/bin/env bash
# Instalação do Alfred do zero numa bancada Ubuntu 24.04 descartável, seguindo só o que o
# instalar.sh manda fazer. Uso: medir_do_zero.sh <repositório git> <pasta de saída>
# Saída: <pasta>/tempos.tsv (passo, segundos, código de saída) e um .log por passo.
# A GPU/Ollama fica fora: o instalar.sh acusa "ollama" como pendente e a medição segue.
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${1:?repositório}"; OUT="${2:?pasta de saída}"
mkdir -p "$OUT"; : >"$OUT/tempos.tsv"
CT=${CT:-alfred-aval-zero}
source "$AQUI/lib_bancada.sh"

t_inicio=$(date +%s)
bancada_subir || { echo "bancada não subiu"; exit 1; }
repo_para_bancada "$REPO" || { echo "bundle falhou"; exit 1; }

# 0. git não vem no Ubuntu desktop; clone a partir do bundle (sem rede para o GitHub)
passo 00-git 'sudo apt-get update -qq && sudo apt-get install -y -qq git >/dev/null'
passo 01-clone 'git clone -q /tmp/alfred.bundle Alfred-Pennyworth && git -C Alfred-Pennyworth log -1 --format=%h'

# 1. o que o próprio instalador diz que falta
passo 02-verificar 'Alfred-Pennyworth/services/scripts/instalar.sh --verificar'
# 2. instala exatamente os pacotes apt que ele sugere para Ubuntu nas linhas ✖ (ollama = curl|sh, fora)
pacotes=$(grep '✖' "$OUT/02-verificar.log" | grep -o 'Ubuntu: sudo apt install [^·]*' \
          | sed 's/Ubuntu: sudo apt install//' | tr '\n' ' ')
echo "pacotes sugeridos: $pacotes" | tee "$OUT/pacotes.txt"
passo 03-apt "sudo apt-get install -y -qq $pacotes >/dev/null"
passo 04-verificar 'Alfred-Pennyworth/services/scripts/instalar.sh --verificar'
# 3. o verificar pede docker no grupo + sair/entrar; cada passo abaixo já é uma sessão de login nova
if grep -q 'usermod -aG docker' "$OUT/04-verificar.log"; then
    passo 05-grupo-docker 'sudo systemctl enable --now docker && sudo usermod -aG docker $USER'
fi
passo 06-verificar 'Alfred-Pennyworth/services/scripts/instalar.sh --verificar'

# 4. instalação de fato (sem --modelos: Ollama fora da bancada)
passo 07-instalar 'Alfred-Pennyworth/services/scripts/instalar.sh'
esperar_saude 08-saude 900
inventario 09-inventario

# 5. idempotência: rodar de novo não deve quebrar nada
passo 10-reinstalar 'Alfred-Pennyworth/services/scripts/instalar.sh'
esperar_saude 11-saude 300
# 6. reinício da "máquina": tudo volta sozinho?
docker restart "$CT" >/dev/null && docker exec "$CT" systemctl is-system-running --wait >/dev/null 2>&1
esperar_saude 12-saude-pos-reboot 600

echo -e "total\t$(( $(date +%s) - t_inicio ))\t-" | tee -a "$OUT/tempos.tsv"
[[ "${MANTER:-0}" == 1 ]] || bancada_remover
