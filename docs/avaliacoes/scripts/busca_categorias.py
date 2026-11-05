#!/usr/bin/env python3
"""
Avaliação de Pesquisa — precisão da busca no SearXNG local por configuração, sem LLM (não precisa do flock).

Para cada pergunta de perguntas.json, duas consultas (o tema em português e uma versão curta em inglês) em três
configurações:
  alfred     categories=news,it            (padrão de searxng_search em _shared/lib_alfred.py)
  general    categories=general            (sem idioma, motores padrão da instância)
  odysseus   categories=general, language=en, engines=google,brave,duckduckgo (provedor searxng do Odysseus)

"Relevante" = o termo-chave da pergunta aparece no título, no trecho ou na URL (regex abaixo, fixada antes de
rodar). Mede: resultados, relevantes nos 10 primeiros, domínios distintos entre os relevantes.

  python3 docs/avaliacoes/scripts/busca_categorias.py > /tmp/busca.json
"""

import json
import re
import sys
import time
from urllib.parse import urlsplit

import httpx

SEARXNG = "http://localhost:8889"
CHAVES = {
    "tecnico":   (r"systemd", "systemd Restart RestartSec StartLimitBurst"),
    "noticia":   (r"elei[çc][ãa]o|elei[çc][õo]es|election|presidencial|presidential", "Brazil 2026 presidential election result"),
    "produtos":  (r"orange ?pi ?5|raspberry ?pi ?5", "Raspberry Pi 5 vs Orange Pi 5 home server"),
    "academico": (r"lottery ticket|bilhete de loteria", "lottery ticket hypothesis neural networks"),
    "brasil":    (r"pix autom[áa]tico|pix recorrente|recurring pix|automatic pix", "Pix Automático Banco Central regras"),
}
CONFIGS = {
    "alfred":   {"categories": "news,it"},
    "general":  {"categories": "general"},
    "odysseus": {"categories": "general", "language": "en", "engines": "google,brave,duckduckgo"},
}


def main() -> int:
    perguntas = json.load(open(__file__.rsplit("/", 1)[0] + "/perguntas.json"))["perguntas"]
    saida = []
    with httpx.Client(timeout=25) as c:
        for p in perguntas:
            padrao, consulta_en = CHAVES[p["id"]]
            for consulta in (p["tema"], consulta_en):
                for nome, params in CONFIGS.items():
                    try:
                        dados = c.get(f"{SEARXNG}/search", params={"q": consulta, "format": "json", **params}).json()
                    except Exception as e:
                        dados = {"results": [], "erro": repr(e)}
                    res = [r for r in dados.get("results", []) if r.get("url")]
                    top = res[:10]
                    rel = [r for r in top if re.search(padrao, f"{r.get('title','')} {r.get('content','')} {r['url']}", re.I)]
                    saida.append({"pergunta": p["id"], "consulta": "pt" if consulta == p["tema"] else "en",
                                  "config": nome, "resultados": len(res), "relevantes_top10": len(rel),
                                  "dominios_relevantes": len({urlsplit(r["url"]).hostname for r in rel}),
                                  "motores_falhos": [e[0] for e in dados.get("unresponsive_engines", [])]})
                    time.sleep(1.5)   # não estourar o limite dos motores de busca de cima
    json.dump(saida, sys.stdout, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
