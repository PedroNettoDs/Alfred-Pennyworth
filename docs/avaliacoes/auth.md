# Avaliação: Auth (Odysseus `core/auth.py` × Alfred token mestre)

Data: 03 a 05/11/2026. Odysseus no commit `c21980f` (clone raso, só leitura; nenhum trecho copiado; o Odysseus não
foi executado). Alfred no commit `4c30b84` (branch `laya`). Scripts em [`scripts/`](scripts/): `auth_rotas.py`,
`auth_testes.py` (com a saída em `auth_testes.saida.txt`) e `auth_custo_verificacao.py`.

Escopo: avaliação **defensiva** de projeto. Nada foi disparado contra os serviços em produção (7071 a 7074) e não
houve teste de força bruta nem de timing. As medições são: leitura de código, inventário estático (AST),
testes em processo (`TestClient` com token fictício e vault temporário) e a suíte existente.

## 1. O que o Odysseus faz

- **Contas e senha** (`core/auth.py`, `AuthManager`): várias contas em `data/auth.json`, senha com bcrypt,
  sessão = token aleatório de 32 bytes guardado no servidor (`sessions.json`), validade de 7 dias. Apagar ou
  renomear conta revoga as sessões dela.
- **2FA TOTP** (`core/auth.py`, `totp_*`; rotas em `routes/auth_routes.py`): segredo `pyotp` gerado como
  "pendente", só vira ativo depois que um código confere (janela ±1 passo de 30 s). Ao ativar, gera 8 códigos de
  recuperação de 8 caracteres hex, de uso único. Desligar exige a senha. O login é em duas etapas: senha certa
  sem código devolve `requires_totp`; com código errado, 401.
- **Limite de tentativas** (`src/rate_limiter.py` + `routes/auth_routes.py`): janela deslizante em memória,
  chave = IP do cliente. Login: 15 por 60 s; cadastro e setup inicial: 3 por 300 s. Passou do limite, 429.
- **Tokens de API com escopo** (`routes/api_token_routes.py`, modelo `ApiToken` em `core/database.py`, checagem
  em `app.py` `AuthMiddleware` e `src/auth_helpers.py` `require_api_token_scope`): token `ody_…` mostrado uma
  vez, guardado como hash bcrypt com prefixo de 8 caracteres para achar o candidato. Cada token tem dono e uma
  lista de 14 escopos (`chat`, `todos:read|write`, `documents:*`, `email:read|draft|send`, `calendar:*`,
  `memory:*`, `cookbook:*`), mais perfis prontos (ex.: só rascunho de e-mail). Rotas sensíveis pedem o escopo e
  devolvem 403 sem ele; o token nunca herda a autoridade de sessão do dono (ex.: shell). Revogar = apagar a linha.
- **Bypass de localhost desligado por padrão** (`app.py`, `LOCALHOST_BYPASS=false`): mesmo ligado, só vale para
  conexão direta de `127.0.0.1`/`::1` **sem** cabeçalhos de proxy (`X-Forwarded-For`, `CF-Connecting-IP`…),
  para um túnel não herdar a confiança local. `AUTH_ENABLED=false` desliga tudo, com aviso.
- **Token interno de ferramenta** (`core/middleware.py`): segredo por processo, só aceito de loopback direto,
  para o agente chamar rotas de admin sem o cookie do usuário.

## 2. O que o Alfred faz hoje

Um único segredo, `SHELL_EXECUTOR_TOKEN` (o "token mestre"), protege tudo:

- **Painel** (`services/inbox/painel.py`, rotas `/painel/*` na porta 7073):
  - toda rota (até login e estáticos) passa por `_rede_ok`: cliente `127.0.0.1`/`::1` **e** cabeçalho `Host`
    local (contra DNS rebinding); fora disso, 403;
  - login (`POST /painel/login`) compara o token digitado com `hmac.compare_digest`; errou, espera
    `ATRASO_LOGIN_FALHO = 0.5` s com `asyncio.sleep` e devolve 401. Não há contador nem bloqueio;
  - sessão sem estado: cookie `v1.<expira>.<nonce>.<HMAC-SHA256(token)>`, HttpOnly, SameSite=Strict,
    `PAINEL_SESSAO_DIAS` (padrão 7). "Sair" revoga o nonce em memória; trocar o token derruba todas;
  - todo POST exige `X-CSRF-Token` (HMAC do nonce) e `Origin`/`Referer` da própria origem; CSP sem inline.
