# inbox — Assistente de e-mail (Thunderbird → jornal diário + pendências "vou / não vou")

Lê os e-mails que o Thunderbird já baixou (cópias offline, sem senha e sem rede), classifica cada um com o Ollama, monta a **edição do dia** do jornal a partir das newsletters (mesmo pipeline do briefing: clustering → template → Ollama → título → `.md` + `.mp3`) e, para **convites a eventos e contatos profissionais**, escreve um texto explicativo + áudio em que Alfred pergunta **"vou ou não vou?"** — e a pergunta fica aguardando a resposta de Pedro.

## Como rodar

```bash
./run.sh                 # ciclo completo (inbox padrão: INBOX_DEFAULT do .env)
./run.sh scan            # só classifica e mostra uma tabela — nada é gravado
./run.sh scan --desde 400   # janela maior (horas) — útil se o Thunderbird ficou dias sem sincronizar
./run.sh jornal          # só a edição do dia
./run.sh pendencias      # fila de convites/contatos aguardando resposta
./run.sh perguntar       # modo interativo: Alfred apresenta cada pendência e espera 'vou / nao' no terminal (--ouvir toca o áudio)
./run.sh ver <id>        # texto explicativo da pendência
./run.sh ouvir <id>      # toca o áudio da pergunta
./run.sh responder <id> vou           # "vou" → cria tarefa em VAULT_ALFRED/tasks.yml
./run.sh responder <id> nao-vou --nota "conflito com a Fatec"
./run.sh serve           # API HTTP (FastAPI :7072) para o N8N
```

Flags do ciclo: `--inbox NOME`, `--desde HORAS`, `--reprocessar` (ignora o `inbox_seen.json`), `--max N`, `--sem-notificar`, `--json`.

## Pipeline

```
perfis.yml (inbox: pedro_hotmail)
    ↓
lib_mail.fetch_messages()  → perfil do Thunderbird (snap/deb/flatpak) → prefs.js → contas → mbox/maildir
    ↓                        só e-mails da janela (janela_horas) e ainda não processados (logs/inbox_seen.json)
classificar()
    ├─ heurísticas: remetentes_ignorar → ignorado · anexo .ics → evento · remetentes_noticia → noticia
    └─ Ollama (format=json, think=false) com email_classificar.md
         → noticia | evento | contato | transacional | promocao | pessoal | outro  (+ confiança)
    ↓                                              ↓
noticia                                        evento / contato
    ↓                                              ↓
extrair_noticias()                             extrair_evento()
  email_noticias_extrair.md → itens              email_evento_extrair.md (+ ICS tem prioridade)
  {title, url, snippet, source}                  {titulo, data_inicio, local, link, o_que_pedem...}
  (URLs só se existirem no e-mail)                 ↓
    ↓                                          criar_pendencia()
gerar_jornal()                                   pergunta_evento.md → texto (vault/pendencias/<id>.md)
  dedup → cluster_items → rank_clusters           audio_pergunta_evento.md → edge-tts (<id>.mp3)
  → jornal_email.md → Ollama → título             pendencias.yml: status=aguardando
  → vault/jornal/<data>_<slug>.md                  notify-send com botões Vou / Não vou / Ouvir
  → audio_jornal.md → edge-tts → .mp3              (processo destacado fica ocioso até o clique)
    ↓
logs/inbox_seen.json  ·  expirar_e_lembrar(): expira evento passado, re-notifica a cada N horas
```

## Onde as coisas ficam

```
VAULT_ALFRED/
  jornal/      2026-10-19_<slug>.md  + .mp3          ← edição do dia
  pendencias/  pendencias.yml                        ← fila (status: aguardando | vou | nao_vou | ignorada | expirada)
               20261019-<slug>.md + .mp3             ← texto explicativo + áudio da pergunta
  tasks.yml                                          ← "vou" cria tarefa (mesmo formato da tool alfred_tasks)
logs/inbox_seen.json                                 ← message-ids processados (janela 14 dias)
```

## Como responder "vou / não vou"

Qualquer caminho atualiza o mesmo `pendencias.yml`:

| Caminho | Como |
|---------|------|
| Pergunta gráfica (fica ociosa até o clique) | processo destacado por pendência: `notify-send` com botões **Vou / Não vou / Ouvir** → se o servidor não aceitar ações, `kdialog --yesnocancel` → `zenity`. "Ouvir" toca o áudio e pergunta de novo |
| Terminal interativo | `./run.sh perguntar [--ouvir]` — Alfred apresenta uma a uma e espera `vou` / `nao` / `ouvir` / `ver` / `pular` |
| Terminal | `./run.sh responder <id> vou` |
| Chat (Open WebUI) | tool `tools/alfred_pendencias.py` → "responder pendência <id>: vou" |
| N8N | sub-workflow fica em **Wait**; o link `resumeUrl?resposta=vou` chama `POST /pendencias/{id}/responder` |

## API HTTP (para o N8N)

`./run.sh serve` sobe o FastAPI em `PORT_INBOX` (7072), autenticado com `Authorization: Bearer <SHELL_EXECUTOR_TOKEN>`:

