#!/usr/bin/env python3
"""
Integrações — programas de fora deste repositório que o Alfred instala, liga e abre. Hoje só o Pestinha
(pet de tela, GPL-3.0, repositório próprio), que mostra a fila e o trabalho do Alfred — ver PESTINHA.md.

    ./run.sh integracoes                       # estado
    ./run.sh integracoes instalar | atualizar | abrir | fechar | login-ligar | login-desligar

O Painel (tela Integrações) chama as mesmas funções. Só é baixado o que está fixo aqui (REPO e RAMO): nenhum
endereço vem de fora. O pet abre pela unit de usuário alfred-pestinha.service, porque a API não tem janela.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared import lib_atividade  # noqa: E402
from _shared.lib_alfred import PROJECT_ROOT, gravar_atomico, log  # noqa: E402

REPO = "https://github.com/PedroNettoDs/pestinha.git"
RAMO = "alfred"                              # trocar para "main" quando o PR alfred → main for aceito
PASTAS = ("pestinha", "pets", "assets")      # o que o pet precisa para rodar; fica de fora o .exe de 61 MB
UNIT = "alfred-pestinha.service"
MODELO_UNIT = PROJECT_ROOT / "services" / "scripts" / UNIT
ACOES = ("instalar", "atualizar", "abrir", "fechar", "login-ligar", "login-desligar")
LONGAS = ("instalar", "atualizar")           # baixam e instalam: no Painel rodam em segundo plano


class Falha(RuntimeError):
    """Erro com o texto pronto para mostrar a Pedro."""


def pasta() -> Path:
    """Onde o pet mora. PESTINHA_DIR existe para os testes; o lançador da unit usa o mesmo padrão."""
    return Path(os.getenv("PESTINHA_DIR") or Path.home() / "dev" / "pestinha")


def _dir_units() -> Path:
    return Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def _rodar(cmd: list, timeout: float = 15) -> tuple[int, str]:
    """(código de saída, saída) de um comando; 127 se o programa não existe, 124 se estourou o tempo."""
    try:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except FileNotFoundError:
        return 127, f"{cmd[0]} não encontrado"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} não terminou em {timeout:.0f} s"
    return r.returncode, (r.stdout + r.stderr).strip()


def _exigir(cmd: list, erro: str, timeout: float = 15) -> str:
    rc, saida = _rodar(cmd, timeout)
    if rc != 0:
        raise Falha(f"{erro}: {(saida.splitlines() or ['sem saída'])[-1][:200]}")
    return saida


def _ler_json(arquivo: Path) -> dict:
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dados if isinstance(dados, dict) else {}


def _pids_do_pet(p: Path) -> list[int]:
    """Processos que são o pet desta pasta, conferidos pelo argv exato ([python, <pasta>/main.py, …]): um editor
    com o main.py aberto, ou um shell com o caminho na linha de comando, não conta (e não é fechado)."""
    rc, saida = _rodar(["pgrep", "-f", "main.py"])
    pids = []
    for pid in saida.split() if rc == 0 else []:
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(argv) >= 2 and b"python" in argv[0] and argv[1].decode(errors="replace") == str(p / "main.py"):
            pids.append(int(pid))
    return pids


def _systemctl_diz(verbo: str, unit: str) -> str:
    return _rodar(["systemctl", "--user", verbo, unit])[1]


# ── estado ────────────────────────────────────────────────────

def estado() -> dict:
    p = pasta()
    codigo = (p / "main.py").is_file()
    venv = (p / "venv" / "bin" / "python").is_file()
    ramo = _rodar(["git", "-C", p, "branch", "--show-current"])[1] if codigo else ""
    commit = _rodar(["git", "-C", p, "log", "-1", "--format=%h"])[1] if codigo else ""
    pela_unit = _systemctl_diz("is-active", UNIT) == "active"
    a_mao = bool(_pids_do_pet(p)) if not pela_unit else False
    return {
        "pasta": str(p), "repo": REPO, "codigo": codigo, "venv": venv, "instalado": codigo and venv,
        "ramo": ramo if "\n" not in ramo else "", "commit": commit if "\n" not in commit else "",
        "ligado": _ler_json(p / "memory" / "alfred.json").get("pasta") == str(lib_atividade.pasta()),
        "unit": (_dir_units() / UNIT).is_file(),
        "rodando": pela_unit or a_mao, "a_mao": a_mao,
        "no_login": _systemctl_diz("is-enabled", UNIT) == "enabled",
        "xprintidle": shutil.which("xprintidle") is not None,
        "sessao_grafica": _systemctl_diz("is-active", "graphical-session.target") == "active",
        "publicando": time.time() - (lib_atividade.estado().get("atualizado") or 0) < 20,
        "tarefa": tarefa(),
    }


# ── ações ─────────────────────────────────────────────────────

def _dependencias(p: Path, passo: Callable[[str], None]) -> None:
    if not (p / "venv" / "bin" / "python").is_file():
        passo("Criando o ambiente Python")
        _exigir([sys.executable, "-m", "venv", p / "venv"], "Não deu para criar o venv", timeout=120)
    passo("Instalando as dependências (PySide6, numpy)")
    _exigir([p / "venv" / "bin" / "pip", "install", "--quiet", "-r", p / "requirements.txt"],
            "Não deu para instalar as dependências", timeout=900)


def _ligar(p: Path) -> None:
    """memory/alfred.json do pet aponta para a pasta de atividade deste repositório."""
    alvo = {"pasta": str(lib_atividade.pasta())}
    arquivo = p / "memory" / "alfred.json"
    if _ler_json(arquivo) != alvo:
        gravar_atomico(arquivo, json.dumps(alvo, ensure_ascii=False) + "\n")


def _gravar_unit() -> None:
    """A unit de usuário, a partir do mesmo modelo e com a mesma troca que o instalar.sh faz."""
    raiz = str(PROJECT_ROOT).replace("%", "%%")          # % é especificador no systemd
    texto = MODELO_UNIT.read_text(encoding="utf-8").rstrip("\n").replace("@ALFRED_DIR@", raiz) + "\n"
    destino = _dir_units() / UNIT
    if not destino.is_file() or destino.read_text(encoding="utf-8") != texto:
        gravar_atomico(destino, texto)
        _rodar(["systemctl", "--user", "daemon-reload"])


def instalar(passo: Callable[[str], None] = lambda _t: None) -> str:
    """Baixa (se faltar), instala as dependências, liga ao Alfred e grava a unit. Pode rodar de novo."""
    p = pasta()
    if not (p / "main.py").is_file():
        if p.exists() and any(p.iterdir()):
            raise Falha(f"{p} já existe e não é o Pestinha. Mova a pasta e tente de novo.")
        passo("Baixando o código")
        _exigir(["git", "clone", "--depth", "1", "--branch", RAMO, "--filter=blob:none", "--sparse", REPO, p],
                "Não deu para baixar o Pestinha", timeout=300)
        _exigir(["git", "-C", p, "sparse-checkout", "set", *PASTAS],
                "Não deu para preparar a pasta do Pestinha", timeout=120)
    _dependencias(p, passo)
    passo("Ligando ao Alfred")
    _ligar(p)
    _gravar_unit()
    return f"Pestinha instalado em {p}."


def atualizar(passo: Callable[[str], None] = lambda _t: None) -> str:
    """Só avança (--ff-only) e nunca por cima de trabalho local na pasta do pet."""
    p = pasta()
    if not (p / "main.py").is_file():
        raise Falha("O Pestinha ainda não está instalado.")
    if _exigir(["git", "-C", p, "status", "--porcelain", "--untracked-files=no"],
               "Não deu para conferir a pasta do Pestinha"):
        raise Falha(f"Há alterações locais em {p}. Guarde ou descarte com o git antes de atualizar.")
    antes = _rodar(["git", "-C", p, "log", "-1", "--format=%h"])[1]
    passo("Buscando a versão nova")
    _exigir(["git", "-C", p, "pull", "--ff-only"], "Não deu para atualizar", timeout=300)
    _dependencias(p, passo)
    _ligar(p)
    _gravar_unit()
    depois = _rodar(["git", "-C", p, "log", "-1", "--format=%h"])[1]
    if antes == depois:
        return "O Pestinha já estava na última versão."
    return f"Pestinha atualizado ({antes} → {depois}). Feche e abra o pet para valer."


def abrir() -> str:
    e = estado()
    if not e["instalado"]:
        raise Falha("Instale o Pestinha primeiro.")
    if e["rodando"]:
        return "O Pestinha já está aberto" + (" (aberto à mão, fora da unit)." if e["a_mao"] else ".")
    if not e["sessao_grafica"]:
        raise Falha("Sem sessão gráfica ativa: o pet só abre com a área de trabalho aberta.")
    _gravar_unit()
    _exigir(["systemctl", "--user", "start", UNIT], "Não deu para abrir o Pestinha")
    return "Pestinha aberto."


def fechar() -> str:
    _rodar(["systemctl", "--user", "stop", UNIT])
    for pid in _pids_do_pet(pasta()):            # aberto à mão, fora da unit
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return "Pestinha fechado."


def no_login(ligar: bool) -> str:
    _gravar_unit()
    _exigir(["systemctl", "--user", "enable" if ligar else "disable", UNIT],
            "Não deu para mudar a abertura no login")
    return "O Pestinha vai abrir no login." if ligar else "O Pestinha não abre mais no login."


def executar(acao: str, passo: Callable[[str], None] = lambda _t: None) -> str:
    """Uma ação pelo nome (ACOES). → mensagem para mostrar; Falha com o motivo se não deu."""
    if acao == "instalar":
        return instalar(passo)
    if acao == "atualizar":
        return atualizar(passo)
    if acao == "abrir":
        return abrir()
    if acao == "fechar":
        return fechar()
    if acao in ("login-ligar", "login-desligar"):
        return no_login(acao == "login-ligar")
    raise Falha("Ação desconhecida.")


# ── tarefa longa (Painel): uma por vez, em segundo plano ──────

_TAREFA_LOCK = threading.Lock()
_TAREFA = {"acao": "", "andando": False, "passo": "", "mensagem": "", "erro": ""}


def tarefa() -> dict:
    with _TAREFA_LOCK:
        return dict(_TAREFA)


def _marcar(**campos) -> None:
    with _TAREFA_LOCK:
        _TAREFA.update(campos)


def iniciar(acao: str) -> Optional[threading.Thread]:
    """Roda instalar/atualizar numa thread. → a thread, ou None se já há uma em andamento."""
    with _TAREFA_LOCK:
        if _TAREFA["andando"]:
            return None
        _TAREFA.update(acao=acao, andando=True, passo="Começando", mensagem="", erro="")

    def _fazer():
        mensagem = erro = ""
        try:
            mensagem = executar(acao, lambda texto: _marcar(passo=texto))
        except Falha as e:
            erro = str(e)
        except Exception as e:
            erro = f"Erro inesperado: {e}"
            log(f"[integracoes] {acao}: {e}")
        _marcar(andando=False, passo="", mensagem=mensagem, erro=erro)

    t = threading.Thread(target=_fazer, name="integracoes", daemon=True)
    t.start()
    return t


# ── CLI ───────────────────────────────────────────────────────

def adicionar_parser(sub) -> None:
    sp = sub.add_parser("integracoes", help="Pestinha (pet de tela): estado | " + " | ".join(ACOES))
    sp.add_argument("acao", nargs="?", default="estado", choices=["estado", *ACOES])


def cmd_integracoes(args) -> int:
    if args.acao != "estado":
        try:
            print(executar(args.acao, lambda texto: print(f"… {texto}", flush=True)))
        except Falha as e:
            print(f"Erro: {e}")
            return 1
        return 0
    e = estado()
    sim = lambda v: "sim" if v else "não"   # noqa: E731
    onde = f"{e['pasta']} ({e['ramo']} {e['commit']})" if e["codigo"] else "não instalado"
    print(f"Pestinha:          {onde}" + ("" if e["venv"] or not e["codigo"] else " — falta o venv (instalar)"))
    print(f"Ligado ao Alfred:  {sim(e['ligado'])}" + ("" if e["publicando"] else " — o Alfred não está publicando"))
    print(f"Rodando:           {sim(e['rodando'])}" + (" (aberto à mão, fora da unit)" if e["a_mao"] else ""))
    print(f"Abre no login:     {sim(e['no_login'])}")
    print("xprintidle:        " + ("instalado" if e["xprintidle"] else "falta — sudo apt install xprintidle"))
    return 0
