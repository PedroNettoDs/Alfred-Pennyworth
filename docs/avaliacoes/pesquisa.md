# Avaliação — Pesquisa (Odysseus × Alfred)

Data: 03/11/2026. Branch `claude/avaliacao-pesquisa-a824c0`, código do Alfred no commit `4c30b84` (`laya`).
Scripts em [`docs/avaliacoes/scripts/`](scripts/). O relatório tem só números e trechos técnicos; nenhum conteúdo
pessoal, e nada foi gravado no cofre real, no ChromaDB nem no índice RAG.

## 1. O que o Odysseus faz

Clone `--depth 1` de `odysseus-dev/odysseus` (AGPL-3.0), lido para entender; nenhum trecho copiado.
O motor é um loop iterativo no estilo IterResearch (o LLM decide o que buscar, o que serve e quando parar):

| etapa | o que faz | onde |
|---|---|---|
| plano | LLM recebe a data de hoje e devolve 3–6 subperguntas, tópicos e um critério de sucesso; a interface pode mostrar o plano antes de começar | `src/deep_research.py` (`_create_plan`), `src/research_handler.py` (`generate_plan`) |
| categoria | regex e depois LLM classificam a pergunta em produto, comparação, how-to ou checagem de fato, e o relatório final ganha um formato próprio (tabela comparativa, ranking, passo a passo, veredito) | `src/deep_research.py` (`_infer_research_category`, `_classify_category`, `CATEGORY_PROMPTS`) |
| rodadas | a cada rodada o LLM escolhe ações limitadas (`web_search`, `web_fetch` de URL conhecida, `browser_read` com navegador headless) olhando o relatório parcial, a cobertura de fontes e o histórico de navegação; sem JSON válido, cai para "gere N consultas" | `_plan_research_actions`, `_generate_queries`, `src/research_navigator.py` |
| busca | SearXNG categoria `general`, idioma fixo em inglês, motores google/brave/duckduckgo; `news` quando a consulta parece notícia | `services/search/providers.py` (`searxng_search_api`) |
| priorização | ordena os resultados por termos em comum com a pergunta e pela nota do tipo de fonte (oficial 90, primária 82, secundária 55, fórum 45, comercial 40), um por domínio primeiro; até 3 páginas por consulta | `_prioritize_search_results`, `assess_source` |
| leitura | baixa a página (10 s, PDF incluído, proteção SSRF), manda até 15.000 caracteres ao LLM com o objetivo = a pergunta original e pede `{rational, evidence, summary}`; descarta extração "sem informação relevante"; página fraca ou só JavaScript tenta o navegador | `_fetch_and_extract`, `src/goal_based_extractor.py`, `services/search/content.py` |
| síntese | um relatório que evolui: reescrito a cada rodada com as últimas 10 descobertas, citação por URL | `_synthesize` |
| parada | portão de profundidade (precisa de fonte primária e ≥ 2 descobertas) e depois o LLM responde SIM/NÃO | `_needs_more_evidence_before_stop`, `_should_stop` |
| relatório | ≥ 1.500 palavras pedidas, citação `[texto](url)`, resumo executivo e conclusão; se vier com < 400 palavras, pede expansão | `_final_report` |
| entorno | job em segundo plano com cancelamento, continuação de uma pesquisa anterior (relatório, descobertas e URLs reaproveitados), página HTML com sumário e imagens, ferramenta para o chat | `src/research_handler.py`, `src/visual_report.py`, `src/tools/research.py` |

Limites padrão: `max_time` 300 s (checado só no começo de cada rodada), `max_rounds` 20 com `min_rounds` 18 no
handler da interface (na prática o tempo decide), teto de 1.800 s no job. Modelos de até 10 B entram num modo
simplificado (sem plano, uma rodada para pergunta factual).

## 2. O que o Alfred faz hoje

`services/researcher/main.py` (v3.2, porta 7072), com as libs de `services/_shared/`:

- **Rápido** (padrão, síncrono): `MODEL_CHAT` (`gemma4:12b`) gera 4 consultas em PT e EN →
  `searxng_search` (8 resultados cada) → dedup por URL → os 12 primeiros **trechos** (até 600 caracteres) vão
  para uma síntese em seções fixas. Nenhuma página é lida e o prompt não pede citação.
- **Profundo** (`"profundo": true`, job em segundo plano, portado do Odysseus em 31/10): plano de 3–6
  subperguntas com `gemma4:e4b` e JSON Schema → por subpergunta, `searxng_search` → dedup → filtro de relevância
  `_relevante` (cosseno `bge-m3` entre subpergunta e título+trecho ≥ 0,5) → até 3 páginas por subpergunta lidas
  por `_shared/lib_web.py` (só IP público, 2 MB, 15 s, só HTML/texto) e BeautifulSoup → nota de até 8 fatos por
  página (`gemma4:e4b`, 8.000 caracteres) → relatório com citações `[n]` (`qwen3.6:35b-a3b`, `num_ctx` 16384) →
  `_limpar_citacoes` tira número inventado → cofre `research/<slug>/`.
- Os dois passam pelo cache exato e semântico (ChromaDB, 7 dias) e indexam o resultado no RAG.
- `searxng_search` (`_shared/lib_alfred.py`) usa **`categories="news,it"`** por padrão, e os dois modos herdam isso.

