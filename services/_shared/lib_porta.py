"""
lib_porta — a porta da GPU: antes de cada chamada ao Ollama, o Laya decide se ela entra agora ou espera.

    with lib_porta.passagem(modelo, num_ctx):      # dentro de ollama_generate e do stream da conversa
        ... chamada ao Ollama ...

    @lib_porta.origem("chat turn typed by the owner in the panel", dono=True)
    def _rodar(turno): ...                          # quem começa um trabalho diz quem é

Quem decide é o checkpoint treinado "porta" do Laya (services/laya/treino/gerar_porta.py), servido pelo
laya-serve (LAYA_EXTRA_MODELS no run.sh). Ele recebe quem pede, quem ocupa a placa, há quanto tempo
Pedro falou com o Alfred e se o modelo pedido já está carregado, e responde open ou wait.

O modelo só acerta se a pergunta, o estado e as descrições de origem chegarem a ele como no treino:
mudou um texto aqui ou uma descrição num @origem, acrescente ao gerador e treine de novo.

Travas fora do Laya (erro dele não pode parar o Alfred):
  - Laya sem resposta, ou resposta que não veio do checkpoint "porta" → a chamada entra, como antes da porta
  - placa livre e Pedro longe → entra sem perguntar
  - ninguém espera mais que TETO_S

logs/porta/
  rodando/<pid>-<id>.json   uma chamada ao Ollama em andamento; só o processo dono grava e apaga
  dono                      a data do arquivo é a última vez que Pedro falou com o Alfred

Os imports do projeto ficam dentro das funções: o gerador e o avaliador do treino carregam este
arquivo sozinho, no venv do Laya.
"""

import contextvars
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

PERGUNTA = {"porta": {
    "type": "choice",
    "instructions": "Should the job asking for the GPU start now?",
    "criteria": {
        "open": "the owner is waiting for this answer, or it will not get in the way of the owner",
        "wait": "background work that would slow down the owner or swap the loaded model",
    },
}}

# há quanto tempo Pedro falou com o Alfred (conversa, voz, WhatsApp dele)
DONO_SEGUNDOS = "talked to Alfred seconds ago"
DONO_MINUTOS = "talked to Alfred minutes ago"
DONO_LONGE = "away for a long time"

PLACA_LIVRE = "nothing"
ORIGEM_PADRAO = "background routine without a name"   # quem não passou por um @origem

MODELO_LAYA = "porta"    # nome do checkpoint no laya-serve
TETO_S = 300             # espera máxima; depois disso a chamada entra mesmo com o Laya dizendo wait
PASSO_S = 3              # de quanto em quanto quem espera pergunta de novo
SEGUNDOS_S = 90          # Pedro falou há menos que isto: "segundos" (conversa em curso)
MINUTOS_S = 15 * 60      # há menos que isto: "minutos"; mais: "longe"

_ORIGEM: contextvars.ContextVar = contextvars.ContextVar("alfred_porta_origem", default=(ORIGEM_PADRAO, False))


def montar_estado(pede: str, rodando: str, dono: str, modelo_carregado: bool) -> dict:
    """
    O estado que vai ao Laya. `pede` e `rodando` são descrições curtas em inglês de quem pede a
    placa e de quem a ocupa agora (PLACA_LIVRE se ninguém); `dono` é um dos DONO_*.
    """
    return {
        "asking_for_gpu": pede,
        "gpu_running_now": rodando or PLACA_LIVRE,
        "owner": dono,
        "model": "already loaded" if modelo_carregado else "another model has to be loaded",
    }


def pasta() -> Path:
    from _shared.lib_alfred import PROJECT_ROOT
    return Path(os.getenv("ALFRED_PORTA_DIR") or PROJECT_ROOT / "logs" / "porta")


# ── quem começa um trabalho diz quem é ────────────────────────

def _dono_falou() -> None:
    try:
        pasta().mkdir(parents=True, exist_ok=True)
        (pasta() / "dono").touch()
    except OSError:
        pass


@contextmanager
def origem(descricao: str, dono: bool = False):
    """
    As chamadas ao Ollama feitas dentro do bloco (ou da função decorada) pedem a porta como `descricao`.
    `dono`: é Pedro esperando a resposta; marca a hora em que ele falou, na entrada e na saída.
    """
    if dono:
        _dono_falou()
    marca = _ORIGEM.set((descricao, dono))
    try:
        yield
    finally:
        _ORIGEM.reset(marca)
        if dono:
            _dono_falou()


