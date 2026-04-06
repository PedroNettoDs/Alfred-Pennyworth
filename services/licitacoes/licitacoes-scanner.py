#!/usr/bin/env python3
"""
Alfred Licitações Scanner v2 — busca licitações em TODOS os portais estaduais do Brasil.
Fontes: SearXNG (queries genéricas + site:portal), PNCP API, portais.csv.
Análise: Ollama classifica relevância por perfil da empresa.
Saída: Markdown no vault + ChromaDB para histórico.
"""

import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import yaml

SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
CONFIG_FILE  = SCRIPT_DIR / "config.yml"
PORTAIS_CSV  = SCRIPT_DIR / "portais.csv"

SEARXNG_URL  = os.getenv("SEARXNG_URL", "http://localhost:8888")
OLLAMA_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL        = os.getenv("MODEL_CHAT", "llama3.1:8b")
VAULT_ALFRED = Path(os.getenv("VAULT_ALFRED", ""))
CHROMADB_URL = os.getenv("CHROMADB_URL", "http://localhost:8000")
OLLAMA_EMBED = os.getenv("MODEL_EMBED", "nomic-embed-text-v2-moe:latest")


def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_portais() -> list[dict]:
    csv_path = SCRIPT_DIR / "portais.csv"
    if not csv_path.exists():
        log(f"[AVISO] portais.csv não encontrado em {csv_path}")
        return []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def extrair_dominio(url: str) -> str:
    """Extrai o domínio de uma URL para usar com site:"""
    url = url.replace("https://", "").replace("http://", "")
    return url.split("/")[0]


# ── SearXNG ───────────────────────────────────────────────────
def searxng_search(query: str, max_results: int = 10) -> list[dict]:
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(
                f"{SEARXNG_URL}/search",
                params={"q": query, "format": "json"},
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])[:max_results]
                return [
                    {
                        "titulo": r.get("title", ""),
                        "url": r.get("url", ""),
                        "descricao": re.sub(r"<[^>]+>", "", r.get("content", r.get("snippet", "")))[:500],
                        "fonte": "SearXNG",
                        "uf": "",
                    }
                    for r in results if r.get("url")
                ]
    except Exception as e:
        log(f"[SearXNG] Erro: {e}")
    return []


def buscar_por_portal(portais: list[dict], config: dict) -> list[dict]:
    """Faz buscas site:domínio para cada portal do CSV."""
    busca_config = config.get("busca_por_portal", {})
    if not busca_config.get("enabled", False):
        return []

    tipos_aceitos = busca_config.get("tipos", ["estadual", "federal"])
    ufs_filtro = busca_config.get("ufs_filtro", [])
    queries = busca_config.get("queries_por_site", ["licitação TI"])

    all_items = []
    portais_filtrados = [
        p for p in portais
        if p.get("tipo", "") in tipos_aceitos
        and (not ufs_filtro or p.get("uf", "") in ufs_filtro)
    ]

    log(f"[Portais] {len(portais_filtrados)} portais para buscar")

    for portal in portais_filtrados:
        dominio = extrair_dominio(portal.get("url", ""))
        uf = portal.get("uf", "BR")
        nome_portal = portal.get("portal", dominio)

        for query in queries:
            search_query = f"site:{dominio} {query}"
            results = searxng_search(search_query, max_results=5)

            for r in results:
                r["fonte"] = nome_portal
                r["uf"] = uf

            all_items.extend(results)

            if results:
                log(f"[Portal] {uf} {nome_portal}: {len(results)} resultados")

    return all_items


