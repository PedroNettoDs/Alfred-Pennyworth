# Avaliação — Memória (Odysseus × Alfred)

Datas: 03/11/2026 (1ª rodada) e 05/11/2026 (2ª rodada, depois do reboot) · Branch: `claude/avaliacao-memoria-309d9c` · Odysseus lido no commit `c21980f` (clone raso, só leitura)

Pergunta: quanto melhoraria o Alfred se ele adotasse a memória do Odysseus (extração automática a cada
conversa, auditoria periódica e índice vetorial), ou algo parecido?

## 1. O que o Odysseus faz

- **Extração automática** (`services/memory/memory_extractor.py`, `extract_and_store`):
  - Roda em segundo plano depois da resposta do chat, e só a cada 4 pares de mensagens
    (`routes/chat_helpers.py`, `_should_extract`). As chamadas entram numa fila sequencial para não
    disputar o backend com o chat.
  - Pega as 6 últimas mensagens e as achata numa transcrição única, porque mandar os papéis
    alternados fazia o modelo "continuar a conversa" e não extrair nada.
  - O prompt de sistema é em inglês e pede só fatos duráveis **sobre o usuário**: no máximo 2 por
    conversa, frases curtas e JSON com `text`, `category` e `evidence`.
  - **Filtro de evidência** (`_has_user_fact_evidence`): o fato só entra se `evidence` for uma citação
    literal de uma afirmação do usuário. Uma regex em inglês exige "I'm / I am / my X is…", e perguntas
    são recusadas. Com isso, a saída do assistente ou de ferramentas nunca vira fato.
  - **Candidatos por regex** (`_fallback_memory_candidates`): "my name is", "call me", "I live in",
    "I prefer/like/hate", "I want to travel to". Funcionam sem LLM e só em inglês.
- **Deduplicação na entrada**, sem LLM. Na ordem:
  1. vetor parecido com cosseno ≥ 0,72 (`find_similar`): o novo é descartado;
  2. "enriquecimento de nome": um nome mais completo substitui o mais curto;
  3. texto idêntico;
  4. Jaccard ≥ 0,6 sobre tokens de conteúdo, ou contenção (`_is_text_duplicate`).
- **Auditoria** (`audit_memories`):
  - Dispara a cada 5 fatos novos (contador em memória do processo) ou por botão
    (`routes/memory/memory_routes.py`).
  - Manda **todas** as memórias do dono numa chamada só, com um prompt "conservador": funde apenas
    duplicatas verdadeiras, apaga só o inútil e mantém o texto original. Usa `max_tokens` 16384 e
    timeout de 120 s.
  - Recusa um resultado que corte mais de 50% com 8 ou mais entradas.
  - Uma impressão digital sha256 num sidecar (`memory_tidy_state.json`) pula a auditoria quando nada mudou.
  - No fim, reconstrói o índice vetorial.
- **Armazenamento e busca**:
  - Os fatos ficam em `memory.json` (`src/memory.py`, `MemoryManager`, gravação atômica), sem sujeito
    e sem validade.
  - O índice vetorial **hoje é ChromaDB**, não FAISS (`src/memory_vector.py`, coleção com
    `hnsw:space=cosine` em `src/embedding_lanes.py`). O FAISS ficou para trás:
    `scripts/migrate_faiss_to_chroma.py` migra `data/memory_vectors/` (FAISS) para o Chroma.
  - O embedding padrão é `all-MiniLM-L6-v2` (`src/embeddings.py`, 384 dimensões, treinado em inglês).
    É para ele que o limiar de 0,72 foi ajustado.
  - Até 5 fatos de identidade ou contato são "fixados" e entram em todo prompt.

## 2. O que o Alfred faz hoje

- **Biblioteca já portada:** `services/_shared/lib_memoria.py`, com prompts em
  `services/_shared/prompts/memoria_{extrair,deduplicar,auditar}.md` e configuração em
  `services/_shared/perfis.yml`, seção `memoria:`.
