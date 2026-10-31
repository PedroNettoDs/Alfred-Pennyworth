#!/usr/bin/env python3
"""
Avalia um checkpoint treinado do Laya no teste, por limiar de confiança (modo cascata: abaixo do
limiar o e-mail vai para o LLM). Itens do teste que também estão no treino são descartados.

    venv/bin/python treino/avaliar.py treino/dados/modelo-email
"""
import json
import sys
from pathlib import Path

import laya

DADOS = Path(__file__).resolve().parent / "dados"
treino = {json.loads(l)["state"] for l in (DADOS / "treino.jsonl").open()}
itens = [json.loads(l) for l in (DADOS / "teste.jsonl").open()]
itens = [i for i in itens if i["state"] not in treino]

agente = laya.load(sys.argv[1])
res = []
for i in itens:
    a = agente.predict(i["state"], i["questions"])["answers"]["categoria"]
    res.append((a["choice"] == i["expected"]["categoria"], a.get("answer_confidence", a.get("confidence", 0))))

print(f"teste sem sobreposição: {len(res)} e-mails, acerto geral {sum(c for c, _ in res) / len(res):.0%}")
for lim in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
    s = [c for c, conf in res if conf >= lim]
    if s:
        print(f"limiar {lim:.2f}: Laya decide {len(s)}/{len(res)} ({len(s) / len(res):.0%}), acerto {sum(s) / len(s):.0%}")
