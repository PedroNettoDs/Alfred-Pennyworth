# _shared — Fundação compartilhada

Biblioteca de código e configuração usada por todos os serviços do Alfred. Nenhum serviço duplica lógica que está aqui.

## Arquivos

### `perfis.yml`
Fonte única de verdade do projeto. Define perfis de usuário, fontes de dados, briefings e scanners. Editar aqui propaga automaticamente para todos os serviços que chamam `perfis.py`.

**Estrutura:**
```
perfis:           # quem é o usuário e seus interesses
  pedro_dev:      # perfil pessoal — briefing matinal
  attanotech:     # perfil da empresa — briefing vespertino

fontes:           # coleções de fontes reutilizáveis
  rss_tech_internacional, rss_tech_brasil, searxng_tech_*

briefings:        # configuração de cada briefing agendado
  matinal_tech:   # 07:00 · perfil pedro_dev · RSS internacional + brasil
  vespertino_mercado: # 18:00 · perfil attanotech · RSS + SearXNG brasil

scanners: {}      # reservado para scanners futuros

inbox:            # caixas de e-mail do Inbox Assistant
  pedro_hotmail:  # Thunderbird · perfil pedro_dev · jornal + pendências

whatsapp:         # contas de WhatsApp (Evolution API) do Inbox Assistant
  pedro_whatsapp: # perfil pedro_dev · grupos, contatos, agrupamento, templates da pergunta
```

### `perfis.py`
Loader com cache em memória. Expõe funções tipadas:
- `get_perfil(nome)` → dict com campos do perfil
- `get_briefing(nome)` → dict com `_perfil` e `_fontes` já resolvidos
- `get_scanner(nome)` → igual para scanners
- `get_inbox(nome)` → inbox com `_perfil` resolvido (`python3 perfis.py inbox pedro_hotmail`)
- `get_whatsapp(nome)` → conta de WhatsApp com `_perfil` resolvido (`python3 perfis.py whatsapp pedro_whatsapp`)
- `list_all()` → sumário de tudo (útil para debug: `python3 perfis.py list`)

Faz validação leve de referências cruzadas na carga — se um briefing apontar para um perfil inexistente, falha com mensagem clara.

### `lib_alfred.py`
Funções utilitárias base usadas por todos os serviços:

| Função | O que faz |
|--------|-----------|
| `log(msg)` | Print com timestamp `[HH:MM:SS]`, passado por `sanitizar()` |
| `sanitizar(texto)` | Função pura que mascara segredos e contatos numa linha de log, deixando o resto legível: `Bearer ***`, `NOME_TOKEN=***` / `*_KEY` / `*_SECRET` / `*_PASSWORD` / `apikey=***` (também em JSON e dict), telefone `55119****1234` (cru, no JID `@s.whatsapp.net` ou `+55 (11) 9…`) e e-mail `p***@gmail.com`. JIDs de grupo (`@g.us`) ficam como estão |
| `gravar_atomico(caminho, conteudo)` | Grava texto (UTF-8) ou bytes sem nunca deixar o arquivo pela metade: temporário oculto e único no mesmo diretório (`.<nome>.<aleatório>.tmp`), fsync, `os.replace` e fsync do diretório. Falhou no meio → o arquivo antigo fica intacto e o temporário some. Mantém as permissões do arquivo trocado. Não substitui a trava (`core.trava_arquivo`) em "ler → mudar → gravar" concorrente. É o mesmo objeto que `core.gravar_atomico` |
| `slugify(text)` | Texto → slug seguro para nome de arquivo |
| `ollama_generate(prompt, model, timeout, fmt, think, options, system, images)` | Chama `/api/generate` do Ollama. `images` = mídias em base64 — imagem **ou áudio** (WAV/MP3) em modelos com capacidade `audio`, como o gemma4 |
| `ollama_embed(text, model)` | Gera embedding via `/api/embeddings` |
| `ollama_embed_batch(texts, model)` | Embeddings em lote, reutiliza um único client HTTP |
| `searxng_search(query, categories, max_results)` | Busca no SearXNG, retorna `[{title, url, snippet, source}]` |
| `vault_write(vault_path, folder, filename, content, frontmatter)` | Escreve arquivo no vault Obsidian com frontmatter YAML (por `gravar_atomico`) |
| `deduplicate_by_url_and_title(items, title_key)` | Remove duplicatas por URL exata ou título similar (≥80% palavras) |
| `load_env(env_path)` | Carrega `.env` da raiz do projeto para `os.environ` |