- **`extrair(texto, fonte, sujeito)`**:
  - Um portão heurístico barato (`_vale_extrair`) evita chamar o LLM à toa.
  - O prompt é em português e o LLM responde com **JSON Schema** (o `format` do Ollama).
  - Cada fato vem como `{fato, sujeito, tipo, validade}`: guarda fatos de Pedro **e dos contatos**,
    com data de validade.
- **`guardar()`**:
  - Calcula o vetor `bge-m3` de "sujeito: fato" na CPU.
  - Compara só com fatos do **mesmo sujeito**: texto igual é descartado sem LLM; cosseno > 0,85 vai
    para o LLM, que decide `manter_novo`, `manter_antigo` ou `fundir`.
- **`auditar()`**:
  - Apaga os vencidos pela validade, sem LLM.
  - Manda ao LLM lotes de 60 fatos **por sujeito**.
  - Guarda a impressão digital na tabela `estado` e recusa corte de mais de 50%.
- **`contexto()`**:
  - Funde por RRF o vetor (cosseno ≥ 0,45, **calculado em Python puro** sobre os blobs) e o FTS5 (bm25).
  - Lê **todas** as linhas com o vetor a cada consulta.
- **Armazenamento:** SQLite com FTS5. Espelho legível, um `.md` por sujeito, no cofre.
- **Onde está ligada hoje:**
  - `services/inbox/conversa.py:393` chama `contexto()` em **todo turno** do chat.
  - As ferramentas `lembrar`, `buscar_memoria` e `listar_memoria` ficam em
    `services/_shared/lib_ferramentas.py:1117`.
- **O que falta em relação ao Odysseus:**
  - A extração só acontece quando o modelo chama `lembrar`. **Não há extração automática** depois de
    cada conversa, pendência ou e-mail.
  - **Não há agendamento de `auditar()`**: nenhuma unit ou timer chama a auditoria.
  - A memória de produção está vazia.

## 3. Método

Tudo roda contra o Ollama de produção (RTX 3060 de 12 GB), sem ler o `.env` e sem tocar no banco real.
As variáveis imitam o `.env.example`: `OLLAMA_THINK=false`, `OLLAMA_NUM_CTX=8192`, `bge-m3` na CPU, e
`MEMORIA_DB` aponta para um SQLite no scratchpad. Cada etapa que usa o Ollama roda dentro de
`flock /tmp/alfred-bench-gpu.lock`, com um amostrador de ruído ligado (`memoria_ruido.py`, a cada 2 s:
modelos carregados, uso e memória da GPU, carga da CPU).

**Conjuntos sintéticos** (nomes e fatos inventados, sem nada de Pedro):

- `scripts/memoria_trechos.json`: 30 trechos e 33 fatos esperados.
  - 16 conversas Pedro↔Alfred, 8 WhatsApps e 6 e-mails de contatos.
  - 11 trechos não têm fato nenhum: saudação, agenda do dia, senha, newsletter, pergunta cuja resposta
    vem do assistente.
  - Cada fato esperado é um conjunto de grupos de chaves. Há também chaves "aceitáveis" (não contam) e
    "proibidas" (erro grave: agenda, senha, dedução, saída do assistente tomada como fato).
- `scripts/memoria_dedup.json`: 39 entradas, inseridas em ordem.
  - 19 fatos que devem sobreviver, entre eles 4 pares "parecidos mas distintos" e 2 fatos de outra
    pessoa parecidos com fatos de Pedro.
  - 8 duplicatas, 2 contradições (velho → novo), 4 vagos, 4 lixos e 2 vencidos.

**Extração** (`memoria_extracao.py`, `memoria_odysseus.py extrair`, `memoria_pontuar.py`):

- **Alfred:** roda o `lib_memoria.extrair()` de produção, com `guardar()` trocado pela identidade
  (mede só portão + prompt + LLM + limpeza). A conversa vai como transcrição "Pedro: … / Alfred: …".
