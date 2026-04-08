#!/usr/bin/env python3
"""
Bootstrap ChromaDB — indexa retroativamente todas as pesquisas do vault.

Lê todos os vault_alfred/research/*/synthesis.md, gera embedding do topic
via Ollama e faz upsert no ChromaDB. Idempotente (upsert por slug).

Uso:
    services/researcher/venv/bin/python \
        services/researcher/scripts/bootstrap_chromadb.py

    # Dry-run (lista arquivos sem indexar):
    ... bootstrap_chromadb.py --dry-run

    # Forçar reindexação mesmo de docs já indexados:
    ... bootstrap_chromadb.py --force
"""

import argparse
import sys
import os
from datetime import datetime
from pathlib import Path

import yaml

# ── sys.path: adiciona services/ pra importar _shared ────────
SCRIPTS_DIR  = Path(__file__).parent
RESEARCHER   = SCRIPTS_DIR.parent
SERVICES_DIR = RESEARCHER.parent
sys.path.insert(0, str(SERVICES_DIR))

from _shared.lib_alfred import log, load_env, ollama_embed, slugify
import _shared.lib_chromadb as chroma

COLLECTION_NAME = "research_alfred"


# ── Helpers ───────────────────────────────────────────────────
def parse_frontmatter(path: Path) -> dict:
    """Extrai e parseia o bloco YAML de frontmatter do arquivo."""
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.startswith("---"):
            return {}
        end = raw.find("\n---", 3)
        if end == -1:
            return {}
        return yaml.safe_load(raw[3:end].strip()) or {}
    except Exception as e:
        log(f"[bootstrap] Erro ao parsear frontmatter de {path}: {e}")
        return {}


def extract_topic(frontmatter: dict, slug: str) -> str:
    """
    Extrai o topic legível do frontmatter.
    O título tem formato 'Síntese — <topic>', então removemos o prefixo.
    """
    title = frontmatter.get("title", "")
    if "— " in title:
        return title.split("— ", 1)[1].strip()
    if title:
        return title.strip()
    # Fallback: converte slug em texto legível
    return slug.replace("-", " ").title()


def find_synthesis_files(vault_alfred: Path) -> list[Path]:
    """Retorna todos os synthesis.md dentro de vault_alfred/research/."""
    research_dir = vault_alfred / "research"
    if not research_dir.exists():
        log(f"[bootstrap] Diretório research não encontrado: {research_dir}")
        return []
    return sorted(research_dir.glob("*/synthesis.md"))


# ── Main ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Bootstrap ChromaDB com pesquisas do vault")
    parser.add_argument("--dry-run", action="store_true", help="Lista arquivos sem indexar")
    parser.add_argument("--force",   action="store_true", help="Reindexar mesmo docs já existentes")
    args = parser.parse_args()

    # Carrega .env da raiz do projeto
    load_env()

    vault_alfred = Path(os.getenv("VAULT_ALFRED", ""))
    if not vault_alfred or not vault_alfred.exists():
        log(f"[bootstrap] VAULT_ALFRED inválido ou não encontrado: {vault_alfred!r}")
        sys.exit(1)

    log(f"[bootstrap] VAULT_ALFRED={vault_alfred}")
    log(f"[bootstrap] dry_run={args.dry_run}  force={args.force}")

    # Localiza arquivos
    files = find_synthesis_files(vault_alfred)
    if not files:
        log("[bootstrap] Nenhum synthesis.md encontrado — nada a fazer.")
        return

    log(f"[bootstrap] {len(files)} arquivos encontrados")

    if args.dry_run:
        for f in files:
            slug = f.parent.name
            fm   = parse_frontmatter(f)
            topic = extract_topic(fm, slug)
            log(f"  [dry-run] {slug!r:40s} → {topic!r}")
        return

    # Inicializa coleção
    cid = chroma.ensure_collection(COLLECTION_NAME)
    if not cid:
        log("[bootstrap] ChromaDB indisponível — abortando.")
        sys.exit(1)

    log(f"[bootstrap] Coleção '{COLLECTION_NAME}' pronta (id={cid[:8]}…)")

    # Verifica dimensão do embedding antes de começar
    log("[bootstrap] Verificando dimensão do embedding com nomic-embed…")
    test_emb = ollama_embed("test")
    if not test_emb:
        log("[bootstrap] ollama_embed retornou vazio — verifique se o modelo está baixado.")
        sys.exit(1)
    log(f"[bootstrap] Embedding OK — {len(test_emb)} dimensões")

    # Indexa
    ok_count = err_count = skip_count = 0

    for i, path in enumerate(files, 1):
        slug  = path.parent.name
        fm    = parse_frontmatter(path)
        topic = extract_topic(fm, slug)
        date  = str(fm.get("date", datetime.now().strftime("%Y-%m-%d")))
        vault_path = str(path.parent)

        print(f"[{i}/{len(files)}] indexando {slug!r}…", flush=True)

        embedding = ollama_embed(topic)
        if not embedding:
            log(f"  [ERRO] embedding vazio para '{topic}' — pulando")
            err_count += 1
            continue

        ok = chroma.upsert_document(
            collection_id=cid,
            doc_id=slug,
            text=topic,
            metadata={
                "slug":       slug,
                "topic":      topic,
                "date":       date,
                "vault_path": vault_path,
            },
            embedding=embedding,
        )

        if ok:
            ok_count += 1
        else:
            log(f"  [ERRO] upsert falhou para '{slug}'")
            err_count += 1

    log(f"[bootstrap] Concluído — {ok_count} indexados, {err_count} erros, {skip_count} pulados")
    log(f"[bootstrap] Coleção '{COLLECTION_NAME}' pronta para uso semântico.")


if __name__ == "__main__":
    main()
