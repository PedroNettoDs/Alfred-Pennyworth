# Avaliação — Instalação (Odysseus × Alfred)

Medições em 03/11/2026 e 05/11/2026 (a máquina reiniciou no meio; ver §3). Odysseus lido no commit
`c21980f` (clone raso, AGPL-3.0, nada copiado). Alfred medido no commit `4c30b84` (ramo `laya`).
Scripts em [`docs/avaliacoes/scripts/instalacao/`](scripts/instalacao/), números brutos em
[`resultados/`](scripts/instalacao/resultados/).

## 1. O que o Odysseus faz

- **Imagem única da aplicação.** `Dockerfile` (raiz) monta uma imagem `python:3.14.9-slim` em três
  estágios: base com `libbrotli` corrigida, um estágio que compila rodas corrigidas do Real-ESRGAN e o
  estágio final com apt (Chromium, Node, git, cmake, nmap, tmux…), cliente Docker estático de versão
  fixa, `pip install -r requirements.txt` e o código. `docker/entrypoint.sh` troca para o usuário
  `PUID/PGID` (padrão 1000) com `gosu` e corrige o dono dos volumes montados antes de subir o uvicorn.
- **Publicação multi-arch.** `.github/workflows/docker-publish.yml` constrói amd64 e arm64 em runners
  nativos, publica por digest e junta os dois num manifest list no GHCR. Tags: `:latest` e `:X.Y.Z`
  (móveis, a cada push em `main`), `:X.Y.Z-<sha>` (imutável, a recomendada para produção), `:dev` e
  `:X.Y.Z-dev.<sha>`. As actions do workflow estão fixadas por SHA.
- **Compose.** `docker-compose.yml` usa `${ODYSSEUS_IMAGE:-ghcr.io/odysseus-dev/odysseus:latest}` com
  `build: .` de reserva (só constrói se o pull falhar). Os acompanhantes (ChromaDB, SearXNG, ntfy) vêm
  com **tag de versão + digest** (`chroma:1.5.9@sha256:1e0b73a1…`), e um teste
  (`tests/test_compose_sidecar_pins.py`) reprova `:latest` ou referência sem digest. Variantes
  `docker-compose.gpu-nvidia.yml` e `docker-compose.gpu-amd.yml`. O Ollama fica no host, acessado por
  `host.docker.internal`.
- **Dependências Python soltas de propósito.** `requirements.txt` não fixa versões (38 linhas, 8 com
  piso, nenhuma exata); só o bootstrap (`requirements-bootstrap.txt`: pip e certifi) vai por URL +
  hash. O `website/setup.md` diz que duas instalações nativas em datas diferentes dão versões
  diferentes e sugere `uv pip compile` para um lock local. Ou seja: a reprodutibilidade do Odysseus
  é a **da imagem publicada** (congelada no build da CI), não a do build.
- **Instalação nativa** existe como alternativa: `install-service.sh` copia `odysseus-ui.service`
  para `/etc/systemd/system` com `sudo`, depois de o usuário editar caminhos à mão.
- **Atualização/rollback:** `docker compose pull && docker compose up -d`; rollback = apontar
  `ODYSSEUS_IMAGE` para a tag `X.Y.Z-<sha>` anterior. Mudança de volume tem guia manual próprio
  (`docs/chromadb-volume-migration.md`, com seção de rollback).

## 2. O que o Alfred faz hoje

- `services/scripts/instalar.sh` (441 linhas, sem sudo, idempotente): confere pré-requisitos e
  imprime o comando Arch/Ubuntu de cada falta; cria o `.env` a partir do `.env.example` com segredos
  do `openssl` (nunca sobrescreve um `.env` existente); cria pastas e o `config/searxng/settings.yml`;
  um venv por serviço (`shell-executor`, `researcher`, `inbox`, `briefing`, `laya`) com
  `pip install -r requirements.txt`; grava as units de usuário a partir dos modelos
  `services/*/alfred-*.{service,timer}` (troca `@ALFRED_DIR@`), `enable --now` e `try-restart` só
  das units cujo arquivo mudou; `docker compose up -d` de `open-webui searxng chromadb
  evolution-postgres evolution-api`; com `--modelos`, `ollama pull` dos modelos do `.env`.
- Processos: 4 serviços HTTP como units de usuário (`alfred-executor` 7071, `alfred-researcher`
  7072, `alfred-inbox-api` 7073, `alfred-laya` 7074) e 7 timers (inbox, semanal, vigia, briefing
  matinal/vespertino, backup, rag). Cada `run.sh` lê o `.env` literalmente e cria o venv se faltar.
