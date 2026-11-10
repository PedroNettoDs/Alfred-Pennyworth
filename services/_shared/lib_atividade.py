#!/usr/bin/env python3
"""
lib_atividade — o que o Alfred está fazendo agora, a fila do que vem depois e os avisos, publicados em
arquivos locais para quem quiser mostrar (o Pestinha desenha isso na tela — services/inbox/PESTINHA.md).

logs/atividade/
  agora/<pid>-<id>.json    uma atividade em andamento; só o processo dono grava e apaga
  eventos/<ms>-<id>.json   aviso pontual (pendência nova)
  pedidos/<ms>-<id>.json   pedido de fora (pet, CLI): enfileirar | frente | cancelar
  fila.json                fila pedida pelo usuário
  estado.json              retrato para quem lê: agora, fila, agendadas, catálogo, eventos

Só o Despachante (uma thread da API) grava fila.json e estado.json; os outros processos só deixam arquivos
novos nas pastas. Por isso não há trava. A gravação é temporário + os.replace, sem fsync: o estado é
descartável e é regravado a cada poucos segundos.

Quem instrumenta (atividade, passo, evento) nunca recebe exceção daqui: falhar ao publicar não pode
derrubar o trabalho.
"""

import contextvars
import json
import os
import secrets
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .lib_alfred import PROJECT_ROOT, log

VERSAO = 1
TICK_S = 2               # de quanto em quanto o Despachante olha as pastas e o systemd
BATIMENTO_S = 5          # estado.json é regravado ao menos a cada tanto: quem lê sabe que o Alfred está vivo
SISTEMA_A_CADA_S = 30    # units instaladas e próximos horários dos timers
EVENTO_VALE_S = 120      # um aviso fica no estado por este tempo e depois é apagado
MAX_FILA = 20

# unit do systemd → (tipo, rótulo): as rotinas que podem entrar na fila e o nome que aparece para cada uma.
CATALOGO = {
    "alfred-inbox.service": ("email", "Ler e classificar os e-mails"),
    "alfred-briefing-matinal.service": ("briefing_matinal", "Briefing da manhã"),
    "alfred-briefing-vespertino.service": ("briefing_vespertino", "Briefing da tarde"),
    "alfred-rag.service": ("rag", "Indexar os cofres"),
    "alfred-vigia.service": ("vigia", "Ronda dos serviços"),
    "alfred-backup.service": ("backup", "Backup"),
    "alfred-semanal.service": ("podcast", "Agenda da semana e podcast (se for dia)"),
    "alfred-conversas.service": ("conversas", "Resumir as conversas paradas"),
}


def pasta() -> Path:
    return Path(os.getenv("ALFRED_ATIVIDADE_DIR") or PROJECT_ROOT / "logs" / "atividade")


