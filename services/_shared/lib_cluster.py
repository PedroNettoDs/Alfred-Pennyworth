"""
Alfred — clustering e ranqueamento de itens de notícia.

Extraído de services/briefing/tech-briefing.py (v3.2) para ser reutilizado
pelo briefing (RSS/SearXNG) e pelo inbox-assistant (newsletters por e-mail).
Comportamento idêntico ao original; a única mudança é que `pesos_fontes`
virou parâmetro em vez de variável global.

Cada item é um dict com ao menos: title, url, snippet, source.
Campos opcionais usados no desempate: published / updated (ISO 8601).

    from _shared.lib_cluster import cluster_items, rank_clusters, build_clustered_news_text
"""

from datetime import datetime

import numpy as np
from sklearn.cluster import AgglomerativeClustering

from _shared.lib_alfred import log, ollama_embed_batch

PESO_DEFAULT = 0.5
DISTANCE_THRESHOLD = 0.50


# ── cluster_items ─────────────────────────────────────────────
def cluster_items(items: list[dict]) -> list[dict]:
    """
    Agrupa items por similaridade semântica de título via AgglomerativeClustering.

    Adiciona 'cluster_id' (int) e 'cluster_size' (int) a cada item.
    Items com embedding vazio recebem cluster_id=-1, cluster_size=1.
    Listas com menos de 2 items pulam o clustering (cluster_id=0).
    """
    if not items:
        return items

    if len(items) < 2:
        items[0]["cluster_id"]   = 0
        items[0]["cluster_size"] = 1
        return items

    # Montar textos: título curto → concatena primeiras 15 palavras do snippet
    textos = []
    for item in items:
        title = item.get("title", "")
        if len(title.split()) < 4:
            snippet_words = item.get("snippet", "").split()[:15]
            text = (title + " " + " ".join(snippet_words)).strip()
        else:
            text = title
        textos.append(text)

    log(f"[cluster] Gerando {len(textos)} embeddings...")
    embeddings_raw = ollama_embed_batch(textos)

    valid_idx   = [i for i, e in enumerate(embeddings_raw) if e]
    invalid_idx = [i for i, e in enumerate(embeddings_raw) if not e]

    # Itens sem embedding → cluster solo
    for i in invalid_idx:
        items[i]["cluster_id"]   = -1
        items[i]["cluster_size"] = 1

    if len(valid_idx) < 2:
        for i in valid_idx:
            items[i]["cluster_id"]   = 0
            items[i]["cluster_size"] = len(valid_idx)
        return items

    X = np.array([embeddings_raw[i] for i in valid_idx], dtype=np.float32)

    clustering = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=DISTANCE_THRESHOLD,
        metric="cosine",
        linkage="average",
    )
    labels = clustering.fit_predict(X)

    for pos, idx in enumerate(valid_idx):
        items[idx]["cluster_id"] = int(labels[pos])

    cluster_counts: dict[int, int] = {}
    for i in valid_idx:
        cid = items[i]["cluster_id"]
        cluster_counts[cid] = cluster_counts.get(cid, 0) + 1

    for i in valid_idx:
        items[i]["cluster_size"] = cluster_counts[items[i]["cluster_id"]]

    n_clusters = len(set(labels))
    log(f"[cluster] {len(valid_idx)} itens → {n_clusters} clusters "
        f"(+{len(invalid_idx)} solos sem embedding)")
    return items


