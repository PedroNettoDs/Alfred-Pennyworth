#!/usr/bin/env python3
"""
vault-indexer.py — Indexa os vaults do Obsidian no ChromaDB via embeddings nomic-embed-text.

Uso:
  python3 vault-indexer.py              # indexa ambos os vaults
  python3 vault-indexer.py alfred       # só o vault do Alfred
  python3 vault-indexer.py pedro        # só o vault do Pedro

Variáveis de ambiente (opcionais — os defaults já funcionam para o projeto Alfred):
  OLLAMA_URL      URL base do Ollama          (default: http://localhost:11434)
  CHROMADB_URL    URL base do ChromaDB        (default: http://localhost:8000)
  VAULT_ALFRED    Caminho do vault do Alfred  (default: /mnt/SSD/alfred/vaults/alfred)
  VAULT_PEDRO     Caminho do vault do Pedro   (default: ~/Obsidian - MrNotte)
  COLLECTION      Nome da coleção no Chroma   (default: alfred-brain)
  CHUNK_SIZE      Palavras por chunk          (default: 300)
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ── Configuração ──────────────────────────────────────────────
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434")
CHROMADB_URL = os.getenv("CHROMADB_URL", "http://localhost:8000")
COLLECTION   = os.getenv("COLLECTION",   "alfred-brain")
CHUNK_SIZE   = int(os.getenv("CHUNK_SIZE", "300"))

VAULTS = {
    "alfred": Path(os.getenv("VAULT_ALFRED", "/mnt/SSD/alfred/vaults/alfred")),
    "pedro":  Path(os.getenv("VAULT_PEDRO",  Path.home() / "Obsidian - MrNotte")),
}

# ── Utilitários ───────────────────────────────────────────────

def log(msg: str):
    """Imprime com timestamp — capturado pelo systemd/journald."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def strip_frontmatter(content: str) -> tuple[str, dict]:
    """
    Separa o frontmatter YAML do conteúdo real.
    Retorna (conteúdo_limpo, metadados_extraídos).
    """
    meta = {}
    if not content.startswith("---"):
        return content, meta

    end = content.find("---", 3)
    if end == -1:
        return content, meta

    fm_block = content[3:end].strip()
    body     = content[end + 3:].strip()

    # Extrai campos simples do frontmatter para metadados
    for line in fm_block.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()

    return body, meta


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[str]:
    """
    Divide o texto em chunks de ~chunk_size palavras,
    respeitando quebras de parágrafo quando possível.
    """
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks  = []
    current = []
    count   = 0

    for para in paragraphs:
        words = para.split()
        if count + len(words) > chunk_size and current:
            chunks.append(" ".join(current))
            current = []
            count   = 0
        current.extend(words)
        count += len(words)

    if current:
        chunks.append(" ".join(current))

    # Parágrafo único maior que chunk_size: divide por palavras mesmo
    result = []
    for chunk in chunks:
        words = chunk.split()
        if len(words) <= chunk_size:
            result.append(chunk)
        else:
            for i in range(0, len(words), chunk_size):
                result.append(" ".join(words[i:i + chunk_size]))

    return result or [""]


# ── ChromaDB ──────────────────────────────────────────────────

