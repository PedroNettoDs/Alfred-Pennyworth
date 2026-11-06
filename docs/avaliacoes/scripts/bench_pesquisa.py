#!/usr/bin/env python3
"""
Avaliação de Pesquisa — roda UMA pergunta num modo e grava as medidas em JSON.

Modos:
  rapido     main.research() do researcher: consultas → SearXNG → síntese só com os trechos (MODEL_CHAT)
  profundo   main._pesquisa_profunda(): plano → busca → filtro → leitura → notas → relatório [n]
  iterativo  fluxo_iterativo.py: aproximação de sala limpa do loop do Odysseus, com as mesmas libs
  rapido-general / profundo-general   o mesmo, mas o SearXNG na categoria general em vez de news,it (o padrão
             de searxng_search) — separa o efeito da busca do efeito do pipeline

Isolamento: VAULT_ALFRED e JOBS_DIR vão para --saida; ChromaDB (cache semântico e indexação) e indexar_local()
ficam desligados — nada encosta no cofre real, no ChromaDB nem no índice RAG. O .env não é lido: os modelos
vêm das variáveis abaixo (padrão = o que a produção usa, segundo README e perfis.yml).

Medidas: tempo total, buscas, URLs devolvidas, páginas lidas, tokens (prompt_eval_count/eval_count do Ollama)
por modelo, tempo de carga de modelo (load_duration: o "ruído" de outro modelo ter ocupado a GPU), e o que o
/api/ps mostrava antes e depois.

Uso (sempre dentro do flock da GPU compartilhada):
  flock /tmp/alfred-bench-gpu.lock services/researcher/venv/bin/python \
      docs/avaliacoes/scripts/bench_pesquisa.py --modo profundo --pergunta tecnico --saida /tmp/bench
"""

import argparse
import json
import os
import sys
import time
import types
import uuid
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[3]
AQUI = Path(__file__).resolve().parent


