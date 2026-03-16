# Alfred Pennyworth — Assistente Pessoal com IA Local

Assistente pessoal rodando 100% localmente, baseado em LLMs via Ollama, com capacidade de executar comandos no sistema, pesquisar na web, escrever no vault Obsidian e indexar documentos para RAG.

**Autor:** Pedro Henrique Netto dos Santos
**Ambiente:** Pop!_OS 24.04 · NVIDIA RTX 3060 12GB · `/mnt/SSD/alfred`

---

## Status dos Serviços

| Serviço | URL | Status |
|---|---|---|
| Ollama (host) | `localhost:11434` | ✅ Ativo |
| Open WebUI | `localhost:3000` | ✅ Ativo |
| SearXNG | `localhost:8888` | ✅ Ativo |
| ChromaDB | `localhost:8000` | ✅ Ativo |
| N8N | `localhost:5678` | ✅ Ativo |
| Shell Executor | `localhost:7070` | ✅ Ativo |
| alfred-executor (systemd) | serviço permanente | ✅ Ativo |
| Cloudflare Tunnel | — | ⚠️ Pendente |

---

## Modelos Ollama

| Modelo | Função | Tamanho |
|---|---|---|
| `qwen3:8b` (Q4_K_M) | Raciocínio principal e uso de tools | ~5.2 GB |
| `nomic-embed-text` (F16) | Embeddings para RAG | ~274 MB |

---

## Arquitetura

O projeto é dividido em duas camadas:

- **Host:** Ollama e Shell Executor (serviços nativos, acesso direto ao sistema)
- **Docker:** Open WebUI, SearXNG, ChromaDB, N8N (isolados na rede `alfred-net`)

Toda configuração é centralizada no arquivo `.env` — nenhum valor é hardcoded no `docker-compose.yml`.

---

## Estrutura de Diretórios

```
/mnt/SSD/alfred/
├── .env                        ← fonte da verdade (não versionar)
├── .env.example                ← template sem valores reais
├── .gitignore
├── docker-compose.yml
├── README.md
├── config/
│   └── searxng/
│       └── settings.yml        ← não versionar (tokens/ajustes locais)
├── data/                       ← volumes persistentes (não versionar)
│   ├── chromadb/
│   ├── n8n/
│   └── open-webui/
├── vaults/
│   └── alfred/                 ← vault RW do Alfred (não versionar)
└── services/
    ├── indexer.py
    └── shell-executor/
        ├── main.py
        ├── requirements.txt
        ├── run.sh
        └── venv/               ← não versionar
```

---

## Pré-requisitos

- Docker + Docker Compose
- [Ollama](https://ollama.com) instalado no host
- Python 3.12+ (para o Shell Executor)
- GPU NVIDIA com drivers + CUDA (recomendado)

---

## Setup

### 1. Configurar variáveis de ambiente

```bash
cp .env.example .env
# editar .env com seus valores reais
```

> **Atenção:** nunca adicione comentários na mesma linha de um valor no `.env`.
> `TOKEN=abc123   # comentário` — o comentário é lido como parte do valor e quebra a autenticação.

### 2. Subir a stack Docker

```bash
cd /mnt/SSD/alfred
docker compose up -d
docker compose ps        # verificar status
```

### 3. Instalar o Shell Executor

```bash
cd services/shell-executor
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn
```

### 4. Habilitar o serviço systemd

```bash
# copiar alfred-executor.service para /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable alfred-executor
sudo systemctl start alfred-executor
```

---

## Serviço systemd — Shell Executor

```ini
# /etc/systemd/system/alfred-executor.service
[Unit]
Description=Alfred Shell Executor
After=network.target

[Service]
Type=simple
User=pedro.netto
WorkingDirectory=/mnt/SSD/alfred/services/shell-executor
EnvironmentFile=/mnt/SSD/alfred/.env
ExecStart=/mnt/SSD/alfred/services/shell-executor/venv/bin/uvicorn main:app --host 0.0.0.0 --port 7070
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

---

## Comunicação entre Serviços

| De → Para | Endereço |
|---|---|
| Container → Container | `http://<nome-do-serviço>:<porta>` |
| Container → Host | `http://172.17.0.1:<porta>` |
| N8N → Host | `http://host.docker.internal:<porta>` |

---

## RAG — Indexação dos Vaults

```bash
# Indexar vault do Alfred
WEBUI_TOKEN=$(grep WEBUI_API_TOKEN .env | cut -d= -f2) \
VAULT_ALFRED=/mnt/SSD/alfred/vaults/alfred \
python3 services/indexer.py alfred

# Indexar vault do Pedro
WEBUI_TOKEN=$(grep WEBUI_API_TOKEN .env | cut -d= -f2) \
python3 services/indexer.py pedro
```

O N8N re-indexa o vault do Alfred automaticamente a cada 6 horas.

---

## Referência Rápida

```bash
# Stack Docker
docker compose up -d
docker compose down
docker compose ps
docker compose logs -f
docker compose up -d --no-deps open-webui   # recriar só o WebUI

# Ollama
ollama list
ollama ps
curl http://localhost:11434/api/tags

# Shell Executor
sudo systemctl status alfred-executor
sudo systemctl restart alfred-executor
curl http://localhost:7070/health

# ChromaDB
curl http://localhost:8000/api/v2/version
curl http://localhost:8000/api/v2/collections | python3 -m json.tool

# Diagnóstico geral
docker compose ps
ss -tlnp | grep -E '3000|5678|7070|8000|8888|11434'
nvidia-smi
df -h /mnt/SSD
free -h
```

---

## Próximos Passos

- [ ] **Nginx + Cloudflare Tunnel** — expor o WebUI via HTTPS público (`alfred.seudominio.com`)
- [ ] **Hooks e Scripts** — Alfred disparar `.sh` e `.py` por nome via Shell Executor
- [ ] **Webhooks N8N** — acionados por padrões de mensagem
- [ ] **Integração com Aider** — edição de código sob demanda

---

## Troubleshooting

Consulte a documentação técnica completa (`Alfred_Documentacao_Tecnica.docx`) para diagnóstico detalhado dos seguintes problemas já resolvidos:

- Alfred inventando resultados de comandos (Code Interpreter ativo / tool não vinculada)
- `SHELL_EXECUTOR_TOKEN` inválido por comentário inline no `.env`
- `main.py` corrompido por heredoc no terminal
- Open WebUI rejeitando API keys (`ENABLE_API_KEY=true`)
- Indexer retornando erro 405 (endpoint errado)
- Alfred chamando `save_to_vault` antes de pesquisar
