# Avaliação — E-mail: IMAP/SMTP do Odysseus × cópia local do Thunderbird no Alfred

Data: 03/11/2026. Odysseus lido no commit `c21980f` (clone raso, só leitura; nada foi copiado).
Medições feitas só com leitura do perfil do Thunderbird, de `logs/inbox_seen.json` e do journal do
systemd de usuário. Nenhuma senha, app password ou OAuth foi usada. Nada foi gravado na caixa nem no perfil.

**Resumo.** O IMAP do Odysseus não serve para o Hotmail de Pedro. Ele não tem OAuth da Microsoft nem IDLE.
O problema medido no Alfred é outro, e é barato de resolver sem IMAP:

- **55% dos e-mails do Hotmail desde 20/10 nunca entraram num ciclo** (89 de 161). O Thunderbird ficou
  fechado 87% do tempo em que a máquina estava ligada, e a janela de 36 h do ciclo é contada pelo cabeçalho
  `Date`.
- Os que entraram levaram 20,5 h (mediana) da chegada até o Alfred.

Abrir o Thunderbird com a sessão e disparar o ciclo quando o mbox muda dá o mesmo frescor que o IMAP IDLE
(mediana 0 h, 61% em até 15 min). Esse frescor tem o mesmo teto: a máquina ligada. **Nota 3/10:
adaptar, sem IMAP.**

## 1. O que o Odysseus faz

- **Contas genéricas IMAP/SMTP**, gravadas no SQLite (`core/database.py`, `EmailAccount`). Senhas e tokens
  ficam cifrados com Fernet, com a chave em `data/.app_key` ao lado do banco (`src/secret_storage.py`). O
  próprio arquivo diz que isso protege contra vazamento do `.db`, não contra quem lê o processo ou a chave.
- **OAuth2 só do Google**: `_xoauth2_raw` / refresh em `oauth2.googleapis.com`
  (`routes/email/email_helpers.py`), rotas `/api/email/oauth/google/authorize|callback`
  (`routes/email/email_routes.py`). Para Microsoft, o próprio código traduz a falha de login em "o Odysseus
  ainda não suporta OAuth/Graph da Microsoft" (`email_helpers.py`, por volta da linha 310). Uma conta
  Outlook/Hotmail não pode ser adicionada.
- **Sem IMAP IDLE.** O `_auto_summarize_poller` (`routes/email/email_pollers.py`) acorda a cada 60 s com
  auto-resposta ligada, senão a cada 1800 s. Ele faz `UID SEARCH SINCE` (data de chegada no servidor) no
  último dia e resume, marca urgência e responde ausência. O `_scheduled_email_poller` envia os agendados a
  cada 30 s.
- **Ações direto no servidor** (`email_routes.py`, ~60 rotas): ler, buscar, flags (lido, estrela,
  respondido), mover/arquivar com `UID MOVE`, "apagar" = mover para a Lixeira, apagar de vez, em lote,
  descadastro de newsletters, rascunho, envio e agendamento.
  - A Lixeira é criada se não existir. O apagar é idempotente: um UID que já sumiu conta como sucesso, e o
    UID é re-resolvido pelo Message-ID.
  - O módulo não guarda UIDVALIDITY nem usa CONDSTORE.
- **Desempenho da interface**: cache de lista (45 s) e de leitura (30 min), pool de uma conexão IMAP por
  conta (reciclada após 60 s ociosa), pré-busca das primeiras mensagens.
- **Ferramentas para o agente**: o servidor MCP `mcp_servers/email_server.py` expõe 19 ferramentas
  (listar, buscar, ler, enviar, rascunho, responder, arquivar, apagar, marcar, lote, bloquear remetente,
  descadastrar, varrer spam).

## 2. O que o Alfred faz hoje

- **Leitura** (`services/_shared/lib_mail.py`): o Alfred lê o mbox offline que o Thunderbird (snap) mantém
  no perfil, sem rede e sem senha.
  - `fetch_messages` lê o arquivo inteiro, faz o parse completo de cada mensagem e só depois filtra pela
    data do cabeçalho `Date`.
  - `ids_no_indice` (leitor Mork) confere o índice `.msf`. Só a limpeza liga esse filtro
    (`checar_indice=True`, ver `services/inbox/LIMPEZA.md`).
  - `imap_fetch` com senha existe, mas não serve para o Hotmail.
