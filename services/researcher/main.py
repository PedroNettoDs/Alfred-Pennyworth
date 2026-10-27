#!/usr/bin/env python3
"""
Alfred Research Service v3.1
Orquestrador: cache exato → cache semântico → busca web → síntese LLM → vault → sync KB

POST /research  {"topic": "...", "num_queries": 4, "results_per_query": 8,
                  "perfil": null, "refresh": false}
GET  /health
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# ── Shared libs ───────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared.lib_alfred import (
    log, slugify, ollama_generate, ollama_embed, searxng_search, vault_write,
)
from _shared.perfis import get_perfil
import _shared.lib_chromadb as chroma

app = FastAPI(title="Alfred Researcher", version="3.1.0")

# ── Config ────────────────────────────────────────────────────
TOKEN        = os.getenv("SHELL_EXECUTOR_TOKEN", "")
VAULT_ALFRED = Path(os.getenv("VAULT_ALFRED", ""))
WEBUI_TOKEN  = os.getenv("WEBUI_API_TOKEN", "")
WEBUI_URL    = os.getenv("WEBUI_URL", "http://localhost:3000")
SCRIPTS_DIR  = Path(__file__).parent.parent / "scripts"
SYNC_LOG     = Path(__file__).resolve().parent.parent.parent / "logs" / "sync-knowledge.log"
CACHE_DAYS   = 7
CHROMA_COLLECTION = "research_alfred"
SEMANTIC_DIST_MAX = 0.25   # distância coseno ≤ 0.25 ≡ similaridade ≥ 0.75

# collection_id lazy-init (None enquanto ChromaDB não for contactado)
_chroma_id: Optional[str] = None


# ── Auth ──────────────────────────────────────────────────────
def check_auth(authorization: Optional[str]):
    if not TOKEN or authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Models ────────────────────────────────────────────────────
class ResearchRequest(BaseModel):
    topic: str
    num_queries: int = 4
    results_per_query: int = 8
    perfil: Optional[str] = None
    refresh: bool = False


# ── ChromaDB helpers ──────────────────────────────────────────
def _get_chroma() -> Optional[str]:
    """Retorna collection_id, inicializando na primeira chamada. None se offline."""
    global _chroma_id
    if _chroma_id is None:
        _chroma_id = chroma.ensure_collection(CHROMA_COLLECTION)
        if _chroma_id:
            log(f"[researcher] ChromaDB OK — collection '{CHROMA_COLLECTION}' id={_chroma_id[:8]}…")
        else:
            log("[researcher] ChromaDB indisponível — operando sem cache semântico")
    return _chroma_id


def _index_synthesis(slug: str, topic: str, date_str: str, topic_dir: Path, synthesis: str):
    """Indexa a síntese no ChromaDB de forma não-bloqueante. Falha silenciosa."""
    cid = _get_chroma()
    if not cid:
        return

    embedding = ollama_embed(topic)
    if not embedding:
        log("[researcher] ollama_embed retornou vazio — pulando indexação ChromaDB")
        return

    ok = chroma.upsert_document(
        collection_id=cid,
        doc_id=slug,
        text=topic,
        metadata={
            "slug":       slug,
            "topic":      topic,
            "date":       date_str,
            "vault_path": str(topic_dir),
        },
        embedding=embedding,
    )
    if ok:
        log(f"[researcher] ChromaDB indexado — {slug}")


# ── Cache exato ───────────────────────────────────────────────
def _synthesis_vazia(texto: str) -> bool:
    """True se a síntese não tem conteúdo além do título "# tema" e do rodapé "*Sintetizado em ...*"
    (o que ficava gravado quando o Ollama falhava)."""
    corpo = re.sub(r"^# .*\n?", "", texto.strip(), count=1)
    corpo = re.sub(r"(^|\n)---\s*\n\*Sintetizado em [^\n]*\*\s*$", "", corpo.strip())
    return not corpo.strip()


def _cache_valid(synthesis_path: Path) -> bool:
    """True se synthesis.md existe, tem conteúdo e tem menos de CACHE_DAYS dias."""
    if not synthesis_path.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(synthesis_path.stat().st_mtime)
    if age >= timedelta(days=CACHE_DAYS):
        return False
    try:
        return not _synthesis_vazia(_read_synthesis(synthesis_path))
    except OSError:
        return False


def _read_synthesis(synthesis_path: Path) -> str:
    """Lê synthesis.md removendo frontmatter YAML."""
    raw = synthesis_path.read_text(encoding="utf-8")
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            return raw[end + 4:].lstrip("\n")
    return raw


# ── Cache semântico ───────────────────────────────────────────
def _meta_age_ok(metadata: dict) -> bool:
    """True se a data do metadata está dentro de CACHE_DAYS."""
    date_str = metadata.get("date", "")
    try:
        date = datetime.strptime(date_str, "%Y-%m-%d")
        return (datetime.now() - date) < timedelta(days=CACHE_DAYS)
    except Exception:
        return False


def check_semantic_cache(topic: str) -> Optional[dict]:
    """
    Gera embedding do topic e busca no ChromaDB.
    Retorna dict com resultado se similaridade ≥ 0.75 e idade ≤ 7 dias, None caso contrário.
    """
    cid = _get_chroma()
    if not cid:
        return None

    embedding = ollama_embed(topic)
    if not embedding:
        return None

    results = chroma.query_similar(cid, embedding, n_results=3)
    for r in results:
        distance = r.get("distance", 1.0)
        if distance > SEMANTIC_DIST_MAX:
            continue

        metadata = r.get("metadata", {})
        if not _meta_age_ok(metadata):
            continue

        matched_slug = metadata.get("slug", r["id"])
        matched_topic = metadata.get("topic", matched_slug)
        vault_path_str = metadata.get("vault_path", "")
        vault_path = Path(vault_path_str) if vault_path_str else VAULT_ALFRED / "research" / matched_slug
        synthesis_path = vault_path / "synthesis.md"

        # Confirma que o arquivo ainda existe no disco (e não é uma síntese vazia de quando o LLM falhou)
        if not synthesis_path.exists():
            log(f"[researcher] Semantic hit {matched_slug} mas synthesis.md não existe — ignorando")
            continue
        if _synthesis_vazia(_read_synthesis(synthesis_path)):
            log(f"[researcher] Semantic hit {matched_slug} mas synthesis.md está vazio — ignorando")
            continue

        similarity = round(1.0 - distance, 4)
        log(f"[researcher] SEMANTIC HIT — '{matched_topic}' (distância={distance:.4f}, similaridade={similarity:.4f})")

        return {
            "matched_slug":  matched_slug,
            "matched_topic": matched_topic,
            "similarity":    similarity,
            "vault_path":    str(vault_path),
            "synthesis":     _read_synthesis(synthesis_path),
        }

    return None


# ── Perfil ────────────────────────────────────────────────────
def _build_perfil_context(perfil_nome: str) -> str:
    """Retorna bloco de contexto do perfil para injetar nos prompts."""
    try:
        p = get_perfil(perfil_nome)
        lines = [f"Contexto do usuário: {p.get('descricao', '')}"]
        if p.get("interesses_quentes"):
            lines.append("Priorize ângulos sobre: " + ", ".join(p["interesses_quentes"]))
        if p.get("excluir"):
            lines.append("Evite: " + ", ".join(p["excluir"]))
        if p.get("tom"):
            lines.append(f"Tom: {p['tom']}")
        return "\n".join(lines)
    except KeyError:
        log(f"[researcher] Perfil '{perfil_nome}' não encontrado — ignorando")
        return ""


# ── Sync ──────────────────────────────────────────────────────
def trigger_sync():
    """Dispara sync incremental assíncrono do vault para a Knowledge Base.
    Roda com o Python deste venv (o python3 do sistema não tem as libs) e grava a saída em
    logs/sync-knowledge.log (antes ia para DEVNULL e toda falha passava em silêncio)."""
    sync_script = SCRIPTS_DIR / "sync-knowledge.py"
    if sync_script.exists() and WEBUI_TOKEN:
        env = {**os.environ, "WEBUI_API_TOKEN": WEBUI_TOKEN, "WEBUI_URL": WEBUI_URL}
        try:
            SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(SYNC_LOG, "a", encoding="utf-8") as saida:
                saida.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} sync disparado pelo researcher ===\n")
                saida.flush()
                subprocess.Popen(
                    [sys.executable, str(sync_script)],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=saida,
                    stderr=subprocess.STDOUT,
                )
        except OSError as e:
            log(f"[researcher] Falha ao disparar sync-knowledge: {e}")


# ── Main endpoint ─────────────────────────────────────────────
@app.post("/research")
def research(req: ResearchRequest, authorization: Optional[str] = Header(None)):
    check_auth(authorization)

    topic     = req.topic.strip()
    if not topic:
        raise HTTPException(status_code=422, detail="Tema vazio")
    # tema só de símbolos/emoji vira slug vazio: gravaria em research/ e serviria de "cache exato" a qualquer outro
    slug      = slugify(topic) or "tema-" + hashlib.sha1(topic.encode("utf-8")).hexdigest()[:8]
    topic_dir = VAULT_ALFRED / "research" / slug
    synthesis_path = topic_dir / "synthesis.md"

    log(f"[researcher] topic={topic!r} slug={slug} perfil={req.perfil} refresh={req.refresh}")

    # ── 1. Cache exato ────────────────────────────────────────
    if not req.refresh and _cache_valid(synthesis_path):
        log(f"[researcher] CACHE EXATO HIT — {slug}")
        return {
            "status":     "success",
            "topic":      topic,
            "slug":       slug,
            "vault_path": str(topic_dir),
            "cached":     True,
            "cache_type": "exact",
            "synthesis":  _read_synthesis(synthesis_path),
            "files":      ["index.md", "synthesis.md", "sources.md"],
        }

    # ── 2. Cache semântico ────────────────────────────────────
    if not req.refresh:
        sem = check_semantic_cache(topic)
        if sem:
            return {
                "status":        "success",
                "topic":         topic,
                "slug":          slug,
                "cached":        True,
                "cache_type":    "semantic",
                "matched_topic": sem["matched_topic"],
                "matched_slug":  sem["matched_slug"],
                "similarity":    sem["similarity"],
                "vault_path":    sem["vault_path"],
                "synthesis":     sem["synthesis"],
                "files":         ["index.md", "synthesis.md", "sources.md"],
            }

    # ── 3. Pesquisa nova ──────────────────────────────────────
    now      = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    dt_str   = now.strftime("%d/%m/%Y %H:%M")

    perfil_ctx  = _build_perfil_context(req.perfil) if req.perfil else ""
    perfil_hint = f"\n{perfil_ctx}\n" if perfil_ctx else ""

    # 3a. Gerar consultas
    queries_prompt = (
        f'Gere {req.num_queries} consultas de pesquisa variadas sobre: "{topic}"\n'
        f"{perfil_hint}"
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

    # 3b. Buscar
    all_results = []
    for q in queries:
        all_results.extend(searxng_search(q, max_results=req.results_per_query))

    seen, unique = set(), []
    for r in all_results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(r)

    # 3c. Sintetizar
    sources_text = "\n\n".join(
        f"[{i+1}] {r.get('title', '')}\nURL: {r.get('url', '')}\n"
        f"{r.get('snippet', '')[:600]}"
        for i, r in enumerate(unique[:12])
    )

    synthesis_prompt = (
        f'Você é um pesquisador técnico. Com base nas fontes abaixo sobre "{topic}", '
        f"crie uma nota de conhecimento estruturada em português.\n"
        f"{perfil_hint}"
        f"FONTES:\n{sources_text}\n\n"
        f"Escreva APENAS o conteúdo markdown (sem frontmatter) com estas seções:\n"
        f"## Visão Geral\n## Conceitos Principais\n"
        f"## Como Funciona / Arquitetura\n## Aplicações Práticas\n"
        f"## Referências Importantes\n## Perguntas para Explorar\n\n"
        f"Seja técnico, preciso e objetivo. "
        f"Não adicione introduções ou meta-comentários sobre o que vai fazer."
    )

    synthesis = ollama_generate(synthesis_prompt, timeout=180)
    if not synthesis.strip():
        # ollama_generate devolve "" em erro/timeout: nada é gravado nem cacheado (antes ficava 7 dias no cache)
        log(f"[researcher] LLM não devolveu síntese para {slug!r} — nada gravado")
        raise HTTPException(status_code=503, detail="O LLM não respondeu (síntese vazia). Tente de novo em instantes.")

    # ── 4. Salvar no vault ────────────────────────────────────
    sources_body = (
        f"# Fontes: {topic}\n\n"
        f"> Coletado em {dt_str} | {len(unique)} fontes únicas\n\n"
    )
    for i, r in enumerate(unique[:20], 1):
        sources_body += (
            f"## {i}. {r.get('title', 'Sem título')}\n"
            f"- **URL**: {r.get('url', '')}\n"
            f"- {r.get('snippet', '')[:300]}\n\n"
        )
    vault_write(VAULT_ALFRED, f"research/{slug}", "sources", sources_body, frontmatter={
        "title": f"Fontes — {topic}",
        "date":  date_str,
        "topic": slug,
        "type":  "sources",
    })

    synthesis_body = (
        f"# {topic}\n\n"
        f"{synthesis}\n\n"
        f"---\n*Sintetizado em {dt_str} a partir de {len(unique)} fontes*\n"
    )
    vault_write(VAULT_ALFRED, f"research/{slug}", "synthesis", synthesis_body, frontmatter={
        "title": f"Síntese — {topic}",
        "date":  date_str,
        "topic": slug,
        "type":  "synthesis",
        "tags":  ["research", slug],
    })

    queries_list = "\n".join(f"- `{q}`" for q in queries)
    index_body = (
        f"# {topic}\n\n"
        f"> Pesquisa realizada em {dt_str}\n\n"
        f"## Arquivos\n\n"
        f"- [[synthesis|Síntese e análise]]\n"
        f"- [[sources|Fontes ({len(unique)} resultados)]]\n\n"
        f"## Consultas realizadas\n\n{queries_list}\n\n"
        f"## Resumo rápido\n\n"
        f"{synthesis[:500]}{'...' if len(synthesis) > 500 else ''}\n\n"
        f"---\n*Gerado pelo Alfred Research Service*\n"
    )
    vault_write(VAULT_ALFRED, f"research/{slug}", "index", index_body, frontmatter={
        "title": topic,
        "date":  date_str,
        "topic": slug,
        "type":  "index",
        "tags":  ["research", slug],
    })

    # ── 5. Indexar no ChromaDB ────────────────────────────────
    _index_synthesis(slug, topic, date_str, topic_dir, synthesis)

    # ── 6. Sync KB ────────────────────────────────────────────
    trigger_sync()

    return {
        "status":       "success",
        "topic":        topic,
        "slug":         slug,
        "vault_path":   str(topic_dir),
        "cached":       False,
        "cache_type":   None,
        "queries_used": queries,
        "sources_found": len(unique),
        "files":        ["index.md", "synthesis.md", "sources.md"],
    }


@app.get("/health")
def health():
    chroma_ok = _get_chroma() is not None
    return {
        "status":     "ok",
        "version":    "3.1.0",
        "vault":      str(VAULT_ALFRED),
        "cache_days": CACHE_DAYS,
        "chromadb":   "online" if chroma_ok else "offline",
    }
