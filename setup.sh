#!/usr/bin/env bash
# =============================================================
#  PROJETO ALFRED PENNYWORTH — setup interativo
#  Gera o .env com segredos reais e caminhos do ambiente local
# =============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

# ── Cores ─────────────────────────────────────────────────────
BOLD='\033[1m'
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'

header() { echo -e "\n${CYAN}${BOLD}$*${RESET}"; }
ok()     { echo -e "${GREEN}✔${RESET}  $*"; }
warn()   { echo -e "${YELLOW}⚠${RESET}  $*"; }
ask()    { echo -en "${BOLD}$*${RESET}"; }

# ── Verificar dependências ─────────────────────────────────────
for cmd in openssl docker python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo -e "${RED}Erro:${RESET} '$cmd' não encontrado. Instale antes de continuar."
        exit 1
    fi
done

# ── Banner ────────────────────────────────────────────────────
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║       ALFRED PENNYWORTH — setup          ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${RESET}"

# ── Verificar .env existente ──────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
    warn ".env já existe em $ENV_FILE"
    ask "Deseja recriar do zero? Isso sobrescreverá o arquivo atual. [s/N] "
    read -r resposta
    if [[ ! "$resposta" =~ ^[sS]$ ]]; then
        echo "Operação cancelada. Nenhuma alteração feita."
        exit 0
    fi
    cp "$ENV_FILE" "${ENV_FILE}.bak"
    ok "Backup salvo em .env.bak"
fi

# ── Vault do Pedro ────────────────────────────────────────────
header "📁  Vault Obsidian"

DEFAULT_VAULT=""
# Tentar encontrar automaticamente
for candidate in \
    "$HOME/Obsidian"* \
    "$HOME/obsidian"* \
    "$HOME/Documents/Obsidian"* \
    "$HOME/Documentos/Obsidian"* \
    "/mnt/"*"/Obsidian"* \
    "/mnt/SSD/Obsidian"*; do
    if [[ -d "$candidate" ]]; then
        DEFAULT_VAULT="$candidate"
        break
    fi
done

if [[ -n "$DEFAULT_VAULT" ]]; then
    echo "  Vault encontrado: ${BOLD}$DEFAULT_VAULT${RESET}"
    ask "  Usar este caminho? [S/n] "
    read -r resp_vault
    if [[ "$resp_vault" =~ ^[nN]$ ]]; then
        DEFAULT_VAULT=""
    fi
fi

if [[ -z "$DEFAULT_VAULT" ]]; then
    ask "  Informe o caminho completo do seu vault Obsidian: "
    read -r DEFAULT_VAULT
fi

DEFAULT_VAULT="${DEFAULT_VAULT%/}"   # remove barra final

if [[ ! -d "$DEFAULT_VAULT" ]]; then
    warn "Caminho não encontrado: $DEFAULT_VAULT"
    warn "O .env será criado assim mesmo — corrija VAULT_PEDRO depois se necessário."
fi

ok "VAULT_PEDRO=$DEFAULT_VAULT"

# ── Vault Alfred (no repositório) ────────────────────────────
VAULT_ALFRED="$SCRIPT_DIR/vaults/alfred"
mkdir -p "$VAULT_ALFRED"/{research,decisions,logs}
ok "VAULT_ALFRED=$VAULT_ALFRED"

# ── Credenciais N8N ───────────────────────────────────────────
header "🔑  Credenciais N8N"

DEFAULT_N8N_USER="${USER:-pedro}"
ask "  Usuário N8N [${DEFAULT_N8N_USER}]: "
read -r N8N_USER
N8N_USER="${N8N_USER:-$DEFAULT_N8N_USER}"

ask "  Senha N8N (Enter para gerar automaticamente): "
read -rs N8N_PASS
echo
if [[ -z "$N8N_PASS" ]]; then
    N8N_PASS="$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)"
    ok "Senha gerada automaticamente (salva no .env)"
else
    ok "Senha definida pelo usuário"
