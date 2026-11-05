# Avaliação: Shell (Odysseus `ShellService` × Alfred `shell-executor`)

Data: 03 e 04/11/2026. Odysseus no commit `c21980f` (clone raso, só leitura; nenhum trecho copiado). Alfred no
commit `4c30b84` (branch `laya`). Scripts em [`scripts/`](scripts/): `shell_instancia_teste.sh`, `shell_medir.py`,
`shell_odysseus.py` e `shell_fuga.py`.

## 1. O que o Odysseus faz

O Odysseus tem dois caminhos para rodar comandos, com garantias bem diferentes.

**`services/shell/service.py` — `ShellService`** (o que foi pedido para comparar). O mesmo padrão aparece em
`routes/shell_routes.py:_exec_shell`, atrás da rota `POST /api/shell/exec`.

- Roda o texto inteiro em `/bin/sh -c` (`asyncio.create_subprocess_shell`). Não há allowlist nem filtro: quem
  pode chamar roda qualquer coisa. O controle é de autorização: a rota só aceita administrador
  (`_require_admin`) e recusa chamadas internas de ferramenta.
- O timeout é `asyncio.wait_for(proc.communicate(), timeout)`. O padrão é 30 s, e a rota usa `EXEC_TIMEOUT = 30`.
  Quando estoura, o serviço chama `proc.kill()`, que mata só o processo do `sh` (não o grupo), e depois espera
  com `await proc.wait()`.
- O limite de saída é `max_output = 200_000` caracteres. O corte acontece depois de `communicate()` ler toda a
  saída para a memória: ele limita a resposta, não a memória.
- O serviço é assíncrono: não há pool de threads e não há limite de pedidos simultâneos.
- Também há `stream()`, que devolve a saída linha a linha, com o mesmo `kill()` só no `sh`.

**`src/containment.py` — ferramenta `bash` do agente** (`src/agent_tools/subprocess_tools.py`). Este caminho é
bem mais forte, mas é outro mecanismo.

- Isola com bubblewrap: raiz em tmpfs, `/usr` e `/etc` só leitura, novo PID namespace, `--die-with-parent` e
  `--new-session`. A rede é herdada.
- Usa `start_new_session=True` e, no fim, manda SIGTERM e depois SIGKILL ao **grupo** de processos. Antes de
  declarar o processo morto, confere que ele morreu.
- Lê a saída em blocos de 64 KiB com orçamento de bytes (`_drain`). Passado o teto (`MAX_OUTPUT_CHARS = 10_000`),
  continua lendo e descartando, para o filho não travar com o pipe cheio. Assim a memória fica limitada.
- O relógio de parede é de 120 s (`DEFAULT_BASH_TIMEOUT`). Há `RLIMIT_AS` e `RLIMIT_NPROC` opcionais.
- O modo padrão é "enforcing": se o host não oferece o isolamento exigido, o comando não roda.

## 2. O que o Alfred faz hoje

`services/shell-executor/main.py` (serviço `alfred-executor`, porta 7071):

- **Política:** allowlist por prefixo, comparada palavra a palavra (`ALLOWED_PREFIXES`). Há regras extras para
  `docker`, `curl`, `find`, `journalctl`, `cat`, `grep` e `ollama`, e um filtro de caminho sensível
  (`_caminho_sensivel`).
