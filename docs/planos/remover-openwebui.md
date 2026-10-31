# Plano — levar o chat, o RAG e as ferramentas para o Painel e remover o Open WebUI

Situação em 31/10/2026: o Painel (`services/inbox/painel.py`, FastAPI + Jinja + `painel.js` sem
framework, só 127.0.0.1, login por `SHELL_EXECUTOR_TOKEN`, CSRF) já tem início, pendências, agenda,
limpeza, sistema e WhatsApp. O Open WebUI ainda faz três coisas que o Painel não faz, e é só por
isso que o container `open-webui` (porta 3001) continua no `docker-compose.yml`.

| O Open WebUI faz | Onde está hoje | Substituto |
|---|---|---|
| Chat com o "modelo Alfred" e system prompt | config do Open WebUI; texto em `docs/alfred-system-prompt.md` | aba **Conversa** no Painel → Ollama `/api/chat` |
| Tool calling | 10 tools em `tools/*.py` (classe `Tools` + `Valves`, formato do Open WebUI) | registro de ferramentas Python no próprio Alfred (`_shared/lib_ferramentas.py`) |
| RAG / Knowledge Base | `services/scripts/sync-knowledge.py` + `trigger_sync()` do researcher sobem o vault para a KB | `lib_db.buscar_notas` (FTS5, já existe) + ChromaDB (`lib_chromadb`) + `lib_memoria.contexto()` |
| Busca web no chat | `ENABLE_RAG_WEB_SEARCH` → SearXNG | ferramenta `buscar_web` sobre `searxng_search` |

Não há histórico de conversas a migrar: `data/open-webui/` não existe nesta máquina.

Os três modelos locais têm `tools` nas capacidades do Ollama (`gemma4:12b`, `gemma4:e4b`,
`qwen3.6:35b-a3b`), então o tool calling nativo do `/api/chat` serve; nada de LangChain.

## Etapa 1 — Ferramentas fora do formato do Open WebUI

`services/_shared/lib_ferramentas.py`:
- registro `@ferramenta(nome, descricao, perigosa=False)` que gera o JSON Schema a partir da
  assinatura e da docstring (tipos simples: str, int, bool, Optional);
- `executar(nome, argumentos) -> str` com timeout e erro como texto (o modelo nunca recebe exceção);
- portar as 10 tools chamando as funções Python direto quando o código está no repositório
  (`core.listar_pendencias`, `agenda`, `lib_db.buscar_notas`, `limpeza`...) e HTTP só para serviços
  separados (executor :7071, researcher :7072);
- juntar o que se sobrepõe: `alfred_memory` (chave-valor) passa a usar `lib_memoria`;
  `alfred_tasks` (tasks.yml) usa o mesmo arquivo da agenda; `vault_reader.search_vault` e
  `vault_writer.search_vault` viram uma só (`buscar_cofre`);
- **perigosas** (`execute_command`, `responder_pendencia`, `responder_limpeza`, `save_to_vault`)
  não executam direto: o chat mostra um cartão "Executar? [Sim] [Não]" e só roda com o clique.
  Mesma regra da limpeza: nada irreversível sem aprovação explícita.

Pronto quando: as 10 ferramentas têm teste offline e `executar()` nunca levanta exceção.

## Etapa 2 — Conversa no Painel

- `services/inbox/conversa.py` (lógica) + rotas no `painel.py` + `conversa.html`.
- Laço: mensagens → `/api/chat` com `tools` → executa as chamadas (ou pede aprovação) → devolve o
  resultado → repete até resposta final (limite de 6 voltas).
- Streaming por SSE (`text/event-stream`) consumido em `painel.js` (`EventSource`/`fetch`).
- System prompt carregado de `docs/alfred-system-prompt.md` (versionado; o `/no_think` do topo vira
  `think: false`), com o bloco de `lib_memoria.contexto(pergunta)` injetado.
- Conversas salvas em SQLite (`VAULT_ALFRED/conversas.db`, FTS5) — lista lateral, renomear, apagar.
- Modelo escolhido por conversa (padrão `MODEL_CHAT`).
- Áudio opcional depois: microfone → `lib_audio` (transcrição), resposta → `lib_tts`.

Pronto quando: "quais pendências tenho?", "pesquise X", "rode `df -h`" (com aprovação) e "o que
eu disse sobre Y?" funcionam no Painel com o Open WebUI desligado.

## Etapa 3 — RAG local

- Busca híbrida em `_shared/lib_rag.py`: FTS5 (`lib_db.buscar_notas`) + vetores ChromaDB, com fusão
  por posição (RRF); citações com caminho da nota.
- Indexação: estender o que já existe (`lib_db.indexar_cofre` e `scripts/chromadb-indexer.py`) para
  rodar incremental num timer; o researcher chama esse índice no lugar de `trigger_sync()`.
- Ferramenta `buscar_cofre` usa `lib_rag`; a conversa pode citar notas.

Pronto quando: a mesma pergunta que hoje acha uma nota pela KB do Open WebUI acha pelo `lib_rag`.

## Etapa 4 — Visões novas no Painel

| Aba | Conteúdo |
|---|---|
| Pesquisa | `POST /research` (normal ou `profundo`), lista de pesquisas, progresso por `GET /research/{id}`, relatório com citações |
| Memória | fatos da `lib_memoria` por sujeito; editar, apagar, auditar |
| Base de dados | consulta só leitura às bases SQLite do Alfred (`lib_db`, memória, conversas, cache); tabelas e contagens, busca FTS |
| RAG | busca no índice com as notas e trechos que entrariam no contexto; estado da indexação |
| Laya | estado do serviço, decisões recentes e confiança, resultado dos treinos |
| Rastro (opcional) | por conversa: ferramentas chamadas, argumentos, tempo, modelo |

## Etapa 5 — Desligar o Open WebUI

1. Remover `open-webui` do `docker-compose.yml` e de `CONTAINERS` em `instalar.sh` / `painel.py`.
2. Apagar `sync-knowledge.py`, `webui-indexer.py` (placeholder) e `trigger_sync()` do researcher;
   tirar `WEBUI_*` e `RESEARCHER_KB_*` do `.env.example`.
3. Mover `tools/` para o histórico (o formato só servia ao Open WebUI).
4. README: arquitetura, portas (3001 some), seção "Tools do Open WebUI" vira "Ferramentas do Alfred".
5. O ChromaDB e o SearXNG continuam (RAG e busca).

Pronto quando: `docker compose ps` sem `open-webui`, todos os testes passam e a Conversa cobre os
casos da Etapa 2.

## Ordem e paralelismo

- Etapa 1 e Etapa 3 são independentes (arquivos diferentes) e podem correr em paralelo.
- Etapa 2 depende da 1 (e usa a 3 quando pronta).
- Etapa 4: Pesquisa, Memória, Base de dados e Laya só dependem do que já existe — podem começar já,
  mas todas mexem em `painel.py`/`base.html`, então uma sessão por vez nesses arquivos.
- Etapa 5 por último.

## Em aberto

- "Pesquisa aprofundada camuflada": definir se é privacidade (SearXNG atrás de Tor/proxy, sem
  rastros nas buscas) ou pesquisa em segundo plano que só avisa no fim.
- LangChain: fora. O tool calling nativo do Ollama cobre o caso; a visão "Rastro" mostra o fluxo.
