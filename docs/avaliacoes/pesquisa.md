# Avaliação — Pesquisa (Odysseus × Alfred)

Medido em 03 e 05/11/2026. Branch `claude/avaliacao-pesquisa-a824c0`, código do Alfred no commit `4c30b84` (`laya`).
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
citação. O que ficou de fora: a data de hoje no plano, rodadas (o plano é fixo, não olha o que já foi achado),
decisão de parar, extração guiada pela pergunta original, priorização por tipo de fonte e domínio,
categoria/formato do relatório, navegador headless, PDF, continuação.

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

**GPU compartilhada e memória** — cada execução roda dentro de `flock /tmp/alfred-bench-gpu.lock`
([`scripts/rodar_bench.sh`](scripts/rodar_bench.sh)), uma por vez e uma pergunta por vez; o script anota a espera
pelo lock e a memória disponível, e os modos `profundo*` só começam com ≥ 10 GB livres (o `qwen3.6:35b-a3b` da
síntese tem 7,5 GB na VRAM e ~14 GB fora dela).

**Quando** — técnico e notícia em 03/11/2026 (05:00–05:35, com as outras 7 avaliações disputando o lock);
produtos, acadêmico, brasil e o `profundo-general` da notícia em 05/11/2026 (02:05–03:40, lock livre, swap cheio).
O reboot de 04/11 apagou os brutos de 03/11 em `/tmp`; os números dessas 10 execuções foram recuperados da
transcrição da sessão para [`scripts/resultados/metricas.jsonl`](scripts/resultados/metricas.jsonl) (campo
`origem`), e as conferências de citação delas já estavam em `julgamento.json`. A partir de 05/11 os brutos ficam em
`scripts/resultados/brutos/` (fora do git: têm texto de páginas da web).

Para reproduzir (na raiz do repositório; num worktree sem venv o script usa o venv do checkout principal):

```bash
docs/avaliacoes/scripts/rodar_bench.sh docs/avaliacoes/scripts/resultados/brutos rapido profundo iterativo rapido-general profundo-general -- tecnico
```

```bash
python3 docs/avaliacoes/scripts/citacoes.py docs/avaliacoes/scripts/resultados/brutos/profundo-tecnico.json
```

```bash
python3 docs/avaliacoes/scripts/resumo.py docs/avaliacoes/scripts/resultados docs/avaliacoes/scripts/julgamento.json
```

```bash
python3 docs/avaliacoes/scripts/busca_categorias.py > docs/avaliacoes/scripts/resultados/busca-categorias.json
```

## 4. Resultados

### 4.1 Busca no SearXNG, sem LLM

Relevantes entre os 10 primeiros resultados (média das consultas em PT e EN; 03/11, antes da suspensão dos
motores). Dados em [`scripts/resultados/busca-categorias.json`](scripts/resultados/busca-categorias.json).

| pergunta | Alfred (`news,it`) | `general` | Odysseus (`general` + `en`) |
|---|---:|---:|---:|
| técnico | 1,0 | 9,5 | 9,5 |
| notícia | 5,5 | 10,0 | 9,5 |
| produtos | 5,0 | 10,0 | 9,5 |
| acadêmico | 0,0 | 6,0 | 10,0 |
| brasil | 5,5 | 9,5 | 10,0 |
| **média** | **3,4** | **9,0** | **9,7** |
| domínios distintos entre os relevantes | 2,7 | 7,6 | 8,5 |

Com `news,it` o SearXNG manda as consultas a MDN, Hackage, StackOverflow e aos motores de notícia: "systemd",
"Pi" e "lottery ticket" viram `Math.PI`, `<em>`, `do...while` e listas de pacotes Haskell.

### 4.2 Custo por modo

Custo: média das execuções ok. Tabela completa por execução em
[`scripts/resultados/tabelas.md`](scripts/resultados/tabelas.md).

| modo | ok / total | tempo (s) | URLs buscadas | páginas lidas | fontes no relatório | tokens entrada | tokens saída | modelos (chamadas por execução) |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `rapido` | 5 / 5 | 40 | 25,6 | 0 | 12 trechos | 1.458 | 899 | `gemma4:12b` ×2 |
| `rapido-general` | 5 / 5 | 47 | 24,0 | 0 | 12 trechos | 1.496 | 960 | `gemma4:12b` ×2 |
| `profundo` | 4 / 5 | 243 | 26,5 | 5,5 | 5,0 | 9.832 | 2.378 | `gemma4:e4b` ×2–15, `qwen3.6:35b-a3b` ×1 |
| `profundo-general` | 5 / 5 | 300 | 33,4 | 12,0 | 11,0 | 25.379 | 4.230 | `gemma4:e4b` ×7–16, `qwen3.6:35b-a3b` ×1 |
| `iterativo` (Odysseus) | 5 / 5 | 338 | 42,2 | 11,4 | 10,8 | 39.285 | 9.390 | `gemma4:12b` ×13–23 |

Tempo por pergunta (s):

