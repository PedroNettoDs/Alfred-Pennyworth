# Avaliação — Skills (procedimentos reutilizáveis do agente)

Datas: 03/11/2026 (demanda, seletores, risco) e 04/11/2026 (bench de tokens). Odysseus lido no commit
`c21980f` (clone raso de 03/11/2026). Nenhum trecho foi copiado. Branch: `claude/avaliacao-skills-544700`.

Onde está cada coisa:

- scripts: `docs/avaliacoes/scripts/skills_*.py`;
- skills, pedidos e esquemas de teste: `docs/avaliacoes/scripts/skills_proto/`;
- resultados brutos: `docs/avaliacoes/scripts/skills_proto/resultados/` e `skills_proto/risco.json`.

Os brutos de `embed` e `llm` (seletor) estavam no scratchpad e se perderam no reboot de 03/11 às 05:43. Os
números deles na seção 4b vêm da saída do `resumo` de 03/11 às 05:03. O léxico e o Laya foram rodados de novo e
deram o mesmo resultado.

## 1. O que o Odysseus faz

No Odysseus, uma skill é **memória procedural em texto**, não código executável. É um arquivo
`data/skills/<categoria>/<nome>/SKILL.md` com frontmatter YAML (nome, descrição, categoria, tags, status
`draft`/`published`, confiança 0–1, origem `builtin`/`learned`/`imported`, toolsets exigidos) e um corpo com
quatro seções: Quando usar, Procedimento, Armadilhas e Verificação. A skill não roda nada sozinha: o agente lê o
texto e segue os passos com as ferramentas que já tem (terminal, web, e-mail...).

- **Armazenamento e índice** (`services/memory/skills.py`, `services/memory/skill_format.py`): lê e grava os
  `SKILL.md` e guarda contadores de uso num `_usage.json` à parte. `index_for()` monta a lista curta
  (nome + descrição) que entra no prompt. Só entram skills embutidas e skills do usuário publicadas, com
  auditoria aprovada e confiança acima do limiar do usuário (padrão 0,85). Também filtra por plataforma, por
  toolsets ativos e pela tag `remote-only`, que esconde a skill de modelos locais.
- **Divulgação progressiva** (`src/chat_processor.py`, `src/tools/system.py`): no modo agente, o prompt leva só
  o índice. O modelo chama `manage_skills(action="view", name=...)` para carregar o procedimento inteiro quando
  precisa. A mesma ferramenta também lista, busca, cria, edita, aplica patch, publica e apaga skills.
- **Busca** (`skills.py: get_relevant_skills`, `src/skill_index.py`): junta Jaccard léxico com reforço por tag
  e cosseno de embeddings (fastembed já carregado; nunca carrega modelo a frio). Os limiares são 0,3 (léxico) e
  0,4 (semântico).
- **Extração automática** (`services/memory/skill_extractor.py`): depois de um turno com 2 ou mais voltas ou 2
  ou mais chamadas de ferramenta, um LLM recebe as 12 últimas mensagens e devolve `null` ou um JSON (título,
  problema, solução, 3 a 7 passos, tags, confiança). Abaixo de 0,6 de confiança ou com título repetido, a skill
  é descartada. O que sobra entra como `draft`.
- **Auditoria** (`services/skills/audit_workflow.py`, `agent_runner.py`, `evaluation.py`, `publication.py`,
  `services/memory/skill_lifecycle.py`): uma fila de no máximo 8 skills por vez. Cada skill passa por
  deduplicação, teste num agente, julgamento, autoedição e reteste, e por fim um modelo "professor" corrige o
  que falhou. Só depois disso a skill é publicada. As verificações inconclusivas voltam em 1 dia, as reprovadas
  em 7.
- **Importação** (`services/memory/skill_importer.py`): baixa o pacote de uma skill do GitHub ou do skills.sh.
  Tem defesa contra SSRF com DNS fixado, limite de 64 arquivos e de 2 MB, e caminhos confinados.
- **Rotas e UI** (`routes/skills_routes.py`, `static/js/skills.js`): listar, editar, testar e auditar.
- **Tamanho:** 5 099 linhas Python só nos módulos de skill, mais 45 arquivos de teste com "skill" no nome. Vêm
  12 skills embutidas e genéricas em `resources/skills/` (por exemplo terminal-recovery e verified-state-change).

