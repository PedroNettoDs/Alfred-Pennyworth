#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  Alfred — instalação em qualquer pasta, SEM sudo. Pode rodar quantas vezes quiser.
#
#    services/scripts/instalar.sh                    → tudo o que dá para fazer sem sudo
#    services/scripts/instalar.sh --modelos          → também baixa no Ollama os modelos do .env (~8 GB)
#    services/scripts/instalar.sh --sem-containers   → não roda `docker compose up`
#    services/scripts/instalar.sh --sem-units        → não mexe no systemd de usuário
#    services/scripts/instalar.sh --verificar        → só confere os pré-requisitos
#    services/scripts/instalar.sh --forcar-units     → regrava units que apontam para OUTRO repositório existente
#
#  Passos:
#    1. pré-requisitos (lista com clareza o que falta e como instalar — Arch e Ubuntu)
#    2. .env: cria a partir do .env.example se NÃO existir (segredos com openssl, caminhos deste repositório);
#       se já existir, não toca — só aponta as chaves obrigatórias vazias
#    3. pastas (vaults/, data/, logs/) — só cria o que falta
#    4. config/searxng/settings.yml se não existir (JSON ligado, limiter desligado, chave própria)
#    5. venvs + requirements de cada serviço
#    6. units de usuário em ~/.config/systemd/user/ com o caminho real (modelos com @ALFRED_DIR@)
#    7. containers do Alfred (sem N8N) e, com --modelos, os modelos do Ollama
#  Nunca sobrescreve .env, vaults/, data/ nem logs/. Segredos nunca são impressos. Sai com 1 se algo ficou faltando.
#  Timer/serviço que já existia DESABILITADO (ex.: briefing vespertino na máquina original) continua desabilitado.
#
#  Para criar o .env com outros modelos: ALFRED_MODEL_CHAT=... ALFRED_MODEL_EMBED=... instalar.sh
# ══════════════════════════════════════════════════════════════
set -uo pipefail

RAIZ="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
ENV_FILE="$RAIZ/.env"
ENV_EXEMPLO="$RAIZ/.env.example"
UNITS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
FLAG_DESLIGADO="$RAIZ/logs/alfred_desligado"
CONTAINERS=(open-webui searxng chromadb evolution-postgres evolution-api)
SERVICOS_PY=(shell-executor researcher inbox briefing)
UNITS_SERVICO=(alfred-inbox-api.service alfred-executor.service alfred-researcher.service)
OBRIGATORIAS=(SHELL_EXECUTOR_TOKEN WEBUI_SECRET_KEY VAULT_ALFRED VAULT_PEDRO OLLAMA_BASE_URL MODEL_CHAT MODEL_EMBED
              EVOLUTION_API_KEY EVOLUTION_POSTGRES_PASSWORD N8N_ENCRYPTION_KEY)

MODELOS=0; CONTAINERS_ON=1; UNITS_ON=1; SO_VERIFICAR=0; FORCAR_UNITS=0
for a in "$@"; do
    case "$a" in
        --modelos) MODELOS=1 ;;
        --sem-containers) CONTAINERS_ON=0 ;;
        --sem-units) UNITS_ON=0 ;;
        --verificar) SO_VERIFICAR=1 ;;
        --forcar-units) FORCAR_UNITS=1 ;;
        -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "Opção desconhecida: $a (veja --help)"; exit 2 ;;
    esac
done

FEITO=(); FALTA=(); AVISOS=()
feito()  { FEITO+=("$*");  echo "  ✔ $*"; }
falta()  { FALTA+=("$*");  echo "  ✖ $*"; }
aviso()  { AVISOS+=("$*"); echo "  ! $*"; }
secao()  { echo; echo "── $* ──"; }
tem()    { command -v "$1" >/dev/null 2>&1; }
LOG_TMP=$(mktemp "${TMPDIR:-/tmp}/alfred-instalar.XXXXXX")   # nome imprevisível; apagado na saída
trap 'rm -f "$LOG_TMP"' EXIT

# valor de uma chave do .env (sem executar o arquivo)
env_get() { [[ -f "$ENV_FILE" ]] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- ; }