# ── PNCP API ──────────────────────────────────────────────────
def buscar_pncp(config: dict) -> list[dict]:
    pncp_config = config.get("pncp", {})
    if not pncp_config.get("enabled"):
        return []

    base_url = pncp_config.get("base_url", "")
    dias = pncp_config.get("dias_retroativos", 3)
    max_items = pncp_config.get("max_items", 30)
    palavras = pncp_config.get("palavras", "tecnologia informação")
    ufs_filtro = pncp_config.get("ufs_filtro", [])

    data_inicio = (datetime.now() - timedelta(days=dias)).strftime("%Y%m%d")
    data_fim = datetime.now().strftime("%Y%m%d")

    items = []
    for palavra in palavras.split(","):
        palavra = palavra.strip()
        if not palavra:
            continue
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(
                    base_url,
                    params={
                        "dataInicial": data_inicio,
                        "dataFinal": data_fim,
                        "pagina": 1,
                        "tamanhoPagina": min(max_items, 50),
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    registros = data if isinstance(data, list) else data.get("data", data.get("registros", []))
                    if isinstance(registros, list):
                        for r in registros:
                            objeto = r.get("objetoCompra", r.get("objeto", r.get("description", "")))
                            if not objeto:
                                continue
                            objeto_lower = objeto.lower()
                            if any(p.lower() in objeto_lower for p in palavra.split()):
                                uf = r.get("unidadeOrgao", {}).get("ufSigla", r.get("uf", "BR"))
                                if ufs_filtro and uf not in ufs_filtro:
                                    continue
                                items.append({
                                    "titulo": objeto[:200],
                                    "url": r.get("linkSistemaOrigem", r.get("link", "https://pncp.gov.br")),
                                    "descricao": (
                                        f"Órgão: {r.get('orgaoEntidade', {}).get('razaoSocial', r.get('orgao', 'N/I'))}\n"
                                        f"UF: {uf}\n"
                                        f"Modalidade: {r.get('modalidadeNome', r.get('modalidade', 'N/I'))}\n"
                                        f"Valor: R$ {r.get('valorTotalEstimado', r.get('valor', 'N/I'))}\n"
                                        f"Data: {r.get('dataPublicacaoPncp', r.get('data', 'N/I'))}"
                                    ),
                                    "fonte": "PNCP",
                                    "uf": uf,
                                })
                    log(f"[PNCP] '{palavra}': {len(registros) if isinstance(registros, list) else 0} resultados")
        except Exception as e:
            log(f"[PNCP] Erro buscando '{palavra}': {e}")

    return items[:max_items]


# ── Deduplicação ──────────────────────────────────────────────
def deduplicate(items: list[dict]) -> list[dict]:
    seen_urls = set()
    seen_titles = set()
    unique = []
    for item in items:
        url = item.get("url", "")
        title_norm = re.sub(r"\s+", " ", item.get("titulo", "").lower().strip())
        if url in seen_urls:
            continue
        skip = False
        for seen in seen_titles:
            words_new = set(title_norm.split())
            words_seen = set(seen.split())
            if len(words_new) > 3 and len(words_new & words_seen) / max(len(words_new), 1) > 0.7:
                skip = True
                break
        if not skip:
            seen_urls.add(url)
            seen_titles.add(title_norm)
            unique.append(item)
    return unique


# ── Ollama ────────────────────────────────────────────────────
def ollama_generate(prompt: str, timeout: int = 300) -> str:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": MODEL, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
    except Exception as e:
        log(f"[Ollama] Erro: {e}")
        return ""


def ollama_embed(text: str) -> list[float]:
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": OLLAMA_EMBED, "prompt": text},
            )
            if resp.status_code == 200:
                return resp.json().get("embedding", [])
    except Exception as e:
        log(f"[Embed] Erro: {e}")
    return []


# ── ChromaDB ──────────────────────────────────────────────────
def ensure_collection(collection_name: str = "licitacoes") -> str:
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{CHROMADB_URL}/api/v1/collections",
                json={"name": collection_name, "get_or_create": True},
            )
            if resp.status_code in (200, 201):
                return resp.json().get("id", "")
            # Fallback v2
            resp = client.post(
                f"{CHROMADB_URL}/api/v2/tenants/default_tenant/databases/default_database/collections",
                json={"name": collection_name, "get_or_create": True},
            )
            if resp.status_code in (200, 201):
                return resp.json().get("id", "")
    except Exception as e:
        log(f"[ChromaDB] Erro ao criar coleção: {e}")
    return ""