| modo | técnico | notícia | produtos | acadêmico | brasil |
|---|---:|---:|---:|---:|---:|
| `rapido` | 27 | 22 | 54 | 47 | 52 |
| `rapido-general` | 54 | 53 | 45 | 48 | 34 |
| `profundo` | 292 | 241 | 213 | 33 (falhou) | 226 |
| `profundo-general` | 362 | 259 | 304 | 306 | 271 |
| `iterativo` | 341 | 244 | 416 | 280 | 412 |

- O filtro de relevância do profundo deixou passar 3, 6, 2, 0 e 27 resultados com `news,it` e 14, 31, 37, 26 e 35
  com `general` (técnico, notícia, produtos, acadêmico, brasil).
- A síntese no `qwen3.6:35b-a3b` levou 140–276 s para 633–2.143 tokens de saída (4–18 tokens/s descontada a carga,
  com 2/3 do modelo na CPU); só a carga do qwen custa 54–66 s por pesquisa, porque o `gemma4:e4b` das notas ocupa a
  GPU antes.
- Carga de modelo ocupa 26–45% do tempo em todos os modos (o rápido recarrega o `gemma4:12b` que outro serviço
  tirou da GPU; o iterativo recarrega quando o `num_ctx` alterna entre 8.192 e 16.384).

### 4.3 Qualidade

Conferência à mão de até 10 afirmações por relatório (233 no total). Detalhe e observação de cada execução em
[`scripts/julgamento.json`](scripts/julgamento.json) e na tabela completa.

| modo | citações sustentadas (S + ½P) | cobertura dos aspectos | alucinações por relatório |
|---|---:|---:|---:|
| `rapido` | 47% (50) | 48% | 0,2 |
| `rapido-general` | 90% (50) | 78% | 0,0 |
| `profundo` | 94% (35) | 45% | 0,0 |
| `profundo-general` | 92% (50) | 85% | 0,2 |
| `iterativo` (Odysseus) | 72% (48) | 88% | 0,6 |

Cobertura por pergunta (aspectos de 4):

| modo | técnico | notícia | produtos | acadêmico | brasil |
|---|---:|---:|---:|---:|---:|
| `rapido` | 2,5 | 2 | 1 | 2 | 2 |
| `rapido-general` | 3,5 | 4 | 2,5 | 3 | 2,5 |
| `profundo` | 0 | 4 | 1 | 0 (falhou) | 4 |
| `profundo-general` | 4 | 2 | 3 | 4 | 4 |
| `iterativo` | 3 | 4 | 3,5 | 3,5 | 3,5 |

- **Rápido com `news,it`**: no técnico e no acadêmico os 12 trechos não têm nada a ver com o tema e o modelo
  escreve de memória (0 de 20 afirmações sustentadas); no de produtos só 3 trechos (notícias de homelab) tocam o
  tema e nenhum traz desempenho, consumo ou preço. Uma afirmação falsa (exemplo de `StartLimitBurst` que ignora o
  intervalo padrão de 10 s).
- **Profundo com `news,it`**: quando a busca falha ele é honesto — o relatório do técnico diz que a única fonte
  (uma página MDN) não fala de systemd —, mas não responde; no acadêmico nenhum resultado passa no filtro e o job
  termina em erro. Quando a busca acha (notícia, brasil), as citações são as melhores medidas: código numera as
  fontes e monta a lista, o modelo não consegue inventar URL.
- **Profundo com `general`**: cobre 4/4 em técnico, acadêmico e brasil. Uma alucinação: "o padrão de `Restart=` é
  `on-failure`", que veio da nota de uma página do Plesk (padrão do Plesk) atribuída a outras fontes.
- **Notícia, os dois profundos**: o plano não recebe a data de hoje e o `gemma4:e4b` escreveu subperguntas no
  futuro ("tendências", "quem tem chances"). Com `news,it` a sorte trouxe páginas pós-eleição; com `general` as 14
  páginas lidas eram de pré-eleição e o relatório **não diz quem venceu**.

### 4.4 O que o fluxo do Odysseus fez diferente nas mesmas perguntas

Medido com a reconstrução `iterativo`:

| ponto | Odysseus (medido) | Alfred profundo hoje |
|---|---|---|
| busca | `general` + inglês achou fonte boa nas 5 (arXiv no acadêmico, onde o Alfred falhou) | `news,it`: lixo em 3 de 5 |
| data | põe a data de hoje no plano e nas consultas: na notícia buscou "resultado oficial" e acertou os números | sem data: subperguntas no futuro |
| rodadas | com `min_rounds` 2 (padrão do `DeepResearcher`), o LLM mandou parar na rodada 2 em 4 de 5; na 5ª o SearXNG suspenso deu 2 rodadas vazias. No handler da interface (`min_rounds` 18) iria até os 300 s, ~3 rodadas pelos tempos medidos (63–164 s cada) | plano fixo, uma passada |
| formato | relatório ~1.400 palavras em todas; tabela comparativa no de produtos | 590–1.620 palavras; seções por subpergunta |
| citação | o relatório que evolui perde as URLs: 0 URL nos 5 relatórios, "[Descoberta n]" ou "[n]" numerados pela janela das últimas 10 descobertas, que muda entre rodadas; sem lista de fontes. 72% sustentadas | número dado pelo código + lista `## Fontes`: 92–94% |
| erro | 3 afirmações falsas em 48 (padrões do systemd, "RestartSec < 100 ms é ignorado"; "mínimos locais") e 2 contradições com a própria fonte fora da amostra | 1 em 85 |
| custo | +55% tokens de entrada, +122% de saída e +13% de tempo sobre o `profundo-general` | — |

