# Plano — Conversa com o Alfred no Painel

Objetivo: uma aba **Conversa** no Painel (`http://127.0.0.1:7073/painel/conversa`) onde Pedro fala
com o Alfred — mesma personalidade, sabendo da agenda, das mensagens, dos estudos e do que já foi
combinado, com busca na internet — 100% local. É a Etapa 2 de
[remover-openwebui.md](remover-openwebui.md), detalhada.

## Onde estamos (31/10/2026)

**Painel** (`services/inbox/painel.py` + `painel_web/`, FastAPI + Jinja + `painel.js` sem framework,
CSP `default-src 'self'`, só 127.0.0.1, login pelo `SHELL_EXECUTOR_TOKEN`, CSRF em todo POST):

| Aba | O que faz |
|---|---|
| Início | pendências aguardando, Alfred ligado/desligado, estado do WhatsApp |
| Pendências | aguardando/resolvidas; detalhe com resumo, áudio, **Autorizar envio**, **Não enviar**, **Agendar**, **Vou/Não vou** |
| Agenda | próximos compromissos e desfeitos; **Desfazer** |
| Limpeza | lotes de e-mail para a Lixeira; **Aprovar/Recusar** |
| Sistema | serviços, WhatsApp, fila |
| WhatsApp (`/painel/whatsapp`) | QR code para parear |

Não há chat: hoje só o Open WebUI conversa, e ele está saindo.

**Peças prontas (branch `laya`):**

| Peça | Arquivo | Serve para |
|---|---|---|
| Personalidade | `docs/alfred-system-prompt.md` | system prompt (precisa adaptar: cita Open WebUI, `qwen3:8b`, nomes antigos de tools) |
| Ferramentas | `_shared/lib_ferramentas.py` — 27 ferramentas, 5 perigosas | tool calling nativo do Ollama (`esquemas()`, `executar()`, `perigosa()`) |
| RAG | `_shared/lib_rag.py` — FTS5 + ChromaDB (8001), RRF, citações | estudos e notas: `VAULT_PEDRO` (825 notas: Cursos, Certificações, Tecnologia, Física…) e `VAULT_ALFRED` |
| Memória | `_shared/lib_memoria.py` | fatos duráveis; `contexto(pergunta)` |
| Mensagens | `_shared/lib_db.py` → `wa_msgs` (`buscar_wa`, `historico_contato`) | histórico do WhatsApp |
| Agenda | `inbox/agenda.py` → `agenda.yml` + `tasks.yml` | compromissos, tarefas, lembretes |
| E-mail | `_shared/lib_mail.py` (Thunderbird) + pendências | e-mails recentes e o que está esperando resposta |
| Busca web | `lib_alfred.searxng_search` (SearXNG 8889) + researcher (7072, modo `profundo`) | rápida e aprofundada |
| Decisões rápidas | Laya (7074) | roteamento de ferramentas (depois de treinado) |

## Como uma volta da conversa funciona

```
Pedro digita ──► POST /painel/api/conversas/{id}/mensagens ──► turno na fila do chat
                                                                  │
   montar contexto:  system prompt  +  bloco "Agora"  +  memória  +  (RAG automático, se couber)
                     + histórico da conversa (últimas N mensagens + resumo das antigas)
                                                                  │
   Ollama /api/chat (stream, tools=esquemas()) ◄──────────────────┘
        │  texto ─────────────────────────────► SSE "texto" (aparece letra a letra)
        │  tool_call segura ──► executar() ──► SSE "ferramenta" (chip com nome, argumentos, resultado)
        │  tool_call perigosa ► SSE "aprovar" ─► cartão [Executar] [Não] ─► POST …/aprovar → executar()
        └─ repete até resposta final (máx. 6 voltas de ferramenta)
                                                                  │
   grava a conversa (SQLite)  ──►  lib_memoria.extrair(volta) em segundo plano
```

### Contexto montado a cada volta

1. **Personalidade** — `docs/alfred-system-prompt.md`, revisado: sem Open WebUI, ferramentas com os
   nomes da `lib_ferramentas`, regra "nunca invente resultado de ferramenta" mantida, `think: false`.