## 2. O que o Alfred faz hoje

O Alfred não tem skills. Os três pontos mais próximos:

- **`services/_shared/lib_ferramentas.py`**: tem 35 ferramentas registradas com `@ferramenta`. Delas, 6 são
  perigosas e pedem o clique "Executar? [Sim] [Não]": `executar_comando`, `responder_pendencia`,
  `responder_limpeza`, `completar_tarefa`, `criar_compromisso` e `salvar_nota`. Algumas já são
  "procedimentos congelados em código". Por exemplo, `relatorio_sistema` roda lscpu, uptime, free, df,
  nvidia-smi e docker ps numa chamada, e `resumo_do_dia` faz o mesmo para o dia. Os 35 esquemas somam 17 254
  caracteres de JSON e vão inteiros em toda chamada com ferramentas.
- **`services/shell-executor/main.py`**: allowlist por prefixo, palavra a palavra, com regras extras para
  docker (só leitura), curl (só localhost), find (sem `-exec`/`-delete`), cat (só `/proc` informativo), grep
  (sem recursão), ollama (só leitura) e caminhos sensíveis (`.env*`, `~/.ssh`, perfis de navegador e de
  e-mail). O pipeline é montado sem shell. Nada de reiniciar, apagar ou instalar.
- **`services/inbox/conversa.py`**: motor do chat. O system leva a personalidade
  (`docs/alfred-system-prompt.md`), o bloco "Agora", a memória e o RAG. O Ollama `/api/chat` recebe
  `tools=esquemas()` e `num_ctx` 16384, com até 6 voltas de ferramenta por turno; a chamada seguinte vai sem
  tools. O histórico fica em `VAULT_ALFRED/conversas.db`. Chamadas de ferramenta de turnos anteriores não voltam
  ao contexto.
- Fora do chat, os procedimentos repetidos do Alfred já são **código**: inbox, briefing, jornal, limpeza,
  pesquisa, cada um com seu timer systemd. Pedro fala com o Alfred pelo WhatsApp de dois jeitos: responde a uma
  pendência citando-a (fluxo fixo) ou manda uma nota para si mesmo. Não existe pedido livre ao agente por esse
  canal.

### Mapa: cada peça do Odysseus na stack do Alfred

| Peça do Odysseus | Equivalente no Alfred | O que custaria aqui |
|---|---|---|
| `SKILL.md` em `data/skills/` | `VAULT_ALFRED/skills/*.md`, legível e editável no Obsidian, como a memória | quase nada: o protótipo em `skills_proto/skills/` já segue o formato |
| índice no prompt + `manage_skills(view)` | `conversa.montar_contexto()` + uma ferramenta `ver_skill` em `lib_ferramentas` | o índice de 10 skills tem cerca de 1 000 caracteres (~300 tokens) em toda chamada; carregar sob demanda custa 1 volta a mais (~7 000 tokens avaliados por volta, veja 4c) |
| busca léxica + fastembed já carregado | `lib_rag`/bge-m3 na CPU (`OLLAMA_EMBED_NUM_GPU=0`), ou o próprio gemma4:12b como seletor | bge-m3: 62 ms na CPU, 74% de acerto; gemma4:12b: 0,77 s e ~400 tokens, 97% de acerto (veja 4b) |
| Laya como seletor (ideia da Fase 4 do plano) | `lib_laya.escolher` | zero-shot acerta 21%; precisaria de `laya-train` com pedidos reais, que não existem (demanda zero, veja 4a) |
| extrator pós-turno (≥ 2 voltas ou ≥ 2 ferramentas) | uma chamada a mais ao gemma4:12b no fim de `conversa._turno` | cerca de 1 a 3 mil tokens por turno elegível, na mesma GPU que a fila do WhatsApp (`conversa.ocupado()` segura a fila) |
| auditoria (testar no agente, julgar, autoeditar, professor) | rodar a skill no próprio gemma4:12b; o "professor" seria o qwen3.6:35b-a3b, que não cabe junto (22 GB, metade na CPU) | dezenas de chamadas por skill e troca de modelo na 3060; é a peça mais cara e a que mais dá trabalho |
| importador (GitHub, skills.sh) | nenhum. Baixar procedimentos de terceiros fere o princípio "100% local" e é vetor de prompt injection | fora |
| `requires_toolsets` (esconder skill sem as ferramentas) | o inverso também serve: escolhida a skill, mandar só as ferramentas dela | medido em 4c: é o maior ganho do protótipo |
| aprovação de skill (draft → published) | o Painel (Jinja), com uma aba "Skills" | 1 template + rotas em `painel.py` |
| execução dos passos | `lib_ferramentas` + `shell-executor` com allowlist + cartão "Executar?" | já existe; a allowlist recusa os passos de "conserto" (veja 4d) |