# ── Docker: direto, ou via `sg docker` logo depois do usermod (antes de sair e entrar na sessão) ──
DOCKER_MODO=""; NO_GRUPO_DOCKER=0
detectar_docker() {
    tem docker || return 1
    if docker info >/dev/null 2>&1; then DOCKER_MODO=direto; return 0; fi
    if getent group docker | cut -d: -f4 | tr ',' '\n' | grep -qx "$USER"; then
        NO_GRUPO_DOCKER=1   # já está no grupo, mas a sessão atual ainda não (usermod só vale no próximo login)
        if tem sg && sg docker -c "docker info" >/dev/null 2>&1; then DOCKER_MODO=sg; return 0; fi   # Ubuntu tem sg; Arch não
    fi
    return 1
}
dk() {
    if [[ "$DOCKER_MODO" == direto ]]; then docker "$@"; else sg docker -c "$(printf '%q ' docker "$@")"; fi
}

# ══════════════════════════════════════════════════════════════
secao "1. Pré-requisitos"
# ══════════════════════════════════════════════════════════════
echo "Repositório: $RAIZ"

checar() {   # checar <nome> <comando de teste> <pacote(s) Arch> <comando Ubuntu> [opcional]
    local nome="$1" teste="$2" arch="$3" ubuntu="$4" opcional="${5:-}"
    if eval "$teste" >/dev/null 2>&1; then
        echo "  ✔ $nome"
    elif [[ -n "$opcional" ]]; then
        aviso "$nome ausente ($opcional) — Arch: sudo pacman -Syu --needed $arch · Ubuntu: $ubuntu"
    else
        falta "$nome — Arch: sudo pacman -Syu --needed $arch · Ubuntu: $ubuntu"
    fi
}
checar "python3 ≥ 3.12 com venv" 'python3 -c "import sys, venv, ensurepip; assert sys.version_info >= (3, 12)"' "python" "sudo apt install python3 python3-venv"
checar "openssl" "tem openssl" "openssl" "sudo apt install openssl"
checar "curl" "tem curl" "curl" "sudo apt install curl"
checar "docker" "tem docker" "docker" "sudo apt install docker.io"
checar "docker compose (plugin)" "docker compose version" "docker-compose" "sudo apt install docker-compose-v2"
checar "ollama" "tem ollama" "ollama ollama-cuda" "curl -fsSL https://ollama.com/install.sh | sh"
checar "ffmpeg" "tem ffmpeg" "ffmpeg" "sudo apt install ffmpeg"
checar "notify-send" "tem notify-send" "libnotify" "sudo apt install libnotify-bin"
checar "kdialog ou zenity" "tem kdialog || tem zenity" "kdialog" "sudo apt install kdialog" "caixas de texto da notificação no PC"
checar "PyGObject no Python do sistema" '/usr/bin/python3 -c "import gi"' "python-gobject" "sudo apt install python3-gi" "botões na notificação (notificar_dbus.py)"
checar "thunderbird" "tem thunderbird || flatpak info org.mozilla.Thunderbird || snap list thunderbird" "thunderbird" "sudo apt install thunderbird" "e-mail do jornal e das pendências"
checar "systemd de usuário" "systemctl --user show-environment" "systemd" "(systemd já vem no Ubuntu)"

if detectar_docker; then
    echo "  ✔ acesso ao Docker ($([[ $DOCKER_MODO == sg ]] && echo 'via sg docker — vale sem sg a partir do próximo login' || echo direto))"
elif tem docker; then
    if [[ $NO_GRUPO_DOCKER == 1 ]]; then
        falta "sem acesso ao Docker nesta sessão: seu usuário já está no grupo docker, mas o grupo só vale depois de sair e entrar de novo (ou reiniciar) — depois rode o instalar.sh outra vez"
    else
        falta "sem acesso ao Docker — Docker parado ou usuário fora do grupo: sudo systemctl enable --now docker && sudo usermod -aG docker $USER (e saia/entre na sessão)"
    fi
fi

OLLAMA_LOCAL="http://127.0.0.1:11434"
if tem ollama; then
    if curl -s --max-time 3 "$OLLAMA_LOCAL/api/version" >/dev/null; then
        echo "  ✔ Ollama respondendo em $OLLAMA_LOCAL"
        if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -qE '^127\.0\.0\.1:11434$'; then
            aviso "Ollama escuta só em 127.0.0.1 — os containers não o alcançam. Drop-in: Environment=OLLAMA_HOST=0.0.0.0:11434 (README → Setup)"
        fi
    else
        falta "Ollama instalado mas não responde — sudo systemctl enable --now ollama"
    fi
