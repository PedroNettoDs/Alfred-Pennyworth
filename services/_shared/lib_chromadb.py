"""
Alfred — wrapper minimalista para ChromaDB.

Usa a API HTTP v2 do ChromaDB (não o client Python) pra manter dependências
mínimas. Todas as funções degradam graciosamente se o ChromaDB estiver offline:
retornam None/False/[] e logam aviso, nunca crasham.

Compatibilidade: tenta v2 primeiro (API atual), fallback para v1 (versões antigas).

Import pattern (caller deve ter services/ no sys.path):
    from _shared.lib_chromadb import ensure_collection, upsert_document, query_similar
"""

import os
from typing import Optional

import httpx

# Log via lib_alfred quando disponível, fallback inline pra evitar dep circular
try:
    from _shared.lib_alfred import log
except ImportError:
    from datetime import datetime as _dt
    def log(msg: str):  # type: ignore[misc]
        print(f"[{_dt.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Config ────────────────────────────────────────────────────
_DEFAULT_CHROMADB_URL = "http://localhost:8000"

# Cache de collection_id por nome (evita round-trip em cada request)
_collection_cache: dict[str, str] = {}


def _url() -> str:
    return os.getenv("CHROMADB_URL", _DEFAULT_CHROMADB_URL)


def _v2_base() -> str:
    return f"{_url()}/api/v2/tenants/default_tenant/databases/default_database"


# ── ensure_collection ─────────────────────────────────────────
def ensure_collection(name: str) -> Optional[str]:
    """
    Cria ou recupera uma coleção pelo nome, com espaço de distância coseno.

    Tenta v2 primeiro (API atual), fallback para v1 (versões mais antigas).
    Retorna o collection_id (str) ou None se ChromaDB estiver offline.
    """
    if name in _collection_cache:
        return _collection_cache[name]

    payload = {
        "name": name,
        "get_or_create": True,
        "metadata": {"hnsw:space": "cosine"},  # distância coseno para texto
    }

    try:
        with httpx.Client(timeout=15) as client:
            # v2 (API atual)
            resp = client.post(f"{_v2_base()}/collections", json=payload)
            if resp.status_code in (200, 201):
                cid = resp.json().get("id", "")
                if cid:
                    _collection_cache[name] = cid
                    return cid

            # v1 fallback (ChromaDB < 0.5)
            resp = client.post(f"{_url()}/api/v1/collections", json=payload)
            if resp.status_code in (200, 201):
                cid = resp.json().get("id", "")
                if cid:
                    _collection_cache[name] = cid
                    return cid

            log(f"[chromadb] Falha ao criar/recuperar '{name}': {resp.status_code} {resp.text[:150]}")
    except Exception as e:
        log(f"[chromadb] Offline ou erro em ensure_collection('{name}'): {e}")

    return None


# ── upsert_document ───────────────────────────────────────────
def upsert_document(
    collection_id: str,
    doc_id: str,
    text: str,
    metadata: dict,
    embedding: list[float],
) -> bool:
    """
    Insere ou atualiza um documento no ChromaDB.

    :param collection_id: ID da coleção (retornado por ensure_collection)
    :param doc_id:        ID único do documento (slug pra research)
    :param text:          Texto do documento (pra exibição — não afeta distância)
    :param metadata:      Dict com campos extras (slug, topic, date, vault_path)
    :param embedding:     Embedding pré-calculado via Ollama
    :return: True em sucesso, False em falha
    """
    if not collection_id or not embedding:
        return False

    # ChromaDB rejeita valores não-escalares em metadata
    safe_meta = {k: str(v) for k, v in metadata.items()}

    payload = {
        "ids":        [doc_id],
        "embeddings": [embedding],
        "documents":  [text],
        "metadatas":  [safe_meta],
    }

    try:
        with httpx.Client(timeout=30) as client:
            # v2
            resp = client.post(
                f"{_v2_base()}/collections/{collection_id}/upsert", json=payload
            )
            if resp.status_code in (200, 201):
                return True

            # v1 fallback
            resp = client.post(
                f"{_url()}/api/v1/collections/{collection_id}/upsert", json=payload
            )
            if resp.status_code in (200, 201):
                return True

            log(f"[chromadb] Erro upsert '{doc_id}': {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log(f"[chromadb] Erro em upsert_document('{doc_id}'): {e}")

    return False


# ── query_similar ─────────────────────────────────────────────
def query_similar(
    collection_id: str,
    embedding: list[float],
    n_results: int = 3,
    where: Optional[dict] = None,
) -> list[dict]:
    """
    Busca os n_results documentos mais similares ao embedding dado.

    Com espaço coseno: distance = 1 - cosine_similarity
        distance = 0.0 → idênticos  (similarity = 1.0)
        distance = 0.25 → threshold (similarity = 0.75)
        distance = 1.0 → ortogonais (similarity = 0.0)

    Retorna lista ordenada por distância (menor = mais similar):
        [{"id": str, "distance": float, "metadata": dict, "document": str}, ...]

    Retorna [] se ChromaDB offline, coleção vazia ou n_results > tamanho da coleção.
    """
    if not collection_id or not embedding:
        return []

    payload: dict = {
        "query_embeddings": [embedding],
        "n_results":        n_results,
        "include":          ["distances", "metadatas", "documents"],
    }
    if where:
        payload["where"] = where

    def _parse_response(data: dict) -> list[dict]:
        ids       = (data.get("ids")       or [[]])[0]
        distances = (data.get("distances") or [[]])[0]
        metadatas = (data.get("metadatas") or [[]])[0]
        documents = (data.get("documents") or [[]])[0]
        return [
            {
                "id":       ids[i],
                "distance": distances[i] if i < len(distances) else 1.0,
                "metadata": metadatas[i] if i < len(metadatas) else {},
                "document": documents[i] if i < len(documents) else "",
            }
            for i in range(len(ids))
        ]

    try:
        with httpx.Client(timeout=20) as client:
            # v2
            resp = client.post(
                f"{_v2_base()}/collections/{collection_id}/query", json=payload
            )
            if resp.status_code == 200:
                return _parse_response(resp.json())

            # n_results > tamanho da coleção → silencioso
            if resp.status_code == 400 and "number of requested results" in resp.text:
                return []

            # v1 fallback
            resp = client.post(
                f"{_url()}/api/v1/collections/{collection_id}/query", json=payload
            )
            if resp.status_code == 200:
                return _parse_response(resp.json())

            log(f"[chromadb] Erro query: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log(f"[chromadb] Erro em query_similar: {e}")

    return []


# ── delete_document ───────────────────────────────────────────
def delete_document(collection_id: str, doc_id: str) -> bool:
    """
    Remove um documento da coleção. Útil pra reindexação forçada.
    Retorna True em sucesso, False em falha.
    """
    if not collection_id:
        return False

    payload = {"ids": [doc_id]}

    try:
        with httpx.Client(timeout=15) as client:
            # v2
            resp = client.post(
                f"{_v2_base()}/collections/{collection_id}/delete", json=payload
            )
            if resp.status_code in (200, 201):
                return True

            # v1 fallback
            resp = client.post(
                f"{_url()}/api/v1/collections/{collection_id}/delete", json=payload
            )
            if resp.status_code in (200, 201):
                return True

            log(f"[chromadb] Erro delete '{doc_id}': {resp.status_code}")
    except Exception as e:
        log(f"[chromadb] Erro em delete_document('{doc_id}'): {e}")

    return False
