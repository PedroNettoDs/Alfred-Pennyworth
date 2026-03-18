# Alfred — Auditoria de Estrutura

**Data:** 2026-03-18
**Gerado por:** Claude Code (claude-sonnet-4-6)

---

## Estrutura atual

```
/mnt/SSD/alfred/
├── .claude/
│   └── settings.local.json          ← permissões do Claude Code
├── .env                              ← fonte da verdade (não versionado)
├── .env.example                      ← template de variáveis
├── .gitattributes
├── .gitignore
├── Alfred - system prompt.md         ← persona e regras comportamentais do Alfred
├── README.md                         ← guia de setup e operação
├── docker-compose.yml               ← orquestração da stack Docker
├── config/
│   └── searxng/
│       └── settings.yml             ← configuração do SearXNG
├── data/                             ← volumes persistentes (não versionado)
│   ├── chromadb/
│   ├── n8n/
│   └── open-webui/
├── docs/
│   ├── Alfred_Documentacao_Tecnica.docx
│   ├── alfred-pennyworth.json        ← perfil exportado do Open WebUI
│   ├── alfred-system-prompt-research-addon.md
│   ├── tool-researcher.py            ← script avulso em docs/ (orphan?)
│   └── tools-export.json            ← definições de tools exportadas
├── logs/                             ← diretório de logs (vazio)
├── scripts/                          ← diretório vazio (não usado)
├── secrets/                          ← diretório vazio (não usado)
├── services/
│   ├── indexer.py                   ← indexa vaults nas memories do Open WebUI
│   ├── link_knowledge.sh            ← vincula arquivos às Knowledge Bases
│   ├── sync_knowledge.py            ← sync incremental vault → Knowledge Base
│   ├── upload_knowledge.sh          ← upload em lote de arquivos .md
│   ├── vault-indexer.py             ← indexa vaults no ChromaDB com embeddings
│   ├── researcher/
│   │   ├── alfred-researcher.service ← unit systemd do researcher
│   │   ├── main.py                  ← FastAPI: orquestrador de pesquisa web
│   │   ├── requirements.txt
│   │   ├── run.sh
│   │   └── venv/                    ← virtualenv (não versionado)
│   └── shell-executor/
│       ├── main.py                  ← FastAPI: executor de comandos shell
│       ├── requirements.txt
│       ├── run.sh
│       └── venv/                    ← virtualenv (não versionado)
├── tools/
│   ├── alfred_shell_executor.py     ← tool Open WebUI: executa comandos
│   ├── alfred_system_monitor.py     ← tool Open WebUI: monitora recursos
│   ├── alfred_vault_reader.py       ← tool Open WebUI: lê vault do Pedro
│   └── alfred_vault_writer.py       ← tool Open WebUI: escreve no vault do Alfred
└── vaults/
    ├── .obsidian/
    └── alfred/                      ← vault RW do Alfred (não versionado)
```

---

## Diagnóstico por arquivo

### Serviços com servidor (subpastas próprias) ✅

| Arquivo | Função | Porta | Chamado por | Dependências | Problemas |
|---------|--------|-------|-------------|--------------|-----------|
| `services/shell-executor/main.py` | FastAPI: executa comandos shell whitelistados com autenticação Bearer | 7070 | Open WebUI (via tools), N8N, chamada manual | fastapi, uvicorn | Token padrão `"token-secreto-alfred"` em código se env var ausente |
| `services/shell-executor/requirements.txt` | Dependências do shell-executor | — | pip durante setup | — | Nenhum |
| `services/shell-executor/run.sh` | Script de inicialização manual | — | Operador (manual) | venv local | Redundante com systemd; não há `.service` nesta subpasta |
| `services/researcher/main.py` | FastAPI: orquestra pesquisa web (SearXNG + Ollama + vault) | 7071 | N8N, chamada manual, Open WebUI | httpx, fastapi, uvicorn, pydantic, Ollama, SearXNG, sync_knowledge.py | IDs de KB hardcoded dentro de sync_knowledge.py; sem unit systemd instalada |
| `services/researcher/requirements.txt` | Dependências do researcher | — | pip durante setup | — | Nenhum |
| `services/researcher/run.sh` | Script de inicialização manual | — | Operador (manual) | venv local | Nenhum |
| `services/researcher/alfred-researcher.service` | Unit systemd do researcher | — | systemd | — | Não instalada em `/etc/systemd/system/` ainda |

### Scripts utilitários soltos em `services/` ⚠️

