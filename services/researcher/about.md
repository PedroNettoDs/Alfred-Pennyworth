# researcher — Serviço de pesquisa sob demanda

API REST que recebe um tópico, faz pesquisa na web, sintetiza via LLM e salva no vault. Usado como ferramenta pelo Open WebUI (tool calling) e pelo briefing para aprofundamento contextual futuro.

Dois modos: o **rápido** (padrão, síncrono — síntese só com os trechos que o SearXNG devolve) e o **profundo**
(`"profundo": true`, em segundo plano — plano em subperguntas, leitura das páginas, notas por fonte e relatório com
citações `[n]`). Ver [Pesquisa profunda](#pesquisa-profunda).

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
  "refresh": false,
  "profundo": false,
  "paginas_por_subpergunta": 3
}
```

`profundo` e `paginas_por_subpergunta` são opcionais; sem eles a chamada é a mesma de antes (modo rápido). No modo
profundo `num_queries` não é usado (o plano decide quantas subperguntas, de 3 a 6) e `results_per_query` vira o
número de resultados pedidos ao SearXNG por subpergunta.

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

**Modo profundo:** acerto de cache responde na hora (200, mesmo formato acima). Sem cache, responde **HTTP 202**
e a pesquisa continua em segundo plano:

```json
{"status": "running", "profundo": true, "job_id": "3f9c0a1b2d4e", "topic": "...", "slug": "...",
 "progresso": "/research/3f9c0a1b2d4e"}
```

Pedir de novo o mesmo tema enquanto ele roda devolve o mesmo `job_id` (não abre outro job).

### `GET /research/{job_id}`
Progresso de uma pesquisa profunda (mesma autenticação). Campos principais:

| Campo | Conteúdo |
|-------|----------|
| `status` | `running` · `done` · `error` · `interrompido` (o serviço reiniciou no meio — a thread morreu junto) |
| `etapa` | `fila` → `plano` → `busca` / `leitura` (por subpergunta) → `sintese` → `vault` → `concluido` |
| `mensagem` | a última linha de progresso (a mesma que vai para o log) |
| `subperguntas` | o plano |
| `notas` | as notas de máquina já extraídas: `{"url", "titulo", "subpergunta", "fatos": [...]}` |
| `contagem` | `resultados` (únicos), `relevantes` (passaram no filtro), `tentadas` (downloads), `lidas` (viraram nota) |
| `erro` | com `status: error` — ex.: síntese vazia, nenhuma página lida |
| `resultado` | com `status: done` — o mesmo formato da pesquisa nova acima, mais `profundo: true`, `subperguntas` e `synthesis` (o relatório) |

ID desconhecido → 404. O estado fica em `<repositório>/logs/research-jobs/<job_id>.json`, gravado de forma atômica
(temporário + `os.replace`), então sobrevive a um restart do serviço.

### `GET /health`
Retorna `{"status": "ok", "version": "3.2.0"}`.

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

## Pesquisa profunda

`"profundo": true` troca o passo `[Nova pesquisa]` acima por um job em segundo plano (uma thread; **um job profundo
por vez** — dois modelos grandes na mesma GPU só ficariam trocando de lugar, então o segundo espera em `fila`):

```
1. Plano       modelo leve + JSON Schema (ollama_generate_json) → 3 a 6 subperguntas
               (LLM fora / resposta inútil → o próprio tema vira a única subpergunta)
2. Busca       por subpergunta: searxng_search → deduplicate_by_url_and_title → tira URLs já tentadas
               → _relevante(subpergunta, resultado) — filtro barato ANTES de baixar
3. Leitura     até paginas_por_subpergunta páginas por subpergunta, na ordem do SearXNG (falhou → tenta o próximo)
               httpx → texto (BeautifulSoup) → nota de máquina com o modelo leve: {"url", "titulo", "fatos": [...]}
4. Síntese     modelo maior, só com as notas numeradas → Markdown com citações [n]
               ([1, 3] vira [1][3]; número fora da lista é removido) + lista "## Fontes" [n] → URL, montada pelo código
5. Vault       research/<slug>/ synthesis.md + sources.md + index.md (frontmatter modo: profundo) → ChromaDB → sync KB
```

Sem nenhuma nota (busca vazia, tudo filtrado, downloads falharam) ou com síntese vazia, o job termina em `error` e
**nada é gravado** — como o HTTP 503 do modo rápido. Cada etapa sai no log com o ID do job
(`[researcher] [3f9c0a1b2d4e] leitura: subpergunta 2/4: nota 3 — 5 fatos de https://...`), então
`journalctl --user -fu alfred-researcher` acompanha o andamento tão bem quanto o `GET /research/{job_id}`.

**Filtro de relevância (`_relevante`)** — função isolada, de propósito: hoje compara o embedding da subpergunta com
o de título + trecho do resultado (`ollama_embed`, cosseno ≥ `pesquisa.relevancia_minima`, padrão 0.5); será
trocada por uma decisão do **Laya** (classificador local, `127.0.0.1:7073`) quando o modelo treinado for aprovado —
só o corpo muda. Sem embedding (Ollama fora), deixa passar: o filtro economiza downloads, não derruba a pesquisa.
O limiar depende do `MODEL_EMBED`; os números de `contagem` (resultados × relevantes) ajudam a calibrar.