## 3. Método

Tudo foi rodado em 03/11/2026, de madrugada, junto com as outras 7 avaliações. A carga de CPU estava em 15 a 19
em 12 núcleos. As medições com Ollama rodaram dentro de `flock /tmp/alfred-bench-gpu.lock`, um lock por pedido
para não prender a GPU. Nada do Alfred foi importado nem iniciado: os scripts falam HTTP direto com o Ollama
(127.0.0.1:11434) e com o laya-serve (127.0.0.1:7074) e não carregam o `.env`. A única exceção é
`is_allowed()`, que vem do `services/shell-executor/main.py`.

**a) Demanda real** (`skills_demanda.py`). Fontes:

- `VAULT_ALFRED/conversas.db`, aberto só para leitura e só com contagens: turnos, ferramentas por turno e
  sequências repetidas;
- o journal das units `alfred-executor` (contagem de `POST /execute`) e `alfred-inbox` (falhas de ferramenta);
- `~/.bash_history`: comandos ligados a Alfred, Ollama, Docker e systemd, e n-gramas de 2 a 4 comandos
  seguidos que se repetem 3 vezes ou mais.

A pasta `logs/` do repositório só tem estado de fila e caches; o executor não grava log ali. Não existe
histórico do Open WebUI: `data/open-webui/` nunca existiu nesta máquina (`docs/planos/remover-openwebui.md`).
Uma sequência "vira skill" quando cumpre quatro condições: tem 2 passos distintos ou mais, aparece 3 vezes ou
mais, o agente do Alfred conseguiria executá-la (ferramenta ou allowlist) e ainda não é uma ferramenta única.

**b) Protótipo descartável.** São 10 skills em `.md` com frontmatter (`nome`, `descricao`, `gatilhos`,
`ferramentas`, `comandos`, `perigosa`) e corpo nas seções Quando usar, Passos, Cuidados e Verificação. Somam
9,1 KB. Cinco cobrem situações de máquina (serviço caído, VRAM, disco, containers, liberar VRAM) e cinco cobrem
a rotina (resumo do dia, preparar conversa, pesquisa para o cofre, triagem de pendências, limpeza da caixa).
Os 34 pedidos de teste (`pedidos.json`) são frases curtas em PT-BR: 26 têm uma skill certa e 8 não têm nenhuma
(conta, tradução, piada, lembrar um fato, adicionar tarefa, ler o jornal). Pedidos e gatilhos foram escritos
pelo mesmo autor (esta avaliação), e isso favorece o seletor léxico.

**c) Seletores** (`skills_seletor.py`). Quatro, e mais uma cascata:

| Seletor | Como escolhe |
|---|---|
| `lexico` | fração das palavras do pedido presentes em nome, descrição e gatilhos; limiar 0,34, fixado antes de rodar |
| `embed` | cosseno do bge-m3 (Ollama `/api/embeddings`, `num_gpu` 0, como o `lib_alfred`); limiar 0,50, fixado antes |
| `laya` | `choice` no `/v1/systemone`, o mesmo payload de `lib_laya.escolher`; abaixo de 0,7 de `answer_confidence` não decide |
| `llm` | gemma4:12b no `/api/chat` com saída JSON restrita ao enum dos nomes, `num_ctx` 16384 (o mesmo da Conversa, para não recarregar o modelo) e temperatura 0 |
| cascata | Laya quando passa do limiar; senão, LLM |

**d) Tokens economizados** (`skills_tokens.py`). Para 10 pedidos, um por skill, o gemma4:12b resolveu o pedido
em três braços:

1. `zero`: personalidade + as 35 ferramentas;
2. `skill`: personalidade + o corpo da skill certa no system + as 35 ferramentas;
3. `skill_ferramentas`: personalidade + o corpo da skill + só as ferramentas da skill.

As ferramentas não rodam: cada chamada recebe uma saída sintética fixa, igual nos três braços, e nada toca
caixa, WhatsApp, cofres nem executor. O loop imita o `conversa._turno`: até 6 voltas com tools e depois uma
sem. Um nonce no início do system obriga a 1ª volta a avaliar o prompt inteiro; as voltas seguintes
reaproveitam o cache, como num turno real. A ordem dos braços alterna entre os pedidos.

Medidas: tokens avaliados (soma de `prompt_eval_count`), tokens gerados, voltas, tempo e carga de modelo (o
ruído). Duas medidas de qualidade completam: a cobertura dos passos essenciais da skill e os comandos que o
executor recusaria.

**e) Risco** (`skills_risco.py`). O script passa três listas por `is_allowed()`, sem executar nada:

- os 15 comandos declarados nas skills do protótipo;
- 22 passos de "conserto" típicos de procedimentos genéricos de recuperação (reiniciar, instalar, apagar,
  atualizar), justamente o que um extrator grava depois de um turno que deu certo;
- 12 comandos que leem ou escrevem por caminhos que a allowlist deixa abertos.

Para reproduzir, da raiz do repositório:

```bash
PY=services/shell-executor/venv/bin/python; S=docs/avaliacoes/scripts
$PY $S/skills_demanda.py --conversas "$VAULT_ALFRED/conversas.db" --historico ~/.bash_history
python3 $S/skills_seletor.py lexico --saida /tmp/sel_lexico.json
python3 $S/skills_seletor.py laya --saida /tmp/sel_laya.json
flock /tmp/alfred-bench-gpu.lock python3 $S/skills_seletor.py embed --saida /tmp/sel_embed.json
flock /tmp/alfred-bench-gpu.lock python3 $S/skills_seletor.py llm --saida /tmp/sel_llm.json
python3 $S/skills_seletor.py resumo /tmp/sel_lexico.json /tmp/sel_laya.json /tmp/sel_embed.json /tmp/sel_llm.json
flock /tmp/alfred-bench-gpu.lock $PY $S/skills_tokens.py calibrar
k=0; for i in 1 4 7 9 11 14 17 19 22 25; do flock /tmp/alfred-bench-gpu.lock $PY $S/skills_tokens.py rodar --id $i --rotacao $k --saida /tmp/tokens.jsonl; k=$(( (k+1) % 3 )); done
$PY $S/skills_tokens.py resumo /tmp/tokens.jsonl
$PY $S/skills_risco.py
```

## 4. Resultados

### 4a. Demanda real

| Fonte | Medida | Valor |
|---|---|---|
| `conversas.db` | conversas / pedidos / dias com uso | 8 / 9 / 1 (todas de 31/10, no teste da sessão que criou o motor) |
| `conversas.db` | chamadas de ferramenta | 10 (`listar_pendencias` 6, `buscar_cofre` 2, `tarefas_vencidas` 1, `ler_nota` 1) |
| `conversas.db` | turnos com 2 ferramentas ou mais (gatilho do extrator do Odysseus) | 1 |
| `conversas.db` | sequências de ferramentas repetidas 2 vezes ou mais | 0 |
| `conversas.db` | tokens de entrada por chamada do chat (média) | 6 174 |
| journal `alfred-executor` (unit criada em 31/10) e journal de usuário inteiro (desde 25/09); o de sistema não tem unit `alfred*` | `POST /execute` | 0 (só `/health`) |
| journal `alfred-inbox` | falhas de ferramenta (`[ferramentas]`) | 0 |
| `~/.bash_history` | comandos / ligados a Alfred, Ollama, Docker ou systemd | 1 268 / 156 |
| `~/.bash_history` | desses 156, quantos passariam na allowlist | 13 |
| `~/.bash_history` | n-gramas repetidos 3 vezes ou mais | 2: `sudo nvidia-ctk runtime > sudo systemctl restart` (instalação do driver no Docker) e `clear > docker compose up` |
| **Total** | **sequências que viram skill** | **0** |