Todas as funções degradam graciosamente — erros são logados, nunca levantam exceção para o caller.

### `lib_chromadb.py`
Wrapper HTTP para o ChromaDB (API v2, com fallback v1). Operações:
- `ensure_collection(name)` → cria ou recupera coleção com espaço coseno
- `upsert_document(collection_id, doc_id, text, metadata, embedding)` → insere/atualiza
- `query_similar(collection_id, embedding, n_results, where)` → busca por similaridade
- `delete_document(collection_id, doc_id)` → remove documento

Distância coseno: `0.0` = idênticos, `0.25` = limiar de "similar" (similaridade ≥ 75%), `1.0` = ortogonais. Se ChromaDB estiver offline, todas as funções retornam `None`/`False`/`[]` e logam aviso — nunca travam o serviço chamador.

### `lib_mail.py`
Leitura de e-mails sem rede e sem senha, direto dos arquivos do Thunderbird (cópias offline):
- `find_thunderbird_profile()` → descobre o perfil (snap `~/snap/thunderbird/current/.config/thunderbird`, Thunderbird ≥ 158 `$XDG_CONFIG_HOME/thunderbird` — padrão `~/.config/thunderbird` —, deb/tarball antigo `~/.thunderbird`, flatpak) ou usa `THUNDERBIRD_PROFILE`
- `list_accounts(profile)` → contas a partir do `prefs.js` (hostname, diretório, mbox × maildir)
- `fetch_messages(accounts, pastas, desde, conta)` → mensagens normalizadas em dict (`subject`, `from_*`, `text`, `links`, `ics`, `is_bulk`, `read`...)
- `parse_message(msg)` → HTML→texto com extração de links, anexos `.ics` → eventos (`icalendar` com fallback regex), flags `X-Mozilla-Status`
- `imap_fetch(host, user, password)` → backend IMAP opcional (Outlook/Hotmail exige OAuth2, não funciona por aqui)

Divide o mbox pelo separador real do Thunderbird (`From - <data>`), evitando os falsos cortes do `mailbox.mbox`.

### `lib_cluster.py`
`cluster_items()`, `rank_clusters()`, `build_clustered_news_text()` — extraídos do `tech-briefing.py` para serem usados também pelo inbox. Comportamento idêntico ao original; `pesos_fontes` virou parâmetro.