- **API do inbox** (`services/inbox/main.py`, 7073, escuta em `0.0.0.0`): `check_auth` compara
  `Authorization == "Bearer " + token` com `!=` (comparação comum de string). Sem limite de tentativas nem atraso.
  O webhook do WhatsApp aceita `EVOLUTION_API_KEY` (cabeçalho `apikey`, Bearer ou campo `apikey` do JSON) **ou**
  o token mestre. `/agenda/alfred.ics` só responde a `127.0.0.1`.
- **Executor** (`services/shell-executor/main.py`, 7071, `0.0.0.0`): `POST /execute` com o mesmo Bearer e `!=`.
  Allowlist de comandos sem shell (ver `docs/avaliacoes/shell.md`).
- **Researcher** (`services/researcher/main.py`, 7072, `0.0.0.0`): `POST /research` e `GET /research/{id}` com o
  mesmo Bearer e `!=`.
- **Laya** (7074): escuta só em `127.0.0.1`.
- **Rede** (README, "Rede: quem alcança o quê"): ufw com entrada DROP e exceção `allow in on br-alfred` para
  7071:7073 e 11434. Containers chegam ao host pela bridge `br-alfred`.

## 3. Método

Tudo roda local, sem rede de produção e sem GPU (nenhuma medição usou Ollama; o harness de teste aponta
`OLLAMA_BASE_URL` para `127.0.0.1:9`, e a linha `[ollama_generate] Erro: Connection refused` nos logs dos testes é essa
porta morta; por isso não foi preciso `flock`). O `.env` não foi lido: só o modo do arquivo (`stat -c %a`).

1. **Código dos dois lados.** Odysseus: `core/auth.py`, `core/middleware.py`, `src/rate_limiter.py`,
   `routes/auth_routes.py`, `routes/api_token_routes.py`, `src/auth_helpers.py`, trecho `AuthMiddleware` de `app.py`.
   Alfred: `services/inbox/painel.py`, `services/inbox/main.py`, `services/shell-executor/main.py`,
   `services/researcher/main.py`, `services/_shared/lib_ferramentas.py`, extensões do Thunderbird, fluxos N8N.
2. **Inventário estático de rotas e do tipo de credencial** (`auth_rotas.py`, módulo `ast`, não importa nem chama nada):
   ```bash
   python3 -I docs/avaliacoes/scripts/auth_rotas.py
   ```
3. **Contrato de autenticação em processo** (`auth_testes.py`): o app do inbox e o do executor são chamados dentro do
   processo com um IP de container (`172.17.0.2`), sem credencial, token errado, token vazio no servidor, chave da
   Evolution e token mestre. Usa o harness de `services/inbox/tests/test_painel.py`. Comando (o venv do inbox fica no
   checkout principal):
   ```bash
   cd services/inbox && /home/netto/dev/Alfred-Pennyworth/services/inbox/venv/bin/python ../../docs/avaliacoes/scripts/auth_testes.py -v
   ```
4. **Testes existentes do Painel** (sessão, cookie, CSRF, rede, cabeçalhos, áudio):
   ```bash
   cd services/inbox && venv/bin/python -m unittest tests.test_painel.TestAutenticacao tests.test_painel.TestRede \
     tests.test_painel.TestCSRF tests.test_painel.TestCabecalhos tests.test_painel.TestAudio
   ```
   E a suíte completa (`unittest discover -s tests -t .`, em segundo plano, com `nice`).
5. **Superfície do host (leitura passiva):** `ss -ltn`, `ip -br addr`, `docker ps`, `docker network inspect
   alfred_alfred-net`, `systemctl is-active ufw`, `/etc/ufw/ufw.conf`. Nenhuma conexão foi aberta para as portas.
6. **Inventário de clientes do token:** `grep -rn SHELL_EXECUTOR_TOKEN` e leitura de `background.js`/`executor.js` das
   extensões, dos JSON do N8N e de `lib_ferramentas.py`.
7. **Custo de verificar token** (`auth_custo_verificacao.py`): bcrypt (como o Odysseus guarda os tokens) contra
   SHA-256 + `compare_digest`, microbenchmark local com token gerado na hora.
8. **Custo de implementar:** estimativa por contagem de pontos de chamada e do que o Odysseus precisou. Nada foi
   prototipado em código de produção.