| Arquivo | Função | Chamado por | Dependências | Problemas |
|---------|--------|-------------|--------------|-----------|
| `services/indexer.py` | Indexa arquivos .md dos vaults nas memories do Open WebUI | README (manual), N8N (a cada 6h segundo README) | requests, Open WebUI API | Sobreposição com `sync_knowledge.py` e `vault-indexer.py`; nome genérico demais; sem subpasta |
| `services/vault-indexer.py` | Indexa arquivos .md no ChromaDB com embeddings (`nomic-embed-text`) | Chamada manual | requests, ChromaDB API, Ollama | Nome similar a `indexer.py` gera confusão; sem subpasta; responsabilidade diferente de `indexer.py` |
| `services/sync_knowledge.py` | Sync incremental vault → Knowledge Base do Open WebUI (apenas arquivos modificados desde último run) | Chamado pelo `researcher/main.py` após pesquisa | requests, Open WebUI API | IDs de KB hardcoded (`b3dc99a4...`, `621560c8...`); rastreia última execução em `/tmp` (não persiste após reboot) |
| `services/link_knowledge.sh` | Vincula todos os arquivos existentes no Open WebUI às KBs corretas | README (manual) | curl, Open WebUI API | Não documentado no README; IDs/tokens dependem do ambiente |
| `services/upload_knowledge.sh` | Upload em lote de arquivos .md do vault para o Open WebUI | README (manual) | curl, Open WebUI API | Não documentado no README; pode duplicar o que `indexer.py` já faz |

### Tools do Open WebUI em `tools/` ✅

| Arquivo | Função | Chamado por | Dependências | Problemas |
|---------|--------|-------------|--------------|-----------|
| `tools/alfred_shell_executor.py` | Tool Open WebUI: proxy para o Shell Executor | Open WebUI (ferramenta do Alfred) | httpx (ou requests), Shell Executor (7070) | **Token Bearer hardcoded** (`4695e1b8...`) como default; deve vir de Valves/env |
| `tools/alfred_system_monitor.py` | Tool Open WebUI: relatório de CPU, RAM, disco, GPU, containers | Open WebUI | Shell Executor (7070) | Mesmo problema de token hardcoded; wraps do executor |
| `tools/alfred_vault_reader.py` | Tool Open WebUI: busca e leitura do vault do Pedro | Open WebUI | Sistema de arquivos (`/vaults/pedro`) | Acesso direto ao FS via caminho container; sem autenticação |
| `tools/alfred_vault_writer.py` | Tool Open WebUI: salva e busca no vault do Alfred | Open WebUI | Sistema de arquivos (`/vaults/alfred`) | Idem; sem validação de path traversal |

### Arquivos avulsos em locais inesperados

| Arquivo | Função | Chamado por | Problemas |
|---------|--------|-------------|-----------|
| `docs/tool-researcher.py` | Desconhecido/protótipo de tool | Nada identificado | **Orphan**: em `docs/` em vez de `tools/` ou `services/`; pode ser versão anterior do researcher |
| `Alfred - system prompt.md` | Persona e regras comportamentais do Alfred | Open WebUI (colado manualmente no system prompt) | Na raiz do projeto; poderia estar em `docs/` |
| `scripts/` | — | — | Diretório vazio sem propósito aparente |
| `secrets/` | — | — | Diretório vazio; sensível por nome, sem conteúdo |

---

## Dependências entre serviços

```
                    ┌─────────────────────────────────────────────────────────┐
                    │  HOST (Pop!_OS 24.04)                                   │
                    │                                                          │
  Pedro             │  ┌──────────────┐    ┌───────────────────┐             │
  (usuário) ──────▶ │  │  Open WebUI  │    │     Ollama        │             │
                    │  │  :3000       │◀───│  :11434           │             │
                    │  │  (Docker)    │    │  qwen3:8b         │             │
                    │  └──────┬───────┘    │  nomic-embed-text │             │
                    │         │            └───────────────────┘             │
                    │         │ tools                 ▲                       │
                    │         ▼                       │                       │
                    │  ┌──────────────┐    ┌──────────┴────────┐             │
                    │  │    tools/    │    │  Shell Executor   │             │
                    │  │  (4 tools)   │───▶│  :7070 (systemd)  │             │
                    │  └──────────────┘    └───────────────────┘             │
                    │         │                                               │
                    │         │ pesquisa                                      │
                    │         ▼                                               │
                    │  ┌──────────────┐    ┌───────────────────┐             │
                    │  │  Researcher  │───▶│    SearXNG        │             │
                    │  │  :7071       │    │  :8888 (Docker)   │             │
                    │  └──────┬───────┘    └───────────────────┘             │
                    │         │                                               │
                    │         │ após pesquisa                                 │
                    │         ▼                                               │
                    │  ┌──────────────┐    ┌───────────────────┐             │
                    │  │sync_knowledge│───▶│  Open WebUI KB    │             │
                    │  │.py           │    │  API :3000        │             │
                    │  └──────────────┘    └───────────────────┘             │
                    │                                                         │
                    │  ┌──────────────┐    ┌───────────────────┐             │
                    │  │vault-indexer │───▶│    ChromaDB       │             │
                    │  │.py           │    │  :8000 (Docker)   │             │
                    │  └──────────────┘    └───────────────────┘             │
                    │                                                         │
                    │  ┌──────────────┐                                       │
                    │  │     N8N      │ (workflows de automação, re-indexação)│
                    │  │  :5678       │                                       │
                    │  │  (Docker)    │                                       │
                    │  └──────────────┘                                       │
                    └─────────────────────────────────────────────────────────┘
```

