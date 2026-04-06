#!/usr/bin/env python3
"""
Alfred Research Service v2.0
Orquestrador: busca web → síntese com LLM → vault Obsidian → sync Knowledge Base

POST /research  {"topic": "...", "num_queries": 4, "results_per_query": 8}
GET  /health
"""

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Alfred Researcher", version="2.0.0")

# ── Config (tudo via env, zero hardcode) ──────────────────────
TOKEN        = os.getenv("SHELL_EXECUTOR_TOKEN", "")
SEARXNG_URL  = os.getenv("SEARXNG_URL", "http://localhost:8888")
OLLAMA_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL        = os.getenv("MODEL_CHAT", "llama3.1:8b")
VAULT_ALFRED = Path(os.getenv("VAULT_ALFRED", ""))
WEBUI_TOKEN  = os.getenv("WEBUI_API_TOKEN", "")
WEBUI_URL    = os.getenv("WEBUI_URL", "http://localhost:3000")
SCRIPTS_DIR  = Path(__file__).parent.parent / "scripts"


# ── Auth ──────────────────────────────────────────────────────
def check_auth(authorization: Optional[str]):
    if not TOKEN or authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Models ────────────────────────────────────────────────────
class ResearchRequest(BaseModel):
    topic: str
    num_queries: int = 4
    results_per_query: int = 8


# ── Helpers ───────────────────────────────────────────────────
def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:60].strip("-")


def ollama_generate(prompt: str, timeout: int = 180) -> str:
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": MODEL, "prompt": prompt, "stream": False},
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()


def searxng_search(query: str, num_results: int = 8) -> list[dict]:
    with httpx.Client(timeout=30) as client:
        try:
            resp = client.get(
                f"{SEARXNG_URL}/search",
                params={"q": query, "format": "json"},
            )
            if resp.status_code == 200:
                return resp.json().get("results", [])[:num_results]
        except Exception:
            pass
    return []


def trigger_sync():
    """Dispara sync incremental assíncrono do vault para a Knowledge Base."""
    sync_script = SCRIPTS_DIR / "sync-knowledge.py"
    if sync_script.exists() and WEBUI_TOKEN:
        env = {**os.environ, "WEBUI_API_TOKEN": WEBUI_TOKEN, "WEBUI_URL": WEBUI_URL}
        subprocess.Popen(
            ["python3", str(sync_script)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


# ── Main endpoint ─────────────────────────────────────────────
@app.post("/research")
def research(req: ResearchRequest, authorization: Optional[str] = Header(None)):
    check_auth(authorization)

    topic    = req.topic.strip()
    slug     = slugify(topic)
    now      = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    dt_str   = now.strftime("%d/%m/%Y %H:%M")

    topic_dir = VAULT_ALFRED / "research" / slug
    topic_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Gerar consultas de pesquisa ────────────────────────
    queries_prompt = (
        f'Gere {req.num_queries} consultas de pesquisa variadas sobre: "{topic}"\n'
        f"Inclua variações em português e inglês.\n"
        f"Retorne SOMENTE um array JSON, sem texto adicional:\n"
        f'["consulta 1", "consulta 2", "consulta 3", "consulta 4"]'
    )

    queries_raw = ollama_generate(queries_prompt, timeout=60)
    try:
        match   = re.search(r"\[.*?\]", queries_raw, re.DOTALL)
        queries = json.loads(match.group()) if match else [topic]
    except Exception:
        queries = [topic, f"{topic} tutorial", f"what is {topic}", f"{topic} examples"]

    queries = queries[: req.num_queries]

    # ── 2. Executar buscas ────────────────────────────────────
    all_results = []
    for q in queries:
        all_results.extend(searxng_search(q, req.results_per_query))

    seen, unique = set(), []
    for r in all_results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(r)

    # ── 3. Sintetizar com LLM ─────────────────────────────────
    sources_text = "\n\n".join(
        f"[{i+1}] {r.get('title', '')}\nURL: {r.get('url', '')}\n"
        f"{r.get('content', r.get('snippet', ''))[:600]}"
        for i, r in enumerate(unique[:12])
    )

    synthesis_prompt = (
        f'Você é um pesquisador técnico. Com base nas fontes abaixo sobre "{topic}", '
        f"crie uma nota de conhecimento estruturada em português.\n\n"
        f"FONTES:\n{sources_text}\n\n"
        f"Escreva APENAS o conteúdo markdown (sem frontmatter) com estas seções:\n"
        f"## Visão Geral\n## Conceitos Principais\n"
        f"## Como Funciona / Arquitetura\n## Aplicações Práticas\n"
        f"## Referências Importantes\n## Perguntas para Explorar\n\n"
        f"Seja técnico, preciso e objetivo. "
        f"Não adicione introduções ou meta-comentários sobre o que vai fazer."
    )

    synthesis = ollama_generate(synthesis_prompt, timeout=180)

    # ── 4. Salvar no vault ────────────────────────────────────
    sources_md_lines = [
        "---", f'title: "Fontes — {topic}"', f"date: {date_str}",
        f"topic: {slug}", "type: sources", "---", "",
        f"# Fontes: {topic}", "",
        f"> Coletado em {dt_str} | {len(unique)} fontes únicas", "",
    ]
    for i, r in enumerate(unique[:20], 1):
        title   = r.get("title", "Sem título")
        url     = r.get("url", "")
        snippet = r.get("content", r.get("snippet", ""))[:300]
        sources_md_lines += [f"## {i}. {title}", f"- **URL**: {url}", f"- {snippet}", ""]

    (topic_dir / "sources.md").write_text("\n".join(sources_md_lines), encoding="utf-8")

    synthesis_md = "\n".join([
        "---", f'title: "Síntese — {topic}"', f"date: {date_str}",
        f"topic: {slug}", "type: synthesis", f"tags: [research, {slug}]",
        "---", "", f"# {topic}", "", synthesis, "",
        "---", f"*Sintetizado em {dt_str} a partir de {len(unique)} fontes*",
    ])
    (topic_dir / "synthesis.md").write_text(synthesis_md, encoding="utf-8")

    queries_list = "\n".join(f"- `{q}`" for q in queries)
    index_md = "\n".join([
        "---", f'title: "{topic}"', f"date: {date_str}",
        f"topic: {slug}", "type: index", f"tags: [research, {slug}]",
        "---", "", f"# {topic}", "", f"> Pesquisa realizada em {dt_str}", "",
        "## Arquivos", "", "- [[synthesis|Síntese e análise]]",
        f"- [[sources|Fontes ({len(unique)} resultados)]]", "",
        "## Consultas realizadas", "", queries_list, "",
        "## Resumo rápido", "",
        synthesis[:500] + ("..." if len(synthesis) > 500 else ""), "",
        "---", "*Gerado pelo Alfred Research Service*",
    ])
    (topic_dir / "index.md").write_text(index_md, encoding="utf-8")

    # ── 5. Disparar sync assíncrono ───────────────────────────
    trigger_sync()

    return {
        "status": "success",
        "topic": topic,
        "slug": slug,
        "vault_path": str(topic_dir),
        "queries_used": queries,
        "sources_found": len(unique),
        "files": ["index.md", "synthesis.md", "sources.md"],
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0", "model": MODEL, "vault": str(VAULT_ALFRED)}
