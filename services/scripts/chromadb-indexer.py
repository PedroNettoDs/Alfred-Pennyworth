#!/usr/bin/env python3
"""
RAG local do Alfred: indexa os cofres e busca neles. A lógica está em services/_shared/lib_rag.py.

    services/inbox/venv/bin/python services/scripts/chromadb-indexer.py indexar [--cofre alfred|pedro] [--tudo]
    services/inbox/venv/bin/python services/scripts/chromadb-indexer.py buscar "consulta" [--cofre ...] [--pasta ...] [--limite N]

indexar: FTS5 (lib_db) + vetores no ChromaDB (embeddings do Ollama com MODEL_EMBED) de VAULT_ALFRED e VAULT_PEDRO.
Só nota nova ou alterada vai ao Ollama; nota apagada sai do índice; --tudo recalcula todos os vetores. ChromaDB ou
Ollama fora do ar: atualiza só o FTS5 e avisa no log. Roda sozinho a cada 30 min (alfred-rag.timer) — em modo jogo
(logs/alfred_desligado) a execução pelo timer não faz nada; no terminal roda sempre.

Usa o Python de um venv do Alfred (lib_alfred importa httpx). O .env é lido em tempo de execução (variáveis já
exportadas vencem).
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _shared.lib_alfred import PROJECT_ROOT, load_env, log  # noqa: E402
from _shared import lib_rag  # noqa: E402


def cmd_indexar(args) -> int:
    flag = PROJECT_ROOT / "logs" / "alfred_desligado"
    if flag.exists() and os.getenv("ALFRED_AUTOMACAO") == "1" and os.getenv("ALFRED_FORCAR") != "1":
        log("[rag] desligado (modo jogo) — indexação não executada. Religue: services/scripts/alfred-switch.sh on")
        return 0
    print(json.dumps(lib_rag.indexar(cofre=args.cofre, tudo=args.tudo), ensure_ascii=False, indent=2))
    return 0


def cmd_buscar(args) -> int:
    for n, r in enumerate(lib_rag.buscar(args.consulta, cofre=args.cofre, pasta=args.pasta, limite=args.limite), 1):
        trecho = " ".join(r["trecho"].split())
        print(f"{n:>2}. {r['pontuacao']:.4f}  {r['origem']:<5} [{r['cofre']}] {r['caminho']}\n      {trecho[:160]}")
    return 0


def main() -> int:
    load_env(override=False)
    ap = argparse.ArgumentParser(description="RAG local do Alfred (FTS5 + ChromaDB)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("indexar", help="indexa os cofres (incremental)")
    p.add_argument("--cofre", choices=sorted(lib_rag.COFRES))
    p.add_argument("--tudo", action="store_true", help="recalcula todos os vetores")
    p.set_defaults(func=cmd_indexar)
    p = sub.add_parser("buscar", help="busca híbrida nos cofres")
    p.add_argument("consulta")
    p.add_argument("--cofre", choices=sorted(lib_rag.COFRES))
    p.add_argument("--pasta", default="")
    p.add_argument("--limite", type=int, default=8)
    p.set_defaults(func=cmd_buscar)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
