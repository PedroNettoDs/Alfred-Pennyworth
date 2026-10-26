# Alfred Pennyworth — Assistente Pessoal com IA Local

![version](https://img.shields.io/badge/version-2.0-blue) ![status](https://img.shields.io/badge/status-active-brightgreen) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

Assistente pessoal 100% local baseado em LLMs via Ollama, com execução de comandos no host, pesquisa web autônoma, vault Obsidian com memória persistente e RAG via Knowledge Base do Open WebUI.

**Autor:** Pedro Henrique Netto dos Santos  
**Ambiente:** Kubuntu 25.10 · AMD Ryzen 5 4500 · NVIDIA RTX 3060 12GB · 32GB RAM

---

## Sumário

1. [Arquitetura](#1-arquitetura)
2. [Modelos Ollama](#2-modelos-ollama)
3. [Estrutura de Diretórios](#3-estrutura-de-diretórios)
4. [Setup](#4-setup)
5. [Tools do Open WebUI](#5-tools-do-open-webui)
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
┌─────────────────────────────────────────────────────────────────┐
│  HOST (Kubuntu 25.10)                                           │
│                                                                 │
│  ┌─────────────────┐   ┌──────────────────┐   ┌─────────────┐  │
│  │  Ollama          │   │  Shell Executor  │   │  Researcher │  │
│  │  :11434          │   │  :7070 (systemd) │   │  :7071      │  │
│  │  (llama3.1:8b)   │   │  FastAPI         │   │  (systemd)  │  │
│  └────────▲─────────┘   └────────▲─────────┘   └──────▲──────┘  │
│           │                      │                     │         │
│  172.17.0.1 (Docker bridge gateway)                              │
└───────────┼──────────────────────┼─────────────────────┼────────┘
            │                      │                     │
┌───────────┼──────────────────────┼─────────────────────┼────────┐
│  Docker (rede: alfred-net)       │                     │        │
│           │                      │                     │        │
│  ┌────────┴─────────────────┐    │                     │        │
│  │  Open WebUI              │────┘─────────────────────┘        │
│  │  :3000                   │  (tools chamam host via           │
│  │  (modelo Alfred)         │   172.17.0.1)                     │
│  └──────────┬───────────────┘                                   │
│             │                                                    │
│    ┌────────┴───────┐  ┌──────────────┐  ┌──────────────┐      │
│    │  SearXNG       │  │  ChromaDB    │  │  N8N         │      │
│    │  :8888         │  │  :8000       │  │  :5678       │      │
│    └────────────────┘  └──────────────┘  └──────────────┘      │
└────────────────────────────────────────────────────────────────┘
```

### Tabela de Serviços

| Serviço          | Porta | Tipo          | Função                                                   |
|------------------|-------|---------------|----------------------------------------------------------|
| Open WebUI       | 3000  | Docker        | Interface principal; executa modelo Alfred com tools     |
| SearXNG          | 8888  | Docker        | Motor de busca privado; usado pelo WebUI e pelo Researcher |
| ChromaDB         | 8000  | Docker        | Banco vetorial para RAG com embeddings                   |
| N8N              | 5678  | Docker        | Automação de workflows (futuro)                          |
| Ollama           | 11434 | Host (nativo) | Servidor de LLMs; serve llama3.1:8b e nomic-embed       |
| Shell Executor   | 7070  | Host (systemd)| API REST que executa comandos no host sob whitelist       |
| Researcher       | 7071  | Host (systemd)| Orquestra busca web → síntese LLM → escrita no vault     |
| Inbox Assistant  | 7072  | Host (systemd --user) | Lê o Thunderbird, monta o jornal diário das newsletters e cria pendências "vou / não vou"; recebe o webhook do WhatsApp e cria pendências "o que você quer responder?" (CLI + API para o N8N); **Painel do Alfred** em `/painel` (só 127.0.0.1, login com o `SHELL_EXECUTOR_TOKEN`) |
| Evolution API    | 8080 (127.0.0.1) | Docker  | WhatsApp pareado por QR code; entrega mensagens/áudios por webhook e envia as respostas de Pedro (+ PostgreSQL `evolution-postgres`) |

### Comunicação entre Serviços

| Origem          | Destino         | Endereço                        | Protocolo |
|-----------------|-----------------|---------------------------------|-----------|
| Open WebUI      | Ollama          | `http://172.17.0.1:11434`       | HTTP      |
| Open WebUI      | SearXNG         | `http://searxng:8080`           | HTTP      |
| Open WebUI      | ChromaDB        | `http://chromadb:8000`          | HTTP      |
| Open WebUI      | Shell Executor  | `http://172.17.0.1:7070`        | HTTP (tool) |
| Open WebUI      | Researcher      | `http://172.17.0.1:7071`        | HTTP (tool) |
| Researcher      | SearXNG         | `http://localhost:8888`         | HTTP      |
| Researcher      | Ollama          | `http://localhost:11434`        | HTTP      |
| Researcher      | Open WebUI API  | `http://localhost:3000`         | HTTP      |
| Researcher      | Vault (alfred)  | filesystem                      | I/O direto |
| Evolution API   | Inbox Assistant | `http://172.17.0.1:7072/whatsapp/webhook-async` (ou o Webhook do N8N: `http://n8n:5678/webhook/alfred-whatsapp`) | HTTP (webhook) |
| Inbox Assistant | Evolution API   | `http://localhost:8080` (`EVOLUTION_URL`; vazio = simulado) | HTTP (`apikey`) |
| N8N             | Evolution API   | `http://evolution-api:8080`     | HTTP (`apikey`) |

> **Nota:** Containers acessam o host via `172.17.0.1` (gateway padrão da bridge `docker0`). Serviços no host acessam containers via `localhost:<porta-exposta>`.

---

## 2. Modelos Ollama

| Modelo                       | Tamanho aprox. | Função                                          |
|------------------------------|----------------|-------------------------------------------------|
| `llama3.1:8b`                | ~5 GB          | Raciocínio principal, uso de tools, síntese     |
| `nomic-embed-text-v2-moe:latest` | ~1 GB      | Geração de embeddings para RAG no WebUI         |
| `gemma3:12b`                 | ~8 GB          | Disponível como alternativa para tarefas pesadas|

> **Nota (outubro/2026):** a máquina atual tem `gemma4:12b` / `gemma4:e4b` (chat) e `bge-m3` (embeddings) — configure `MODEL_CHAT`/`MODEL_EMBED` no `.env` conforme o `ollama list`. Modelos com *thinking* (gemma4, qwen3) exigem `OLLAMA_THINK=false` no `.env`, senão gastam os tokens pensando e devolvem resposta vazia.

Baixar os modelos:

```bash
ollama pull llama3.1:8b
ollama pull nomic-embed-text-v2-moe:latest
ollama pull gemma3:12b
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
│   ├── open-webui/                 # Bind mount do Open WebUI (usuários, histórico, RAG)
│   ├── chromadb/                   # Dados do ChromaDB
│   ├── n8n/                        # Workflows e credenciais do N8N
│   └── evolution/                  # Sessão do WhatsApp (instances/) e PostgreSQL da Evolution API
│
├── docs/
│   ├── Alfred.png                  # Avatar/ícone do Alfred
│   ├── Alfred-Pennyworth-v2-Guia-Reconstrucao.md  # Guia de reconstrução do projeto
│   └── alfred-system-prompt.md    # System prompt completo do assistente
│
├── logs/                           # Logs e timestamps de sync (NÃO versionado)
│
├── services/
│   ├── researcher/
│   │   ├── alfred-researcher.service  # Unit file do systemd
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
│   │   ├── inbox-assistant.py         # CLI (ciclo, scan, jornal, pendencias, responder, responder-texto, whatsapp, ouvir)
│   │   ├── main.py                    # FastAPI :7072 — fachada HTTP para o N8N e webhook da Evolution
│   │   ├── painel.py                  # Painel do Alfred (/painel, só 127.0.0.1): telas e ações de aprovação
│   │   ├── painel_web/                # Templates Jinja2 + painel.css / painel.js do painel (sem build)
│   │   ├── n8n/                       # Workflows importáveis (ciclo, aguardar resposta, whatsapp)
│   │   ├── tests/                     # test_whatsapp, test_painel, test_trava… + payloads da Evolution (texto, áudio .ogg, resposta citada…)
│   │   ├── alfred-{inbox,semanal,vigia}.{service,timer}, alfred-inbox-api.service  # units de usuário
│   │   └── about.md
│   │
│   ├── scripts/
│   │   ├── alfred-switch.sh       # Liga/desliga "modo jogo" (atalho na área de trabalho)
│   │   ├── chromadb-indexer.py    # Indexação de .md no ChromaDB (placeholder)
│   │   ├── sync-knowledge.py      # Sync incremental vault → Knowledge Base do WebUI
│   │   └── webui-indexer.py       # Indexação de .md nas memories do WebUI (placeholder)
│   │
│   └── shell-executor/
│       ├── alfred-executor.service    # Unit file do systemd
│       ├── main.py                    # FastAPI — executor de comandos com whitelist
│       ├── requirements.txt           # fastapi, uvicorn
│       ├── run.sh                     # Script de inicialização manual
│       └── venv/                      # Ambiente virtual Python (NÃO versionado)
│
├── tools/                          # Tools do Open WebUI (importar manualmente)
│   ├── alfred_researcher.py        # Aciona o Research Service
│   ├── alfred_shell_executor.py    # Executa comandos via Shell Executor
│   ├── alfred_system_monitor.py    # Relatório de sistema (CPU/RAM/GPU/Docker)
│   ├── alfred_pendencias.py        # Fila "vou / não vou" (e-mail) + "o que você quer responder?" (WhatsApp) + jornal do dia
│   ├── alfred_vault_reader.py      # Leitura do vault do Pedro (somente leitura)
│   └── alfred_vault_writer.py      # Escrita e busca no vault do Alfred
│
└── vaults/                         # Vaults Obsidian (NÃO versionados)
    ├── alfred/                     # Vault do Alfred — memória persistente (leitura/escrita)
    └── pedro/                      # Vault do Pedro — base de conhecimento (somente leitura)
```

---

## 4. Setup

### Pré-requisitos

- Docker e Docker Compose (v2)
- [Ollama](https://ollama.com) instalado e rodando no host
- Python 3.12+
- GPU NVIDIA com driver instalado (para `nvidia-smi` funcionar nas tools)

### Passo a passo

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
WEBUI_SECRET_KEY=<gerar abaixo>
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

Use saídas diferentes para `WEBUI_SECRET_KEY`, `N8N_ENCRYPTION_KEY` e `SHELL_EXECUTOR_TOKEN`.

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

# Instalar e ativar o serviço systemd
sudo cp alfred-executor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable alfred-executor
sudo systemctl start alfred-executor

# Confirmar que subiu
sleep 4
curl -s http://localhost:7070/health
```

**6. Instalar o Researcher**

```bash
cd services/researcher

python3 -m venv venv
venv/bin/pip install -r requirements.txt

sudo cp alfred-researcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable alfred-researcher
sudo systemctl start alfred-researcher

sleep 4
curl -s http://localhost:7071/health
```

**7. Configurar o Open WebUI**

1. Acesse `http://localhost:3000`
2. Crie a conta de administrador no primeiro acesso
3. Vá em **Workspace → Tools → +** e importe cada arquivo de `tools/` (ver [seção 5](#5-tools-do-open-webui))
4. Configure as Valves de cada tool com os tokens corretos (ver [seção 5](#5-tools-do-open-webui))
5. Em **Settings → Connections**, confirme que o Ollama está apontando para `http://172.17.0.1:11434`
6. Configure o RAG para usar `nomic-embed-text-v2-moe:latest` como modelo de embedding
7. Crie dois Knowledge Bases: um para o vault do Alfred e um para o vault do Pedro
8. Crie o modelo **Alfred** (em **Workspace → Models**), selecione `llama3.1:8b`, ative todas as tools e cole o system prompt de `docs/alfred-system-prompt.md`

**8. Gerar o WEBUI_API_TOKEN**

No Open WebUI, vá em **Settings → Account → API Keys** e gere uma nova chave. Adicione ao `.env`:

```ini
WEBUI_API_TOKEN=sk-...
RESEARCHER_KB_ALFRED_ID=<ID do Knowledge Base do Alfred>
RESEARCHER_KB_PEDRO_ID=<ID do Knowledge Base do Pedro>
```

Reinicie o Researcher para carregar as novas variáveis:

```bash
sudo systemctl restart alfred-researcher
```

---

## 5. Tools do Open WebUI

### Tabela de Tools

| Tool                      | Arquivo                      | Função                                                      | Valves necessárias                          |
|---------------------------|------------------------------|-------------------------------------------------------------|---------------------------------------------|
| Alfred Shell Executor     | `alfred_shell_executor.py`   | Executa comandos no host via API REST (whitelist restrita)  | `executor_url`, `token`                     |
| Alfred System Monitor     | `alfred_system_monitor.py`   | Relatório de sistema em uma chamada: CPU, RAM, disco, GPU   | `executor_url`, `token`                     |
| Alfred Vault Reader       | `alfred_vault_reader.py`     | Busca e leitura do vault do Pedro (somente leitura)         | `vault_path` (padrão: `/vaults/pedro`)      |
| Alfred Vault Writer       | `alfred_vault_writer.py`     | Salva notas e pesquisas no vault do Alfred                  | `vault_path` (padrão: `/vaults/alfred`)     |
| Alfred Researcher         | `alfred_researcher.py`       | Aciona o Research Service: busca → síntese → vault → sync  | `researcher_url`, `token`                   |
| Alfred Pendências e Jornal | `alfred_pendencias.py`      | Lista/responde convites e contatos aguardando "vou / não vou"; devolve a edição do dia do jornal | `vault_path` (padrão: `/vaults/alfred`) |

### Como importar

1. No Open WebUI, acesse **Workspace → Tools**
2. Clique em **+** (novo tool)
3. Cole o conteúdo do arquivo `.py` correspondente
4. Salve
5. Repita para cada tool

> **Atualização de 25/10/2026:** `alfred_pendencias.py` (v1.2.0) e `alfred_tasks.py` (v1.1.0) passaram a usar a mesma trava entre processos do Inbox Assistant — reimporte as duas (cole o conteúdo novo por cima). Enquanto a versão antiga estiver instalada, o que o chat grava continua sem trava. Ver [gravação concorrente](services/inbox/about.md#gravação-concorrente-trava-entre-processos).

### Configurar os tokens nas Valves

Após importar, clique no ícone de engrenagem de cada tool e preencha:

- **`token`**: valor de `SHELL_EXECUTOR_TOKEN` do `.env`
- **`executor_url`**: `http://172.17.0.1:7070`
- **`researcher_url`**: `http://172.17.0.1:7071`

> **Importante:** Sempre configure o token pela interface do browser (Valves), nunca diretamente no código-fonte da tool. O valor padrão no código é uma string vazia por segurança.

---

## 6. System Prompt do Alfred

O arquivo completo está em [`docs/alfred-system-prompt.md`](docs/alfred-system-prompt.md).

### Persona

Alfred Pennyworth — mordomo pessoal de Pedro Netto. Responde sempre em português brasileiro. Tom formal o suficiente para ter dignidade, direto o suficiente para ser útil. Humor seco britânico permitido; sycophancy proibida.

### Ordem de decisão das tools

O Alfred segue estritamente esta hierarquia antes de qualquer resposta que envolva informação externa:

1. **Vault próprio** (`alfred_vault_writer` → `search_vault`) — verifica se já pesquisou antes
2. **Vault do Pedro** (`alfred_vault_reader` → `search_vault`) — apenas para questões pessoais/projetos do Pedro
3. **Shell Executor** (`alfred_shell_executor` → `execute_command`) — para qualquer estado observável na máquina
4. **Pesquisa web** (`alfred_researcher` → `research_topic`) — último recurso; sintetiza e salva no vault

### Regras do vault

O Alfred usa seu vault como memória persistente entre sessões:

- **`research/`** — pesquisas web realizadas (3 arquivos: `index.md`, `synthesis.md`, `sources.md`)
- **`decisions/`** — decisões técnicas e raciocínio por trás delas
- **`logs/`** — erros diagnosticados e solucionados; resumos de conversas importantes

Toda nota salva inclui frontmatter com `title`, `date`, `tags` e `topics` para facilitar buscas futuras.

---

## 7. Serviços systemd

| Serviço               | Arquivo unit                                    | WorkingDirectory                                      | Porta |
|-----------------------|-------------------------------------------------|-------------------------------------------------------|-------|
| alfred-executor       | `services/shell-executor/alfred-executor.service` | `/home/netto/Alfred-Pennyworth/services/shell-executor` | 7070  |
| alfred-researcher     | `services/researcher/alfred-researcher.service`  | `/home/netto/Alfred-Pennyworth/services/researcher`   | 7071  |

Ambos os serviços carregam variáveis diretamente de `/home/netto/Alfred-Pennyworth/.env` via `EnvironmentFile`.

### Comandos de gerenciamento

```bash
# Habilitar e iniciar
sudo systemctl enable alfred-executor alfred-researcher
sudo systemctl start alfred-executor alfred-researcher

# Verificar status
sudo systemctl status alfred-executor
sudo systemctl status alfred-researcher

# Reiniciar (necessário após alterar o .env)
sudo systemctl restart alfred-executor
sudo systemctl restart alfred-researcher

# Ver logs em tempo real
sudo journalctl -fu alfred-executor
sudo journalctl -fu alfred-researcher
```

---

## 7b. Inbox Assistant — Thunderbird → jornal + pendências

O Alfred lê os e-mails que o **Thunderbird** já baixou (cópias offline do perfil — sem senha, sem rede, funciona com Outlook/Gmail via OAuth2), classifica cada um com o Ollama e faz duas coisas:

1. **Edição do dia do jornal** — as newsletters viram itens de notícia (título, resumo, URL só se existir no e-mail) e passam pelo **mesmo pipeline do briefing** (`lib_cluster` → template `jornal_email.md` → Ollama → título → `.md` + `.mp3` com edge-tts) em `VAULT_NOTICIAS/jornal/`.
   - **Cofre de notícias:** `VAULT_NOTICIAS` (padrão desta máquina: `VAULT_ALFRED/Noticias`, com `.obsidian` próprio — abra como vault separado no Obsidian). Fica dentro do vault do Alfred para o Open WebUI, que só monta `/vaults/alfred`, continuar lendo o jornal (valve `noticias_path` da tool). Vazio = layout antigo (`VAULT_ALFRED/jornal`), que continua sendo lido como fallback.
   - **Podcast a cada 3 dias** (`run.sh podcast`, `POST /semanal`; timer diário com `--se-devido`): relê as edições do jornal e os briefings por RSS dos últimos 3 dias, escolhe os temas que mais se repetiram em mais newsletters e gera um episódio de ~10 min em **diálogo de duas vozes** (Alfred + Lúcia, edge-tts + ffmpeg) em `VAULT_NOTICIAS/podcast/episodios/`. Também grava um manifesto `podcast/manifestos/<período>.json` com os e-mails usados, que serve para a limpeza do Thunderbird. Detalhes em [services/inbox/about.md](services/inbox/about.md#podcast--resumo-do-alfred-a-cada-3-dias-semanalpy).
2. **Pendências "vou / não vou"** — convites a eventos e contatos profissionais (recrutador, proposta, retorno de processo seletivo) geram um texto explicativo + um recado de voz em que Alfred pergunta se Pedro vai. A pergunta fica **aguardando** em `VAULT_ALFRED/pendencias/pendencias.yml` até a resposta, que pode vir por uma pergunta gráfica que fica ociosa esperando o clique (notificação com botões via D-Bus → kdialog → zenity), pelo terminal (`perguntar` ou `responder`), pelo chat (tool `alfred_pendencias`) ou pelo N8N. "Vou" cria uma tarefa em `tasks.yml`.

```bash
cd services/inbox
./run.sh scan            # classifica e mostra — nada gravado
./run.sh                 # ciclo completo
./run.sh pendencias      # fila aguardando resposta
./run.sh perguntar       # Alfred pergunta uma a uma no terminal e espera vou/nao
./run.sh responder <id> vou
./run.sh serve           # API HTTP :7072 para o N8N + Painel do Alfred em /painel
```

**Painel do Alfred** — com a API no ar, `http://127.0.0.1:7072/painel` (só desta máquina; login com o `SHELL_EXECUTOR_TOKEN`) mostra as pendências, o rascunho que o Alfred propõe para cada resposta (**Autorizar envio** / **Não enviar** / **Agendar**; **Vou** / **Não vou** no e-mail), a agenda (**Desfazer**), os lotes de limpeza (**Aprovar** / **Recusar**) e o estado do sistema, atualizando a cada 15 s. Dependência nova `jinja2`: num venv existente rode `venv/bin/pip install -r requirements.txt` antes de reiniciar a API (sem ele a API sobe sem o painel e o log avisa). Detalhes: [about.md](services/inbox/about.md#painel-do-alfred).

Configuração em `services/_shared/perfis.yml` (seção `inbox:`) e no `.env` (`THUNDERBIRD_PROFILE`, `INBOX_DEFAULT`, `OLLAMA_THINK=false` para gemma4/qwen3). Agendamento com units de **usuário** (`systemctl --user`), porque notificação e áudio precisam da sessão gráfica. Detalhes, API e workflows do N8N: [`services/inbox/about.md`](services/inbox/about.md).

**Divisão de papéis com o N8N:** o Python é o motor (ler mbox, clustering, TTS, templates) exposto como API; o N8N orquestra (cron, `Wait` aguardando o "vou/não vou" via `resumeUrl`, e o WhatsApp via Evolution API). Os workflows em `services/inbox/n8n/` são o ponto de partida.

---

## 7c. WhatsApp — Evolution API → pendências "o que você quer responder?"

Mensagens e **áudios** recebidos no WhatsApp passam pelo mesmo tratamento do e-mail. A Evolution API (container `alfred-evolution`, número pareado por QR code) entrega cada mensagem por webhook ao Inbox Assistant, que:

1. **transcreve o áudio localmente** — ffmpeg converte o OGG/Opus do WhatsApp em WAV e o **gemma4** transcreve via Ollama (o áudio vai em base64 no campo `images` do `/api/generate`; fatias de 30 s); fallback `faster-whisper` se instalado;
2. **classifica** com o mesmo padrão do e-mail (`whatsapp_classificar.md`, JSON Schema): `profissional` / `pessoal` / `grupo` / `automatico` / `spam` + "requer resposta?" + urgência;
3. **espera ~20 min** (`atraso_minutos` em `perfis.yml → whatsapp:`): se Pedro responder a conversa direto pelo celular nesse tempo, nada é notificado nem enviado — o Alfred só registra um item `respondida_manual` com a mensagem do contato, o rascunho que teria proposto e a resposta de Pedro, lado a lado na aba **Resolvidas** do Painel; mensagens seguidas do contato viram uma pendência só;
4. para contatos relevantes cria uma **pendência `whatsapp`** em `VAULT_ALFRED/pendencias/pendencias.yml` (mesmo esquema, `status: aguardando`, campo `resposta_texto`) com um texto explicativo (`pergunta_whatsapp.md`) e um recado de voz (`audio_pergunta_whatsapp.md` → edge-tts) em que Alfred resume a mensagem e pergunta **"o que você quer responder?"**;
5. avisa Pedro no WhatsApp (resumo com `ref: <id>` + áudio) — pelo N8N (`services/inbox/n8n/alfred-whatsapp.json`) ou pelo próprio Python (`/whatsapp/webhook-async`) — e **no PC**: notificação com a sugestão de resposta escrita pelo Alfred e os botões **Enviar** (abre o rascunho para editar e confirmar) / **Não enviar** / **Agenda** (tarefa em `tasks.yml` + lembrete no PC + evento no Google Agenda já preenchido) / **Ouvir** — o resumo em áudio começa a tocar junto com a notificação (botão **Parar**) e o `.mp3` local é apagado 2 dias depois (a cópia fica no WhatsApp);
6. **a resposta de Pedro é enviada ao contato**: ele responde *citando* a mensagem do Alfred (ou pelo botão Enviar no PC, **Autorizar envio** no Painel, `./run.sh responder-texto <id> "texto"`, o chat: "responde pro Carlos que…", ou `POST /pendencias/{id}/responder-texto`). `ignorar` fecha sem enviar; se Pedro responder o contato direto no celular, a pendência fecha sozinha (guardando o rascunho do Alfred e a resposta real).

```bash
docker compose up -d evolution-postgres evolution-api       # .env: EVOLUTION_API_KEY, EVOLUTION_POSTGRES_PASSWORD, WHATSAPP_NUMERO_PEDRO
cd services/inbox
./run.sh whatsapp qrcode                                   # cria a instância e abre o QR (WhatsApp → Aparelhos conectados)
./run.sh serve                                             # API :7072
./run.sh whatsapp webhook http://172.17.0.1:7072/whatsapp/webhook-async   # ou a URL do Webhook do N8N
./run.sh whatsapp status                                   # instância "open", fila
./run.sh responder-texto <id> "Carlos, mando até quinta."  # resposta → WhatsApp do contato
venv/bin/python -m unittest tests.test_whatsapp -v         # 63 testes offline (inclui o formato real da Evolution v2.3.7)
```

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
| para os timers (briefing 07:00, e-mail 07:30/18:30, podcast 19:00, vigia) e qualquer execução em andamento | `docker start` banco + Evolution |
| para a API do Alfred (:7072) — WhatsApp deixa de ser processado e o Painel não abre | sobe a API |
| `docker stop` da Evolution API (WhatsApp) e do banco dela | religa os timers (o vigia confere tudo em seguida) |
| fecha as notificações que esperam clique (as pendências continuam aguardando) | reabre essas notificações |
| descarrega os modelos do Ollama da VRAM (GPU livre para jogar) | — |

O estado sobrevive a reinicializações: `logs/alfred_desligado` faz os `run.sh` recusarem qualquer execução vinda
das units do systemd (elas exportam `ALFRED_AUTOMACAO=1`), e a Evolution parada com `docker stop` não volta sozinha
(`restart: unless-stopped`). Comandos manuais no terminal continuam funcionando (`ALFRED_FORCAR=1` libera até
`serve`/`vigia`). Não existe WAHA nesta máquina: a ponte do WhatsApp é a Evolution API.

---

## 8. Scripts Utilitários

Localização: `services/scripts/`

| Script                 | Função                                                                          | Quando usar                                           | Exemplo de uso                                      |
|------------------------|---------------------------------------------------------------------------------|-------------------------------------------------------|-----------------------------------------------------|
| `sync-knowledge.py`    | Sync incremental do vault → Knowledge Base do Open WebUI. Só re-indexa arquivos modificados desde o último sync (timestamp em `logs/alfred_last_sync`). | Após editar notas manualmente no Obsidian, ou para forçar re-indexação. Também é disparado automaticamente pelo Researcher após cada pesquisa. | `python3 services/scripts/sync-knowledge.py` |
| `webui-indexer.py`     | Indexa arquivos `.md` dos vaults nas *memories* do Open WebUI (não na Knowledge Base). | Quando quiser popular as memories globais do WebUI com o conteúdo dos vaults. | Implementação pendente — ver placeholder. |
| `chromadb-indexer.py`  | Indexa arquivos `.md` no ChromaDB com embeddings via `nomic-embed-text`. Permite buscas vetoriais diretas fora do WebUI. | Quando precisar de RAG externo ao Open WebUI (ex.: workflows N8N consultando ChromaDB diretamente). | Implementação pendente — ver placeholder. |

### Diferença entre os três scripts

| Script               | Destino               | Tipo de busca       | Quem consume                    |
|----------------------|-----------------------|---------------------|---------------------------------|
| `sync-knowledge.py`  | Knowledge Base WebUI  | Full-text + RAG     | Open WebUI (modelo Alfred)      |
| `webui-indexer.py`   | Memories WebUI        | Contexto de sessão  | Open WebUI (contexto automático)|
| `chromadb-indexer.py`| ChromaDB              | Busca vetorial      | N8N, scripts externos           |

### Variáveis de ambiente necessárias para `sync-knowledge.py`

```bash
WEBUI_API_TOKEN=<token gerado no WebUI>
WEBUI_URL=http://localhost:3000
RESEARCHER_KB_ALFRED_ID=<ID da KB do Alfred no WebUI>
RESEARCHER_KB_PEDRO_ID=<ID da KB do Pedro no WebUI>
VAULT_ALFRED=/caminho/para/vault/alfred
VAULT_PEDRO=/caminho/para/vault/pedro
```

---

## 9. Referência Rápida

```bash
# ── Docker ────────────────────────────────────────────────────
docker compose up -d                          # Subir todos os containers
docker compose down                           # Parar todos os containers
docker compose ps                             # Ver status
docker compose logs -f open-webui             # Logs do WebUI
docker compose logs -f alfred-searxng         # Logs do SearXNG

# ── systemd ───────────────────────────────────────────────────
sudo systemctl status alfred-executor         # Status do Shell Executor
sudo systemctl status alfred-researcher       # Status do Researcher
sudo systemctl restart alfred-executor        # Reiniciar após alterar .env
sudo journalctl -fu alfred-researcher         # Logs em tempo real

# ── Health checks ─────────────────────────────────────────────
curl -s http://localhost:7070/health          # Shell Executor
curl -s http://localhost:7071/health          # Researcher
curl -s http://localhost:3000/health          # Open WebUI
curl -s http://localhost:8888/search?q=test&format=json | head -c 200  # SearXNG

# ── Ollama ────────────────────────────────────────────────────
ollama list                                   # Modelos instalados
ollama ps                                     # Modelos em execução
ollama pull llama3.1:8b                       # Baixar modelo
curl -s http://localhost:11434/api/tags       # API REST do Ollama

# ── GPU ───────────────────────────────────────────────────────
nvidia-smi                                    # Status completo da GPU
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader

# ── Sincronizar vault com o WebUI ─────────────────────────────
cd ~/Alfred-Pennyworth
source .env
python3 services/scripts/sync-knowledge.py
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

### Token inválido nas tools (erro 401)

**Causa:** O valor do token nas Valves está incorreto ou vazio.  
**Solução:** No Open WebUI, acesse **Workspace → Tools**, clique na engrenagem da tool afetada e preencha o campo `token` com o valor de `SHELL_EXECUTOR_TOKEN` do `.env`. Configure pelo browser — nunca edite o código da tool diretamente.

---

### Open WebUI não exibe a tela de cadastro (pula direto para login)

**Causa:** O diretório `data/open-webui/` já existe com dados de uma instalação anterior, ou foi criado pelo Docker antes da configuração correta.  
**Solução:**

```bash
docker compose down
rm -rf data/open-webui
docker compose up -d
```

> O dado em `data/open-webui/` é um bind mount, não um volume Docker nomeado. Isso significa que `docker volume prune` não remove — é necessário deletar o diretório no host.

---

### Researcher exibe modelo desatualizado no health check

**Causa:** O serviço systemd carregou o `.env` na inicialização e não relê após mudanças.  
**Solução:**

```bash
sudo systemctl restart alfred-researcher
curl -s http://localhost:7071/health
```

---

### `curl http://localhost:7070/health` retorna "Connection refused" logo após `systemctl start`

**Causa:** O uvicorn leva alguns segundos para completar a inicialização.  
**Solução:** Aguarde 3–5 segundos após o start antes de testar. Para scripts de automação, use um loop com retry:

```bash
for i in $(seq 1 10); do
    curl -sf http://localhost:7070/health && break
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

### Poder 2 — Scanner de Licitações

Scraper de portais públicos de licitação (ComprasNet, Portal de Compras SP) que indexa editais no ChromaDB e identifica licitações com perfil compatível com critérios configuráveis via RAG.

**Pipeline:** N8N (cron diário) → Web scraping (portais públicos) → Ollama (geração de embeddings) → ChromaDB (upsert + query por similaridade) → Relatório de oportunidades no vault

**Status:** 🔲 Planejado

---

### Poder 3 — WhatsApp + Agendamento

Integração com WhatsApp via Evolution API: mensagens e áudios recebidos (transcrição local com gemma4) passam pelo mesmo tratamento do e-mail e viram uma pendência em que Alfred resume a mensagem e pergunta "o que você quer responder?" (texto + áudio); a resposta de Pedro é enviada de volta pelo WhatsApp.

**Pipeline:** Evolution API (Docker) → webhook → Inbox Assistant (`whatsapp.py`: transcrever → classificar → pendência + áudio) → N8N ou Python avisa Pedro → resposta citada / CLI / chat / API / Painel → Evolution API `sendText`

**Status:** ✅ Implementado e **validado com WhatsApp real em 2026-10-19** (instância no próprio número de Pedro, Evolution API "v2.3.7", sem N8N): texto e áudio viram pendência, resumo + áudio chegam a Pedro, a resposta citada é entregue ao contato, `ignorar` e resposta direta pelo celular fecham a pendência, e a notificação no PC (Enviar / Não enviar / Agenda / Ouvir) envia o rascunho. Depois da validação: **espera de ~20 min antes de resumir** (`atraso_minutos: 20` em `perfis.yml → whatsapp:`) — resumo, áudio e avisos só saem se Pedro não responder a conversa nesse tempo; se ele responder direto pelo celular antes, nada é notificado nem enviado e fica só o registro com o rascunho que o Alfred proporia e a resposta de Pedro (testado offline, ainda não com WhatsApp real — ver [espera](services/inbox/about.md#espera-antes-de-resumir-atraso_minutos)). Falta validar o workflow N8N (stack Docker fora do ar nesta máquina) — ver [validação](services/inbox/about.md#validação-com-whatsapp-real-2026-10-19).

**Agenda automática (20/10/2026):** evento, tarefa ou lembrete com data detectado numa mensagem de contato, no que Pedro escreve para si mesmo na conversa "Você" ("me lembra de pagar o boleto sexta") ou num "vou" de convite por e-mail vai para `VAULT_ALFRED/agenda/agenda.yml` e, pela extensão **Alfred — agenda** do Thunderbird, para o calendário "Agenda" (Google via CalDAV → celular), com notificação **[Ok] [Desfazer]**. Extensão validada num perfil de teste do Thunderbird 157; falta instalar no perfil real — ver [agenda](services/inbox/about.md#agenda--evento--tarefa--lembrete--calendário-do-thunderbird-agendapy).

**Vigia (20/10/2026):** a conexão caiu em 19/10 às 19:10 (`device_removed`: o WhatsApp desvinculou o aparelho). O `alfred-vigia.timer` (no login e a cada 10 min) reinicia API/containers parados e, se o WhatsApp cair, mostra uma notificação com **[Mostrar QR code]** para parear de novo.

**Próximos passos:**
- **Comandos para o Alfred pelo WhatsApp** — hoje tudo o que Pedro escreve citando o resumo vai para o contato: na validação real, "Pode agendar para terça-feira às 18:00 (coloque na minha agenda também)" foi entregue inteiro à Attano Tech. Separar instrução para o Alfred do texto ao contato (ex.: trecho marcado `[Alfred: põe na agenda]` é executado e removido antes do envio) e aceitar outros comandos diretos ao Alfred na conversa "Você" além de agenda/lembrete (já implementado), ex.: pesquisa, tarefa sem data.
- Usar os nós "Evolution: texto/áudio para Pedro" do `alfred-whatsapp.json` também no sub-workflow "aguardar resposta" do e-mail (links vou/não vou pelo WhatsApp).

---

### Melhorias de Infraestrutura Planejadas

| Componente         | Função                                              | Status          |
|--------------------|-----------------------------------------------------|-----------------|
| Cloudflare Tunnel  | Acesso externo seguro sem expor portas no roteador  | 🔲 Planejado    |
| Redis              | Filas e cache para workflows N8N de alta frequência | 🔲 Planejado    |
| Grafana            | Monitoramento de uso de GPU, RAM e saúde dos serviços | 🔲 Planejado  |
| Traefik            | Reverse proxy com SSL automático para os containers | 🔲 Planejado    |