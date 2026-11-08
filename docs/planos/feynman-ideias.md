# Plano — ideias do Feynman para o Alfred

Referência: https://www.feynman.is/ · https://github.com/Companion-Inc/feynman (MIT, v0.5.66, lido em
08/11/2026: site, `README`, `.feynman/SYSTEM.md`, os quatro agentes em `.feynman/agents/`, `prompts/`,
`skills/`, `extensions/research-tools/`, `evals/`).

Feynman é um agente de pesquisa em TypeScript sobre o framework Pi (`@earendil-works/pi-coding-agent`),
feito para modelos de nuvem (GPT, Claude, OAuth). Roda com Ollama como servidor OpenAI-compatível, mas a
própria documentação avisa que modelos locais pequenos pulam os workflows e precisam de 64k de contexto.
O código não serve ao Alfred (Python + FastAPI + systemd, Ollama na RTX 3060, `num_ctx` 16k). As ideias
servem, e este plano lista as que compensam, em ordem de ganho por hora.

**Licença:** Feynman é MIT, como o Alfred. Diferente do Odysseus (AGPL, ver
[odysseus-port.md](odysseus-port.md)), prompts e formatos podem ser copiados e adaptados; o arquivo que
usar um trecho cita a origem num comentário.

## O que o Alfred já tem (não repetir)

- Plano em 3–6 subperguntas, leitura das páginas, nota por fonte, relatório com citação `[n]` numerada
  pelo código e lista `## Fontes` montada pelo código (`services/researcher/main.py`, modo profundo).
- Data de hoje no plano e nas consultas (`_hoje`); busca em `general` com volta para `news,it` (`_buscar`).
- Cache exato e semântico; cofre `research/<slug>/` (`index.md`, `synthesis.md`, `sources.md`); RAG
  local; aba Pesquisar no Painel; botão do OpenNotebook.
- Defesas de rede em `services/_shared/lib_web.py` (só IP público, redirects revalidados, 2 MB, 15 s).

Medição de partida: [docs/avaliacoes/pesquisa.md](../avaliacoes/pesquisa.md) (05/11/2026). O profundo
com `general` cobre 85% dos aspectos do gabarito, 92–94% das citações são sustentadas pela fonte, e a
pergunta acadêmica só não cobre mais porque os artigos do arXiv vêm em PDF, que o Alfred ignora.

## Princípios

1. **Modelos 100% locais.** As APIs de metadados acadêmicos (OpenAlex, Semantic Scholar, arXiv,
   Crossref) são a mesma classe de tráfego que o SearXNG já faz: uma consulta sai, texto público volta;
   nada do cofre, das conversas ou dos relatórios sai da máquina. O alphaXiv (login, IA na nuvem que lê
   o paper) fica fora.
2. **Laya decide, LLM escreve.** Toda decisão sim/não ou de rótulo nasce em `ollama_generate_json` com
   `gemma4:e4b` e tem o ponto de troca para `lib_laya` marcado, para quando houver modelo treinado.
3. **Cada fase tem teste e medida.** Testes offline em `services/researcher/tests/test_profundo.py`
   (mocks de Ollama, SearXNG, DNS e httpx, como os atuais); medida com
   `docs/avaliacoes/scripts/rodar_bench.sh … profundo` antes e depois, nas mesmas perguntas, com o
   resultado commitado em `scripts/resultados/metricas.jsonl`. Uma fase por branch.
4. **Fases independentes**, salvo onde indicado (a 4 usa a 2 para PDF; a 7 mede a 3 e a 6).

## Fase 1 — Bases acadêmicas como fonte

**Feynman:** uma ferramenta, `feynman_science_database_search`, consulta Semantic Scholar (ordenado por
citações), OpenAlex (prefixo `semantic:` para busca por embedding; grafo de citações e referências;
autores), arXiv (só por ID; texto completo em `arxiv.org/html/<id>`), Crossref (DOI), Europe PMC (texto
completo em acesso aberto), PubMed e bioRxiv/medRxiv. Nenhuma chave obrigatória; chave grátis só sobe o
rate limit. IDs estáveis (DOI, arXiv ID) são preservados no relatório. Em 429 do Semantic Scholar, cai
para o OpenAlex.