def ensure_collection() -> bool:
    """Garante que a coleção existe no ChromaDB. Retorna True se ok."""
    try:
        # Tenta criar — se já existir, o ChromaDB retorna 200 ou o body indica que existe
        resp = requests.post(
            f"{CHROMADB_URL}/api/v2/collections",
            json={"name": COLLECTION, "metadata": {"description": "Alfred Brain Vault"}},
            timeout=10,
        )
        if resp.status_code in (200, 201):
            log(f"Coleção '{COLLECTION}' pronta.")
            return True
        if resp.status_code == 409:
            log(f"Coleção '{COLLECTION}' já existe.")
            return True
        log(f"[erro] Não foi possível criar coleção: {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        log(f"[erro] ChromaDB inacessível: {e}")
        return False


def get_collection_id() -> str | None:
    """Busca o UUID interno da coleção (necessário para upsert)."""
    try:
        resp = requests.get(
            f"{CHROMADB_URL}/api/v2/collections/{COLLECTION}",
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("id")
        log(f"[erro] Coleção não encontrada: {resp.status_code}")
        return None
    except Exception as e:
        log(f"[erro] get_collection_id: {e}")
        return None


def upsert_chunks(
    collection_id: str,
    chunks: list[str],
    embeddings: list[list[float]],
    base_id: str,
    metadata: dict,
) -> bool:
    """Faz upsert de todos os chunks de uma nota na coleção."""
    ids         = [f"{base_id}__chunk{i}" for i in range(len(chunks))]
    meta_list   = [{**metadata, "chunk": i, "total_chunks": len(chunks)} for i in range(len(chunks))]

    try:
        resp = requests.post(
            f"{CHROMADB_URL}/api/v2/collections/{collection_id}/upsert",
            json={
                "ids":        ids,
                "embeddings": embeddings,
                "documents":  chunks,
                "metadatas":  meta_list,
            },
            timeout=30,
        )
        return resp.status_code in (200, 201)
    except Exception as e:
        log(f"[erro] upsert: {e}")
        return False


# ── Ollama Embeddings ─────────────────────────────────────────

def embed(texts: list[str]) -> list[list[float]] | None:
    """
    Gera embeddings para uma lista de textos via nomic-embed-text.
    Retorna lista de vetores ou None em caso de erro.
    """
    vectors = []
    for text in texts:
        try:
            resp = requests.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": "nomic-embed-text", "prompt": text},
                timeout=60,
            )
            if resp.status_code == 200:
                vectors.append(resp.json()["embedding"])
            else:
                log(f"[erro] embedding falhou: {resp.status_code} {resp.text[:100]}")
                return None
        except Exception as e:
            log(f"[erro] embed: {e}")
            return None
    return vectors


# ── Indexação ─────────────────────────────────────────────────

def index_vault(name: str, vault_path: Path, collection_id: str) -> tuple[int, int, int]:
    """
    Indexa todos os .md de um vault.
    Retorna (indexados, pulados_curtos, erros).
    """
    if not vault_path.exists():
        log(f"[{name}] Vault não encontrado: {vault_path}")
        return 0, 0, 0

    files = sorted(vault_path.rglob("*.md"))
    log(f"[{name}] {len(files)} arquivo(s) encontrado(s) em {vault_path}")

    indexados = 0
    pulados   = 0
    erros     = 0

    for md_file in files:
        try:
            raw = md_file.read_text(encoding="utf-8", errors="ignore").strip()

            # Pula notas muito curtas (sem conteúdo real)
            if len(raw.split()) < 20:
                pulados += 1
                continue

            body, meta = strip_frontmatter(raw)

            # Monta metadados para o ChromaDB
            metadata = {
                "vault":    name,
                "filename": md_file.name,
                "path":     str(md_file.relative_to(vault_path)),
                "stem":     md_file.stem,
                **{k: v for k, v in meta.items() if k in ("title", "date", "tags", "topics", "folder")},
            }

            # ID base: vault + path relativo (garante unicidade entre vaults)
            base_id = f"{name}__{md_file.relative_to(vault_path)}".replace("/", "__").replace(" ", "_")

            chunks = chunk_text(body)
            vectors = embed(chunks)

            if vectors is None:
                log(f"  [erro] embedding falhou: {md_file.name}")
                erros += 1
                continue

            ok = upsert_chunks(collection_id, chunks, vectors, base_id, metadata)
            if ok:
                log(f"  [ok] {md_file.relative_to(vault_path)} ({len(chunks)} chunk(s))")
                indexados += 1
            else:
                log(f"  [erro] upsert falhou: {md_file.name}")
                erros += 1

        except Exception as e:
            log(f"  [erro] {md_file.name}: {e}")
            erros += 1

    return indexados, pulados, erros


# ── Entry point ───────────────────────────────────────────────

def main():
    target = sys.argv[1].lower() if len(sys.argv) > 1 else "both"
    if target not in ("alfred", "pedro", "both"):
        print(f"Uso: {sys.argv[0]} [alfred|pedro|both]")
        sys.exit(1)

    log("=== vault-indexer iniciando ===")
    log(f"ChromaDB : {CHROMADB_URL}")
    log(f"Ollama   : {OLLAMA_URL}")
    log(f"Coleção  : {COLLECTION}")
    log(f"Target   : {target}")

    if not ensure_collection():
        sys.exit(1)

    collection_id = get_collection_id()
    if not collection_id:
        log("[erro] Não foi possível obter o ID da coleção. Abortando.")
        sys.exit(1)

    log(f"Collection ID: {collection_id}")

    total_idx = total_skip = total_err = 0

    vaults_to_run = {k: v for k, v in VAULTS.items() if target in (k, "both")}

    for name, path in vaults_to_run.items():
        idx, skip, err = index_vault(name, path, collection_id)
        total_idx  += idx
        total_skip += skip
        total_err  += err

    log("=== Concluído ===")
    log(f"Indexados: {total_idx} | Pulados (curtos): {total_skip} | Erros: {total_err}")


if __name__ == "__main__":
    main()