### `lib_db.py`
SQLite + FTS5 em `VAULT_ALFRED/.alfred/alfred.db` (`ALFRED_DB` troca): `indexar_cofre()` / `buscar_notas()` (índice derivado dos `.md`), `registrar_wa()` / `historico_contato()` / `buscar_wa()` / `podar_wa()` (histórico do WhatsApp) e `cache_get()` / `cache_put()` (cache de classificação do LLM). Nenhuma função levanta: sem banco, devolve vazio/None e o chamador segue pelo caminho antigo. Detalhes e regras do cache em [`inbox/about.md`](../inbox/about.md#busca-e-histórico--sqlite-fts5-_sharedlib_dbpy).

### `lib_memoria.py`
Memória automática: fatos duráveis sobre Pedro e seus contatos ("é vegetariano", "é diretor de TI da Unimed Bauru"), extraídos pelo LLM local, guardados e devolvidos como contexto para os prompts. Ideia trazida do Odysseus (só o comportamento; o código é próprio).

| Função | O que faz |
|--------|-----------|
| `extrair(texto, fonte, sujeito=None)` | Portão `_vale_extrair()` → LLM (`memoria_extrair.md`, JSON Schema `{fatos: [{fato, sujeito, tipo, validade}]}`) → `guardar()`. `sujeito` = quem escreveu (o "eu" do texto); um nome que é parte dele ("Carlos" para "Carlos Silva") vira o nome completo. Devolve os fatos com `id` e `acao` (`novo` / `fundido` / `substituiu` / `descartado`) |
| `guardar(fatos, fonte)` | Dedup contra o mesmo sujeito: texto igual → descarta sem LLM; cosseno > `limiar_duplicata` (0,85) → LLM decide `manter_novo` / `manter_antigo` / `fundir` (`memoria_deduplicar.md`). LLM sem decisão → o novo entra (a auditoria junta depois) |
| `auditar()` | Por sujeito: apaga vencidos (sem LLM), refaz vetores que faltam ou de outro modelo, e manda os fatos em lotes ao LLM (`memoria_auditar.md`) para juntar duplicatas, reescrever vagos e tirar lixo. Guarda a impressão digital (sha256) do estado auditado na tabela `estado` e pula o sujeito que não mudou. Recusa lote que apagaria mais da metade (8+ fatos); ids inventados são ignorados |
| `contexto(consulta, k=8)` | Bloco `Fatos que Alfred já sabe...` com até `k` linhas `- [Sujeito] fato`. Busca vetorial (cosseno ≥ `limiar_contexto`) + FTS5 (inclusive pelo nome do sujeito), fundidas por RRF; sem Ollama, só FTS5. `""` se nada servir |
| `listar(sujeito=None)` | Fatos guardados, sem o vetor |

- **Banco:** `MEMORIA_DB`, `db` da seção `memoria:`, ou `VAULT_ALFRED/memoria/memoria.db`. Tabela `fatos` + FTS5 + vetor `bge-m3` normalizado (float32) como blob na mesma linha; cosseno em Python puro.
- **Espelho:** `<pasta do banco>/<sujeito>.md` (ex.: `VAULT_ALFRED/memoria/carlos-silva.md`), regravado de forma atômica (temporário + `os.replace`) a cada mudança. Editar o `.md` não muda a memória.
- **Portão:** `_vale_extrair(texto) -> bool` é heurística (3+ palavras, 15+ letras, não é só saudação/risada). Vai virar uma decisão sim/não do Laya (127.0.0.1:7073) quando o classificador treinado for aprovado — a troca fica só nessa função.
- **Configuração:** seção `memoria:` do `perfis.yml` (`ativo`, `db`, `modelo`, `modelo_embed`, limiares, `auditar_lote`).
- Nenhuma função levanta: sem banco ou sem Ollama, devolve vazio.
- **CLI** (de `services/`): `python -m _shared.lib_memoria {extrair,listar,auditar,buscar}`. Testes: `inbox/tests/test_memoria.py` (offline).

**Onde ligar depois (ainda não integrado):**
- `inbox/whatsapp.py` → `gerar_rascunho()` e o prompt da pergunta (`pergunta_whatsapp.md`): `contexto(f"{contato} {conteudo}")` como variável nova do template, ao lado de `historico`.
- `inbox/whatsapp.py` → `processar_mensagem_whatsapp()` (mensagem do contato, `sujeito=contato`, `fonte="whatsapp"`) e `responder_texto()` / `registrar_resposta_direta()` (texto de Pedro, `sujeito="Pedro"`): `extrair()` em segundo plano, depois da resposta, para não atrasar a notificação.
- `inbox/core.py` → `responder()` (nota de Pedro numa pendência de e-mail) e e-mails da categoria `contato`/`pessoal`: `extrair(..., fonte="email", sujeito=remetente)`.
- Um timer diário (`alfred-vigia` ou novo) chamando `auditar()` — sem mudança, não chama o LLM.

### `lib_ferramentas.py`
Registro de ferramentas do Alfred para tool calling nativo do Ollama (`/api/chat` com `tools`), sem LangChain. Substitui as 10 tools do Open WebUI em `tools/*.py` (classe `Tools` + `Valves`); quem usa é a aba Conversa do Painel (próxima etapa).

| Função | O que faz |
|--------|-----------|
| `@ferramenta(nome, descricao, perigosa=False, timeout=60)` | Registra uma função comum. O JSON Schema dos parâmetros sai da assinatura (`str`, `int`, `float`, `bool`, `Optional[...]`, `Literal[...]` → `enum`, valores padrão → fora de `required` e "(padrão: …)" no fim da descrição) e das linhas `:param x:` da docstring. Tipo não suportado ou nome repetido falham já na importação |
| `esquemas()` | Lista `[{"type": "function", "function": {name, description, parameters}}]` pronta para o campo `tools` (cópia: mexer nela não muda o registro) |
| `executar(nome, argumentos)` | Converte os argumentos do modelo para os tipos da assinatura (`"5"` → 5, `"true"` → True, `null`/ausente → padrão, chave desconhecida ignorada, `enum` conferido; aceita dict ou texto JSON), roda a função numa thread com o `timeout` do registro e devolve o texto para a mensagem `role: "tool"`, cortado em `MAX_SAIDA` (12 000) caracteres. **Nunca levanta:** argumento inválido, exceção e timeout viram uma linha `[erro] …`. Timeout abandona a thread (não dá para matá-la); numa ferramenta perigosa o texto avisa que a ação pode ter terminado depois |
| `perigosa(nome)` | `True` para `executar_comando`, `responder_pendencia`, `responder_limpeza`, `salvar_nota` e `completar_tarefa`. A aprovação **não** é feita aqui: o chat mostra "Executar? [Sim] [Não]" e só então chama `executar()` |

**Ferramentas (27):**

| Origem (`tools/`) | Ferramentas | Como |
|---|---|---|
| `alfred_shell_executor` | `executar_comando` ⚠ | HTTP `POST /execute` no Shell Executor (whitelist dele; teto 1800 s) |
| `alfred_system_monitor` | `relatorio_sistema`, `status_disco`, `status_gpu` | HTTP no Shell Executor, comandos fixos; `status_disco` só aceita caminho com letras, números, espaço e `/ . _ -` |
| `alfred_researcher` | `pesquisar(tema, profundo=False)`, `andamento_pesquisa(job_id)` | HTTP `POST /research`; `profundo=true` → 202 + `job_id`, progresso em `GET /research/{id}` |
| `alfred_briefing` | `gerar_briefing`, `ver_briefings`, `ler_briefing` | `services/briefing/run.sh` como subprocesso (600 s; lê o `BRIEFING_FILE=` impresso) e `VAULT_ALFRED/briefings/` |
| `alfred_daily_digest` | `resumo_do_dia(dias=1)` | Sem LLM: briefings, `research/*/synthesis.md`, `decisions/`, `logs/`, `tasks.yml` e fatos da `lib_memoria` que vencem em 7 dias |
| `alfred_pendencias` | `listar_pendencias`, `ver_pendencia`, `responder_pendencia` ⚠, `jornal_de_hoje`, `listar_limpezas`, `responder_limpeza` ⚠ | Direto: `core.listar_pendencias` / `obter_pendencia` / `responder(..., so_se_aguardando=True)`, `whatsapp.responder_texto` (pendência de WhatsApp), `core.ultimo_jornal_md`, `limpeza.listar_lotes` / `aprovar` / `recusar`. Origem gravada: `respondido_via: chat` |
| `alfred_tasks` | `adicionar_tarefa`, `listar_tarefas`, `completar_tarefa` ⚠, `tarefas_vencidas` | O mesmo `VAULT_ALFRED/tasks.yml` do "vou" e da agenda: cria por `core._add_task` (ganhou `priority`), marca dentro de `core.trava_arquivo` com gravação atômica. `tasks.yml` ilegível → erro, nada é gravado por cima |
| `alfred_memory` | `lembrar(texto, sujeito)`, `buscar_memoria(consulta)`, `listar_memoria(sujeito)` | `lib_memoria.extrair` / `contexto` / `listar` — o `memory.yml` chave-valor fica para trás (não há "esquecer": a `lib_memoria` não apaga fato avulso; a auditoria faz a faxina) |
| `alfred_vault_reader` + `alfred_vault_writer` | `buscar_cofre(consulta, cofre="todos")`, `ler_nota(caminho, cofre="pedro")`, `listar_cofre`, `salvar_nota` ⚠ | As duas `search_vault` viraram `buscar_cofre`. Caminhos resolvidos dentro do cofre (absoluto, `..` ou symlink para fora → erro); `salvar_nota` nunca sobrescreve |

- **Busca:** `buscar_cofre` passa por `_buscar(query, cofre, limite) -> [{cofre, caminho, titulo, trecho, score}]`, hoje `lib_db.buscar_notas` (FTS5). A busca híbrida de `lib_rag.py` entra trocando só essa função, com a mesma saída.
- **Serviços:** sempre `127.0.0.1` — Shell Executor em `PORT_SHELL_EXECUTOR` (7070), Researcher em `PORT_RESEARCHER` (7071) —, com `Authorization: Bearer $SHELL_EXECUTOR_TOKEN`. Nada de `172.17.0.1`.
- **Importação:** `core`, `whatsapp` e `limpeza` (de `services/inbox/`) só são importados quando uma ferramenta precisa; `esquemas()` não carrega o pipeline do inbox.
- Testes: `inbox/tests/test_ferramentas.py` (offline: vault temporário, índice FTS5 de verdade, HTTP / subprocess / WhatsApp / limpeza / `lib_memoria` mockados).
- Os 27 esquemas custam ~3 200 tokens de prompt no `gemma4:e4b`. Teste real (31/10/2026): "quais pendências tenho?" → `listar_pendencias` com argumentos válidos.

### `lib_tts.py`
`generate_tts()` (async, edge-tts), `tts_save()` (síncrono, nunca levanta), `limpar_para_fala()` (fallback sem LLM), `tocar_audio()` (ffplay → mpv → xdg-open; com `bloquear=True` passa ao próximo player se um sair com erro).

### `lib_titulo.py`
`generate_title_via_llm()` e `extrair_titulo_manchete()` — título/manchete de um markdown, com fallback heurístico.

### `lib_audio.py`
Transcrição de áudio 100% local, usada pelo inbox para os áudios do WhatsApp:
- `transcrever_audio(fonte, mimetype, max_segundos)` → `{texto, metodo, duracao_s, segmentos, cortado, erro}`. `fonte` pode ser caminho, bytes ou base64.
- Ordem: **gemma4 via Ollama** (o áudio vai em base64 no campo `images` do `/api/generate` — o Ollama usa o mesmo campo para imagem e áudio; só aceita WAV/MP3, por isso tudo passa antes pelo ffmpeg → WAV mono 16 kHz; o gemma4 ouve ~30 s por chamada, então áudios longos são fatiados) → **faster-whisper** se instalado → `""`.
- `para_wav()`, `duracao_segundos()`, `ffmpeg_disponivel()`. Modelo: `MODEL_TRANSCRIBE` (vazio = `MODEL_CHAT`).

### `lib_whatsapp.py`
Cliente da Evolution API (WhatsApp) — `enviar_texto(numero, texto, quoted)`, `enviar_audio(numero, arquivo|bytes|base64)`, `baixar_midia(key, message)` (passe o bloco original com `mediaKey`; sem ele a Evolution busca pelo `key.id` — nunca `message: {}`), `estado_instancia()`, `qrcode_instancia()`, `criar_instancia()`, `configurar_webhook(url, headers)`, helpers `jid_para_numero()`, `formatar_numero()`, `numero_pedro()`.
Com `EVOLUTION_URL` vazio ou `WHATSAPP_DRY_RUN=true` **nada sai da máquina**: os envios são registrados em `logs/whatsapp_enviados.jsonl` e devolvidos como `{"ok": true, "simulado": true}` (`ultimos_envios()` lê o registro — é o que os testes usam).

### `lib_templates.py`
Sistema de templates para prompts LLM:
- Templates ficam em `prompts/*.md` com frontmatter YAML (`name`, `description`, `variables`)
- `load_template(name)` → carrega e faz cache do template
- `render_template(name, values)` → substitui `{variavel}` pelo valor; variáveis extras geram aviso; variáveis faltando levantam `ValueError`
- Chaves literais no corpo (exemplos JSON) precisam ser escritas como `{{` e `}}` — o corpo passa por `str.format_map`
- `SafeDict` garante que chaves extras não crasham o `.format_map()` — útil para templates com exemplos JSON usando `{{}}`

### `prompts/`
Templates de prompt versionados como arquivos de texto:

| Arquivo | Uso |
|---------|-----|
| `briefing_tecnico.md` | Briefing matinal do Pedro (perfil pedro_dev) — inclui regra anti-alucinação no topo |
| `briefing_executivo.md` | Briefing vespertino da AttanoTech (perfil attanotech) |
| `briefing_adhoc.md` | Síntese neutra para temas livres (modo ad-hoc) |
| `audio_tecnico.md` | Roteiro de áudio para o briefing matinal — estilo reportagem descontraída |
| `audio_executivo.md` | Roteiro de áudio para o briefing vespertino — estilo noticiário executivo |
| `audio_adhoc.md` | Roteiro de áudio para modo ad-hoc — estilo podcast de curiosidades |
| `email_classificar.md` | Inbox: classifica um e-mail (noticia / evento / contato / transacional / promocao / pessoal / outro) — JSON |
| `email_noticias_extrair.md` | Inbox: extrai as notícias individuais de uma newsletter — JSON (só URLs que existem no e-mail) |
| `email_evento_extrair.md` | Inbox: dados estruturados de convite/contato (data, local, link, o que pedem) — JSON |
| `jornal_email.md` | Inbox: edição do dia a partir das newsletters (regra anti-alucinação igual ao briefing) |
| `audio_jornal.md` | Inbox: roteiro de áudio da edição do dia — podcast matinal |
| `pergunta_evento.md` | Inbox: texto em que Alfred apresenta o evento/contato e pergunta "vou ou não vou?" |
| `audio_pergunta_evento.md` | Inbox: recado de voz curto (30-60s) com a mesma pergunta |
| `whatsapp_classificar.md` | WhatsApp: classifica uma mensagem curta (profissional / pessoal / grupo / automatico / spam) e diz se requer resposta e a urgência — JSON |
| `pergunta_whatsapp.md` | WhatsApp: texto em que Alfred resume a mensagem e pergunta "o que você quer responder?" |
| `audio_pergunta_whatsapp.md` | WhatsApp: recado de voz curto (20-40s) com a mesma pergunta |
| `memoria_extrair.md` | Memória: fatos duráveis de um texto (`{fatos: [{fato, sujeito, tipo, validade}]}`) — JSON |
| `memoria_deduplicar.md` | Memória: fato novo × parecido já guardado → `manter_novo` / `manter_antigo` / `fundir` — JSON |
| `memoria_auditar.md` | Memória: faxina dos fatos de um sujeito (duplicatas, vagos, lixo), devolve a lista limpa por id — JSON |

## Como importar nos serviços

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from _shared.lib_alfred import log, ollama_generate, searxng_search
from _shared.lib_chromadb import ensure_collection, upsert_document
from _shared.lib_templates import render_template
from _shared.perfis import get_briefing, get_scanner, get_inbox
from _shared.lib_mail import find_thunderbird_profile, list_accounts, fetch_messages
from _shared.lib_cluster import cluster_items, rank_clusters, build_clustered_news_text
from _shared.lib_tts import tts_save, tocar_audio
from _shared.lib_titulo import generate_title_via_llm
from _shared.lib_audio import transcrever_audio
from _shared.lib_whatsapp import enviar_texto, enviar_audio, baixar_midia
from _shared.lib_memoria import extrair, contexto, auditar
from _shared.lib_ferramentas import esquemas, executar, perigosa
```

## Variáveis de ambiente relevantes

| Variável | Default | Uso |
|----------|---------|-----|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Endpoint do Ollama |
| `MODEL_CHAT` | `llama3.1:8b` | Modelo de geração de texto |
| `MODEL_EMBED` | `nomic-embed-text-v2-moe:latest` | Modelo de embeddings |
| `SEARXNG_URL` | `http://localhost:8888` | Endpoint do SearXNG |
| `CHROMADB_URL` | `http://localhost:8000` | Endpoint do ChromaDB |
| `VAULT_ALFRED` | `""` | Caminho do vault Obsidian |
| `OLLAMA_THINK` | `""` | `false` desliga o thinking (gemma4/qwen3); vazio não envia o campo |
| `OLLAMA_NUM_CTX` | `""` | `options.num_ctx` enviado ao Ollama (ex.: 8192) |
| `THUNDERBIRD_PROFILE` | `""` | Vazio = descoberta automática do perfil (inclui `~/.config/thunderbird` do Thunderbird ≥ 158) |
| `MODEL_TRANSCRIBE` | `""` | Modelo com entrada de áudio para `lib_audio` (vazio = `MODEL_CHAT`; gemma4 funciona) |
| `EVOLUTION_URL` / `EVOLUTION_API_KEY` / `EVOLUTION_INSTANCE` | `""` / `""` / `alfred` | Evolution API (`lib_whatsapp`). URL vazia = envios simulados |
| `WHATSAPP_NUMERO_PEDRO` | `""` | Número de Pedro (DDI + DDD + número, só dígitos) que recebe os resumos |
| `WHATSAPP_DRY_RUN` | `false` | `true` = simula os envios mesmo com a URL configurada |
| `MEMORIA_DB` | `""` | Banco da memória automática (vazio = `db` da seção `memoria:` → `VAULT_ALFRED/memoria/memoria.db`) |
| `VAULT_PEDRO` | `""` | Cofre do Pedro (`ler_nota(..., cofre="pedro")` em `lib_ferramentas`; indexado por `core.indexar_cofres`) |
| `PORT_SHELL_EXECUTOR` / `PORT_RESEARCHER` | `7070` / `7071` | Portas em `127.0.0.1` que `lib_ferramentas` chama |
| `SHELL_EXECUTOR_TOKEN` | `""` | Bearer do Shell Executor e do Researcher (`lib_ferramentas`) |