**Leitura das páginas** — o researcher roda no host, ao lado de Ollama, painel e APIs sem senha, então a página
nunca escolhe para onde ele vai:

- só `http`/`https`, e o host precisa resolver **apenas** para IPs públicos (nada de `localhost`, rede local,
  `169.254.169.254`, CGNAT, IPv6 local ou IPv4 mapeado em IPv6);
- redirects seguidos à mão (até 3), cada destino validado de novo — um 302 para `http://127.0.0.1:7072/` é recusado;
- no máximo 2 MB lidos por página (o resto é descartado) e 15 s no total, do pedido ao último byte;
- só `text/html`, `application/xhtml+xml` e `text/plain` (PDF e afins ficam de fora); menos de 200 caracteres de
  texto (página só de JavaScript, paywall) não vira nota;
- o texto vai ao modelo de notas cortado em 8.000 caracteres, com o aviso de que é dado da web, não instrução.

Fica uma janela entre a resolução do DNS e a conexão (DNS rebinding) — risco aceito para um assistente de um usuário
só que lê o que o SearXNG local devolveu.

**Modelos por etapa** — bloco `pesquisa:` do `services/_shared/perfis.yml`:

```yaml
pesquisa:
  modelo_notas: "gemma4:e4b"          # plano e notas de cada página
  modelo_sintese: "qwen3.6:35b-a3b"   # só o relatório final (vazio = MODEL_CHAT)
  relevancia_minima: 0.5
```

`RESEARCH_MODEL_NOTAS` / `RESEARCH_MODEL_SINTESE` no `.env` passam por cima (máquina sem o qwen, por exemplo). A
síntese pede `num_ctx` 16384 ao Ollama: as notas de 6 subperguntas × 3 páginas passam de 8k tokens.

## Cache em dois níveis

**Nível 1 — Exato (O(1)):** Verifica se o arquivo `VAULT_ALFRED/research/<slug>/synthesis.md` existe, tem conteúdo (síntese vazia não conta) e tem menos de 7 dias. Não faz nenhuma chamada de rede.

**Nível 2 — Semântico (ChromaDB):** Se o cache exato falhar, gera embedding do tópico e busca os 3 documentos mais próximos na coleção `research_alfred`. O limiar é distância coseno ≤ 0.25 (≡ similaridade ≥ 75%). "Kubernetes networking" vai encontrar "redes no Kubernetes" como hit semântico.

O parâmetro `refresh: true` ignora os dois caches e força nova pesquisa.

**Modo do cache:** pedido profundo só aceita síntese profunda (frontmatter `modo: profundo` no cache exato,
metadata `modo: profundo` no ChromaDB); uma síntese rápida do mesmo tema não conta. Pedido rápido aceita as duas.

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
    index.md        ← consultas usadas (modo profundo: subperguntas) e resumo rápido
    synthesis.md    ← síntese gerada pelo LLM (modo profundo: relatório com [n] + "## Fontes")
    sources.md      ← fontes (modo profundo: só as páginas lidas, com subpergunta e fatos extraídos)
```

Os dois modos gravam na mesma pasta; a pesquisa profunda de um tema substitui a rápida (e vice-versa, com `refresh`).

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
fastapi, uvicorn, httpx, pyyaml, beautifulsoup4
```
E as libs compartilhadas: `lib_alfred`, `lib_chromadb`, `perfis`.

`beautifulsoup4` (só o modo profundo usa; parser `html.parser` da biblioteca padrão, sem lxml) entrou na versão 3.2.
O `run.sh` só instala dependências quando cria o venv — num venv que já existe, rode uma vez
`venv/bin/pip install -r requirements.txt` e reinicie o serviço. Sem ela, o modo rápido segue normal e o profundo
termina em `error` dizendo isso.

## Testes

```bash
cd services/researcher
venv/bin/python -m unittest tests.test_profundo -v
```

Offline (Ollama, SearXNG, DNS e httpx trocados por funções locais): plano, filtro, leitura (página grande, timeout,
servidor que pinga devagar, URL privada, redirect para IP privado, PDF), síntese com citações, cache por modo,
progresso/`interrompido` e o modo rápido. Os testes antigos do researcher continuam em
`services/inbox/tests/test_servicos.py` (rodam no venv do inbox, que não tem `beautifulsoup4`).

## Variáveis de ambiente

| Variável | Uso |
|----------|-----|
| `SHELL_EXECUTOR_TOKEN` | Token Bearer de autenticação |
| `VAULT_ALFRED` | Caminho do vault para salvar pesquisas |
| `WEBUI_API_TOKEN` | Token para sync da Knowledge Base no Open WebUI |
| `WEBUI_URL` | URL do Open WebUI (default: `http://localhost:3000`) |
| `PORT_RESEARCHER` | Porta do serviço (default: `7071`) |
| `RESEARCH_MODEL_NOTAS` | Modo profundo: modelo do plano e das notas (sobrescreve `pesquisa.modelo_notas`; padrão `gemma4:e4b`) |
| `RESEARCH_MODEL_SINTESE` | Modo profundo: modelo do relatório (sobrescreve `pesquisa.modelo_sintese`; padrão `MODEL_CHAT`) |
