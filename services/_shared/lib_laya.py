"""
Alfred — cliente do Laya (decisões tipadas locais, sem gerar texto).

O Laya (services/laya, `laya-serve` em 127.0.0.1:PORT_LAYA) responde perguntas tipadas sobre um
estado (texto, e-mail, dict) numa só passada do encoder:

    choice  escolhe um rótulo           → {"choice": "trabalho", "confidence": 0.91, ...}
    score   nota numa escala ordinal    → {"score": 1.7, "confidence": ..., ...}
    noul    probabilidade de "sim"      → {"noul": 0.93, ...}

Regra do projeto: o Laya decide, o LLM escreve. Toda função aqui devolve None quando o serviço
está fora do ar, dá erro ou a confiança fica abaixo do limiar — o chamador então segue pelo
caminho LLM de sempre (ollama_generate_json). Nunca levanta exceção.

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from _shared.lib_laya import sim_nao, escolher
"""

import os
from typing import Optional, Union

import httpx

from _shared.lib_alfred import log

Estado = Union[str, dict, list]

TIMEOUT = 10
CONFIANCA_MINIMA = 0.7


def _url() -> str:
    return os.getenv("LAYA_URL", f"http://127.0.0.1:{os.getenv('PORT_LAYA', '7073')}")


def _confianca_minima() -> float:
    try:
        return float(os.getenv("LAYA_CONFIANCA_MINIMA", CONFIANCA_MINIMA))
    except ValueError:
        return CONFIANCA_MINIMA


def disponivel() -> bool:
    """True se o laya-serve responde ao /health."""
    try:
        with httpx.Client(timeout=2) as client:
            return client.get(f"{_url()}/health").status_code == 200
    except Exception:
        return False


def decidir(estado: Estado, perguntas: dict, timeout: int = TIMEOUT) -> Optional[dict]:
    """
    Chamada crua: POST /v1/systemone. Devolve o dict `answers` ({id: resposta}) ou None.
    `perguntas` segue o formato do Laya: {id: {"type": ..., "instructions": ..., "criteria": ...}}.
    """
    if os.getenv("LAYA_DESLIGADO", "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{_url()}/v1/systemone", json={"state": estado, "questions": perguntas})
            resp.raise_for_status()
            return resp.json().get("answers") or None
    except Exception as e:
        log(f"[laya] Erro: {e}")
        return None


def _confiavel(resp: Optional[dict]) -> bool:
    # answer_confidence = probabilidade da resposta escolhida. `confidence` é entropia normalizada
    # sobre todas as opções e fica bem mais baixa (0.5 em acertos claros) — ruim para um limiar.
    if not resp:
        return False
    return resp.get("answer_confidence", resp.get("confidence", 1.0)) >= _confianca_minima()


def escolher(estado: Estado, instrucao: str, opcoes: dict) -> Optional[str]:
    """
    Escolhe um rótulo. `opcoes` = {rótulo: descrição}. None se fora do ar ou confiança baixa.
    """
    r = decidir(estado, {"q": {"type": "choice", "instructions": instrucao, "criteria": opcoes}})
    resp = (r or {}).get("q")
    return resp.get("choice") if _confiavel(resp) else None


def pontuar(estado: Estado, instrucao: str, escala: list[str]) -> Optional[float]:
    """
    Nota esperada na escala ordinal (0 = primeiro item de `escala`). None se fora do ar ou
    confiança baixa.
    """
    r = decidir(estado, {"q": {"type": "score", "instructions": instrucao, "criteria": escala}})
    resp = (r or {}).get("q")
    return resp.get("score") if _confiavel(resp) else None


def sim_nao(estado: Estado, afirmacao: str) -> Optional[float]:
    """
    Probabilidade (0–1) de `afirmacao` ser verdadeira sobre o estado. None se fora do ar.
    Sem limiar de confiança: a própria probabilidade já é a medida — o chamador decide o corte.
    """
    r = decidir(estado, {"q": {"type": "noul", "instructions": afirmacao}})
    resp = (r or {}).get("q")
    return resp.get("noul") if resp else None