2. **Bloco "Agora"** (sempre, curto, ~400 tokens): data/hora e dia da semana; compromissos das
   próximas 48 h (`agenda.yml`); tarefas vencidas e de hoje (`tasks.yml`); pendências aguardando
   (quantas + as 3 mais urgentes, com contato e uma linha); estado do WhatsApp.
3. **Memória** — `lib_memoria.contexto(pergunta, k=8)`: o que Pedro já disse sobre si, preferências,
   pessoas.
4. **Estudos/notas** — por padrão via ferramenta (`buscar_cofre`, `ler_nota`) para não gastar
   contexto; RAG automático (`lib_rag.contexto(pergunta, 1500 caracteres)`) só quando a pergunta
   for claramente sobre conteúdo dos cofres (heurística agora; Laya depois).
5. **Histórico** — últimas 12 mensagens inteiras; antes disso, um resumo da conversa refeito a cada
   ~20 mensagens (LLM, formato de máquina).

Orçamento: `num_ctx` 16k no `gemma4:12b`. Os esquemas das 27 ferramentas custam ~3–4k tokens;
o roteador (Fase 5) reduz para as 6–8 relevantes.

### Ferramentas na conversa

Já existem (27): sistema, pesquisa (`pesquisar`, `andamento_pesquisa`), briefing, pendências,
limpeza, tarefas, memória, cofres (`buscar_cofre`, `ler_nota`, `listar_cofre`, `salvar_nota`).

Faltam para o pedido:

| Nova | Faz | Base |
|---|---|---|
| `ver_agenda(dias=7)` | compromissos (não só tarefas) | `agenda.listar` |
| `criar_compromisso(texto)` **perigosa** | "marca dentista sexta 15h" | `agenda.detectar_compromisso` + agenda |
| `buscar_mensagens(consulta, contato?)` | busca no histórico do WhatsApp | `lib_db.buscar_wa` |
| `conversa_com(contato, n=20)` | últimas mensagens com alguém | `lib_db.historico_contato` |
| `emails_recentes(dias=2, categoria?)` | e-mails da caixa com categoria | `lib_mail` + `inbox_categorias.json` |
| `ler_email(message_id)` | corpo de um e-mail | `lib_mail` |
| `buscar_web(consulta, n=5)` | busca rápida (títulos, trechos, URLs) | `searxng_search` |
| `ler_pagina(url)` | texto de uma página, com as mesmas defesas do researcher (sem IP privado, limite de tamanho, redirects validados) | código do modo `profundo` |
| `buscar_cofre` → `lib_rag.buscar` | trocar FTS puro pela busca híbrida | `_buscar()` isolado já prevê a troca |

Perigosas continuam pedindo clique: `executar_comando`, `responder_pendencia`,
`responder_limpeza`, `salvar_nota`, `completar_tarefa`, `criar_compromisso`. Enviar WhatsApp pela
conversa só via `responder_pendencia` (o mesmo cartão de confirmação do Painel).

### Interface (aba Conversa)

- Coluna esquerda: conversas (título gerado com `lib_titulo`, data), **Nova conversa**, busca (FTS5).
- Centro: mensagens em Markdown seguro (o mesmo renderizador mínimo do detalhe de pendência);
  resposta aparecendo por SSE; **chips de ferramenta** recolhíveis (nome, argumentos, resultado);
  **citações** `[n]` levando à nota (`obsidian://open?vault=…&file=…`) ou à URL.
- **Cartão de aprovação** para ferramenta perigosa: o que vai rodar, com quais argumentos,
  [Executar] [Não]; sem clique em 5 min, cancela.
- Campo de texto (Enter envia, Shift+Enter quebra), **Parar** durante a geração, seletor de modelo
  (padrão `MODEL_CHAT`).
- Depois: 🎤 (grava → `lib_audio` transcreve) e 🔊 (resposta em voz com `lib_tts`).
- Tudo dentro da CSP atual (`default-src 'self'`): nada de CDN, `EventSource` nativo.

