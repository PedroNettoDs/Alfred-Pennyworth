# Alfred Pennyworth — Assistente Pessoal com IA Local

![version](https://img.shields.io/badge/version-2.0-blue) ![status](https://img.shields.io/badge/status-active-brightgreen) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

Assistente pessoal 100% local baseado em LLMs via Ollama, com execução de comandos no host, pesquisa web autônoma, vault Obsidian com memória persistente, RAG local (SQLite FTS5 + ChromaDB) e um Painel web para aprovar o que ele propõe.

**Autor:** Pedro Henrique Netto dos Santos  
**Ambiente:** Kubuntu 25.10 · AMD Ryzen 5 4500 · NVIDIA RTX 3060 12GB · 32GB RAM

---

## Sumário

1. [Arquitetura](#1-arquitetura)
2. [Modelos Ollama](#2-modelos-ollama)
3. [Estrutura de Diretórios](#3-estrutura-de-diretórios)
4. [Setup](#4-setup)
5. [Ferramentas e Conversa](#5-ferramentas-e-conversa)
6. [System Prompt do Alfred](#6-system-prompt-do-alfred)
7. [Serviços systemd](#7-serviços-systemd)
7b. [Inbox Assistant — Thunderbird → jornal + pendências](#7b-inbox-assistant--thunderbird--jornal--pendências)
7d. [Liga / desliga — modo jogo](#7d-liga--desliga--modo-jogo)
7c. [WhatsApp — Evolution API → pendências "o que você quer responder?"](#7c-whatsapp--evolution-api--pendências-o-que-você-quer-responder)
8. [Scripts Utilitários](#8-scripts-utilitários)
9. [Referência Rápida](#9-referência-rápida)
10. [Troubleshooting](#10-troubleshooting)
11. [Roadmap — Próximos Poderes](#11-roadmap--próximos-poderes)

---

## 1. Arquitetura

### Diagrama

```
┌────────────────────────────────────────────────────────────────────┐
│  HOST (Kubuntu 25.10)                                              │
│                                                                    │
│  ┌─────────────┐  ┌────────────────┐  ┌─────────────┐  ┌─────────┐ │
│  │ Ollama      │  │ Shell Executor │  │ Researcher  │  │ Laya    │ │
│  │ :11434      │  │ :7071          │  │ :7072       │  │ :7074   │ │
│  └──────▲──────┘  └───────▲────────┘  └──────▲──────┘  └────▲────┘ │
│         │                 │                  │              │      │
│  ┌──────┴─────────────────┴──────────────────┴──────────────┴────┐ │
│  │  Inbox Assistant :7073 (FastAPI)                              │ │
│  │  Painel (/painel) · conversa + ferramentas · e-mail ·         │ │
│  │  WhatsApp · agenda · RAG local (lib_rag)                      │ │
│  └──────▲──────────────────────────────────────────▲─────────────┘ │
│         │ webhook via 172.17.0.1                   │ localhost     │
└─────────┼──────────────────────────────────────────┼───────────────┘
          │                                          │
┌─────────┼──────────────────────────────────────────┼───────────────┐
│  Docker (rede alfred-net, bridge br-alfred)        │               │
│  ┌──────┴────────┐  ┌──────────────┐  ┌────────────┴─┐  ┌────────┐ │
│  │ Evolution API │  │ SearXNG      │  │ ChromaDB     │  │ N8N    │ │
│  │ :8081 (+ PG)  │  │ :8889        │  │ :8001        │  │ :5679  │ │
│  └───────────────┘  └──────────────┘  └──────────────┘  └────────┘ │
└────────────────────────────────────────────────────────────────────┘
```

Até 05/11/2026 havia um container do **Open WebUI** (:3001) como interface de chat, com as tools em `tools/` e o RAG
na Knowledge Base dele. Foi removido (plano em [docs/planos/remover-openwebui.md](docs/planos/remover-openwebui.md)):
as ferramentas viraram Python comum em `services/_shared/lib_ferramentas.py`, o RAG é o `lib_rag` e a conversa é o
`services/inbox/conversa.py` — hoje pelo terminal; a aba **Conversa** no Painel é a próxima etapa
([docs/planos/chat-alfred.md](docs/planos/chat-alfred.md)). O código antigo continua no histórico do git.

### Tabela de Serviços

| Serviço          | Porta | Tipo          | Função                                                   |
|------------------|-------|---------------|----------------------------------------------------------|
| SearXNG          | 8889  | Docker        | Motor de busca privado; usado pelo Researcher, pelos briefings e pela ferramenta `buscar_web` |
| ChromaDB         | 8001  | Docker        | Banco vetorial do RAG local (`lib_rag`) e do Researcher  |
| N8N              | 5679  | Docker        | Automação de workflows (opcional; o `instalar.sh` não sobe) |
| Ollama           | 11434 | Host (nativo) | Servidor de LLMs: chat, transcrição e embeddings (ver [Modelos](#2-modelos-ollama)) |
| Shell Executor   | 7071  | Host (systemd --user; sistema na máquina original) | API REST que executa comandos no host sob whitelist       |
| Researcher       | 7072  | Host (systemd --user; sistema na máquina original) | Orquestra busca web → síntese LLM → escrita no vault     |
| Inbox Assistant  | 7073  | Host (systemd --user) | Lê o Thunderbird, monta o jornal diário das newsletters e cria pendências "vou / não vou"; recebe o webhook do WhatsApp e cria pendências "o que você quer responder?" (CLI + API para o N8N); **Painel do Alfred** em `/painel` (só 127.0.0.1, login com o `SHELL_EXECUTOR_TOKEN`); motor da **conversa** com o Alfred (`./run.sh conversa`) |
| Laya             | 7074  | Host (systemd --user) | Classificador local (ModernBERT) para decisões tipadas rápidas — ver [services/laya/about.md](services/laya/about.md) |
| Evolution API    | 8081 (127.0.0.1) | Docker  | WhatsApp pareado por QR code; entrega mensagens/áudios por webhook e envia as respostas de Pedro (+ PostgreSQL `evolution-postgres`) |

### Comunicação entre Serviços

| Origem          | Destino         | Endereço                        | Protocolo |
|-----------------|-----------------|---------------------------------|-----------|
| Inbox Assistant | Ollama          | `OLLAMA_BASE_URL` (`http://172.17.0.1:11434`) | HTTP      |
| Inbox Assistant | Shell Executor  | `http://127.0.0.1:7071`         | HTTP (ferramentas `executar_comando`, `relatorio_sistema`…) |
| Inbox Assistant | Researcher      | `http://127.0.0.1:7072`         | HTTP (ferramentas `pesquisar`, `andamento_pesquisa`) |
| Inbox Assistant | Laya            | `http://127.0.0.1:7074`         | HTTP      |
| Inbox Assistant | SearXNG         | `http://localhost:8889`         | HTTP (ferramenta `buscar_web`) |
| Inbox Assistant | ChromaDB        | `http://localhost:8001`         | HTTP (RAG local) |
| Researcher      | SearXNG         | `http://localhost:8889`         | HTTP      |
| Researcher      | Ollama          | `http://localhost:11434`        | HTTP      |
| Researcher      | ChromaDB        | `http://localhost:8001`         | HTTP      |
| Researcher      | Vault (alfred)  | filesystem                      | I/O direto |
| Evolution API   | Inbox Assistant | `http://172.17.0.1:7073/whatsapp/webhook-async` (ou o Webhook do N8N: `http://n8n:5678/webhook/alfred-whatsapp`) | HTTP (webhook) |
| Inbox Assistant | Evolution API   | `http://localhost:8081` (`EVOLUTION_URL`; vazio = simulado) | HTTP (`apikey`) |
| N8N             | Evolution API   | `http://evolution-api:8080`     | HTTP (`apikey`) |

> **Nota:** Containers acessam o host via `172.17.0.1` (gateway padrão da bridge `docker0`). Serviços no host acessam containers via `localhost:<porta-exposta>`.

---

## 2. Modelos Ollama

| Modelo                       | Tamanho aprox. | Função                                          |
|------------------------------|----------------|-------------------------------------------------|
| `gemma4:e4b`                 | ~6,6 GB        | Chat padrão das instalações novas (`MODEL_CHAT`), transcrição de áudio do WhatsApp, notas da pesquisa profunda |
| `gemma4:12b`                 | ~8 GB          | Chat mais forte (troque no `MODEL_CHAT` ou em `perfis.yml → conversa: modelo`) |
| `qwen3.6:35b-a3b`            | ~22 GB         | Relatório final da pesquisa profunda (`perfis.yml → pesquisa: modelo_sintese`); não cabe inteiro na 3060, roda com parte na RAM |
| `bge-m3`                     | ~1,2 GB        | Embeddings do RAG local (`MODEL_EMBED`)         |

> **Nota:** o `instalar.sh` grava `gemma4:e4b` e `bge-m3` num `.env` novo; o `.env.example` ainda traz `llama3.1:8b` /
> `nomic-embed-text-v2-moe` como exemplo (configuração original). Configure `MODEL_CHAT`/`MODEL_EMBED` conforme o
> `ollama list`. Modelos com *thinking* (gemma4, qwen3) exigem `OLLAMA_THINK=false` no `.env`, senão gastam os tokens
> pensando e devolvem resposta vazia.

Baixar os modelos:

```bash
ollama pull gemma4:e4b
ollama pull bge-m3
ollama pull gemma4:12b          # opcional
ollama pull qwen3.6:35b-a3b     # opcional (pesquisa profunda)
```

---

## 3. Estrutura de Diretórios

```
Alfred-Pennyworth/
│
├── .env.example                    # Template de variáveis — copiar para .env
├── .env                            # Configuração local (NÃO versionado)
├── .gitignore
├── docker-compose.yml              # Stack Docker completa
│
├── config/
│   └── searxng/
│       └── settings.yml            # Configuração do SearXNG (NÃO versionado)
│
├── data/                           # Dados persistentes dos containers (NÃO versionado)
│   ├── chromadb/                   # Dados do ChromaDB
│   ├── n8n/                        # Workflows e credenciais do N8N
│   └── evolution/                  # Sessão do WhatsApp (instances/) e PostgreSQL da Evolution API
│
├── docs/
│   ├── Alfred.png                  # Avatar/ícone do Alfred
│   ├── Alfred-Pennyworth-v2-Guia-Reconstrucao.md  # Guia de reconstrução do projeto
│   └── alfred-system-prompt.md    # Personalidade do Alfred (system prompt da conversa)
│
├── logs/                           # Logs e timestamps de sync (NÃO versionado)
│
├── services/
│   ├── _shared/                       # Bibliotecas comuns: lib_ferramentas (ferramentas da conversa), lib_rag,
│   │                                  # lib_memoria, lib_db (SQLite FTS5), lib_alfred, perfis.yml
│   ├── researcher/
│   │   ├── alfred-researcher.service  # Modelo de unit de usuário (@ALFRED_DIR@ — preenchido pelo instalar.sh)
│   │   ├── main.py                    # FastAPI — orquestrador de pesquisa
│   │   ├── requirements.txt           # fastapi, uvicorn, httpx
│   │   ├── run.sh                     # Script de inicialização manual
│   │   └── venv/                      # Ambiente virtual Python (NÃO versionado)
│   │
│   ├── inbox/
│   │   ├── core.py                    # Pipeline: Thunderbird → classificar → jornal + pendências
│   │   ├── whatsapp.py                # Poder 3: webhook Evolution → transcrever → classificar → fila (atraso_minutos) → pendência → responder
│   │   ├── agenda.py                  # Evento/tarefa/lembrete detectado → agenda.yml → calendário do Thunderbird
│   │   ├── vigia.py                   # A cada 10 min: API, Evolution, WhatsApp (aviso + QR), Thunderbird, áudios > 2 dias
│   │   ├── thunderbird-ext-agenda/    # Extensão "Alfred — agenda" (Experiment API de calendário)
│   │   ├── conversa.py                # Motor da conversa com o Alfred: contexto, tool calling, aprovação, conversas.db
│   │   ├── inbox-assistant.py         # CLI (ciclo, scan, jornal, pendencias, responder, responder-texto, whatsapp, ouvir, conversa)
│   │   ├── main.py                    # FastAPI :7073 — fachada HTTP para o N8N e webhook da Evolution
│   │   ├── painel.py                  # Painel do Alfred (/painel, só 127.0.0.1): telas e ações de aprovação
│   │   ├── pesquisa.py                # aba Pesquisar: cliente do researcher, leitura de research/ e o botão do OpenNotebook
│   │   ├── painel_web/                # Templates Jinja2 + painel.css / painel.js do painel (sem build)
│   │   ├── n8n/                       # Workflows importáveis (ciclo, aguardar resposta, whatsapp)
│   │   ├── tests/                     # test_whatsapp, test_painel, test_trava… + payloads da Evolution (texto, áudio .ogg, resposta citada…)
│   │   ├── alfred-{inbox,semanal,vigia}.{service,timer}, alfred-inbox-api.service  # modelos de unit de usuário (preenchidos e instalados pelo instalar.sh)
│   │   └── about.md
│   │
│   ├── scripts/
│   │   ├── instalar.sh            # Instalação sem sudo em qualquer pasta: .env, pastas, venvs, units de usuário, containers
│   │   ├── alfred-switch.sh       # Liga/desliga "modo jogo" (atalho na área de trabalho)
│   │   ├── chromadb-indexer.py    # RAG local: indexa os cofres (FTS5 + ChromaDB) e busca — CLI do _shared/lib_rag.py
│   │   ├── alfred-rag.{service,timer}  # indexação incremental a cada 30 min
│   │   └── backup.py              # backup/restore datado (alfred-backup.timer, domingo 13:00)
│   │
│   └── shell-executor/
│       ├── alfred-executor.service    # Modelo de unit de usuário (@ALFRED_DIR@ — preenchido pelo instalar.sh)
│       ├── main.py                    # FastAPI — executor de comandos com whitelist
│       ├── requirements.txt           # fastapi, uvicorn
│       ├── run.sh                     # Script de inicialização manual
│       └── venv/                      # Ambiente virtual Python (NÃO versionado)
│
└── vaults/                         # Vaults Obsidian (NÃO versionados)
    ├── alfred/                     # Vault do Alfred — memória persistente (leitura/escrita)
    └── pedro/                      # Vault do Pedro — base de conhecimento (somente leitura)
```

---

## 4. Setup

### Instalação com um comando (qualquer pasta, sem sudo)

```bash
git clone <url-do-repositório> ~/Alfred-Pennyworth      # qualquer pasta serve
cd ~/Alfred-Pennyworth
services/scripts/instalar.sh              # pode rodar de novo quando quiser: não sobrescreve nada
services/scripts/instalar.sh --modelos    # também baixa no Ollama os modelos do .env (~8 GB)
```

O [`instalar.sh`](services/scripts/instalar.sh) não usa sudo e é idempotente:

1. confere os pré-requisitos e lista o que falta, com o comando de instalação no Arch e no Ubuntu; também avisa
   quando o ufw está inativo, sem entrada DROP ou sem regra para `br-alfred` (ver [Rede](#rede-quem-alcança-o-quê));
2. cria o `.env` a partir do `.env.example` **só se ele não existir**: segredos com `openssl` (nunca impressos),
   vaults em `<repositório>/vaults/`, `gemma4:e4b` + `bge-m3`, `WHATSAPP_DRY_RUN=true`, `DOCKER_BIND_IP=127.0.0.1`.
   Com `.env` existente, só aponta as chaves obrigatórias vazias;
3. cria as pastas que faltam (`vaults/`, `data/`, `logs/`) sem tocar nas existentes. Se o `.env` aponta um vault
   para um caminho **fora** do repositório que não existe (repositório movido?), não cria um vault vazio lá: marca
   como faltando e pede para ajustar o `.env`;
4. cria `config/searxng/settings.yml` se não existir (JSON ligado, limiter desligado, chave própria);
5. cria os venvs e instala os `requirements.txt` de cada serviço. Venv quebrado (sem `python`/`pip`) ou criado
   em outra pasta (repositório movido: o shebang de `venv/bin/pip` aponta o caminho antigo) é recriado do zero;
6. gera as units de usuário em `~/.config/systemd/user/` a partir dos modelos do repositório (`@ALFRED_DIR@` →
   caminho real), recarrega o systemd de usuário e habilita API do inbox, shell executor, researcher e todos os
   timers, reiniciando o serviço cuja unit mudou. Exceções, sempre com aviso:
   - executor/researcher que já existem como unit de **sistema** (máquina original): a de usuário não é instalada;
   - unit ou timer que já estava instalado e **desabilitado** (ex.: briefing vespertino na máquina original)
     continua desabilitado;
   - unit já instalada que aponta para **outro** repositório que ainda existe não é sobrescrita — `--forcar-units`
     regrava com este;
   - em modo jogo (`logs/alfred_desligado`) as units são habilitadas mas não iniciadas;
7. sobe os containers do Alfred (`searxng chromadb evolution-postgres evolution-api` — sem N8N; em modo jogo, só
   `searxng chromadb`: a Evolution e o banco dela voltam com `alfred-switch.sh on`) e, com `--modelos`, baixa os
   modelos. Termina com um resumo do que fez e do que ainda falta e **sai com código 1 se algo ficou faltando**.

Opções: `--verificar` (só pré-requisitos), `--sem-containers`, `--sem-units`, `--forcar-units`. Outros modelos no
`.env` novo: `ALFRED_MODEL_CHAT=... ALFRED_MODEL_EMBED=... services/scripts/instalar.sh`.

### Pré-requisitos com sudo — Arch Linux

O Arch não suporta atualização parcial: instale sempre com `-Syu` (neste PC, `pacman -S` com o banco de pacotes
desatualizado falhou com 404).

```bash
sudo pacman -Syu --needed docker docker-compose ollama ollama-cuda ffmpeg libnotify python-gobject ufw \
    kdialog thunderbird thunderbird-i18n-pt-br firefox firefox-i18n-pt-br
```

**Se o `-Syu` atualizou `linux` ou `nvidia-*`, reinicie antes de seguir** — até o reboot o kernel em execução não
acha os próprios módulos e Docker, ufw e driver da GPU falham ao carregar. Depois:

```bash
sudo systemctl enable docker.service ollama.service    # sobem sozinhos a partir do próximo boot
sudo usermod -aG docker "$USER"
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment=OLLAMA_HOST=0.0.0.0:11434\n' | sudo tee /etc/systemd/system/ollama.service.d/alfred.conf
sudo systemctl daemon-reload
sudo ufw default deny incoming
sudo systemctl enable --now ufw
sudo ufw enable
sudo ufw allow in on br-alfred to any port 7071:7073 proto tcp comment 'Alfred: containers -> APIs do host'
sudo ufw allow in on br-alfred to any port 11434 proto tcp comment 'Alfred: containers -> Ollama'
```

Por fim, **reinicie**: Docker e Ollama sobem já com o drop-in e o grupo `docker` passa a valer (sem reiniciar:
`sudo systemctl start docker.service ollama.service` e saia e entre na sessão). Só então rode o `instalar.sh`.

- O ufw ativo com entrada DROP é o que barra a rede local (ver [Rede](#rede-quem-alcança-o-quê)); as duas regras
  `br-alfred` são as únicas exceções. Se você entra nesta máquina por SSH, libere antes do `ufw enable`: `sudo ufw allow ssh`.
- Python 3.12+, `openssl` e `curl` costumam já estar instalados; `zenity` serve no lugar do `kdialog` — o
  `instalar.sh --verificar` diz se falta algum.
- `ollama-cuda` traz a aceleração NVIDIA (puxa o pacote `cuda`, ~5 GB). Driver NVIDIA já instalado.
- O grupo `docker` só vale numa sessão nova. No Arch não existe `sg`: depois do `usermod`, saia e entre na sessão (ou
  reinicie) — antes disso o `instalar.sh` para em "sem acesso ao Docker nesta sessão". No Ubuntu o `instalar.sh` usa
  `sg docker` até o próximo login.
- `firefox` só é necessário para usar o Painel neste navegador.

### Pré-requisitos — Ubuntu (máquina original)

Docker com o plugin compose (`docker.io docker-compose-v2`), [Ollama](https://ollama.com) (`curl -fsSL https://ollama.com/install.sh | sh`),
Python 3.12+ com `python3-venv`, `ffmpeg`, `libnotify-bin`, `python3-gi`, `kdialog` e GPU NVIDIA com driver
(para `nvidia-smi` funcionar nas ferramentas).

A proteção de rede vale igual: drop-in `OLLAMA_HOST=0.0.0.0:11434` no `ollama.service` (criado pelo script do Ollama),
ufw com entrada DROP e as mesmas regras `br-alfred`. **No Ubuntu o ufw vem inativo por padrão** — sem o `ufw enable`,
as portas que escutam em `0.0.0.0` ficam abertas para a rede local:

```bash
sudo usermod -aG docker "$USER"
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment=OLLAMA_HOST=0.0.0.0:11434\n' | sudo tee /etc/systemd/system/ollama.service.d/alfred.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama.service
sudo ufw default deny incoming
sudo ufw enable                       # acesso por SSH? antes: sudo ufw allow ssh
sudo ufw allow in on br-alfred to any port 7071:7073 proto tcp comment 'Alfred: containers -> APIs do host'
sudo ufw allow in on br-alfred to any port 11434 proto tcp comment 'Alfred: containers -> Ollama'
```

Depois, o mesmo `services/scripts/instalar.sh` (no Ubuntu ele usa `sg docker` até o próximo login).

### Rede: quem alcança o quê

| Serviço | Escuta em | Quem alcança |
|---|---|---|
| Ollama (11434), API do inbox (7073), shell executor (7071), researcher (7072) | `0.0.0.0` | o próprio PC e os containers do Alfred; a rede local só é barrada **com o ufw ativo e entrada DROP** — sem isso, essas portas ficam abertas na rede local |
| SearXNG (8889), ChromaDB (8001), N8N (5679) | `DOCKER_BIND_IP` (`127.0.0.1` nas instalações novas) | só o próprio PC |
| Evolution API (8081) | `127.0.0.1` | só o próprio PC |

Os containers chegam ao host por `172.17.0.1` (webhook da Evolution e N8N → API do inbox), mas o
tráfego entra pela bridge da rede `alfred-net`, que tem nome fixo (`br-alfred`, no `docker-compose.yml`). Com o ufw
ativo e política de entrada DROP, só 7071–7073 e 11434 são liberadas, e só para quem entra por `br-alfred`. O
`instalar.sh` avisa quando o ufw está inativo (ou ausente), quando a entrada não é DROP e quando falta a regra de
`br-alfred`. Bind em `127.0.0.1` cortaria os containers e bind em `172.17.0.1` quebraria o Painel (que só aceita
cliente `127.0.0.1`). Porta publicada pelo Docker passa por cima do ufw — por isso SearXNG, ChromaDB e N8N são
publicados em `DOCKER_BIND_IP`. Máquina que já tinha a rede `alfred-net` sem o nome fixo: `docker compose down` e
`docker compose up -d searxng chromadb evolution-postgres evolution-api` (N8N opcional) recriam a rede —
ou `docker compose down` e o `instalar.sh` de novo.

### Depois da instalação (só você pode fazer)

1. Baixe os modelos do `.env` (`services/scripts/instalar.sh --modelos`, ou `ollama pull` um a um — ver [Modelos](#2-modelos-ollama)).
2. Abra o Painel em `http://127.0.0.1:7073/painel` e entre com o `SHELL_EXECUTOR_TOKEN` (o resumo do `instalar.sh`
   mostra como copiá-lo para a área de transferência sem exibi-lo).
3. Teste a conversa: `services/inbox/run.sh conversa "quais pendências tenho?"` (ver [seção 5](#5-ferramentas-e-conversa)).
4. Se for usar e-mail e WhatsApp: perfil do Thunderbird ([seção 7b](#7b-inbox-assistant--thunderbird--jornal--pendências))
   e pareamento por QR code ([seção 7c](#7c-whatsapp--evolution-api--pendências-o-que-você-quer-responder)).

Instalações antigas: o container `alfred-webui` e a pasta `data/open-webui/` (dono root) podem ser apagados
(`docker rm -f alfred-webui` e `sudo rm -rf data/open-webui`), e as chaves `WEBUI_*` e `RESEARCHER_KB_*` saem do `.env` — nada mais as lê.

### Instalação manual (Ubuntu — como foi feita na máquina original)

**1. Clonar o repositório**

```bash
git clone <url-do-repositório> ~/Alfred-Pennyworth
cd ~/Alfred-Pennyworth
```

**2. Criar o arquivo de configuração**

```bash
cp .env.example .env
```

Abra `.env` e preencha os valores. Os campos obrigatórios antes de subir os containers:

```ini
HOST_IP=172.17.0.1
VAULT_PEDRO=/caminho/absoluto/para/vault/pedro
VAULT_ALFRED=/caminho/absoluto/para/vault/alfred
VAULT_NOTICIAS=/caminho/absoluto/para/vault/alfred/Noticias   # jornal + podcast (cofre próprio)
N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=<senha>
N8N_ENCRYPTION_KEY=<gerar abaixo>
SHELL_EXECUTOR_TOKEN=<gerar abaixo>
```

**3. Gerar segredos**

Execute para cada campo de token/chave:

```bash
openssl rand -hex 32
```

Use saídas diferentes para `N8N_ENCRYPTION_KEY` e `SHELL_EXECUTOR_TOKEN`.

> **Atenção:** Nunca adicione comentários na mesma linha de um valor no `.env`. A linha `TOKEN=abc123 # meu token` faz o `#` ser interpretado como parte do valor, quebrando a autenticação.

**4. Subir os containers Docker**

```bash
docker compose up -d
```

Verifique se todos os containers subiram:

```bash
docker compose ps
```

**5. Instalar o Shell Executor**

```bash
cd services/shell-executor

# Criar venv e instalar dependências
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# A máquina original roda o executor como unit de SISTEMA (/etc/systemd/system/alfred-executor.service,
# User=netto). O arquivo do repositório agora é um MODELO de unit de usuário (@ALFRED_DIR@): numa máquina
# nova use o instalar.sh; ele detecta a unit de sistema antiga e não instala a de usuário por cima.
sudo systemctl enable --now alfred-executor

# Confirmar que subiu
sleep 4
curl -s http://localhost:7071/health
```

**6. Instalar o Researcher**

```bash
cd services/researcher

python3 -m venv venv
venv/bin/pip install -r requirements.txt

# idem: unit de SISTEMA já instalada na máquina original; numa máquina nova, instalar.sh
sudo systemctl enable --now alfred-researcher

sleep 4
curl -s http://localhost:7072/health
```

---

## 5. Ferramentas e Conversa

### Conversa com o Alfred

Motor em [`services/inbox/conversa.py`](services/inbox/conversa.py): monta o contexto (personalidade de
`docs/alfred-system-prompt.md`, bloco "Agora" com data, agenda e pendências, memória, RAG quando a pergunta pede e
histórico), chama o Ollama `/api/chat` com tool calling nativo e repete até a resposta final. Ferramentas marcadas
⚠ **perigosas** não rodam sem aprovação explícita. As conversas ficam em `VAULT_ALFRED/conversas.db` (SQLite + FTS5).

```bash
cd services/inbox
./run.sh conversa "quais pendências tenho?"            # nova conversa (aprovação das perigosas no terminal)
./run.sh conversa "e a de e-mail?" --conversa <ID>     # continua uma conversa
```

Modelos e contexto em `perfis.yml → conversa:` (`modelo`, `modelo_ferramentas`, `num_ctx`). A aba **Conversa** no
Painel (streaming, cartão "Executar? [Sim] [Não]") é a próxima etapa — ver [docs/planos/chat-alfred.md](docs/planos/chat-alfred.md).

### Ferramentas

Registro em [`services/_shared/lib_ferramentas.py`](services/_shared/lib_ferramentas.py): cada ferramenta é uma
função Python com `@ferramenta`, o JSON Schema sai da assinatura e `executar()` devolve erro como texto (o modelo
nunca recebe exceção). Código do repositório é chamado direto; HTTP só para o Shell Executor e o Researcher
(`127.0.0.1`, Bearer `SHELL_EXECUTOR_TOKEN` do `.env`) e para o SearXNG. Não há Valves nem importação manual.

| Área | Ferramentas |
|---|---|
| Sistema (Shell Executor) | `executar_comando` ⚠, `relatorio_sistema`, `status_disco`, `status_gpu` |
| Pesquisa e web | `pesquisar`, `andamento_pesquisa` (Researcher), `buscar_web`, `buscar_artigos` (OpenAlex / Semantic Scholar), `ler_pagina` |
| Briefings e resumo | `gerar_briefing`, `ver_briefings`, `ler_briefing`, `resumo_do_dia` |
| Pendências, jornal e limpeza | `listar_pendencias`, `ver_pendencia`, `responder_pendencia` ⚠, `jornal_de_hoje`, `listar_limpezas`, `responder_limpeza` ⚠ |
| E-mail e WhatsApp | `emails_recentes`, `ler_email`, `buscar_mensagens`, `conversa_com` |
| Tarefas e agenda | `adicionar_tarefa`, `listar_tarefas`, `completar_tarefa` ⚠, `tarefas_vencidas`, `ver_agenda`, `criar_compromisso` ⚠ |
| Memória e cofres | `lembrar`, `buscar_memoria`, `listar_memoria`, `buscar_cofre`, `ler_nota`, `listar_cofre`, `salvar_nota` ⚠ |

De onde veio cada uma (as antigas tools do Open WebUI) e as regras de cada ferramenta:
[services/_shared/about.md](services/_shared/about.md).

---

## 6. System Prompt do Alfred

O arquivo completo está em [`docs/alfred-system-prompt.md`](docs/alfred-system-prompt.md).

### Persona

Alfred Pennyworth — mordomo pessoal de Pedro Netto. Responde sempre em português brasileiro. Tom formal o suficiente para ter dignidade, direto o suficiente para ser útil. Humor seco britânico permitido; sycophancy proibida.

### Ordem de decisão das ferramentas

O Alfred segue estritamente esta hierarquia antes de qualquer resposta que envolva informação externa:

1. **Vault próprio** (`buscar_cofre`, cofre `alfred`) — verifica se já pesquisou antes
2. **Vault do Pedro** (`buscar_cofre`, cofre `pedro`) — apenas para questões pessoais/projetos do Pedro
3. **Shell Executor** (`executar_comando`) — para qualquer estado observável na máquina
4. **Pesquisa web** (`pesquisar`) — último recurso; sintetiza e salva no vault

> O texto de `docs/alfred-system-prompt.md` ainda usa os nomes das tools do Open WebUI (`alfred_vault_writer`,
> `execute_command`…); a conversa o carrega como está. A revisão para os nomes acima é a Fase 4 de
> [docs/planos/chat-alfred.md](docs/planos/chat-alfred.md).

### Regras do vault

O Alfred usa seu vault como memória persistente entre sessões:

- **`research/`** — pesquisas web realizadas (3 arquivos: `index.md`, `synthesis.md`, `sources.md`)
- **`decisions/`** — decisões técnicas e raciocínio por trás delas
- **`logs/`** — erros diagnosticados e solucionados; resumos de conversas importantes

Toda nota salva inclui frontmatter com `title`, `date`, `tags` e `topics` para facilitar buscas futuras.

---

## 7. Serviços systemd

Units de **usuário** (`~/.config/systemd/user/`), geradas pelo [`instalar.sh`](services/scripts/instalar.sh) a partir
dos modelos do repositório (`@ALFRED_DIR@` vira o caminho real). Cada `run.sh` carrega o `.env` da raiz — não há
caminho fixo em nenhuma unit nem script. A leitura é literal, linha a linha: o `run.sh` do executor e o do researcher
faziam `source` do `.env`, e um valor com `$` ou `(` (uma senha, por exemplo) derrubava o serviço; agora leem como o
inbox e o briefing já liam.

| Unit | Modelo no repositório | O quê | Quando |
|---|---|---|---|
| `alfred-inbox-api.service` | `services/inbox/` | API do inbox + Painel (`PORT_INBOX`, 7073) | sempre no ar |
| `alfred-executor.service` | `services/shell-executor/` | Shell Executor (`PORT_SHELL_EXECUTOR`, 7071) | sempre no ar |
| `alfred-researcher.service` | `services/researcher/` | Researcher (`PORT_RESEARCHER`, 7072) | sempre no ar |
| `alfred-briefing-matinal.timer` / `-vespertino.timer` | `services/briefing/` | briefings por RSS | 07:00 / 18:00 |
| `alfred-inbox.timer` | `services/inbox/` | ciclo do e-mail | 07:30 e 18:30 |
| `alfred-semanal.timer` | `services/inbox/` | podcast (`--se-devido`) | 19:00 |
| `alfred-vigia.timer` | `services/inbox/` | vigia | 2 min após o login e a cada 10 min |
| `alfred-backup.timer` | `services/scripts/` | backup (`backup.py criar`, ver [Backup e restore](#backup-e-restore)) | domingo 13:00 (`Persistent=true`) |
| `alfred-rag.timer` | `services/scripts/` | índice do RAG local (`chromadb-indexer.py indexar`, ver [`lib_rag.py`](services/_shared/about.md#lib_ragpy)) | 5 min após o login e a cada 30 min |

Units de usuário sobem no login (com `loginctl enable-linger` sobem já no boot — o `instalar.sh` não liga o linger).
Os containers voltam sozinhos com o Docker (`restart: unless-stopped`).

- `alfred-inbox.timer` e `alfred-semanal.timer` não têm mais `Requires=` da `.service`: com ele, cada start do timer
  (login, `instalar.sh`, `alfred-switch.sh on`) rodava o ciclo do e-mail e o podcast na hora, fora do horário. O
  timer já dispara a `.service` de mesmo nome.
- `alfred-vigia.timer` não tem mais `OnBootSec=`: ele vencia antes do login e o vigia rodava junto com a API ainda
  subindo (e a reiniciava). Fica só `OnStartupSec=2min` (conta do login, ou do boot com linger) + a cada 10 min.
- Timer desabilitado de propósito (ex.: briefing vespertino na máquina original) continua desabilitado: o
  `instalar.sh` não o religa e o `alfred-switch.sh on` só religa timers habilitados.

> **Máquina original:** o executor e o researcher continuam como units de **sistema** (`/etc/systemd/system/`,
> `User=netto`, `EnvironmentFile=` do `.env`). O `instalar.sh` detecta isso e não instala as versões de usuário.
> Lá os comandos continuam com `sudo systemctl ... alfred-executor` / `alfred-researcher`.

### Comandos de gerenciamento

```bash
# Estado
systemctl --user status alfred-inbox-api alfred-executor alfred-researcher
systemctl --user list-timers 'alfred-*'

# Reiniciar (necessário após alterar o .env)
systemctl --user restart alfred-inbox-api alfred-executor alfred-researcher

# Ver logs em tempo real
journalctl --user -fu alfred-executor
journalctl --user -fu alfred-researcher
journalctl --user -fu alfred-inbox-api

# Rodar um timer agora (ex.: briefing matinal)
systemctl --user start alfred-briefing-matinal.service
```

---

## 7b. Inbox Assistant — Thunderbird → jornal + pendências

O Alfred lê os e-mails que o **Thunderbird** já baixou (cópias offline do perfil — sem senha, sem rede, funciona com Outlook/Gmail via OAuth2), classifica cada um com o Ollama e faz duas coisas:

1. **Edição do dia do jornal** — as newsletters viram itens de notícia (título, resumo, URL só se existir no e-mail) e passam pelo **mesmo pipeline do briefing** (`lib_cluster` → template `jornal_email.md` → Ollama → título → `.md` + `.mp3` com edge-tts) em `VAULT_NOTICIAS/jornal/`.
   - **Cofre de notícias:** `VAULT_NOTICIAS` (padrão desta máquina: `VAULT_ALFRED/Noticias`, com `.obsidian` próprio — abra como vault separado no Obsidian). Vazio = layout antigo (`VAULT_ALFRED/jornal`), que continua sendo lido como fallback.
   - **Podcast a cada 3 dias** (`run.sh podcast`, `POST /semanal`; timer diário com `--se-devido`): relê as edições do jornal e os briefings por RSS dos últimos 3 dias, escolhe os temas que mais se repetiram em mais newsletters e gera um episódio de ~10 min em **diálogo de duas vozes** (Alfred + Lúcia, edge-tts + ffmpeg) em `VAULT_NOTICIAS/podcast/episodios/`. Também grava um manifesto `podcast/manifestos/<período>.json` com os e-mails usados, que serve para a limpeza do Thunderbird. Detalhes em [services/inbox/about.md](services/inbox/about.md#podcast--resumo-do-alfred-a-cada-3-dias-semanalpy).
2. **Pendências "vou / não vou"** — convites a eventos e contatos profissionais (recrutador, proposta, retorno de processo seletivo) geram um texto explicativo + um recado de voz em que Alfred pergunta se Pedro vai. A pergunta fica **aguardando** em `VAULT_ALFRED/pendencias/pendencias.yml` até a resposta, que pode vir por uma pergunta gráfica que fica ociosa esperando o clique (notificação com botões via D-Bus → kdialog → zenity), pelo terminal (`perguntar` ou `responder`), pela conversa (ferramenta `responder_pendencia`), pelo Painel ou pelo N8N. "Vou" cria uma tarefa em `tasks.yml`.

```bash
cd services/inbox
./run.sh scan            # classifica e mostra — nada gravado
./run.sh                 # ciclo completo
./run.sh pendencias      # fila aguardando resposta
./run.sh perguntar       # Alfred pergunta uma a uma no terminal e espera vou/nao
./run.sh responder <id> vou
./run.sh serve           # API HTTP :7073 para o N8N + Painel do Alfred em /painel
```

**Painel do Alfred** — com a API no ar, `http://127.0.0.1:7073/painel` (só desta máquina; login com o `SHELL_EXECUTOR_TOKEN`) mostra as pendências, o rascunho que o Alfred propõe para cada resposta (**Autorizar envio** / **Não enviar** / **Agendar**; **Vou** / **Não vou** no e-mail), a agenda (**Desfazer**), os lotes de limpeza (**Aprovar** / **Recusar**), a aba **Pesquisar** (pesquisa rápida ou profunda pelo researcher, com as recentes do cofre e o botão **Abrir um notebook sobre isso**, que leva fontes e relatório ao OpenNotebook local em 127.0.0.1:8502) e o estado do sistema, atualizando a cada 15 s. Dependência nova `jinja2`: num venv existente rode `venv/bin/pip install -r requirements.txt` antes de reiniciar a API (sem ele a API sobe sem o painel e o log avisa). Detalhes: [about.md](services/inbox/about.md#painel-do-alfred).

Configuração em `services/_shared/perfis.yml` (seção `inbox:`) e no `.env` (`THUNDERBIRD_PROFILE` — vazio = descoberta automática: snap, `~/.config/thunderbird` (Thunderbird 158+), `~/.thunderbird` e flatpak —, `INBOX_DEFAULT`, `OLLAMA_THINK=false` para gemma4/qwen3). Agendamento com units de **usuário** (`systemctl --user`), porque notificação e áudio precisam da sessão gráfica. Detalhes, API e workflows do N8N: [`services/inbox/about.md`](services/inbox/about.md).

**Divisão de papéis com o N8N:** o Python é o motor (ler mbox, clustering, TTS, templates) exposto como API; o N8N orquestra (cron, `Wait` aguardando o "vou/não vou" via `resumeUrl`, e o WhatsApp via Evolution API). Os workflows em `services/inbox/n8n/` são o ponto de partida.

---

## 7c. WhatsApp — Evolution API → pendências "o que você quer responder?"

Mensagens e **áudios** recebidos no WhatsApp passam pelo mesmo tratamento do e-mail. A Evolution API (container `alfred-evolution`, número pareado por QR code) entrega cada mensagem por webhook ao Inbox Assistant, que:

1. **transcreve o áudio localmente** — ffmpeg converte o OGG/Opus do WhatsApp em WAV e o **gemma4** transcreve via Ollama (o áudio vai em base64 no campo `images` do `/api/generate`; fatias de 30 s); fallback `faster-whisper` se instalado;
2. **classifica** com o mesmo padrão do e-mail (`whatsapp_classificar.md`, JSON Schema): `profissional` / `pessoal` / `grupo` / `automatico` / `spam` + "requer resposta?" + urgência;
3. **espera ~10 min** (`atraso_minutos` em `perfis.yml → whatsapp:`): se Pedro responder a conversa direto pelo celular nesse tempo, nada é gerado; mensagens seguidas do contato viram uma pendência só;
4. para contatos relevantes cria uma **pendência `whatsapp`** em `VAULT_ALFRED/pendencias/pendencias.yml` (mesmo esquema, `status: aguardando`, campo `resposta_texto`) com um texto explicativo (`pergunta_whatsapp.md`) e um recado de voz (`audio_pergunta_whatsapp.md` → edge-tts) em que Alfred resume a mensagem e pergunta **"o que você quer responder?"**;
5. avisa Pedro no WhatsApp (resumo com `ref: <id>` + áudio) — pelo N8N (`services/inbox/n8n/alfred-whatsapp.json`) ou pelo próprio Python (`/whatsapp/webhook-async`) — e **no PC**: notificação com a sugestão de resposta escrita pelo Alfred e os botões **Enviar** (abre o rascunho para editar e confirmar) / **Não enviar** / **Agenda** (tarefa em `tasks.yml` + lembrete no PC + evento no Google Agenda já preenchido) / **Ouvir** — o resumo em áudio começa a tocar junto com a notificação (botão **Parar**) e o `.mp3` local é apagado 2 dias depois (a cópia fica no WhatsApp);
6. **a resposta de Pedro é enviada ao contato**: ele responde *citando* a mensagem do Alfred (ou pelo botão Enviar no PC, **Autorizar envio** no Painel, `./run.sh responder-texto <id> "texto"`, o chat: "responde pro Carlos que…", ou `POST /pendencias/{id}/responder-texto`). `ignorar` fecha sem enviar; se Pedro responder o contato direto no celular, a pendência fecha sozinha (guardando o rascunho do Alfred e a resposta real).

```bash
docker compose up -d evolution-postgres evolution-api       # .env: EVOLUTION_API_KEY, EVOLUTION_POSTGRES_PASSWORD, WHATSAPP_NUMERO_PEDRO
cd services/inbox
./run.sh whatsapp qrcode                                   # cria a instância e abre o QR (WhatsApp → Aparelhos conectados)
./run.sh serve                                             # API :7073
./run.sh whatsapp webhook http://172.17.0.1:7073/whatsapp/webhook-async   # ou a URL do Webhook do N8N
./run.sh whatsapp status                                   # instância "open", fila
./run.sh responder-texto <id> "Carlos, mando até quinta."  # resposta → WhatsApp do contato
venv/bin/python -m unittest tests.test_whatsapp -v         # 63 testes offline (inclui o formato real da Evolution v2.3.7)
```

**Busca e histórico (SQLite FTS5).** O texto das conversas fica em `VAULT_ALFRED/.alfred/alfred.db` (90 dias, `perfis.yml → whatsapp: historico_dias`) e as 5 últimas mensagens do contato entram no prompt da pendência; o mesmo arquivo indexa os cofres para a busca das ferramentas e guarda um cache que faz contatos de spam/automáticos e remetentes de promoção pularem o LLM. `./run.sh indexar` · `./run.sh buscar <termo> [--whatsapp]` — ver [`services/inbox/about.md`](services/inbox/about.md#busca-e-histórico--sqlite-fts5-_sharedlib_dbpy).

Sem `EVOLUTION_URL` no `.env` (ou com `WHATSAPP_DRY_RUN=true`) **nenhuma mensagem sai da máquina**: os envios ficam em `logs/whatsapp_enviados.jsonl`. Pareamento, formatos de payload, configuração (`perfis.yml → whatsapp:`) e limitações: [`services/inbox/about.md`](services/inbox/about.md#whatsapp-poder-3--evolution-api).

---

## 7d. Liga / desliga — "modo jogo"

Atalho na área de trabalho **"Alfred: ligado (clique para modo jogo)"** (também no menu de aplicativos/KRunner,
dá para associar um atalho de teclado global). Um clique desliga, outro religa; o nome e o ícone mostram o estado.

```bash
services/scripts/alfred-switch.sh            # toggle (o que o atalho faz)
services/scripts/alfred-switch.sh off        # desliga
services/scripts/alfred-switch.sh on         # religa
services/scripts/alfred-switch.sh status
```

| Desligar faz | Religar faz |
|---|---|
| para os timers (briefings 07:00 e 18:00, e-mail 07:30/18:30, podcast 19:00, vigia) e qualquer execução em andamento | `docker start` banco + Evolution |
| para a API do Alfred (:7073) — WhatsApp deixa de ser processado e o Painel não abre | sobe a API |
| `docker stop` da Evolution API (WhatsApp) e do banco dela | religa só os timers **habilitados** — um timer desabilitado de propósito (ex.: briefing vespertino) continua parado (o vigia confere tudo em seguida) |
| fecha as notificações que esperam clique (as pendências continuam aguardando) | reabre essas notificações |
| descarrega os modelos do Ollama da VRAM (GPU livre para jogar) | — |

O estado sobrevive a reinicializações: `logs/alfred_desligado` faz os `run.sh` recusarem qualquer execução vinda
das units do systemd (elas exportam `ALFRED_AUTOMACAO=1`), e a Evolution parada com `docker stop` não volta sozinha
(`restart: unless-stopped`). Comandos manuais no terminal continuam funcionando (`ALFRED_FORCAR=1` libera até
`serve`/`vigia`). Rodar o `instalar.sh` em modo jogo também não religa nada: habilita as units sem iniciá-las e não
sobe a Evolution nem o banco dela. Não existe WAHA nesta máquina: a ponte do WhatsApp é a Evolution API.

---

## 8. Scripts Utilitários

Localização: `services/scripts/`

| Script                 | Função                                                                          | Quando usar                                           | Exemplo de uso                                      |
|------------------------|---------------------------------------------------------------------------------|-------------------------------------------------------|-----------------------------------------------------|
| `chromadb-indexer.py`  | RAG local (`_shared/lib_rag.py`): indexa os dois cofres no FTS5 e no ChromaDB (embeddings `MODEL_EMBED`), só o que mudou, e faz a busca híbrida. | A cada 30 min pelo `alfred-rag.timer`; à mão depois de editar muito no Obsidian (`--tudo` reembeda tudo). | `services/inbox/venv/bin/python services/scripts/chromadb-indexer.py buscar "proxy reverso"` |
| `backup.py`            | Backup datado (`.tar.gz`) de `logs/`, do `VAULT_ALFRED`, dos bancos SQLite e do `perfis.yml`; restore com confirmação e `--dry-run`. Ver [Backup e restore](#backup-e-restore). | Toda semana pelo `alfred-backup.timer`; à mão antes de mexer em algo grande; restore depois de perda de dados ou ao trocar de máquina. | `services/inbox/venv/bin/python services/scripts/backup.py criar` |

### Backup e restore

`services/scripts/backup.py` guarda o estado do Alfred num `.tar.gz` datado (`alfred-backup-AAAA-MM-DD_HHMMSS.tar.gz`).
Roda com o Python de um venv do Alfred e lê o `.env` só para achar o `VAULT_ALFRED` e o `ALFRED_BACKUP_DIR`.

| Entra | Não entra |
|---|---|
| `logs/` (seen, fila, categorias, estado do vigia…) | `.env` — segredos; guarde à parte ou refaça com o `instalar.sh` |
| `VAULT_ALFRED` inteiro (pendências, agenda, memória, jornal, podcast) | `data/` — volumes do Docker, do root |
| bancos SQLite (ex.: `VAULT_ALFRED/.alfred/alfred.db`), copiados pela API de backup online do SQLite (a do `sqlite3 .backup`): cópia consistente mesmo com a API gravando | `-wal`/`-shm`, `*.tmp`, `*.lock`, symlinks |
| `services/_shared/perfis.yml` | o resto do repositório (está no git) |

- **Destino:** `--destino`, `ALFRED_BACKUP_DIR` do `.env` ou `~/.local/share/alfred/backups` — nunca o `/tmp`, que
  some no reboot. Para sobreviver a um disco que morre, aponte o `ALFRED_BACKUP_DIR` para outro disco.
- O arquivo nasce `0600` (tem e-mails e conversas do WhatsApp), só ganha o nome final depois de completo e ficam os
  `--manter` mais novos (padrão 8; `0` = todos).
- **Restore:** escreve só o que está no backup (não apaga nada a mais), cada arquivo por troca atômica, e apaga o
  `-wal`/`-shm` velho ao lado de cada banco restaurado. `repo/` volta para a raiz do repositório (`--raiz`) e `vault/`
  para o `VAULT_ALFRED` (`--vault`; sem nenhum dos dois, o caminho de origem gravado no backup). Recusa o arquivo
  inteiro se algum caminho escapar dessas pastas.
- **Agendamento:** `alfred-backup.timer` (domingo 13:00, `Persistent=true`), instalado e habilitado pelo
  `instalar.sh` como os outros timers. Ele não para no modo jogo (só lê e compacta, sem GPU).

```bash
services/inbox/venv/bin/python services/scripts/backup.py criar                              # backup agora
services/inbox/venv/bin/python services/scripts/backup.py criar --destino /media/hd/alfred   # em outro disco
services/inbox/venv/bin/python services/scripts/backup.py restaurar ARQUIVO.tar.gz --dry-run  # só mostra o que faria
services/scripts/alfred-switch.sh off                                                         # pare o Alfred antes de restaurar
services/inbox/venv/bin/python services/scripts/backup.py restaurar ARQUIVO.tar.gz            # pede "sim" (--sim pula)
services/scripts/alfred-switch.sh on
journalctl --user -u alfred-backup                                                            # resultado do backup semanal
```

---

## 9. Referência Rápida

```bash
# ── Docker ────────────────────────────────────────────────────
docker compose up -d searxng chromadb evolution-postgres evolution-api   # Subir os containers (+ n8n, opcional)
docker compose down                           # Parar todos os containers
docker compose ps                             # Ver status
docker compose logs -f searxng                # Logs do SearXNG (nome do serviço, não do container)

# ── systemd (units de usuário; na máquina original executor/researcher são de sistema: sudo systemctl …)
systemctl --user status alfred-executor       # Status do Shell Executor
systemctl --user status alfred-researcher     # Status do Researcher
systemctl --user restart alfred-executor      # Reiniciar após alterar .env
journalctl --user -fu alfred-researcher       # Logs em tempo real

# ── Health checks ─────────────────────────────────────────────
curl -s http://localhost:7071/health          # Shell Executor
curl -s http://localhost:7072/health          # Researcher
curl -s http://localhost:7073/health          # Inbox Assistant (API + Painel)
curl -s 'http://localhost:8889/search?q=test&format=json' | head -c 200  # SearXNG

# ── Ollama ────────────────────────────────────────────────────
ollama list                                   # Modelos instalados
ollama ps                                     # Modelos em execução
ollama pull gemma4:e4b                        # Baixar modelo
curl -s http://localhost:11434/api/tags       # API REST do Ollama

# ── GPU ───────────────────────────────────────────────────────
nvidia-smi                                    # Status completo da GPU
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader

# ── Conversa e RAG ────────────────────────────────────────────
services/inbox/run.sh conversa "pergunta"     # Conversa com o Alfred (ferramentas, aprovação no terminal)
services/inbox/venv/bin/python services/scripts/chromadb-indexer.py buscar "consulta"   # Busca híbrida nos cofres
```

---

## 10. Troubleshooting

### SearXNG retorna 403 nas buscas

**Causa:** O limiter de taxa do SearXNG está bloqueando requisições programáticas.  
**Solução:** Edite `config/searxng/settings.yml` e adicione ou altere:

```yaml
server:
  limiter: false
```

Reinicie o container:

```bash
docker compose restart searxng
```

---

### Ferramenta do Shell Executor ou do Researcher responde 401

**Causa:** O `SHELL_EXECUTOR_TOKEN` mudou no `.env` e algum serviço ainda roda com o valor antigo (o `.env` só é lido
na inicialização).
**Solução:** Reinicie os três serviços que usam o token:

```bash
systemctl --user restart alfred-inbox-api alfred-executor alfred-researcher   # máquina original: executor/researcher com sudo systemctl
```

---

### Researcher exibe modelo desatualizado no health check

**Causa:** O serviço systemd carregou o `.env` na inicialização e não relê após mudanças.  
**Solução:**

```bash
systemctl --user restart alfred-researcher     # máquina original: sudo systemctl restart alfred-researcher
curl -s http://localhost:7072/health
```

---

### `curl http://localhost:7071/health` retorna "Connection refused" logo após `systemctl start`

**Causa:** O uvicorn leva alguns segundos para completar a inicialização.  
**Solução:** Aguarde 3–5 segundos após o start antes de testar. Para scripts de automação, use um loop com retry:

```bash
for i in $(seq 1 10); do
    curl -sf http://localhost:7071/health && break
    sleep 1
done
```

---

### Token quebrado — autenticação falha mesmo com o valor correto

**Causa:** Comentário inline no `.env` faz o `#` e o texto após ele serem incluídos no valor da variável.  
**Errado:**

```ini
SHELL_EXECUTOR_TOKEN=abc123def456  # token gerado em 2025-01-01
```

**Correto:**

```ini
# token gerado em 2025-01-01
SHELL_EXECUTOR_TOKEN=abc123def456
```

---

## 11. Roadmap — Próximos Poderes

### Poder 1 — Scanner de Notícias Tech

Briefing matinal/vespertino (RSS + SearXNG → clustering → Ollama → vault + áudio) em `services/briefing/`, e **edição do dia do jornal** a partir das newsletters do e-mail em `services/inbox/`.

**Status:** ✅ Implementado (briefing via systemd timer; jornal por e-mail via Inbox Assistant)

---

### Poder 1b — Inbox Assistant (Thunderbird)

Classificação dos e-mails do Thunderbird, jornal diário das newsletters e fila de convites/contatos aguardando "vou / não vou" (texto + áudio), com API para o N8N.

**Status:** ✅ Implementado — ver [seção 7b](#7b-inbox-assistant--thunderbird--jornal--pendências). Workflows N8N escritos, ainda não validados num N8N em execução.

---

### Poder 1c — Briefings no Blogspot

Briefings, jornal diário e podcast nascem com `Blogspot: none` no frontmatter. A extensão **Alfred — blog** do Thunderbird envia cada um por e-mail para o endereço de "Postar usando e-mail" do Blogger (`BLOGGER_EMAIL` no `.env`), com o `.mp3` anexado, e o Alfred troca a propriedade por `Blogspot: postado <data>`.

**Status:** 🟡 Implementado (03/11/2026) e testado offline. A extensão ainda não rodou num Thunderbird real — ver [BLOG.md](services/inbox/BLOG.md).

**Próximo passo — áudio dentro do post:** o Blogger por e-mail só aceita texto e imagens, então o `.mp3` anexado não vira player. Hospedar os `.mp3` numa plataforma online e pôr no HTML do post um player (`<audio controls src=…>` ou o embed da plataforma). Candidatas a avaliar:
- **Internet Archive (archive.org):** gratuito, upload por API S3 (chave da conta), link direto do `.mp3` e player embutível.
- **Google Drive:** mesma conta Google do Blogger, embed por `iframe` de preview, upload pela API do Drive (OAuth).
- **GitHub Releases/Pages:** link direto do `.mp3` para `<audio>`, upload pelo `gh`; limites de tamanho e de uso.
- **Plataforma de podcast** (Spotify for Creators, Podbean…): player e feed RSS, mas a maioria não tem API pública de upload.

O fluxo seria: `blog.py` sobe o `.mp3` antes de montar a fila, guarda a URL no frontmatter (ex.: `audio_url:`) e põe o player no HTML. Antes, conferir se o Blogger mantém `<audio>`/`iframe` num post que chega por e-mail.

---

### Poder 2 — Scanner de Licitações

Scraper de portais públicos de licitação (ComprasNet, Portal de Compras SP) que indexa editais no ChromaDB e identifica licitações com perfil compatível com critérios configuráveis via RAG.

**Pipeline:** N8N (cron diário) → Web scraping (portais públicos) → Ollama (geração de embeddings) → ChromaDB (upsert + query por similaridade) → Relatório de oportunidades no vault

**Status:** 🔲 Planejado

---

### Poder 3 — WhatsApp + Agendamento

Integração com WhatsApp via Evolution API: mensagens e áudios recebidos (transcrição local com gemma4) passam pelo mesmo tratamento do e-mail e viram uma pendência em que Alfred resume a mensagem e pergunta "o que você quer responder?" (texto + áudio); a resposta de Pedro é enviada de volta pelo WhatsApp.

**Pipeline:** Evolution API (Docker) → webhook → Inbox Assistant (`whatsapp.py`: transcrever → classificar → pendência + áudio) → N8N ou Python avisa Pedro → resposta citada / CLI / chat / API / Painel → Evolution API `sendText`

**Status:** ✅ Implementado e **validado com WhatsApp real em 2026-10-19** (instância no próprio número de Pedro, Evolution API "v2.3.7", sem N8N): texto e áudio viram pendência, resumo + áudio chegam a Pedro, a resposta citada é entregue ao contato, `ignorar` e resposta direta pelo celular fecham a pendência, e a notificação no PC (Enviar / Não enviar / Agenda / Ouvir) envia o rascunho. Depois da validação: **espera de ~10 min antes de resumir** (`atraso_minutos: 10` em `perfis.yml → whatsapp:`) — resumo, áudio e avisos só saem se Pedro não responder a conversa nesse tempo; se ele responder direto pelo celular antes, nada é gerado (testado offline, ainda não com WhatsApp real — ver [espera](services/inbox/about.md#espera-antes-de-resumir-atraso_minutos)). Falta validar o workflow N8N (stack Docker fora do ar nesta máquina) — ver [validação](services/inbox/about.md#validação-com-whatsapp-real-2026-10-19).

**Agenda automática (20/10/2026):** evento, tarefa ou lembrete com data detectado numa mensagem de contato, no que Pedro escreve para si mesmo na conversa "Você" ("me lembra de pagar o boleto sexta") ou num "vou" de convite por e-mail vai para `VAULT_ALFRED/agenda/agenda.yml` e, pela extensão **Alfred — agenda** do Thunderbird, para o calendário "Agenda" (Google via CalDAV → celular), com notificação **[Ok] [Mais detalhes] [Desfazer]** (Mais detalhes abre o item no painel). Extensão validada num perfil de teste do Thunderbird 157; falta instalar no perfil real — ver [agenda](services/inbox/about.md#agenda--evento--tarefa--lembrete--calendário-do-thunderbird-agendapy).

**Vigia (20/10/2026):** a conexão caiu em 19/10 às 19:10 (`device_removed`: o WhatsApp desvinculou o aparelho). O `alfred-vigia.timer` (no login e a cada 10 min) reinicia API/containers parados e, se o WhatsApp cair, mostra uma notificação com **[Mostrar QR code]** para parear de novo.

**Próximos passos:**
- **Comandos para o Alfred pelo WhatsApp** — hoje tudo o que Pedro escreve citando o resumo vai para o contato: na validação real, "Pode agendar para terça-feira às 18:00 (coloque na minha agenda também)" foi entregue inteiro à Attano Tech. Separar instrução para o Alfred do texto ao contato (ex.: trecho marcado `[Alfred: põe na agenda]` é executado e removido antes do envio) e aceitar outros comandos diretos ao Alfred na conversa "Você" além de agenda/lembrete (já implementado), ex.: pesquisa, tarefa sem data.
- Usar os nós "Evolution: texto/áudio para Pedro" do `alfred-whatsapp.json` também no sub-workflow "aguardar resposta" do e-mail (links vou/não vou pelo WhatsApp).
- **Sugestão proativa de resposta pendente** — avaliar mensagens de Pedro sem resposta do contato (não só o inverso, já cobre pendências), pesando amizade/proximidade e urgência, pra sugerir follow-up. Ideia detalhada em [docs/planos/ideia-sugestao-resposta-pendente.md](docs/planos/ideia-sugestao-resposta-pendente.md). **Status:** 🔲 ideia, não planejada.

---

### Melhorias de Infraestrutura Planejadas

| Componente         | Função                                              | Status          |
|--------------------|-----------------------------------------------------|-----------------|
| Cloudflare Tunnel  | Acesso externo seguro sem expor portas no roteador  | 🔲 Planejado    |
| Redis              | Filas e cache para workflows N8N de alta frequência | 🔲 Planejado    |
| Grafana            | Monitoramento de uso de GPU, RAM e saúde dos serviços | 🔲 Planejado  |
| Traefik            | Reverse proxy com SSL automático para os containers | 🔲 Planejado    |