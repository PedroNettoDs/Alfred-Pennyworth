"""
Alfred Shell Executor — FastAPI
Executa comandos no host sob whitelist restrita.
Token via variável de ambiente SHELL_EXECUTOR_TOKEN.
"""

import os
import re
import selectors
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Alfred Shell Executor", version="2.2.0")

TOKEN = os.getenv("SHELL_EXECUTOR_TOKEN", "")

# Cada prefixo é comparado PALAVRA a PALAVRA com o início de cada trecho do comando ("date" não casa "dateX").
# Prefixo terminado em "-" ou "/" casa o início da última palavra ("alfred-" → "alfred-inbox").
# docker, curl, find e cat /proc têm regras extras em _segmento_permitido().
ALLOWED_PREFIXES = [
    "docker",
    "df",
    "free",
    "uptime",
    "nvidia-smi",
    "ollama",
    "systemctl status",
    "systemctl is-active",
    "systemctl --user status",
    "systemctl --user is-active",
    "journalctl --user -u alfred-",
    "ps aux",
    "top -bn1",
    "cat /proc/",
    "lsblk",
    "ip addr",
    "ss -tlnp",
    "uname",
    "lscpu",
    "sensors",
    "du -sh",
    "wc -l",
    "head",
    "tail",
    "grep",
    "find",
    "ls",
    "pwd",
    "whoami",
    "date",
    "curl http://localhost",
    "curl http://127.0.0.1",
    # caminho real do repositório (antes fixo em /home/netto/Alfred-Pennyworth): vale em qualquer pasta
    str(Path(__file__).resolve().parent.parent / "briefing" / "run.sh"),
]
# A tool alfred_briefing manda o caminho que está na Valve alfred_dir_host — pode passar por symlink
# (ex.: ~/Alfred-Pennyworth → /mnt/SSD/alfred). is_allowed() aceita qualquer caminho que leve a este arquivo.
BRIEFING_RUN_SH = os.path.realpath(Path(__file__).resolve().parent.parent / "briefing" / "run.sh")

# docker: só leitura. Subcomando logo depois de "docker" (sem opção global antes).
DOCKER_LEITURA = {"ps", "logs", "inspect", "images", "version", "info"}
DOCKER_COMPOSE_LEITURA = {"ps", "logs"}
# find: ações que executam, apagam ou gravam arquivo
FIND_PROIBIDOS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
# journalctl: operações que mexem no journal
JOURNALCTL_PROIBIDOS = ("--vacuum", "--rotate", "--flush", "--sync", "--relinquish", "--smart-relinquish",
                        "--setup-keys", "--update-catalog", "--cursor-file")
# curl: só leitura de localhost — nada de gravar/enviar arquivo, proxy, redirecionamento ou outro host
CURL_FLAGS = {"-s", "-S", "-sS", "-Ss", "-f", "-sf", "-fs", "-sSf", "-fsS", "-i", "-I", "-v",
              "--silent", "--show-error", "--fail", "--include", "--head", "--compressed"}
CURL_COM_VALOR = {"-H", "--header", "-X", "--request", "-m", "--max-time", "--connect-timeout",
                  "-d", "--data", "--data-raw", "-w", "--write-out"}
CURL_HOSTS = {"localhost", "127.0.0.1"}

# Operadores de shell rejeitados fora de aspas simples (o pipe simples "|" é tratado à parte)
_METACARACTERES = set(";&<>()`$\\{}")
_DESCARTA_STDERR = re.compile(r"\s+2>/dev/null\s*$")   # "docker ps ... 2>/dev/null" (alfred_system_monitor)
TIMEOUT_MAX = 1800
# Saída: cada stream (stdout, stderr) guarda até MAX_SAIDA_BYTES; o resto é lido e descartado e a resposta vem com
# "truncado": true. Sem teto, 200 MB de saída viravam 315 MB de resposta e 896 MiB de pico no executor.
MAX_SAIDA_BYTES = 1 << 20
# Comandos ao mesmo tempo. O handler síncrono roda no pool do anyio (40 threads): com 40 comandos lentos o pool
# enchia e até o /health esperava. Acima do limite, HTTP 429 na hora.
MAX_SIMULTANEOS = 8
_vagas = threading.BoundedSemaphore(MAX_SIMULTANEOS)
# ollama: só leitura (nada de rm, run, pull, cp...)
OLLAMA_LEITURA = {"list", "ls", "ps", "show", "--version", "-v"}
# cat: só arquivos informativos do /proc (nunca /proc/<pid>/environ, cmdline, mem...: o ambiente tem segredos)
CAT_PROC_OK = re.compile(r"^/proc/(cpuinfo|meminfo|loadavg|uptime|version|stat|swaps|mounts|partitions|diskstats|vmstat"
                         r"|net/dev|driver/nvidia/version|pressure/(cpu|memory|io))$")
# grep recursivo varreria o repositório (e o .env): não
GREP_RECURSIVO = re.compile(r"^(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--dereference-recursive|--directories=recurse|-d)$")
# Ambiente dos comandos: só o necessário. O run.sh do executor exporta o .env inteiro (tokens, senhas) para este
# processo; os comandos executados não herdam isso.
ENV_FILHOS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "DOCKER_HOST", "TERM")