Ruído: o host estava com swap em 8,1 de 8,2 GB durante as medições, então os tempos da suíte (29,6 s) e do
microbenchmark valem como ordem de grandeza.

## 4. Resultados

### 4.1 Superfície real (o que alcança 7071 a 7074)

| Item | Medido |
|---|---|
| Sockets (`ss -ltn`) | 7071, 7072 e 7073 em `0.0.0.0`; 7074 (Laya) em `127.0.0.1` |
| Interfaces do host (`ip -br addr`) | `lo`; `enp42s0` (LAN); `tailscale0`; `br-alfred` (172.21.0.1); duas bridges de outros projetos (172.18.x e 172.19.x) no ar; `docker0` e `virbr0` desligadas |
| ufw | ativo (`systemctl is-active`, `ENABLED=yes`). **Regras não lidas**: `/etc/ufw/user.rules` é `root:root 640`. O README diz: entrada DROP + `allow in on br-alfred` para 7071:7073 e 11434 |
| Containers em `br-alfred` | 4: `alfred-evolution`, `alfred-evolution-db`, `alfred-chromadb`, `alfred-searxng`. Pela regra do README todos chegam a 7071–7073, mas só a Evolution precisa de 7073 (webhook) |
| N8N | **container não está rodando** (`docker ps`); o Open WebUI também não. Os 3 fluxos N8N do repositório existem, mas hoje ninguém os chama |
| Container em rede `host` | `open-notebook` (outro projeto). Pela rede `host`, o Alfred o vê como `127.0.0.1`, então passaria na checagem do Painel (`_rede_ok`) e do feed `.ics` (inferência, não testado) |
| Evolution (8081) | publicada em `127.0.0.1` |

Rotas (inventário estático): **64** no total. Inbox 37, executor 2, researcher 3, Painel 22. As que aceitam o token
mestre: inbox 34 (29 Bearer + 1 cookie-ou-Bearer + 4 webhook) + executor 1 + researcher 2 = **37**.

Pedido **sem credencial**, vindo de IP de container, contra as 37 rotas do inbox (`auth_testes.py`):

| Resultado | Rotas |
|---|---|
| 401 (recusada) | 27 |
| 422 (corpo ou parâmetro inválido, recusado antes de chegar na checagem do token; nada executa) | 7 |
| 403 (`/agenda/alfred.ics` fora de loopback) | 1 |
| **200 sem credencial** | **2: `GET /vivo` e `GET /health`** |

Executor: `POST /execute` sem token, com token errado e com servidor sem token configurado dá 401 nos três casos
(falha fechada). Bearer vazio no inbox com `TOKEN=""` também dá 401.

Painel (testes existentes, 17 casos, todos OK): cliente fora de `127.0.0.1`/`::1` ou `Host` estranho → 403 em toda
rota `/painel/*`; cookie adulterado, expirado, revogado ou não ASCII não derruba nem entra; POST sem CSRF ou com
`Origin` estranho não muda nada; o token não aparece em nenhuma resposta nem cabeçalho.

Três lacunas, todas lidas no código e confirmadas em processo:

1. **`GET /health` sem credencial devolve** (só os nomes dos campos): `vault`, `vault_noticias`, `thunderbird_profile`,
   `inbox_default`, `model_chat`, `pendencias_aguardando`, `whatsapp.{url, instancia, numero_pedro, configurado,
   simulado, dry_run}` e `db.{db, notas, wa_msgs, cache_*}`. Ou seja, caminhos do disco, o **número de WhatsApp do
   dono** e contagens, para qualquer um que alcance a 7073 (`/vivo` basta para o vigia; ninguém automatizado usa o
   `/health` do inbox). O `/health` do researcher devolve o caminho do cofre.
2. **`GET /agenda/alfred.ics` só olha `request.client.host`**: com cliente `127.0.0.1` responde 200 mesmo com
   `Host: evil.example` e com `X-Forwarded-For`. O Painel, no mesmo cenário (`Host` estranho), responde 403. É o buraco
   de DNS rebinding e de proxy local que o `_is_trusted_loopback` do Odysseus fecha.
3. **A validação de corpo roda antes do token** em 7 rotas (422 em vez de 401). Não executa nada, só revela que a rota existe.

### 4.2 Força bruta e limite de tentativas

Não medi tentativas por segundo (fora do escopo desta rodada). O que o código e a aritmética dizem:

| Ponto | Hoje |
|---|---|
| Login do Painel (`painel.py`, `POST /painel/login`) | sem contador nem bloqueio. Cada erro espera `ATRASO_LOGIN_FALHO = 0.5 s` com `asyncio.sleep`, que não segura a próxima conexão |
| `check_auth` do inbox, executor e researcher | sem atraso, sem contador, sem log de falha |
| Token mestre | `openssl rand -hex 32` em `instalar.sh` = **256 bits** |
| Tempo esperado para adivinhar | 2^256 ≈ 1,2·10^77 tentativas; a 10^9 por segundo (absurdamente generoso) ≈ 3,7·10^60 anos |
| Comparação | Painel usa `hmac.compare_digest`; inbox, executor e researcher usam `!=` em 3 pontos. Timing **não medido** (fora do escopo); com 256 bits aleatórios e jitter de rede é higiene, não risco |

Conclusão: o limite de tentativas do Odysseus (15 por 60 s por IP, `src/rate_limiter.py`) existe porque senha humana
é adivinhável. No Alfred quem protege é a entropia do token. O que um limitador entregaria é **detecção** (um 429 e
uma linha de log quando um container sondar a API). No Painel a chave por IP não serve (o cliente é sempre
`127.0.0.1`); teria de ser contador global.

### 4.3 Quem usa o token mestre e o que cada um alcançaria se vazasse

Cópias do segredo **fora do `.env`**: 2 extensões do Thunderbird (`storage.local` do perfil, preenchido em
`options.html`) + 3 fluxos N8N (nó `Config`, texto puro no JSON do fluxo) + 1 valve do Open WebUI (em remoção) = **6**
(7 com a extensão do blog, que só existe no checkout `laya`, não nesta árvore). Trocar o token significa mexer em 6 a 7
lugares e derruba todas as sessões do Painel.

| Cliente | Rotas que usa | Fração das 37 | O que o token vazado dele alcança |
|---|---|---|---|
| Extensão Thunderbird (limpeza) | `/limpeza/fila`, `/limpeza/{id}/resultado` | 2/37 (5%) | as 37 |
| Extensão Thunderbird (agenda) | `/agenda/fila`, `/agenda/{id}/resultado` | 2/37 (5%) | as 37 |
| N8N (3 fluxos) | `/ciclo`, `/pendencias/{id}/links`, `/pendencias/{id}/responder`, `/whatsapp/webhook`, `/pendencias/{id}/audio` | 5/37 (14%) | as 37 |
| Ferramentas do chat (`lib_ferramentas.py`, lê do ambiente) | `/execute`, `/research`, `/research/{id}` | 3/37 (8%) | as 37 |
| Tool do Open WebUI (`alfred_pendencias.py`) | `/pendencias*` | — | as 37 |
| Painel (humano) | 22 rotas de tela | — | as 37 |
| **Evolution** | só o webhook, com `EVOLUTION_API_KEY` | 4/37 | **só o webhook** (já é um token com escopo; medido: com essa chave as outras rotas dão 401) |

O que as 37 rotas permitem (por leitura de código, **não exercitado**): ler o histórico do WhatsApp e as notas dos
cofres (`/whatsapp/buscar`, `/busca`, `/pendencias`); **enviar WhatsApp em nome de Pedro**
(`POST /pendencias/{id}/responder-texto` com `enviar: true`); aprovar lotes de limpeza de e-mail que a extensão
executa (`/limpeza/{id}/aprovar`); anotar na agenda; disparar `/ciclo`, `/semanal`, `/indexar` e `/research` (CPU e GPU);
e rodar a allowlist do executor (com as brechas semânticas já medidas em `shell.md`). Uma extensão que só lê a fila de
agenda, hoje, carrega a chave de tudo isso.

O token mestre também vale no webhook (`check_webhook_auth` aceita `EVOLUTION_API_KEY` **ou** o token mestre).

Como o token mais provavelmente vazaria: o `.env` está com modo **664** (`stat -c %a`: legível por qualquer usuário
local; só há 1 conta de login, mas o modo é frouxo), o perfil do Thunderbird, o JSON do fluxo N8N. Adivinhar não é o caminho.

### 4.4 Custo de verificar token

| Verificação | Custo medido (CPU local) |
|---|---|
| bcrypt custo 12 (como o Odysseus guarda cada token de API; `checkpw` em toda requisição Bearer) | **212 ms** por verificação |
| SHA-256 + `compare_digest` (o que serve para token aleatório de 256 bits) | **0,61 µs** |

