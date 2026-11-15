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

Descarga: OLLAMA_DESCARREGAR_S segundos (padrão 10) depois da última chamada sem ninguém na placa, os modelos
saem da memória do Ollama (keep_alive 0). O Ollama só fica com modelo carregado enquanto há trabalho.

logs/porta/
  rodando/<pid>-<id>.json        uma chamada ao Ollama em andamento; só o processo dono grava e apaga
  dono                           a data do arquivo é a última vez que Pedro falou com o Alfred
  vagas/<nome>/<pid>-<id>.json   quem está dentro ou na fila de uma vaga(nome)

Sinais e semáforos para o orquestrador (lib_orquestrador), sem o Laya: carregados() e vram_livre_mb()
dizem o que está na placa; vaga(nome, limite) deixa só `limite` blocos de um tipo rodarem juntos.

Os imports do projeto ficam dentro das funções: o gerador e o avaliador do treino carregam este
arquivo sozinho, no venv do Laya.
"""

import atexit
import contextvars
import json
import os
import secrets
import threading
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


def carregados() -> Optional[list[dict]]:
    """Modelos na memória do Ollama (/api/ps): [{"name", "size_vram", "context_length", ...}]. None = sem resposta."""
    import httpx
    from _shared.lib_alfred import DEFAULT_OLLAMA_URL
    try:
        url = os.getenv("OLLAMA_BASE_URL", "").strip() or DEFAULT_OLLAMA_URL
        return httpx.get(f"{url}/api/ps", timeout=2).json().get("models") or []
    except Exception:
        return None


def _modelo_carregado(modelo: str, num_ctx: Optional[int]) -> bool:
    """O Ollama já tem `modelo` na memória com este num_ctx? Sem resposta do /api/ps: True (não segura por isso)."""
    lista = carregados()
    if lista is None:
        return True
    return any(m.get("name") in (modelo, f"{modelo}:latest")
               and (not num_ctx or m.get("context_length") == num_ctx) for m in lista)


def vram_livre_mb() -> Optional[int]:
    """VRAM livre da primeira placa, em MiB (nvidia-smi). None = sem placa ou sem resposta."""
    import subprocess
    try:
        saida = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=3, check=True).stdout
        return int(saida.split()[0])
    except Exception:
        return None


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
        _agendar_descarga()


# ── descarga: o Ollama só fica com modelo na memória enquanto há trabalho ─────

_descarga = None            # threading.Timer da descarga marcada por este processo
_descarga_lock = threading.Lock()


def descarga_s() -> Optional[float]:
    """Segundos sem chamada ao Ollama até os modelos saírem da memória (OLLAMA_DESCARREGAR_S, padrão 10).
    None = desligado (valor negativo ou inválido): o modelo fica pelo keep_alive do próprio Ollama."""
    try:
        s = float(os.getenv("OLLAMA_DESCARREGAR_S", "").strip() or 10)
    except ValueError:
        return None
    return s if s >= 0 else None


def descarregar() -> list[str]:
    """Tira da memória do Ollama todos os modelos carregados, se nenhuma chamada está em andamento
    (em qualquer processo). → nomes descarregados. Nunca levanta exceção."""
    try:
        import httpx
        from _shared.lib_alfred import DEFAULT_OLLAMA_URL, log
        if _rodando() != PLACA_LIVRE:
            return []
        url = os.getenv("OLLAMA_BASE_URL", "").strip() or DEFAULT_OLLAMA_URL
        nomes = [m["name"] for m in carregados() or [] if m.get("name")]
        for nome in nomes:
            httpx.post(f"{url}/api/generate", json={"model": nome, "keep_alive": 0}, timeout=20)
        if nomes:
            log(f"[porta] Ollama descarregado: {', '.join(nomes)}")
        return nomes
    except Exception:
        return []


def _agendar_descarga() -> None:
    """Marca (ou remarca) a descarga para daqui a descarga_s(): a espera cobre o intervalo entre as chamadas de
    uma mesma tarefa. Processo que termina antes disso descarrega na saída."""
    global _descarga
    espera = descarga_s()
    if espera is None:
        return
    with _descarga_lock:
        if _descarga is None:
            atexit.register(_descarga_na_saida)
        else:
            _descarga.cancel()
        _descarga = threading.Timer(espera, descarregar)
        _descarga.daemon = True
        _descarga.start()


def _descarga_na_saida() -> None:
    if _descarga is not None and _descarga.is_alive():
        _descarga.cancel()
        descarregar()


# ── vagas: no máximo N de uma coisa ao mesmo tempo ────────────

def _posicao(arq: Path) -> int:
    """Quantos pedidos vivos chegaram antes de `arq` na mesma vaga. Apaga o que sobrou de processo morto."""
    fila = []
    for outro in arq.parent.glob("*.json"):
        try:
            if not _vivo(int(outro.name.split("-")[0])):
                outro.unlink(missing_ok=True)
                continue
            fila.append((json.loads(outro.read_text(encoding="utf-8"))["inicio"], outro.name))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    fila.sort()
    nomes = [nome for _, nome in fila]
    return nomes.index(arq.name) if arq.name in nomes else 0


@contextmanager
def vaga(nome: str, limite: int = 1, teto_s: float = TETO_S):
    """
    Semáforo de contagem entre processos: no máximo `limite` blocos `nome` rodam ao mesmo tempo (ex.:
    vaga("script"), um script por vez), por ordem de chegada. Não pergunta ao Laya. Mesmas travas da
    passagem: ninguém espera mais que `teto_s`, e erro aqui não impede o bloco de rodar.
    """
    arq = None
    try:
        arq = pasta() / "vagas" / nome / f"{os.getpid()}-{secrets.token_hex(4)}.json"
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text(json.dumps({"origem": _ORIGEM.get()[0], "inicio": time.time()}), encoding="utf-8")
        inicio = time.monotonic()
        while _posicao(arq) >= limite:
            if time.monotonic() - inicio >= teto_s:
                from _shared.lib_alfred import log
                log(f"[porta] vaga {nome}: passou do teto de {teto_s:.0f} s, entra assim mesmo")
                break
            time.sleep(PASSO_S)
    except Exception as e:
        from _shared.lib_alfred import log
        log(f"[porta] vaga {nome} não funcionou, o bloco segue: {e}")
    try:
        yield
    finally:
        try:
            if arq:
                arq.unlink(missing_ok=True)
        except OSError:
            pass