fi

# ── Gerar segredos ────────────────────────────────────────────
header "🔐  Gerando segredos criptográficos"

WEBUI_SECRET_KEY="$(openssl rand -hex 32)"
N8N_ENCRYPTION_KEY="$(openssl rand -hex 32)"
SHELL_EXECUTOR_TOKEN="$(openssl rand -hex 32)"

ok "WEBUI_SECRET_KEY  gerada"
ok "N8N_ENCRYPTION_KEY  gerada"
ok "SHELL_EXECUTOR_TOKEN  gerado"

# ── Detectar HOST_IP (bridge docker) ─────────────────────────
HOST_IP="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || echo '172.17.0.1')"
ok "HOST_IP=$HOST_IP"

# ── Escrever .env ─────────────────────────────────────────────
header "📝  Criando .env"

cat > "$ENV_FILE" << EOF
# =============================================================
#  PROJETO ALFRED PENNYWORTH — configuração central
#  Gerado por setup.sh em $(date '+%Y-%m-%d %H:%M:%S')
# =============================================================

# ── Rede / Host ───────────────────────────────────────────────
HOST_IP=${HOST_IP}
OLLAMA_BASE_URL=http://${HOST_IP}:11434

# ── Portas dos serviços ───────────────────────────────────────
PORT_WEBUI=3000
PORT_SEARXNG=8888
PORT_N8N=5678
PORT_CHROMADB=8000
PORT_SHELL_EXECUTOR=7070

# ── Modelos Ollama ────────────────────────────────────────────
MODEL_CHAT=qwen3:8b
MODEL_EMBED=nomic-embed-text

# ── Vaults Obsidian ───────────────────────────────────────────
VAULT_PEDRO=${DEFAULT_VAULT}
VAULT_ALFRED=${VAULT_ALFRED}

# ── Open WebUI ────────────────────────────────────────────────
WEBUI_SECRET_KEY=${WEBUI_SECRET_KEY}
WEBUI_AUTH=false
# WEBUI_API_TOKEN: gere após subir o WebUI com o comando abaixo:
#   docker exec alfred-webui python3 -c "
#   import sqlite3, secrets, time, uuid
#   db = sqlite3.connect('/app/backend/data/webui.db')
#   token = 'sk-alfred-' + secrets.token_hex(24)
#   now = int(time.time())
#   uid = db.execute(\"SELECT id FROM user LIMIT 1\").fetchone()[0]
#   db.execute('INSERT INTO api_key (id, user_id, key, data, created_at, updated_at) VALUES (?,?,?,?,?,?)',
#              (str(uuid.uuid4()), uid, token, '{}', now, now))
#   db.commit(); print('Token:', token); db.close()"
# Em seguida, cole aqui:
WEBUI_API_TOKEN=

# ── N8N ───────────────────────────────────────────────────────
N8N_BASIC_AUTH_USER=${N8N_USER}
N8N_BASIC_AUTH_PASSWORD=${N8N_PASS}
N8N_ENCRYPTION_KEY=${N8N_ENCRYPTION_KEY}

# ── Shell Executor ────────────────────────────────────────────
SHELL_EXECUTOR_TOKEN=${SHELL_EXECUTOR_TOKEN}
EOF

chmod 600 "$ENV_FILE"
ok ".env criado com permissões 600"

LOG_FILE="$SCRIPT_DIR/setup.log"
: > "$LOG_FILE"   # zerar/criar o log

# ─────────────────────────────────────────────────────────────
# ETAPA 2 — Shell Executor
# ─────────────────────────────────────────────────────────────
EXECUTOR_DIR="$SCRIPT_DIR/services/shell-executor"
EXECUTOR_INSTALLED=false

echo ""
ask "Deseja instalar o Shell Executor agora? [s/N] "
read -r resp_exec