- `docker-compose.yml`: só os acompanhantes (Open WebUI, SearXNG, ChromaDB, N8N, Evolution + Postgres)
  na rede `br-alfred`, portas em `DOCKER_BIND_IP`.
- Integração com o desktop que um container não tem de graça: perfil do Thunderbird (8 arquivos),
  notificações D-Bus/`notify-send` (7), `kdialog/zenity` (4), player de áudio (3), `systemctl` (6),
  cofres Obsidian (20 arquivos usam `VAULT_*`) e o `shell-executor`, cuja função é executar comandos
  **no host**.

## 3. Método

Tudo numa **bancada descartável**, nunca no sistema do Pedro: container Ubuntu 24.04 com systemd
como PID 1 e um usuário comum com sudo e linger
([`testbed.Dockerfile`](scripts/instalacao/testbed.Dockerfile)). Aproxima um Ubuntu desktop recém-
instalado: tem `python3` 3.12.3, `openssl`, `wget`, `ss` e `notify-send`; não tem git, curl,
python3-venv, Docker, ffmpeg nem Ollama. Roda `--privileged` só para o Docker do Alfred subir
**dentro** dela; nenhuma porta é publicada no host e nada do Pedro é montado (nem `.env`, nem cofres,
nem units). O repositório entra por `git bundle` do HEAD. A GPU/Ollama fica fora: o `instalar.sh`
acusa "ollama" pendente e a medição segue. Nenhuma medição usou Ollama ou GPU, por isso o
`flock /tmp/alfred-bench-gpu.lock` não foi necessário.

| O que | Script | Como |
|---|---|---|
| Do zero | [`medir_do_zero.sh`](scripts/instalacao/medir_do_zero.sh) | segue só o que o `instalar.sh` manda: `--verificar`, instala os pacotes apt das linhas ✖, grupo docker + nova sessão, `instalar.sh`; espera a saúde dos 7 serviços; inventário; reinstala; reinicia a bancada |
| Reprodutibilidade | [`auditar_pins.py`](scripts/instalacao/auditar_pins.py) | classifica cada linha de requirements, imagem de compose e modelo Ollama, no Alfred e no Odysseus; digests do host × registro (`docker buildx imagetools inspect`, só manifesto); `pip list` da produção (só leitura) × venvs recém-criados na bancada |
| Atualização e rollback | [`medir_atualizacao.sh`](scripts/instalacao/medir_atualizacao.sh) | instala `8e71429` (27/10, sem Laya, portas antigas), `git pull` até `4c30b84` (107 arquivos), `instalar.sh`; depois `git checkout 8e71429` + `instalar.sh` + reboot. Registra PIDs, chaves do `.env`, versões e units |
| Imagem única | [`imagem_unica.Dockerfile`](scripts/instalacao/imagem_unica.Dockerfile), [`medir_imagem_unica.sh`](scripts/instalacao/medir_imagem_unica.sh) | build `--no-cache` dentro de uma bancada; variante enxuta (1 venv, torch CPU) medida; a fiel (5 venvs, torch CUDA) estimada |

Reproduzir (da raiz do repositório; cada um leva de 5 a 30 min e baixa de 1 a 10 GB):

```bash
python3 -I docs/avaliacoes/scripts/instalacao/auditar_pins.py .
```

```bash
docs/avaliacoes/scripts/instalacao/medir_do_zero.sh "$PWD" /tmp/aval-zero
```

```bash
docs/avaliacoes/scripts/instalacao/medir_atualizacao.sh "$PWD" /tmp/aval-atualizacao
```

```bash
docs/avaliacoes/scripts/instalacao/medir_imagem_unica.sh "$PWD" /tmp/aval-imagem
```

**Ruído e incidentes.** As 8 avaliações rodaram juntas em 03/11 (load 8,6–10,3 em 12 threads), o que
infla os tempos de pip e pull. A máquina reiniciou em 04/11 e levou os logs do scratchpad: os números
do "do zero" foram copiados na hora para [`resultados/do_zero_notas.md`](scripts/instalacao/resultados/do_zero_notas.md);
o build "fiel" da imagem única e o primeiro teste de atualização morreram e não foram refeitos do mesmo
jeito. Em 05/11 a memória estava apertada (swap 8,2 de 8 GB), por isso só a variante enxuta foi
construída, a atualização rodou com `--sem-containers` e cada medição rodou sozinha. Na bancada, o
Docker aninhado com o snapshotter do containerd não extrai a imagem do Open WebUI (`failed to convert
whiteout file … operation not permitted`); com overlay2 extrai. É artefato da bancada, não do Alfred,
e a bancada já sobe com overlay2.

