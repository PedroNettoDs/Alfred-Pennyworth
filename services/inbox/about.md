# inbox — Assistente de e-mail e WhatsApp (Thunderbird → jornal diário + pendências "vou / não vou"; Evolution API → pendências "o que você quer responder?")

Lê os e-mails que o Thunderbird já baixou (cópias offline, sem senha e sem rede), classifica cada um com o Ollama, monta a **edição do dia** do jornal a partir das newsletters (mesmo pipeline do briefing: clustering → template → Ollama → título → `.md` + `.mp3`) e, para **convites a eventos e contatos profissionais**, escreve um texto explicativo + áudio em que Alfred pergunta **"vou ou não vou?"** — e a pergunta fica aguardando a resposta de Pedro.

**Poder 3 — WhatsApp:** mensagens e áudios recebidos pela Evolution API passam pelo mesmo tratamento (transcrição local com gemma4 → classificação → pendência com texto + áudio em que Alfred pergunta **"o que você quer responder?"**); a resposta de Pedro — por WhatsApp, terminal, chat ou API — é enviada de volta ao contato. Ver [WhatsApp (Poder 3)](#whatsapp-poder-3--evolution-api).

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
./run.sh responder-texto <id> "Carlos, mando até quinta."   # pendência de WhatsApp → envia ao contato
./run.sh whatsapp status | qrcode | webhook URL | simular payload.json | notificar <id>
./run.sh whatsapp fila   # mensagens esperando atraso_minutos (contato, prazo, textos) · processar-fila [--todos] [--sem-enviar]
./run.sh serve           # API HTTP (FastAPI :7072) para o N8N e para o webhook da Evolution
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
  → vault/jornal/<data>_<slug>.md                  notificação com botões Vou / Não vou / Ouvir
  → audio_jornal.md → edge-tts → .mp3              (processo destacado fica ocioso até o clique)
    ↓
logs/inbox_seen.json  ·  expirar_e_lembrar(): expira evento passado, re-notifica a cada N horas
```

## Onde as coisas ficam

```
VAULT_NOTICIAS/  (= VAULT_ALFRED/Noticias, cofre Obsidian próprio; vazio no .env → VAULT_ALFRED)
  jornal/      2026-10-19_<slug>.md  + .mp3 + .json  ← edição do dia (.json = e-mails e notícias de origem)
  podcast/episodios/   2026-W43_resumo-semanal.md + .mp3   ← podcast semanal
  podcast/manifestos/  2026-W43.json                       ← e-mails usados no episódio (contrato com a limpeza)
VAULT_ALFRED/
  jornal/      (antigo — lido só como fallback)
  pendencias/  pendencias.yml                        ← fila (status: aguardando | vou | nao_vou | ignorada | expirada
               20261019-<slug>.md + .mp3                       | respondida | respondida_manual — estes dois são do WhatsApp)
                                                    ← texto explicativo + áudio da pergunta (e-mail e WhatsApp)
  tasks.yml                                          ← "vou" cria tarefa (mesmo formato da tool alfred_tasks)
logs/inbox_seen.json                                 ← message-ids de e-mail processados (janela 14 dias)
logs/whatsapp_seen.json · whatsapp_enviados.jsonl    ← ids de WhatsApp processados · envios simulados (dry-run)
logs/whatsapp_fila.json                              ← mensagens esperando atraso_minutos (texto/transcrição, sem o áudio)
```

## Como responder "vou / não vou"

Qualquer caminho atualiza o mesmo `pendencias.yml`:

| Caminho | Como |
|---------|------|
| Pergunta gráfica (fica ociosa até o clique) | processo destacado por pendência: notificação com botões **Vou / Não vou / Ouvir** (`notificar_dbus.py`, D-Bus direto) → se não houver suporte a ações, `kdialog --yesnocancel` → `zenity`. "Ouvir" toca o áudio e pergunta de novo |
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
| POST | `/whatsapp/webhook` | webhook da Evolution (`messages.upsert`): texto/áudio → pendência `whatsapp`; resposta citada de Pedro → envia ao contato. Query `enviar=0` (nunca chama a Evolution), `notificar=1` (o Python avisa Pedro). Auth: header `apikey`, Bearer (`EVOLUTION_API_KEY` ou `SHELL_EXECUTOR_TOKEN`) ou campo `apikey` do payload |
| POST | `/whatsapp/webhook-async` | igual, mas responde `202` na hora e processa em segundo plano — para a Evolution chamar direto, sem N8N (`notificar=1` por padrão) |
| POST | `/pendencias/{id}/responder-texto` | `{"texto": "...", "enviar": true, "nota": ""}` → grava `resposta_texto` e envia pela Evolution (`"ignorar"` fecha sem enviar). `502` se o envio falhar (a resposta fica guardada; repita) |
| POST | `/pendencias/{id}/notificar-whatsapp` | manda a Pedro o resumo (com `ref: <id>`) + áudio da pergunta |
| GET | `/whatsapp/status` | configuração, estado da instância, pendências de WhatsApp aguardando, `fila_espera` (mensagens esperando `atraso_minutos`) |

Do container do N8N o host é `http://172.17.0.1:7072`.

## N8N — workflows em `n8n/`

- `alfred-inbox-aguardar-resposta.json` — sub-workflow: recebe uma pendência, registra os links `vou`/`nao_vou` (`$execution.resumeUrl`) na API, fica parado no nó **Wait** (até 7 dias) e, quando o link é aberto, chama `/responder`. O envio de título + links + áudio pelo WhatsApp pode ser copiado do workflow abaixo (nós "Evolution: texto/áudio para Pedro").
- `alfred-inbox-ciclo.json` — cron 07:30/18:30 → `POST /ciclo` → uma execução do sub-workflow por pendência nova (sem esperar).
- `alfred-whatsapp.json` — **Webhook** (Evolution) → `POST /whatsapp/webhook` → se o Python devolveu `notificacao`, manda a Pedro o texto (termina com `ref: <id>`) via `sendText` e o áudio da pergunta via `GET /pendencias/{id}/audio` → base64 → `sendWhatsAppAudio`. A resposta de Pedro (mensagem citando a do Alfred) entra pelo mesmo webhook: o Python casa o `ref`, chama `responder_texto()` e envia ao contato; o workflow só repassa a confirmação.

Importe primeiro o sub-workflow, copie o ID dele para o nó "Aguardar resposta" do ciclo e preencha o token no nó **Config** de cada um (no `alfred-whatsapp.json` também `evolution_apikey`). **Os workflows foram escritos à mão e ainda não foram executados num N8N real** — valide os nomes dos parâmetros ao importar.

## Agendamento (systemd de usuário — sem sudo, com acesso à sessão gráfica)

```bash
mkdir -p ~/.config/systemd/user
cp alfred-inbox.service alfred-inbox.timer alfred-inbox-api.service \
   alfred-semanal.service alfred-semanal.timer alfred-vigia.service alfred-vigia.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alfred-inbox.timer      # ciclo de e-mail 07:30 e 18:30
systemctl --user enable --now alfred-semanal.timer    # podcast semanal, domingo 19:00
systemctl --user enable --now alfred-vigia.timer      # vigia: 2 min após o login e a cada 10 min
systemctl --user enable --now alfred-inbox-api        # API (WhatsApp, extensões do Thunderbird, N8N)
journalctl --user -fu alfred-vigia
```

Tudo sobe sozinho com o computador: Docker (`enabled`) traz a Evolution API (`restart: unless-stopped`), o
linger do usuário mantém as units de usuário, e `~/.config/autostart/alfred-thunderbird.desktop` abre o
Thunderbird no login (sem ele aberto não há e-mail novo nem gravação na agenda).

As notificações que esperam clique nascem como **units transitórias** (`systemd-run --user`, nomes
`alfred-aguardar-*` / `alfred-vigia-qr-*`): um filho comum de uma unit oneshot morria quando o ciclo
terminava (ou a API reiniciava) e a pergunta sumia da tela sem resposta.

### Vigia (`vigia.py`, `run.sh vigia`)

A cada 10 min confere e conserta o que dá:

| Checagem | Se falhar |
|----------|-----------|
| API do inbox (`:7072/health`) | `systemctl --user restart alfred-inbox-api` |
| containers `alfred-evolution`, `alfred-evolution-db` | `docker start` |
| conexão do WhatsApp (`connectionState`) | notificação **WhatsApp desconectado** com **[Mostrar QR code]** (gera e abre o QR; escaneie em Aparelhos conectados). Repete no máximo a cada 3 h; avisa quando reconectar |
| Thunderbird aberto | só informa |
| áudios de resumo do WhatsApp | apaga os `.mp3` com mais de `audio_reter_dias` (2) — ver abaixo |
| agenda | avisa se há compromisso esperando a extensão há mais de 30 min |

**Por que o WhatsApp parou em 19/10 às 19:10:** o WhatsApp desvinculou o aparelho (`stream:error 401
conflict device_removed`) e a Evolution fez logout da instância (`state: close`). Isso acontece quando o
aparelho é removido em *Aparelhos conectados* no celular ou quando o WhatsApp encerra a sessão vinculada.
O autostart não resolve: precisa parear de novo pelo QR code. O vigia agora avisa na hora.

### Áudios de resumo do WhatsApp: apagados depois de 2 dias

O `.mp3` de cada pendência de WhatsApp já foi enviado a Pedro pelo WhatsApp, então o arquivo local some
`audio_reter_dias` depois de criado (`perfis.yml → whatsapp.pedro_whatsapp.audio_reter_dias: 2`). O `.md` e a
pendência ficam (`arquivos.mp3: null`, `audio_removido_em`). Só arquivos de pendências `tipo: whatsapp` dentro
de `VAULT_ALFRED/pendencias/` são tocados — áudios de e-mail, jornal e podcast não.

```bash
./run.sh whatsapp limpar-audios --simular     # lista o que seria apagado
./run.sh whatsapp limpar-audios --dias 2      # apaga (o vigia faz isso a cada 10 min)
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

## WhatsApp (Poder 3) — Evolution API

Mensagens recebidas no WhatsApp viram pendências em que Alfred resume a mensagem e pergunta **"o que você quer responder?"** (texto + áudio). A resposta de Pedro é enviada ao contato pelo próprio WhatsApp.

```
Evolution API (container, número pareado por QR code)
    │  webhook messages.upsert — texto ou áudio (OGG/Opus em base64 no payload)
    ▼
POST /whatsapp/webhook  (direto, ou via Webhook do N8N)          services/inbox/whatsapp.py
    ├─ normalizar_payload()        → contato, número, tipo, texto, citação, fromMe, grupo
    ├─ _descarte_sem_texto()       → grupo fora de `grupos` (ignorar / mencao sem Pedro), contatos_ignorar:
    │                                 não vira pendência → o áudio NÃO é transcrito (sem GPU, sem trecho no log)
    ├─ áudio → transcrever_audio() → ffmpeg (OGG → WAV 16 kHz) → gemma4 via Ollama (campo images)
    │                                 → fallback faster-whisper → "" (pendência avisa "não transcrito")
    ├─ classificar_whatsapp()      → whatsapp_classificar.md (JSON Schema):
    │                                 profissional | pessoal | grupo | automatico | spam
    │                                 + requer_resposta + urgencia   (heurísticas: contatos_ignorar/sempre, grupos)
    ├─ relevante? (profissional; pessoal se pessoal_vira_pendencia; só se requer_resposta)
    │     não → acao: ignorada (motivo)         mesma pessoa com pendência aberta → acao: agrupada
    ├─ atraso_minutos > 0 → enfileirar_mensagem() → logs/whatsapp_fila.json → acao: agendada + prazo
    │     Pedro escreve na conversa (fromMe) antes do prazo → sai da fila, nada é gerado (acao: fila_cancelada)
    │     prazo vencido → processar_fila() (thread da API, a cada 60 s) → segue abaixo
    └─ criar_pendencia_whatsapp()  → pergunta_whatsapp.md → <id>.md
                                     audio_pergunta_whatsapp.md → edge-tts → <id>.mp3
                                     pendencias.yml: tipo=whatsapp, status=aguardando, resposta_texto=null
                                   → devolve notificacao {numero: Pedro, texto (termina com "ref: <id>"), audio_url}

Pedro responde CITANDO a mensagem do Alfred (ou por terminal / chat / API)
    ▼
responder_texto(id, texto)         → pendencias.yml: status=respondida, resposta_texto
                                   → Evolution: POST /message/sendText/{instancia} (quoted = mensagem original)
                                   "ignorar" → status=ignorada, nada é enviado
                                   Pedro respondeu o contato direto no celular (fromMe) → status=respondida_manual
```

O webhook devolve `{"acao": pendencia_criada | agendada | agrupada | resposta_registrada | pendencia_ignorada | respondida_manual | fila_cancelada | duplicada | ignorada, "motivo", "classificacao", "transcricao", "pendencia", "notificacao"}` (+ `prazo` e `fila` quando `agendada`; `fila_cancelada` = ids que saíram da fila). Quem avisa Pedro é o orquestrador: o N8N (`n8n/alfred-whatsapp.json`) lê `notificacao` e manda texto + áudio pela Evolution; sem N8N, chame o webhook com `?notificar=1` (ou use `/whatsapp/webhook-async`, que já notifica) e o próprio Python envia.

### Espera antes de resumir (`atraso_minutos`)

Com a instância no próprio número, toda conversa passa pelo Alfred — inclusive as que Pedro está respondendo em tempo real. Por isso a parte cara e barulhenta espera `atraso_minutos` (20 no `perfis.yml`) **sem resposta de Pedro naquela conversa**:

- **Na hora** (o webhook continua rápido): normalizar → transcrever o áudio (só se puder virar pendência — grupo ignorado e `contatos_ignorar` não são transcritos) → classificar → decidir a relevância → dedup em `whatsapp_seen.json`. Mensagem relevante entra em `logs/whatsapp_fila.json` (mensagem normalizada sem o base64, transcrição, classificação, prazo e as opções `enviar`/`notificar` do webhook) e o webhook responde `acao: agendada` com o `prazo`.
- **Pedro escreve na mesma conversa antes do prazo** (mensagem `fromMe` no mesmo `remote_jid` — direto pelo celular): o item sai da fila e nada é gerado (`acao: fila_cancelada`). Ecos das respostas que o próprio Alfred envia **não** cancelam: Pedro respondeu a pendência anterior e pode não ter visto a mensagem nova.
- **Mensagens seguidas do contato** entram no mesmo item; o prazo continua contado da primeira. Ao vencer viram **uma** pendência: textos/transcrições juntos (o resumo e o rascunho cobrem tudo), maior urgência, `whatsapp.mensagens` com todos os ids e a resposta citando a última.
- **Prazo vencido:** uma thread iniciada com a API (`main.py`, a cada 60 s; os vencidos enquanto a API estava fora saem na primeira volta) cria a pendência, o `.md`, o áudio, a notificação no PC e — se o webhook veio com `notificar=1`, como o `/whatsapp/webhook-async` — o aviso pelo WhatsApp. O item só sai da fila depois da pendência gravada (erro → tenta de novo, até 3 vezes).
- Contato que **já tem pendência aguardando** dentro de `agrupar_minutos` não espera: a mensagem é agrupada na hora, como antes — a pendência aberta já prova que Pedro não respondeu.
- O `/whatsapp/webhook` síncrono não devolve `notificacao` para mensagens agendadas (quando o prazo vence ninguém está esperando a resposta HTTP): com o N8N no meio, chame-o com `?notificar=1` para o Python avisar Pedro, ou use `atraso_minutos: 0`.
- Depurar: `./run.sh whatsapp fila` lista contato, prazo e textos; `./run.sh whatsapp processar-fila --todos --sem-enviar` cria tudo na hora sem chamar a Evolution (com a API no ar a thread dela faz o mesmo — rodar os dois juntos pode duplicar); `whatsapp simular payload.json --imediato` ignora a espera.
- `atraso_minutos: 0` volta ao comportamento anterior (pendência e avisos na hora). Qualquer mudança (código ou `perfis.yml`) só vale depois de reiniciar a API: `systemctl --user restart alfred-inbox-api` (logs: `journalctl --user -fu alfred-inbox-api`); não suba `./run.sh serve` em paralelo — a porta 7072 é a mesma.

### Subir a Evolution API e parear o número (QR code)

1. No `.env`: `EVOLUTION_API_KEY` (`openssl rand -hex 16`), `EVOLUTION_POSTGRES_PASSWORD` (`openssl rand -hex 12`), `EVOLUTION_URL=http://localhost:8080`, `EVOLUTION_INSTANCE=alfred`, `WHATSAPP_NUMERO_PEDRO=55DDDNÚMERO` (só dígitos).
2. `docker compose up -d evolution-postgres evolution-api` (a porta 8080 fica só em `127.0.0.1`; a imagem está fixada pelo digest da build validada — a `:latest` de 2026-05-06, que reporta v2.3.7). Confira: `curl -s -H "apikey: $EVOLUTION_API_KEY" http://localhost:8080/instance/fetchInstances`.
3. `./run.sh whatsapp qrcode` — cria a instância (integração Baileys, `token` = `EVOLUTION_API_KEY`) se não existir, salva o QR em `logs/evolution_qr.png` e abre a imagem. No celular: **WhatsApp → Aparelhos conectados → Conectar aparelho** → escanear. Repita o comando se o QR expirar (a Evolution gira o QR a cada ~45 s; `QRCODE_LIMIT=30`). O PNG da Evolution é pequeno (348 px, módulos azuis): na validação o celular só leu depois de ampliado 2× em preto e branco e reaberto a cada QR novo. Depois de parear, `WHATSAPP_NUMERO_PEDRO` = número do `ownerJid` em `fetchInstances` quando a instância é o próprio número de Pedro.
   - Equivalente em curl: `curl -X POST -H "apikey: $EVOLUTION_API_KEY" -H 'Content-Type: application/json' -d '{"instanceName":"alfred","integration":"WHATSAPP-BAILEYS","qrcode":true,"token":"'$EVOLUTION_API_KEY'"}' http://localhost:8080/instance/create` e depois `curl -H "apikey: ..." http://localhost:8080/instance/connect/alfred` (campo `base64` = PNG do QR).
   - Ou pelo Evolution Manager (`http://localhost:8080/manager`, chave = `EVOLUTION_API_KEY`).
4. `./run.sh whatsapp status` deve mostrar `"estado_instancia": "open"`.
5. Aponte o webhook da instância para o Alfred (`./run.sh serve` precisa estar no ar):
   - **Sem N8N:** `./run.sh whatsapp webhook http://172.17.0.1:7072/whatsapp/webhook-async` — responde `202` na hora, processa em segundo plano e o Python manda o resumo + áudio a Pedro.
   - **Com N8N:** importe `n8n/alfred-whatsapp.json`, ative, e `./run.sh whatsapp webhook http://n8n:5678/webhook/alfred-whatsapp` (containers na mesma rede `alfred-net`).
   - O comando configura `base64: true` e só o evento `MESSAGES_UPSERT`, com o header `apikey` para o Alfred validar. **Na v2.3.7 o áudio chegou SEM `base64`** (o webhook foi configurado com a instância já conectada — a Evolution só lê `webhookBase64` ao conectar): o Python baixa com `getBase64FromMediaMessage` passando o `audioMessage` original (com `mediaKey`), e funciona igual.
   - Mensagens que o Alfred envia pela API não voltam como `messages.upsert` (são `SEND_MESSAGE`, que não assinamos) — o eco só aparece para o que Pedro digita no celular.
6. Qual número é "Pedro"? Duas formas, ambas suportadas:
   - **Instância num número separado (recomendado):** Alfred tem o próprio chip/número. Contatos escrevem para o número de Pedro normalmente? Não — escrevem para o número do Alfred (é ele que está pareado). Útil como "secretária": divulgue o número do Alfred para contatos profissionais. `WHATSAPP_NUMERO_PEDRO` = o número pessoal de Pedro; o resumo chega lá e Pedro responde citando.
   - **Instância no próprio número de Pedro:** tudo que chega ao celular de Pedro passa pelo Alfred. O resumo é enviado para o próprio número (conversa "Você") e Pedro responde citando ali. Se Pedro responder o contato direto no celular, o webhook `fromMe` fecha a pendência como `respondida_manual`. Ecos das mensagens que o Alfred envia são ignorados pelo id.

### Responder

| Caminho | Como |
|---------|------|
| WhatsApp | responda **citando** (deslize para responder) a mensagem do Alfred — o texto vira a resposta ao contato; `ignorar` fecha sem enviar; áudio citado também vale (é transcrito) |
| Notificação no PC | **Enviar** abre o rascunho do Alfred numa caixa para editar e confirmar · **Não enviar** = `ignorar` · **Agenda** · **Ouvir** — ver [abaixo](#notificação-no-pc-enviar--não-enviar--agenda--ouvir) |
| Terminal | `./run.sh responder-texto <id> "texto"` · `./run.sh perguntar` (modo interativo aceita texto livre nas pendências de WhatsApp) |
| Chat (Open WebUI) | tool `alfred_pendencias.py` → "responde pro Carlos que mando até quinta" → `responder_pendencia(id, "texto")` chama `POST /responder-texto` (Valves `inbox_api_url` / `inbox_api_token`); sem API no ar, guarda no vault e avisa |
| API / N8N | `POST /pendencias/{id}/responder-texto` |

Sem `EVOLUTION_URL` (ou com `WHATSAPP_DRY_RUN=true`) **nada sai da máquina**: os envios ficam em `logs/whatsapp_enviados.jsonl` e a pendência registra `envio.simulado: true`.

### Notificação no PC (Enviar / Não enviar / Agenda / Ouvir)

Cada pendência de WhatsApp abre um processo destacado (`inbox-assistant.py _aguardar-whatsapp <id>`, sobrevive à API) com uma notificação crítica — fica na tela até o clique — mostrando a mensagem, o resumo e **💡 a sugestão de resposta**. O rascunho e o compromisso citado (data/hora) saem de uma chamada JSON ao gemma4 (`whatsapp_rascunho.md`, ~2 s) na criação da pendência e ficam em `whatsapp.rascunho` / `whatsapp.compromisso` + seção "Sugestão de resposta" no `.md`.

| Botão | O que faz |
|-------|-----------|
| **Enviar** | caixa (`kdialog --textinputbox` → `zenity`) já preenchida com o rascunho; OK → `responder_texto()` → sai pelo WhatsApp citando a mensagem original; Cancelar volta à notificação |
| **Não enviar** | fecha como `ignorada` (nada é enviado) |
| **Agenda** | pergunta "Quando?" (sugere o compromisso da mensagem ou amanhã 09:00; aceita `20/10 14:00`, `amanhã 10h`, `terça 15h`, `2026-10-21 09:30`) e faz as três coisas: tarefa em `VAULT_ALFRED/tasks.yml` (`due_date`, tags `whatsapp`), lembrete — a pendência continua aguardando e a notificação volta nesse horário — e abre no navegador o "criar evento" do Google Agenda já preenchido (Pedro clica Salvar; sem credencial nem API, nada vai ao Google antes disso) |
| **▶ Ouvir** / **⏹ Parar áudio** | o resumo em áudio **começa a tocar sozinho quando a notificação aparece** (`pendencias.tocar_ao_notificar: true`; `INBOX_AUTOPLAY=false` desliga). Enquanto toca, o botão é **Parar**; depois vira **Ouvir** para repetir. Qualquer outro botão para o áudio |

O Plasma não toca áudio *dentro* da notificação: a dica `sound-file` foi testada e o `plasmashell` não abriu
nenhum stream no PipeWire. Por isso o aguardador toca o mp3 (ffplay) em paralelo e controla pelos botões.

Se a pendência for respondida por outro caminho (WhatsApp, terminal, chat), a notificação fecha sozinha no próximo clique e não envia nada. Sem servidor de notificações com ações, cai para `kdialog --menu` → `zenity --list`.

### Validação com WhatsApp real (2026-10-19)

Instância no **próprio número de Pedro**, Evolution API "v2.3.7" (digest no compose), API do Alfred em `:7072` chamada direto pela Evolution (`/whatsapp/webhook-async`, sem N8N), contato de teste num segundo aparelho. Validado: texto → pendência → resumo + áudio na conversa "Você" → resposta citada entregue ao contato (citando a mensagem dele) + confirmação; áudio → download + transcrição gemma4 (~1 s para 10 s de fala) → pendência; `ignorar` citado; resposta direta pelo celular → `respondida_manual`; notificação no PC → Enviar (rascunho) entregue.

Divergências do formato real e correções (cobertas por `tests/payloads/evolution_v237_*.json` e `TestEvolutionV237`):

| Divergência | Efeito antes | Correção |
|-------------|--------------|----------|
| `extendedTextMessage` vira `conversation` e o `contextInfo` (citação `stanzaId`/`quotedMessage`, menções) vem em **`data.contextInfo`**, fora de `message` | a resposta citada de Pedro era ignorada ("mensagem de Pedro sem citação"): nada chegava ao contato e a pendência ficava aguardando | `_contextos()` lê os blocos da mensagem **e** `data.contextInfo` |
| áudio chega **sem `base64`** (só `audioMessage` com `mediaKey` em bytes JSON `{"0": 12, …}`) | fallback mandava um `audioMessage` só com `url`/`mimetype` → HTTP 400 "Cannot derive from empty media key" → "áudio não transcrito" | `normalizar_payload` guarda o `audioMessage` original e `baixar_midia` o repassa; sem ele, omite `message` (a Evolution busca no próprio banco — `message: {}` quebra) |
| em grupo, `key.participant` é `<id>@lid` e o número vem em `key.participantAlt` | participante = id lid | `_sem_lid()` prefere o `participantAlt` |
| key traz `remoteJidAlt` e `addressingMode: "lid"` (remoteJid ainda era o número) | — | já tratado |
| duas mensagens simultâneas (webhook-async = uma thread por mensagem) | `criar_pendencia_whatsapp` lê o yml, gasta ~15 s em LLM/TTS e grava → **uma pendência sumia** (ficavam só o .md/.mp3) | `_LOCK` (RLock) serializa `processar_mensagem_whatsapp`, `responder_texto`, `notificar_pedro`, `agendar_pendencia` |

Observações da validação:
- Com a instância no próprio número, **toda** conversa passa pelo gemma4 (local): uma conversa real em andamento virou pendência "profissional". Grupos são ignorados (`grupos: ignorar`). Resolvido depois com a [espera antes de resumir](#espera-antes-de-resumir-atraso_minutos) (`atraso_minutos: 20`).
- Tudo o que Pedro escreve citando o resumo vai ao contato — inclusive instruções para o Alfred ("(coloque na minha agenda também)" foi entregue). Roadmap: comandos para o Alfred pelo WhatsApp.
- O log da API (`./run.sh serve`) imprime os primeiros 60 caracteres de cada mensagem recebida — inclusive de grupos e conversas pessoais.
- N8N: **não validado** (stack Docker do Alfred fora do ar nesta máquina); `n8n/alfred-whatsapp.json` continua escrito à mão.

### Configuração (`perfis.yml → whatsapp:`)

| Campo | Função |
|-------|--------|
| `grupos` | `ignorar` (padrão) · `mencao` (só quando Pedro é mencionado) · `todos` |
| `pessoal_vira_pendencia` | `false` = só mensagens `profissional` viram pendência |
| `so_se_requer_resposta` | `false` = cria pendência mesmo para "ok, obrigado" |
| `confianca_minima` | abaixo disso profissional/pessoal viram `outro` (sem pendência) |
| `atraso_minutos` | espera isso (20) sem resposta de Pedro na conversa antes de gerar resumo, áudio e avisos; `fromMe` na conversa antes do prazo cancela. `0` = na hora. Ver [espera](#espera-antes-de-resumir-atraso_minutos) |
| `agrupar_minutos` | mensagens seguidas do mesmo contato entram na pendência aberta (sem novo LLM/áudio) |
| `audio_max_segundos` | corta a transcrição depois disso |
| `contatos_ignorar` / `contatos_sempre` | substrings de número ou nome: nunca / sempre viram pendência (sem LLM) |
| `pendencias.*` | voz, templates, `lembrar_a_cada_horas` |

### Testes

```bash
cd services/inbox
venv/bin/python -m unittest tests.test_whatsapp -v             # offline: LLM, TTS e transcrição mockados
ALFRED_TESTE_LIVE=1 venv/bin/python -m unittest tests.test_whatsapp -v   # Ollama + edge-tts reais (lento)
venv/bin/python tests/gerar_payloads.py [--audio]              # regrava tests/payloads/*.json (e o .ogg com edge-tts)
./run.sh whatsapp simular tests/payloads/evolution_audio.json --sem-enviar --imediato   # um payload contra o seu .env
```

Os payloads em `tests/payloads/` seguem o formato do evento `messages.upsert` da Evolution API v2 (texto, áudio OGG/Opus em base64 gerado com edge-tts, grupo, resposta de Pedro citando a pergunta, "ignorar", `fromMe`, spam, pessoal sem pedido). Os `evolution_v237_*.json` reproduzem o **formato real capturado na validação** (conteúdo sintético): `data.contextInfo`, áudio sem base64 com `mediaKey` em bytes JSON, resposta citada na conversa "Você", resposta direta citando o contato, grupo com `@lid`. 50 testes, incluindo a regressão de concorrência, a notificação no PC com a GUI simulada e a fila de espera (`TestFilaAtraso`, relógio mockado em `core._now`: agendada, cancelamento por `fromMe`, mensagens seguidas, criação ao vencer, thread da API, CLI). As outras classes rodam com `atraso_minutos: 0` (fluxo imediato). Os testes nunca chamam a Evolution: `EVOLUTION_URL` fica vazio.

## Podcast — "Resumo do Alfred", a cada 3 dias (`semanal.py`)

Antes só existia o áudio diário do jornal (monólogo de 2-3 min, `audio_jornal.md`). O episódio relê as edições do período e conta tudo em **diálogo de duas vozes**: Alfred (`pt-BR-AntonioNeural`) conduz e Lúcia (`pt-BR-FranciscaNeural`) faz as perguntas do ouvinte.

**Desde 20/10/2026 sai a cada 3 dias** (`perfis.yml → podcast.intervalo_dias: 3`), cobrindo os últimos 3 dias. O `alfred-semanal.timer` roda todo dia às 19:00 com `--se-devido`: só gera se o último episódio (o manifesto mais recente, por `gerado_em`) tiver 3 dias de calendário ou mais. O systemd não tem "a cada 3 dias" ancorado no calendário sem saltos na virada do mês, por isso a decisão fica no script. Arquivos: `AAAA-MM-DD_AAAA-MM-DD_resumo-do-alfred.{md,mp3}` e `manifestos/AAAA-MM-DD_AAAA-MM-DD.json` (com `periodo_inicio`/`periodo_fim`; a limpeza lê o conteúdo, não o nome).

**Fontes:** as edições do jornal do e-mail e, com `incluir_briefings: true`, os **briefings por RSS** de `services/briefing` (`VAULT_ALFRED/briefings/AAAA-MM-DD_*.md`, lista "Fontes citadas"). Em 20/10 a caixa de e-mail não tinha nenhuma newsletter nos últimos 4 dias — sem o RSS o episódio ficaria vazio.

```
./run.sh podcast                       # últimos 3 dias até hoje (sempre gera)
./run.sh podcast --se-devido           # o que o timer faz: só se o último tiver 3+ dias
./run.sh podcast --dias 7              # outro tamanho de período
./run.sh podcast --semana 2026-W43     # modo antigo: semana ISO
./run.sh podcast --dry-run             # só mostra os temas escolhidos (nada gravado, nada de LLM/TTS)
./run.sh podcast --sem-audio           # .md + manifesto, sem edge-tts
curl -X POST :7072/semanal -H "Authorization: Bearer $SHELL_EXECUTOR_TOKEN" -d '{"dias":3}'
```
(`semanal` continua funcionando como nome do subcomando.)

1. **Seleção:** junta as notícias de todas as edições `AAAA-MM-DD_*` do período em `VAULT_NOTICIAS/jornal` (e no antigo `VAULT_ALFRED/jornal`, e nos briefings), deduplica, agrupa com `lib_cluster` e pontua cada tema: `nº de notícias × peso das fontes × (1 + 0,5 por dia extra em que o assunto apareceu) + 0,3 por newsletter distinta`. Assunto que se repetiu na semana e saiu em várias newsletters sobe. Ficam os `max_temas` melhores.
2. **Roteiro em partes:** uma chamada ao LLM para a abertura e o encerramento (`podcast_semanal_moldura.md`) e uma por tema (`podcast_semanal_segmento.md`, ~`palavras_por_tema` palavras, 6-10 falas). Gerar em partes segura a duração: o gemma4 encurta roteiros longos pedidos de uma vez. Um tema com 1 notícia rende ~1 min e um tema rico rende 1,5-2 min. Com 7 temas o episódio fica em ~10-13 min. As falas são lidas linha a linha (`ALFRED:` / `LUCIA:`). Se o diálogo não vier no formato, o segmento cai para narração do Alfred.
3. **Áudio:** cada fala vira um .mp3 no edge-tts (4 em paralelo, 3 tentativas), e o ffmpeg concatena com `pausa_ms` de silêncio e uma `vinheta` opcional no início e no fim.
4. **Saídas** em `VAULT_NOTICIAS/podcast/`: `episodios/<AAAA-Www>_resumo-semanal.md` (frontmatter, `![[mp3]]`, temas com links e fontes, wikilinks para as edições usadas, transcrição), o `.mp3` e `manifestos/<AAAA-Www>.json`. Rodar de novo na mesma semana sobrescreve os três.

**Manifesto (contrato com a limpeza do Thunderbird):**
```json
{"semana": "2026-W43", "episodio_md": "...", "episodio_mp3": "... | null", "gerado_em": "ISO",
 "emails": [{"message_id", "from", "subject", "date", "pasta", "conta"}],
 "nao_resolvidos": [{"from", "subject", "date"}]}
```
`emails` lista só os e-mails cujas notícias **entraram em algum tema do episódio**. Desde esta versão o jornal diário grava um `.json` ao lado do `.md` com `message_id`/`pasta`/`conta` de cada newsletter. Nas edições antigas (só `.md`), o e-mail é procurado no Thunderbird pelo assunto e pelo remetente. O que não for achado vai para `nao_resolvidos`, sem `message_id`, e a limpeza deve ignorar. **`semanal.py` nunca apaga nem move e-mails.**

**Configuração** (`perfis.yml → inbox.<nome>.podcast`): `vozes`, `max_temas`, `min_noticias`, `palavras_por_tema`, `pausa_ms`, `vinheta`, `formato`, `template_segmento`, `template_moldura`. Os pesos das fontes vêm de `jornal.pesos_fontes`.

**Agendamento (proposto, não ativado):** `alfred-semanal.service` + `alfred-semanal.timer` (domingo 19:00, `Persistent=true`). Se o PC estava desligado e o timer só roda na segunda, o default já cai na semana anterior. Para instalar, siga o cabeçalho do `.service`.

**Testes:** `venv/bin/python -m unittest tests.test_semanal -v` (offline: semana ISO, parser de diálogo, leitura de edição antiga, resolução de e-mails).

## Agenda — evento / tarefa / lembrete → calendário do Thunderbird (`agenda.py`)

```
WhatsApp de um contato ("fechado: reunião quinta 14h")  ─┐
WhatsApp para você mesmo ("me lembra de pagar o boleto sexta") ─┼─ detectar_compromisso()  prefiltro + gemma4 (JSON Schema, agenda_detectar.md)
"vou" num convite de e-mail com data                     ─┘
        ↓ adicionar()  → VAULT_ALFRED/agenda/agenda.yml (status pendente) + tasks.yml (tarefa/lembrete)
        ↓ notificação [Ok] [Desfazer]   (nota para você mesmo: confirmação "📅 Anotado na agenda" no WhatsApp)
extensão "Alfred — agenda" no Thunderbird (a cada 2 min)
        GET /agenda/fila → cria o evento (com alarme 30 min antes) no calendário "Agenda" (Google via CalDAV
        → aparece no celular) → POST /agenda/{id}/resultado → status no_calendario
        Desfazer → status desfazer → a extensão remove o evento → desfeito
```

- Convite ainda não aceito ("consegue dia 25?") e conversa sem data não viram compromisso. Abaixo de
  `confianca_minima` (0.7), compromisso no passado ou sem data resolvida: ignorado.
- Mensagens que o próprio Alfred manda para a conversa "você" (resumos, confirmações) são reconhecidas
  pelo id enviado (`logs/whatsapp_ids_enviados.json`), pelas marcas 🎩/✔/📅/`ref:` e, para áudio, pela
  janela de 45 s após um envio — não viram lembrete.
- Grupos ficam de fora.

```bash
./run.sh agenda                                        # lista
./run.sh agenda anotar "reunião com a Attano sexta 14h" # detecta e anota (mesmo caminho do WhatsApp)
./run.sh agenda desfazer <id>
./run.sh agenda ics                                    # feed dos que ainda não chegaram ao calendário
```

API (Bearer): `GET/POST /agenda`, `GET /agenda/fila`, `POST /agenda/{id}/resultado`, `POST /agenda/{id}/desfazer`;
`GET /agenda/alfred.ics` sem token, só para 127.0.0.1 (dá para assinar no Thunderbird como agenda *Na rede*
caso não queira a extensão — somente leitura e não sincroniza com o celular).

Configuração em `perfis.yml → agenda:` (calendário, duração, alarme, confiança, o que é automático).

### Extensão "Alfred — agenda" (`thunderbird-ext-agenda/`)

O Thunderbird não tem API pública de calendário para extensões; a extensão traz uma *Experiment API*
(`experiment/implementation.js`) que usa os módulos internos `cal.manager` / `CalEvent` do Thunderbird
(conferidos no 157). Ela só cria e remove eventos que o Alfred mandou, pelo UID `alfred-…@alfred-pennyworth.local`.

**Validada em 20/10/2026** num perfil descartável do Thunderbird 157 (headless, calendário local de teste)
contra uma API de teste: listou os calendários, criou o evento com título/horário corretos, a API marcou
`no_calendario`, e o Desfazer removeu o evento. **Ainda não instalada no perfil real** — isso depende de Pedro:

1. `cd services/inbox/thunderbird-ext-agenda && zip -r ../alfred-agenda.xpi manifest.json background.js options.html options.js experiment`
2. Extensão não assinada: no Thunderbird, *Configurações → Geral → Editor de configuração*, `xpinstall.signatures.required = false`
   (ou carregue temporariamente em *Ferramentas de desenvolvedor → Depurar complementos → Carregar complemento
   temporário → manifest.json*, repetindo a cada abertura).
3. *Complementos → engrenagem → Instalar complemento de um arquivo* → `alfred-agenda.xpi`; aceite a permissão
   de "acesso total" (é o que toda Experiment API pede).
4. Opções da extensão: URL `http://127.0.0.1:7072`, token = `SHELL_EXECUTOR_TOKEN` do `.env`, calendário **Agenda**.
5. Teste: `./run.sh agenda anotar "teste do Alfred amanhã às 15h"` → em até 2 min aparece na Agenda → `./run.sh agenda desfazer <id>`.

## Limitações conhecidas

- WhatsApp: a Evolution API (Baileys) usa o WhatsApp Web não oficial — o número pode ser desconectado se a Meta detectar automação; mantenha o uso pessoal/moderado. O gemma4 transcreve bem português limpo; áudio com ruído ou muito longo (> 30 s é fatiado) perde qualidade — instale `faster-whisper` no venv se precisar de mais precisão. O envio para Pedro depende de `WHATSAPP_NUMERO_PEDRO`; a Evolution precisa de PostgreSQL (serviço `evolution-postgres` no compose).

- Só vê o que o Thunderbird já sincronizou offline. Se o Thunderbird ficar fechado, a INBOX local para no último sync (use `--desde` maior ou abra o Thunderbird antes do ciclo).
- Classificação é LLM: promoções disfarçadas de newsletter podem passar; ajuste `remetentes_ignorar`/`remetentes_noticia`.
- edge-tts usa o serviço de voz da Microsoft (único componente que sai da máquina, mesmo caso do briefing).
- Botões na notificação: `notify-send --action` **não funciona nesta máquina** (Ubuntu + Plasma 6.6): o perfil AppArmor `notify-send` bloqueia `GetCapabilities` para o `plasmashell` (`apparmor="DENIED" ... label="notify-send"` no journal) e ele responde "Actions are not supported". `gdbus call` também não serve: o Plasma fecha na hora (motivo 1) a notificação com ações cujo remetente saiu do barramento. Por isso os aguardadores usam `notificar_dbus.py` (Python do sistema + PyGObject: a mesma conexão envia e espera o clique, urgência crítica, corpo escapado para a marcação do Plasma) → `kdialog` → `zenity`. Sem nenhum, a pergunta só aparece como notificação simples e a resposta vem por `perguntar`, CLI, chat ou N8N.
- Extração usa *structured outputs* do Ollama (JSON Schema em `core.py`): sem isso o gemma4 às vezes ecoava o prompt (`{"links": [...]}`) ou truncava o JSON. URLs de rastreamento com mais de 300 caracteres ficam fora do prompt, mas ainda podem virar o link da pendência via fallback por texto-âncora.
