# briefing — Gerador de briefings diários

Serviço que coleta notícias de fontes RSS e SearXNG, agrupa por tema via clustering semântico, gera um briefing em markdown com Ollama e produz áudio com edge-tts.

## Como rodar

```bash
./run.sh                    # briefing matinal_tech (default)
./run.sh matinal_tech       # briefing nomeado
./run.sh vespertino_mercado # briefing vespertino (perfil AttanoTech)
./run.sh "fusão nuclear"    # modo ad-hoc: tema livre
```

O `run.sh` carrega o `.env` da raiz do projeto, cria o venv se não existir, e executa `tech-briefing.py`.

## Pipeline — modo briefing nomeado

```
perfis.yml
    ↓
Coletar fontes (RSS + SearXNG)
    ↓
Deduplicar por URL/título
    ↓
Memória 24h → separar novas × continuações
    ↓
_cluster_items()     → embeddings dos títulos via nomic-embed + AgglomerativeClustering
_rank_clusters()     → ordena por tamanho × peso_médio_fonte
_build_clustered_news_text() → monta TEMA 1..N (destaques) + lista radar
    ↓
render_template(briefing_tecnico.md / briefing_executivo.md)
    ↓
ollama_generate() → briefing em markdown
    ↓
generate_title_via_llm() → título via JSON (fallback: heurística)
    ↓
Salvar .md no vault (VAULT_ALFRED/briefings/)
    ↓
[se formato incluir áudio]
  render_template(audio_tecnico.md / audio_executivo.md)
  ollama_generate() → roteiro de áudio
  edge-tts → .mp3 no vault
    ↓
mark_seen() + save_seen()  → atualiza logs/briefing_seen.json
```

## Clustering semântico

A função `_cluster_items()` em `tech-briefing.py` agrupa as notícias novas antes de mandá-las ao LLM:

1. Para cada item, usa o título como texto de embedding. Se o título tiver menos de 4 palavras, concatena as 15 primeiras palavras do snippet (fallback para títulos genéricos tipo "Update" ou "New release").
2. Chama `ollama_embed_batch()` — todos os embeddings em um único `httpx.Client`.
3. Itens sem embedding (erro de rede) recebem `cluster_id=-1` (cluster solo).
4. Roda `AgglomerativeClustering(distance_threshold=0.50, metric="cosine", linkage="average")` nos embeddings válidos.
5. Resultado: cada item tem `cluster_id` e `cluster_size`.

O ranqueamento (`_rank_clusters()`) ordena os clusters por `tamanho × peso_médio_das_fontes`. Desempate por data do item mais recente do cluster; se não houver campo de data, usa a posição no array (RSS retorna newest-first).

## Pesos de fontes (`PESOS_FONTES`)

Hardcoded em `tech-briefing.py`, migra para `perfis.yml` no próximo PR:

| Fonte | Peso |
|-------|------|
| MIT News | 1.0 |
| Ars Technica | 0.95 |
| Hacker News | 0.9 |
| The Verge, InfoQ, MIT Tech Review | 0.85 |
| TechCrunch, Lobsters | 0.75 |
| TLDR, Tecnoblog | 0.7 |
| (outras) | 0.5 |

## Memória deslizante de 24h

O arquivo `logs/briefing_seen.json` guarda `{url: iso_timestamp}` das notícias já incluídas nos últimas 24h. A cada execução:
- Entradas com mais de 24h são purgadas automaticamente no carregamento.
- Itens cujas URLs estão em `seen` vão para `continuacoes` (formato curto no template).
- Itens novos passam pelo clustering e vão para `destaques` + `radar`.
- Ao final, todas as URLs (novas + continuações) são marcadas com timestamp atual.

## Modo ad-hoc

Se o argumento não for um briefing nomeado em `perfis.yml`, o script trata como tema livre:
- Busca no SearXNG com `categories="general,news"`.
- Usa o template neutro `briefing_adhoc.md` (sem perfil de usuário).
- Gera áudio com `audio_adhoc.md` se a variável `TTS_ADHOC=true` estiver no `.env`.
- Salva em `VAULT_ALFRED/briefings/` com slug derivado do tema.

## Saída no vault

Cada execução produz arquivos nomeados como:
```
VAULT_ALFRED/briefings/
  2026-04-10_0700_matinal_tech_<slug-do-titulo>.md
  2026-04-10_0700_matinal_tech_<slug-do-titulo>.mp3  (se formato incluir áudio)
```

O `.md` tem frontmatter YAML (`title`, `date`, `time`, `briefing`, `perfil`, `sources`) seguido do corpo gerado pelo LLM e uma seção "## Fontes coletadas" com todos os links.

## Dependências

```
httpx, feedparser, pyyaml, edge-tts, scikit-learn, numpy
```
E as libs compartilhadas: `lib_alfred`, `lib_templates`, `perfis`.

## Configuração relevante

- `services/briefing/config.yml` — schedule, tts_voice padrão, max_items (legado, a maioria migrou para `perfis.yml`)
- `services/_shared/perfis.yml` — fontes, perfis, briefings nomeados
- `services/_shared/prompts/briefing_tecnico.md` — template principal do matinal