## 4. Resultados

### 4.1 Do zero (Ubuntu 24.04 limpo)

| Passo | Tempo | Intervenção manual |
|---|---|---|
| instalar o git | 13,6 s | sim (`sudo apt`) |
| `instalar.sh --verificar` | 0,2 s | — · 6 faltas (python3-venv, curl, docker, compose, ollama, ffmpeg), 4 avisos |
| `sudo apt install python3 python3-venv curl docker.io docker-compose-v2 ffmpeg` | 60,4 s | sim (comando impresso pelo script) |
| `usermod -aG docker` + sair e entrar | 0,6 s + nova sessão | sim (o script avisa) |
| `instalar.sh` | **660,7 s** | não |
| ↳ venvs (shell-executor 9, researcher 8, inbox 30, briefing 23, **laya 369**) | 443 s | |
| ↳ units (18 gravadas; 4 serviços + 7 timers ligados) | 2 s | |
| ↳ `docker compose up` (4 imagens ~190 s; Open WebUI 193 s à parte) | 215 s | |
| 4 serviços Python respondendo | já de pé na 1ª conferência, 215 s depois do `enable --now` (o Laya leva 75–79 s na 1ª partida, para baixar os pesos) | |
| **Até os 4 serviços Python de pé** | **≈ 10 min** (522 s até as units ligarem + ~80 s do Laya) | 3 passos com sudo |
| **Até tudo de pé, com containers** | **≈ 12–16 min** (736 s + Open WebUI se baixado à parte) | |
| reinstalar sem mudanças (idempotência) | 6,7 s, nada alterado | |
| reiniciar a bancada | os 7 serviços voltam sozinhos em ≤ 13 s | |
| testes do inbox no venv novo | 464 testes, 3 falhas: 2 por fuso (bancada em UTC; passam com `TZ=America/Sao_Paulo`), 1 de tempo sob carga | |

Fora da medição e ainda manuais: Ollama (`curl | sh`, drop-in `OLLAMA_HOST=0.0.0.0`, `--modelos`:
`gemma4:12b` 8,0 GB + `bge-m3` 1,2 GB), trocar `MODEL_CHAT` (o `instalar.sh` grava `gemma4:e4b`, a
produção usa `gemma4:12b`), ufw para `br-alfred`, Thunderbird + extensões `.xpi` não assinadas,
pareamento do WhatsApp por QR. Nenhum deles some com uma imagem Docker.

Onde quebra ou engana:
- o `instalar.sh` imprimiu `docker compose up falhou: ` **sem motivo**: o filtro `grep -iE 'error|erro'`
  não casa com `failed to extract layer`;
- o fuso não é conferido: numa máquina em UTC (ou num container sem `TZ`) o Painel mostra os horários
  deslocados 3 h (15:00 vira 12:00; 2 testes do `test_painel` falham);
- o `pip` deixa **2,97 GB** em `~/.cache/pip` e o venv do Laya traz torch com CUDA (**5,2 GB** de
  torch + nvidia + triton) embora o Laya rode na CPU por padrão;
- o `instalar.sh` sobe o Open WebUI (4,58 GB, 193 s de pull), que a produção já não roda
  (`docs/planos/remover-openwebui.md`).

Disco da instalação atual (bancada): venvs 6,40 GB (Laya 5,65 GB = 88 %) + cache do pip 2,97 GB +
pesos do Laya 0,65 GB + imagens 8,04 GB (3,46 GB sem o Open WebUI).

### 4.2 Reprodutibilidade

| Item | Alfred | Odysseus |
|---|---|---|
| linhas de requirements | 22: **1 exata** (`laya[serve]==0.4.19`), 21 soltas | `requirements.txt`: 38, 0 exatas, 8 com piso; bootstrap com hash |
| dependências transitivas | 96 pacotes / 180 pares serviço-pacote, sem lock | sem lock; a imagem publicada congela |
| imagens do compose | 6: 1 digest (Evolution), 1 tag de major (`postgres:16-alpine`), **4 móveis** (`open-webui:main`, `searxng:latest`, `chroma:latest`, `n8n:latest`) | acompanhantes 3/3 com tag + digest e teste que reprova `:latest`; a da app é `:latest` por padrão e `:X.Y.Z-<sha>` recomendada |
| deriva medida nas imagens | `searxng:latest` e `postgres:16-alpine`: o host roda outro digest do que uma instalação nova baixa; `open-webui:main` mudou de digest entre 03/11 e 05/11 | — |
| modelos Ollama | 6 modelos em 7 grafias no código/config, **0 com digest** (`ollama pull` não aceita digest); `.env.example` (`llama3.1:8b`), `instalar.sh` (`gemma4:e4b`) e produção (`gemma4:12b`) discordam | fora do escopo (o Cookbook escolhe) |
| pesos do Laya | fixados por SHA dentro do pacote (`laya/revisions.py`) | — |
| Python | `≥ 3.12`, sem teto: Ubuntu 3.12.3, produção 3.14.4 | 3.14.9 fixo na imagem |
| produção × instalação nova de hoje | **20 de 180 pares** com versão diferente (13 pacotes, sem contar o pip): `python-dateutil` 2.9 → **4.0.0** (major; a suíte passou com ela), `fastapi` 0.147 → 0.162, `starlette` 1.9 → 1.10 | — |