Os dois n-gramas do histórico são de instalação e de outro projeto. Nenhum passa na allowlist (`sudo`,
`docker compose up`) e nenhum é pedido ao agente. Os comandos de shell mais comuns também ficam fora da
allowlist: `docker compose up/down` (30 vezes), `docker exec` (9) e `sudo systemctl restart` (7).

A demanda medida por skills é zero. O chat do Alfred existe desde 31/10 e ainda não teve uso real; o executor
não recebeu nenhum comando em todo o journal guardado, que começa em 25/09.

### 4b. Seletor (34 pedidos: 26 com skill, 8 sem)

| Seletor | Acerto (34) | Acerto nas 26 com skill | Falsos positivos (8 sem skill) | p50 ms | p95 ms |
|---|---|---|---|---|---|
| léxico, limiar 0,34 | 19/34 (56%) | 13/26 | 2/8 | <1 | <1 |
| embed bge-m3 (CPU), limiar 0,50 | 25/34 (74%) | 18/26 | 1/8 | 62 | 89 |
| Laya zero-shot, limiar 0,7 | 7/34 (21%) | 6/26 | 4/8 | 600 a 1 742 (3 rodadas) | 1 981 a 4 010 |
| **LLM gemma4:12b** | **33/34 (97%)** | **25/26** | **0/8** | **772** | **878** |
| cascata Laya → LLM (LLM em 18/34) | 24/34 (71%) | 20/26 | 4/8 | 2 047 | 4 684 |

- **Léxico:** sem limiar, acerta o top-1 em 24 dos 26 pedidos com skill. O limiar fixado antes de rodar corta
  metade deles. Os gatilhos e os pedidos foram escritos pela mesma pessoa, então o número é otimista.
- **Embed:** sem limiar, acerta o top-1 em 22/26. A varredura do limiar dá 0,45 → 27/34 com 2 falsos
  positivos, 0,50 → 25/34 com 1 e 0,55 → 22/34 com 0. Indexar as 10 skills levou 7,0 s com o modelo frio na
  CPU.
- **Laya:** decidiu (confiança ≥ 0,7) só 16 vezes e acertou 7 delas; o top-1 sem limiar acertou 8/34. As três
  rodadas, uma delas depois do reboot das 05:43, deram o mesmo acerto, porque o Laya é determinístico. O p50
  foi de 1 742, 836 e 600 ms e só acompanhou a carga de CPU (19, 15 e 11). Uma pergunta `noul` curta levou 186 ms nessa carga; o
  `about.md` registra cerca de 90 ms com a máquina livre. O resultado repete o do e-mail (28% zero-shot): sem
  treino, o Laya não serve para escolher skill. Além disso, a cascata **piora** o LLM sozinho, porque os 4
  falsos positivos confiantes do Laya nunca chegam ao LLM.
- **LLM:** errou 1 caso, "o searxng não retorna nada", mandado para diagnosticar-servico em vez de
  containers-docker, um erro defensável. O custo foi de 393 tokens de entrada e 11 de saída por escolha
  (prompt de 1 337 caracteres). A primeira chamada pagou 27,7 s de carga, porque outra avaliação tinha deixado o
  qwen3.6:35b-a3b na GPU; nas outras 33, a carga ficou abaixo de 30 ms.

### 4c. Tokens: do zero × com skill

Rodado em 04/11/2026, das 17:13 às 17:24 (carga de CPU de 2 a 4), com o gemma4:12b de produção já carregado e
`num_ctx` 16384. Foram 10 pedidos × 3 braços. Os brutos estão em `skills_proto/resultados/tokens.jsonl`.

Calibração: com o nonce, o prompt da 1ª volta é avaliado inteiro todas as vezes (6 958 tokens em 3 de 3
chamadas). Sem ferramentas, o mesmo prompt dá 2 613 tokens, então **as 35 ferramentas custam 4 345 tokens por
volta**: 62% do prompt. O Ollama conta o prompt inteiro em cada volta, e por isso os tokens avaliados ficam
perto de voltas × tamanho do prompt.