def _caminho_sensivel(arg: str) -> bool:
    """.env*, ~/.ssh, ~/.gnupg, perfis de navegador/e-mail e /proc/<pid>|self — nunca lidos pelo executor."""
    if arg.startswith("-") and "=" not in arg:
        return False
    alvo = arg.split("=", 1)[1] if arg.startswith("-") else arg
    real = os.path.realpath(os.path.expanduser(alvo))
    nome = os.path.basename(real)
    if nome.startswith(".env") or nome in (".netrc", ".pgpass", "token", "id_rsa", "id_ed25519"):
        return True
    if re.match(r"^/proc/(\d+|self|thread-self)(/|$)", real):
        return True
    return any(s in real + "/" for s in ("/.ssh/", "/.gnupg/", "/.mozilla/", "/.thunderbird/", "/.config/thunderbird/",
                                          "/.config/Claude/", "/.local/share/keyrings/", "/.docker/"))


class CommandRequest(BaseModel):
    command: str
    timeout: int = Field(15, ge=1, le=TIMEOUT_MAX)


def _dividir_pipeline(cmd: str):
    """Separa o comando nos trechos de um pipeline "a | b | c" e devolve a lista de argv (shlex).
    None se houver qualquer outro operador de shell (; && || \\n $( ) crase < > ...) fora de aspas simples,
    ou aspas sem fechar. Nada é executado por shell: o pipeline é montado à mão em _executar()."""
    if any(c in cmd for c in "\n\r\x00"):
        return None
    trechos, atual, aspas = [], [], None
    for i, c in enumerate(cmd):
        if aspas == "'":
            if c == "'":
                aspas = None
        elif aspas == '"':
            if c in "$`\\":
                return None
            if c == '"':
                aspas = None
        elif c in "'\"":
            aspas = c
        elif c in _METACARACTERES:
            return None
        elif c == "|":
            if cmd[i + 1:i + 2] == "|" or (i and cmd[i - 1] == "|"):
                return None
            trechos.append("".join(atual))
            atual = []
            continue
        atual.append(c)
    if aspas:
        return None
    trechos.append("".join(atual))
    argvs = []
    for t in trechos:
        try:
            argv = shlex.split(t)
        except ValueError:
            return None
        if not argv:
            return None
        argvs.append([os.path.expanduser(a) if a.startswith("~") else a for a in argv])
    return argvs


def _casa_prefixo(argv: list, prefixo: str) -> bool:
    p = prefixo.split()
    if len(argv) < len(p) or argv[:len(p) - 1] != p[:-1]:
        return False
    ultimo, arg = p[-1], argv[len(p) - 1]
    if ultimo.endswith(("-", "/")):
        return arg.startswith(ultimo) and len(arg) > len(ultimo)
    return arg == ultimo


def _curl_permitido(argv: list) -> bool:
    urls, i = 0, 1
    while i < len(argv):
        a = argv[i]
        if a in CURL_FLAGS:
            i += 1
            continue
        if a in CURL_COM_VALOR:
            if i + 1 >= len(argv) or (a in ("-d", "--data") and argv[i + 1].startswith("@")):
                return False
            i += 2
            continue
        if a.startswith("-"):
            return False
        try:
            u = urlsplit(a)
            host = u.hostname
        except ValueError:
            return False
        if u.scheme not in ("http", "https") or host not in CURL_HOSTS or u.username or u.password:
            return False
        # o host tem de ser exatamente o netloc (sem "@", sem "localhost.qualquer.com")
        if u.netloc.rsplit(":", 1)[0] not in CURL_HOSTS:
            return False
        urls += 1
        i += 1
    return urls > 0


def _docker_permitido(argv: list) -> bool:
    if len(argv) < 2:
        return False
    sub = argv[1]
    if sub in DOCKER_LEITURA:
        return True
    if sub == "stats":
        return "--no-stream" in argv[2:]
    if sub == "system":
        return argv[2:3] == ["df"]
    if sub == "compose":
        return len(argv) > 2 and argv[2] in DOCKER_COMPOSE_LEITURA
    return False


def _segmento_permitido(argv: list) -> bool:
    if argv[0].startswith("/") and os.path.realpath(argv[0]) == BRIEFING_RUN_SH:
        return True   # caminho por symlink → o real (argumentos livres: tema do briefing ad-hoc)
    if any(_caminho_sensivel(a) for a in argv[1:]):
        return False
    if argv[0] == "ollama":
        return len(argv) > 1 and argv[1] in OLLAMA_LEITURA
    if argv[0] == "grep" and any(GREP_RECURSIVO.match(a) for a in argv[1:]):
        return False
    if argv[0] == "docker":
        return _docker_permitido(argv)
    if argv[0] == "curl":
        return _curl_permitido(argv)
    if argv[0] == "find" and any(a in FIND_PROIBIDOS for a in argv[1:]):
        return False
    if argv[0] == "journalctl" and any(a.startswith(JOURNALCTL_PROIBIDOS) for a in argv[1:]):
        return False
    if argv[0] == "cat":
        return len(argv) > 1 and all(CAT_PROC_OK.match(os.path.realpath(a)) for a in argv[1:])
    return any(_casa_prefixo(argv, p) for p in ALLOWED_PREFIXES)