**Versão Alfred** — `services/_shared/lib_academico.py`:

- `buscar(consulta, fonte="openalex" | "semanticscholar", limite=10) -> list[dict]` com
  `{url, title, content, doi, arxiv_id, ano, autores, citacoes, fonte}`. `url`, `title` e `content`
  (o resumo) seguem o formato de `searxng_search`, para o resultado entrar em `_candidatos` e no filtro
  `_relevante` sem mudança. Endpoints: `https://api.openalex.org/works?search=…`,
  `https://api.semanticscholar.org/graph/v1/paper/search`, `https://export.arxiv.org/api/query?id_list=…`,
  `https://api.crossref.org/works/<doi>`. httpx, timeout 20 s, sem LLM. `User-Agent` próprio;
  `OPENALEX_MAILTO` opcional no `.env` entra no pool educado do OpenAlex. Respeitar o intervalo que cada
  API pede (o arXiv pede 3 s entre pedidos); 429 no Semantic Scholar tenta uma vez e cai para o OpenAlex.
- Roteamento: `SCHEMA_PLANO` ganha `"academico": bool` e o prompt de `_plano` pede a marcação ("o tema
  é tratado em artigos científicos?"). Com `academico: true`, `_candidatos` junta ao SearXNG os
  resultados de `lib_academico.buscar` para a mesma subpergunta antes do filtro. Ponto de troca para o
  Laya: `lib_laya.sim_nao(topic, "é tema acadêmico")`.
- Leitura: para arXiv, a URL preferida é `https://arxiv.org/html/<id>` (HTML, já passa em `lib_web`);
  sem HTML, `https://arxiv.org/abs/<id>` (resumo). O PDF só entra na Fase 2.
- `sources.md` e a lista `## Fontes` mostram DOI ou arXiv ID ao lado da URL; o frontmatter do
  `index.md` ganha `fontes_academicas: N`.
- Ferramenta da conversa `buscar_artigos(consulta, n=5)` em `services/_shared/lib_ferramentas.py`,
  irmã de `buscar_web`.
- `services/_shared/perfis.yml → pesquisa: fontes_academicas: ["openalex", "semanticscholar"]`
  (lista vazia desliga).

**Testes:** `httpx.MockTransport` com uma resposta gravada de cada API; plano com `academico: true`
chama `lib_academico`; 429 cai para o OpenAlex; DOI aparece em `## Fontes`; plano com `academico: false`
não chama nada.

**Pronto quando:** a pergunta `academico` do bench (lottery ticket hypothesis, arXiv 1803.03635) lê ao
menos 3 fontes vindas do arXiv/OpenAlex e cobre 4/4 aspectos; `tecnico`, `brasil` e `produtos` não
pioram em cobertura nem em citações.

**Custo:** 4–6 h.

**Status (08/11/2026):** implementada na branch `laya` (`lib_academico.py`, flag `academico` no plano, artigos na frente dos candidatos, `url_html` do arXiv, IDs em `## Fontes`/`sources.md`/`index.md`, ferramenta `buscar_artigos`, `fontes_academicas` no `perfis.yml`, `OPENALEX_MAILTO`); 21 testes offline novos. Falta medir com o bench (`rodar_bench.sh … profundo -- academico`) contra as duas APIs reais.

## Fase 2 — Leitura de PDF

**Feynman:** `fetch_content` extrai PDF para Markdown num cache em disco (`FEYNMAN_FETCH_CACHE_DIR`); a
skill `pdf-explore` cruza métodos, tabelas e figuras mantendo o número da página; `/summarize` roda
`pdftotext` antes de medir o documento.

**Versão Alfred:**

- `lib_web.baixar` aceita `application/pdf` quando o chamador o incluir em `tipos`. Nova
  `lib_web.extrair_pdf(corpo: bytes) -> list[str]` (texto por página) com `pypdf` (puro Python, BSD),
  acrescentado a `services/researcher/requirements.txt` e `services/inbox/requirements.txt`. Tetos:
  10 MB só para PDF (o `PAGINA_MAX_BYTES` de 2 MB continua para HTML) e 40 páginas (o resto é
  descartado com aviso no log). PDF é dado: o `pypdf` não executa JavaScript; a extração roda numa
  thread com 30 s de teto porque um PDF malformado pode prender o parser; falha vira `ErroPagina`.
- Researcher: `TIPOS_TEXTO` + `application/pdf`; `_extrair_texto` chama `extrair_pdf` e junta as
  páginas com o marcador `[p. N]` no começo de cada uma, para a nota poder citar a página.
  `TEXTO_MAX_CHARS` continua valendo: PDF longo é cortado; a Fase 4 trata o documento inteiro.
- `ler_pagina` (chat) idem: devolve os primeiros `PAGINA_MAX` caracteres com `[p. N]`.
- Cofre (opcional, separado): `lib_rag._notas` passa a incluir `.pdf` de `VAULT_PEDRO`, com o texto
  extraído em cache em `logs/rag-pdf/<hash>.txt` e o mesmo chunking dos `.md`. Ganho para os estudos
  do Pedro (cursos, física), não para a pesquisa.

**Testes:** PDF de duas páginas gerado no próprio teste (`pypdf.PdfWriter`) servido pelo
`MockTransport`; marcadores `[p. 1]`/`[p. 2]`; PDF acima do teto truncado com aviso; bytes inválidos
→ `None`.

**Pronto quando:** o `academico` do bench lê o PDF do arXiv quando não há HTML;
`ler_pagina("https://arxiv.org/pdf/1803.03635")` devolve texto que começa com `[p. 1]`.

**Custo:** 3–4 h (+2 h para o cofre).

## Fase 3 — Verificação das citações

**Feynman:** o agente `verifier` roda depois do rascunho: toda afirmação factual recebe citação; toda URL
é baixada e precisa conter o conteúdo citado ("significado, não só o tema"); DOI/arXiv ID é cruzado em
dois índices (título, ano, primeiro autor); afirmação sem fonte sai ou vira `TODO`; não fica citação
órfã (número sem fonte) nem fonte órfã (fonte sem citação); "verificado/confirmado" só aparece com a
checagem apontada.

**Alfred hoje:** `_limpar_citacoes` só remove número fora de 1..n. A avaliação mediu 6–8% de citações
não sustentadas e uma alucinação ("o padrão de `Restart=` é `on-failure`", padrão do Plesk atribuído a
outras fontes).

**Versão Alfred** — etapa `verificacao` entre `sintese` e `vault` em `_pesquisa_profunda`:

- O código separa o relatório em frases com citação (sentença terminada em `.`, `;` ou `:` seguida de
  um ou mais `[n]`).
- Por frase, `ollama_generate_json(_modelo("notas"), schema={"sustenta": bool})` recebe a frase e **só**
  os fatos da nota `[n]`: "A NOTA sustenta a FRASE? Mesmo sentido, mesmo número, mesma data." A
  verificação é contra a nota, que é o que a síntese viu; não baixa a página de novo.
- Decisão: sustenta → fica. Não sustenta → o `[n]` sai e a frase ganha ` (não verificado)`; frase que
  perde todas as citações é movida para uma seção `## Não verificado` no fim do relatório (não some:
  Pedro vê o que o modelo escreveu sem apoio).
- Fonte que ficou sem nenhuma citação sai da lista `## Fontes` do `synthesis.md` (regra da fonte órfã)
  e continua em `sources.md`.
- `job["contagem"]` ganha `citacoes`, `sustentadas`, `removidas`; frontmatter do `index.md` ganha
  `verificacao: "N/M"`. `andamento_pesquisa` e a aba Pesquisar mostram a linha.
- `perfis.yml → pesquisa: verificar_citacoes: true` (desliga em máquina lenta). Custo de tempo: ~30
  frases × `gemma4:e4b` ≈ 1–2 min a mais no profundo (hoje 4–5 min).
- Ponto de troca para o Laya: `lib_laya.sim_nao({"frase": …, "fatos": […]}, "a nota sustenta a frase")`
  com `confianca_minima`; abaixo dela cai no LLM. É a decisão `noul` típica do
  [odysseus-port.md](odysseus-port.md).

**Testes:** relatório falso com três frases citadas, mock devolve sustenta/sustenta/não → a terceira
perde o `[n]`, ganha a marca e vai para `## Não verificado`; mock tudo `true` → relatório intacto;
fonte órfã some de `synthesis.md` e fica em `sources.md`; `contagem` certa.

**Pronto quando:** `citacoes.py` sobre o `profundo` do bench dá ≥ 98% de S/P (hoje 92–94%), a
alucinação do Plesk aparece em `## Não verificado`, e a cobertura não cai mais de 0,5 ponto em nenhuma
pergunta.

**Custo:** 4–6 h.

## Fase 4 — Resumo de documento em janelas (padrão RLM)

**Feynman `/summarize`:** o documento fica em disco e nunca entra inteiro no contexto. Três faixas por
tamanho em caracteres: abaixo de 8.000, leitura direta; de 8.000 a 60.000, janelas de 6.000 com
sobreposição de 500, e a nota de cada janela é gravada num arquivo antes de ler a próxima (checkpoint:
a sessão pode cair e retomar); acima de 60.000, fatias em paralelo para sub-agentes. Formato fixo:
afirmações-chave, contexto do campo, "dobradiças técnicas", método, limitações, três perguntas de
acompanhamento, veredito, fontes.

**Versão Alfred** — `services/_shared/lib_resumo.py` e a ferramenta `resumir_documento(url_ou_caminho)`:

- Baixa com `lib_web` (HTML, texto, PDF da Fase 2) ou lê um arquivo do cofre; grava o texto em
  `logs/resumos/<slug>-bruto.txt`.
- Abaixo de 8.000 caracteres: um `ollama_generate` direto. Acima: janelas de 6.000/500 (constantes do
  módulo), por janela `ollama_generate_json(_modelo("notas"))` → `{"fatos": […], "parcial_na_borda": bool}`,
  acrescentado a `<slug>-notas.jsonl` antes da próxima janela; a retomada pula janelas já gravadas. A
  síntese final usa `_modelo("sintese")` só com as notas, no formato de
  `services/_shared/prompts/resumo_documento.md` (via `render_template`). Sem paralelo: uma GPU.
- Teto de 60.000 caracteres (10 janelas, ~5 s cada, mais a síntese); acima, corta e avisa no resultado.
- Resultado em `VAULT_ALFRED/research/resumos/<slug>.md` (indexado pelo RAG como os outros); a
  ferramenta devolve o resumo ao chat. É a saída para "resume esse link/PDF" com 16k de contexto —
  hoje `ler_pagina` corta em `PAGINA_MAX` (8.000 caracteres) e o resto nunca é lido.
- Painel: botão "Resumir" na aba Pesquisar (opcional, depois).

**Testes:** texto sintético de 20.000 caracteres → 4 janelas, 4 notas, síntese chamada uma vez; retomada
com 2 notas gravadas processa só as 2 janelas restantes; texto de 5.000 não cria notas.

**Pronto quando:** `resumir_documento` de um artigo do arXiv (~40.000 caracteres) devolve o resumo no
formato em menos de 3 min na 3060, sem erro de contexto.

**Custo:** 4–5 h. Depende da Fase 2 para PDF.

## Fase 5 — Proveniência

**Feynman:** cada relatório sai com `<slug>.provenance.md` ao lado: data, rodadas, fontes consultadas /
aceitas / rejeitadas (mortas, não verificáveis, removidas), `Verification: PASS | PASS WITH NOTES |
BLOCKED`, caminho do plano e dos arquivos intermediários. Falha de ferramenta vira status, não
omissão.

**Alfred:** os números já existem em `job["contagem"]` (`resultados`, `relevantes`, `tentadas`,
`lidas`) e nas URLs vistas; só não são gravados, e os motivos de rejeição ficam só no log.

- `proveniencia.md` em `research/<slug>/`: data e hora; modelo de cada etapa; subperguntas; categoria
  do SearXNG e fontes acadêmicas usadas; funil de fontes (devolvidas × passaram no filtro × tentadas ×
  lidas × citadas); URLs rejeitadas com motivo (`filtro`, `tipo`, `HTTP 404`, `sem texto`, `nota vazia`
  — guardar em `job["rejeitadas"]` onde hoje só há `log()`); verificação da Fase 3 (`N de M sustentadas`
  ou `não executada`); `status: completo | parcial` (parcial quando alguma subpergunta ficou sem nota).
- `index.md` linka o arquivo; `pesquisa.py` (aba Pesquisar) e `andamento_pesquisa` mostram a linha do
  funil no fim.
- O modo rápido grava o mesmo arquivo com `páginas lidas: 0` explícito — hoje ele sintetiza 12 trechos
  do SearXNG sem ler página nenhuma, e nada diz isso a quem lê.

**Testes:** arquivo existe com o funil e as rejeitadas; `status: parcial` quando uma subpergunta fica sem
nota.

**Pronto quando:** `proveniencia.md` reproduz os números que `bench_pesquisa.py` coleta hoje por
instrumentação; o bench passa a lê-lo.

**Custo:** 1–2 h.

## Fase 6 — Regras de integridade no prompt

**Feynman (`SYSTEM.md` e agentes):** evidência acima de fluência; separar observação de inferência e
marcar a inferência; "verificado/confirmado/reproduzido" só com a checagem apontada; falha de ferramenta
vira status `Bloqueado`/`Não verificado`, não omissão; nenhum número, tabela ou comparação sem fonte;
"URL ou não aconteceu".

**Alfred:** `_sintese_profunda` tem "Não invente nada que não esteja nas notas";
`docs/alfred-system-prompt.md` tem "nunca invente resultado de ferramenta".

- Prompt da síntese: "Quando uma conclusão for sua, e não de uma nota, escreva (inferência) depois
  dela. Número, data ou nome só com citação. Subpergunta sem nota: escreva 'Sem fonte encontrada' na
  seção, não responda de memória."
- `docs/alfred-system-prompt.md`: "diga 'verifiquei' só depois de uma ferramenta devolver o dado; se a
  ferramenta falhou, diga que falhou e o que ficou sem conferir."
- Mover o prompt da síntese de f-string em `main.py` para `services/_shared/prompts/pesquisa_sintese.md`
  (`render_template`), como os outros prompts do projeto — facilita medir variantes.

**Pronto quando:** bench antes/depois nas 5 perguntas em `profundo`: alucinações 0 (hoje 1), cobertura
não cai, e as frases marcadas "(inferência)" aparecem onde `julgamento.json` marcava N.

**Custo:** 1–2 h, mais uma rodada do bench (~40 min de GPU).

## Fase 7 — Avaliação determinística

**Feynman `evals/run.mjs`:** sem juiz LLM. Por relatório: `done` (relatório e proveniência existem),
`cites` (IDs arXiv/DOI distintos), `valid` (cada ID resolve na API do arXiv, Crossref ou doi.org),
`title` (o título resolvido está ≥ 60% presente, por tokens, na linha que cita o ID), `recall` (artigos
-chave esperados aparecem), tempo e tokens. Doze perguntas fixas; regra: rodar antes e depois de mexer em
prompt, agente, skill ou ferramenta, e commitar os dois resultados.

**Alfred:** `docs/avaliacoes/scripts/bench_pesquisa.py` mede tempo, tokens e fontes; a qualidade é
julgada à mão em `julgamento.json` (26 entradas).

- `docs/avaliacoes/scripts/citacoes_auto.py`: para cada URL de `## Fontes`, GET com `lib_web.baixar`
  (status ≠ 200 ou redirect para outro domínio = morta); para DOI/arXiv ID (Fase 1), resolve e compara
  o título por tokens com a linha da fonte. Saída `validas`, `mortas`, `titulo_ok` em `metricas.jsonl`.
- `perguntas.json` ganha `artigos_chave` (DOI/arXiv) nas perguntas acadêmicas → `recall`. Duas perguntas
  acadêmicas novas de física (área do cofre do Pedro), com ids novos: não editar as existentes, para a
  comparação com 03 e 05/11 continuar válida.
- Regra em `docs/avaliacoes/pesquisa.md`: toda mudança em prompt do researcher roda
  `rodar_bench.sh … profundo` antes e depois nas mesmas perguntas e commita `metricas.jsonl`.
- O julgamento à mão continua para "a fonte sustenta a frase" (o automático não mede sentido). Com a
  Fase 3, `contagem.sustentadas` entra como métrica.

**Pronto quando:** `citacoes_auto.py` rodado sobre os brutos de 05/11 acha as URLs mortas que o
julgamento manual achou.

**Custo:** 3 h.

## Fase 8 — Formatos por tipo de pedido

**Feynman:** `/compare` gera matriz fonte × afirmação-chave × tipo de evidência × ressalvas × confiança,
com acordo, desacordo e incerteza separados; `/lit` separa consenso, divergências e perguntas abertas, e
aponta 3–5 trabalhos que mudaram a direção; `/eli5` responde em seis blocos (resumo em uma frase, a
grande ideia, como funciona, por que importa, do que desconfiar, três coisas para lembrar).

**Alfred:** um formato só (Resumo / uma seção por subpergunta / Lacunas e divergências).

- `services/_shared/prompts/pesquisa_sintese_{padrao,comparacao,revisao,eli5}.md` (a Fase 6 cria o
  `padrao`). `perfis.yml → pesquisa: formato_padrao: "padrao"`.
- Escolha: `SCHEMA_PLANO` ganha `"formato": "padrao" | "comparacao" | "revisao" | "eli5"` a partir do
  tema ("X vs Y" → comparação; "estado da arte", "literatura" → revisão; "me explica simples" → eli5).
  `ResearchRequest.formato` opcional passa por cima; a ferramenta `pesquisar` e a aba Pesquisar expõem
  o campo. Ponto de troca para o Laya: `lib_laya.escolher(topic, "formato do relatório", opcoes)`.
- `comparacao`: tabela com uma linha por item × critério, cada célula com `[n]`, e a seção "Onde as
  fontes discordam". `revisao`: Consenso / Divergências / Perguntas abertas / Leitura recomendada (3
  fontes). `eli5`: os seis blocos, em português simples, todo termo técnico definido na hora.

