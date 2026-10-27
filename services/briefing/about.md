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
Rodízio entre as fontes (cada feed RSS / query SearXNG) e corte em max_items × 2
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
mark_seen() + save_seen()  → atualiza logs/briefing_seen.json
    ↓
[se formato incluir áudio]
  render_template(audio_tecnico.md / audio_executivo.md)
  ollama_generate() → roteiro de áudio
  edge-tts → .mp3 no vault (falha no TTS: loga, apaga o .mp3 parcial e segue sem áudio)
```

O corte em `max_items × 2` acontece depois de um **rodízio** entre as fontes de coleta (1º item de cada feed/query,
depois o 2º de cada...): antes era pela ordem de coleta e só as primeiras fontes do `perfis.yml` entravam (no
matinal, 6 dos 10 feeds RSS; no vespertino, nenhum RSS). A memória de 24h é gravada logo depois do `.md` — uma falha
no áudio não faz a próxima execução repetir as mesmas notícias como novas.

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
- Logo após salvar o `.md`, todas as URLs (novas + continuações) são marcadas com timestamp atual.

## Modo ad-hoc

Se o argumento não for um briefing nomeado em `perfis.yml`, o script trata como tema livre:
- Busca no SearXNG com `categories="general,news"`.
- Usa o template neutro `briefing_adhoc.md` (sem perfil de usuário).
- Gera áudio **sempre**, com `audio_adhoc.md` (não há variável para desligar; falha no TTS só deixa o briefing sem áudio).
- Salva em `VAULT_ALFRED/briefings/AAAA-MM-DD_HHMM_adhoc_<slug-do-tema>.md` (+ `.mp3`).

## Saída no vault

Cada execução produz arquivos nomeados como:
```
VAULT_ALFRED/briefings/
  2026-04-10_0700_matinal_tech_<slug-do-titulo>.md
  2026-04-10_0700_matinal_tech_<slug-do-titulo>.mp3  (se formato incluir áudio)
  2026-04-10_1532_adhoc_<slug-do-tema>.md            (modo ad-hoc)
```

Hora e nome do briefing no nome do arquivo (desde 26/10/2026; antes era `AAAA-MM-DD_<slug>.md`): matinal e
vespertino do mesmo dia, ou o mesmo tema ad-hoc pedido duas vezes, não se sobrescrevem. O prefixo `AAAA-MM-DD_`
continua — é o que o podcast do inbox (`semanal.py`) usa para achar os briefings do período.

O `.md` tem frontmatter YAML (`title`, `date`, `time`, `briefing`, `perfil`, `sources`) seguido do corpo gerado pelo LLM e uma seção "## Fontes citadas" (ad-hoc: "## Fontes coletadas") com os links.

## Dependências

```
httpx, feedparser, pyyaml, edge-tts, scikit-learn, numpy
```
E as libs compartilhadas: `lib_alfred`, `lib_templates`, `perfis`.

## Configuração relevante

- `services/briefing/config.yml` — schedule, tts_voice padrão, max_items (legado, a maioria migrou para `perfis.yml`)
- `services/_shared/perfis.yml` — fontes, perfis, briefings nomeados
- `services/_shared/prompts/briefing_tecnico.md` — template principal do matinal

## Agendamento (units de usuário)

Desde 20/10/2026 as units são de **usuário** (mesmo padrão do inbox: sem sudo, com acesso à sessão gráfica
para o `notify-send`). Os `.service` deste diretório são **modelos**: o `services/scripts/instalar.sh` troca
`@ALFRED_DIR@` pelo caminho do repositório, grava as units em `~/.config/systemd/user/` e habilita os timers — não
copie os arquivos direto (ver [README → Setup](../../README.md#4-setup)).

```bash
services/scripts/instalar.sh                                    # na raiz do repositório: gera e habilita as units
systemctl --user list-timers 'alfred-briefing-*'                # matinal_tech 07:00 · vespertino_mercado 18:00
systemctl --user start alfred-briefing-matinal.service          # roda agora
journalctl --user -fu alfred-briefing-matinal
systemctl --user disable --now alfred-briefing-vespertino.timer # desligar o vespertino (AttanoTech)
```

Numa instalação nova os dois timers são habilitados. Timer que já estava instalado e **desabilitado** continua
desabilitado — é o caso do vespertino na máquina original, onde só o matinal está ativo; o `instalar.sh` mantém e
avisa. Se o `alfred-briefing-vespertino.timer` nunca tinha sido copiado para `~/.config/systemd/user/`, o
`instalar.sh` o instala e habilita como qualquer outro: desligue com o último comando acima. O
`alfred-switch.sh off` (modo jogo) para os dois briefings e o `on` só religa os timers habilitados.

Sob demanda pelo chat: tool **Alfred Tech Briefing** (`tools/alfred_briefing.py`), que roda
`<alfred_dir_host>/services/briefing/run.sh` pelo shell executor — Valves `alfred_dir_host` (pasta do repositório)
e `vault_alfred_host` (`VAULT_ALFRED` do `.env`); ver [README → Tools](../../README.md#configurar-os-tokens-nas-valves).

O podcast do Alfred (`services/inbox`, a cada 3 dias) usa estes briefings como fonte de notícias além das
newsletters do e-mail (`perfis.yml → inbox.<nome>.podcast.incluir_briefings`).
