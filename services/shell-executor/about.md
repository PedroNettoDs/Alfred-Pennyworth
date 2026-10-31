# shell-executor — Gateway de comandos do host

API REST mínima que permite ao Open WebUI executar comandos shell no host sob uma whitelist estrita. Resolve o problema de o Open WebUI (Docker) não ter acesso direto ao sistema operacional do host.

## Como rodar

```bash
./run.sh          # inicia na porta PORT_SHELL_EXECUTOR (default: 7071)
```

O `run.sh` lê o `.env` da raiz literalmente, linha a linha (antes fazia `source`, e um valor com `$` ou `(`
derrubava o serviço).

Em execução contínua como unit de **usuário** `alfred-executor.service`, gerada pelo `services/scripts/instalar.sh`
a partir do modelo deste diretório (`@ALFRED_DIR@` → caminho do repositório; não copie o arquivo direto — ver
[README → Setup](../../README.md#4-setup)):

```bash
systemctl --user status alfred-executor
systemctl --user restart alfred-executor      # depois de alterar o .env ou o main.py
journalctl --user -fu alfred-executor
```

**Máquina original:** o executor roda como unit de **sistema** (`/etc/systemd/system/alfred-executor.service`,
`User=netto`, `EnvironmentFile=` do `.env`, uvicorn direto, sem o `run.sh`). O `instalar.sh` detecta essa unit e não instala a de
usuário por cima (seriam duas na mesma porta); lá os comandos são `sudo systemctl status|restart alfred-executor` e
`sudo journalctl -fu alfred-executor`.

## Endpoints

### `POST /execute`
Executa um comando da whitelist e retorna stdout/stderr.

**Request:**
```json
{
  "command": "ollama list",
  "timeout": 15
}
```

**Response (sucesso):**
```json
{
  "stdout": "NAME                    ID      SIZE   MODIFIED\nllama3.1:8b  ...",
  "stderr": "",
  "returncode": 0
}
```

**Response (comando não permitido):**
```json
{
  "detail": "Comando não permitido: 'rm -rf /'. Consulte ALLOWED_PREFIXES."
}
```
HTTP 403.

### `GET /health`
Retorna `{"status": "ok", "version": "2.1.0"}`.

## Autenticação

Todas as requisições exigem `Authorization: Bearer <SHELL_EXECUTOR_TOKEN>`. O token é definido no `.env` da raiz do projeto. Sem token correto: HTTP 401.

## Whitelist de comandos (`ALLOWED_PREFIXES`)

Apenas comandos cujo início bate, **palavra a palavra**, com um dos prefixos abaixo são aceitos (`date` não casa
`dateX`; prefixo terminado em `-` ou `/` casa o começo da última palavra, ex.: `alfred-` → `alfred-inbox`):

```
docker (só leitura), df, free, uptime, nvidia-smi, ollama
systemctl status, systemctl is-active, systemctl --user status, systemctl --user is-active
journalctl --user -u alfred-<unit>
ps aux, top -bn1, cat /proc/<arquivo>, lsblk
ip addr, ss -tlnp, uname, lscpu, sensors
du -sh, wc -l, head, tail, grep, find, ls, pwd, whoami, date
curl http://localhost, curl http://127.0.0.1
<repositório>/services/briefing/run.sh   (calculado a partir de main.py; aceita caminho por symlink)
```

As units do Alfred são de **usuário**: para ver o estado use `systemctl --user status alfred-executor --no-pager`,
`systemctl --user is-active alfred-inbox` e `journalctl --user -u alfred-inbox -n 50 --no-pager` (o `systemctl
status` sem `--user` olha as units de sistema e responde "could not be found"/"inactive").

Regras extras:

- **Operadores de shell:** o comando é recusado se tiver, fora de aspas simples, `;` `&` `&&` `||` `<` `>` `$` `(`
  `)` crase `{` `}` `\` ou quebra de linha. Só o pipe simples `a | b | c` é aceito, e **cada** trecho tem de bater
  com a whitelist (`lscpu | grep -E 'Model name|CPU\(s\)' | head -5` passa; `pwd | sed ...` não). Exceção única:
  `2>/dev/null` no fim do comando (usado pelo `alfred_system_monitor`) — o stderr é descartado.
- **docker:** só `ps`, `logs`, `inspect`, `images`, `version`, `info`, `stats --no-stream`, `system df`,
  `compose ps` e `compose logs` (subcomando logo após `docker`).
- **curl:** toda URL tem de ter host exatamente `localhost` ou `127.0.0.1` (a URL é interpretada: `localhost.x.com`
  e `localhost@outro-host` são recusados) e só opções de leitura (`-s -S -f -i -I -v -H -X -m -d -w` e afins);
  `-o`, `-O`, `-T`, `-K`, `-x`, `-L`, `-d @arquivo` etc. são recusadas.
- **find:** sem `-exec`, `-execdir`, `-ok`, `-delete`, `-fprint*`, `-fls`. **journalctl:** sem `--vacuum-*`,
  `--rotate`, `--flush` e afins. **cat:** só arquivos que, resolvidos, ficam em `/proc/`.
- **run.sh do briefing:** o caminho não é fixo (antes era `/home/netto/Alfred-Pennyworth/...`), é calculado a partir
  da pasta do `main.py`; se a primeira palavra do comando é um caminho absoluto que, resolvido (`realpath`), leva a
  esse arquivo — por exemplo via symlink `~/Alfred-Pennyworth` → outra pasta —, o comando é aceito (com argumentos,
  ex.: o tema do briefing ad-hoc).

Para adicionar um comando novo, edite `ALLOWED_PREFIXES` em `main.py`.

## Casos de uso no Open WebUI

O Open WebUI usa este serviço via tool calling. Exemplos de tools que chamam o executor:

- **`alfred_system_monitor.py`** — `df -h`, `free -h`, `nvidia-smi`, `uptime` → status do servidor
- **`alfred_shell_executor.py`** — execução direta de comandos permitidos
- **`alfred_briefing.py`** (indireto) — dispara `<alfred_dir_host>/services/briefing/run.sh` para gerar briefing sob demanda. A Valve `alfred_dir_host` precisa ser a pasta do repositório (o `instalar.sh` imprime no resumo): com o padrão `/home/netto/Alfred-Pennyworth` numa instalação em outra pasta, o executor recusa o comando (HTTP 403)

## Segurança

- **Whitelist por prefixo, palavra a palavra:** nenhum comando fora da lista passa, nem encadeado a um permitido
  (`date; id`, `date && id`, `date $(id)`, `ls\nid` → HTTP 403).
- **Token Bearer:** mesmo dentro da rede local, o caller precisa do token.
- **Timeout:** padrão de 15s, de 1 a 1800 por request (fora disso: HTTP 422). No timeout o **grupo de processos**
  inteiro do comando é morto (antes só o `/bin/sh` morria e os filhos seguiam rodando) e a resposta é HTTP 408.
- **Sem shell:** o comando é quebrado com `shlex` e o pipeline é montado pelo próprio executor (`shell=False`) —
  não há expansão de variável, glob ou substituição de comando; só `~` no início de um argumento vira a home.
- **Saída estável:** os comandos rodam com `LC_ALL=C.UTF-8` (saída em inglês: o `alfred_system_monitor` procura
  `Model name` no `lscpu`).

## Dependências

```
fastapi, uvicorn
```

Nenhuma lib compartilhada — serviço propositalmente mínimo.

## Variáveis de ambiente

| Variável | Uso |
|----------|-----|
| `SHELL_EXECUTOR_TOKEN` | Token Bearer obrigatório para autenticação |
| `PORT_SHELL_EXECUTOR` | Porta do serviço (default: `7071`) |
