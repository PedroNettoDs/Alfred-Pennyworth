#!/usr/bin/env python3
"""
Alfred Licitações Scanner — busca e analisa licitações relevantes para o perfil da empresa.
Fontes: SearXNG (portais gov) + PNCP API.
Análise: Ollama classifica relevância por perfil.
Saída: Markdown no vault + ChromaDB para histórico.
"""

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

SEARXNG_URL  = os.getenv("SEARXNG_URL", "http://localhost:8888")
OLLAMA_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL        = os.getenv("MODEL_CHAT", "llama3.1:8b")
VAULT_ALFRED = Path(os.getenv("VAULT_ALFRED", ""))
CHROMADB_URL = os.getenv("CHROMADB_URL", "http://localhost:8000")
OLLAMA_EMBED = os.getenv("MODEL_EMBED", "nomic-embed-text-v2-moe:latest")


def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


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
                    }
                    for r in results if r.get("url")
                ]
    except Exception as e:
        log(f"[SearXNG] Erro: {e}")
    return []


# ── PNCP API ──────────────────────────────────────────────────
def buscar_pncp(config: dict) -> list[dict]:
    pncp_config = config.get("pncp", {})
    if not pncp_config.get("enabled"):
        return []

    base_url = pncp_config.get("base_url", "")
    dias = pncp_config.get("dias_retroativos", 3)
    max_items = pncp_config.get("max_items", 30)
    palavras = pncp_config.get("palavras", "tecnologia informação")

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
                        "codigoModalidadeContratacao": "",
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
                            # Filtro básico por palavra-chave
                            objeto_lower = objeto.lower()
                            if any(p.lower() in objeto_lower for p in palavra.split()):
                                items.append({
                                    "titulo": objeto[:200],
                                    "url": r.get("linkSistemaOrigem", r.get("link", f"https://pncp.gov.br")),
                                    "descricao": (
                                        f"Órgão: {r.get('orgaoEntidade', {}).get('razaoSocial', r.get('orgao', 'N/I'))}\n"
                                        f"Modalidade: {r.get('modalidadeNome', r.get('modalidade', 'N/I'))}\n"
                                        f"Valor: R$ {r.get('valorTotalEstimado', r.get('valor', 'N/I'))}\n"
                                        f"Data: {r.get('dataPublicacaoPncp', r.get('data', 'N/I'))}"
                                    ),
                                    "fonte": "PNCP",
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
    """Cria ou obtém a coleção no ChromaDB, retorna o ID."""
    try:
        with httpx.Client(timeout=15) as client:
            # Tentar criar
            resp = client.post(
                f"{CHROMADB_URL}/api/v1/collections",
                json={"name": collection_name, "get_or_create": True},
            )
            if resp.status_code in (200, 201):
                return resp.json().get("id", "")

            # Fallback: v2 API
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
    """Insere licitações no ChromaDB com embeddings."""
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
            "data": datetime.now().strftime("%Y-%m-%d"),
        })

    if not ids:
        log("[ChromaDB] Nenhum embedding gerado")
        return

    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{CHROMADB_URL}/api/v1/collections/{collection_id}/upsert",
                json={
                    "ids": ids,
                    "embeddings": embeddings,
                    "documents": documents,
                    "metadatas": metadatas,
                },
            )
            if resp.status_code in (200, 201):
                log(f"[ChromaDB] {len(ids)} licitações indexadas")
            else:
                log(f"[ChromaDB] Erro upsert: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log(f"[ChromaDB] Erro: {e}")


# ── Main ──────────────────────────────────────────────────────
def main():
    config = load_config()
    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d")
    date_human = today.strftime("%d/%m/%Y")

    empresa = config.get("empresa", {})
    filtros = config.get("filtros", {})

    log(f"=== Licitações Scanner — {date_human} ===")
    log(f"Empresa: {empresa.get('nome', 'N/A')} | Área: {empresa.get('area', 'N/A')}")

    # ── 1. Coletar licitações ─────────────────────────────────
    all_items = []

    # SearXNG
    for query in config.get("searxng_queries", []):
        results = searxng_search(query)
        all_items.extend(results)
        log(f"[SearXNG] '{query}': {len(results)} resultados")

    # PNCP
    pncp_items = buscar_pncp(config)
    all_items.extend(pncp_items)

    log(f"[Total] {len(all_items)} itens coletados")

    # ── 2. Deduplicar ─────────────────────────────────────────
    unique = deduplicate(all_items)
    log(f"[Dedup] {len(unique)} itens únicos")

    if not unique:
        log("[AVISO] Nenhuma licitação encontrada hoje.")
        # Salvar relatório vazio
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
        f"Fonte: {item['fonte']}\n"
        f"URL: {item['url']}\n"
        f"{item['descricao']}"
        for i, item in enumerate(unique[:25])
    )

    max_relatorio = config.get("max_items_relatorio", 10)
    score_minimo = filtros.get("score_minimo", 30)

    analysis_prompt = f"""Você é um analista de licitações públicas. Analise as licitações abaixo e classifique-as por relevância para esta empresa:

PERFIL DA EMPRESA:
{perfil_empresa}

LICITAÇÕES ENCONTRADAS:
{licitacoes_text}

Para cada licitação, atribua um SCORE de 0 a 100 (100 = perfeitamente alinhada).
Retorne SOMENTE um array JSON neste formato, sem texto adicional:
[
  {{"numero": 1, "score": 85, "motivo": "Consultoria em TI, alinhado com perfil"}},
  {{"numero": 2, "score": 20, "motivo": "Equipamento hospitalar, fora do escopo"}}
]

Considere: objeto da licitação, região, porte, modalidade e alinhamento com os serviços da empresa.
Ordene por score decrescente. Inclua TODAS as licitações."""

    scores_raw = ollama_generate(analysis_prompt, timeout=300)

    # Parsear scores
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
                    if item["score"] >= score_minimo:
                        scored_items.append(item)
    except Exception as e:
        log(f"[Ollama] Erro parseando scores: {e}")
        # Fallback: incluir todos sem score
        scored_items = [dict(**item, score=50, motivo="Score não disponível") for item in unique]

    scored_items.sort(key=lambda x: x.get("score", 0), reverse=True)
    scored_items = scored_items[:max_relatorio]

    log(f"[Análise] {len(scored_items)} licitações acima do score mínimo ({score_minimo})")

    # ── 5. Gerar relatório com Ollama ─────────────────────────
    log("[Ollama] Gerando relatório...")

    top_items_text = "\n\n".join(
        f"[{i+1}] Score: {item['score']}/100\n"
        f"Título: {item['titulo']}\n"
        f"URL: {item['url']}\n"
        f"Motivo: {item['motivo']}\n"
        f"{item['descricao']}"
        for i, item in enumerate(scored_items)
    )

    report_prompt = f"""Você é Alfred Pennyworth, assistente de Pedro Netto da empresa AttanoTech.
Crie um RELATÓRIO DE LICITAÇÕES em português brasileiro baseado nas licitações classificadas abaixo.

PERFIL:
{perfil_empresa}

LICITAÇÕES CLASSIFICADAS (por relevância):
{top_items_text}

Formato obrigatório:

## Resumo do dia
Quantas licitações analisadas, quantas relevantes, tendência geral.

## Oportunidades prioritárias
As 3-5 licitações mais relevantes (score > 60), cada uma com:
- **Título** e link
- Score de relevância e por que se encaixa
- Prazo/valor se disponível
- Ação recomendada (participar, acompanhar, ignorar)

## Outras oportunidades
Licitações com score 30-60, resumidas em 1 linha cada.

## Análise de mercado
Padrões observados: tipos de serviço mais demandados, órgãos comprando mais, faixas de valor.

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
        f"tags: [licitacoes, attanotech, daily]",
        "---",
        "",
        f"# Licitações — {date_human}",
        "",
        report,
        "",
        "---",
        "",
        "## Todas as licitações analisadas",
        "",
    ])

    for i, item in enumerate(scored_items, 1):
        md_content += (
            f"### {i}. [{item['titulo'][:100]}]({item['url']})\n"
            f"- **Score:** {item['score']}/100\n"
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
            "notify-send",
            "--icon=dialog-information",
            "Alfred — Licitações",
            f"Relatório de {date_human} pronto.\n"
            f"{len(unique)} encontradas · {len(scored_items)} relevantes.",
        ], timeout=5, capture_output=True)
    except Exception:
        pass

    log("=== Scanner concluído ===")
    log(f"  Total: {len(unique)} | Relevantes: {len(scored_items)}")
    log(f"  Relatório: {md_path}")


if __name__ == "__main__":
    main()