- **Odysseus:**
  - Roda o `extract_and_store()` dele, importado do clone (nada copiado), num venv vazio com
    `python -I` e ambiente limpo.
  - Os stubs ficam só nas bordas: o cliente LLM vira `/api/chat` do Ollama (`think=false`, mesma
    `num_ctx`), o índice vetorial vira um stub em memória com `bge-m3`, e eventos e esforço viram no-op.
  - Duas variantes saem da mesma chamada: **completo** (o que ele guardaria) e **só LLM** (a lista que
    o LLM devolveu, antes do filtro de evidência).
  - Só nos 16 trechos de conversa, porque o Odysseus só extrai fatos "do usuário".
- **Pontuação:**
  - Precisão = fatos que casam com um esperado / (esses + falsos positivos). Os aceitáveis ficam fora.
  - Revocação = esperados cobertos / esperados.
  - Sujeito estrito para o Alfred. Para comparar com o Odysseus, só o texto ("--ignorar-sujeito"),
    porque ele não tem sujeito.
- `gemma4:12b` e `gemma4:e4b`, 3 rodadas cada (temperatura 0,1).
- Tempo por trecho = relógio de parede da chamada. O aquecimento (carga do modelo) fica à parte.

**Deduplicação e auditoria** (`memoria_dedup.py`, `memoria_odysseus.py deduplicar`):

- **Alfred:** `guardar()` entrada por entrada (bge-m3 + LLM acima de 0,85), depois um `auditar()`.
- **Odysseus:**
  - Mesma sequência de checagens do `extract_and_store`, chamando as funções dele: vetor ≥ 0,72,
    enriquecimento de nome, texto igual e Jaccard. Depois, `audit_memories()` de verdade.
  - Como não tem sujeito, o texto entra como "Sujeito fato".
  - Roda também com limiar 0,85, para separar o efeito do limiar (ajustado para o MiniLM em inglês)
    do resto.
- **Contagem**, sobre as linhas que sobram depois de cada fase:
  - duplicatas eliminadas;
  - **perda de informação**: um grupo cujo fato não aparece em nenhuma linha final do mesmo sujeito.
    Fundir dois fatos numa frase que mantém os dois não é perda;
  - contradições resolvidas: o fato novo presente e o velho não afirmado como atual;
  - vagos, lixos e vencidos removidos;
  - cosseno bge-m3 de cada par.

**Busca** (`memoria_busca.py`, só CPU, sem Ollama):

- Vetores aleatórios de norma 1 e dimensão 1024 (bge-m3) para N de 100 a 100 mil.
- Compara:
  - o laço do `contexto()` em Python puro, usando as próprias `_vetor` e `_cos`;
  - numpy decodificando os blobs a cada consulta;
  - numpy com a matriz em cache;
  - FAISS `IndexFlatIP` (exato) e `IndexHNSWFlat` (M=32, efSearch=64);
  - o `contexto()` de ponta a ponta num SQLite temporário com N fatos e `ollama_embed` trocado por um
    vetor fixo.
- Mediana de 20 repetições (5 com 30 mil e 100 mil), BLAS e FAISS em 1 thread.

**Reproduzir** (de `docs/avaliacoes/scripts/`; `PASTA` = scratchpad com os venvs `venv-bench`, que tem
numpy, faiss-cpu, httpx e pyyaml, e `venv-odysseus`, vazio; `ODY` = clone raso do Odysseus):

```bash
RES=resultados/nova ./memoria_rodar.sh "$PASTA" "$ODY" 3
```

```bash
python3 memoria_pontuar.py memoria_trechos.json resultados/nova/alfred_12b.json resultados/nova/alfred_e4b.json
```

```bash
python3 memoria_pontuar.py memoria_trechos.json resultados/nova/{alfred_12b,alfred_e4b,odysseus_12b_completo,odysseus_12b_so_llm,odysseus_e4b_completo,odysseus_e4b_so_llm}.json --so conversa --ignorar-sujeito
```