Diferença de ~350 000 vezes. Copiar o bcrypt do Odysseus para a API do Alfred somaria ~0,2 s de CPU a cada chamada
(a extensão de agenda consulta a fila a cada 2 minutos por padrão, `intervaloMin: 2`; o custo seria pequeno ali,
mas cada chamada de N8N ou de ferramenta pagaria o mesmo).

### 4.5 Odysseus × Alfred, ponto a ponto

| Ideia do Odysseus | Alfred hoje | Vale para um usuário, local, ufw? |
|---|---|---|
| Contas com senha bcrypt, privilégios por usuário | um segredo, um dono | não: não há segundo usuário |
| TOTP (`pyotp`, códigos de recuperação) | nenhum | quase não: ver 5.2 |
| Limite de login (15/60 s por IP) | atraso de 0,5 s no Painel, nada na API | pouco: a entropia já cobre; vale como detecção |
| Token de API com escopo (14 escopos, perfis) | um token para tudo | **sim, em versão enxuta** (4.3) |
| Bypass de localhost desligado por padrão | não há bypass; o Painel só aceita loopback + `Host` local | já é igual ou mais estrito |
| Loopback só se direto, sem cabeçalho de proxy | o Painel confere `Host`; o `.ics` não | sim, 5 linhas no `.ics` |
| Token interno por processo para ferramentas | ferramentas usam o token mestre do ambiente | sim, é o mesmo caso do escopo |
| Sessão guardada no servidor, revogação persistente | cookie sem estado; revogados ficam em memória e somem ao reiniciar | marginal |
| Códigos de recuperação em texto puro (hex de 4 bytes) | — | não copiar (guardar hash, se um dia houver TOTP) |

### 4.6 Suíte

- Testes de auth do Painel: **17 de 17 OK** (3,7 s).
- Contrato `auth_testes.py`: **7 de 7 OK**.
- Suíte completa do inbox (`unittest discover -s tests -t .`): **464 testes, OK**, 29,6 s.
- Não rodei o researcher em `TestClient` (importa ChromaDB e sklearn; swap cheio). As rotas dele entram só no
  inventário estático.

## 5. Pontuação

### 5.1 Por ideia

| # | Ideia | Vem do Odysseus? | Ganho 0–5 | Custo (h) | Risco 0–5 | Arquivos |
|---|---|---|---|---|---|---|
| 1 | `chmod 600 .env` e ufw só 7073 na `br-alfred` (regra do README hoje libera 7071:7073) | não | 3 | 0,2 | 0 | nenhum (Pedro executa) |
| 2 | `hmac.compare_digest` nos 3 `check_auth` | parecido | 1 | 0,5 | 0 | 3 `main.py` + teste |
| 3 | `/health` do inbox só com `status` (ou exigir Bearer) | não | 2 | 0,5 | 1 | `main.py`, `about.md`, teste |
| 4 | `.ics` recusa `Host` não local e cabeçalho de proxy | sim (`_is_trusted_loopback`) | 1 | 0,5 | 0 | `main.py` + teste |
| 5 | Limite de falhas: contador global no Painel, por IP na API | sim | 1 | 3–4 | 1 | `_shared/lib_auth.py` novo, `painel.py`, 3 `main.py` |
| 6 | Token com escopo **enxuto**: segredos em env por cliente (`agenda`, `limpeza`, `n8n`) + mestre | sim, simplificado | 3 | 5–7 | 3 | `lib_auth.py`, 3 `main.py` (≈36 chamadas de `check_auth`), `.env.example`, `instalar.sh`, docs, 3 JSON N8N |
| 7 | Token em banco com hash bcrypt, criar/listar/revogar no Painel | sim, como é | 1 | 12–16 | 3 | + `painel.py`, templates |
| 8 | TOTP no Painel | sim | 0,5 | 5–7 | 2 | `painel.py` (+≈150 linhas), 2 templates, `requirements.txt`, testes |
| 9 | Contas e privilégios por usuário | sim | 0 | 15+ | 3 | — |
| 10 | Revogação de sessão persistente | sim | 1 | 0,5–1 | 1 | `painel.py` |

### 5.2 Por que o TOTP rende tão pouco aqui

1. **Não protege a API.** O token mestre abre as 37 rotas sem passar pelo Painel; o TOTP só atrapalharia quem entra
   pela tela de login.
