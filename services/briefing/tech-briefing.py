#!/usr/bin/env python3
"""
Alfred Tech Briefing v3.2 — templates, título via Ollama, modo ad-hoc.

Uso:
    python tech-briefing.py                    → default matinal_tech
    python tech-briefing.py matinal_tech       → briefing nomeado
    python tech-briefing.py vespertino_mercado → briefing nomeado
    python tech-briefing.py "fusão nuclear"    → modo ad-hoc (tema livre)
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
import numpy as np
from sklearn.cluster import AgglomerativeClustering
import yaml

# ── Shared libs ───────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared.lib_alfred import (
    log, slugify, ollama_generate, ollama_embed_batch, searxng_search,
    deduplicate_by_url_and_title, vault_write,
)
from _shared.perfis import get_briefing, list_all
from _shared.lib_templates import render_template

import edge_tts

SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
CONFIG_FILE  = SCRIPT_DIR / "config.yml"
VAULT_ALFRED = Path(os.getenv("VAULT_ALFRED", ""))

SEEN_FILE         = PROJECT_ROOT / "logs" / "briefing_seen.json"
SEEN_WINDOW_HOURS = 24


# ── Pesos de fontes para ranqueamento de clusters ─────────────
# Fonte não listada usa default 0.5. Migra pro perfis.yml no PR seguinte.
PESOS_FONTES: dict[str, float] = {
    "MIT News":        1.0,
    "Ars Technica":    0.95,
    "Hacker News":     0.9,
    "The Verge":       0.85,
    "InfoQ":           0.85,
    "MIT Tech Review": 0.85,
    "TechCrunch":      0.75,
    "Lobsters":        0.75,
    "TLDR":            0.7,
    "Tecnoblog":       0.7,
}
_PESO_DEFAULT = 0.5


# ── Config ────────────────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Extração de título — heurística (fallback) ────────────────
# Mantida como fallback interno de generate_title_via_llm. NÃO REMOVER.
NOMES_SECAO = {
    "manchete do dia", "manchete", "destaques", "destaque",
    "tendências", "tendencias", "vale ficar de olho",
    "leitura recomendada", "leituras recomendadas",
    "análise", "analise", "resumo", "briefing",
    "tech briefing", "briefing matinal", "briefing de tecnologia",
    "notícias", "noticias", "headlines", "top stories",
    "sobre o tema", "pontos principais", "conexões interessantes",
    "para saber mais", "panorama do dia", "movimentos relevantes",
    "oportunidades sinalizadas", "para acompanhar",
}


def extrair_titulo_manchete(briefing: str) -> str:
    """Extrai título por heurística. Usado como fallback do generate_title_via_llm."""
    lines = briefing.strip().split("\n")

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        clean = re.sub(r"[*_\[\]()\"']", "", line).strip()
        if len(clean) < 10:
            continue
        if clean.lower().rstrip(":").strip() in NOMES_SECAO:
            continue
        if clean.startswith(">") or clean.startswith("-"):
            continue
        if ". " in clean:
            clean = clean[:clean.index(". ")]
        elif clean.endswith("."):
            clean = clean[:-1]
        return clean[:100]

    encontrou_manchete = False
    for line in lines:
        line = line.strip()
        if re.match(r"^#+\s*manchete", line, re.IGNORECASE):
            encontrou_manchete = True
            continue
        if encontrou_manchete and line:
            clean = re.sub(r"[*_\[\]()\"']", "", line).strip()
            if len(clean) > 10 and clean.lower().rstrip(":") not in NOMES_SECAO:
                if ". " in clean:
                    clean = clean[:clean.index(". ")]
                return clean[:100]

    return "Tech Briefing"


# ── Título via Ollama (JSON) ───────────────────────────────────
def generate_title_via_llm(briefing_md: str) -> str:
    """
    Segunda chamada Ollama: extrai título do briefing como JSON.
    Fallback pra extrair_titulo_manchete() em caso de falha no parse.
    """
    prompt = (
        "Leia o briefing abaixo e extraia apenas o título principal (manchete) "
        "da notícia ou tema mais importante. Responda em JSON válido:\n\n"
        '{"titulo": "título curto aqui, sem markdown"}\n\n'
        "Regras:\n"
        "- Máximo 80 caracteres\n"
        "- Sem markdown, sem aspas extras, sem prefixos\n"
        "- Em português brasileiro\n"
        "- Deve ser uma frase informativa e específica\n\n"
        f"BRIEFING:\n{briefing_md[:2000]}"
    )

    raw = ollama_generate(prompt, timeout=60)
    try:
        match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            titulo = (data.get("titulo") or "").strip()
            if titulo and len(titulo) > 5:
                log(f"[titulo] Via JSON: {titulo[:60]}")
                return titulo[:100]
    except Exception as e:
        log(f"[titulo] Falha no parse JSON ({e}) — usando fallback heurístico")

    log("[titulo] Usando fallback extrair_titulo_manchete()")
    return extrair_titulo_manchete(briefing_md)


# ── RSS ───────────────────────────────────────────────────────
def fetch_rss(feed_config: dict) -> list[dict]:
    name      = feed_config.get("name") or feed_config.get("nome") or "RSS"
    url       = feed_config.get("url", "")
    max_items = feed_config.get("max_items") or feed_config.get("max") or 5

    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            summary = entry.get("summary", entry.get("description", ""))
            summary = re.sub(r"<[^>]+>", "", summary)[:400]
            items.append({
                "title":   entry.get("title", ""),
                "url":     entry.get("link", ""),
                "snippet": summary,
                "source":  name,
            })
        log(f"[RSS] {name}: {len(items)} itens")
        return items
    except Exception as e:
        log(f"[RSS] Erro em {name}: {e}")
        return []


# ── TTS ───────────────────────────────────────────────────────
async def generate_tts(text: str, voice: str, output_path: Path):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))


# ── Memória deslizante de 24h ─────────────────────────────────

def load_seen() -> dict:
    """
    Carrega logs/briefing_seen.json, purgando entradas com mais de SEEN_WINDOW_HOURS.
    Retorna dict {url: iso_timestamp}. Em caso de erro ou arquivo inexistente, retorna {}.
    """
    if not SEEN_FILE.exists():
        return {}

    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log(f"[seen] Erro lendo {SEEN_FILE}: {e} — começando vazio")
        return {}

    if not isinstance(data, dict):
        log(f"[seen] {SEEN_FILE} não é um dict — começando vazio")
        return {}

    cutoff = datetime.now() - timedelta(hours=SEEN_WINDOW_HOURS)
    purged = {}
    for url, ts in data.items():
        try:
            if datetime.fromisoformat(ts) > cutoff:
                purged[url] = ts
        except (ValueError, TypeError):
            continue  # ignora entradas com timestamp malformado

    removed = len(data) - len(purged)
    if removed > 0:
        log(f"[seen] Purgado {removed} entradas > {SEEN_WINDOW_HOURS}h")

    return purged


def save_seen(seen: dict):
    """Persiste dict em logs/briefing_seen.json. Cria diretório se não existir."""
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)


def split_new_vs_continuation(
    items: list[dict], seen: dict
) -> tuple[list[dict], list[dict]]:
    """
    Separa items em (novas, continuacoes) com base no seen dict.
    URL é a chave. Itens sem URL são tratados como novas.
    """
    novas, continuacoes = [], []
    for item in items:
        url = item.get("url", "")
        if not url or url not in seen:
            novas.append(item)
        else:
            continuacoes.append(item)
    return novas, continuacoes


def mark_seen(items: list[dict], seen: dict):
    """Adiciona/atualiza URLs dos items no dict seen com timestamp ISO atual."""
    now_iso = datetime.now().isoformat()
    for item in items:
        url = item.get("url", "")
        if url:
            seen[url] = now_iso


def format_items_long(items: list[dict]) -> str:
    """Formato longo pras notícias novas — título + fonte + URL + snippet."""
    if not items:
        return ""
    return "\n\n".join(
        f"[{i+1}] {item.get('title', '')}\n"
        f"Fonte: {item.get('source', '')}\n"
        f"URL: {item.get('url', '')}\n"
        f"{item.get('snippet', '')}"
        for i, item in enumerate(items)
    )


def format_items_short(items: list[dict]) -> str:
    """Formato curto pras continuações — uma linha por item."""
    if not items:
        return ""
    return "\n".join(
        f"- [{item.get('title', 'sem título')}]({item.get('url', '')}) — {item.get('source', '')}"
        for item in items
    )


def strip_acompanhamento_section(text: str) -> str:
    """
    Remove programaticamente qualquer variação da seção Acompanhamento:
      ## Acompanhamento, **ACOMPANHAMENTO**, **Acompanhamento**, etc.
    Usada quando news_text_continuacoes está vazio.
    """
    # Padrão largo: qualquer linha que seja só o header de Acompanhamento
    # seguida pelo conteúdo até o próximo header markdown/bold ou fim
    return re.sub(
        r"\n?(?:#{1,3}\s*|(?:\*{1,2})?)Acompanhamento\b(?:\*{1,2})?"
        r".*?(?=\n#{1,3}\s|\n\*{1,2}[A-Z]|\Z)",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()


# ── Clustering e ranqueamento de notícias ─────────────────────

def _cluster_items(items: list[dict]) -> list[dict]:
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
        # Não há embeddings suficientes para clusterizar
        for i in valid_idx:
            items[i]["cluster_id"]   = 0
            items[i]["cluster_size"] = len(valid_idx)
        return items

    X = np.array([embeddings_raw[i] for i in valid_idx], dtype=np.float32)

    clustering = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=0.50,
        metric="cosine",
        linkage="average",
    )
    labels = clustering.fit_predict(X)

    for pos, idx in enumerate(valid_idx):
        items[idx]["cluster_id"] = int(labels[pos])

    # Calcular tamanho de cada cluster
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


def _rank_clusters(items: list[dict], pesos_fontes: dict[str, float]) -> list[int]:
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

    # Agrupar por cluster_id, preservando índice original como proxy de data
    clusters: dict[int, list[tuple[int, dict]]] = {}
    for idx, item in enumerate(items):
        cid = item.get("cluster_id", -1)
        clusters.setdefault(cid, []).append((idx, item))

    scores = []
    for cid, pairs in clusters.items():
        idxs         = [idx for idx, _ in pairs]
        cluster_itms = [item for _, item in pairs]

        weights  = [pesos_fontes.get(i.get("source", ""), _PESO_DEFAULT) for i in cluster_itms]
        avg_w    = sum(weights) / len(weights)
        score    = len(cluster_itms) * avg_w

        # Desempate por data — tenta campos published/updated; fallback: min_idx
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

        # Se nenhum campo de data disponível, usa min_idx (menor = mais recente)
        min_idx = min(idxs)

        # Título-âncora = item de maior peso no cluster
        anchor = max(cluster_itms, key=lambda i: pesos_fontes.get(i.get("source", ""), _PESO_DEFAULT))
        anchor_title = anchor.get("title", "")

        scores.append({
            "cid":          cid,
            "score":        score,
            "best_ts":      best_ts,
            "min_idx":      min_idx,
            "anchor_title": anchor_title,
        })

    def _sort_key(s: dict):
        # Score desc; depois data desc (best_ts negate) ou min_idx asc; depois título asc
        if s["best_ts"] is not None:
            date_key = -s["best_ts"]
        else:
            date_key = float(s["min_idx"])  # menor índice = mais recente = melhor
        return (-s["score"], date_key, s["anchor_title"])

    scores.sort(key=_sort_key)
    return [s["cid"] for s in scores]


def _build_clustered_news_text(
    items: list[dict],
    ranked_cluster_ids: list[int],
    max_destaques: int = 10,
) -> tuple[str, str]:
    """
    Monta duas strings a partir dos clusters ranqueados.

    news_text_destaques: top max_destaques clusters, formato TEMA N com todas as
                         fontes do cluster listadas.
    news_text_radar:     clusters restantes, uma linha por item.
                         Vazia se não sobrar nenhum cluster.
    """
    # Agrupar por cluster_id
    clusters: dict[int, list[dict]] = {}
    for item in items:
        cid = item.get("cluster_id", -1)
        clusters.setdefault(cid, []).append(item)

    destaque_ids = ranked_cluster_ids[:max_destaques]
    radar_ids    = ranked_cluster_ids[max_destaques:]

    # ── Destaques ──
    destaques_parts: list[str] = []
    for tema_num, cid in enumerate(destaque_ids, 1):
        cluster_itms = clusters.get(cid, [])
        if not cluster_itms:
            continue

        # Ordenar por peso decrescente — item de maior peso = âncora
        sorted_itms = sorted(
            cluster_itms,
            key=lambda i: PESOS_FONTES.get(i.get("source", ""), _PESO_DEFAULT),
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

    # ── Radar ──
    radar_lines: list[str] = []
    for cid in radar_ids:
        for item in clusters.get(cid, []):
            source = item.get("source", "")
            title  = item.get("title", "")
            url    = item.get("url", "")
            radar_lines.append(f"- [{source}] {title} — {url}")

    news_text_radar = "\n".join(radar_lines)

    return news_text_destaques, news_text_radar


# ── Extração de tópicos do briefing gerado ───────────────────
def _extrair_topicos_do_briefing(briefing_text: str, n: int = 3) -> list[str]:
    """
    Extrai N conceitos/termos-chave CURTOS do briefing para definição posterior.
    Retorna nomes próprios, tecnologias ou conceitos específicos — nunca títulos completos.
    """
    prompt = (
        f"Leia o briefing abaixo e extraia exatamente {n} CONCEITOS-CHAVE curtos (1 a 3 palavras cada).\n"
        f"Regras:\n"
        f"- Extraia NOMES PRÓPRIOS, TECNOLOGIAS ou CONCEITOS ESPECÍFICOS mencionados\n"
        f"- NUNCA copie títulos ou frases longas — apenas o conceito central\n"
        f"- Prefira termos que um leitor possa não conhecer e que merecem explicação\n"
        f'- Responda APENAS um JSON array. Exemplo: ["Artemis II", "Windows Insider", "mantle plume"]\n\n'
        f"BRIEFING:\n{briefing_text[:2000]}"
    )
    raw = ollama_generate(prompt, timeout=60).strip()
    try:
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if match:
            topicos = json.loads(match.group())
            if isinstance(topicos, list):
                # Filtra entradas longas (mais de 5 palavras = provavelmente um título)
                return [str(t).strip() for t in topicos if t and len(str(t).split()) <= 5][:n]
    except Exception as e:
        log(f"[Palavras-chave] Falha ao extrair tópicos: {e}")
    return []


# ── Seção de palavras-chave (substitui Radar vazio) ──────────
def _build_keywords_section(interesses: list[str], max_termos: int = 3) -> str:
    """
    Quando não há itens de Radar, para cada interesse do perfil:
      1. Busca 1 resultado no SearXNG com query "o que é: <termo>"
      2. Usa o snippet como contexto e pede ao LLM uma definição concisa
    Retorna string vazia se SearXNG e LLM não responderem.
    """
    if not interesses:
        return ""

    termos = interesses[:max_termos]
    blocos: list[str] = []

    for termo in termos:
        resultados = searxng_search(f"o que é: {termo}", categories="general,it", max_results=1)

        snippet_ctx = ""
        link_ref    = ""
        if resultados:
            r = resultados[0]
            snippet_ctx = r.get("snippet", "").strip()[:400]
            url   = r.get("url", "")
            title = r.get("title", "").strip()
            if url and title:
                link_ref = f"\n\n*Referência: [{title}]({url})*"

        ctx_bloco = (
            f"\n\nContexto encontrado na web (use SOMENTE se for claramente sobre '{termo}' — "
            f"se o conteúdo não tiver relação direta, IGNORE completamente e use seu próprio conhecimento):\n{snippet_ctx}"
            if snippet_ctx else ""
        )
        prompt = (
            f"Em 2-3 frases diretas, explique o que é '{termo}' para um desenvolvedor."
            f"{ctx_bloco}"
            f"\n\nResponda apenas a definição, sem introdução nem prefixo."
        )

        definicao = ollama_generate(prompt, timeout=60).strip()
        if not definicao:
            continue

        blocos.append(f"### {termo}\n\n{definicao}{link_ref}")

    if not blocos:
        return ""

    return "## Palavras-chave\n\n" + "\n\n".join(blocos)


# ── Fluxo briefing nomeado ────────────────────────────────────
def _run_briefing_nomeado(briefing_cfg: dict, briefing_name: str):
    perfil = briefing_cfg["_perfil"]
    fontes = briefing_cfg["_fontes"]

    today      = datetime.now()
    date_str   = today.strftime("%Y-%m-%d")
    time_str   = today.strftime("%H:%M")
    date_human = today.strftime("%d/%m/%Y")
    timestamp  = today.strftime("%Y-%m-%d_%H%M")

    log(f"=== Briefing '{briefing_name}' (perfil: {briefing_cfg['perfil']}) — {date_human} {time_str} ===")

    config = load_config()

    # ── Coletar ───────────────────────────────────────────────
    all_items: list[dict] = []
    for fonte in fontes:
        tipo = fonte.get("tipo", "")
        if tipo == "searxng":
            for q in fonte.get("queries", []):
                results = searxng_search(q["q"], categories=q.get("cat", "news,it"), max_results=10)
                all_items.extend(results)
                log(f"[SearXNG] '{q['q']}': {len(results)} resultados")
        elif tipo == "rss":
            for feed in fonte.get("feeds", []):
                all_items.extend(fetch_rss(feed))
        else:
            log(f"[AVISO] Tipo de fonte desconhecido: {tipo!r} — pulando")

    log(f"[Total] {len(all_items)} itens coletados")

    max_items = briefing_cfg.get("max_items", config.get("max_items", 15))
    unique = deduplicate_by_url_and_title(all_items)[: max_items * 2]
    log(f"[Dedup] {len(unique)} itens únicos")

    if not unique:
        log("[ERRO] Nenhuma notícia coletada. Abortando.")
        sys.exit(1)

    # ── Memória 24h — separar novas de continuações ───────────
    seen = load_seen()
    novas, continuacoes = split_new_vs_continuation(unique, seen)
    log(f"[Seen] {len(novas)} novas, {len(continuacoes)} continuações")

    if not novas:
        log("[AVISO] Todas as notícias são continuações — briefing focará em acompanhamento")

    # ── Clusterizar e ranquear notícias novas ─────────────────
    novas_para_cluster = novas[:20]
    novas_clusterizadas = _cluster_items(novas_para_cluster)
    ranked_ids = _rank_clusters(novas_clusterizadas, PESOS_FONTES)
    news_text_destaques, news_text_radar = _build_clustered_news_text(
        novas_clusterizadas, ranked_ids, max_destaques=10,
    )
    news_text_continuacoes = format_items_short(continuacoes[:15])

    # ── Síntese via template ──────────────────────────────────
    template_name = briefing_cfg.get("template_prompt")
    if not template_name:
        log(f"[ERRO] Briefing '{briefing_name}' sem template_prompt definido no perfis.yml")
        sys.exit(1)

    # Dict com todas as variáveis possíveis — cada template puxa o que declara.
    # news_text mantido como alias de news_text_destaques para compat com briefing_executivo.
    template_vars = {
        "data":                    date_human,
        "perfil_descricao":        perfil.get("descricao", ""),
        "interesses_quentes":      ", ".join(perfil.get("interesses_quentes", [])),
        "interesses_mornos":       ", ".join(perfil.get("interesses_mornos", [])),
        "excluir":                 ", ".join(perfil.get("excluir", [])),
        "area":                    perfil.get("area", ""),
        "palavras_chave":          ", ".join(perfil.get("palavras_chave", [])),
        "tom":                     perfil.get("tom", ""),
        "news_text":               news_text_destaques,   # alias — compat briefing_executivo
        "news_text_destaques":     news_text_destaques,
        "news_text_radar":         news_text_radar,
        "news_text_continuacoes":  news_text_continuacoes,
    }

    synthesis_prompt = render_template(template_name, template_vars)

    log("[Ollama] Gerando briefing escrito...")
    briefing_text = ollama_generate(synthesis_prompt, timeout=300)
    if not briefing_text:
        log("[ERRO] Ollama não retornou síntese. Abortando.")
        sys.exit(1)
    log(f"[Ollama] Briefing gerado ({len(briefing_text)} chars)")

    # Garante omissão da seção Acompanhamento quando não há continuações
    if not news_text_continuacoes:
        original_len = len(briefing_text)
        briefing_text = strip_acompanhamento_section(briefing_text)
        if len(briefing_text) < original_len:
            log("[seen] Seção 'Acompanhamento' removida (sem continuações)")

    # ── Palavras-chave (substitui Radar quando vazio) ─────────
    keywords_section = ""
    if not news_text_radar:
        log("[Palavras-chave] Radar vazio — extraindo tópicos do briefing...")
        topicos = _extrair_topicos_do_briefing(briefing_text, n=3)
        log(f"[Palavras-chave] Tópicos extraídos: {topicos}")
        if topicos:
            keywords_section = _build_keywords_section(topicos, max_termos=3)
            if keywords_section:
                log(f"[Palavras-chave] Seção gerada ({len(keywords_section)} chars)")
            else:
                log("[Palavras-chave] SearXNG sem resultados — seção omitida")

    # ── Título via LLM ────────────────────────────────────────
    log("[Ollama] Extraindo título...")
    titulo_manchete = generate_title_via_llm(briefing_text)
    titulo_slug     = slugify(titulo_manchete)
    filename_base   = f"{date_str}_{titulo_slug}" if titulo_slug else f"{date_str}_{briefing_name}"

    log(f"[Título] {titulo_manchete}")
    log(f"[Slug]   {titulo_slug}")

    # ── Salvar markdown ───────────────────────────────────────
    briefing_dir = VAULT_ALFRED / "briefings"
    briefing_dir.mkdir(parents=True, exist_ok=True)

    # Templates instruem "não coloque título na primeira linha" — usar direto
    briefing_body = briefing_text
    if keywords_section:
        briefing_body = briefing_body.rstrip() + "\n\n" + keywords_section

    # ── Fontes citadas — somente as que aparecem no briefing ────
    urls_citadas = set(re.findall(r'https?://[^\s\)\]"]+', briefing_body))
    fontes_citadas = [
        item for item in unique
        if item.get("url") in urls_citadas
    ]
    # Fallback: se nenhuma URL do briefing bateu (LLM não usou links),
    # usa os itens dos clusters de destaque (novas_clusterizadas)
    if not fontes_citadas:
        urls_destaques = set(re.findall(r'https?://[^\s\)\]"]+', news_text_destaques))
        fontes_citadas = [item for item in unique if item.get("url") in urls_destaques]

    n_citadas = len(fontes_citadas)
    log(f"[Fontes] {n_citadas}/{len(unique)} itens citados no briefing")

    md_content = "\n".join([
        "---",
        f'title: "{titulo_manchete}"',
        f"date: {date_str}",
        f'time: "{time_str}"',
        f"briefing: {briefing_name}",
        f"perfil: {briefing_cfg['perfil']}",
        "type: briefing",
        f"sources: {n_citadas}",
        f"tags: [briefing, {briefing_name}]",
        "---",
        "",
        f"# {titulo_manchete}",
        "",
        f"> {briefing_name} · {date_human} às {time_str} · {n_citadas} fonte(s) citada(s)",
        "",
        briefing_body,
        "",
        "---",
        "",
        "## Fontes citadas",
        "",
    ])
    for i, item in enumerate(fontes_citadas, 1):
        md_content += f"{i}. [{item['title']}]({item['url']}) — *{item['source']}*\n"

    md_path = briefing_dir / f"{filename_base}.md"
    md_path.write_text(md_content, encoding="utf-8")
    log(f"[Vault] Markdown salvo: {md_path.name}")

    # ── Áudio via template ────────────────────────────────────
    mp3_path = None
    if "audio" in briefing_cfg.get("formato", []):
        audio_template_name = briefing_cfg.get("template_audio")
        if not audio_template_name:
            log(f"[ERRO] Briefing '{briefing_name}' sem template_audio definido no perfis.yml")
            sys.exit(1)

        log("[Ollama] Gerando versão para áudio...")
        audio_prompt = render_template(audio_template_name, {"briefing": briefing_text})
        audio_text   = ollama_generate(audio_prompt, timeout=180)

        if not audio_text:
            log("[AVISO] Falha na versão áudio, usando texto original limpo.")
            audio_text = re.sub(r"[#*\[\]\(\)]", "", briefing_text)

        voice    = briefing_cfg.get("tts_voice") or config.get("tts_voice", "pt-BR-FranciscaNeural")
        mp3_path = briefing_dir / f"{filename_base}.mp3"
        log(f"[TTS] Gerando áudio com voz {voice}...")
        asyncio.run(generate_tts(audio_text, voice, mp3_path))
        log(f"[TTS] Áudio salvo: {mp3_path.name}")
    else:
        log("[Áudio] Formato não inclui áudio — pulando TTS.")

    # ── Notificação ───────────────────────────────────────────
    audio_nota = "Áudio disponível." if mp3_path else "Somente markdown."
    try:
        subprocess.run([
            "notify-send", "--icon=dialog-information", "--urgency=normal",
            f"Alfred — {briefing_name}",
            f"{titulo_manchete}\n{len(unique)} fontes · {audio_nota}",
        ], timeout=5, capture_output=True)
    except Exception:
        pass

    # ── Persistir memória ─────────────────────────────────────
    mark_seen(unique, seen)
    save_seen(seen)
    log(f"[Seen] Salvo: {len(seen)} URLs na janela de {SEEN_WINDOW_HOURS}h")

    log("=== Briefing concluído ===")
    log(f"  Briefing: {briefing_name}")
    log(f"  Título:   {titulo_manchete}")
    log(f"  Markdown: {md_path.name}")
    if mp3_path:
        log(f"  Áudio:    {mp3_path.name}")

    print(f"BRIEFING_FILE={md_path}")


# ── Fluxo ad-hoc ─────────────────────────────────────────────
def _run_adhoc(tema: str):
    """Trata argumento como tema livre: SearXNG → síntese neutra → vault + áudio."""
    config = load_config()
    today      = datetime.now()
    date_str   = today.strftime("%Y-%m-%d")
    time_str   = today.strftime("%H:%M")
    date_human = today.strftime("%d/%m/%Y")
    timestamp  = today.strftime("%Y-%m-%d_%H%M")

    slug = slugify(tema)

    log(f"=== Modo ad-hoc — tema: {tema!r} ===")
    log(f"[ad-hoc] slug: {slug}")

    # ── Coletar ───────────────────────────────────────────────
    # Usa query qualificada para reduzir resultados irrelevantes por polissemia
    query_qualificada = f'"{tema}" o que é'
    results = searxng_search(query_qualificada, categories="general,news", max_results=15)
    log(f"[SearXNG] '{query_qualificada}': {len(results)} resultados")

    # Filtra resultados que não mencionam o tema no título ou snippet
    tema_lower = tema.lower()
    results_filtrados = [
        r for r in results
        if tema_lower in (r.get("title", "") + r.get("snippet", "")).lower()
    ]
    log(f"[Filtro] {len(results_filtrados)}/{len(results)} resultados com '{tema}' no conteúdo")

    # Fallback: se o filtro zerou, usa os resultados originais
    if not results_filtrados:
        results_filtrados = results

    if not results_filtrados:
        log(f"[ERRO] Nenhum resultado encontrado para '{tema}'. Abortando.")
        sys.exit(1)

    unique = deduplicate_by_url_and_title(results_filtrados)
    log(f"[Dedup] {len(unique)} itens únicos")

    # ── Síntese via template ──────────────────────────────────
    news_text = "\n\n".join(
        f"[{i+1}] {item['title']}\nFonte: {item['source']}\nURL: {item['url']}\n{item['snippet']}"
        for i, item in enumerate(unique[:15])
    )
    synthesis_prompt = render_template("briefing_adhoc", {
        "data":      date_human,
        "tema":      tema,
        "news_text": news_text,
    })

    log("[Ollama] Gerando resumo ad-hoc...")
    resumo = ollama_generate(synthesis_prompt, timeout=300)
    if not resumo:
        log("[ERRO] Ollama não retornou síntese. Abortando.")
        sys.exit(1)
    log(f"[Ollama] Resumo gerado ({len(resumo)} chars)")

    # ── Título via LLM ────────────────────────────────────────
    log("[Ollama] Extraindo título...")
    titulo = generate_title_via_llm(resumo)
    if not titulo or titulo == "Tech Briefing":
        titulo = tema.title()
    filename_base = f"{date_str}_{slug}"

    log(f"[Título] {titulo}")
    log(f"[Arquivo] {filename_base}")

    # ── Salvar markdown ───────────────────────────────────────
    briefing_dir = VAULT_ALFRED / "briefings"
    briefing_dir.mkdir(parents=True, exist_ok=True)

    md_content = "\n".join([
        "---",
        f'title: "{titulo}"',
        f"date: {date_str}",
        f'time: "{time_str}"',
        "type: briefing",
        "mode: adhoc",
        f'tema: "{tema}"',
        f"sources: {len(unique)}",
        "tags: [briefing, adhoc]",
        "---",
        "",
        f"# {titulo}",
        "",
        f"> Resumo ad-hoc sobre *{tema}* — {date_human} às {time_str} · {len(unique)} fontes",
        "",
        resumo,
        "",
        "---",
        "",
        "## Fontes coletadas",
        "",
    ])
    for i, item in enumerate(unique[:15], 1):
        md_content += f"{i}. [{item['title']}]({item['url']}) — *{item['source']}*\n"

    md_path = briefing_dir / f"{filename_base}.md"
    md_path.write_text(md_content, encoding="utf-8")
    log(f"[Vault] Markdown salvo: {md_path}")

    # ── Áudio via template ────────────────────────────────────
    log("[Ollama] Gerando versão para áudio...")
    audio_prompt = render_template("audio_adhoc", {
        "briefing": resumo,
        "tema":     tema,
    })
    audio_text = ollama_generate(audio_prompt, timeout=180)

    if not audio_text:
        log("[AVISO] Falha na versão áudio, usando texto original.")
        audio_text = re.sub(r"[#*\[\]\(\)]", "", resumo)

    voice    = config.get("tts_voice", "pt-BR-FranciscaNeural")
    mp3_path = briefing_dir / f"{filename_base}.mp3"
    log(f"[TTS] Gerando áudio com voz {voice}...")
    asyncio.run(generate_tts(audio_text, voice, mp3_path))
    log(f"[TTS] Áudio salvo: {mp3_path}")

    # ── Notificação ───────────────────────────────────────────
    try:
        subprocess.run([
            "notify-send", "--icon=dialog-information", "--urgency=normal",
            "Alfred — Briefing ad-hoc",
            f"{titulo}\nTema: {tema} · {len(unique)} fontes",
        ], timeout=5, capture_output=True)
    except Exception:
        pass

    log("=== Ad-hoc concluído ===")
    log(f"  Tema:     {tema}")
    log(f"  Título:   {titulo}")
    log(f"  Markdown: {md_path.name}")
    log(f"  Áudio:    {mp3_path.name}")

    print(f"BRIEFING_FILE={md_path}")


# ── Main ──────────────────────────────────────────────────────
def main():
    argumento = sys.argv[1] if len(sys.argv) > 1 else "matinal_tech"

    try:
        briefing_cfg = get_briefing(argumento)
        _run_briefing_nomeado(briefing_cfg, argumento)
    except KeyError:
        log(f"[ad-hoc] '{argumento}' não é um briefing nomeado — tratando como tema de pesquisa")
        _run_adhoc(argumento)
    except Exception as e:
        log(f"[ERRO] Falha ao carregar perfis.yml: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