```bash
python3 memoria_dedup.py --dados memoria_dedup.json --pontuar resultados/nova/*dedup*.json
```

```bash
OPENBLAS_NUM_THREADS=1 "$PASTA/venv-bench/bin/python" memoria_busca.py --services ../../../services --pasta "$PASTA/busca" --ns 100,500,1000,2000,5000,10000,30000,100000 --saida resultados/nova/busca.json
```

Brutos em `scripts/resultados/rodada-2026-11-05/` (JSON de cada etapa, logs, amostras de ruído e
`resumo.txt`). Uma 1ª rodada em 03/11 deu os mesmos números, com diferença de ±0,02 em P e R; os JSON
dela se perderam no reboot e os resumos ficaram em `scripts/resultados/rodada-2026-11-03.md`.

## 4. Resultados

### 4.1 Extração

Médias de 3 rodadas; entre parênteses, a faixa. "Graves" = fato proibido extraído (agenda, senha,
dedução).

| sistema | modelo | trechos | precisão | revocação | FP/rodada | graves | s/trecho (mediana · p90) |
|---|---|---|---|---|---|---|---|
| Alfred | gemma4:12b | 30, sujeito estrito | **1,00** | **0,97** | 0 | 0 | 1,36 · 2,28 |
| Alfred | gemma4:e4b | 30, sujeito estrito | 0,92 (0,92–0,93) | 0,81 (0,79–0,82) | 2 | 1 | 0,60 · 0,89 |
| Alfred | gemma4:12b | 16 conversas, só texto | 1,00 | 1,00 | 0 | 0 | 1,35 · 2,28 |
| Alfred | gemma4:e4b | 16 conversas, só texto | 1,00 | 0,88 (0,84–0,89) | 0 | 0 | 0,47 · 0,74 |
| Odysseus completo | gemma4:12b | 16 conversas | — (0 fatos) | **0,00** | 0 | 0 | 1,71 · 1,90 |
| Odysseus só LLM | gemma4:12b | 16 conversas | 0,94 | 0,89 | 1 | 0 | 1,71 · 1,90 |
| Odysseus completo | gemma4:e4b | 16 conversas | — (0 fatos) | **0,00** | 0 | 0 | 0,59 · 0,93 |
| Odysseus só LLM | gemma4:e4b | 16 conversas | 0,85 (0,81–0,87) | 0,74 | 2,3 | 0,3 | 0,59 · 0,93 |

- **O Odysseus como é guarda zero fatos em português.**
  - O LLM devolveu 20 fatos (12b) e 15 a 16 (e4b) por rodada, com a evidência citada literalmente em
    português.
  - O filtro `_has_user_fact_evidence` recusou todos, porque a regex de afirmação só aceita inglês
    ("I'm", "my … is").
  - Os candidatos por regex também são só em inglês.
- **Sem o filtro**, o prompt do Odysseus fica abaixo do Alfred:
  - o limite de 2 fatos por conversa corta o 3º fato dos trechos com três (pai, aniversário e cidade;
    esposa, profissão e domingo);
  - ele toma a saudação do assistente como fato ("The user's name is Pedro") e, no e4b, chega a
    escrever "The user's name is Alfred";
  - os fatos saem em inglês.
- **Erros do Alfred 12b:** só um fato de e-mail ficou de fora ("é sócio responsável"). Não houve falso
  positivo nem erro de sujeito. Nos trechos em que a informação vinha do assistente (resposta de
  ferramenta, previsão do tempo), ele não extraiu nada.
- **O e4b é pior nos dois sistemas.** No Alfred:
  - pôs a licença de um contato no sujeito "Pedro";
  - guardou uma consulta marcada (agenda do dia, erro grave) em todas as rodadas;
  - perdeu fatos de trechos com 3 fatos.