def _gravar(caminho: Path, dados: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(f".{caminho.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, caminho)


def _ler(caminho: Path) -> Optional[dict]:
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dados if isinstance(dados, dict) else None


def _novo(sub: str, dados: dict) -> str:
    """Deixa um arquivo novo em `sub`; o nome começa pelo horário, então a ordem alfabética é a de chegada."""
    nome = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    _gravar(pasta() / sub / f"{nome}.json", dados)
    return nome


def _tentar(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as e:
        log(f"[atividade] não publicou: {e}")


# ── quem trabalha avisa ───────────────────────────────────────

_ATUAL: contextvars.ContextVar = contextvars.ContextVar("alfred_atividade", default=None)


@contextmanager
def atividade(tipo: str, rotulo: str):
    """Marca uma atividade em andamento enquanto o bloco (ou a função decorada) roda."""
    dados = {"id": secrets.token_hex(4), "tipo": tipo, "rotulo": rotulo, "passo": "", "feito": None,
             "total": None, "inicio": time.time(), "pid": os.getpid()}
    caminho = pasta() / "agora" / f"{os.getpid()}-{dados['id']}.json"
    _tentar(_gravar, caminho, dados)
    marca = _ATUAL.set((caminho, dados))
    try:
        yield
    finally:
        _ATUAL.reset(marca)
        _tentar(caminho.unlink, missing_ok=True)


def passo(texto: str, feito: Optional[int] = None, total: Optional[int] = None) -> None:
    """Em que parte a atividade atual está ("Classificando e-mails", 3, 12). Fora de uma atividade, não faz nada."""
    atual = _ATUAL.get()
    if atual is None:
        return
    caminho, dados = atual
    dados.update(passo=texto, feito=feito, total=total)
    _tentar(_gravar, caminho, dados)


def evento(tipo: str, texto: str) -> None:
    """Aviso pontual para quem acompanha (ex.: "pendencia", título)."""
    _tentar(_novo, "eventos", {"tipo": tipo, "texto": str(texto)[:200], "quando": time.time()})


def pedir(acao: str, **campos) -> str:
    """Pedido ao Despachante: enfileirar (tipo, frente) | frente (id) | cancelar (id)."""
    return _novo("pedidos", {"acao": acao, **campos})


def estado() -> dict:
    """O último retrato publicado ({} se o Despachante nunca rodou)."""
    return _ler(pasta() / "estado.json") or {}


# ── despachante ───────────────────────────────────────────────

def _systemctl(*args: str) -> list:
    try:
        r = subprocess.run(["systemctl", "--user", *args, "--output=json", "--no-pager"],
                           capture_output=True, text=True, timeout=5)
        dados = json.loads(r.stdout or "[]")
    except Exception:
        return []
    return dados if isinstance(dados, list) else []


class Sistema:
    """O que o Despachante pergunta e pede ao systemd. Os testes trocam por um falso."""

    def instaladas(self) -> set:
        return {u.get("unit_file") for u in _systemctl("list-unit-files", "alfred-*.service")}

    def rodando(self) -> set:
        # rotina oneshot em execução fica "activating" até o ExecStart terminar
        return {u.get("unit") for u in _systemctl("list-units", "alfred-*.service", "--state=activating")}

    def timers(self) -> dict:
        """unit .service → horário (epoch) da próxima execução agendada."""
        out = {}
        for t in _systemctl("list-timers", "--all"):
            prox = t.get("next")
            if isinstance(prox, (int, float)) and prox > 0:
                out[str(t.get("activates"))] = prox / 1e6
        return out

    def iniciar(self, unit: str) -> None:
        """Roda a rotina e só volta quando ela termina (as units são oneshot)."""
        subprocess.run(["systemctl", "--user", "start", unit], capture_output=True)


def _vivo(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OverflowError):
        return False
    return True


class Despachante:
    """Junta o que está rodando, atende os pedidos, roda a fila (uma rotina por vez) e publica estado.json."""

    def __init__(self, sistema: Optional[Sistema] = None):
        self.sistema = sistema or Sistema()
        self.base = pasta()
        self.fila: list = [i for i in (_ler(self.base / "fila.json") or {}).get("fila", [])
                           if isinstance(i, dict) and i.get("id") and i.get("tipo")]
        self.catalogo: dict = {}       # tipo → (unit, rótulo), só das units instaladas
        self.agendadas: list = []
        self._sistema_em: Optional[float] = None
        self._em_curso: Optional[tuple] = None    # (unit, thread) da rotina que a fila mandou rodar
        self._publicado: Optional[dict] = None
        self._publicado_em = 0.0

    def tick(self, agora: Optional[float] = None) -> dict:
        agora = time.time() if agora is None else agora
        fila_antes = list(self.fila)
        if self._sistema_em is None or agora - self._sistema_em >= SISTEMA_A_CADA_S:
            self._ler_sistema(agora)
        self._pedidos()

        units = {unit for unit, _rotulo in self.catalogo.values()}
        rodando = self.sistema.rodando() & units
        if self._em_curso and not self._em_curso[1].is_alive():
            self._em_curso = None
        if self._em_curso:
            rodando.add(self._em_curso[0])    # entre o start e o systemd mostrar "activating"
        if self.fila and not rodando:
            unit = self.catalogo[self.fila.pop(0)["tipo"]][0]
            t = threading.Thread(target=self.sistema.iniciar, args=(unit,), name="alfred-fila", daemon=True)
            t.start()
            self._em_curso = (unit, t)
            rodando.add(unit)
        if self.fila != fila_antes:
            _gravar(self.base / "fila.json", {"fila": self.fila})

        estado = {
            "agora": self._agora(rodando),
            "fila": list(self.fila),
            "agendadas": self.agendadas,
            "catalogo": [{"tipo": tipo, "rotulo": rotulo} for tipo, (_unit, rotulo) in self.catalogo.items()],
            "eventos": self._eventos(agora),
        }
        if estado != self._publicado or agora - self._publicado_em >= BATIMENTO_S:
            _gravar(self.base / "estado.json", {"versao": VERSAO, "atualizado": agora, **estado})
            self._publicado, self._publicado_em = estado, agora
        return estado

    def _ler_sistema(self, agora: float) -> None:
        instaladas = self.sistema.instaladas()
        self.catalogo = {tipo: (unit, rotulo) for unit, (tipo, rotulo) in CATALOGO.items() if unit in instaladas}
        por_unit = {unit: (tipo, rotulo) for tipo, (unit, rotulo) in self.catalogo.items()}
        self.agendadas = sorted(
            ({"tipo": por_unit[unit][0], "rotulo": por_unit[unit][1], "quando": quando}
             for unit, quando in self.sistema.timers().items() if unit in por_unit),
            key=lambda a: a["quando"])
        self.fila = [i for i in self.fila if i["tipo"] in self.catalogo]
        self._sistema_em = agora

    def _pedidos(self) -> None:
        for f in sorted((self.base / "pedidos").glob("*.json")):
            p = _ler(f) or {}
            f.unlink(missing_ok=True)
            acao, tipo, alvo = p.get("acao"), p.get("tipo"), p.get("id")
            if acao == "enfileirar" and tipo in self.catalogo and len(self.fila) < MAX_FILA:
                item = {"id": f.stem, "tipo": tipo, "rotulo": self.catalogo[tipo][1]}
                self.fila.insert(0 if p.get("frente") else len(self.fila), item)
            elif acao in ("cancelar", "frente"):
                achados = [i for i in self.fila if i["id"] == alvo]
                self.fila = [i for i in self.fila if i["id"] != alvo]
                if acao == "frente":
                    self.fila = achados + self.fila

    def _agora(self, rodando: set) -> list:
        vivos = []
        for f in sorted((self.base / "agora").glob("*.json")):
            d = _ler(f)
            if not d or not _vivo(d.get("pid")):
                f.unlink(missing_ok=True)     # o processo morreu sem apagar o próprio arquivo
                continue
            vivos.append({k: d.get(k) for k in ("id", "tipo", "rotulo", "passo", "feito", "total", "inicio")})
        tipos = {a["tipo"] for a in vivos}
        for tipo, (unit, rotulo) in self.catalogo.items():
            if unit in rodando and tipo not in tipos:   # rotina sem instrumentação: só o nome
                vivos.append({"id": unit, "tipo": tipo, "rotulo": rotulo, "passo": "", "feito": None,
                              "total": None, "inicio": None})
        return vivos

    def _eventos(self, agora: float) -> list:
        vivos = []
        for f in sorted((self.base / "eventos").glob("*.json")):
            d = _ler(f) or {}
            quando = d.get("quando")
            if not isinstance(quando, (int, float)) or agora - quando > EVENTO_VALE_S:
                f.unlink(missing_ok=True)
                continue
            vivos.append({"id": f.stem, "tipo": d.get("tipo"), "texto": d.get("texto"), "quando": quando})
        return vivos


def laco(parar: threading.Event) -> None:
    """Thread da API: um Despachante até a API parar."""
    despachante = Despachante()
    while not parar.wait(TICK_S):
        try:
            despachante.tick()
        except Exception as e:
            log(f"[atividade] despachante: erro inesperado ({e})")