### Tabela de serviços

| Serviço | Porta | Processo | Consome | Consumido por |
|---------|-------|----------|---------|---------------|
| Ollama | 11434 | Host (nativo) | GPU | Open WebUI, Researcher, vault-indexer |
| Open WebUI | 3000 | Docker | Ollama, SearXNG, ChromaDB, Shell Executor | Pedro (browser), N8N, scripts de indexação |
| SearXNG | 8888 | Docker | Internet | Open WebUI (RAG), Researcher |
| ChromaDB | 8000 | Docker | Disco | Open WebUI (RAG), vault-indexer |
| N8N | 5678 | Docker | Open WebUI, Shell Executor, Ollama | Pedro (workflows) |
| Shell Executor | 7070 | Host (systemd) | Sistema operacional | Open WebUI (tools), N8N |
| Researcher | 7071 | Host (systemd) | Ollama, SearXNG, Open WebUI API | N8N, Open WebUI (chamada manual) |

---

## Problemas encontrados

### Alta severidade

1. **[ALTA] Token Bearer hardcoded nas tools** — `tools/alfred_shell_executor.py` e `tools/alfred_system_monitor.py` contêm um token Bearer como valor default (`4695e1b8...`). Qualquer pessoa com acesso ao repositório tem o token. O valor deve vir exclusivamente de Valves configurados no Open WebUI, sem default hardcoded.

2. **[ALTA] IDs de Knowledge Base hardcoded** — `services/sync_knowledge.py` contém UUIDs das KBs do Open WebUI em código. Se a instância for recriada, esses IDs mudam e o sync silenciosamente para de funcionar. Devem ser variáveis de ambiente.

### Média severidade

3. **[MÉDIA] Três mecanismos de indexação sobrepostos** — `indexer.py`, `vault-indexer.py`, `sync_knowledge.py`, `upload_knowledge.sh` e `link_knowledge.sh` fazem variações da mesma operação (enviar arquivos do vault para o Open WebUI). Não há documentação clara sobre qual usar em qual contexto, quando usar cada um, ou se são complementares ou substitutos.

4. **[MÉDIA] Researcher não tem unit systemd instalada** — O arquivo `alfred-researcher.service` existe na subpasta mas o README não documenta sua instalação. O README menciona o serviço como existente mas não descreve o setup do researcher (apenas do shell-executor).

5. **[MÉDIA] `docs/tool-researcher.py` é orphan sem propósito documentado** — Arquivo Python dentro de `docs/` que não é chamado por ninguém e não é documentado no README. Pode ser um protótipo descartado ou versão anterior de uma tool.

6. **[MÉDIA] `WEBUI_TOKEN=` na whitelist do shell-executor** — O prefixo `WEBUI_TOKEN=` está listado como comando permitido no executor, o que permite ao Alfred (ou a qualquer chamador autenticado) extrair variáveis de ambiente sensíveis via `env | grep WEBUI_TOKEN`. Isso é um vetor de exposição de credenciais.

### Baixa severidade

7. **[BAIXA] Sync de knowledge base rastreia estado em `/tmp`** — `sync_knowledge.py` grava o timestamp do último sync em `/tmp/alfred_last_sync`. Após reboot, esse arquivo some e a próxima execução faz um full sync desnecessário. Poderia usar um arquivo em `data/` ou `logs/`.

8. **[BAIXA] README desatualizado** — A estrutura documentada no README não inclui `tools/`, `services/researcher/`, `services/vault-indexer.py`, `services/sync_knowledge.py`, `services/link_knowledge.sh`, `services/upload_knowledge.sh`. Também não menciona o serviço systemd do researcher.