### 4.3 Atualização e rollback (`8e71429` → `4c30b84` → `8e71429`)

Rodado com `--sem-containers` (as imagens têm as mesmas tags nas duas versões). 107 arquivos mudam
entre as duas: entram o Laya, as portas +1 e 3 chaves novas no `.env.example`.

| Etapa | Tempo | O que aconteceu |
|---|---|---|
| instalar a versão antiga | 65,1 s | 4 venvs, executor/researcher/inbox de pé em ≤ 4 s |
| `git pull` | 0,2 s | os serviços continuam com o **código antigo** (mesmos PIDs) até reinstalar ou reiniciar |
| `instalar.sh` depois do pull | **363,3 s** | venv do Laya criado (torch CUDA); executor/researcher/inbox reiniciados (PIDs novos) porque **os arquivos das units mudaram**; Laya ligado e de pé 79 s depois (pesos) |
| `.env` depois do pull | — | não recebe as 3 chaves novas (`ALFRED_BACKUP_DIR`, `OLLAMA_EMBED_NUM_GPU`, `PORT_LAYA`; os padrões do código cobrem) e continua com as portas antigas (7070–7072, 8080…), enquanto README e avisos do próprio `instalar.sh` falam em 7071–7073 |
| versões dos pacotes | — | 0 pacotes atualizados nos 4 venvs que já existiam; só os 66 novos do Laya e 2 novos no researcher |
| `git checkout 8e71429` + `instalar.sh` (rollback) | 0,2 s + 4,3 s | executor/researcher/inbox reiniciados; **nenhum pacote volta** (0 removidos, 0 rebaixados); `services/laya/` fica como pasta ignorada com o venv de 5,66 GB; o processo do Laya iniciado pela versão nova segue rodando até o reboot |
| reboot depois do rollback | executor/researcher/inbox em ≤ 4 s | `alfred-laya.service` em **loop de reinício** (`activating auto-restart`): a unit da versão nova continua instalada e aponta para um `run.sh` que não existe mais |

Pela leitura do `instalar.sh` (linhas 371–374), o reinício só acontece para unit cujo **arquivo**
mudou: um `git pull` que mexe só em Python deixa os serviços com o código velho até alguém rodar
`systemctl --user restart`. Desta vez reiniciou porque as units mudaram junto.

### 4.4 Imagem única hipotética

| | Instalação atual (venvs) | Imagem fiel (estimada) | Imagem enxuta (medida) | Odysseus `:latest` amd64 |
|---|---|---|---|---|
| conteúdo | 5 venvs, torch CUDA | 5 venvs, torch CUDA | 1 venv, torch 2.15.0+cpu | app inteira (Chromium, Node…) |
| disco | 6,40 GB + 2,97 GB de cache do pip | ≈ 7,0 GB | **2,01 GB** (venv 1,44 GB, apt 452 MB, base 120 MB, código 3,7 MB) | — |
| transferência | ≈ 3 GB de wheels (proxy: cache do pip) | — | 0,71 GB (`docker save \| gzip -1`) | 0,97 GB comprimido, 25 camadas |
| tempo | pip 443 s (Laya 369 s) | ≥ 443 s | build **166 s** sem cache + 7,7 s de base | só pull |
| partida a frio | units de pé em segundos; Laya 75–79 s na 1ª (pesos) | — | 2–3 s; Laya 75 s na 1ª (pesos, 648 MB) | — |
| memória dos 4 serviços | — | — | 2,0 GiB | — |

A fiel foi estimada assim: torch CUDA no Laya (5,2 GB) menos o torch CPU (0,82 GB) = +4,4 GB, mais
~0,6 GB de pacotes duplicados nos 5 venvs. Quase toda a diferença entre a instalação atual e a
enxuta vem do **torch CPU** e do **cache do pip**, que não precisam de container.