def upsert_chromadb(collection_id: str, items: list[dict]):
    if not collection_id:
        log("[ChromaDB] Sem collection_id, pulando upsert")
        return

    ids, embeddings, documents, metadatas = [], [], [], []
    for item in items:
        text = f"{item['titulo']}\n{item['descricao']}"
        embedding = ollama_embed(text)
        if not embedding:
            continue
        item_id = re.sub(r"[^a-zA-Z0-9]", "_", item.get("url", "")[-60:])
        ids.append(item_id)
        embeddings.append(embedding)
        documents.append(text)
        metadatas.append({
            "titulo": item["titulo"][:200],
            "url": item.get("url", ""),
            "fonte": item.get("fonte", ""),
            "uf": item.get("uf", ""),
            "data": datetime.now().strftime("%Y-%m-%d"),
        })

    if not ids:
        log("[ChromaDB] Nenhum embedding gerado")
        return

    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{CHROMADB_URL}/api/v1/collections/{collection_id}/upsert",
                json={"ids": ids, "embeddings": embeddings, "documents": documents, "metadatas": metadatas},
            )
            if resp.status_code in (200, 201):
                log(f"[ChromaDB] {len(ids)} licitações indexadas")
            else:
                log(f"[ChromaDB] Erro upsert: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log(f"[ChromaDB] Erro: {e}")


# ── Estatísticas por UF ───────────────────────────────────────
def gerar_stats_uf(items: list[dict]) -> str:
    """Gera resumo de licitações encontradas por UF."""
    contagem = {}
    for item in items:
        uf = item.get("uf", "N/I") or "N/I"
        contagem[uf] = contagem.get(uf, 0) + 1
    
    if not contagem:
        return "Nenhuma licitação com UF identificada."
    
    lines = []
    for uf, count in sorted(contagem.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"| {uf} | {count} |")
    
    return "| UF | Quantidade |\n|----|-----------|\n" + "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────
def main():
    config = load_config()
    portais = load_portais()
    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d")
    date_human = today.strftime("%d/%m/%Y")

    empresa = config.get("empresa", {})
    filtros = config.get("filtros", {})

    log(f"=== Licitações Scanner v2 — {date_human} ===")
    log(f"Empresa: {empresa.get('nome', 'N/A')} | Área: {empresa.get('area', 'N/A')}")
    log(f"Portais carregados: {len(portais)} (CSV)")

    # ── 1. Coletar licitações ─────────────────────────────────
    all_items = []

    # 1a. SearXNG — queries genéricas
    for query in config.get("searxng_queries", []):
        results = searxng_search(query)
        all_items.extend(results)
        if results:
            log(f"[SearXNG] '{query}': {len(results)} resultados")

    # 1b. SearXNG — busca direcionada por portal (site:domínio)
    portal_items = buscar_por_portal(portais, config)
    all_items.extend(portal_items)

    # 1c. PNCP API
    pncp_items = buscar_pncp(config)
    all_items.extend(pncp_items)

    log(f"[Total] {len(all_items)} itens coletados")

    # ── 2. Deduplicar ─────────────────────────────────────────
    unique = deduplicate(all_items)
    log(f"[Dedup] {len(unique)} itens únicos")

    if not unique:
        log("[AVISO] Nenhuma licitação encontrada hoje.")
        lic_dir = VAULT_ALFRED / "licitacoes"
        lic_dir.mkdir(parents=True, exist_ok=True)
        (lic_dir / f"{date_str}.md").write_text(
            f"---\ntitle: Licitações — {date_human}\ndate: {date_str}\n"
            f"type: licitacoes\nstatus: vazio\n---\n\n"
            f"# Licitações — {date_human}\n\nNenhuma licitação relevante encontrada hoje.\n",
            encoding="utf-8",
        )
        return

    # ── 3. Indexar no ChromaDB ────────────────────────────────
    collection_id = ensure_collection("licitacoes")
    if collection_id:
        upsert_chromadb(collection_id, unique)

    # ── 4. Classificar relevância com Ollama ──────────────────
    log("[Ollama] Classificando relevância...")

    perfil_empresa = (
        f"Empresa: {empresa.get('nome', '')}\n"
        f"Área: {empresa.get('area', '')}\n"
        f"Serviços: {', '.join(empresa.get('servicos', []))}\n"
        f"Região: {empresa.get('regiao', '')}\n"
        f"Porte: {empresa.get('porte', '')}"
    )

    licitacoes_text = "\n\n".join(
        f"[{i+1}] {item['titulo']}\n"
        f"Fonte: {item['fonte']} | UF: {item.get('uf', 'N/I')}\n"
        f"URL: {item['url']}\n"
        f"{item['descricao']}"
        for i, item in enumerate(unique[:25])
    )

    max_relatorio = config.get("max_items_relatorio", 15)
    score_minimo = filtros.get("score_minimo", 30)

    analysis_prompt = f"""Você é um analista de licitações públicas. Analise as licitações abaixo e classifique-as por relevância para esta empresa:

PERFIL DA EMPRESA:
{perfil_empresa}

LICITAÇÕES ENCONTRADAS:
{licitacoes_text}

Para cada licitação, atribua um SCORE de 0 a 100 (100 = perfeitamente alinhada).
Retorne SOMENTE um array JSON neste formato, sem texto adicional:
[
  {{"numero": 1, "score": 85, "motivo": "Consultoria em TI, alinhado com perfil", "uf": "SP"}},
  {{"numero": 2, "score": 20, "motivo": "Equipamento hospitalar, fora do escopo", "uf": "MG"}}
]

Considere: objeto, região, porte, modalidade e alinhamento com os serviços.
Ordene por score decrescente. Inclua TODAS."""

    scores_raw = ollama_generate(analysis_prompt, timeout=300)

    scored_items = []
    try:
        match = re.search(r"\[.*\]", scores_raw, re.DOTALL)
        if match:
            scores = json.loads(match.group())
            for s in scores:
                idx = s.get("numero", 0) - 1
                if 0 <= idx < len(unique):
                    item = unique[idx].copy()
                    item["score"] = s.get("score", 0)
                    item["motivo"] = s.get("motivo", "")
                    if not item.get("uf"):
                        item["uf"] = s.get("uf", "N/I")
                    if item["score"] >= score_minimo:
                        scored_items.append(item)
    except Exception as e:
        log(f"[Ollama] Erro parseando scores: {e}")
        scored_items = [dict(**item, score=50, motivo="Score não disponível") for item in unique]

    scored_items.sort(key=lambda x: x.get("score", 0), reverse=True)
    scored_items = scored_items[:max_relatorio]

    log(f"[Análise] {len(scored_items)} licitações acima do score mínimo ({score_minimo})")

    # ── 5. Gerar relatório com Ollama ─────────────────────────
    log("[Ollama] Gerando relatório...")

    stats_uf = gerar_stats_uf(unique)

    top_items_text = "\n\n".join(
        f"[{i+1}] Score: {item['score']}/100 | UF: {item.get('uf', 'N/I')}\n"
        f"Título: {item['titulo']}\n"
        f"URL: {item['url']}\n"
        f"Motivo: {item['motivo']}\n"
        f"{item['descricao']}"
        for i, item in enumerate(scored_items)
    )

    report_prompt = f"""Você é Alfred Pennyworth, assistente de Pedro Netto da empresa AttanoTech.
Crie um RELATÓRIO DE LICITAÇÕES em português brasileiro.

PERFIL:
{perfil_empresa}

LICITAÇÕES CLASSIFICADAS (por relevância):
{top_items_text}

DISTRIBUIÇÃO POR UF:
{stats_uf}

Formato obrigatório:

## Resumo do dia
Quantas licitações analisadas, quantas relevantes, quais UFs mais ativas, tendência geral.

## Oportunidades prioritárias
As 3-5 licitações mais relevantes (score > 60), cada uma com:
- **Título** e link
- UF e órgão
- Score de relevância e por que se encaixa
- Prazo/valor se disponível
- Ação recomendada (participar, acompanhar, ignorar)

## Outras oportunidades
Licitações com score 30-60, resumidas em 1 linha cada com UF.

## Mapa de oportunidades por estado
Resumo dos estados com mais licitações relevantes.

## Análise de mercado
Padrões: tipos de serviço mais demandados, órgãos comprando mais, faixas de valor.

Seja direto e objetivo. Não use introduções."""

    report = ollama_generate(report_prompt, timeout=300)

    if not report:
        report = "Relatório não gerado — erro na síntese com Ollama."

    # ── 6. Salvar no vault ────────────────────────────────────
    lic_dir = VAULT_ALFRED / "licitacoes"
    lic_dir.mkdir(parents=True, exist_ok=True)

    md_content = "\n".join([
        "---",
        f"title: Licitações — {date_human}",
        f"date: {date_str}",
        "type: licitacoes",
        f"total_encontradas: {len(unique)}",
        f"relevantes: {len(scored_items)}",
        f"portais_consultados: {len(portais)}",
        f"tags: [licitacoes, attanotech, daily]",
        "---",
        "",
        f"# Licitações — {date_human}",
        "",
        report,
        "",
        "---",
        "",
        "## Distribuição por UF",
        "",
        stats_uf,
        "",
        "## Todas as licitações analisadas",
        "",
    ])

    for i, item in enumerate(scored_items, 1):
        md_content += (
            f"### {i}. [{item['titulo'][:100]}]({item['url']})\n"
            f"- **Score:** {item['score']}/100 | **UF:** {item.get('uf', 'N/I')}\n"
            f"- **Fonte:** {item['fonte']}\n"
            f"- **Motivo:** {item['motivo']}\n"
            f"- {item['descricao']}\n\n"
        )

    md_path = lic_dir / f"{date_str}.md"
    md_path.write_text(md_content, encoding="utf-8")
    log(f"[Vault] Relatório salvo: {md_path}")

    # ── 7. Notificação desktop ────────────────────────────────
    try:
        import subprocess
        subprocess.run([
            "notify-send", "--icon=dialog-information",
            "Alfred — Licitações",
            f"Relatório de {date_human} pronto.\n"
            f"{len(unique)} encontradas · {len(scored_items)} relevantes · {len(portais)} portais.",
        ], timeout=5, capture_output=True)
    except Exception:
        pass

    log("=== Scanner concluído ===")
    log(f"  Total: {len(unique)} | Relevantes: {len(scored_items)} | Portais: {len(portais)}")
    log(f"  Relatório: {md_path}")


if __name__ == "__main__":
    main()
