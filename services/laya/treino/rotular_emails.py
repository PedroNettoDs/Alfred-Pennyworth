#!/usr/bin/env python3
"""
Rotula e-mails do Thunderbird com o classificar() atual (LLM) e grava o conjunto de treino do Laya.

    cd services/inbox
    MODEL_CHAT=gemma4:12b OLLAMA_THINK=false OLLAMA_NUM_CTX=8192 \
        venv/bin/python ../laya/treino/rotular_emails.py [max_emails] [janela_dias]

Saída em services/laya/treino/dados/ (fora do git — dados pessoais):
    emails.jsonl         um e-mail rotulado por linha (retomável: e-mails já rotulados são pulados)
    treino.jsonl/teste.jsonl  divisão fixa 90/10 por hash do Message-ID, no formato do laya-train

Mostra progresso e ETA a cada e-mail.
"""

import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "..")
import core  # noqa: E402

DADOS = Path(__file__).resolve().parent / "dados"
OPCOES = {
    "noticia": "newsletter or digest with several news items, articles or analyses",
    "evento": "invitation to one specific event with a date: meetup, webinar, live class, meeting, interview, open application with deadline",
    "contato": "a real person contacting Pedro professionally: recruiter, client, job or partnership offer, meeting request",
    "transacional": "receipt, invoice, purchase or signup confirmation, verification code, security alert, automatic platform notification",
    "promocao": "marketing, offers, coupons, discounts, sales with urgency",
    "pessoal": "friends, family, private matters",
    "outro": "none of the above",
}
PERGUNTA = {"categoria": {"type": "choice", "instructions": "Which category fits this e-mail?", "criteria": OPCOES}}
CONFIANCA_MINIMA = 0.8   # rótulo do LLM com confiança menor não entra no treino


def estado(m: dict) -> str:
    return f"From: {m['from_name']} <{m['from_addr']}>\nSubject: {m['subject']}\n\n{core._corpo_prompt(m, 2000)}"


def chave(m: dict) -> str:
    return m.get("message_id") or m.get("id") or f"{m['from_addr']}|{m['subject']}|{m.get('date_iso')}"


def main():
    max_emails = int(sys.argv[1]) if len(sys.argv) > 1 else 800
    janela_dias = int(sys.argv[2]) if len(sys.argv) > 2 else 365
    DADOS.mkdir(parents=True, exist_ok=True)
    arq = DADOS / "emails.jsonl"
    feitos = {json.loads(l)["chave"] for l in arq.open()} if arq.exists() else set()

    _, cfg = core.carregar_cfg()
    cfg = {**cfg, "cache_classificacao": False}
    msgs = [m for m in core.coletar(cfg, janela_horas=24 * janela_dias, max_emails=max_emails)
            if chave(m) not in feitos]
    total = len(msgs)
    print(f"{len(feitos)} já rotulados; {total} a rotular", flush=True)

    t0 = time.time()
    with arq.open("a") as f:
        for i, m in enumerate(msgs, 1):
            r = core.classificar(m, cfg)
            if r.get("origem") == "llm" and not r.get("falhou"):
                f.write(json.dumps({"chave": chave(m), "state": estado(m), "label": r["categoria"],
                                    "conf": r["confianca"]}, ensure_ascii=False) + "\n")
                f.flush()
            seg = (time.time() - t0) / i
            print(f"[{i}/{total}] {r['categoria']:<12} {seg:.1f}s/e-mail  ETA {(total - i) * seg / 60:.0f} min",
                  flush=True)

    dividir(arq)


def dividir(arq: Path):
    treino = (DADOS / "treino.jsonl").open("w")
    teste = (DADOS / "teste.jsonl").open("w")
    n = [0, 0]
    for linha in arq.open():
        e = json.loads(linha)
        if e["conf"] < CONFIANCA_MINIMA:
            continue
        item = {"state": e["state"], "questions": PERGUNTA, "expected": {"categoria": e["label"]}}
        eh_teste = int(hashlib.sha1(e["chave"].encode()).hexdigest(), 16) % 10 == 0
        (teste if eh_teste else treino).write(json.dumps(item, ensure_ascii=False) + "\n")
        n[eh_teste] += 1
    print(f"treino: {n[0]}  teste: {n[1]}  (confiança do LLM ≥ {CONFIANCA_MINIMA})")


if __name__ == "__main__":
    main()