O que uma imagem única do Alfred precisaria além do build: agendador para os 7 timers; montar o perfil
do Thunderbird, o socket D-Bus da sessão, o áudio e os cofres; `TZ`; repensar o `shell-executor` (dentro
do container ele executa no container, não no host); refazer o modo jogo (`alfred-switch.sh` para
units, não containers). O Ollama continuaria no host, como no Odysseus.

## 5. Pontuação

| Critério | Valor |
|---|---|
| **Ganho para Pedro** | **1/5** para a imagem única: um PC amd64 (o multi-arch não serve), instalação rara, ≈ 10 min automáticos até os serviços de pé, e os passos manuais que sobram (Ollama, grupo docker, Thunderbird, WhatsApp, modelos) continuam com imagem. **3/5** para a parte "tags fixas": hoje 4 de 6 imagens e 21 de 22 dependências andam sozinhas, já há deriva medida entre a produção e uma instalação nova, e o rollback por `git checkout` não volta nenhum pacote e deixou o Laya em loop de reinício |
| **Custo de implementar** | Imagem única completa: **~24 h** em ~30 arquivos (Dockerfile e entrypoint novos, `docker-compose.yml`, `instalar.sh`, `alfred-switch.sh`, 18 modelos de unit trocados por agendador, 5 `run.sh`, `lib_mail.py`/notificações para caminhos montados, README). Adaptação do §6: **~7 h** em ~13 arquivos (`docker-compose.yml`, 5 `requirements.txt` + 5 locks novos, `instalar.sh`, um teste novo em `services/inbox/tests/`) |
| **Risco** | Imagem única **4/5**: quebra a integração com o desktop (notificações, Thunderbird, áudio, modo jogo) e muda o sentido do `shell-executor`. Adaptação **1/5**: um lock velho pode segurar uma correção de segurança, então precisa de rotina de atualização |
| **Custo de recursos** | VRAM 0 nos dois casos (Ollama fica no host). Imagem enxuta: 2,0 GB de disco contra 9,4 GB de venvs + cache; memória igual (os mesmos 4 processos, 2,0 GiB); latência igual; build 166 s a cada versão. Adaptação: −4,4 GB e ~−5 min de instalação com torch CPU, −3 GB sem cache do pip; zero em tempo de execução |
| **Nota final** | **3/10**. A imagem única resolve um problema que o Pedro não tem (instalar em várias máquinas) e cria outros (desktop, shell no host); o que vale do Odysseus é fixar versões, e isso cabe no `instalar.sh` de hoje |

## 6. Veredito: adaptar (só as fixações)

Não aplicar a imagem única. Adaptar:

1. **Imagens por tag + digest** no `docker-compose.yml` (SearXNG, ChromaDB 1.5.9, Postgres, e Open
   WebUI/N8N enquanto existirem) e um teste `unittest` que reprova `:latest`, `:main` ou falta de
   digest, na linha do `tests/test_compose_sidecar_pins.py` do Odysseus.
2. **Lock por serviço**: `requirements.txt` continua com os nomes; um `requirements.lock` gerado
   (`pip freeze` do venv validado ou `uv pip compile`) entra como `-c` no `pip install` do
   `instalar.sh`. Atualizar vira um passo explícito: gerar o lock, rodar os testes, fazer commit. O
   rollback por `git checkout` passa a voltar também as versões.
3. **Laya com torch CPU** (`--extra-index-url https://download.pytorch.org/whl/cpu` e `torch==…+cpu`
   no `services/laya/requirements.txt`): −4,4 GB e ~−5 min, depois de conferir que as respostas e a
   latência do Laya na CPU não mudam.
4. **`instalar.sh`**: mostrar a última linha real do erro do compose, `--no-cache-dir` no pip
   (−3 GB), aviso de fuso diferente de `America/Sao_Paulo`, reiniciar os serviços quando o commit
   mudou desde a última instalação (não só quando a unit mudou), desabilitar units instaladas que não
   têm mais modelo no repositório, avisar as chaves do `.env.example` que faltam no `.env`, e no
   `--verificar` comparar o digest dos modelos do `.env` (`/api/tags`) com os esperados.

**Menor próximo passo (~1 h, 2 arquivos):** fixar por tag + digest as 4 imagens móveis do
`docker-compose.yml` (SearXNG e ChromaDB com os digests que a produção roda hoje, `f4aa660c…` e
`1e0b73a1…` = 1.5.9) e adicionar o teste que reprova referência móvel.