# ── o que a porta vê ──────────────────────────────────────────

def _vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return True


def _rodando() -> str:
    """Descrição de quem ocupa a placa há mais tempo (PLACA_LIVRE se ninguém). Apaga o que sobrou de processo morto."""
    mais_antigo = None
    for arq in (pasta() / "rodando").glob("*.json"):
        try:
            if not _vivo(int(arq.name.split("-")[0])):
                arq.unlink(missing_ok=True)
                continue
            dados = json.loads(arq.read_text(encoding="utf-8"))
            if mais_antigo is None or dados["inicio"] < mais_antigo["inicio"]:
                mais_antigo = dados
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return mais_antigo["origem"] if mais_antigo else PLACA_LIVRE


def _dono() -> str:
    try:
        ha = time.time() - (pasta() / "dono").stat().st_mtime
    except OSError:
        return DONO_LONGE
    return DONO_SEGUNDOS if ha < SEGUNDOS_S else DONO_MINUTOS if ha < MINUTOS_S else DONO_LONGE


def _modelo_carregado(modelo: str, num_ctx: Optional[int]) -> bool:
    """O Ollama já tem `modelo` na memória com este num_ctx? Sem resposta do /api/ps: True (não segura por isso)."""
    import httpx
    from _shared.lib_alfred import DEFAULT_OLLAMA_URL
    try:
        url = os.getenv("OLLAMA_BASE_URL", "").strip() or DEFAULT_OLLAMA_URL
        carregados = httpx.get(f"{url}/api/ps", timeout=2).json().get("models") or []
    except Exception:
        return True
    return any(m.get("name") in (modelo, f"{modelo}:latest")
               and (not num_ctx or m.get("context_length") == num_ctx) for m in carregados)


# ── a decisão ─────────────────────────────────────────────────

def decidir(pede: str, rodando: str, dono: str, modelo_carregado: bool) -> Optional[str]:
    """Pergunta ao Laya: "open", "wait" ou None (sem resposta, ou quem respondeu não foi o checkpoint "porta")."""
    from _shared import lib_laya
    r = lib_laya.decidir(montar_estado(pede, rodando, dono, modelo_carregado), PERGUNTA, modelo=MODELO_LAYA)
    return ((r or {}).get("porta") or {}).get("choice")


def _esperar(pede: str, modelo: str, num_ctx: Optional[int]) -> None:
    from _shared.lib_alfred import log
    inicio = time.monotonic()
    while True:
        rodando, dono = _rodando(), _dono()
        if rodando == PLACA_LIVRE and dono == DONO_LONGE:
            break
        if decidir(pede, rodando, dono, _modelo_carregado(modelo, num_ctx)) != "wait":
            break
        esperou = time.monotonic() - inicio
        if esperou >= TETO_S:
            log(f"[porta] {pede}: passou do teto de {TETO_S} s, entra assim mesmo")
            return
        if esperou < PASSO_S:
            log(f"[porta] {pede}: esperando (placa: {rodando}; dono: {dono})")
        time.sleep(PASSO_S)
    if time.monotonic() - inicio >= PASSO_S:
        log(f"[porta] {pede}: entrou depois de {time.monotonic() - inicio:.0f} s")


@contextmanager
def passagem(modelo: str, num_ctx: Optional[int] = None):
    """
    Espera a porta abrir e marca a chamada como rodando enquanto o bloco dura. Nunca levanta exceção
    própria: falhou em perguntar ou em marcar, a chamada ao Ollama segue como se a porta não existisse.
    """
    pede, dono = _ORIGEM.get()
    arq = None
    try:
        _esperar(pede, modelo, num_ctx)
        arq = pasta() / "rodando" / f"{os.getpid()}-{secrets.token_hex(4)}.json"
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text(json.dumps({"origem": pede, "inicio": time.time()}), encoding="utf-8")
    except Exception as e:
        from _shared.lib_alfred import log
        log(f"[porta] não funcionou, a chamada segue: {e}")
    try:
        yield
    finally:
        try:
            if arq:
                arq.unlink(missing_ok=True)
            if dono:
                _dono_falou()
        except OSError:
            pass