**Testes:** plano devolve `comparacao` para "Pi 5 vs Orange Pi 5" (mock) → prompt de comparação usado;
formato inválido cai no padrão; `formato` da requisição vence o do plano.

**Pronto quando:** `produtos` do bench em `comparacao` entrega tabela com ≥ 4 critérios citados;
`academico` em `revisao` separa consenso de divergência.

**Custo:** 3 h.

## Ordem sugerida

1 → 2 → 5 → 3 → 6 (com a 7 junto, para medir a 3 e a 6) → 8 → 4. As Fases 5 e 6 são as mais baratas e
podem ir na frente quando a GPU estiver disputada. Total: 25–32 h.

## Fica de fora, e por quê

| Peça do Feynman | Motivo |
|---|---|
| alphaXiv (`alpha_search`, `alpha_get_paper`, `alpha_ask_paper`, anotações) | login e IA na nuvem: o texto do paper e a pergunta iriam para fora. `alpha_read_code` (ler o repositório do paper) é só GitHub público e pode virar `ler_repositorio` via `raw.githubusercontent.com` mais tarde |
| Sub-agentes paralelos (`researcher` × 4, `runs.all`) | uma RTX 3060, o Ollama serializa; a reconstrução do loop do Odysseus mediu isso (`pesquisa.md` §4.4) |
| `/review`, `/audit`, `/replicate`, `/recipe`, `/autoresearch` | fluxo de quem publica paper de ML (baselines, ablations, código do paper, treino) |
| Hugging Face (`hf_dataset_info`, `hf_repo_files`, `hf_repo_read_file`) | sem uso no Alfred |
| Telemetria PostHog | não |
| `session-search` (grep nos transcripts JSONL) | o Alfred tem `conversas.db`; buscar conversas antigas vira uma ferramenta pequena quando a aba Conversa existir ([chat-alfred.md](chat-alfred.md)) |
| `AGENTS.md` de projeto com ledger de tarefas e portões de verificação | cofre, memória e `tasks.yml` cobrem |
| Pi, ACP/Zed, provedores de nuvem, instalador | stack diferente |