Não medido (a reconstrução não tem): navegador headless para página com JavaScript, leitura de PDF (o Alfred
ignora `application/pdf`; no acadêmico os artigos do arXiv viriam em PDF), continuação de uma pesquisa anterior.

### 4.5 Ruído

- **Lock**: em 03/11 as execuções esperaram 0–656 s (média ~230 s) pelas outras 7 avaliações; em 05/11, 0 s.
- **SearXNG**: a instância só tem 3 motores web em `general` (duckduckgo, google cse, brave). Em 03/11 ~05:30 os
  três foram suspensos ("too many requests", 1 h; duckduckgo "CAPTCHA", 24 h) — o `profundo-general` da notícia
  falhou e o iterativo da notícia teve 2 rodadas vazias; ambos foram refeitos/registrados. Em 05/11 03:40 o brave
  voltou a "too many requests". A suspensão também deixa sem resultado a ferramenta de busca do chat em produção
  (`lib_ferramentas.py:472` usa `general`).
- **Memória**: em 05/11 o swap ficou cheio (8,0 de 8,0 GB) o tempo todo; disponível entre 6,6 e 13,5 GB. Nenhuma
  execução foi pulada pela guarda de 10 GB, nenhuma falhou por memória.
- **Produção**: o `load_duration` mostra recargas de modelo no início do rápido (11–26 s) quando outro serviço
  tinha trocado o modelo da GPU.

## 5. Pontuação

Duas propostas, porque a medição separa o que vale do que não vale:

| | **A. Adaptar: busca `general` com volta para `news,it` + data de hoje no plano** | B. Portar o loop iterativo do Odysseus |
|---|---|---|
| Ganho para Pedro (0–5) | **4** — cobertura do profundo 45% → 85%, fim da falha no acadêmico, citações do rápido 47% → 90%; a data conserta a notícia | 1 — cobertura 88% contra 85% do item A, com citações piores (72%) e 3× mais erros |
| Custo de implementar | **3–4 h**: `services/researcher/main.py` (`_candidatos`, consultas do rápido, prompt de `_plano`), `services/_shared/perfis.yml` (`pesquisa.categorias`), `services/researcher/tests/test_profundo.py`, `services/researcher/about.md` | 12–16 h: reescrever `_pesquisa_profunda` em rodadas, estado do relatório, decisão de parada, numeração estável das fontes entre rodadas, testes |
| Risco (0–5) | **2** — `general` depende de 3 motores que suspendem sob carga (visto 2 vezes em 3 dias); a volta para `news,it` cobre o resultado vazio, não o resultado ruim | 3 — citação sem lista e numerada por janela; mais chamadas, mais pontos de falha |
| Recursos na máquina | 0 VRAM a mais; profundo +24% de tempo (243 → 300 s) e +158% de tokens de entrada no `gemma4:e4b` porque lê mais páginas; rápido +16% (40 → 47 s) | 0 VRAM a mais com um modelo só; +13% de tempo e +55%/+122% de tokens sobre o item A |
| **Nota final (0–10)** | **7** — o que o Odysseus faz de diferente e mede melhor aqui é buscar no lugar certo e saber a data; isso custa poucas linhas | 3 — o loop em si não melhora a resposta com os modelos que cabem na 3060 e piora a rastreabilidade |

## 6. Veredito

**Adaptar.** Trazer do Odysseus a categoria `general` (com volta para `news,it` quando vier vazio) e a data de hoje
no prompt do plano e das consultas. Não portar o loop iterativo, a decisão de parada nem o relatório longo: com
`gemma4:12b`/`gemma4:e4b` o ganho de cobertura sobre o profundo atual com `general` é de 3 pontos percentuais e a
rastreabilidade cai de 92% para 72%. A numeração de fontes pelo código, que o Alfred já tem, é melhor que a do
Odysseus e deve ficar.

**Menor próximo passo:** em `services/researcher/main.py`, passar `categories="general"` nas duas chamadas a
`searxng_search` (`_candidatos` e o laço de consultas do rápido) e repetir com `news,it` quando vier lista vazia;
acrescentar "Hoje é DD/MM/AAAA" no começo do prompt de `_plano` e do prompt de consultas do rápido; um teste em
`tests/test_profundo.py` para a volta; medir de novo com `rodar_bench.sh … rapido profundo`.

Depois, separados e opcionais: ler PDF (o acadêmico ganharia os artigos do arXiv) e acrescentar motores web à
categoria `general` do SearXNG (hoje são 3 e suspendem) — esta é mudança de configuração, fora desta avaliação.