if [[ "$resp_exec" =~ ^[sS]$ ]]; then
    header "⚙️   Instalando Shell Executor"
    echo "  Log completo em: ${BOLD}setup.log${RESET}"
    echo ""

    (
        echo "=== Shell Executor install — $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
        cd "$EXECUTOR_DIR"
        python3 -m venv venv                                   2>&1 >> "$LOG_FILE"
        ./venv/bin/pip install --quiet fastapi uvicorn          2>&1 >> "$LOG_FILE"
        echo "=== FIM install — $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
    ) &
    EXEC_PID=$!

    FRAMES=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    i=0
    while kill -0 "$EXEC_PID" 2>/dev/null; do
        printf "\r  ${CYAN}${FRAMES[$((i % 10))]}${RESET}  Instalando dependências Python..."
        sleep 0.1
        i=$((i + 1))
    done
    printf "\r%-60s\r" " "

    if wait "$EXEC_PID"; then
        ok "Shell Executor instalado"
        EXECUTOR_INSTALLED=true
    else
        echo -e "  ${RED}✖${RESET}  Falha na instalação — veja setup.log"
    fi
fi

# ─────────────────────────────────────────────────────────────
# ETAPA 3 — Serviço systemd
# ─────────────────────────────────────────────────────────────
SYSTEMD_ENABLED=false
SERVICE_FILE="/etc/systemd/system/alfred-executor.service"

echo ""
ask "Deseja habilitar o serviço systemd alfred-executor agora? (requer sudo) [s/N] "
read -r resp_svc

if [[ "$resp_svc" =~ ^[sS]$ ]]; then
    header "🔧  Configurando serviço systemd"

    # Criar o arquivo de serviço
    SERVICE_CONTENT="[Unit]