- **Custo na GPU:**
  - Uma rodada de 30 trechos leva 44,6 a 45,3 s com o 12b (média de 1,5 s por trecho) e 17,3 a 17,9 s
    com o e4b.
  - O aquecimento com troca de modelo levou 27,4 s para pôr o e4b no lugar do 12b. Na 1ª rodada,
    trocar o qwen3.6:35b de outra avaliação pelo 12b levou 30,9 s.
  - VRAM no pico: ~10,7 GB com o 12b e ~7,0 GB com o e4b, contra uma base sem modelo de ~2,0 GB.

### 4.2 Deduplicação e auditoria

39 entradas: 10 eliminações esperadas (8 duplicatas e 2 versões velhas de contradições), 19 fatos que
precisam sobreviver, 4 vagos, 4 lixos e 2 vencidos. Os números são o estado depois da entrada e depois
de uma auditoria.

| sistema | modelo | fase | elim. certos /10 | perdas de informação /19 | contradições ok /2 | vagos fora /4 | lixo fora /4 | vencidos fora /2 | linhas | tempo |
|---|---|---|---|---|---|---|---|---|---|---|
| Alfred | 12b | entrada | 6 | 0 | 0 | 0 | 0 | 0 | 32 | 11,1 s (6 decisões LLM) |
| Alfred | 12b | + auditoria | **10** | **0** | **2** | 2 | **4** | **2** | 20 | 17,1 s |
| Alfred | e4b | + auditoria | 8 | 1 | 1 | 2 | 4 | 2 | 22 | 9,8 s |
| Odysseus, limiar 0,72 | 12b | entrada | 9 | **6** | 0 | 3* | 0 | 0 | 22 | 3,1 s (sem LLM) |
| Odysseus, limiar 0,72 | 12b | + auditoria | 9 | 6 | 0 | 3* | 0 | 0 | 22 | 24,9 s |
| Odysseus, limiar 0,85 | 12b | + auditoria | 7 | 1 | 0 | 0 | 0 | 0 | 31 | 35,7 s |
| Odysseus, limiar 0,72 | e4b | + auditoria | 9 | 6 | 0 | 3* | 0 | 0 | 22 | 10,8 s |
| Odysseus, limiar 0,85 | e4b | + auditoria | 7 | 1 | 0 | 0 | 0 | 0 | 31 | 14,9 s |

\* Os vagos sumiram por acaso: o vetor ≥ 0,72 os tomou por duplicatas de outros fatos.

- **O limiar 0,72 sem LLM apaga informação com o bge-m3.**
  - Foram 6 de 19 fatos perdidos: "alérgico a camarão" descartado como igual a "vegetariano", Python
    como hobby e no trabalho, café, a mudança para Curitiba (o fato novo de uma contradição foi jogado
    fora) e a preferência financeira por e-mail.
  - Com 0,85 sobra 1 perda: "usa Python no trabalho" virou duplicata de "gosta de programar em Python".
- **A auditoria do Odysseus não mudou nada** (antes = depois) nas quatro variantes. Ficaram os 4 lixos
  ("perguntou sobre a previsão do tempo"…), os vencidos (ele não tem validade) e as duas versões de cada
  contradição.
- **O Alfred 12b chegou ao estado ideal em tudo, menos em 2 dos 4 vagos**, que ficaram como estavam.
  - A auditoria juntou as duplicatas que escaparam da entrada (cosseno 0,83 a 0,85, logo abaixo do
    limiar).
  - Resolveu as 2 contradições e apagou lixo e vencidos.
  - Fundiu dois fatos distintos ("usa Python no trabalho e também gosta de programar na hora vaga") sem
    perder nenhum dos dois.
- **Cosseno bge-m3** ("sujeito: fato"):

  | pares | n | mínimo | mediana | máximo |
  |---|---|---|---|---|
  | duplicatas | 8 | 0,74 | 0,93 | 1,00 |
  | contradições (novo × velho) | 2 | 0,75 | 0,77 | 0,78 |
  | parecidos mas distintos | 4 | 0,63 | 0,79 | 0,88 |
  | fatos de pessoas diferentes | 2 | 0,62 | 0,64 | 0,67 |

  As faixas se sobrepõem: nenhum limiar sozinho separa duplicata de fato distinto. É por isso que o
  passo do LLM no `guardar()` do Alfred, mais a auditoria, faz diferença.