| Método | Rota | Função |
|--------|------|--------|
| GET | `/health` | status, perfil do Thunderbird, pendências aguardando |
| POST | `/ciclo` | ciclo completo; body `{dry_run, janela_horas, reprocessar, so_jornal, so_pendencias, notificar}` |
| POST | `/scan` | só classificação |
| POST | `/jornal` | só a edição |
| GET | `/jornal/ultimo` · `/jornal/ultimo/audio` | última edição (texto / mp3) |
| GET | `/pendencias?status=aguardando` | fila |
| GET | `/pendencias/{id}` · `/pendencias/{id}/audio` | detalhe (texto) / mp3 da pergunta |
| POST | `/pendencias/{id}/responder` | `{"resposta": "vou" \| "nao_vou" \| "ignorar", "nota": ""}` |
| POST | `/pendencias/{id}/links` | guarda URLs externas de resposta (resumeUrl do N8N) |

Do container do N8N o host é `http://172.17.0.1:7072`.

## N8N — workflows em `n8n/`

- `alfred-inbox-aguardar-resposta.json` — sub-workflow: recebe uma pendência, registra os links `vou`/`nao_vou` (`$execution.resumeUrl`) na API, fica parado no nó **Wait** (até 7 dias) e, quando o link é aberto, chama `/responder`. É aqui que entra o WhatsApp (Evolution API) no próximo passo: enviar título + links + áudio.
- `alfred-inbox-ciclo.json` — cron 07:30/18:30 → `POST /ciclo` → uma execução do sub-workflow por pendência nova (sem esperar).

Importe primeiro o sub-workflow, copie o ID dele para o nó "Aguardar resposta" do ciclo e preencha o token no nó **Config** de cada um. **Os workflows foram escritos à mão e ainda não foram executados num N8N real** — valide os nomes dos parâmetros ao importar.

## Agendamento (systemd de usuário — sem sudo, com acesso à sessão gráfica)

```bash
mkdir -p ~/.config/systemd/user
cp alfred-inbox.service alfred-inbox.timer alfred-inbox-api.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alfred-inbox.timer      # ciclo 07:30 e 18:30
systemctl --user enable --now alfred-inbox-api        # API para o N8N (opcional)
journalctl --user -fu alfred-inbox
```

Se preferir o padrão dos outros serviços (unit de sistema com `User=netto`), lembre que `notify-send` e áudio precisam de `DBUS_SESSION_BUS_ADDRESS` — a unit de usuário já tem isso.

## Configuração (`services/_shared/perfis.yml` → `inbox:`)

| Campo | Função |
|-------|--------|
| `backend` | `thunderbird` (padrão) ou `imap` (IMAP_HOST/USER/PASSWORD no .env; Outlook/Hotmail exige OAuth2 e não funciona assim) |
| `conta` | substring do hostname/nome da conta no Thunderbird; vazio = todas |
| `pastas`, `janela_horas`, `max_emails`, `so_nao_lidos` | o que ler |
| `remetentes_noticia` / `remetentes_ignorar` | atalhos determinísticos antes do LLM |
| `confianca_minima` | abaixo disso, noticia/evento/contato vira `outro` |
| `jornal.*` | formato, voz, templates, `max_noticias`, `max_por_email`, `min_noticias`, `pesos_fontes` |
| `pendencias.*` | voz, templates, `lembrar_a_cada_horas`, `expirar_apos_evento` |

Variáveis do `.env`: `THUNDERBIRD_PROFILE` (vazio = auto), `INBOX_DEFAULT`, `INBOX_AUTOPLAY`, `INBOX_AUDIO_PLAYER`, `PORT_INBOX`, `MODEL_CLASSIFY` (modelo leve só para classificar), `OLLAMA_THINK=false` (obrigatório para gemma4/qwen3 — senão o modelo gasta os tokens "pensando" e devolve vazio), `OLLAMA_NUM_CTX`.

## Limitações conhecidas

- Só vê o que o Thunderbird já sincronizou offline. Se o Thunderbird ficar fechado, a INBOX local para no último sync (use `--desde` maior ou abra o Thunderbird antes do ciclo).
- Classificação é LLM: promoções disfarçadas de newsletter podem passar; ajuste `remetentes_ignorar`/`remetentes_noticia`.
- edge-tts usa o serviço de voz da Microsoft (único componente que sai da máquina, mesmo caso do briefing).
- `notify-send --action` pode falhar com "Actions are not supported" mesmo no Plasma (aconteceu aqui); por isso o aguardador cai para `kdialog`/`zenity`. Sem nenhum dos três, a pergunta só aparece como notificação simples e a resposta vem por `perguntar`, CLI, chat ou N8N.
- Extração usa *structured outputs* do Ollama (JSON Schema em `core.py`): sem isso o gemma4 às vezes ecoava o prompt (`{"links": [...]}`) ou truncava o JSON. URLs de rastreamento com mais de 300 caracteres ficam fora do prompt, mas ainda podem virar o link da pendência via fallback por texto-âncora.
