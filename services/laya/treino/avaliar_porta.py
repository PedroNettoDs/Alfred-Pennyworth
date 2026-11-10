#!/usr/bin/env python3
"""
Avalia um checkpoint do Laya na porta da GPU, sobre porta_teste.jsonl (origens que o treino não viu).
Separa os dois erros, que custam diferente: abrir errado atrasa Pedro; fechar errado só atrasa a rotina.

    venv/bin/python treino/avaliar_porta.py treino/dados/modelo-porta
"""
import json
import sys
from pathlib import Path

import laya

DADOS = Path(__file__).resolve().parent / "dados"
itens = [json.loads(l) for l in (DADOS / "porta_teste.jsonl").open()]

agente = laya.load(sys.argv[1], device="cpu")
res = []
for i in itens:
    a = agente.predict(i["state"], i["questions"])["answers"]["porta"]
    res.append((a["choice"], i["expected"]["porta"], a.get("answer_confidence", 0), i["state"]))

for lim in (0.0, 0.6, 0.7, 0.8, 0.9):
    s = [(c, e) for c, e, conf, _ in res if conf >= lim]
    if not s:
        continue
    abriu = sum(c == "open" and e == "wait" for c, e in s)
    fechou = sum(c == "wait" and e == "open" for c, e in s)
    print(f"limiar {lim:.1f}: decide {len(s)}/{len(res)} ({len(s) / len(res):.0%}), "
          f"acerto {(len(s) - abriu - fechou) / len(s):.0%}, abriu errado {abriu}, fechou errado {fechou}")

print("\nerros (sem limiar):")
for c, e, conf, estado in res:
    if c != e:
        print(f"  disse {c} ({conf:.2f}), era {e}: {json.dumps(estado, ensure_ascii=False)}")