- **Ciclo**: o `alfred-inbox.timer` roda às 07:30 e às 18:30 (`Persistent=true`) e chama
  `core.ciclo` → `coletar` (`services/inbox/core.py`). O `coletar` lê só a INBOX da conta
  `outlook.office365.com`, com janela de 36 h no `Date` e no máximo 80 mensagens. Depois o ciclo descarta o
  que já está em `logs/inbox_seen.json` e classifica o resto no LLM. Daí saem jornal, pendências e WhatsApp.
  Não há recuperação: o que ficou fora da janela de 36 h não volta.
- **Ações na caixa**: só pelas extensões.
  - `services/inbox/thunderbird-ext/` move para a Lixeira com a permissão `messagesMove` (nunca `delete`),
    só nas contas marcadas.
  - `thunderbird-ext-agenda/` cuida da agenda.
  - Em 02/11 a extensão de limpeza não estava instalada. Ela é carregada como complemento temporário e some
    quando o Thunderbird fecha.
- **Vigia**: o `alfred-vigia.timer` roda a cada 10 min e registra no journal `Thunderbird aberto: sim|não`.
  Ele só informa.
- **Thunderbird**: duas contas IMAP com OAuth2 (`authMethod=10`), Hotmail (`outlook.office365.com`) e Gmail.
  IDLE e verificação ficam no padrão. A cópia offline é um mbox (`berkeleystore`).

## 3. Método

Scripts em `docs/avaliacoes/scripts/` (só leitura). Rode-os da raiz do repositório:

```bash
python3 docs/avaliacoes/scripts/email_confiabilidade.py            # fantasmas/duplicados por pasta
python3 docs/avaliacoes/scripts/email_frescor.py --conta hotmail   # atrasos, perdidos, cenários
python3 docs/avaliacoes/scripts/email_frescor.py --conta gmail     # só o atraso do Thunderbird
python3 docs/avaliacoes/scripts/email_custo.py --rep 5             # tempo/CPU/RSS das varreduras
```

`email_comum.py` importa o `lib_mail` sem carregar o `lib_alfred` e dá rótulos anônimos às pastas: os nomes
padrão ficam, o resto vira `pasta-N`. Nenhum script imprime remetente, assunto ou corpo.

**Frescor.** Para cada Message-ID da INBOX, o script pega a primeira cópia no mbox (o arquivo só cresce, por
append) e três instantes:

- `t_srv`: data do `Received` mais alto, o carimbo do servidor da caixa;
- `t_tb`: data da linha separadora `From - …`, quando o Thunderbird gravou a cópia offline;
- `t_alf`: o primeiro ciclo efetivo depois de `t_tb`. Ciclo efetivo é o que registrou
  `[mail] … selecionadas` no journal do `alfred-inbox.service`.

Com eles:

- `A = t_tb − t_srv` é o atraso do Thunderbird;
- `B = t_alf − t_tb` é a espera pelo ciclo;
- o e-mail está **perdido** se `Date < t_alf − 36 h`, porque o ciclo nunca o seleciona.

Período: cópias gravadas a partir de 20/10/2026, o início do journal. Os cenários usam a mesma
cronologia:

- as amostras do vigia, a cada 10 min, servem de "máquina ligada e sessão ativa";
- "TB aberto com a sessão" = `A` zero sempre que a máquina estava ligada.

Dois cuidados validados:

1. **A linha `From - …` do Thunderbird snap está em UTC.** O sandbox não tem fuso: uma mensagem que chegou
   às 04:47 (-03) aparece como `07:48`. Lida como hora local, todo atraso sai 3 h maior. O script usa UTC
   (`--tz-separador`).
2. **O modelo bate com o que o Alfred registrou.** Dos 27 e-mails do período que estão no
   `inbox_seen.json`, todos foram processados no ciclo que o modelo prevê, no máximo 3,2 min depois do
   início (o tempo do LLM). Nenhum dos 89 "perdidos" consta do `inbox_seen.json`.

**Confiabilidade.** Por pasta, compara o mbox (cabeçalhos, mesmo separador e mesmo critério de fragmento do
`lib_mail`) com a tabela de mensagens do `.msf`, lida pelo leitor Mork do próprio `lib_mail`.

**Custo.** Cada repetição roda em subprocesso novo, para o pico de RSS ser só daquele cenário. São 5
repetições por cenário, com o mbox no cache de páginas (o Thunderbird estava aberto). Limpar o cache exigiria
root.

