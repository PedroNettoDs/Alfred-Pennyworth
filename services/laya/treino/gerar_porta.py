#!/usr/bin/env python3
"""
Gera o conjunto de treino do Laya para a porta da GPU: estados da fila rotulados pela regra abaixo.

    venv/bin/python treino/gerar_porta.py

Saída em treino/dados/ (formato do laya-train):
    porta_treino.jsonl   origens de TREINO
    porta_teste.jsonl    só origens de TESTE, nunca vistas no treino — mede se o Laya decide certo
                         para uma função nova do Alfred sem ninguém editar uma tabela

A regra (rótulo de referência):
    quem pede é interativo (Pedro esperando)                  → open
    há trabalho interativo rodando na placa                   → wait
    Pedro falou com o Alfred há segundos (conversa em curso)  → wait
    precisa trocar o modelo carregado e Pedro falou há minutos,
        ou há outro trabalho de fundo rodando                 → wait
    resto                                                     → open
"""

import itertools
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
import lib_porta as porta  # noqa: E402

DADOS = Path(__file__).resolve().parent / "dados"

# descrição → interativo? As de TESTE ficam fora do treino.
TREINO = {
    "live voice conversation with the owner": True,
    "chat turn typed by the owner in the panel": True,
    "whatsapp message the owner sent to Alfred": True,
    "transcribing a voice note the owner just recorded": True,
    "the owner asked Alfred to draft an e-mail and is watching": True,
    "tool call inside the conversation with the owner": True,
    "rewriting a text the owner selected in the editor": True,
    "question the owner asked about a document open on screen": True,
    "the owner pressed the button to summarize this thread now": True,
    "speaking the answer aloud to the owner": True,
    "owner asked for the GPU temperature by voice": True,
    "the owner clicked a button in the panel and is waiting": True,   # toda ação de Pedro no Painel
    "scheduled e-mail reading and classification": False,
    "morning technology news briefing": False,
    "indexing the vaults for local search": False,
    "weekly agenda and podcast generation": False,
    "deep research job with a large model": False,
    "summarizing conversations that went quiet": False,
    "reply to a whatsapp message from a contact": False,
    "extracting calendar commitments from old messages": False,
    "detecting promises in yesterday's e-mails": False,
    "transcribing an audio a contact sent on whatsapp": False,
    "titles for notes saved in the vault": False,
    "clustering news articles for the journal": False,
    "categorizing e-mails for the cleanup routine": False,
    "service health round every ten minutes": False,
    "writing the research notes for a web page": False,
    "nightly memory consolidation": False,
    "rolling summary of a whatsapp chat with a contact": False,
    "background routine without a name": False,   # lib_porta.ORIGEM_PADRAO: quem não se identificou
}
TESTE = {
    "the owner is dictating a message and waiting for the text": True,
    "follow-up question typed by the owner in the chat": True,
    "the owner asked Alfred on whatsapp what is on the agenda today": True,
    "correcting a paragraph while the owner looks at the screen": True,
    "afternoon market briefing": False,
    "building the person dossier in the background": False,
    "classifying whether a contact's message is serious": False,
    "embedding refresh of the archive after login": False,
    "long synthesis of twenty sources for a research report": False,
    "weekly backup report": False,
}
DONOS = (porta.DONO_SEGUNDOS, porta.DONO_MINUTOS, porta.DONO_LONGE)


def regra(interativo: bool, rodando_interativo, dono: str, modelo_carregado: bool) -> str:
    """`rodando_interativo`: None = placa livre, True/False = tipo de quem ocupa a placa."""
    if interativo:
        return "open"
    if rodando_interativo or dono == porta.DONO_SEGUNDOS:
        return "wait"
    if not modelo_carregado and (dono == porta.DONO_MINUTOS or rodando_interativo is False):
        return "wait"
    return "open"


def itens(pedem: dict, rodam: dict, por_origem: int, rng: random.Random) -> list[dict]:
    """Para cada origem que pede: todas as combinações de dono × modelo, com `por_origem` ocupantes da placa."""
    saida = []
    for pede, interativo in pedem.items():
        outros = [o for o in rodam if o != pede]
        ocupantes = [None] + rng.sample(outros, por_origem)
        for rodando, dono, modelo in itertools.product(ocupantes, DONOS, (True, False)):
            tipo = None if rodando is None else rodam[rodando]
            if tipo and dono != porta.DONO_SEGUNDOS:
                continue   # não existe: com trabalho interativo na placa, Pedro falou há segundos
            saida.append({
                "state": porta.montar_estado(pede, rodando, dono, modelo),
                "questions": porta.PERGUNTA,
                "expected": {"porta": regra(interativo, tipo, dono, modelo)},
            })
    rng.shuffle(saida)
    return saida


def main():
    DADOS.mkdir(parents=True, exist_ok=True)
    rng = random.Random(0)
    conjuntos = {
        "porta_treino.jsonl": itens(TREINO, TREINO, 5, rng),
        "porta_teste.jsonl": itens(TESTE, {**TREINO, **TESTE}, 5, rng),
    }
    for nome, linhas in conjuntos.items():
        (DADOS / nome).write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in linhas))
        abre = sum(l["expected"]["porta"] == "open" for l in linhas)
        print(f"{nome}: {len(linhas)} itens ({abre} open, {len(linhas) - abre} wait)")


if __name__ == "__main__":
    main()
