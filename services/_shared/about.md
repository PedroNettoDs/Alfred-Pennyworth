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
```

### `perfis.py`
Loader com cache em memória. Expõe funções tipadas:
- `get_perfil(nome)` → dict com campos do perfil
- `get_briefing(nome)` → dict com `_perfil` e `_fontes` já resolvidos
- `get_scanner(nome)` → igual para scanners
- `get_inbox(nome)` → inbox com `_perfil` resolvido (`python3 perfis.py inbox pedro_hotmail`)
- `list_all()` → sumário de tudo (útil para debug: `python3 perfis.py list`)

Faz validação leve de referências cruzadas na carga — se um briefing apontar para um perfil inexistente, falha com mensagem clara.

### `lib_alfred.py`
Funções utilitárias base usadas por todos os serviços:

| Função | O que faz |
|--------|-----------|
| `log(msg)` | Print com timestamp `[HH:MM:SS]` |
| `slugify(text)` | Texto → slug seguro para nome de arquivo |
| `ollama_generate(prompt, model, timeout)` | Chama `/api/generate` do Ollama |
| `ollama_embed(text, model)` | Gera embedding via `/api/embeddings` |
| `ollama_embed_batch(texts, model)` | Embeddings em lote, reutiliza um único client HTTP |
| `searxng_search(query, categories, max_results)` | Busca no SearXNG, retorna `[{title, url, snippet, source}]` |
| `vault_write(vault_path, folder, filename, content, frontmatter)` | Escreve arquivo no vault Obsidian com frontmatter YAML |
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
- `find_thunderbird_profile()` → descobre o perfil (snap `~/snap/thunderbird/current/.config/thunderbird`, deb `~/.thunderbird`, flatpak) ou usa `THUNDERBIRD_PROFILE`
- `list_accounts(profile)` → contas a partir do `prefs.js` (hostname, diretório, mbox × maildir)
- `fetch_messages(accounts, pastas, desde, conta)` → mensagens normalizadas em dict (`subject`, `from_*`, `text`, `links`, `ics`, `is_bulk`, `read`...)
- `parse_message(msg)` → HTML→texto com extração de links, anexos `.ics` → eventos (`icalendar` com fallback regex), flags `X-Mozilla-Status`
- `imap_fetch(host, user, password)` → backend IMAP opcional (Outlook/Hotmail exige OAuth2, não funciona por aqui)

Divide o mbox pelo separador real do Thunderbird (`From - <data>`), evitando os falsos cortes do `mailbox.mbox`.

### `lib_cluster.py`
`cluster_items()`, `rank_clusters()`, `build_clustered_news_text()` — extraídos do `tech-briefing.py` para serem usados também pelo inbox. Comportamento idêntico ao original; `pesos_fontes` virou parâmetro.

### `lib_tts.py`
`generate_tts()` (async, edge-tts), `tts_save()` (síncrono, nunca levanta), `limpar_para_fala()` (fallback sem LLM), `tocar_audio()` (ffplay → mpv → xdg-open).

### `lib_titulo.py`
`generate_title_via_llm()` e `extrair_titulo_manchete()` — título/manchete de um markdown, com fallback heurístico.

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
| `THUNDERBIRD_PROFILE` | `""` | Vazio = descoberta automática do perfil |