fi

# Rede (README → Setup → Rede): inbox/executor/researcher/Ollama escutam em 0.0.0.0; quem barra a rede local é o ufw
EXPOSTAS="Ollama (11434), API do inbox (7072), executor (7070) e researcher (7071) escutam em 0.0.0.0"
if grep -qs "^ENABLED=yes" /etc/ufw/ufw.conf && systemctl is-active --quiet ufw 2>/dev/null; then
    if ! grep -qs '^DEFAULT_INPUT_POLICY="\(DROP\|REJECT\)"' /etc/default/ufw; then
        aviso "ufw ativo mas sem política de entrada DROP: $EXPOSTAS e ficam abertos para a rede local — sudo ufw default deny incoming"
    elif [[ ! -r /etc/ufw/user.rules ]]; then
        aviso "não consegui ler as regras do ufw (sem permissão) — confira com 'sudo ufw status' se br-alfred está liberado para 7070:7072 e 11434"
    elif grep -q "br-alfred" /etc/ufw/user.rules; then
        echo "  ✔ ufw ativo (entrada DROP) e libera br-alfred (containers → APIs do host e Ollama)"
    else
        aviso "ufw ativo sem regra para br-alfred: os containers não alcançam o Ollama nem a API do inbox (README → Setup → Rede)"
    fi
else
    aviso "firewall (ufw) inativo ou ausente: $EXPOSTAS e ficam abertos para a rede local (README → Setup → Rede)"
fi