### 4.3 Busca

Mediana em ms (20 repetições; 5 com 30 mil e 100 mil), BLAS e FAISS em 1 thread, carga de CPU entre 4 e 6.
A 1ª rodada (03/11, com carga de 11 a 14) deu números até ~25% maiores no Python e parecidos no resto
(`scripts/resultados/rodada-2026-11-03.md`).

| fatos | Python puro (laço do `contexto`) | **`contexto()` ponta a ponta** | só o SELECT de tudo | numpy (blobs a cada vez) | **numpy (cache)** | FAISS flat | FAISS HNSW | HNSW: construir | HNSW: revocação@8* | RAM dos vetores (MB) |
|---|---|---|---|---|---|---|---|---|---|---|
| 100 | 4,87 | 6,36 | 0,69 | 0,02 | 0,01 | 0,01 | 0,02 | 5,37 | 1,00 | 0,4 |
| 500 | 26 | 31 | 2,87 | 0,72 | 0,08 | 0,09 | 0,09 | 43 | 0,99 | 2,0 |
| 1.000 | 56 | 56 | 4,83 | 1,33 | 0,38 | 0,30 | 0,17 | 119 | 0,99 | 4,1 |
| 2.000 | 105 | 120 | 9,91 | 2,15 | 0,71 | 0,51 | 0,46 | 566 | 0,93 | 8,2 |
| 5.000 | 263 | 299 | 23 | 4,74 | 1,04 | 1,07 | 0,87 | 2.318 | 0,71 | 20,5 |
| 10.000 | 542 | 629 | 46 | 26 | 1,76 | 2,08 | 0,81 | 6.064 | 0,51 | 41,0 |
| 30.000 | 1.693 | 1.815 | 147 | 89 | 5,43 | 6,57 | 1,04 | 24.404 | 0,20 | 122,9 |
| 100.000 | 5.530 | 5.896 | 452 | 259 | 19 | 24 | 1,29 | 98.790 | 0,07 | 409,6 |

\* Revocação do HNSW (efSearch 64) contra a busca exata, com vetores **aleatórios** de 1024 dimensões: é o
pior caso do HNSW e não representa embeddings reais. A latência dele vale; a revocação, não.

- **Python puro custa ~55 µs por fato.** O `contexto()` de hoje passa de 100 ms por volta de **1,7 mil
  fatos** e de 1 s por volta de **16 mil**. Ele roda em todo turno do chat.
- **numpy com a matriz em cache resolve com folga:** 0,4 ms com 1 mil e 19 ms com 100 mil, de 150 a 300
  vezes mais rápido que o Python. O numpy já está no venv do inbox, então não entra dependência nova.
  - O cache também evita o SELECT de todos os vetores a cada consulta, que sozinho custa 46 ms com
    10 mil fatos e 452 ms com 100 mil.
- **FAISS flat empata com o numpy** em todo N (0,30 contra 0,38 ms com 1 mil; 24 contra 19 ms com 100 mil).
  Contra o numpy, ele não compensa em nenhum tamanho.
- **FAISS HNSW** só fica mais de 10 ms à frente do numpy por volta de **100 mil fatos**. Mesmo assim:
  - custa ~99 s para construir o índice;
  - é aproximado;
  - 20 ms por consulta são irrelevantes perto dos segundos do LLM.
- **Resposta à pergunta "a partir de quantos fatos o FAISS compensa":**
  - contra o Python puro, qualquer busca vetorizada compensa já com algumas centenas de fatos, e passa a
    ser necessária perto de 1,5 a 2 mil;
  - o FAISS em si, contra o numpy, só compensa acima de ~100 mil fatos.