O que o profundo já trouxe do Odysseus: plano em subperguntas, leitura das páginas, nota por fonte, relatório com
citação. O que ficou de fora: rodadas (o plano é fixo, não olha o que já foi achado), decisão de parar, extração
guiada pela pergunta original, priorização por tipo de fonte e domínio, categoria/formato do relatório,
navegador headless, PDF, continuação.

## 3. Método

**Perguntas** — 5 fixas, em [`scripts/perguntas.json`](scripts/perguntas.json), cada uma com 4 aspectos que
uma boa resposta precisa cobrir (o gabarito foi escrito antes de rodar):

| id | tipo | tema |
|---|---|---|
| `tecnico` | técnico | diretivas `Restart=`, `RestartSec=`, `StartLimitBurst=` do systemd |
| `noticia` | notícia recente | resultado da eleição presidencial de 2026 no Brasil |
| `produtos` | comparação de produtos | Raspberry Pi 5 × Orange Pi 5 para servidor doméstico |
| `academico` | conceito acadêmico | hipótese do bilhete de loteria (lottery ticket hypothesis) |
| `brasil` | tema local | Pix Automático do Banco Central |

**Modos medidos** — todos pelas funções Python do researcher, num processo de teste
([`scripts/bench_pesquisa.py`](scripts/bench_pesquisa.py)), com `VAULT_ALFRED` e `JOBS_DIR` num diretório
temporário e ChromaDB/índice RAG desligados:

| modo | o que é |
|---|---|
| `rapido` | `main.research()` como está |
| `profundo` | `main._pesquisa_profunda()` como está (`gemma4:e4b` + `qwen3.6:35b-a3b`, do `perfis.yml`) |
| `rapido-general`, `profundo-general` | os mesmos, com uma única troca: SearXNG na categoria `general` em vez de `news,it` |
| `iterativo` | o fluxo do Odysseus reconstruído em sala limpa ([`scripts/fluxo_iterativo.py`](scripts/fluxo_iterativo.py)) com as libs do Alfred e os parâmetros padrão dele: rodadas até 300 s, 3 consultas por rodada, priorização por tipo de fonte e domínio, extração guiada pela pergunta (15.000 caracteres), relatório que evolui, portão de profundidade + decisão SIM/NÃO, relatório final ≥ 1.500 palavras; SearXNG `general` + `language=en` + motores google/brave/duckduckgo; um modelo só (`gemma4:12b`) |

Rodar o código do próprio Odysseus num venv isolado foi tentado e **bloqueado pelo classificador de permissões
desta sessão** (código externo); por isso a reconstrução. Ela não tem o navegador headless (fallback para página
com JavaScript), as ações `web_fetch` de URL conhecida nem a extração em paralelo (no Ollama da 3060 os pedidos
fazem fila de qualquer jeito).

**Medidas por execução** — tempo total; buscas no SearXNG; URLs distintas devolvidas ("fontes buscadas");
páginas baixadas e fontes que chegaram ao relatório ("fontes lidas"); tokens de entrada e saída
(`prompt_eval_count`/`eval_count` do Ollama, capturados por um `httpx.Client` instrumentado só na `lib_alfred`);
modelos e número de chamadas; tempo de carga de modelo (`load_duration`); espera pelo `flock`.

**Qualidade** — [`scripts/citacoes.py`](scripts/citacoes.py) escolhe 10 frases com citação, espaçadas por
igual, e mostra ao lado a nota/descoberta que o modelo recebeu e o trecho da página baixada com mais termos em
comum. A conferência foi feita à mão e está em [`scripts/julgamento.json`](scripts/julgamento.json):

- **citação**: S (a fonte citada diz aquilo), P (diz parte), N (não diz), F (fonte que não existe na lista).
  O modo rápido não cita; nele, S = algum dos 12 trechos do SearXNG diz aquilo.
- **cobertura**: cada um dos 4 aspectos do gabarito vale 1 (respondido com fato concreto), 0,5 (mencionado
  vagamente) ou 0.
- **alucinação**: afirmação conferida que nenhuma fonte da execução sustenta **e** que é falsa ou inverificável.
  Afirmação certa tirada da memória do modelo conta como N na citação, mas não como alucinação.

**Busca isolada** — [`scripts/busca_categorias.py`](scripts/busca_categorias.py) mede, sem LLM, quantos dos 10
primeiros resultados do SearXNG contêm o termo-chave de cada pergunta, nas configurações do Alfred
(`news,it`), `general` e do Odysseus.

**GPU compartilhada** — cada execução roda dentro de `flock /tmp/alfred-bench-gpu.lock`
([`scripts/rodar_bench.sh`](scripts/rodar_bench.sh)), uma por vez; a espera pelo lock e o `load_duration` de cada
chamada registram o ruído das outras 7 avaliações e da produção.

Para reproduzir (na raiz do repositório; `PY_RESEARCHER` aponta para um venv com as dependências do researcher):

```bash
docs/avaliacoes/scripts/rodar_bench.sh /tmp/bench rapido profundo iterativo rapido-general profundo-general
```

```bash
python3 docs/avaliacoes/scripts/citacoes.py /tmp/bench/profundo-tecnico.json
```

```bash
python3 docs/avaliacoes/scripts/resumo.py /tmp/bench docs/avaliacoes/scripts/julgamento.json
```

```bash
python3 docs/avaliacoes/scripts/busca_categorias.py > /tmp/busca.json
```