9. **[BAIXA] Diretórios vazios sem propósito** — `scripts/` e `secrets/` existem mas estão completamente vazios, sem README nem placeholder. O diretório `logs/` também está vazio; os serviços escrevem logs via systemd journal, não em arquivos.

10. **[BAIXA] `Alfred - system prompt.md` na raiz** — Arquivo de documentação na raiz do projeto junto com `docker-compose.yml` e `.env`. Deveria estar em `docs/`.

11. **[BAIXA] Nomes de arquivos ambíguos em `services/`** — `indexer.py` e `vault-indexer.py` têm nomes que sugerem a mesma função mas fazem coisas diferentes (Open WebUI memories vs ChromaDB com embeddings). Um usuário novo não saberia qual usar para qual backend.

---

## Estrutura proposta

```
/mnt/SSD/alfred/
├── .env                              ← sem alteração
├── .env.example                      ← adicionar: RESEARCHER_KB_ALFRED_ID, RESEARCHER_KB_PEDRO_ID
├── .gitignore
├── .gitattributes
├── docker-compose.yml
├── README.md                         ← atualizar estrutura e setup do researcher
├── config/
│   └── searxng/
│       └── settings.yml
├── data/                             ← sem alteração (não versionado)
├── docs/
│   ├── Alfred - system prompt.md     ← MOVER da raiz
│   ├── alfred-pennyworth.json
│   ├── alfred-system-prompt-research-addon.md
│   ├── Alfred_Documentacao_Tecnica.docx
│   ├── tool-researcher.py            ← AVALIAR: mover para tools/ ou deletar
│   └── tools-export.json
├── logs/                             ← adicionar .gitkeep e redirecionar sync state aqui
├── services/
│   ├── researcher/                   ← sem alteração estrutural
│   │   ├── alfred-researcher.service
│   │   ├── main.py                   ← corrigir: KB IDs via env vars
│   │   ├── requirements.txt
│   │   ├── run.sh
│   │   └── venv/
│   ├── shell-executor/               ← sem alteração estrutural
│   │   ├── alfred-executor.service   ← MOVER unit de /etc/systemd para cá (ref only)
│   │   ├── main.py                   ← corrigir: remover WEBUI_TOKEN= da whitelist
│   │   ├── requirements.txt
│   │   ├── run.sh
│   │   └── venv/
│   └── scripts/                      ← NOVO: agrupar scripts utilitários
│       ├── indexer.py                ← MOVER de services/
│       ├── vault-indexer.py          ← MOVER de services/ + renomear: chromadb-indexer.py
│       ├── sync_knowledge.py         ← MOVER de services/ + corrigir: KB IDs via env
│       ├── link_knowledge.sh         ← MOVER de services/
│       └── upload_knowledge.sh       ← MOVER de services/
├── tools/                            ← sem alteração estrutural
│   ├── alfred_shell_executor.py      ← corrigir: remover token hardcoded
│   ├── alfred_system_monitor.py      ← corrigir: remover token hardcoded
│   ├── alfred_vault_reader.py
│   └── alfred_vault_writer.py
└── vaults/                           ← sem alteração (não versionado)
```

---

## Plano de migração

Os passos abaixo são ordenados para evitar quebrar serviços em produção. Cada passo é independente e pode ser executado separadamente.

### Etapa 1 — Correções críticas de segurança (sem mover arquivos)

1. **Remover token hardcoded das tools** — Em `tools/alfred_shell_executor.py` e `tools/alfred_system_monitor.py`, substituir o valor default do token por uma string vazia ou pela instrução de configurar via Valves. Re-importar as tools no Open WebUI após a edição.

2. **Mover IDs de KB para variáveis de ambiente** — Em `services/sync_knowledge.py`, substituir os UUIDs hardcoded por `os.environ.get("RESEARCHER_KB_PEDRO_ID")` e `os.environ.get("RESEARCHER_KB_ALFRED_ID")`. Adicionar essas chaves ao `.env` e ao `.env.example`. Reiniciar o researcher após a edição.

3. **Remover `WEBUI_TOKEN=` da whitelist do shell-executor** — Em `services/shell-executor/main.py`, remover essa entrada do array `ALLOWED_PREFIXES`. Reiniciar o serviço systemd.

### Etapa 2 — Mover estado transitório para `logs/`

4. **Redirecionar timestamp de sync** — Em `services/sync_knowledge.py`, trocar `/tmp/alfred_last_sync` por `/mnt/SSD/alfred/logs/alfred_last_sync`. Adicionar `logs/alfred_last_sync` ao `.gitignore`.

