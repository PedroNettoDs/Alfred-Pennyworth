#!/usr/bin/env bash
# Atualização e rollback numa bancada descartável: instala uma versão antiga, faz `git pull` +
# instalar.sh, depois volta (`git checkout <antiga>` + instalar.sh) e reinicia a "máquina".
# Uso: [ANTIGO=8e71429] [INSTALAR_ARGS=--sem-containers] medir_atualizacao.sh <repositório git> <pasta de saída>
# Padrão --sem-containers: os containers não mudam entre as versões (mesmas tags) e a máquina está com
# pouca memória; INSTALAR_ARGS="" mede também o compose.
# Registra tempos, PIDs dos serviços (pegou o código novo?), chaves novas do .env.example que o .env
# não recebeu, versões dos pacotes e o que sobrou de pé depois do rollback.
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${1:?repositório}"; OUT="${2:?pasta de saída}"
ANTIGO=${ANTIGO:-8e71429}     # 27/10/2026: já tem instalar.sh e br-alfred; ainda sem Laya e com as portas antigas
mkdir -p "$OUT"; : >"$OUT/tempos.tsv"
CT=${CT:-alfred-aval-upd}
source "$AQUI/lib_bancada.sh"
R='cd ~/Alfred-Pennyworth'
INSTALAR_ARGS=${INSTALAR_ARGS---sem-containers}
SEM_LAYA="executor researcher inbox"; TODOS="executor researcher inbox laya"
[[ -z "$INSTALAR_ARGS" ]] && { SEM_LAYA+=" searxng chromadb evolution"; TODOS+=" searxng chromadb evolution"; }
pids() { passo "$1" "systemctl --user show -p MainPID --value alfred-executor alfred-researcher alfred-inbox-api alfred-laya 2>&1 | paste -sd' '"; }
env_novas() {   # chaves do .env.example que o .env não tem (só nomes de chave)
    passo "$1" "$R; comm -23 <(grep -oE '^[A-Z_][A-Z0-9_]*=' .env.example | sort -u) <(grep -oE '^[A-Z_][A-Z0-9_]*=' .env | sort -u) | tr -d = | paste -sd' '; grep -E '^PORT_' .env | paste -sd' '"
}

t_inicio=$(date +%s)
bancada_subir || { echo "bancada não subiu"; exit 1; }
repo_para_bancada "$REPO" || { echo "bundle falhou"; exit 1; }
passo 30-git 'sudo apt-get update -qq && sudo apt-get install -y -qq git >/dev/null'
passo 31-clone-antigo "git clone -q /tmp/alfred.bundle Alfred-Pennyworth && $R && git reset -q --hard $ANTIGO && git log -1 --format='%h %ad' --date=short"
passo 32-verificar "$R; services/scripts/instalar.sh --verificar"
pacotes=$(grep '✖' "$OUT/32-verificar.log" | grep -o 'Ubuntu: sudo apt install [^·]*' | sed 's/Ubuntu: sudo apt install//' | tr '\n' ' ')
passo 33-apt "sudo apt-get install -y -qq $pacotes >/dev/null && sudo systemctl enable --now docker && sudo usermod -aG docker \$USER"
passo 34-instalar-antigo "$R; services/scripts/instalar.sh $INSTALAR_ARGS"
SERVICOS=$SEM_LAYA esperar_saude 35-saude-antigo 900
pids 36-pids-antigo
inventario 37-inventario-antigo

# ── atualização ──
passo 40-pull "$R; git pull -q --ff-only && git log -1 --format='%h %ad' --date=short && git diff --stat $ANTIGO HEAD | tail -1"
pids 41-pids-pos-pull-sem-instalar
passo 42-instalar-pos-pull "$R; services/scripts/instalar.sh $INSTALAR_ARGS"
pids 43-pids-pos-instalar
SERVICOS=$TODOS esperar_saude 44-saude-pos-pull 900
env_novas 45-env-chaves-faltando
inventario 46-inventario-pos-pull

# ── rollback ──
passo 50-checkout-antigo "$R; git checkout -q $ANTIGO && git log -1 --format='%h' && git status --porcelain --ignored services/laya | head -5"
passo 51-instalar-pos-rollback "$R; services/scripts/instalar.sh $INSTALAR_ARGS"
pids 52-pids-pos-rollback
SERVICOS=$SEM_LAYA esperar_saude 53-saude-pos-rollback 300
inventario 54-inventario-pos-rollback
docker restart "$CT" >/dev/null && docker exec "$CT" systemctl is-system-running --wait >/dev/null 2>&1
SERVICOS=$SEM_LAYA esperar_saude 55-saude-rollback-reboot 600
passo 56-units-falhando "systemctl --user list-units 'alfred-*' --all --no-legend --plain | grep -vE ' (active|inactive) (running|exited|waiting|dead) ' ; systemctl --user is-active alfred-laya; true"

echo -e "total\t$(( $(date +%s) - t_inicio ))\t-" | tee -a "$OUT/tempos.tsv"
[[ "${MANTER:-0}" == 1 ]] || bancada_remover
