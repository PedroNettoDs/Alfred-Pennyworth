"""
Avaliação da memória — latência de uma consulta k=8 no ChromaDB de produção (porta 8001), para comparar com o
cosseno em Python e o numpy em cache de memoria_busca.py.

Só leitura: consulta a coleção `cofres-bge-m3` do RAG (1024 dimensões, cosseno) com vetores aleatórios e
mede o tempo. Não grava nada no Chroma nem imprime documento, id ou metadado — só contagem e tempos.

Mede três jeitos de chamar:
  novo_cliente_completo   como lib_chromadb.query_similar faz hoje: httpx.Client novo a cada chamada,
                          include distances + metadatas + documents
  novo_cliente_distancias cliente novo, só distâncias
  keepalive_distancias    cliente reaproveitado (conexão HTTP aberta), só distâncias — o melhor caso
e o heartbeat com conexão aberta (o piso do HTTP).

Uso: python memoria_chroma.py [--url http://localhost:8001] [--colecao cofres-bge-m3] [--rep 40] [--saida x.json]
"""

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path

import httpx


def _vetor(dim: int) -> list:
    v = [random.gauss(0, 1) for _ in range(dim)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def _medir(fn, rep: int) -> dict:
    tempos = []
    for _ in range(rep):
        t = time.perf_counter()
        fn()
        tempos.append((time.perf_counter() - t) * 1000)
    tempos.sort()
    return {"mediana_ms": round(statistics.median(tempos), 2), "p90_ms": round(tempos[int(0.9 * (rep - 1))], 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8001")
    ap.add_argument("--colecao", default="cofres-bge-m3")
    ap.add_argument("--rep", type=int, default=40)
    ap.add_argument("--saida")
    a = ap.parse_args()

    base = f"{a.url}/api/v2/tenants/default_tenant/databases/default_database/collections"
    col = next(c for c in httpx.get(base, timeout=10).json() if c["name"] == a.colecao)
    cid, dim = col["id"], col.get("dimension") or 1024
    url_q = f"{base}/{cid}/query"

    def consulta(cliente, include):
        r = cliente.post(url_q, json={"query_embeddings": [_vetor(dim)], "n_results": 8, "include": include})
        r.raise_for_status()

    def novo(include):
        with httpx.Client(timeout=20) as c:
            consulta(c, include)

    aberto = httpx.Client(timeout=20)
    consulta(aberto, ["distances"])              # aquecimento
    res = {
        "colecao": a.colecao, "dimensao": dim, "itens": httpx.get(f"{base}/{cid}/count", timeout=10).json(),
        "versao_chroma": httpx.get(f"{a.url}/api/v2/version", timeout=10).json(),
        "novo_cliente_completo": _medir(lambda: novo(["distances", "metadatas", "documents"]), a.rep),
        "novo_cliente_distancias": _medir(lambda: novo(["distances"]), a.rep),
        "keepalive_distancias": _medir(lambda: consulta(aberto, ["distances"]), a.rep),
        "heartbeat_keepalive": _medir(lambda: aberto.get(f"{a.url}/api/v2/heartbeat"), a.rep),
        "carga_cpu": os.getloadavg(),
    }
    print(json.dumps(res, ensure_ascii=False))
    if a.saida:
        Path(a.saida).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
