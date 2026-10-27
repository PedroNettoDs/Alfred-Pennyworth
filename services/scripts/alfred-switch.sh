#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  Alfred — liga / desliga ("modo jogo")
#
#    alfred-switch.sh toggle   → inverte o estado (é o que o atalho da área de trabalho chama)
#    alfred-switch.sh off      → desliga automações e verificações, libera a placa de vídeo
#    alfred-switch.sh on       → religa tudo
#    alfred-switch.sh status   → mostra o estado
#
#  DESLIGAR:
#    1. grava logs/alfred_desligado  → os run.sh recusam rodar sob o systemd (timers, API) mesmo
#       depois de reiniciar o PC: o modo jogo só termina quando você liga de novo
#    2. para os timers (briefing, e-mail, podcast, vigia) e qualquer execução em andamento
#    3. para a API do Alfred (:7072) — o WhatsApp deixa de ser processado
#    4. para a Evolution API (WhatsApp) — `docker stop`; com restart unless-stopped, continua parada após reboot
#    5. fecha as notificações que esperam clique (as pendências continuam aguardando; voltam ao religar)
#    6. descarrega os modelos do Ollama da VRAM (o serviço do Ollama continua instalado, ocioso)
#  LIGAR: o inverso, na ordem banco → Evolution → API → timers, e reabre as notificações pendentes.
# ══════════════════════════════════════════════════════════════
set -uo pipefail

RAIZ="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
FLAG="$RAIZ/logs/alfred_desligado"
AGUARDADORES="$RAIZ/logs/alfred_aguardadores_pausados.txt"
INBOX="$RAIZ/services/inbox"
DESKTOP_FILE="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")/alfred-switch.desktop"
APP_FILE="$HOME/.local/share/applications/alfred-switch.desktop"
OLLAMA="${OLLAMA_BASE_URL:-http://localhost:11434}"

TIMERS=(alfred-briefing-matinal.timer alfred-briefing-vespertino.timer alfred-inbox.timer alfred-semanal.timer alfred-vigia.timer)
SERVICOS=(alfred-briefing-matinal.service alfred-briefing-vespertino.service alfred-inbox.service alfred-semanal.service alfred-vigia.service)
CONTAINERS_ORDEM_LIGAR=(alfred-evolution-db alfred-evolution)
CONTAINERS_ORDEM_DESLIGAR=(alfred-evolution alfred-evolution-db)

notificar() {
    command -v notify-send >/dev/null && notify-send --app-name=Alfred --icon="$1" "$2" "$3" 2>/dev/null || true
}

log() { echo "[alfred-switch] $*"; }

esta_desligado() { [[ -f "$FLAG" ]]; }

atualizar_atalho() {
    # O mesmo .desktop na área de trabalho e no menu (KRunner / atalho global); nome e ícone mostram o estado.
    local nome icone comentario
    if esta_desligado; then
        nome="Alfred: modo jogo (clique para ligar)"
        icone="input-gaming"
        comentario="Alfred desligado desde $(head -1 "$FLAG" 2>/dev/null). Clique para religar."
    else
        nome="Alfred: ligado (clique para modo jogo)"
        icone="$RAIZ/docs/Alfred.png"
        comentario="Automações e verificações do Alfred ativas. Clique para desligar e liberar a GPU."
    fi
    for f in "$DESKTOP_FILE" "$APP_FILE"; do
        mkdir -p "$(dirname "$f")"
        cat > "$f" <<EOF
[Desktop Entry]
Type=Application
Name=$nome
Comment=$comentario
Exec=$RAIZ/services/scripts/alfred-switch.sh toggle
Icon=$icone
Terminal=false
Categories=Utility;
Keywords=alfred;jogo;game;liga;desliga;switch;
EOF
        chmod +x "$f"
    done
}