| Braço | Ferramentas no prompt | Prompt da 1ª volta | Tokens avaliados (média) | Tokens gerados | Voltas | Tempo sem carga de modelo | Cobertura dos passos | Comandos recusados pelo executor |
|---|---|---|---|---|---|---|---|---|
| zero | 35 | 6 972 | 19 693 | 224 | 2,8 | 16,7 s | **39%** | 3/11 |
| skill (corpo + 35 ferramentas) | 35 | 7 153 | 22 589 (**+15%**) | 233 | 3,1 | 15,5 s (−7%) | 65% | 2/12 |
| skill_ferramentas (corpo + só as dela) | 1 a 5 | 3 175 | **9 468 (−52%)** | 217* | 2,9 | **10,2 s* (−39%)** | **86%** | 1/12 |

\* sem o pedido 22. Nesse braço o modelo gerou **13 020 tokens e uma resposta vazia, em 333 s**. Com ele, a
média de tokens gerados sobe para 1 497 e a de tempo para 42,5 s.

Cobertura por pedido (zero → skill → skill_ferramentas):

| Pedido | Skill | Cobertura | Tokens avaliados |
|---|---|---|---|
| 1 | diagnosticar-servico | 100 → 100 → 100% | 48 842 → 29 828 → 13 300 |
| 4 | vram-ollama | 50 → 100 → 100% | 14 055 → 21 874 → 9 604 |
| 7 | liberar-vram | 0 → 100 → 100% | 21 314 → 44 470 → 12 754 |
| 9 | espaco-em-disco | 67 → 67 → 100% | 28 578 → 21 743 → 12 950 |
| 11 | containers-docker | 50 → 100 → 100% | 14 073 → 21 811 → 9 415 |
| 14 | resumo-da-manha | 0 → 33 → 33% | 6 963 → 14 290 → 6 604 |
| 17 | preparar-conversa | 25 → 50 → 75% | 21 066 → 28 840 → 7 139 |
| 19 | pesquisa-para-o-cofre | 50 → 50 → 100% | 14 031 → 14 415 → 10 102 |
| 22 | triagem-de-pendencias | 50 → 50 → 50% | 14 022 → 14 332 → 6 670 |
| 25 | limpeza-da-caixa | 0 → 0 → 100% | 13 985 → 14 285 → 6 144 |

O que os números mostram:

- **Injetar a skill e manter as 35 ferramentas não economiza nada.** Gasta 15% mais tokens, porque o modelo
  segue mais passos, e a cobertura sobe de 39% para 65%.
- **O ganho vem de cortar as ferramentas.** Com o corpo da skill e só as ferramentas dela, o custo cai à metade
  e a cobertura vai a 86%. Somando a escolha pelo LLM (≈ 404 tokens e 0,77 s), o saldo é **−50% de tokens e
  −34% de tempo por turno** (11,0 s contra 16,7 s).
- **Do zero, o modelo pula passos e tenta "consertar".** No pedido 25 ("arquiva as promoções"), os braços zero
  e skill chamaram `emails_recentes` em vez de `listar_limpezas`. Entre os comandos recusados pelo executor
  estão `systemctl --user restart alfred-inbox.service`, `systemctl --user restart ollama`,
  `pkill ollama && systemctl --user start ollama` e `sudo du -h -d 1 /`, alguns do braço zero e outros do braço
  skill. É exatamente o tipo de passo que um extrator gravaria como skill.
- **A allowlist recusa até comando inofensivo.** `du -sh ... | sort -h` foi recusado porque `sort` não está na
  lista: o braço skill_ferramentas perdeu uma volta assim.
- **"Bom dia" (pedido 14) quase não aciona a rotina.** Do zero, o modelo só cumprimenta; com a skill, faz 1 dos
  3 passos.
- **Repetibilidade:** o pedido 4, rodado em 03/11 e de novo em 04/11, deu os mesmos tokens (±0,02%). O pedido
  1 no braço zero variou de 4 para 7 voltas (29 348 contra 48 842 tokens). Com temperatura 0, caminhos de
  várias voltas ainda divergem.
- **Ruído:** 3 das 30 execuções pagaram carga de modelo acima de 1 s (46 s no total), porque outra avaliação
  ou a produção tinham trocado o modelo. A coluna de tempo já desconta essa carga.