# Portas: sem root o ss não mostra o dono de socket de outro usuário (o docker-proxy dos containers é do root) —
# por isso a porta publicada por um container alfred-* conta como do Alfred.
PUBLICADAS=""
[[ -n "$DOCKER_MODO" ]] && PUBLICADAS=$(dk ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep '^alfred-')
for v in PORT_WEBUI:3000 PORT_SEARXNG:8888 PORT_CHROMADB:8000 PORT_SHELL_EXECUTOR:7070 PORT_RESEARCHER:7071 PORT_INBOX:7072 PORT_EVOLUTION:8080; do
    chave=${v%%:*}; porta=$(env_get "$chave"); porta=${porta:-${v##*:}}
    dono=$(ss -tlnpH "sport = :$porta" 2>/dev/null | head -1)
    [[ -z "$dono" ]] && continue
    if [[ "$dono" == *"users:(("* ]]; then
        grep -qE 'uvicorn|python|docker' <<<"$dono" \
            || aviso "porta $porta ($chave) já está em uso por outro programa ($(grep -oE 'users:\(\("[^"]+' <<<"$dono" | cut -d'"' -f2)) — troque $chave no .env"
    elif ! grep -qE ":$porta->" <<<"$PUBLICADAS"; then
        aviso "porta $porta ($chave) ocupada por um processo de outro usuário (sem root não dá para ver qual; sudo ss -tlnp mostra) — se não for do Alfred, troque $chave no .env"
    fi
done

if [[ $SO_VERIFICAR == 1 ]]; then
    echo; echo "Faltando: ${#FALTA[@]} · avisos: ${#AVISOS[@]}"
    exit $(( ${#FALTA[@]} > 0 ))
fi

# ══════════════════════════════════════════════════════════════
secao "2. .env"
# ══════════════════════════════════════════════════════════════
if [[ -f "$ENV_FILE" ]]; then
    echo "  .env já existe — não foi alterado."
    vazias=()
    for k in "${OBRIGATORIAS[@]}"; do [[ -z "$(env_get "$k")" ]] && vazias+=("$k"); done
    if [[ ${#vazias[@]} -gt 0 ]]; then
        falta "chaves obrigatórias vazias no .env: ${vazias[*]}"
    else
        echo "  ✔ chaves obrigatórias preenchidas"
    fi
elif [[ ! -f "$ENV_EXEMPLO" ]]; then
    falta ".env.example não encontrado em $RAIZ"
elif ! tem openssl; then
    falta ".env NÃO criado: falta o openssl para gerar os segredos (instale e rode de novo)"
else
    host_ip=172.17.0.1
    [[ -z "$DOCKER_MODO" ]] && detectar_docker
    if [[ -n "$DOCKER_MODO" ]]; then
        gw=$(dk network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null)
        [[ "$gw" =~ ^[0-9.]+$ ]] && host_ip=$gw
    fi
    chat=${ALFRED_MODEL_CHAT:-gemma4:e4b}
    # Os segredos vão por variável de ambiente para o Python — nunca aparecem no terminal.
    ( umask 077
      A_WEBUI=$(openssl rand -hex 32) A_N8N=$(openssl rand -hex 32) A_N8N_PASS=$(openssl rand -hex 12) \
      A_TOKEN=$(openssl rand -hex 32) A_EVO=$(openssl rand -hex 16) A_PG=$(openssl rand -hex 12) \
      A_RAIZ="$RAIZ" A_HOST="$host_ip" A_CHAT="$chat" A_TRANS="${ALFRED_MODEL_TRANSCRIBE:-$chat}" \
      A_EMBED="${ALFRED_MODEL_EMBED:-bge-m3}" A_THINK="${ALFRED_OLLAMA_THINK:-false}" A_USER="$USER" \
      python3 - "$ENV_EXEMPLO" "$ENV_FILE" <<'PY'
import os, re, sys
exemplo, destino = sys.argv[1], sys.argv[2]
e = os.environ
raiz = e["A_RAIZ"]
valores = {
    "HOST_IP": e["A_HOST"], "OLLAMA_BASE_URL": f"http://{e['A_HOST']}:11434",
    "MODEL_CHAT": e["A_CHAT"], "MODEL_TRANSCRIBE": e["A_TRANS"], "MODEL_EMBED": e["A_EMBED"],
    "OLLAMA_THINK": e["A_THINK"],
    "VAULT_ALFRED": f"{raiz}/vaults/alfred", "VAULT_PEDRO": f"{raiz}/vaults/pedro",
    "VAULT_NOTICIAS": f"{raiz}/vaults/alfred/Noticias",
    "WEBUI_SECRET_KEY": e["A_WEBUI"], "N8N_ENCRYPTION_KEY": e["A_N8N"], "N8N_BASIC_AUTH_USER": e["A_USER"],
    "N8N_BASIC_AUTH_PASSWORD": e["A_N8N_PASS"], "SHELL_EXECUTOR_TOKEN": e["A_TOKEN"],
    "EVOLUTION_API_KEY": e["A_EVO"], "EVOLUTION_POSTGRES_PASSWORD": e["A_PG"],
    "EVOLUTION_URL": "http://localhost:8080", "WHATSAPP_DRY_RUN": "true", "DOCKER_BIND_IP": "127.0.0.1",
}
vazios = [k for k in ("WEBUI_SECRET_KEY", "N8N_ENCRYPTION_KEY", "N8N_BASIC_AUTH_PASSWORD", "SHELL_EXECUTOR_TOKEN",
                      "EVOLUTION_API_KEY", "EVOLUTION_POSTGRES_PASSWORD") if not valores[k]]
if vazios:
    sys.exit(f"segredos vazios ({', '.join(vazios)}) — o openssl falhou; .env não criado")
linhas, vistos = [], set()
for linha in open(exemplo, encoding="utf-8").read().splitlines():
    m = re.match(r"^([A-Z_][A-Z0-9_]*)=", linha)
    if m and m.group(1) in valores:
        linha = f"{m.group(1)}={valores[m.group(1)]}"
        vistos.add(m.group(1))
    linhas.append(linha)
faltando = [k for k in valores if k not in vistos]
if faltando:
    linhas += ["", "# ── Acrescentado pelo instalar.sh ──"] + [f"{k}={valores[k]}" for k in faltando]
with open(destino, "x", encoding="utf-8") as f:     # "x": nunca sobrescreve um .env existente
    f.write("\n".join(linhas) + "\n")
PY
    ) && { chmod 600 "$ENV_FILE"; feito ".env criado (segredos gerados com openssl, modo 600; WHATSAPP_DRY_RUN=true; modelos $chat + ${ALFRED_MODEL_EMBED:-bge-m3})"; } \
      || falta "não consegui criar o .env"
fi

# ══════════════════════════════════════════════════════════════
secao "3. Pastas"
# ══════════════════════════════════════════════════════════════
criar_pasta() { if [[ -n "$1" && ! -d "$1" ]]; then mkdir -p "$1" && feito "pasta criada: ${1/#$RAIZ\//}"; fi; }
VA=$(env_get VAULT_ALFRED); VP=$(env_get VAULT_PEDRO); VN=$(env_get VAULT_NOTICIAS)
# Vault FORA deste repositório que não existe: provavelmente o repositório foi movido (o .env aponta para o
# caminho antigo) — não cria um vault vazio lá; quem decide é o usuário.
for k in VAULT_ALFRED VAULT_PEDRO VAULT_NOTICIAS; do
    v=$(env_get "$k")
    if [[ -n "$v" && "$v" != "$RAIZ/"* && ! -d "$v" ]]; then
        falta "$k aponta para '$v', que não existe — o repositório foi movido? Ajuste o .env (nada foi criado lá)"
        case "$k" in VAULT_ALFRED) VA="" ;; VAULT_PEDRO) VP="" ;; VAULT_NOTICIAS) VN="" ;; esac
    fi
done
for d in "$VA" "$VP" "$VN" "${VA:+$VA/research}" "${VA:+$VA/decisions}" "${VA:+$VA/logs}" \
         "$RAIZ/logs" "$RAIZ/data/open-webui" "$RAIZ/data/chromadb" "$RAIZ/data/evolution/instances" \
         "$RAIZ/data/evolution/postgres"; do
    criar_pasta "$d"
done
echo "  ✔ pastas conferidas (nada existente foi alterado)"

# ══════════════════════════════════════════════════════════════
secao "4. SearXNG"
# ══════════════════════════════════════════════════════════════
SX="$RAIZ/config/searxng/settings.yml"
if [[ -f "$SX" ]]; then
    echo "  settings.yml já existe — não foi alterado."
elif ! tem openssl; then
    falta "config/searxng/settings.yml NÃO criado: falta o openssl para a chave secreta"
else
    mkdir -p "$(dirname "$SX")"
    # 644: o processo do SearXNG no container (outro uid) precisa ler; a pasta pessoal (700) protege no host
    ( umask 022; cat > "$SX" <<EOF
# Gerado por services/scripts/instalar.sh — configuração mínima do SearXNG do Alfred (não versionado).
use_default_settings: true
server:
  secret_key: "$(openssl rand -hex 32)"
  limiter: false        # o limiter anti-bot bloqueia o Open WebUI e o Researcher (README → Troubleshooting)
  image_proxy: false
search:
  formats:
    - html
    - json              # os serviços consultam /search?q=...&format=json
EOF
    ) && feito "config/searxng/settings.yml criado (JSON ligado, limiter desligado, chave própria)"
fi

# ══════════════════════════════════════════════════════════════
secao "5. venvs"
# ══════════════════════════════════════════════════════════════
for s in "${SERVICOS_PY[@]}"; do
    d="$RAIZ/services/$s"
    [[ -f "$d/requirements.txt" ]] || continue
    # venv quebrado (sem pip — ex.: criado sem python3-venv no Ubuntu) ou de outra pasta (repositório movido:
    # os scripts de venv/bin têm o caminho antigo no shebang) → recria do zero
    shebang=$(head -1 "$d/venv/bin/pip" 2>/dev/null)
    if [[ -e "$d/venv" ]] && { [[ ! -x "$d/venv/bin/python" ]] || [[ "$shebang" != "#!$d/venv/bin/"* ]]; }; then
        python3 -m venv --clear "$d/venv" >"$LOG_TMP" 2>&1 \
            && aviso "venv de services/$s estava quebrado ou era de outra pasta — recriado" \
            || { falta "venv de services/$s: $(tail -1 "$LOG_TMP")"; continue; }
    elif [[ ! -e "$d/venv" ]]; then
        python3 -m venv "$d/venv" >"$LOG_TMP" 2>&1 && feito "venv criado: services/$s" \
            || { falta "venv de services/$s: $(tail -1 "$LOG_TMP")"; continue; }
    fi
    if "$d/venv/bin/python" -m pip install -q --disable-pip-version-check -r "$d/requirements.txt" >"$LOG_TMP" 2>&1; then
        echo "  ✔ services/$s: requirements instalados"
    else
        falta "pip install em services/$s falhou: $(tail -1 "$LOG_TMP")"
    fi
done

# ══════════════════════════════════════════════════════════════
secao "6. Units de usuário (systemd)"
# ══════════════════════════════════════════════════════════════
unit_de_sistema() {   # a máquina original tem executor/researcher como units de SISTEMA
    local f
    for f in /etc/systemd/system/"$1" /usr/lib/systemd/system/"$1" /lib/systemd/system/"$1"; do
        [[ -e "$f" ]] && return 0
    done
    return 1
}
# WorkingDirectory de uma unit já instalada, com %h expandido, resolvido (para comparar com este repositório)
dir_da_unit() {
    local w; w=$(grep -m1 '^WorkingDirectory=' "$1" 2>/dev/null | cut -d= -f2-)
    w=${w//%h/$HOME}; w=${w//%%/%}
    [[ -n "$w" ]] && readlink -f "$w" 2>/dev/null
}
INSTALADAS=()
if [[ $UNITS_ON == 0 ]]; then
    echo "  (--sem-units)"
elif ! systemctl --user show-environment >/dev/null 2>&1; then
    falta "systemd de usuário indisponível nesta sessão — rode o instalar.sh de dentro da sua sessão gráfica"
else
    mkdir -p "$UNITS_DIR"
    esc=${RAIZ//%/%%}            # % é especificador no systemd
    mudou=0; MUDARAM=(); JA_EXISTIAM=()
    while IFS= read -r modelo; do
        nome=$(basename "$modelo")
        if [[ "$nome" == alfred-executor.service || "$nome" == alfred-researcher.service ]] && unit_de_sistema "$nome"; then
            aviso "$nome já existe como unit de SISTEMA nesta máquina — a de usuário não foi instalada (evita duas na mesma porta)"
            continue
        fi
        # aspas no "$esc": no bash ≥ 5.2 (patsub_replacement) um '&' no caminho viraria o texto casado
        conteudo=$(<"$modelo"); conteudo=${conteudo//@ALFRED_DIR@/"$esc"}
        destino="$UNITS_DIR/$nome"
        if [[ -f "$destino" ]]; then
            JA_EXISTIAM+=("$nome")
            antigo=$(dir_da_unit "$destino"); novo=$(readlink -f "$(dirname "$modelo")")
            if [[ -n "$antigo" && "$antigo" != "$novo" && -d "$antigo" && $FORCAR_UNITS == 0 ]]; then
                aviso "$nome aponta para OUTRO repositório ($antigo) — mantida; para trocar por este: instalar.sh --forcar-units"
                INSTALADAS+=("$nome"); continue
            fi
        fi
        if [[ ! -f "$destino" ]] || [[ "$(<"$destino")" != "$conteudo" ]]; then
            printf '%s\n' "$conteudo" > "$destino"; mudou=1; MUDARAM+=("$nome")
        fi
        INSTALADAS+=("$nome")
    done < <(find "$RAIZ/services" -maxdepth 2 \( -name 'alfred-*.service' -o -name 'alfred-*.timer' \) | sort)
    [[ $mudou == 1 ]] && feito "units gravadas em $UNITS_DIR: ${MUDARAM[*]}"
    systemctl --user daemon-reload
    ativar=(); candidatas=()
    for u in "${UNITS_SERVICO[@]}"; do [[ " ${INSTALADAS[*]} " == *" $u "* ]] && candidatas+=("$u"); done
    for u in "${INSTALADAS[@]}"; do [[ "$u" == *.timer ]] && candidatas+=("$u"); done
    for u in "${candidatas[@]}"; do
        # já existia e foi DESABILITADA por alguém (ex.: briefing vespertino na máquina original) → respeita
        if [[ " ${JA_EXISTIAM[*]} " == *" $u "* ]] && [[ "$(systemctl --user is-enabled "$u" 2>/dev/null)" == disabled ]]; then
            aviso "$u estava desabilitada — mantida assim (para ligar: systemctl --user enable --now $u)"
        else
            ativar+=("$u")
        fi
    done
    if [[ -f "$FLAG_DESLIGADO" ]]; then
        systemctl --user enable "${ativar[@]}" >/dev/null 2>&1
        aviso "Alfred em modo jogo (logs/alfred_desligado): units habilitadas mas não iniciadas — religue com services/scripts/alfred-switch.sh on"
    else
        systemctl --user enable --now "${ativar[@]}" >/dev/null 2>&1 \
            && echo "  ✔ habilitadas e no ar: ${ativar[*]}" \
            || falta "systemctl --user enable --now falhou para: ${ativar[*]}"
        # unit alterada de um serviço que já rodava: reinicia para valer o caminho novo
        for u in "${MUDARAM[@]}"; do
            [[ "$u" == *.service ]] && [[ " ${UNITS_SERVICO[*]} " == *" $u "* ]] && systemctl --user try-restart "$u"
        done
    fi
fi

# ══════════════════════════════════════════════════════════════
secao "7. Containers e modelos"
# ══════════════════════════════════════════════════════════════
if [[ $CONTAINERS_ON == 0 ]]; then
    echo "  (--sem-containers)"
elif [[ -z "$DOCKER_MODO" ]] && ! detectar_docker; then
    falta "containers não subiram: sem acesso ao Docker (ver pré-requisitos)"
elif [[ ! -f "$ENV_FILE" ]]; then
    falta "containers não subiram: falta o .env"
else
    subir=("${CONTAINERS[@]}")
    if [[ -f "$FLAG_DESLIGADO" ]]; then
        # modo jogo: a Evolution foi parada pelo alfred-switch.sh off e só volta com o "on"
        subir=(open-webui searxng chromadb)
        aviso "modo jogo: Evolution e o banco dela não foram iniciados (alfred-switch.sh on religa)"
    fi
    if dk compose --project-directory "$RAIZ" -f "$RAIZ/docker-compose.yml" up -d "${subir[@]}" >"$LOG_TMP" 2>&1; then
        feito "containers no ar: ${subir[*]} (N8N fica de fora)"
    else
        falta "docker compose up falhou: $(grep -iE 'error|erro' "$LOG_TMP" | tail -2 | tr '\n' ' ')"
        grep -q "br-alfred\|needs to be recreated\|incorrect label" "$LOG_TMP" \
            && aviso "rede antiga sem o nome br-alfred: rode 'docker compose down' e o instalar.sh de novo"
    fi
fi

if [[ $MODELOS == 1 ]]; then
    if curl -s --max-time 3 "$OLLAMA_LOCAL/api/version" >/dev/null; then
        for m in $(printf '%s\n' "$(env_get MODEL_CHAT)" "$(env_get MODEL_TRANSCRIBE)" "$(env_get MODEL_EMBED)" | grep . | sort -u); do
            if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qxE "$m(:latest)?"; then
                echo "  ✔ modelo $m já baixado"
            else
                echo "  baixando $m ..."; ollama pull "$m" && feito "modelo baixado: $m" || falta "ollama pull $m falhou"
            fi
        done
    else
        falta "--modelos: Ollama não responde em $OLLAMA_LOCAL"
    fi
fi

# ══════════════════════════════════════════════════════════════
secao "Resumo"
# ══════════════════════════════════════════════════════════════
echo "Feito nesta execução: ${#FEITO[@]}"; for x in "${FEITO[@]}"; do echo "  • $x"; done
[[ ${#AVISOS[@]} -gt 0 ]] && { echo "Avisos:"; for x in "${AVISOS[@]}"; do echo "  • $x"; done; }
if [[ ${#FALTA[@]} -gt 0 ]]; then
    echo "Ainda falta:"; for x in "${FALTA[@]}"; do echo "  • $x"; done
else
    echo "Nada faltando."
fi
CLIP="wl-copy"; tem wl-copy || { tem xsel && CLIP="xsel -ib"; } || { tem xclip && CLIP="xclip -selection clipboard"; }
cat <<EOF

Próximos passos:
  • Painel:   http://127.0.0.1:$(env_get PORT_INBOX | grep . || echo 7072)/painel — token (vai para a área de transferência, sem aparecer):
              grep -E '^SHELL_EXECUTOR_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '\\n' | $CLIP
  • Modelos:  $0 --modelos
  • Estado:   systemctl --user list-timers 'alfred-*' · $RAIZ/services/scripts/alfred-switch.sh status
  • Tool "Alfred Tech Briefing" no Open WebUI — Valves: alfred_dir_host = $RAIZ · vault_alfred_host = $(env_get VAULT_ALFRED)
EOF
# atalho "liga/desliga" já criado (alfred-switch.sh): reaponta para esta pasta
if [[ -f "$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")/alfred-switch.desktop" || -f "$HOME/.local/share/applications/alfred-switch.desktop" ]]; then
    "$RAIZ/services/scripts/alfred-switch.sh" atalho >/dev/null 2>&1
fi
exit $(( ${#FALTA[@]} > 0 ))
