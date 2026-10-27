# researcher — Serviço de pesquisa sob demanda

API REST que recebe um tópico, faz pesquisa na web, sintetiza via LLM e salva no vault. Usado como ferramenta pelo Open WebUI (tool calling) e pelo briefing para aprofundamento contextual futuro.

## Como rodar

```bash
./run.sh          # inicia na porta PORT_RESEARCHER (default: 7071)
```

O `run.sh` lê o `.env` da raiz literalmente, linha a linha (antes fazia `source`, e um valor com `$` ou `(`
derrubava o serviço).

O serviço fica em execução contínua como unit de **usuário** `alfred-researcher.service`, gerada pelo
`services/scripts/instalar.sh` a partir do modelo deste diretório (`@ALFRED_DIR@` → caminho do repositório; não
copie o arquivo direto — ver [README → Setup](../../README.md#4-setup)):

```bash
systemctl --user status alfred-researcher
systemctl --user restart alfred-researcher    # depois de alterar o .env (ex.: WEBUI_API_TOKEN, IDs das KBs)
journalctl --user -fu alfred-researcher
```

**Máquina original:** o researcher roda como unit de **sistema** (`/etc/systemd/system/alfred-researcher.service`,
`User=netto`, `EnvironmentFile=` do `.env`, uvicorn direto, sem o `run.sh`). O `instalar.sh` detecta essa unit e não instala a de usuário por cima
(seriam duas na mesma porta); lá os comandos são `sudo systemctl status|restart alfred-researcher` e
`sudo journalctl -fu alfred-researcher`.

## Endpoints

### `POST /research`
Recebe JSON, executa pesquisa e retorna síntese.

```json
{
  "topic": "Rust async runtime internals",
  "num_queries": 4,
  "results_per_query": 8,
  "perfil": "pedro_dev",
  "refresh": false
}
```

**Resposta:**
```json
{
  "topic": "...",
  "slug": "rust-async-runtime-internals",
  "synthesis": "...",
  "sources": [...],
  "cached": false,
  "cache_type": null,
  "vault_path": "/vaults/alfred/research/rust-async-runtime-internals"
}
```

Se a pesquisa vier do cache, `cached: true` e `cache_type: "exact"` ou `"semantic"` — essas respostas trazem
`synthesis`, mas não `sources_found` nem `queries_used` (só a pesquisa nova traz).

**Erros:** tema vazio → HTTP 422. Se o LLM não devolver a síntese (Ollama fora, GPU ocupada, timeout de 180 s) →
HTTP 503 e **nada é gravado nem cacheado** (antes uma síntese vazia ficava 7 dias como cache). Tema que vira slug
vazio (só símbolos ou emoji, ex.: `???`) é salvo em `research/tema-<hash>/`.

### `GET /health`
Retorna `{"status": "ok", "version": "3.1.0"}`.

## Pipeline — fluxo completo

```
POST /research {topic}
    ↓
slugify(topic) → slug
    ↓
[Cache exato] synthesis.md existe no vault E tem < 7 dias?
    → SIM: retorna síntese cached (cache_type: "exact")
    ↓
[Cache semântico] ChromaDB disponível?
    → gera embedding do topic
    → query_similar() com n_results=3
    → encontrou doc com distância ≤ 0.25 (similaridade ≥ 75%) E idade ≤ 7 dias?
        → SIM: retorna síntese do doc mais próximo (cache_type: "semantic")
    ↓
[Nova pesquisa]
    → gera N queries via Ollama (num_queries)
    → busca cada query no SearXNG (results_per_query resultados cada)
    → deduplica resultados por URL/título
    → monta contexto e chama Ollama para síntese final (vazia → HTTP 503, nada gravado)
    ↓
vault_write() → salva synthesis.md em VAULT_ALFRED/research/<slug>/
    ↓
_index_synthesis() → upsert no ChromaDB (não-bloqueante, falha silenciosa)
    ↓
trigger_sync() → dispara sync assíncrono do vault para Knowledge Base do Open WebUI
```

O `trigger_sync()` só roda com `WEBUI_API_TOKEN` preenchido: executa `services/scripts/sync-knowledge.py` com o
Python do venv do researcher (o script usa `httpx`) e grava a saída em `<repositório>/logs/sync-knowledge.log`.

## Cache em dois níveis

**Nível 1 — Exato (O(1)):** Verifica se o arquivo `VAULT_ALFRED/research/<slug>/synthesis.md` existe, tem conteúdo (síntese vazia não conta) e tem menos de 7 dias. Não faz nenhuma chamada de rede.

**Nível 2 — Semântico (ChromaDB):** Se o cache exato falhar, gera embedding do tópico e busca os 3 documentos mais próximos na coleção `research_alfred`. O limiar é distância coseno ≤ 0.25 (≡ similaridade ≥ 75%). "Kubernetes networking" vai encontrar "redes no Kubernetes" como hit semântico.

O parâmetro `refresh: true` ignora os dois caches e força nova pesquisa.

## Autenticação

Requer `Authorization: Bearer <SHELL_EXECUTOR_TOKEN>` em todas as requisições. O token vem do `.env`.

## ChromaDB e embeddings

- Coleção: `research_alfred` (espaço coseno)
- Modelo de embedding: `MODEL_EMBED` (default: `nomic-embed-text-v2-moe:latest`)
- Se ChromaDB estiver offline: serviço opera normalmente sem cache semântico, só cache exato
- O bootstrap retroativo está em `scripts/bootstrap_chromadb.py` — indexa todos os `synthesis.md` existentes no vault

## Injeção de perfil

Se `perfil` for fornecido na request (ex: `"pedro_dev"`), o serviço injeta o contexto do perfil nos prompts de geração de queries e de síntese, guiando o LLM a priorizar ângulos relevantes para aquele usuário.

## Saída no vault

```
VAULT_ALFRED/research/
  <slug>/
    synthesis.md    ← síntese gerada pelo LLM (frontmatter + conteúdo)
    sources.json    ← lista das fontes consultadas (opcional)
```

O `synthesis.md` tem frontmatter:
```yaml
---
title: "Síntese — <topic>"
date: 2026-04-10
sources: 12
---
```

## Dependências

```
fastapi, uvicorn, httpx, pyyaml
```
E as libs compartilhadas: `lib_alfred`, `lib_chromadb`, `perfis`.

## Variáveis de ambiente

| Variável | Uso |
|----------|-----|
| `SHELL_EXECUTOR_TOKEN` | Token Bearer de autenticação |
| `VAULT_ALFRED` | Caminho do vault para salvar pesquisas |
| `WEBUI_API_TOKEN` | Token para sync da Knowledge Base no Open WebUI |
| `WEBUI_URL` | URL do Open WebUI (default: `http://localhost:3000`) |
| `PORT_RESEARCHER` | Porta do serviço (default: `7071`) |