def _analisar(cmd: str):
    """(lista de argv do pipeline, descartar_stderr) ou None se não permitido."""
    cmd = cmd.strip()
    descartar_stderr = bool(_DESCARTA_STDERR.search(cmd))
    if descartar_stderr:
        cmd = _DESCARTA_STDERR.sub("", cmd)
    argvs = _dividir_pipeline(cmd)
    if not argvs or not all(_segmento_permitido(a) for a in argvs):
        return None
    return argvs, descartar_stderr


def is_allowed(cmd: str) -> bool:
    return _analisar(cmd) is not None


def _executar(argvs: list, timeout: int, descartar_stderr: bool) -> dict:
    """Roda o pipeline sem shell, num grupo de processos próprio: no timeout o grupo inteiro morre
    (antes só o /bin/sh morria e os filhos seguiam rodando). LC_ALL=C.UTF-8: saída estável para as tools
    (o system_monitor procura "Model name" no lscpu).
    stdout e stderr são lidos por pipe enquanto o comando roda, até MAX_SAIDA_BYTES cada; o excedente é lido e
    descartado (o comando não trava com o pipe cheio). Antes iam inteiros para arquivos no /tmp (tmpfs: RAM)."""
    env = {k: os.environ[k] for k in ENV_FILHOS if k in os.environ}
    env["LC_ALL"] = "C.UTF-8"
    procs, pgid = [], 0
    err_r, err_w = os.pipe()   # um pipe de stderr para todos os trechos do pipeline
    stderr = subprocess.DEVNULL if descartar_stderr else err_w
    entrada = subprocess.DEVNULL
    try:
        for argv in argvs:
            p = subprocess.Popen(argv, stdin=entrada, stdout=subprocess.PIPE, stderr=stderr, env=env,
                                 process_group=pgid)
            if not pgid:
                pgid = p.pid
            if entrada is not subprocess.DEVNULL:
                entrada.close()
            entrada = p.stdout
            procs.append(p)
    except OSError as e:
        if pgid:
            os.killpg(pgid, signal.SIGKILL)
        for p in procs:
            p.wait()
        if entrada is not subprocess.DEVNULL:
            entrada.close()
        os.close(err_r)
        return {"stdout": "", "stderr": f"{argv[0]}: {e.strerror or e}\n", "returncode": 127, "truncado": False}
    finally:
        os.close(err_w)
    out_r = procs[-1].stdout.fileno()
    lidos = {out_r: bytearray(), err_r: bytearray()}
    truncado = False
    prazo = time.monotonic() + timeout
    sel = selectors.DefaultSelector()
    for fd in lidos:
        sel.register(fd, selectors.EVENT_READ)
    try:
        while sel.get_map():
            resto = prazo - time.monotonic()
            if resto <= 0:
                raise subprocess.TimeoutExpired(argvs[-1], timeout)
            eventos = sel.select(min(resto, 0.1))
            if not eventos and all(p.poll() is not None for p in procs):
                break   # os trechos já saíram e algum neto segura o pipe: não espera por ele
            for chave, _ in eventos:
                bloco = os.read(chave.fd, 1 << 16)
                if not bloco:
                    sel.unregister(chave.fd)
                    continue
                buf = lidos[chave.fd]
                truncado |= len(buf) + len(bloco) > MAX_SAIDA_BYTES
                buf += bloco[:MAX_SAIDA_BYTES - len(buf)]
        for p in procs:
            p.wait(timeout=max(0.01, prazo - time.monotonic()))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for p in procs:
            p.wait()
        raise
    finally:
        sel.close()
        procs[-1].stdout.close()
        os.close(err_r)
    return {
        "stdout": lidos[out_r].decode("utf-8", "replace"),
        "stderr": lidos[err_r].decode("utf-8", "replace"),
        "returncode": procs[-1].returncode,
        "truncado": truncado,
    }


@app.get("/health")
async def health():
    # async: roda no laço de eventos, fora do pool de threads dos comandos
    return {"status": "ok", "version": "2.2.0"}


@app.post("/execute")
def execute(
    req: CommandRequest,
    authorization: str = Header(default=""),
):
    if not TOKEN or authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="Token inválido")

    analise = _analisar(req.command)
    if analise is None:
        raise HTTPException(
            status_code=403,
            detail=f"Comando não permitido: '{req.command}'. Consulte ALLOWED_PREFIXES.",
        )

    if not _vagas.acquire(blocking=False):
        raise HTTPException(status_code=429, detail=f"Executor ocupado: {MAX_SIMULTANEOS} comandos já em execução")
    try:
        argvs, descartar_stderr = analise
        return _executar(argvs, req.timeout, descartar_stderr)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail=f"Timeout de {req.timeout}s excedido")
    finally:
        _vagas.release()