### Etapa 3 — Organização de scripts utilitários

> Atenção: estes scripts são chamados por linha de comando e pelo `researcher/main.py` (que chama `sync_knowledge.py` via subprocess/import). Atualizar o caminho no `researcher/main.py` antes de mover.

5. **Criar subpasta `services/scripts/`** e mover os cinco scripts utilitários:
   - `services/indexer.py` → `services/scripts/indexer.py`
   - `services/vault-indexer.py` → `services/scripts/chromadb-indexer.py`
   - `services/sync_knowledge.py` → `services/scripts/sync_knowledge.py`
   - `services/link_knowledge.sh` → `services/scripts/link_knowledge.sh`
   - `services/upload_knowledge.sh` → `services/scripts/upload_knowledge.sh`

6. **Atualizar referências** — Após mover, atualizar:
   - `services/researcher/main.py`: qualquer import ou subprocess call para `sync_knowledge.py`
   - `README.md`: todos os comandos de exemplo que referenciam esses caminhos

### Etapa 4 — Organização de documentação

7. **Mover `Alfred - system prompt.md`** para `docs/Alfred - system prompt.md`. Nenhum serviço referencia esse arquivo programaticamente; é colado manualmente no Open WebUI.

8. **Avaliar `docs/tool-researcher.py`** — Determinar se é um protótipo ativo ou descartado. Se descartado: deletar. Se ativo: mover para `tools/` ou `services/researcher/`.

### Etapa 5 — Documentação

9. **Atualizar `README.md`**:
   - Adicionar setup do Researcher (espelho do setup do Shell Executor)
   - Atualizar árvore de diretórios
   - Documentar `services/scripts/` e quando usar cada script
   - Adicionar tabela diferenciando `indexer.py` (Open WebUI memories) de `chromadb-indexer.py` (ChromaDB)

10. **Instalar unit systemd do researcher** (se ainda não estiver):
    ```bash
    sudo cp services/researcher/alfred-researcher.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable alfred-researcher
    sudo systemctl start alfred-researcher
    ```

### Etapa 6 — Limpeza final

11. **Adicionar `.gitkeep`** nos diretórios vazios `logs/`, `scripts/` (ou remover `scripts/` se não for usar), `secrets/` (ou remover se não for usar).

12. **Verificar `secrets/`** — Se o diretório existir para uso futuro com Docker secrets, adicionar ao `.gitignore`. Se não houver plano de uso, remover.

---

## O que está faltando

### Serviços no README sem arquivo correspondente

| Item | Mencionado em | Status |
|------|---------------|--------|
| Unit systemd `alfred-executor.service` | README (seção systemd) | O arquivo existe em `/etc/systemd/system/` (instalado), mas não há cópia de referência em `services/shell-executor/` como existe para o researcher |
| N8N re-indexação automática a cada 6h | README (seção RAG) | Existe como workflow no N8N (não versionado em `data/n8n/`), sem backup em `docs/` |
| Nginx + Cloudflare Tunnel | README (Próximos Passos) | Não existe nenhum arquivo de configuração |

### Arquivos existentes não documentados no README

| Arquivo | Status no README |
|---------|-----------------|
| `services/researcher/` (inteiro) | Não mencionado |
| `services/vault-indexer.py` | Não mencionado |
| `services/sync_knowledge.py` | Não mencionado |
| `services/link_knowledge.sh` | Não mencionado |
| `services/upload_knowledge.sh` | Não mencionado |
| `tools/` (todos os 4 arquivos) | Não mencionado |
| `docs/tool-researcher.py` | Não mencionado |
| `Alfred - system prompt.md` | Não mencionado |

### Variáveis no `.env` sem serviço correspondente

| Variável | Status |
|----------|--------|
| `PORT_RESEARCHER` | Definida, usada pelo researcher — mas o README não documenta esse serviço |
| `MODEL_EMBED` | Definida, usada pelo `vault-indexer.py` — não documentada no README |
| `VAULT_ALFRED` e `VAULT_PEDRO` | Definidas, usadas pelos scripts — não documentadas no README |
| `HOST_IP` | Definida para comunicação container→host — mencionada apenas implicitamente |

### Variáveis ausentes no `.env.example` (necessárias para funcionamento completo)

| Variável necessária | Usado em |
|--------------------|----------|
| `RESEARCHER_KB_ALFRED_ID` | `sync_knowledge.py` (atualmente hardcoded) |
| `RESEARCHER_KB_PEDRO_ID` | `sync_knowledge.py` (atualmente hardcoded) |