### Backend

- `services/inbox/conversa.py` — laço do turno, montagem do contexto, chamada ao Ollama em stream,
  execução de ferramentas, aprovação pendente, resumo do histórico.
- `VAULT_ALFRED/conversas.db` (SQLite + FTS5): `conversas`, `mensagens` (papel, texto, ferramenta,
  argumentos, resultado, tokens, modelo, tempo), `aprovacoes`.
- Rotas no Painel (sessão + CSRF como as demais):
  `GET /painel/conversa`, `GET/POST /painel/api/conversas`, `POST /painel/api/conversas/{id}/mensagens`,
  `GET /painel/api/turnos/{id}/eventos` (SSE), `POST /painel/api/turnos/{id}/parar`,
  `POST /painel/api/aprovacoes/{id}/executar|negar`, `DELETE /painel/api/conversas/{id}`.
- **Fila própria do chat** (1 worker) separada do threadpool do FastAPI — aprendido em 31/10: uma
  rajada de webhooks esgotou o threadpool. O SSE é `async` e só lê a fila de eventos do turno.
- **GPU**: o chat e o processamento do WhatsApp disputam o Ollama. O turno do chat tem prioridade:
  enquanto há turno ativo, a fila de espera do WhatsApp segura o próximo item (não descarta).
- CLI para testar sem interface: `./run.sh conversa "pergunta"`.

## Fases

| Fase | Entrega | Depende de | Pronto quando |
|---|---|---|---|
| **1. Motor** | `conversa.py`, contexto, laço de ferramentas, aprovação, SQLite, CLI | — | testes offline (Ollama mockado) do laço, aprovação, limite de voltas, resumo; CLI responde "quais pendências tenho?" chamando `listar_pendencias` |
| **2. Ferramentas novas** | as 9 da tabela acima em `lib_ferramentas.py` | — (paralela à 1) | testes offline de cada uma; `buscar_cofre` usando `lib_rag` |
| **3. Aba Conversa** | template, JS, CSS, rotas SSE no Painel | 1 | conversa completa no navegador: texto em stream, chips, cartão de aprovação, citações, histórico |
| **4. Personalidade** | `alfred-system-prompt.md` revisado para a nova pilha | 1 | 10 perguntas de referência respondidas com a voz do Alfred e as ferramentas certas |
| **5. Roteador e memória** | Laya escolhe o subconjunto de ferramentas e se vale RAG automático; `lib_memoria.extrair` após cada volta | 1, 2; Laya treinado com os registros das fases 1–3 | menos tokens por volta sem perder acerto de ferramenta (medido no log) |
| **6. Voz** | 🎤 e 🔊 | 3 | pergunta falada → resposta falada |
| **7. Desligar o Open WebUI** | Etapa 5 de `remover-openwebui.md` | 3, 4 | container removido, testes passando |

Fases 1 e 2 podem rodar em paralelo (arquivos diferentes). A 3 é a única que mexe em
`painel.py`/`base.html`.

## Riscos

- **Contexto x VRAM:** `gemma4:12b` (~8 GB) + `bge-m3` + Laya na mesma RTX 3060 de 12 GB; `num_ctx`
  maior come VRAM de KV cache. Medir na Fase 1; se apertar, `gemma4:e4b` para voltas de ferramenta
  e o 12b só na resposta final.
  *Medido (01/11):* o 12b com 16k cabe; o Laya e o `bge-m3` foram para a CPU para o 12b não sair da GPU
  (ver `services/inbox/about.md`, "Medição na RTX 3060").
- **Modelo pequeno com 27 ferramentas:** escolhe mal quando há muitas. Mitigação imediata: agrupar
  por domínio e expor só os grupos que a pergunta menciona (heurística), Laya na Fase 5.
- **Privacidade:** tudo local; a busca web passa pelo SearXNG da máquina. Ainda assim, o histórico
  das conversas fica em `VAULT_ALFRED/conversas.db` — entra no backup semanal.