def _preparar_ambiente(saida: Path) -> None:
    os.environ["VAULT_ALFRED"] = str(saida / "vault")
    os.environ["SHELL_EXECUTOR_TOKEN"] = "bench"
    for chave, valor in {
        "OLLAMA_BASE_URL": "http://localhost:11434",
        "SEARXNG_URL": "http://localhost:8889",
        "MODEL_CHAT": "gemma4:12b",
        "MODEL_EMBED": "bge-m3:latest",
        "OLLAMA_THINK": "false",
        "OLLAMA_NUM_CTX": "8192",
        "OLLAMA_EMBED_NUM_GPU": "0",
    }.items():
        os.environ.setdefault(chave, valor)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modo", choices=("rapido", "profundo", "iterativo", "rapido-general", "profundo-general"),
                    required=True)
    ap.add_argument("--pergunta", required=True, help="id em perguntas.json")
    ap.add_argument("--saida", required=True)
    ap.add_argument("--modelo-iterativo", default="gemma4:12b")
    ap.add_argument("--metricas", help="acrescenta o resumo numérico da execução neste .jsonl (vai para o git; "
                                       "os brutos em --saida têm texto de páginas da web e ficam fora)")
    args = ap.parse_args()

    saida = Path(args.saida).resolve()
    saida.mkdir(parents=True, exist_ok=True)
    _preparar_ambiente(saida)
    pergunta = next(p for p in json.loads((AQUI / "perguntas.json").read_text())["perguntas"]
                    if p["id"] == args.pergunta)
    tema = pergunta["tema"]

    sys.path.insert(0, str(RAIZ / "services" / "researcher"))
    sys.path.insert(0, str(RAIZ / "services"))
    import httpx
    import _shared.lib_alfred as la
    import main as researcher

    assert str(researcher.VAULT_ALFRED).startswith(str(saida)), "VAULT_ALFRED fora do diretório de teste"
    researcher.JOBS_DIR = saida / "jobs"
    researcher._get_chroma = lambda: None          # sem cache semântico e sem indexar no ChromaDB
    researcher.indexar_local = lambda: None        # sem índice RAG

    # ── medição: um httpx.Client só para a lib_alfred, que anota o que o Ollama devolve ──
    medidas = {"llm": [], "embed": {"chamadas": 0, "s": 0.0}, "buscas": [], "paginas": {}}

    class ClienteMedido(httpx.Client):
        def post(self, url, *a, **kw):
            t0 = time.monotonic()
            resp = super().post(url, *a, **kw)
            corpo = kw.get("json") or {}
            if str(url).endswith("/api/generate"):
                try:
                    d = resp.json()
                except Exception:
                    d = {}
                medidas["llm"].append({
                    "modelo": corpo.get("model"),
                    "prompt_tokens": d.get("prompt_eval_count", 0),
                    "saida_tokens": d.get("eval_count", 0),
                    "carga_s": round(d.get("load_duration", 0) / 1e9, 2),
                    "ollama_s": round(d.get("total_duration", 0) / 1e9, 2),
                    "parede_s": round(time.monotonic() - t0, 2),
                    "num_ctx": (corpo.get("options") or {}).get("num_ctx"),
                    "http": resp.status_code,
                })
            elif str(url).endswith("/api/embeddings"):
                medidas["embed"]["chamadas"] += 1
                medidas["embed"]["s"] += time.monotonic() - t0
            return resp

    httpx_medido = types.ModuleType("httpx_medido")
    httpx_medido.__dict__.update(httpx.__dict__)
    httpx_medido.Client = ClienteMedido
    la.httpx = httpx_medido

    busca_original = researcher.searxng_search

    def registrar_busca(consulta, resultados):
        medidas["buscas"].append({"consulta": consulta,
                                  "resultados": [{"url": r["url"], "title": r.get("title", ""),
                                                  "snippet": r.get("snippet", "")} for r in resultados]})

    def busca_medida(consulta, *a, **kw):
        if args.modo.endswith("-general"):
            kw["categories"] = "general"
        resultados = busca_original(consulta, *a, **kw)
        registrar_busca(consulta, resultados)
        return resultados

    researcher.searxng_search = busca_medida

    baixar_original = researcher._baixar_pagina

    def baixar_medido(url):
        t0 = time.monotonic()
        texto = baixar_original(url)
        medidas["paginas"][url] = {"ok": bool(texto), "s": round(time.monotonic() - t0, 2), "texto": texto or ""}
        return texto

    researcher._baixar_pagina = baixar_medido

    def ps():
        try:
            with httpx.Client(timeout=5) as c:
                return [{"nome": m["name"], "vram_mb": m.get("size_vram", 0) // 2**20}
                        for m in c.get(f"{os.environ['OLLAMA_BASE_URL']}/api/ps").json().get("models", [])]
        except Exception as e:
            return [{"erro": repr(e)}]

    registro = {"pergunta": args.pergunta, "tema": tema, "modo": args.modo,
                "inicio": datetime.now().isoformat(timespec="seconds"), "ollama_ps_antes": ps(),
                "env": {k: os.environ[k] for k in ("MODEL_CHAT", "MODEL_EMBED", "OLLAMA_THINK", "OLLAMA_NUM_CTX")},
                "modelos_pesquisa": {"notas": researcher._modelo("notas"), "sintese": researcher._modelo("sintese")}}
    t0 = time.monotonic()
    try:
        if args.modo.startswith("rapido"):
            req = researcher.ResearchRequest(topic=tema, refresh=True)
            r = researcher.research(req, authorization="Bearer bench")
            sintese = researcher._read_synthesis(Path(r["vault_path"]) / "synthesis.md")
            vistos, unicos = set(), []
            for b in medidas["buscas"]:
                for x in b["resultados"]:
                    if x["url"] not in vistos:
                        vistos.add(x["url"])
                        unicos.append(x)
            registro.update(status="ok", relatorio=sintese, consultas=r.get("queries_used"),
                            fontes=[{"n": i, "url": x["url"], "titulo": x["title"], "trecho": x["snippet"]}
                                    for i, x in enumerate(unicos[:12], 1)])
        elif args.modo.startswith("profundo"):
            slug = la.slugify(tema)
            job = {"id": uuid.uuid4().hex[:12], "topic": tema, "slug": slug, "perfil": None, "status": "running",
                   "etapa": "fila", "criado": datetime.now().isoformat(timespec="seconds"), "subperguntas": [],
                   "notas": [], "contagem": {"resultados": 0, "relevantes": 0, "tentadas": 0, "lidas": 0}}
            req = researcher.ResearchRequest(topic=tema, profundo=True, refresh=True)
            r = researcher._pesquisa_profunda(job, req, researcher.VAULT_ALFRED / "research" / slug)
            registro.update(status="ok", relatorio=r["synthesis"], subperguntas=r["subperguntas"],
                            contagem=job["contagem"],
                            fontes=[{"n": i, "url": n["url"], "titulo": n["titulo"], "subpergunta": n["subpergunta"],
                                     "fatos": n["fatos"]} for i, n in enumerate(job["notas"], 1)])
        else:
            sys.path.insert(0, str(AQUI))
            from fluxo_iterativo import FluxoIterativo
            researcher.TEXTO_MAX_CHARS = 15000      # o Odysseus manda até 15.000 caracteres da página ao LLM
            fluxo = FluxoIterativo(la, baixar_medido, args.modelo_iterativo,
                                   os.environ["SEARXNG_URL"], registrar_busca)
            r = fluxo.pesquisar(tema)
            registro.update(status="ok" if r["relatorio"] else "vazio", relatorio=r["relatorio"],
                            subperguntas=r["subperguntas"], plano=r["plano"], rodadas=r["rodadas"],
                            motivo_fim=r["motivo_fim"], relatorio_evolutivo=r["relatorio_evolutivo"],
                            fontes=[dict(d, n=i) for i, d in enumerate(r["descobertas"], 1)])
    except Exception as e:
        registro.update(status="erro", erro=repr(e))
    registro["tempo_total_s"] = round(time.monotonic() - t0, 1)
    registro["ollama_ps_depois"] = ps()

    # ── resumo ──
    por_modelo: dict[str, dict] = {}
    for c in medidas["llm"]:
        m = por_modelo.setdefault(c["modelo"], {"chamadas": 0, "prompt_tokens": 0, "saida_tokens": 0,
                                               "carga_s": 0.0, "ollama_s": 0.0, "parede_s": 0.0})
        m["chamadas"] += 1
        for k in ("prompt_tokens", "saida_tokens", "carga_s", "ollama_s", "parede_s"):
            m[k] = round(m[k] + c[k], 2)
    urls_buscadas = {x["url"] for b in medidas["buscas"] for x in b["resultados"]}
    registro["medidas"] = {
        "buscas": len(medidas["buscas"]),
        "urls_buscadas": len(urls_buscadas),
        "paginas_tentadas": len(medidas["paginas"]),
        "paginas_baixadas": sum(1 for p in medidas["paginas"].values() if p["ok"]),
        "fontes_usadas": len(registro.get("fontes", [])),
        "llm_por_modelo": por_modelo,
        "tokens_prompt": sum(c["prompt_tokens"] for c in medidas["llm"]),
        "tokens_saida": sum(c["saida_tokens"] for c in medidas["llm"]),
        "carga_modelo_s": round(sum(c["carga_s"] for c in medidas["llm"]), 1),
        "embeddings": {"chamadas": medidas["embed"]["chamadas"], "s": round(medidas["embed"]["s"], 1)},
        "palavras_relatorio": len((registro.get("relatorio") or "").split()),
    }
    registro["chamadas_llm"] = medidas["llm"]
    registro["buscas"] = medidas["buscas"]

    base = saida / f"{args.modo}-{args.pergunta}"
    base.with_suffix(".json").write_text(json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(f"{base}-paginas.json").write_text(json.dumps(medidas["paginas"], ensure_ascii=False), encoding="utf-8")
    if args.metricas:
        resumo = {k: registro[k] for k in ("modo", "pergunta", "status", "tempo_total_s", "medidas") if k in registro}
        resumo.update({k: registro[k] for k in ("contagem", "motivo_fim", "erro") if k in registro})
        if "rodadas" in registro:
            resumo["rodadas"] = [{k: r[k] for k in ("rodada", "resultados", "escolhidos", "descobertas", "t_s")
                                  if k in r} for r in registro["rodadas"]]
        resumo["modelos_carregados_antes"] = [m.get("nome") for m in registro["ollama_ps_antes"]]
        resumo["data"] = registro["inicio"]
        with open(args.metricas, "a", encoding="utf-8") as f:
            f.write(json.dumps(resumo, ensure_ascii=False) + "\n")
    print(json.dumps({k: registro[k] for k in ("pergunta", "modo", "status", "tempo_total_s")}
                     | {"medidas": registro["medidas"]}, ensure_ascii=False))
    return 0 if registro["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