2. O segredo TOTP ficaria no mesmo disco e no mesmo usuário do `.env`. Quem lê um lê o outro.
3. O Painel só aceita `127.0.0.1` com `Host` local. O ataque que o TOTP corta (senha roubada usada de fora) não
   chega até lá. Passaria a valer se Pedro expuser o Painel pela `tailscale0` ou na internet; esse é o gatilho para voltar ao assunto.

### 5.3 Resumo

- **Ganho para Pedro: 2/5.** O que paga é o escopo por cliente e a limpeza da superfície (itens 1 a 4 e 6). TOTP, contas e
  token em banco (7 a 9) somam quase nada para um usuário só.
- **Custo de implementar:** pacote adaptado (itens 1 a 6) ≈ **10–13 h**, em `lib_auth.py` (novo), os 3 `main.py`,
  `painel.py`, `.env.example`, `instalar.sh` e docs. Se incluir TOTP e token em banco no estilo Odysseus: **≈ 25–30 h**.
- **Risco: 2/5.** O item 6 é o que arrisca (escopo errado derruba extensão ou N8N em silêncio); mitigação: manter o
  mestre aceito durante a migração e migrar um cliente por vez. O TOTP arrisca travar o próprio Painel (celular,
  relógio).
- **Custo de recursos na máquina:** VRAM 0. CPU: 0,61 µs por verificação com SHA-256; **212 ms** se copiar o bcrypt do
  Odysseus (não copiar). Latência extra < 1 ms. Memória: um dicionário de falhas por IP (KB). Nenhum processo novo.
  `pyotp` só se houver TOTP (Python puro, sem serviço).
- **Nota final: 4/10.** As ideias do Odysseus que servem ao Alfred são o token com escopo e o limite de falhas (e só em
  versão enxuta); o resto de `core/auth.py` resolve um problema multiusuário que o Alfred não tem, e o que mais
  reduz risco hoje (modo do `.env`, regra do ufw, `/health`, `.ics`) nem é do Odysseus.

## 6. Veredito

**Adaptar. Não portar TOTP, contas nem token em banco.**

O que adaptar, por ordem de valor sobre custo:

1. **Hoje, sem código (Pedro executa):** `chmod 600 .env`; trocar a regra do ufw para `7073` só em `br-alfred`
   (7071 e 7072 não têm mais cliente em container: o Open WebUI não roda). Conferir com `sudo ufw status verbose`,
   que não consegui ler.
2. **≈ 1,5 h:** `compare_digest` nos 3 `check_auth`, `/health` do inbox enxuto, `.ics` com a checagem de `Host` e de cabeçalho de proxy.
3. **≈ 5–7 h:** token com escopo enxuto em variáveis de ambiente (`agenda`, `limpeza`, `n8n`, mais o mestre), um por
   cliente, com SHA-256 e `compare_digest`; começar pelas duas extensões do Thunderbird, que usam 2 rotas cada e hoje
   carregam a chave das 37.
4. **≈ 3–4 h:** limitador de falhas com log (contador global no Painel, por IP na API). Entra depois do 3, para o 429 e o log mostrarem qual cliente errou.

Revisitar o TOTP só se o Painel for exposto fora de `127.0.0.1`.

**Menor próximo passo:** o item 2, um commit com `compare_digest`, `/health` enxuto e `.ics` endurecido, estendendo
`auth_testes.py` para `services/inbox/tests/test_auth.py`.

## 7. O que ficou sem medir

- **Tentativas de login por segundo sem limite** (pedido original; fora do escopo desta rodada). Só código e aritmética.
- **Timing da comparação `!=`.** Idem.
- **Alcance real a partir da LAN, da `tailscale0` e das bridges de outros projetos.** Nada foi sondado, e as regras do
  ufw estão em arquivo só do root. Conclusão depende de `sudo ufw status verbose`.
- **N8N** (container desligado) e a **extensão do blog** (fora desta árvore).
- **Researcher em `TestClient`** (só inventário estático).
- **Odysseus não foi executado**: TOTP, limitador e escopos foram lidos, não testados. O 212 ms do bcrypt é o
  `checkpw` isolado nesta máquina, não uma requisição ao Odysseus.
- **Custos de implementar** são estimativas por contagem de pontos de chamada; nada foi prototipado.
- Nenhum conteúdo de `.env`, caixa de e-mail, WhatsApp ou cofre foi lido.
