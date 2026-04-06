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

> **Nota:** Containers acessam o host via `172.17.0.1` (gateway padrão da bridge `docker0`). Serviços no host acessam containers via `localhost:<porta-exposta>`.

---

## 2. Modelos Ollama

| Modelo                       | Tamanho aprox. | Função                                          |
|------------------------------|----------------|-------------------------------------------------|
| `llama3.1:8b`                | ~5 GB          | Raciocínio principal, uso de tools, síntese     |
| `nomic-embed-text-v2-moe:latest` | ~1 GB      | Geração de embeddings para RAG no WebUI         |
| `gemma3:12b`                 | ~8 GB          | Disponível como alternativa para tarefas pesadas|

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
│   └── n8n/                        # Workflows e credenciais do N8N
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
│   ├── scripts/
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

### Como importar

1. No Open WebUI, acesse **Workspace → Tools**
2. Clique em **+** (novo tool)
3. Cole o conteúdo do arquivo `.py` correspondente
4. Salve
5. Repita para cada tool

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

Workflow N8N que escaneia notícias de tecnologia a cada 6 horas, sintetiza os destaques com Ollama e salva um relatório diário no vault do Alfred.

**Pipeline:** N8N (cron a cada 6h) → SearXNG (busca por categoria `news`) → Ollama (síntese em PT-BR) → Vault Alfred (`research/noticias/YYYY-MM-DD.md`)

**Status:** 🔲 Planejado

---

### Poder 2 — Scanner de Licitações

Scraper de portais públicos de licitação (ComprasNet, Portal de Compras SP) que indexa editais no ChromaDB e identifica licitações com perfil compatível com critérios configuráveis via RAG.

**Pipeline:** N8N (cron diário) → Web scraping (portais públicos) → Ollama (geração de embeddings) → ChromaDB (upsert + query por similaridade) → Relatório de oportunidades no vault

**Status:** 🔲 Planejado

---

### Poder 3 — WhatsApp + Agendamento

Integração com WhatsApp via Evolution API para leitura de mensagens recebidas, extração de compromissos com LLM e criação automática de eventos no Google Calendar ou notas no Obsidian.

**Pipeline:** Evolution API (Docker) → N8N (webhook) → Ollama (extração de entidades: data, hora, compromisso) → Google Calendar API / Obsidian (criação de evento ou nota)

**Status:** 🔲 Planejado

---

### Melhorias de Infraestrutura Planejadas

| Componente         | Função                                              | Status          |
|--------------------|-----------------------------------------------------|-----------------|
| Cloudflare Tunnel  | Acesso externo seguro sem expor portas no roteador  | 🔲 Planejado    |
| Redis              | Filas e cache para workflows N8N de alta frequência | 🔲 Planejado    |
| Grafana            | Monitoramento de uso de GPU, RAM e saúde dos serviços | 🔲 Planejado  |
| Traefik            | Reverse proxy com SSL automático para os containers | 🔲 Planejado    |