- **Histórico:** a 1ª tentativa, em 03/11, parou com 2 pedidos feitos (reboot às 05:43), e a 2ª, às 14:39,
  também parou. O runner (`skills_proto/resultados/rodar_bench.sh`) agora pula o que já foi gravado.

### 4d. Risco: comandos que uma skill poderia disparar

Os comandos foram só passados por `is_allowed()`; nenhum foi executado.

| Grupo | Passam na allowlist |
|---|---|
| comandos das 10 skills do protótipo | 15/15 (inclui uma escrita: `curl -X POST .../api/generate` com `keep_alive: 0`, da skill liberar-vram) |
| passos de "conserto" (reiniciar, instalar, apagar, atualizar, `sudo`, `kill`, `pip`, `git pull`, `rm`, `ollama pull/rm/stop`, `docker restart/rm/prune/compose`) | **1/22** (`nvidia-smi --gpu-reset`: o prefixo `nvidia-smi` aceita qualquer argumento, mas sem root o comando falha) |
| escrita ou leitura por brechas da allowlist | **12/12** |

As 12 brechas:

- **Ollama sem autenticação** (`curl -X POST/DELETE http://localhost:11434/...`): descarregar, **apagar**
  (`/api/delete`) e baixar (`/api/pull`) modelos.
- **ChromaDB sem autenticação no compose** (`curl -X DELETE http://localhost:8001/...`): apagar a coleção do
  RAG.
- **Evolution:** `docker inspect alfred-evolution` é leitura permitida e mostra o `Env` do container, onde fica
  `AUTHENTICATION_API_KEY`. Com essa chave, `curl -X POST http://localhost:8081/message/sendText/... -H 'apikey:
  ...'` também passa. Isso é **enviar WhatsApp fora do fluxo de pendências**. A mesma chave é aceita pelo
  webhook do inbox (`services/inbox/main.py:134`).
- **API do inbox** (`curl -X POST http://127.0.0.1:7073/...`): passa na allowlist, mas a API exige Bearer, que
  só existe no `.env`, e devolve 401.
- **Leituras fora do filtro de caminho sensível:** `head ~/.bash_history`, `tail` e `grep` não recursivo em
  qualquer nota dos cofres, e `find / -name '*.db'`.
- `services/briefing/run.sh` com argumentos livres.

O que segura isso hoje é que `executar_comando` é perigosa: na Conversa, cada comando vira um cartão
"Executar? [Sim] [Não]" com o texto do comando. Skills mudam o risco de dois jeitos:

1. **Um extrator automático tende a gravar o "conserto".** Esses comandos ficam 21/22 fora da allowlist, então
   o efeito prático são turnos perdidos em 403 e não dano.
2. **Uma skill importada ou extraída pode carregar um dos 12 comandos acima**, que passam. Uma skill com 3 ou 4
   passos de shell também vira 3 ou 4 cliques por pedido, e isso cansa a aprovação: Pedro passa a clicar "Sim"
   sem ler.

As brechas independem de skills. Corrigi-las cabe a outra frente; esta avaliação não altera o executor.

## 5. Pontuação

Duas opções foram avaliadas. **Porte** é o sistema do Odysseus inteiro: índice, `manage_skills`, extrator,
auditoria, importador e UI. **Adaptação mínima** são skills escritas à mão em `VAULT_ALFRED/skills/*.md`, um
seletor LLM com enum (e embed como reserva), o corpo da skill no system e só as ferramentas dela.