- **Quando a memória chega lá:**
  - No conjunto, o 12b tirou 1,2 fato por trecho (36 fatos em 30 trechos), e a auditoria reduziu 39
    entradas a 20.
  - A uns 20 trechos por dia, a memória cresce ~10 fatos líquidos por dia, ~4 mil por ano.
  - Nesse ritmo, o Python puro passa de 100 ms em ~6 meses, e o FAISS só seria assunto em décadas.

### 4.4 Ruído da produção e das outras avaliações

- 2ª rodada: nenhum modelo alheio nas 238 amostras. A carga de CPU ficou com mediana entre 5,6 e 8,5
  nos 12 núcleos.
- 1ª rodada: o qwen3.6:35b de outra avaliação aparece em 1 de 85 amostras, no início da extração 12b,
  e é o que explica o aquecimento de 30,9 s.
- Chamadas da produção com o mesmo `gemma4:12b` não aparecem como "modelo alheio". O indicador delas é
  a variação entre rodadas:
  - 2ª rodada: 44,6 a 45,3 s, ou 1,6% de variação. Não houve interferência que importe.
  - 1ª rodada: 45,5 a 49,7 s, ou 9%. Houve alguma disputa, mas sem mudar P nem R.
- A carga de CPU de outras sessões pesa mais na busca (Python puro) do que na GPU. Por isso a busca vem
  em faixas.

### 4.5 Limites desta medida

- **Os conjuntos são sintéticos e explícitos.** Foram escritos pela mesma família de modelo que
  escreveu os prompts do Alfred, e há temas em comum com os exemplos do prompt (homelab, horário de
  reunião, férias até uma data).
  - É provável que os números do Alfred estejam otimistas em relação a e-mails longos e reais.
  - Com 33 fatos, cada um vale ~3 pontos de revocação.
- **Temperatura 0,1:** as rodadas quase não variam, então a faixa mede pouco do acaso.
- **O Odysseus rodou com stubs nas bordas:**
  - o cliente LLM é o `/api/chat` do Ollama, e não o cliente OpenAI-compatível dele;
  - o vetor usa bge-m3 num stub, e não Chroma com MiniLM;
  - a auditoria rodou uma vez no fim, e não a cada 5 fatos.
  - O prompt em inglês sobre dados em português é o comportamento real dele para Pedro. A variante "só
    LLM" mostra o modelo sem o filtro.

## 5. Pontuação

A pergunta tem duas leituras, e as duas foram medidas: portar a **lógica do Odysseus** como ela é, ou
trazer **a ideia que falta no Alfred**. A lógica do Odysseus perdeu em tudo que foi medido. A ideia que
falta é a extração automática a cada conversa, mais a auditoria agendada.

| peça | o que os números dizem | vale aplicar? |
|---|---|---|
| Gatilho automático depois da conversa (a ideia do `extract_and_store`) | É o que falta: a memória de produção está vazia porque só o `lembrar` a alimenta. O `extrair()` do Alfred com 12b dá P 1,00 / R 0,97 | **sim, adaptado** |
| Extrator do Odysseus (prompt em inglês, evidência por regex, no máximo 2) | R 0,00 em português; sem o filtro, P 0,94 / R 0,89, abaixo do Alfred | não |
| Dedup sem LLM com limiar 0,72 | Perde 6 de 19 fatos com o bge-m3 | não |
| Auditoria do Odysseus | Não mudou nada no conjunto (lixo, vencidos e contradições ficaram) | não. A do Alfred já existe e é melhor, só falta agendar |
| Índice vetorial FAISS | O próprio Odysseus já migrou para o Chroma. O FAISS exato empata com o numpy; o HNSW só ganha com ~100 mil fatos ou mais | não. Trocar o cosseno em Python por numpy em cache |

- **Ganho para Pedro: 4/5.**
  - A memória passa de vazia para alimentada sozinha, e o `contexto()` já entra em todo turno do chat.
  - A precisão medida (1,00 com 12b) indica pouco lixo no prompt.
  - Não é 5 porque o ganho real depende de dados reais, mais ruidosos que o conjunto sintético.
