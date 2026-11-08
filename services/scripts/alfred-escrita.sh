#!/usr/bin/env bash
# Abre o WordCraft com a porta de controle e a janela do Alfred ao lado (services/inbox/ESCRITA.md).
#   alfred-escrita.sh [arquivo.docx]
set -euo pipefail

PORTA="${WORDCRAFT_CONTROL_PORT:-7981}"
URL="http://127.0.0.1:${PORT_INBOX:-7073}/painel/escrita"
LARGURA_JANELA=480

avisar() { notify-send -a Alfred "Alfred — Escrita" "$1" 2>/dev/null || echo "$1" >&2; }
porta_aberta() { (exec 3<>"/dev/tcp/127.0.0.1/$PORTA") 2>/dev/null; }

NAVEGADOR=""
for b in brave-browser chromium chromium-browser google-chrome; do
    if command -v "$b" >/dev/null; then NAVEGADOR="$b"; break; fi
done
[[ -n "$NAVEGADOR" ]] || { avisar "Instale Brave, Chromium ou Chrome para abrir a janela do Alfred."; exit 1; }

# Monitor principal: largura altura x y
read -r L A X Y < <(xrandr --current | sed -nE 's/.* connected primary ([0-9]+)x([0-9]+)\+([0-9]+)\+([0-9]+).*/\1 \2 \3 \4/p')

ABRIU=0
if ! porta_aberta; then
    if pgrep -f 'wordcraft-.*\.AppImage' >/dev/null; then
        avisar "Feche o WordCraft aberto (ele está sem a porta de controle) e abra por este atalho."
        exit 1
    fi
    APP="$(ls -t "$HOME"/Applications/wordcraft-*.AppImage 2>/dev/null | head -1 || true)"
    [[ -n "$APP" ]] || { avisar "AppImage do WordCraft não encontrado em ~/Applications."; exit 1; }
    nohup "$APP" --control "$PORTA" "$@" >/dev/null 2>&1 &
    disown
    ABRIU=1
    for _ in $(seq 1 75); do
        porta_aberta && break
        sleep 0.2
    done
    porta_aberta || { avisar "O WordCraft não abriu a porta de controle $PORTA."; exit 1; }
fi

if [[ "$ABRIU" == 1 ]] && command -v wmctrl >/dev/null; then
    for _ in $(seq 1 25); do
        wmctrl -lx | grep -qi 'wordcraft' && break
        sleep 0.2
    done
    wmctrl -x -r wordcraft -b remove,maximized_vert,maximized_horz || true
    wmctrl -x -r wordcraft -e "0,$X,$Y,$((L - LARGURA_JANELA)),$A" || true
fi

nohup "$NAVEGADOR" --app="$URL" --window-size="$LARGURA_JANELA,$A" --window-position="$((X + L - LARGURA_JANELA)),$Y" >/dev/null 2>&1 &
disown