descarregar_ollama() {
    local modelos
    modelos=$(curl -s --max-time 5 "$OLLAMA/api/ps" | python3 -c "import sys,json
try: print(' '.join(m['name'] for m in json.load(sys.stdin).get('models', [])))
except Exception: pass" 2>/dev/null)
    for m in $modelos; do
        curl -s --max-time 20 "$OLLAMA/api/generate" -d "{\"model\": \"$m\", \"keep_alive\": 0}" >/dev/null && log "Ollama: $m descarregado da VRAM"
    done
}

pausar_aguardadores() {
    # Notificações que esperam clique viram units transitórias (systemd-run). Guarda o comando para reabrir.
    : > "$AGUARDADORES"
    local u
    for u in $(systemctl --user list-units --type=service --state=running --no-legend --plain 'alfred-aguardar-*' 'alfred-vigia-qr-*' 2>/dev/null | awk '{print $1}'); do
        systemctl --user show "$u" -p ExecStart --value 2>/dev/null \
            | grep -o 'argv\[\]=[^;]*' | head -1 | sed 's/^argv\[\]=//' >> "$AGUARDADORES"
        systemctl --user stop "$u" 2>/dev/null
    done
    local n; n=$(grep -c . "$AGUARDADORES" 2>/dev/null); n=${n:-0}
    if [[ "$n" -gt 0 ]]; then log "$n notificação(ões) pausada(s) — voltam quando religar"; fi
}

reabrir_aguardadores() {
    [[ -s "$AGUARDADORES" ]] || return 0
    local linha i=0
    while IFS= read -r linha; do
        [[ -z "$linha" || "$linha" == *_vigia-qr* ]] && continue   # o vigia avisa de novo se precisar
        i=$((i + 1))
        # shellcheck disable=SC2086
        systemd-run --user --collect --quiet --unit="alfred-aguardar-religado-$(date +%H%M%S)-$i" \
            --working-directory="$INBOX" -- $linha 2>/dev/null || true
    done < "$AGUARDADORES"
    rm -f "$AGUARDADORES"
    if [[ $i -gt 0 ]]; then log "$i notificação(ões) reaberta(s)"; fi
}

desligar() {
    if esta_desligado; then log "já está desligado (desde $(head -1 "$FLAG"))"; atualizar_atalho; return 0; fi
    mkdir -p "$(dirname "$FLAG")"
    printf '%s\nmodo jogo — ligue de novo com: %s on\n' "$(date '+%d/%m %H:%M')" "$0" > "$FLAG"
    log "desligando…"
    systemctl --user stop "${TIMERS[@]}" 2>/dev/null
    systemctl --user stop "${SERVICOS[@]}" 2>/dev/null
    systemctl --user stop alfred-inbox-api.service 2>/dev/null
    pausar_aguardadores
    if command -v docker >/dev/null; then
        for c in "${CONTAINERS_ORDEM_DESLIGAR[@]}"; do
            docker stop -t 20 "$c" >/dev/null 2>&1 && log "container $c parado"
        done
    fi
    descarregar_ollama
    atualizar_atalho
    log "Alfred DESLIGADO — timers, API, WhatsApp e vigia parados; GPU liberada."
    notificar input-gaming "Alfred — modo jogo" "Automações e verificações desligadas, GPU liberada. Clique no atalho de novo para religar."
}

ligar() {
    if ! esta_desligado; then log "já está ligado"; atualizar_atalho; return 0; fi
    rm -f "$FLAG"
    log "ligando…"
    if command -v docker >/dev/null; then
        for c in "${CONTAINERS_ORDEM_LIGAR[@]}"; do
            docker start "$c" >/dev/null 2>&1 && log "container $c iniciado"
        done
    fi
    systemctl --user start alfred-inbox-api.service 2>/dev/null && log "API do Alfred iniciada"
    # só os timers habilitados: um timer desligado de propósito (ex.: briefing vespertino) não volta com o "on"
    local t religados=()
    for t in "${TIMERS[@]}"; do
        systemctl --user is-enabled --quiet "$t" 2>/dev/null && systemctl --user start "$t" 2>/dev/null && religados+=("$t")
    done
    [[ ${#religados[@]} -gt 0 ]] && log "timers religados: ${religados[*]}"
    reabrir_aguardadores
    atualizar_atalho
    log "Alfred LIGADO."
    notificar "$RAIZ/docs/Alfred.png" "Alfred — de volta ao serviço" "Automações, WhatsApp e verificações religados. O vigia confere tudo em instantes."
}

status() {
    if esta_desligado; then echo "Alfred: DESLIGADO (modo jogo) desde $(head -1 "$FLAG")"; else echo "Alfred: LIGADO"; fi
    echo "API (:7072):     $(systemctl --user is-active alfred-inbox-api.service 2>/dev/null)"
    for t in "${TIMERS[@]}"; do printf '%-31s %s\n' "$t:" "$(systemctl --user is-active "$t" 2>/dev/null)"; done
    if command -v docker >/dev/null; then
        for c in "${CONTAINERS_ORDEM_LIGAR[@]}"; do
            printf '%-31s %s\n' "$c:" "$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo ausente)"
        done
    fi
    echo "Ollama na VRAM:  $(curl -s --max-time 5 "$OLLAMA/api/ps" | python3 -c "import sys,json
try: print(', '.join(m['name'] for m in json.load(sys.stdin).get('models', [])) or 'nenhum modelo')
except Exception: print('Ollama não respondeu')" 2>/dev/null)"
}

case "${1:-toggle}" in
    off|desligar)  desligar ;;
    on|ligar)      ligar ;;
    toggle)        if esta_desligado; then ligar; else desligar; fi ;;
    status)        status ;;
    atalho)        atualizar_atalho; echo "atalho: $DESKTOP_FILE" ;;
    *) echo "Uso: $0 [toggle|on|off|status|atalho]"; exit 1 ;;
esac