- **Custo de implementar: ~8 a 12 h.**
  - Gatilho na conversa, em segundo plano e sequencial, a cada 4 turnos, com teste: ~3 h. Arquivos:
    `services/inbox/conversa.py` e `services/inbox/tests/test_conversa.py`.
  - Gatilho nas pendências e mensagens respondidas: ~3 a 5 h. Arquivos: `services/inbox/core.py` e os
    testes.
  - Timer semanal do `python -m _shared.lib_memoria auditar`: ~1 h. Arquivos: units
    `alfred-memoria-auditar.{service,timer}` e `services/_shared/about.md`.
  - Cache numpy da matriz de vetores no `contexto()`, invalidado por uma versão gravada na tabela
    `estado` a cada escrita: ~1 a 2 h.
    Arquivos: `services/_shared/lib_memoria.py` e `services/inbox/tests/test_memoria.py`.
- **Risco: 2/5.**
  - Um fato errado entra em todo prompt do chat.
  - Uma extração que rode junto com um turno disputa a GPU, então precisa ser sequencial e depois da
    resposta, como o Odysseus faz.
  - O volume de e-mail pode gerar muitas chamadas; o portão e o Laya já estão previstos para isso.
  - Com e4b, o risco sobe: sujeito errado e agenda tomada como fato.
- **Custo de recursos na máquina:**
  - VRAM: 0 a mais se a extração usar o mesmo `gemma4:12b` do chat. Com o e4b, cada troca custa ~25 s
    de recarga (medido: 24,5 e 27,4 s) e o chat paga a volta.
  - GPU: ~1,4 s por trecho (mediana; p90 2,3 s), em segundo plano. A 20 trechos por dia, dá ~30 s de GPU por dia.
  - Auditoria: ~17 s por 30 fatos, uma vez por semana.
  - CPU: bge-m3 de cada fato novo, ~0,05 s (a dedup do Odysseus fez ~60 embeddings de 39 entradas em
    3,1 s).
  - Latência do chat: nenhuma com o gatilho em segundo plano. Com o numpy em cache, o `contexto()` passa
    de ~60 ms (1 mil fatos) e ~0,6 a 0,9 s (10 mil) para menos de 5 ms.
- **Nota final: 6/10.** Aplicar a lógica do Odysseus como ela é deixaria o Alfred pior (zero fatos em
  português, perda de informação na dedup, auditoria que não limpa). Mas a ideia que ele tem e o Alfred
  não tem, extrair sozinho depois de cada conversa e auditar de tempos em tempos, é exatamente o que
  falta para a memória já portada deixar de ficar vazia.

## 6. Veredito

**Adaptar.** Ligar o gatilho automático e agendar a auditoria usando a `lib_memoria` do próprio Alfred.
Não trazer o extrator, a dedup por limiar nem a auditoria do Odysseus. Trocar o cosseno em Python puro
por numpy em cache antes de a memória passar de ~1,5 mil fatos, e não usar FAISS.

- **Manter o `gemma4:12b`** na extração (é o do chat: sem troca de modelo, P 1,00). O e4b não vale a
  economia de ~1 s.
- **Do Odysseus, vale copiar o comportamento, não o código:**
  - extração em segundo plano, numa fila sequencial, só depois da resposta e a cada N turnos;
  - transcrição achatada numa mensagem só;
  - a regra "saída do assistente não é evidência", que o prompt do Alfred já cumpriu sem regex nos
    testes.

**Menor próximo passo** (~3 h):

- Em `services/inbox/conversa.py`, depois de gravar a resposta, a cada 4 turnos, pôr numa thread
  `lib_memoria.extrair(<transcrição dos últimos 6 turnos>, "chat", "Pedro")` com o mesmo modelo do chat,
  sob um lock que impeça rodar junto com outro turno.
- Teste em `test_conversa.py` com `extrair` mockado.
- Depois, medir por 2 semanas os fatos por dia e a precisão por amostragem (`listar_memoria` no Painel),
  antes de ligar e-mail e WhatsApp.
