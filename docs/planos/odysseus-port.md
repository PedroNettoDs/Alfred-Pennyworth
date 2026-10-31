# Plano — adaptar o Odysseus ao Alfred (100% local, com Laya)

Referência: https://github.com/odysseus-dev/odysseus (AGPL-3.0).
**Regra de licença:** o Alfred é MIT. Nenhum código do Odysseus é copiado; cada
fase reimplementa a *ideia* no estilo do Alfred (Python + FastAPI + systemd,
`lib_alfred`, `perfis.yml`, vault Obsidian, painel Jinja). Ler o código do
Odysseus para entender o comportamento é permitido; colar trechos não.

## Princípios

1. **100% local.** Nada sai da máquina: Ollama (`gemma4`, `qwen3.6`), embeddings
   `bge-m3`/`qwen3-embedding`, SearXNG local, Laya local. Recursos do Odysseus
   que dependem de nuvem (ChatGPT subscription, Copilot, Codex, Outlook OAuth,
   provedores de API) ficam **fora**.
2. **Laya decide, LLM escreve.** Toda decisão de máquina (classificar, pontuar,
   sim/não, rotear, deduplicar) vai para o Laya. O LLM gerativo só é chamado
   quando o resultado é texto que Pedro vai ler/ouvir. Saídas intermediárias
   (rótulos, scores, JSON) não precisam ser "escrita humana".
3. **Incremental e testado.** Cada fase tem testes em `services/*/tests/` e
   critério de pronto verificável. Uma fase por branch/PR.

## Fase 0 — Infra do Laya (`services/_shared/lib_laya.py`)

Laya: `convaiinnovations/laya` (Apache-2.0), encoder ModernBERT com cabeça de
decisão. Entrada: estado (texto/e-mail/JSON) + perguntas tipadas `choice`,
`score`, `noul` (sim/não). Saída: rótulo/score/probabilidade + confiança, numa
só passada, sem gerar texto. Multilíngue: 322M parâmetros, contexto 1024 (até 8192).
Custo: cerca de 33 ms numa GPU T4 e de 200 a 460 ms em CPU.

- Serviço `alfred-laya.service` (FastAPI em `:7073`, `laya` com extra de
  servidor), modelo pré-carregado na RTX 3060; fallback CPU.
- `lib_laya.py`: `decidir(estado, perguntas) -> dict`, `escolher(texto, opcoes)`,
  `pontuar(texto, escala)`, `sim_nao(texto, pergunta) -> float`.
  Timeout curto; se o serviço cair, retorna `None` e o chamador usa o caminho
  LLM atual (`ollama_generate_json`).
- Limiar de confiança em `perfis.yml → laya: {confianca_minima: 0.7}`; abaixo
  dele, cai para o LLM (cascata System 1 → System 2).
- Observação: o GGUF do Ollama (`fr0stbit3/laya-gguf`) só tem o backbone, sem a
  cabeça de decisão. Usar o pacote Python, não o Ollama.

**Pronto quando:** `lib_laya.sim_nao("reunião amanhã 14h", "é convite?")`
responde em <100 ms na GPU; testes com serviço fora do ar caem no LLM.

## Fase 1 — Migrar decisões existentes para o Laya

Pontos atuais que são decisão, não texto:

| Onde | Hoje | Com Laya |
|---|---|---|
| `inbox/core.py:classificar` | `ollama_generate_json` (até 120 s) | `choice` de categoria + `noul` "precisa resposta?" |
| `inbox/core.py` detecção de evento | `SCHEMA_EVENTO` via LLM | `noul` "contém evento/tarefa?"; LLM só extrai campos se sim |
| `whatsapp.py` triagem | LLM | `choice` (pessoal/trabalho/spam/grupo) + `score` urgência |
| `briefing` relevância de notícia | LLM | `score` 0–5 contra `perfis.yml` |
| `limpeza.py` | heurística + LLM | `noul` "pode arquivar?" |

Manter o cache de classificação existente; chave inclui `motor=laya|llm`.

**Pronto quando:** testes de regressão comparam Laya vs. LLM num lote fixo de
e-mails/mensagens (gravado em `tests/fixtures/`) com concordância ≥ 85%, e o
tempo médio de classificação cai.

**Resultado (31/10/2026):** reprovado para e-mail. O Laya zero-shot concordou com o LLM em 28%
dos 54 e-mails reais (critério: ≥ 85%). Detalhes em `services/laya/about.md`. Antes de usar o
Laya em qualquer decisão é preciso treiná-lo (`laya-train`) com rótulos gerados pelo LLM local e
medir de novo.

### Outros pontos de decisão (levantamento de 31/10/2026)

Chamadas ao LLM que só decidem (candidatas ao Laya). Cada uma precisa de treino próprio com
rótulos do LLM e do mesmo teste de concordância antes de trocar.

| Onde | Decisão | Uso do Laya | Ganho |
|---|---|---|---|
| `agenda.detectar_compromisso` | existe compromisso? tipo | portão: `choice` {evento, tarefa, lembrete, nenhum}; LLM só extrai data/hora se ≠ nenhum | alto: roda em toda mensagem que passa no regex `_PREFILTRO` (que pega "hoje", "pagar", horas...) |
| `whatsapp.classificar_whatsapp` | categoria, requer_resposta, urgência | 3 perguntas numa passada; LLM só gera `resumo` quando categoria ∈ {profissional, pessoal} | alto: toda mensagem recebida |
| `core.classificar` (e-mail) | categoria | em treino; LLM só para acionáveis (`resumo`) | alto |
| `core.extrair_evento` | é evento real? | portão antes da extração | médio |
| `limpeza.avaliar` | pode arquivar? | `choice` {arquivar, manter} sobre regras atuais | médio |
| briefing (antes do `lib_cluster`) | notícia relevante para o perfil? | `score` 0–4 contra `perfis.yml`; descarta antes de embeddings/LLM | médio |
| researcher, cache semântico | resultado em cache responde à pergunta? | `choice` {responde, não} além da distância ≤ 0.25 | baixo |
| roteamento de pedidos (chat/API/CLI) | qual serviço atende? | `choice` {pesquisa, agenda, shell, whatsapp, conversa} | novo recurso |

