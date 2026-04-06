# Alfred Pennyworth v2 — Guia de Reconstrução Completa

**Data:** 2026-04-05
**Autor:** Pedro Henrique Netto dos Santos
**Assistido por:** Claude (Anthropic)
**Ambiente:** Kubuntu 25.10 · AMD Ryzen 5 4500 · RTX 3060 12GB · 32GB RAM

---

## Contexto

O projeto Alfred Pennyworth é um assistente pessoal com IA rodando 100% localmente. Uma auditoria de estrutura (2026-03-18) identificou diversos problemas — tokens hardcoded, scripts duplicados, diretórios inúteis, IDs hardcoded, falhas de segurança na whitelist — que motivaram a decisão de **recriar o projeto do zero**, aplicando todas as correções.

Este guia documenta cada passo executado, os erros encontrados e como foram resolvidos.

---

## Pré-requisitos

Antes de começar, tenha instalado:

- Docker + Docker Compose
- Ollama (https://ollama.com)
- Python 3.12+
- GPU NVIDIA com drivers + CUDA
- openssl (para gerar segredos)

---

## Fase 1 — Estrutura de Diretórios

### O que foi feito

Criação da árvore limpa, sem os diretórios vazios inúteis (`scripts/`, `secrets/`) que existiam antes.

```bash
cd /home/netto/Alfred-Pennyworth

mkdir -p config/searxng \
         data/{chromadb,n8n,open-webui} \
         docs \
         logs \
         services/{shell-executor,researcher,scripts} \
         tools \
         vaults

touch logs/.gitkeep
```

### Vaults via Symlink

Em vez de copiar os vaults para dentro do projeto, usamos symlinks apontando para os caminhos reais. O `docker-compose.yml` resolve symlinks normalmente.

```bash
# Criar o vault do Alfred com as pastas padrão
mkdir -p "/home/netto/Documentos/Obsidian - Alfred"/{research,decisions,logs}

# Criar symlinks
ln -s "/home/netto/Documentos/Obsidian - MrNotte" vaults/pedro
ln -s "/home/netto/Documentos/Obsidian - Alfred" vaults/alfred
```

### Limpeza de resquícios

Havia arquivos do projeto antigo na raiz que precisaram ser movidos/removidos:

```bash
mv "Alfred - system prompt.md" docs/alfred-system-prompt.md
mv Alfred.png docs/
rm README.md   # será reescrito na Fase 8
```

### Estrutura final

```
Alfred-Pennyworth/
├── .env / .env.example / .gitignore
├── docker-compose.yml
├── setup.sh
├── config/searxng/settings.yml
├── data/{chromadb,n8n,open-webui}/
├── docs/
│   ├── alfred-system-prompt.md
│   └── alfred-research-addon.md
├── logs/.gitkeep
├── services/
│   ├── shell-executor/   (main.py, requirements, run.sh, .service)
│   ├── researcher/       (main.py, requirements, run.sh, .service)
│   └── scripts/          (webui-indexer, chromadb-indexer, sync-knowledge, etc.)
├── tools/                (5 tools para o Open WebUI)
└── vaults/
    ├── alfred → symlink
    └── pedro  → symlink
```

### Correções aplicadas

- `scripts/` e `secrets/` (vazios) → removidos
- `docs/tool-researcher.py` (orphan) → removido
- `Alfred - system prompt.md` da raiz → movido para `docs/`
- `indexer.py` → renomeado para `webui-indexer.py`
- `vault-indexer.py` → renomeado para `chromadb-indexer.py`

---

## Fase 2 — Variáveis de Ambiente

### O que foi feito

Criação do `.env.example` com TODAS as variáveis (incluindo as que antes eram hardcoded) e geração do `.env` real com segredos criptográficos.

```bash
# Detectar IP da bridge Docker
HOST_IP=$(docker network inspect bridge \
  --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || echo '172.17.0.1')

# Gerar segredos
WEBUI_SECRET=$(openssl rand -hex 32)
N8N_ENCRYPT=$(openssl rand -hex 32)
N8N_PASS=$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)
EXECUTOR_TOKEN=$(openssl rand -hex 32)
```

### Variáveis completas

```env
# Rede
HOST_IP=172.17.0.1
OLLAMA_BASE_URL=http://172.17.0.1:11434

# Portas
PORT_WEBUI=3000
PORT_SEARXNG=8888
PORT_N8N=5678
PORT_CHROMADB=8000
PORT_SHELL_EXECUTOR=7070
PORT_RESEARCHER=7071

# Modelos
MODEL_CHAT=llama3.1:8b
MODEL_EMBED=nomic-embed-text-v2-moe:latest

# Vaults
VAULT_PEDRO=/home/netto/Documentos/Obsidian - MrNotte
VAULT_ALFRED=/home/netto/Documentos/Obsidian - Alfred

# Open WebUI
WEBUI_SECRET_KEY=(gerado)
WEBUI_AUTH=true
WEBUI_URL=http://localhost:3000
WEBUI_API_TOKEN=(preenchido depois)

# SearXNG / ChromaDB
SEARXNG_URL=http://localhost:8888
CHROMADB_URL=http://localhost:8000
CHROMADB_COLLECTION=alfred-brain

# N8N
N8N_BASIC_AUTH_USER=netto
N8N_BASIC_AUTH_PASSWORD=(gerado)
N8N_ENCRYPTION_KEY=(gerado)

# Serviços
SHELL_EXECUTOR_TOKEN=(gerado)
RESEARCHER_KB_ALFRED_ID=(preenchido depois)
RESEARCHER_KB_PEDRO_ID=(preenchido depois)
CHUNK_SIZE=300
```

### Correções aplicadas

- Tokens que antes estavam hardcoded no código → agora vivem exclusivamente no `.env`
- IDs de Knowledge Base que eram UUIDs fixos em `sync_knowledge.py` → agora são `RESEARCHER_KB_ALFRED_ID` e `RESEARCHER_KB_PEDRO_ID`
- `WEBUI_TOKEN` renomeado para `WEBUI_API_TOKEN` para clareza

### Erro encontrado: modelos desatualizados

O `.env` foi gerado com `MODEL_CHAT=qwen3:8b` e `MODEL_EMBED=nomic-embed-text` (valores antigos), mas o Ollama já tinha modelos diferentes instalados.

**Solução:**

```bash
sed -i 's/MODEL_CHAT=qwen3:8b/MODEL_CHAT=llama3.1:8b/' .env .env.example
sed -i 's/MODEL_EMBED=nomic-embed-text/MODEL_EMBED=nomic-embed-text-v2-moe:latest/' .env .env.example
```

---

## Fase 3 — Docker Compose

### O que foi feito

Criação do `docker-compose.yml` com 4 serviços na rede `alfred-net` e do `config/searxng/settings.yml` já com o fix do erro 403.

### Serviços

| Container | Imagem | Porta | Função |
|-----------|--------|-------|--------|
| alfred-webui | ghcr.io/open-webui/open-webui:main | 3000 | Interface de chat |
| alfred-searxng | searxng/searxng:latest | 8888 | Busca web privada |
| alfred-chromadb | chromadb/chroma:latest | 8000 | Banco de vetores (RAG) |
| alfred-n8n | n8nio/n8n:latest | 5678 | Automação de workflows |

### SearXNG — fix do 403

O SearXNG por padrão bloqueia requisições que não vêm de um browser (proteção anti-bot). Isso impedia o Open WebUI de fazer buscas.

**Solução aplicada no `settings.yml`:**

```yaml
server:
  secret_key: "alfred-searxng-local-only"
  limiter: false      # ← desativa a proteção anti-bot
  image_proxy: false

search:
  formats:
    - html
    - json             # ← habilita retorno em JSON (necessário para a API)
```

### Validação

```bash
docker compose up -d
sleep 5
docker compose ps

# Teste do host
curl -s "http://localhost:8888/search?q=test&format=json" | head -c 100

# Teste de dentro do container (como o Alfred usa)
docker exec alfred-webui curl -s "http://searxng:8080/search?q=test&format=json" | head -c 100
```

Ambos devem retornar JSON com `{"query": "test", "results": [...]}`.

---

## Fase 4 — Ollama

### O que foi feito

Nada — o Ollama já estava instalado com os modelos corretos:

| Modelo | Tamanho | Função |
|--------|---------|--------|
| llama3.1:8b | 4.9 GB | Raciocínio principal e tools |
| nomic-embed-text-v2-moe:latest | 957 MB | Embeddings para RAG |
| gemma3:12b | 8.1 GB | Disponível como alternativa |

---

## Fase 5 — Shell Executor

### O que foi feito

Reescrita do `services/shell-executor/main.py` com as correções de segurança:

- Token vem exclusivamente da env var `SHELL_EXECUTOR_TOKEN` (sem default hardcoded)
- `WEBUI_TOKEN=` removido da whitelist do `ALLOWED_PREFIXES` (era um vetor de exposição de credenciais — qualquer chamador autenticado podia extrair o token do WebUI via `env | grep WEBUI_TOKEN`)

### Instalação

```bash
cd services/shell-executor
python3 -m venv venv
venv/bin/pip install -q -r requirements.txt

# Instalar serviço systemd
sudo cp alfred-executor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable alfred-executor
sudo systemctl start alfred-executor
```

### Validação

```bash
systemctl status alfred-executor --no-pager
curl http://localhost:7070/health
# Esperado: {"status":"ok","version":"2.0.0"}
```

---

## Fase 6 — Researcher

### O que foi feito

Reescrita do `services/researcher/main.py` com correções:

- IDs de Knowledge Base via env vars (`RESEARCHER_KB_ALFRED_ID`, `RESEARCHER_KB_PEDRO_ID`)
- Caminho do sync script atualizado para `services/scripts/sync-knowledge.py`
- Token do WebUI usa `WEBUI_API_TOKEN` (não mais `WEBUI_TOKEN`)

### Scripts utilitários consolidados em `services/scripts/`

| Arquivo | Função | Origem |
|---------|--------|--------|
| sync-knowledge.py | Sync incremental vault → KB do WebUI | era `sync_knowledge.py` solto em `services/` |
| webui-indexer.py | Indexa .md nas memories do WebUI | era `indexer.py` (nome genérico) |
| chromadb-indexer.py | Indexa .md no ChromaDB com embeddings | era `vault-indexer.py` (nome ambíguo) |

### Correções no sync-knowledge.py

- IDs de KB: `os.getenv("RESEARCHER_KB_ALFRED_ID")` em vez de UUIDs hardcoded
- Sync state: salva em `PROJECT_ROOT/logs/alfred_last_sync` em vez de `/tmp/alfred_last_sync` (agora persiste entre reboots)

### Instalação

```bash
cd services/researcher
python3 -m venv venv
venv/bin/pip install -q -r requirements.txt

sudo cp alfred-researcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable alfred-researcher
sudo systemctl start alfred-researcher
```

### Erro encontrado: curl recusado após start

O curl do health check foi executado imediatamente após o `systemctl start` (47ms de vida do processo). O uvicorn ainda não tinha subido.

**Solução:**

```bash
sleep 3
curl http://localhost:7071/health
```

### Erro encontrado: modelo antigo no Researcher

Após subir, o `/health` mostrava `"model":"qwen3:8b"` em vez de `"model":"llama3.1:8b"`. O `.env` não tinha sido atualizado pelo `sed` anterior corretamente.

**Solução:**

```bash
sed -i 's/MODEL_CHAT=qwen3:8b/MODEL_CHAT=llama3.1:8b/' .env
sudo systemctl restart alfred-researcher
```

---

## Fase 7 — Tools do Open WebUI

### O que foi feito

Criação de 5 tools para importar no Open WebUI:

| Tool | Arquivo | Correção |
|------|---------|----------|
| Shell Executor | alfred_shell_executor.py | Token default vazio (era hardcoded) |
| System Monitor | alfred_system_monitor.py | Token default vazio (era hardcoded) |
| Vault Reader | alfred_vault_reader.py | Sem alteração |
| Vault Writer | alfred_vault_writer.py | Sem alteração |
| Researcher | alfred_researcher.py | Token default vazio, URL via Valve |

### Importação

Para cada tool: **Workspace → Tools → `+`** → colar o conteúdo do arquivo Python → salvar.

### Configuração das Valves

Após importar, configurar as Valves (ícone ⚙️) das tools que precisam de token:

| Tool | Campo | Valor |
|------|-------|-------|
| Shell Executor | token | `SHELL_EXECUTOR_TOKEN` do `.env` |
| System Monitor | token | mesmo |
| Researcher | token | mesmo |

```bash
grep SHELL_EXECUTOR_TOKEN ~/Alfred-Pennyworth/.env
```

### Erro encontrado: token inválido nas tools

Após importar as tools, o Alfred retornava `[erro]: token inválido` em todos os comandos.

**Causa raiz:** Alterar o `default=` no código-fonte NÃO atualiza o valor salvo no banco do WebUI. O valor das Valves é persistido no SQLite do Open WebUI na primeira importação.

**Solução:** Configurar o token manualmente pelo browser em Workspace → Tools → ⚙️ → campo `token` → colar o valor → Save.

### Erro encontrado: executor com token antigo

Mesmo com o token correto nas Valves, o executor retornava 401. O serviço systemd estava rodando com um `.env` desatualizado em memória.

**Solução:**

```bash
sudo systemctl restart alfred-executor
```

**Validação ponta a ponta:**

```bash
TOKEN=$(grep SHELL_EXECUTOR_TOKEN ~/Alfred-Pennyworth/.env | cut -d= -f2)
curl -s -X POST http://localhost:7070/execute \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"uptime"}'
```

---

## Fase 8 — Configuração do Open WebUI

### Conta admin

O Open WebUI pede signup na primeira vez que sobe. Se não pediu, é porque os dados antigos persistiram.

### Erro encontrado: WebUI não pedia signup

Após `docker compose down -v` e `docker volume prune`, o WebUI ainda mostrava a conta antiga.

**Causa raiz:** O Open WebUI usa **bind mount** (`./data/open-webui`), não volume gerenciado pelo Docker. O `docker volume prune` só limpa volumes Docker, não bind mounts.

**Solução:**

```bash
docker compose down
rm -rf data/open-webui
mkdir -p data/open-webui
docker compose up -d
```

**Importante:** `rm -rf data/open-webui/*` (com `*`) não apaga arquivos ocultos (`.dotfiles`). Use `rm -rf data/open-webui` sem o `/*` para remover tudo, incluindo o diretório, e depois recrie.

### Criar modelo Alfred Pennyworth

**Workspace → Models → `+`:**

- Nome: Alfred Pennyworth
- Model ID: alfred-pennyworth
- Base model: llama3.1:8b
- System prompt: conteúdo de `docs/alfred-system-prompt.md` (atualizando OS, stack e project root)
- Tools: habilitar todas as 5 tools importadas

### Gerar WEBUI_API_TOKEN

**Settings → Account → API Keys → Create new key** → copiar o token e salvar no `.env`:

```bash
sed -i 's|WEBUI_API_TOKEN=.*|WEBUI_API_TOKEN=TOKEN_COPIADO|' ~/Alfred-Pennyworth/.env
sudo systemctl restart alfred-researcher
```

**Observação:** O formato do token varia conforme a versão do Open WebUI. Pode ou não começar com `sk-`. Use o valor exato que o WebUI gerar.

### Knowledge Bases

**Workspace → Knowledge → `+`** → criar `Vault Alfred` e `Vault Pedro` → copiar os IDs da URL → salvar no `.env`:

```bash
sed -i 's|RESEARCHER_KB_ALFRED_ID=.*|RESEARCHER_KB_ALFRED_ID=UUID_COPIADO|' ~/Alfred-Pennyworth/.env
sed -i 's|RESEARCHER_KB_PEDRO_ID=.*|RESEARCHER_KB_PEDRO_ID=UUID_COPIADO|' ~/Alfred-Pennyworth/.env
sudo systemctl restart alfred-researcher
```

---

## Checklist Final

```bash
echo "=== Docker ===" && docker compose ps
echo -e "\n=== Executor ===" && curl -s http://localhost:7070/health
echo -e "\n=== Researcher ===" && curl -s http://localhost:7071/health
echo -e "\n=== SearXNG ===" && curl -s "http://localhost:8888/search?q=test&format=json" | head -c 80
echo -e "\n\n=== Ollama ===" && ollama list
```

### Resultado esperado

| Serviço | Status |
|---------|--------|
| alfred-webui | Up (healthy) |
| alfred-searxng | Up |
| alfred-chromadb | Up |
| alfred-n8n | Up |
| alfred-executor (systemd) | active (running) |
| alfred-researcher (systemd) | active (running) |
| Ollama | llama3.1:8b + nomic-embed-text-v2-moe |

---

## Resumo das Correções da Auditoria

| Problema | Severidade | Correção |
|----------|-----------|----------|
| Token Bearer hardcoded nas tools | ALTA | Default vazio, valor via Valves |
| IDs de Knowledge Base hardcoded | ALTA | Variáveis de ambiente no `.env` |
| WEBUI_TOKEN= na whitelist do executor | MÉDIA | Removido dos ALLOWED_PREFIXES |
| 3 mecanismos de indexação sobrepostos | MÉDIA | Consolidados em `services/scripts/` com nomes claros |
| Researcher sem unit systemd | MÉDIA | .service criado e habilitado |
| tool-researcher.py orphan em docs/ | MÉDIA | Removido (recriado como tool própria) |
| Sync state em /tmp | BAIXA | Movido para `logs/alfred_last_sync` |
| README desatualizado | BAIXA | Será reescrito |
| Diretórios vazios sem propósito | BAIXA | scripts/ e secrets/ removidos |
| System prompt na raiz | BAIXA | Movido para docs/ |
| Nomes ambíguos de scripts | BAIXA | indexer→webui-indexer, vault-indexer→chromadb-indexer |

---

## Referência Rápida

```bash
# Stack Docker
docker compose up -d
docker compose down
docker compose ps
docker compose logs -f

# Serviços systemd
sudo systemctl {status|restart|stop} alfred-executor
sudo systemctl {status|restart|stop} alfred-researcher

# Health checks
curl http://localhost:7070/health
curl http://localhost:7071/health

# Testar executor manualmente
TOKEN=$(grep SHELL_EXECUTOR_TOKEN ~/Alfred-Pennyworth/.env | cut -d= -f2)
curl -s -X POST http://localhost:7070/execute \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"docker ps"}'

# Ollama
ollama list
ollama ps
nvidia-smi
```

---

## Comunicação entre Serviços

| De → Para | Endereço |
|-----------|----------|
| Container → Container | http://nome-do-servico:porta-interna |
| Container → Host | http://172.17.0.1:porta |
| Host → Container | http://localhost:porta-mapeada |
| N8N → Host | http://host.docker.internal:porta |