| Critério | Porte do Odysseus | Adaptação mínima |
|---|---|---|
| **Ganho para Pedro (0–5)** | 1 | **2**: no protótipo, a cobertura dos passos foi de 39% para 86% e o custo caiu à metade. Mas a demanda medida é zero: o chat não tem uso real e o executor nunca recebeu um comando. O ganho hoje é potencial. |
| **Custo de implementar** | 50 a 70 h. Novo `_shared/lib_skills.py`; `_shared/lib_ferramentas.py` (ferramenta de skills); `inbox/conversa.py` (índice, injeção, extração pós-turno); `inbox/painel.py` + template + JS (aba Skills); auditoria com agente de teste; `perfis.yml`; testes | **6 a 10 h**. Novo `_shared/lib_skills.py` (carregar `.md`, `escolher()` com enum no gemma4:12b e reserva no bge-m3, ~120 linhas); `inbox/conversa.py` (`_turno`: escolher a skill, injetar o corpo, filtrar `esquemas()`; sem skill, segue como hoje); `_shared/perfis.yml` (`conversa.skills`, desligado por padrão); `inbox/tests/test_skills.py`; as 10 skills do protótipo como ponto de partida |
| **Risco (0–5)** | **4**: o extrator grava os "consertos" que o modelo tenta sozinho (restart, `pkill`, `sudo`; 21/22 dão 403, então o efeito é desperdício). Uma skill importada ou extraída pode carregar um dos 12 comandos de escrita que passam na allowlist (4d). Mais passos de shell por pedido também cansam a aprovação. | **2**: se o seletor errar com ferramentas filtradas, falta ao modelo a ferramenta certa (o LLM errou 1 de 34; dá para mitigar com um conjunto mínimo sempre presente). Houve 1 geração degenerada em 30 (13 020 tokens em 333 s), que hoje também pode acontecer sem skill porque a Conversa não limita `num_predict`. |
| **Recursos** | VRAM +0, mas o extrator faz 1 chamada a mais ao gemma4:12b por turno com ≥ 2 ferramentas, e a auditoria faz dezenas por skill e trocaria o modelo pelo "professor" (qwen3.6:35b-a3b, 22 GB) na 3060 | VRAM +0 (mesmo modelo e mesmo `num_ctx`, sem recarga). Seletor: +0,77 s e +404 tokens por turno. Turno com skill: **−50% de tokens e −34% de tempo** (11,0 s contra 16,7 s). Reserva bge-m3: 62 ms de CPU e ~1,2 GB de RAM |
| **Nota final (0–10)** | 2 | **3** |

**Nota final: 3/10.** O protótipo prova o ganho técnico (metade dos tokens, mais que o dobro da cobertura), mas
Pedro ainda não usa o chat. Aplicar agora seria otimizar zero turnos, e as peças que tornam o Odysseus
"autônomo" (extrator, auditoria, importador) são as mais caras e as mais arriscadas nesta máquina.

## 6. Veredito

**Adaptar, mais tarde, e só a parte mínima.** Não portar extrator, auditoria nem importador. O importador fere
o "100% local" e é vetor de prompt injection. O extrator grava justamente os passos que a allowlist recusa. A
auditoria precisaria de um segundo modelo grande que não cabe junto na 3060.

O que vale levar do Odysseus é o `requires_toolsets` ao contrário: **escolhida a skill, mandar ao modelo só as
ferramentas dela**. Foi isso que deu o ganho, mais do que o texto da skill; o corpo injetado sozinho custou 15% a
mais. O Laya fica fora do seletor até ser treinado com pedidos reais, que hoje não existem.

**Gatilho para fazer:** rodar `skills_demanda.py` depois de 30 dias de uso da aba Conversa. Se aparecerem
3 sequências ou mais de 2 ferramentas ou mais, repetidas 3 vezes ou mais, aplicar a adaptação mínima.

**Menor próximo passo, quando o gatilho disparar** (~1 h, desligado por padrão):

- `lib_skills.escolher(pedido)` com o prompt e o enum de `skills_seletor.py:sel_llm`;
- em `conversa._turno`, se houver skill, `ferramentas = [e for e in esquemas() if nome in skill.ferramentas]`
  e o corpo no system.

Achados que independem de skills, para outras frentes:

- **Allowlist do executor.** 12 caminhos de escrita ou leitura passam (4d): `curl -X/-d` para o Ollama e o
  ChromaDB sem auth, e `docker inspect` mostrando a chave da Evolution. Ao mesmo tempo, comando inofensivo é
  recusado (`| sort`). Nota deixada para a avaliação Shell em
  `side-session-notes/.../avaliacao-skills-allowlist.md`.
- **Conversa sem teto de geração.** `conversa._chamar` (`services/inbox/conversa.py:653`) não passa
  `num_predict`. Uma geração degenerada (13 020 tokens, 333 s, resposta vazia) ocupa a GPU e segura a fila do
  WhatsApp até Pedro parar o turno. O `TIMEOUT_OLLAMA` não pega esse caso, porque o stream continua chegando.