Description=Alfred Shell Executor
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${EXECUTOR_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${EXECUTOR_DIR}/venv/bin/uvicorn main:app --host 0.0.0.0 --port 7070
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target"

    echo "=== systemd service — $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
    echo "$SERVICE_CONTENT" >> "$LOG_FILE"

    if echo "$SERVICE_CONTENT" | sudo tee "$SERVICE_FILE" > /dev/null 2>> "$LOG_FILE" \
        && sudo systemctl daemon-reload >> "$LOG_FILE" 2>&1 \
        && sudo systemctl enable alfred-executor >> "$LOG_FILE" 2>&1 \
        && sudo systemctl restart alfred-executor >> "$LOG_FILE" 2>&1; then
        ok "Serviço alfred-executor habilitado e iniciado"
        SYSTEMD_ENABLED=true
    else
        echo -e "  ${RED}✖${RESET}  Falha ao configurar serviço — veja setup.log"
    fi
fi

# ─────────────────────────────────────────────────────────────
# ETAPA 4 — Docker
# ─────────────────────────────────────────────────────────────
DOCKER_RAN=false

echo ""
ask "Deseja subir a stack Docker agora? [s/N] "
read -r resp_docker

if [[ "$resp_docker" =~ ^[sS]$ ]]; then
    header "🐳  Subindo a stack Docker"
    echo "  Log completo em: ${BOLD}setup.log${RESET}"
    echo ""

    (
        echo "=== docker compose up -d — $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
        docker compose -f "$SCRIPT_DIR/docker-compose.yml" --env-file "$ENV_FILE" up -d 2>&1 >> "$LOG_FILE"
        echo "=== FIM docker — $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
    ) &
    DOCKER_PID=$!

    FRAMES=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    i=0
    while kill -0 "$DOCKER_PID" 2>/dev/null; do
        printf "\r  ${CYAN}${FRAMES[$((i % 10))]}${RESET}  Aguardando containers..."
        sleep 0.1
        i=$((i + 1))
    done
    printf "\r%-60s\r" " "

    wait "$DOCKER_PID"
    DOCKER_EXIT=$?
    DOCKER_RAN=true

    # Registrar ps no log
    {
        echo ""
        echo "=== docker compose ps — $(date '+%Y-%m-%d %H:%M:%S') ==="
        docker compose -f "$SCRIPT_DIR/docker-compose.yml" --env-file "$ENV_FILE" ps 2>&1
    } >> "$LOG_FILE"

    # Aguardar containers estabilizarem
    sleep 2

    declare -A SERVICES=(
        ["alfred-webui"]="Open WebUI  →  http://localhost:3000"
        ["alfred-searxng"]="SearXNG     →  http://localhost:8888"
        ["alfred-chromadb"]="ChromaDB    →  http://localhost:8000"
        ["alfred-n8n"]="N8N         →  http://localhost:5678   (usuário: ${N8N_USER})"
    )

    ERRORS=()
    RUNNING=()

    for container in "${!SERVICES[@]}"; do
        status="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || echo 'não encontrado')"
        if [[ "$status" == "running" ]]; then
            RUNNING+=("$container")
        else
            ERRORS+=("$container (status: $status)")
        fi
    done

    echo ""
    if [[ ${#ERRORS[@]} -eq 0 && $DOCKER_EXIT -eq 0 ]]; then
        echo -e "${GREEN}${BOLD}  ✔ Todos os containers estão rodando!${RESET}\n"
        echo -e "  ${BOLD}Acesse os serviços:${RESET}"
        echo -e "  ┌──────────────────────────────────────────────────────────────┐"
        echo -e "  │  ${GREEN}●${RESET}  ${SERVICES["alfred-webui"]}                              │"
        echo -e "  │  ${GREEN}●${RESET}  ${SERVICES["alfred-searxng"]}                              │"
        echo -e "  │  ${GREEN}●${RESET}  ${SERVICES["alfred-chromadb"]}                              │"
        echo -e "  │  ${GREEN}●${RESET}  ${SERVICES["alfred-n8n"]}  │"
        echo -e "  └──────────────────────────────────────────────────────────────┘"
    else
        [[ $DOCKER_EXIT -ne 0 ]] && \
            echo -e "${RED}${BOLD}  ✖ docker compose retornou erro (código $DOCKER_EXIT)${RESET}"
        if [[ ${#ERRORS[@]} -gt 0 ]]; then
            echo -e "${RED}${BOLD}  ✖ Containers com problema:${RESET}"
            for err in "${ERRORS[@]}"; do
                echo -e "    ${RED}•${RESET} $err"
            done
        fi
        if [[ ${#RUNNING[@]} -gt 0 ]]; then
            echo -e "\n${YELLOW}  Containers que subiram normalmente:${RESET}"
            for svc in "${RUNNING[@]}"; do
                echo -e "    ${GREEN}●${RESET}  ${SERVICES[$svc]}"
            done
        fi
        echo -e "\n  Consulte o log completo: ${BOLD}setup.log${RESET}"
    fi
fi

# ─────────────────────────────────────────────────────────────
# Resumo final
# ─────────────────────────────────────────────────────────────
echo -e "\n${GREEN}${BOLD}Setup concluído!${RESET}\n"
echo -e "  ${BOLD}Resumo:${RESET}"
[[ "$EXECUTOR_INSTALLED" == true ]] \
    && echo -e "  ${GREEN}✔${RESET}  Shell Executor instalado" \
    || echo -e "  ${YELLOW}–${RESET}  Shell Executor não instalado"
[[ "$SYSTEMD_ENABLED" == true ]] \
    && echo -e "  ${GREEN}✔${RESET}  Serviço alfred-executor ativo (systemd)" \
    || echo -e "  ${YELLOW}–${RESET}  Serviço systemd não configurado"
[[ "$DOCKER_RAN" == true ]] \
    && echo -e "  ${GREEN}✔${RESET}  Stack Docker iniciada" \
    || echo -e "  ${YELLOW}–${RESET}  Stack Docker não iniciada"
echo -e "\n  ${BOLD}Próximo passo obrigatório:${RESET}"
echo -e "  Gere o ${BOLD}WEBUI_API_TOKEN${RESET} (instruções comentadas no .env) e preencha a variável.\n"