Não entram: tudo que gera texto (jornal, rascunhos, roteiros de áudio, briefing, títulos,
transcrição).

Início rápido: o `laya-serve` já sobe com o checkpoint pré-carregado (`LAYA_PRELOAD=1`, pronto em
segundos após o boot) e funciona offline. O LLM ainda paga carga de modelo na primeira chamada;
com o Laya como portão, ciclos sem nada acionável nem chegam a acordar o Ollama.

## Fase 2 — Memória automática (prioridade escolhida)

Ideia do Odysseus (`memory_extractor.py`): após cada interação, extrair fatos
memoráveis; periodicamente auditar (juntar duplicatas, reescrever vagos, apagar
lixo); busca vetorial.

Versão Alfred, `services/memoria/`:
- **Fontes:** pendências respondidas (e-mail/WhatsApp), chats do painel, notas
  que Pedro escreve na conversa "Você", agenda.
- **Filtro (Laya):** `noul` "contém fato durável sobre Pedro/contatos/preferências?"
  em cada trecho. Só os positivos vão ao LLM.
- **Extração (LLM, `gemma4:e4b`):** JSON `{fato, sujeito, tipo, validade, fonte}`.
  Formato de máquina, sem polimento.
- **Armazenamento:** SQLite (`lib_db`, já tem FTS5) + embeddings `bge-m3` no
  ChromaDB; espelho legível em `VAULT_ALFRED/memoria/*.md` (um arquivo por sujeito).
- **Dedup (Laya + embeddings):** candidatos por similaridade > 0.85, Laya `noul`
  "mesmo fato?"; `choice` {manter_novo, manter_antigo, fundir}.
- **Auditoria:** timer semanal; impressão digital do estado (como o sidecar do
  Odysseus) para pular quando nada mudou.
- **Uso:** `lib_memoria.contexto(consulta, k=8)` injetado nos prompts de
  rascunho de resposta, briefing e chat.
- Painel: aba "Memória" para ver/editar/apagar fatos.

**Pronto quando:** responder uma pendência que menciona "prefiro reuniões de
manhã" gera um fato; o rascunho seguinte para o mesmo contato usa esse fato;
fato duplicado é fundido na auditoria.

## Fase 3 — Robustez de dados

- `lib_alfred.escrever_atomico(path, conteudo)`: arquivo temporário +
  `os.replace`, com `fsync`. Aplicar em `agenda.yml`, caches JSON, vault.
- Sanitização de logs: mascarar tokens, telefones e e-mails em `log()`.
- Backup/restore (`docs/backup-restore` do Odysseus): `alfred backup` gera
  tar do `data/`, vault e SQLite; `alfred restore`.

## Fase 4 — Skills do agente

Ideia: o agente aprende procedimentos reutilizáveis (`skills.py`,
`skill_extractor.py`). Versão Alfred: `VAULT_ALFRED/skills/*.md` com
frontmatter (gatilho, passos, comandos permitidos). Laya `choice` escolhe a
skill pela intenção; o `shell-executor` mantém a allowlist atual. Extração de
skill nova: Laya `noul` "esta sequência foi repetida/útil?" e só depois o LLM
gera o rascunho da skill.

## Fase 5 — Pesquisa profunda

O `researcher` já existe. Adicionar do Odysseus: plano em etapas, leitura
completa das fontes, relatório final. Laya `score` filtra relevância de cada
página antes de ir ao LLM (corta tokens). Presets por hardware (3060 12 GB:
`qwen3.6:35b-a3b` só para síntese final, `gemma4:e4b` nas etapas).

## Fase 6 — Painel: notas, tarefas, agenda, histórico

- Tarefas e lembretes no painel, lidos/escritos em `agenda.yml` (já integrado
  ao Thunderbird/CalDAV). Agendamento de tarefas do agente (cron via systemd
  timer gerado).
- Histórico de conversas pesquisável (FTS5 já existe).
- Compare cego entre modelos locais do Ollama (útil para escolher modelo por
  tarefa); Laya pode servir de juiz em critérios objetivos.

## Fase 7 — Segurança do painel

Do `core/auth.py`: TOTP 2FA (`pyotp`, sem nuvem), tokens de API com escopo para
N8N, bypass de localhost desligado por padrão, rate-limit no login.

## Fase 8 — Voz e mídia (opcional)

STT/TTS já existem (`lib_audio`, `lib_tts`). Avaliar do Odysseus apenas o
pipeline de transcrição de YouTube → briefing (yt-dlp + whisper local).

## Fora do escopo

Integrações em nuvem (ChatGPT/Copilot/Codex/Outlook OAuth/provedores pagos),
Cookbook de download de modelos (Ollama já resolve), editor de documentos,
galeria/editor de imagens, temas, builds Windows/macOS, app companion.

## Ordem sugerida

0 → 1 → 2 → 3 em sequência (cada uma usa a anterior). 4–8 independentes,
na ordem de interesse.