**Ruído.** Nenhuma medição usa Ollama nem GPU, por isso nenhuma passou pelo `flock` da GPU. A CPU estava
dividida com as outras 7 avaliações e com a produção: load average entre 6 e 10 em 12 CPUs durante as
medições. Os tempos de parede são, portanto, um teto. A CPU por processo é mais estável.

**IMAP direto.** Não medido ao vivo, porque exigiria credencial (parei antes, como pedido). Foi avaliado
lendo:

- o código do Odysseus;
- a documentação da Microsoft sobre OAuth em IMAP
  ([Authenticate an IMAP, POP or SMTP connection using OAuth](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth));
- o aviso do fim da autenticação básica no Outlook.com
  ([Modern Authentication Methods now needed…](https://support.microsoft.com/en-us/topic/c5d65390-9676-4763-b41f-d7986499a90d));
- o Python do venv do Alfred: 3.14.4, que já traz `imaplib.IMAP4.idle()`.

## 4. Resultados

### 4.1 Frescor (Hotmail, INBOX, 161 e-mails gravados no mbox de 20/10 a 03/11)

| Medida | Valor |
|---|---|
| Disparos do `alfred-inbox.timer` / ciclos efetivos | 48 / 39 (9 sem coleta: modo jogo ou saída sem ler o mbox; nenhum ciclo efetivo desde 02/11 07:30) |
| Thunderbird aberto (amostras do vigia) | 157 de 1168 = **13%** do tempo com a máquina ligada |
| Máquina ligada (amostras × 10 min) | ≈ 195 h de 332 h = **59%** |
| A, atraso do Thunderbird: p50 / p90 / máx | **30,1 h** / 108 h / 131 h; só 17% em ≤ 5 min e 22% em ≤ 1 h |
| B, espera pelo ciclo (dos vistos): p50 / p90 / máx | 5,1 h / 23,8 h / 23,8 h |
| Chegada → Alfred (dos vistos): p50 / p90 / máx | **20,5 h** / 28,9 h / 32,8 h |
| Vistos por um ciclo | 59 (37%) |
| **Perdidos pela janela de 36 h** | **89 (55%)**; 75 deles só porque o Thunderbird ficou fechado mais de 36 h |
| Pendentes (sem ciclo efetivo desde que chegaram) | 13 (8%) |

Para comparar, a INBOX do Gmail (que o Alfred não lê) teve `A` com p50 de 52 h e p90 de 413 h, com 8% em
≤ 5 min. É o mesmo Thunderbird fechado.

**Cenários** sobre os mesmos 161 e-mails (chegada no servidor → Alfred):

| Cenário | p50 | p90 | máx | ≤ 15 min | Perdidos |
|---|---|---|---|---|---|
| Hoje (timer 07:30/18:30, janela no `Date`, TB como foi usado) | 20,5 h | 28,9 h | 32,8 h | 1,7% dos vistos | 89 |
| Ciclo disparado pela mudança do mbox, TB como foi usado | 30,1 h | 108 h | 131 h | 17% | 0 |
| TB aberto junto com a sessão, timer de hoje | 4,1 h | 23,8 h | 30,0 h | 2,7% | 2 |
| **IMAP IDLE no Alfred** ou **TB aberto com a sessão + ciclo pela mudança do mbox** | **0 h** | 18,9 h | 38,2 h | **61%** | 0 |

O p90 de 18,9 h na última linha é a máquina desligada. Nenhum leitor local, IMAP ou Thunderbird, enxerga a
caixa nesse tempo. Só o timer, com a máquina sempre ligada e o TB aberto, já daria espera média de 6,0 h e
máxima de 13 h.

### 4.2 Confiabilidade: mbox × índice `.msf`, por pasta

`dup` = cópias extras do mesmo Message-ID no mbox. `fantasmas` = no mbox, mas fora do índice (já saíram da
pasta no servidor). `faltantes` = no índice, sem cópia offline (o Alfred não as vê).

| Conta | Pasta | MB | Cópias no mbox | Message-IDs | dup | No índice | Fantasmas | Faltantes | MB recuperáveis |
|---|---|---|---|---|---|---|---|---|---|
| hotmail | **INBOX** | 39,9 | 628 | 540 | **88 (16%)** | 259 | **281 (52%)** | 0 | **23,3 (58%)** |
| hotmail | Sent | 653,9 | 198 | 198 | 0 | 198 | 0 | 0 | 0 |
| hotmail | Archive + pasta-1..4 | 1,9 | 59 | 59 | 0 | 59 | 0 | 0 | 0 |
| gmail | INBOX | 27,1 | 373 | 373 | 0 | 366 | 7 (1,9%) | 0 | 0,6 |
| gmail | [Gmail]/Todos os e-mails | 16,3 | 215 | 215 | 0 | 389 | 0 | 174 | 0 |
| gmail | [Gmail]/Lixeira | 0,6 | 7 | 7 | 0 | 259 | 0 | 252 | 0 |
| gmail | [Gmail]/Importante | 0,2 | 6 | 6 | 0 | 17 | 0 | 11 | 0 |
| gmail | outras 4 pastas | 4,2 | 20 | 20 | 0 | 20 | 0 | 0 | 0 |
| local | pasta-5 | 0,0 | 1 | 1 | 0 | 1 | 0 | 0 | 0 |
| **Total** | | 744,1 | 1507 | 1419 | 88 | 1568 | 288 | 437 | 23,9 |

O que os números mostram:

- **Nenhuma cópia tem a flag expunged** (`X-Mozilla-Status` 0x0008), então o `lib_mail` não tem como
  descartar fantasmas sem o índice.
- **Os 88 duplicados vêm de um único evento.** As primeiras cópias foram gravadas em 06/10 às 01:59, na
  sincronização inicial. As segundas foram regravadas em 19/10 entre 10:06 e 10:07, numa ressincronização
  da pasta. As 88 continuam no índice, então não são fantasmas. O ciclo as descarta pelo `inbox_seen.json`.
- **A INBOX do Hotmail nunca foi compactada.** São 23 MB de lixo, e o `prefs.js` não muda o limiar de
  compactação automática do Thunderbird (padrão: 200 MB).
- **Fantasmas surgem rápido.** Dos 68 e-mails dos últimos 7 dias na INBOX, 19 (28%) já saíram da caixa no
  servidor (apagados no celular ou no webmail). Na janela de 36 h do ciclo de hoje há 0 de 13. Como o ciclo
  não confere o índice, ele pode criar pendência para um e-mail que Pedro já apagou, se o apagou antes do
  ciclo seguinte.
- **No Gmail, o Thunderbird não guarda offline** a Lixeira e boa parte de "Todos os e-mails": 437 mensagens
  só existem no índice. Hoje isso não afeta o Alfred, que lê só a INBOX do Hotmail.

### 4.3 Custo de varredura do `lib_mail` (mediana de 5 repetições, mbox no cache)

| Cenário | O que lê | Mensagens devolvidas | Parede (mín–máx) | CPU | Pico de RSS |
|---|---|---|---|---|---|
| `ciclo` (= `core.coletar` no timer) | INBOX Hotmail, 39,9 MB; janela de 36 h | 13 | 7,6 s (7,2–8,6) | 6,1 s | 158 MB |
| `limpeza` (= `limpeza.candidatos`) | INBOX Hotmail + `.msf` | 628 | 7,1 s (6,4–8,1) | 5,6 s | 169 MB |
| `indice` (só `ids_no_indice`, Mork) | `INBOX.msf` | 259 IDs | 0,04 s | 0,04 s | 29 MB |
| `completa` (todas as contas × pastas, com índice) | 744 MB (654 MB são a pasta Sent do Hotmail) | 1507 | 33,0 s (30,6–39,0) | 30,8 s | **2,0 GB** |

Load average de 10,7 no início e 18,8 no fim. Uma rodada isolada antes, com load ~10, deu 5,0 s de parede e
4,7 s de CPU para o `ciclo`.

- **Para devolver 13 mensagens, o ciclo faz o parse completo das 628 do mbox.** São ~9,7 ms de CPU por
  mensagem, e o filtro por `Date` só vem depois. Uma leitura incremental (só o que foi gravado depois do
  último ciclo) custaria ~0,13 s.
- **Uma varredura completa leva 2 GB de RAM**, porque `_iter_mbox_raw` lê o arquivo inteiro e o quebra em
  linhas. Nenhum caminho de produção faz isso hoje; um leitor futuro "de tudo" precisaria de leitura em
  streaming.

Referência: o Thunderbird aberto agora (6,6 h) ocupa ~600 MB de RSS em 7 processos e gastou 245 s de CPU,
≈ 1% de um núcleo.

### 4.4 IMAP direto: o que traria e quanto custaria

**O que traria a mais:**

| Item | Ganho real para o Alfred | Já dá para ter sem IMAP? |
|---|---|---|
| IDLE em tempo real | Latência de segundos com a máquina ligada (cenário da última linha de 4.1) | **Sim.** TB aberto com a sessão + ciclo disparado pela mudança do mbox (unit `.path` do systemd) ou pelo `messages.onNewMailReceived` da extensão. Mesmo resultado medido. |
| Não depender do TB aberto | É o que derruba hoje 55% dos e-mails | Em parte: abrir o TB minimizado no login custa ~600 MB de RAM. Um processo Python com IDLE custaria ~30–50 MB (estimativa: Python com `imaplib`+`ssl` medido em 18 MB, mais o `msal`). |
| Verdade do servidor (sem fantasmas nem duplicados) | Elimina os 281 fantasmas e os 88 duplicados | Já filtrado na limpeza pelo `.msf`. Estender `checar_indice` ao ciclo custa ~0,05 s. |
| Flags, mover e apagar sem extensão | Acaba com a fricção da extensão: a carga temporária some ao fechar o TB, e a limpeza ficou parada de 20/10 a 02/11 | Instalar o `.xpi` de vez resolve a fricção sem dar ao Alfred um escopo de escrita na caixa. |
| Gmail e outras contas | Só se Pedro quiser o Alfred lendo o Gmail | O mbox do Gmail já está no perfil: é configuração, não código. |

**O que custaria:**

1. **OAuth2 da Microsoft para o Hotmail.** A autenticação básica do Outlook.com acabou em 16/09/2024, e
   senha comum e app password não abrem mais IMAP.
   - **Registrar um app no Microsoft Entra.** Exige um tenant Entra; tipo de conta "contas pessoais
     Microsoft".
   - **Consentir os escopos** `https://outlook.office.com/IMAP.AccessAsUser.All` e `offline_access`.
   - **Obter o token** por fluxo de código de dispositivo, autenticando com `AUTHENTICATE XOAUTH2`.
   - O fluxo client credentials, sem usuário, só existe para tenants Microsoft 365, não para contas
     pessoais.
   - **Reaproveitar o client-id ou o token do Thunderbird foi rejeitado** no `LIMPEZA.md`: seria contornar a
     proteção de credenciais. Continua rejeitado.
   - **Parei aqui:** qualquer passo adiante exige Pedro logar e consentir.
2. **Guardar a credencial.**
   - O refresh token vale o tempo que a Microsoft quiser: expira por inatividade e pode ser revogado. Ele dá
     **leitura e escrita na caixa inteira**, um escopo maior que o `messagesMove` da extensão.
   - O Odysseus guarda tokens com Fernet e a chave no disco ao lado do banco. No Alfred seria preciso, no
     mínimo, Secret Service/keyring ou `systemd-creds`, fora do `.env`.
   - Um bug numa ação de mover vira perda de e-mail no servidor, sem a trava da extensão.
3. **Código.** Seria preciso escrever:
   - autenticação XOAUTH2 e renovação do token;
   - um serviço de IDLE com reconexão (o IDLE precisa ser renovado em até 29 min, RFC 2177) e uma unit nova;
   - estado de UID/UIDVALIDITY por pasta;
   - busca incremental;
   - mover para a Lixeira com `UID MOVE` e flags;
   - substituir o executor da extensão na limpeza;
   - testes com servidor IMAP falso.

   O Python 3.14 do venv já tem `imaplib.IMAP4.idle()`; faltaria só `msal`.
4. **Gmail**, se entrar: outro app OAuth no Google Cloud. Em modo de teste, o refresh token expira em 7 dias;
   o escopo `https://mail.google.com/` é restrito e exige verificação para publicar. A alternativa seria app
   password (não criada aqui).
5. **GPU.** IDLE manda e-mails ao longo do dia, um a um, em vez de dois lotes. Cada e-mail novo vira uma
   chamada ao `gemma4:12b` na RTX 3060 compartilhada. Isso conflita com o modo jogo e com o Laya, a menos
   que os e-mails sejam agrupados em lotes (ex.: a cada 15 min).

## 5. Pontuação

Pontuei "aplicar a tecnologia do Odysseus (IMAP/SMTP genérico, ou um IMAP próprio com OAuth da Microsoft) no
lugar da leitura do Thunderbird":

| Critério | Nota | Por quê |
|---|---|---|
| Ganho para Pedro | **2/5** | O ganho medido (55% de perdidos → 0; 20,5 h → minutos) vem quase todo de consertar a janela e de manter o TB aberto. Sobram para o IMAP: não depender do TB (economiza ~550 MB de RAM), fantasmas zero e ações sem extensão. O Odysseus em si não suporta Hotmail. |
| Custo de implementar | **24–32 h**, mais ~1 h de Pedro no Entra | Arquivos: `services/_shared/lib_mail.py` (ou um `lib_imap.py` novo), `services/inbox/core.py` (`coletar`), um serviço de IDLE novo + unit em `services/inbox/`, `services/inbox/limpeza.py` e o executor, `services/_shared/perfis.yml`, `.env.example`, `services/scripts/instalar.sh`, testes em `services/inbox/tests/`. |
| Risco | **4/5** | Token com escrita na caixa inteira guardado no disco; políticas e expiração de token da Microsoft; bug de mover = perda real no servidor; troca de um caminho que hoje funciona sem rede e sem senha. |
| Recursos na máquina | VRAM: 0 direto (risco de chamadas ao LLM espalhadas); CPU: ~0 em IDLE; RAM: ~30–50 MB contra ~600 MB do TB aberto; latência: segundos | A leitura atual custa 5–8 s de parede e ~6 s de CPU por ciclo, duas vezes ao dia. Não é gargalo. |
| **Nota final** | **3/10** | IMAP direto resolveria o frescor, mas a mesma latência medida sai de manter o TB aberto e disparar o ciclo pela mudança do mbox. A tecnologia do Odysseus não cobre o Hotmail e traz um token de escrita ampla que hoje não existe. |

## 6. Veredito

**Adaptar; não aplicar o IMAP do Odysseus.** O IMAP com OAuth da Microsoft fica como plano B, como o
`LIMPEZA.md` já registra, para o dia em que o Thunderbird deixar de ser o cliente. Em ordem de valor por
hora:

1. **Acabar com os perdidos** (~2 h; `services/inbox/core.py`, `coletar`).
   - Selecionar pelo que é novo **no mbox**, e não pelo `Date`: hora da linha `From - …` em UTC, ou offset
     do arquivo, depois do último ciclo efetivo, com o `inbox_seen.json` deduplicando.
   - Alternativa mínima: janela = desde o último ciclo efetivo menos 36 h.
   - Sozinho, isso leva os perdidos de 55% para 0.
2. **Manter o Thunderbird aberto com a sessão** (~0,5 h).
   - Uma entrada de autostart com o TB minimizado (`services/scripts/instalar.sh`).
   - Ou o vigia avisar quando o TB estiver fechado e houver ciclo perto.
   - Com o timer de hoje, a mediana cai de 20,5 h para 4,1 h.
3. **Disparar o ciclo quando o mbox da INBOX mudar** (~3 h).
   - Uma unit `alfred-inbox.path` (`PathModified=`) com intervalo mínimo entre ciclos, por exemplo 15 min,
     para agrupar chamadas ao LLM.
   - Ou o evento `messages.onNewMailReceived` na extensão chamando o Alfred.
   - Com o item 2: mediana 0 h, 61% em ≤ 15 min, igual ao IMAP IDLE.
4. **Instalar a extensão de limpeza de vez** (`.xpi`), e não como complemento temporário (~0,5 h de Pedro).
   É a fricção que o "mover sem extensão" do IMAP resolveria.
5. Opcional:
   - ligar `checar_indice` no ciclo e no semanal, para não criar pendência de e-mail já apagado (~1 h);
   - compactar a INBOX do Hotmail uma vez no Thunderbird, o que recupera 23 MB e zera 281 fantasmas e 88
     duplicados até a próxima leva.

**Menor próximo passo:** o item 1. Em `core.coletar`, depois de `fetch_messages`, não descartar por `Date`
quem chegou ao mbox depois do último ciclo efetivo. Teste: um mbox sintético com uma mensagem de `Date`
antigo e linha `From - …` recente, que hoje é descartada e deve passar a entrar.
