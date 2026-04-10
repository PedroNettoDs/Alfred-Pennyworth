# researcher — Serviço de pesquisa sob demanda

API REST que recebe um tópico, faz pesquisa na web, sintetiza via LLM e salva no vault. Usado como ferramenta pelo Open WebUI (tool calling) e pelo briefing para aprofundamento contextual futuro.

## Como rodar

```bash
./run.sh          # inicia na porta PORT_RESEARCHER (default: 7071)
```

O serviço fica em execução contínua como daemon. Está registrado como `alfred-researcher.service` no systemd.

```bash
sudo systemctl status alfred-researcher
sudo systemctl restart alfred-researcher
journalctl -u alfred-researcher -f
```

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

Se a pesquisa vier do cache, `cached: true` e `cache_type: "exact"` ou `"semantic"`.

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
    → monta contexto e chama Ollama para síntese final
    ↓
vault_write() → salva synthesis.md em VAULT_ALFRED/research/<slug>/
    ↓
_index_synthesis() → upsert no ChromaDB (não-bloqueante, falha silenciosa)
    ↓
trigger_sync() → dispara sync assíncrono do vault para Knowledge Base do Open WebUI
```

## Cache em dois níveis

**Nível 1 — Exato (O(1)):** Verifica se o arquivo `VAULT_ALFRED/research/<slug>/synthesis.md` existe e tem menos de 7 dias. Não faz nenhuma chamada de rede.

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