- **Sem shell:** `_dividir_pipeline` recusa `; & < > ( ) $ \` { } \` e quebra de linha fora de aspas simples, e
  aceita só o pipe simples. Cada trecho vira um argv (`shlex`), e o executor monta o pipeline com `Popen`
  (`shell=False`).
- **Timeout:** de 1 a 1800 s (padrão 15). Todo o pipeline roda num grupo de processos próprio
  (`process_group=`). No timeout, o executor manda `os.killpg(SIGKILL)` e responde HTTP 408.
- **Ambiente dos filhos:** só as variáveis de `ENV_FILHOS`, mais `LC_ALL=C.UTF-8`. Os tokens do `.env` não
  passam para os comandos.
- **Saída:** stdout e stderr vão para um `tempfile.TemporaryFile()` (no `/tmp`, que nesta máquina é **tmpfs**,
  isto é, RAM). No fim, o arquivo é lido inteiro para a memória e vai no JSON. **Não há teto de saída.**
- **Concorrência:** o handler é `def` síncrono, então roda no pool de threads do Starlette/anyio (40 vagas). O
  `/health` também é `def`.
- **Quem chama:**
  - `services/_shared/lib_ferramentas.py:executar_comando` é marcada perigosa: a Conversa pede o clique
    "Executar?". O resultado é cortado em `MAX_SAIDA = 12000` caracteres (linha 52), mas só depois de receber e
    decodificar o JSON inteiro.
  - `tools/alfred_shell_executor.py` (Open WebUI) não corta nada.
- **Testes:** `services/inbox/tests/test_servicos.py` (allowlist).

## 3. Método

Não foi usado Ollama nem GPU, então `flock /tmp/alfred-bench-gpu.lock` não foi necessário. O ruído vem da CPU
compartilhada: outras 7 avaliações e o Alfred de produção rodavam ao mesmo tempo. A carga média (loadavg 1 min)
ficou entre 5,4 e 15,8 durante as medições, e cada saída dos scripts registra a carga antes e depois.

Ambiente: Python 3.14.4, fastapi 0.162.0, starlette 1.10.0, anyio 4.15.1, uvicorn 0.54.0, `/bin/sh` = dash,
RAM de 31 GiB, 12 CPUs.

**Instância de teste do Alfred:** o `main.py` desta branch, servido em `127.0.0.1:17071`. Ela roda com `env -i` e
um token de teste, sem o `.env`, e usa o venv do executor de produção só para leitura. Os comandos medidos são
inofensivos e da allowlist: `tail -n <marca> -f /dev/null` (nunca termina), `date`, e `head -c N` de um arquivo
`y\n` de 200 MB criado em disco (`~/.cache`, não no tmpfs). O executor de produção (PID da unit) não foi tocado.

**Odysseus:** `services/shell/service.py` carregado pelo caminho, num processo `python3 -I` com `env -i`,
`prlimit --as=3G` e `nice -n 10`, dentro de um diretório vazio do scratchpad. Rodou os mesmos tipos de comando:
`sleep`, `date`, `yes | head -c N`. Cada tamanho de saída rodou num processo novo, para o pico de RSS
(`ru_maxrss`) ser só daquele pedido. Não foi medido por HTTP: as latências dele não incluem rede, JSON nem
thread, e por isso não se comparam em valor absoluto com as do Alfred.

**Fuga da allowlist:** 45 tentativas em 9 categorias (`;`, `&&`/`||`/`&`, `$()`/crase, variáveis, quebra de
linha, redirecionamento, pipe para interpretador, caminhos relativos e absolutos, truques de aspas). O segundo
comando é sempre `id`. As tentativas foram avaliadas **só** com `is_allowed()`, a mesma análise que o
`POST /execute` faz antes de executar. Nada foi executado.

Para reproduzir:

```bash
# instância de teste (outro terminal)
TOKEN=teste VENV=<venv com fastapi+uvicorn> docs/avaliacoes/scripts/shell_instancia_teste.sh
```

```bash
mkdir -p ~/.cache/alfred-aval-shell && yes | head -c 200M > ~/.cache/alfred-aval-shell/yes200M.txt
```

```bash
TOKEN=teste python3 -I docs/avaliacoes/scripts/shell_medir.py --pid <PID do uvicorn> --arquivo ~/.cache/alfred-aval-shell/yes200M.txt --resposta ~/.cache/alfred-aval-shell/resposta.json --so timeout,concorrencia,saida --tamanhos 1,10,50,200
```

```bash
cd <scratch vazio> && env -i PATH=/usr/bin:/bin HOME=$HOME prlimit --as=3221225472 python3 -I -u <repo>/docs/avaliacoes/scripts/shell_odysseus.py <clone>/services/shell/service.py
```

```bash
<venv do executor>/bin/python -I docs/avaliacoes/scripts/shell_fuga.py services/shell-executor
```

No teste de saída enorme, comece por `--tamanhos 1,10` e só passe para 50 e 200 se o RSS ficar razoável. A
resposta de 200 MB tem 315 MB.

## 4. Resultados

### 4.1 Comando que não termina

| Caso | Alfred (HTTP, instância de teste) | Odysseus `ShellService` |
|---|---|---|
| comando simples, timeout 2 s | HTTP 408 em **2,01 s**; 0 processos vivos depois | `timed_out=True`, mas `execute()` só voltou em **30,18 s**; o `sleep` estava vivo aos 3 s |
| sequência (`…; true`), timeout 2 s | recusado pela allowlist | voltou em **30,18 s**; filho vivo aos 3 s |
| pipeline, timeout 2 s | `tail -f … \| grep …`: HTTP 408 em **2,00 s**; 0 vivos | `sleep … \| cat`: voltou em **30,18 s**; filho vivo aos 3 s |
| comando simples, timeout 10 s | HTTP 408 em 10,01 s; 0 vivos | — |
| cliente desiste em 1 s (comando com timeout 6 s) | o comando **continua**: 1 vivo aos 3 s, 0 aos 7,5 s (morre no timeout do comando, não na desconexão) | — |

No Odysseus, o `kill()` acerta só o `/bin/sh`. O filho (`sleep 30`) fica vivo, segura o pipe e, no Python 3.14,
`await proc.wait()` espera os pipes fecharem. Por isso o timeout de 2 s virou 30 s, que é a vida natural do
filho. Com um filho que nunca termina, como `tail -f`, `execute()` não voltaria. O Alfred não tem esse defeito,
porque mata o grupo inteiro.

### 4.2 Saída enorme

Alfred: `head -c N` de um arquivo `y\n`, por HTTP. A resposta foi gravada em arquivo. O RSS é `VmHWM`/`VmRSS` do
uvicorn da instância de teste, com o pico zerado antes de cada pedido.

| N | HTTP | Bytes da resposta | Tempo | Pico de RSS do serviço | RSS depois | Pico no `/tmp` (tmpfs) |
|---|---|---|---|---|---|---|
| 1 MB | 200 | 1 572 904 | 0,03 s | 59 MiB (base 55) | 57 MiB | ~0 MiB |
| 10 MB | 200 | 15 728 680 | 0,17 s | 97 MiB (base 57) | 72 MiB | 10 MiB |
| 50 MB | 200 | 78 643 240 | 0,72 s | 296 MiB (base 72) | 96 MiB | 50 MiB |
| 200 MB | 200 | **314 572 840** | 2,33 s | **896 MiB** (base 96) | 96 MiB | **200 MiB** |

Odysseus: `yes | head -c N`, no próprio processo.

| N | `len(stdout)` devolvido | Tempo | Pico de RSS do processo |
|---|---|---|---|
| 1 MB | 200 000 | 0,01 s | 25 MiB (base 24) |
| 10 MB | 200 000 | 0,03 s | 43 MiB (base 24) |
| 50 MB | 200 000 | 0,12 s | 123 MiB (base 23) |
| 200 MB | 200 000 | 0,44 s | **424 MiB** (base 23) |

Leitura dos números:

- **Alfred:** a resposta cresce 1,5 vez a saída, porque o JSON escapa o `\n`. O pico de RSS cresce cerca de
  4 vezes a saída: os bytes, a `str`, o JSON e o corpo HTTP ficam todos na memória ao mesmo tempo. A saída
  também ocupa o tmpfs enquanto o comando roda. Não há teto: o limite prático é o tamanho do tmpfs (16 GiB) e o
  timeout, que vai até 1800 s.
- **Quem chama o Alfred:** `lib_ferramentas` ainda recebe os 315 MB inteiros antes de cortar para 12 000
  caracteres.
- **Odysseus:** a resposta fica limitada a 200 000 caracteres, mas a memória do processo ainda cresce cerca de
  2 vezes a saída, porque `communicate()` lê tudo antes de cortar.

### 4.3 Concorrência

Alfred: N pedidos `date` disparados juntos, por HTTP. A carga ficou em cerca de 11,5 durante esta rodada.

| N simultâneos | p50 | p95 | Máx | Parede | Status |
|---|---|---|---|---|---|
| 1 | 11 ms | 11 ms | 11 ms | 0,01 s | 1 × 200 |
| 10 | 31 ms | 43 ms | 49 ms | 0,05 s | 10 × 200 |
| 25 | 65 ms | 75 ms | 82 ms | 0,09 s | 25 × 200 |
| 50 | 83 ms | 119 ms | 134 ms | 0,14 s | 50 × 200 |

Alfred: K comandos lentos (`tail -f`, timeout 6 s) já rodando. Depois, 3 `/health` e 5 `date` disparados na mesma
rajada.

| Lentos rodando | `/health` p50 | `date` p50 | `date` máx |
|---|---|---|---|
| 1 | 12 ms | 21 ms | 30 ms |
| 39 | 15 ms | 34 ms | 54 ms |
| **40** | **4 826 ms** | **4 848 ms** | 4 855 ms |
| **45** | **4 893 ms** | **4 917 ms** | 4 934 ms |

Um pedido lento não trava os outros até haver 39 lentos ao mesmo tempo. No 40º, o pool do anyio (40 threads)
enche, e **tudo** espera um lento terminar, inclusive o `/health`. Com timeout de 1800 s, 40 pedidos deixam o
executor parado por até 30 minutos.

Odysseus (sem HTTP): 50 `date` simultâneos dão p50 29 ms e máximo 47 ms. 50 `sleep 1` simultâneos levam 1,04 s
de parede: não há pool que encha, mas também não há limite.

### 4.4 Fuga da allowlist (`shell_fuga.py`)

| Categoria | Tentativas | Passam | Passam só como texto literal |
|---|---|---|---|
| `;` (sequência) | 4 | 0 | 0 |
| `&&`, `\|\|`, `&` | 4 | 0 | 0 |
| `$()` e crase | 5 | 0 | 0 |
| variáveis (`$HOME`, `${…}`, `PATH=… cmd`, `env`) | 6 | 0 | 0 |
| quebra de linha e escape | 4 | 0 | 0 |
| redirecionamento (`>`, `>>`, `<`, `2>&1`) | 5 | 0 | 0 |
| pipe para interpretador (`\| sh`, `\| xargs`) | 5 | 0 | 0 |
| caminhos relativos e absolutos (`./`, `../briefing/run.sh`, `/usr/bin/id`) | 7 | 0 | 0 |
| aspas (`d'a't'e'`, `date '$(id)'`, `date ';' id`) | 5 | 5 | 5 |
| **Total** | **45** | **5** | **5** |

As 5 que passam rodam só o `date`. O resto do comando vira argumento literal, porque sem shell não há expansão,
e o `id` nunca roda. Fugas reais por sintaxe de shell: **0 de 45**. No `ShellService` do Odysseus, que não tem
allowlist, 8 de 8 encadeamentos executaram o segundo comando (`;`, `&&`, `||`, `$()`, crase, variável, `${…}`,
quebra de linha).

As brechas **semânticas** foram medidas pela avaliação de Skills (branch `claude/avaliacao-skills-544700`,
`docs/avaliacoes/scripts/skills_risco.py`) e não foram repetidas aqui. Lá, 12 de 12 comandos da allowlist passam
mesmo lendo ou escrevendo o que não deviam:

- `curl` aceita `-X`/`-d` para serviços locais;
- `docker inspect` passa;
- `head`, `tail` e `grep` leem arquivos fora do filtro de caminho sensível;
- `nvidia-smi` e o `run.sh` do briefing aceitam argumentos livres.

Nada disso é tecnologia do Odysseus: o `ShellService` não tem allowlist nenhuma.

### 4.5 Resumo comparado

| Garantia | Alfred `shell-executor` | Odysseus `ShellService` | Odysseus `containment.py` (agente) |
|---|---|---|---|
| Política do que pode rodar | allowlist + parser sem shell; 0/45 fugas por sintaxe | nenhuma (só admin pode chamar) | nenhuma no texto; isolamento por bwrap |
| Timeout corta no prazo | sim (±0,01 s) | **não** com filho vivo (2 s → 30,18 s) | sim, por grupo, com verificação |
| Filho morre | sim (`killpg`) | **não** | sim (grupo + PID namespace) |
| Teto da resposta | **não há** (200 MB → 315 MB) | 200 000 caracteres | 10 000 caracteres |
| Memória limitada na leitura | **não** (≈4 × a saída, mais o tmpfs) | **não** (≈2 × a saída) | sim (orçamento em streaming) |
| Concorrência | 40 vagas; a 40ª trava tudo, até o `/health` | sem limite | — |
| Desconexão do cliente cancela | não | — | — |

## 5. Pontuação

- **Ganho para Pedro: 2/5.** O executor já é mais seguro que o `ShellService` no que mais importa: o timeout corta
  e a sintaxe de shell não passa. O ganho real seriam duas ideias do `containment.py`:
  - **Teto de saída lido em streaming.** Um `journalctl --no-pager` sem `-n` ou um `find /` escolhido pelo modelo
    hoje vira centenas de MB de RAM no executor, no tmpfs e no inbox, para no fim mostrar 12 000 caracteres.
  - **Não deixar o pool encher.** Com o teto de concorrência, `/health` e os outros pedidos não param.

  É raro no uso diário: o clique "Executar?" filtra a maioria dos comandos.
- **Custo de implementar:** cerca de 3 a 4 horas.
  - `services/shell-executor/main.py`: ler stdout e stderr com orçamento, marcar `truncado`, limitar a
    concorrência com resposta 429/503 e tornar o `/health` `async`.
  - `services/shell-executor/about.md`.
  - `services/inbox/tests/test_servicos.py`: testes novos de teto e de concorrência.
  - Opcional: mostrar o aviso de corte em `lib_ferramentas.executar_comando`.
- **Risco: 1/5.** A mudança é local e sem dependência nova. O único cuidado é escolher um teto (por exemplo,
  1 MiB) que não corte as saídas que as ferramentas usam; hoje todas têm poucos KB (`relatorio_sistema`, `df`,
  `nvidia-smi`).
- **Custo de recursos na máquina:** VRAM 0. A CPU não muda. A latência não muda (`date` ≈ 11 ms). A RAM cai: o
  pico por pedido passa de cerca de 4 × a saída (896 MiB para 200 MB) para cerca de 4 × o teto (estimativa, não
  medida: ≈5 MiB com teto de 1 MiB), e o tmpfs deixa de crescer sem limite.
  - Trazer o bwrap do Odysseus (não medido) somaria um processo a cada comando. Pelo `_bwrap_prefix`, que não
    monta `/run` nem `/home`, ele quebraria `docker`, `systemctl --user` e `journalctl --user`, que precisam do
    host. Não vale a pena para uma allowlist só de leitura.
- **Nota final: 4/10.** Trocar o executor pelo `ShellService` seria um retrocesso: o timeout não corta, o filho
  sobrevive e 8 de 8 encadeamentos passam. Do Odysseus vale adaptar só o teto de saída em streaming e o cuidado com
  o pool, que são pequenos, baratos e de ganho modesto.

## 6. Veredito

**Adaptar**, sem trocar o executor pelo `ShellService`. Trazer duas ideias, com código próprio:

1. **Teto de saída aplicado durante a leitura**, como o `_drain` de `src/containment.py`: ler em blocos, guardar
   até N bytes por stream, descartar o resto sem parar de ler e devolver `truncado: true`.
2. **Limite de comandos simultâneos abaixo das 40 threads do anyio** (por exemplo, 8), respondendo HTTP 429
   quando estiver cheio, e `/health` como `async def`, para nunca disputar o pool.

**Menor próximo passo:** em `_executar` (`services/shell-executor/main.py`), limitar stdout e stderr a 1 MiB cada,
com a marca `truncado`. Escrever antes o teste em `services/inbox/tests/test_servicos.py`: `head -c 50M` de um
arquivo de teste tem de voltar com `truncado: true` e menos de 2 MB de stdout (hoje volta com 78,6 MB de
resposta). Depois, repetir `shell_medir.py --so saida` para conferir o pico de RSS (hoje: 296 MiB para 50 MB).

**Fora do escopo:** as brechas semânticas da allowlist (`curl -X/-d` para serviços locais, `docker inspect`,
leitura de arquivos fora do filtro) não têm relação com o Odysseus. Elas estão na avaliação de Skills e merecem
uma tarefa própria.