# ── rank_clusters ─────────────────────────────────────────────
def rank_clusters(items: list[dict], pesos_fontes: dict[str, float]) -> list[int]:
    """
    Ranqueia clusters por (tamanho × peso_médio_fontes).

    Desempate 1: cluster com item mais recente (campo published/updated).
                 Se não tiver, usa índice de chegada como proxy
                 (menor índice = mais recente, pois RSS retorna newest-first).
    Desempate 2: ordem alfabética do título-âncora (item de maior peso).

    Retorna lista de cluster_ids ordenados do mais para o menos relevante.
    """
    if not items:
        return []

    clusters: dict[int, list[tuple[int, dict]]] = {}
    for idx, item in enumerate(items):
        cid = item.get("cluster_id", -1)
        clusters.setdefault(cid, []).append((idx, item))

    scores = []
    for cid, pairs in clusters.items():
        idxs         = [idx for idx, _ in pairs]
        cluster_itms = [item for _, item in pairs]

        weights  = [pesos_fontes.get(i.get("source", ""), PESO_DEFAULT) for i in cluster_itms]
        avg_w    = sum(weights) / len(weights)
        score    = len(cluster_itms) * avg_w

        best_ts: float | None = None
        for item in cluster_itms:
            for campo in ("published", "updated"):
                val = item.get(campo)
                if not val:
                    continue
                try:
                    if isinstance(val, str):
                        ts = datetime.fromisoformat(val).timestamp()
                        if best_ts is None or ts > best_ts:
                            best_ts = ts
                except Exception:
                    pass

        min_idx = min(idxs)

        anchor = max(cluster_itms, key=lambda i: pesos_fontes.get(i.get("source", ""), PESO_DEFAULT))
        anchor_title = anchor.get("title", "")

        scores.append({
            "cid":          cid,
            "score":        score,
            "best_ts":      best_ts,
            "min_idx":      min_idx,
            "anchor_title": anchor_title,
        })

    def _sort_key(s: dict):
        if s["best_ts"] is not None:
            date_key = -s["best_ts"]
        else:
            date_key = float(s["min_idx"])
        return (-s["score"], date_key, s["anchor_title"])

    scores.sort(key=_sort_key)
    return [s["cid"] for s in scores]


# ── build_clustered_news_text ─────────────────────────────────
def build_clustered_news_text(
    items: list[dict],
    ranked_cluster_ids: list[int],
    pesos_fontes: dict[str, float],
    max_destaques: int = 10,
) -> tuple[str, str]:
    """
    Monta duas strings a partir dos clusters ranqueados.

    news_text_destaques: top max_destaques clusters, formato TEMA N com todas as
                         fontes do cluster listadas.
    news_text_radar:     clusters restantes, uma linha por item.
                         Vazia se não sobrar nenhum cluster.
    """
    clusters: dict[int, list[dict]] = {}
    for item in items:
        cid = item.get("cluster_id", -1)
        clusters.setdefault(cid, []).append(item)

    destaque_ids = ranked_cluster_ids[:max_destaques]
    radar_ids    = ranked_cluster_ids[max_destaques:]

    destaques_parts: list[str] = []
    for tema_num, cid in enumerate(destaque_ids, 1):
        cluster_itms = clusters.get(cid, [])
        if not cluster_itms:
            continue

        sorted_itms = sorted(
            cluster_itms,
            key=lambda i: pesos_fontes.get(i.get("source", ""), PESO_DEFAULT),
            reverse=True,
        )
        anchor = sorted_itms[0]
        lines  = [f"NOTÍCIA: {anchor.get('title', '')} | URL: {anchor.get('url', '')}"]
        for item in sorted_itms:
            source  = item.get("source", "")
            title   = item.get("title", "")
            url     = item.get("url", "")
            snippet = item.get("snippet", "")[:300]
            lines.append(f"  [{source}] {title} | {url}\n  Contexto: {snippet}")

        destaques_parts.append("\n".join(lines))

    news_text_destaques = "\n\n".join(destaques_parts)

    radar_lines: list[str] = []
    for cid in radar_ids:
        for item in clusters.get(cid, []):
            source = item.get("source", "")
            title  = item.get("title", "")
            url    = item.get("url", "")
            radar_lines.append(f"- [{source}] {title} — {url}")

    news_text_radar = "\n".join(radar_lines)

    return news_text_destaques, news_text_radar
