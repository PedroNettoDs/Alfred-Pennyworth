#!/usr/bin/env python3
"""
Alfred Licitações Scanner v3 — busca licitações ABERTAS em TODOS os portais do Brasil.
Mudanças v3:
  - Novo endpoint PNCP /proposta para licitações com propostas abertas
  - Filtro de ano corrente em todas as fontes
  - Filtro de status: descarta licitações encerradas/canceladas/homologadas
  - codigoModalidadeContratacao enviado corretamente (obrigatório na API)
  - SearXNG queries incluem ano corrente e termos de status

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

# ── Ano corrente (usado em filtros e queries) ─────────────────
ANO_CORRENTE = datetime.now().year


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


# ── Filtro de status: palavras que indicam licitação ENCERRADA ──
TERMOS_ENCERRADA = [
    "homologad", "encerrad", "cancelad", "anulad", "revogad",
    "deserta", "fracassad", "adjudicad", "arquivad", "suspens",
    "resultado final", "vencedor definido", "contrato assinado",
]


def parece_encerrada(texto: str) -> bool:
    """Verifica se o texto contém indicadores de licitação encerrada."""
    texto_lower = texto.lower()
    return any(termo in texto_lower for termo in TERMOS_ENCERRADA)


def filtrar_ano_corrente(items: list[dict]) -> list[dict]:
    """Remove itens que claramente NÃO são do ano corrente.
    Heurística: se o texto menciona um ano anterior (2020-ANO_CORRENTE-1)
    mas NÃO menciona o ano corrente, descarta.
    """
    ano_str = str(ANO_CORRENTE)
    filtrados = []
    for item in items:
        texto = f"{item.get('titulo') or ''} {item.get('descricao') or ''}".lower()
        # Se menciona explicitamente o ano corrente, inclui
        if ano_str in texto:
            filtrados.append(item)
            continue
        # Se menciona um ano anterior E não menciona o corrente, descarta
        menciona_ano_antigo = any(
            str(ano) in texto
            for ano in range(2020, ANO_CORRENTE)
        )
        if menciona_ano_antigo:
            continue
        # Sem indicação de ano → inclui (pode ser corrente)
        filtrados.append(item)
    return filtrados


def filtrar_abertas(items: list[dict]) -> list[dict]:
    """Remove itens que parecem já encerrados/cancelados/homologados."""
    filtrados = []
    for item in items:
        texto = f"{item.get('titulo') or ''} {item.get('descricao') or ''}".lower()
        if not parece_encerrada(texto):
            filtrados.append(item)
    return filtrados


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
# Códigos de modalidade (Manual PNCP API Consultas v1.0, seção 5.2):
# 1=Leilão Eletrônico, 2=Diálogo Competitivo, 3=Concurso,
# 4=Concorrência Eletrônica, 5=Concorrência Presencial,
# 6=Pregão Eletrônico, 7=Pregão Presencial,
# 8=Dispensa de Licitação, 9=Inexigibilidade,
# 10=Manifestação de Interesse, 11=Pré-qualificação,
# 12=Credenciamento, 13=Leilão Presencial
#
# Categoria do Processo (seção 5.11):
# 3=Informática (TIC) — NÃO disponível como filtro de entrada na API,
# apenas como campo de retorno em /v1/contratos.
# Filtramos por palavras-chave no objetoCompra como alternativa.
MODALIDADES_PNCP = {
    "pregao": 6,
    "pregao_presencial": 7,
    "concorrencia": 4,
    "concorrencia_presencial": 5,
    "dispensa": 8,
    "inexigibilidade": 9,
    "credenciamento": 12,
}


def buscar_pncp_propostas_abertas(config: dict) -> list[dict]:
    """
    Usa o endpoint /proposta do PNCP que retorna SOMENTE
    contratações com recebimento de propostas ABERTO.
    
    ATENÇÃO: codigoModalidadeContratacao é OBRIGATÓRIO neste endpoint
    (Manual PNCP API Consultas v1.0, seção 6.4).
    Iteramos sobre cada modalidade configurada em filtros.modalidades_aceitas.
    """
    pncp_config = config.get("pncp", {})
    if not pncp_config.get("enabled"):
        return []

    base_url = "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta"
    max_items = pncp_config.get("max_items", 50)
    palavras = pncp_config.get("palavras", "tecnologia informação")
    ufs_filtro = pncp_config.get("ufs_filtro", [])

    # dataFinal = até quando buscar (obrigatório)
    data_fim = datetime.now().strftime("%Y%m%d")

    # codigoModalidadeContratacao é obrigatório — iterar sobre cada uma
    filtros = config.get("filtros", {})
    modalidades_config = filtros.get("modalidades_aceitas", ["pregao", "dispensa", "concorrencia"])
    codigos_modalidade = [
        MODALIDADES_PNCP[m] for m in modalidades_config
        if m in MODALIDADES_PNCP
    ]
    if not codigos_modalidade:
        codigos_modalidade = [6, 8, 4]  # pregão eletrônico, dispensa, concorrência

    palavras_list = [p.strip().lower() for p in palavras.split(",") if p.strip()]
    items = []

    for codigo_mod in codigos_modalidade:
        pagina = 1
        try:
            with httpx.Client(timeout=30) as client:
                while len(items) < max_items:
                    resp = client.get(
                        base_url,
                        params={
                            "dataFinal": data_fim,
                            "codigoModalidadeContratacao": codigo_mod,
                            "pagina": pagina,
                            "tamanhoPagina": min(max_items, 500),
                        },
                    )
                    if resp.status_code != 200:
                        log(f"[PNCP/proposta] mod={codigo_mod} HTTP {resp.status_code}")
                        break

                    data = resp.json()
                    registros = data if isinstance(data, list) else data.get("data", data.get("registros", []))
                    if not isinstance(registros, list) or not registros:
                        break

                    encontrados_mod = 0
                    for r in registros:
                        objeto = r.get("objetoCompra", r.get("objeto", r.get("description", "")))
                        if not objeto:
                            continue

                        # Filtrar por palavras-chave do perfil da empresa
                        objeto_lower = objeto.lower()
                        match = any(
                            all(word in objeto_lower for word in p.split())
                            for p in palavras_list
                        )
                        if not match:
                            continue

                        uf = r.get("unidadeOrgao", {}).get("ufSigla", r.get("uf", "BR"))
                        if ufs_filtro and uf not in ufs_filtro:
                            continue

                        # Extrair datas de proposta para mostrar prazo
                        data_abertura = r.get("dataAberturaProposta", "")
                        data_encerramento = r.get("dataEncerramentoProposta", "")
                        prazo_info = ""
                        if data_encerramento:
                            prazo_info = f"\nPrazo propostas: até {data_encerramento[:10]}"

                        items.append({
                            "titulo": objeto[:200],
                            "url": r.get("linkSistemaOrigem", r.get("link", "https://pncp.gov.br")),
                            "descricao": (
                                f"Órgão: {r.get('orgaoEntidade', {}).get('razaoSocial', r.get('orgao', 'N/I'))}\n"
                                f"UF: {uf}\n"
                                f"Modalidade: {r.get('modalidadeNome', r.get('modalidade', 'N/I'))}\n"
                                f"Valor est.: R$ {r.get('valorTotalEstimado', r.get('valor', 'N/I'))}\n"
                                f"Situação: {r.get('situacaoCompraNome', 'N/I')}\n"
                                f"Data pub.: {r.get('dataPublicacaoPncp', r.get('data', 'N/I'))}"
                                f"{prazo_info}\n"
                                f"Status: PROPOSTAS ABERTAS"
                            ),
                            "fonte": "PNCP (propostas abertas)",
                            "uf": uf,
                            "status": "aberta",
                        })
                        encontrados_mod += 1

                    log(f"[PNCP/proposta] mod={codigo_mod} pag={pagina}: {len(registros)} registros, {encontrados_mod} relevantes")

                    # Paginação
                    if len(registros) < 500:
                        break
                    pagina += 1
                    if pagina > 3:  # Limitar a 3 páginas por modalidade
                        break

        except Exception as e:
            log(f"[PNCP/proposta] mod={codigo_mod} Erro: {e}")

    log(f"[PNCP/proposta] Total: {len(items)} licitações abertas relevantes")
    return items[:max_items]


def buscar_pncp_publicacao(config: dict) -> list[dict]:
    """
    Usa o endpoint /publicacao do PNCP com os parâmetros corretos.
    Agora envia codigoModalidadeContratacao (obrigatório) e filtra por ano.
    """
    pncp_config = config.get("pncp", {})
    if not pncp_config.get("enabled"):
        return []

    base_url = pncp_config.get(
        "base_url",
        "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"
    )
    dias = pncp_config.get("dias_retroativos", 3)
    max_items = pncp_config.get("max_items", 50)
    palavras = pncp_config.get("palavras", "tecnologia informação")
    ufs_filtro = pncp_config.get("ufs_filtro", [])

    # Garantir que o range de datas fique dentro do ano corrente
    data_inicio_dt = max(
        datetime.now() - timedelta(days=dias),
        datetime(ANO_CORRENTE, 1, 1),
    )
    data_inicio = data_inicio_dt.strftime("%Y%m%d")
    data_fim = datetime.now().strftime("%Y%m%d")

    # Modalidades a buscar (obrigatório na API)
    filtros = config.get("filtros", {})
    modalidades_config = filtros.get("modalidades_aceitas", ["pregao", "dispensa", "concorrencia"])
    codigos_modalidade = [
        MODALIDADES_PNCP[m] for m in modalidades_config
        if m in MODALIDADES_PNCP
    ]
    if not codigos_modalidade:
        codigos_modalidade = [6, 8, 4]  # pregão, dispensa, concorrência

    items = []
    for codigo_mod in codigos_modalidade:
        for palavra in [p.strip() for p in palavras.split(",") if p.strip()]:
            try:
                with httpx.Client(timeout=30) as client:
                    resp = client.get(
                        base_url,
                        params={
                            "dataInicial": data_inicio,
                            "dataFinal": data_fim,
                            "codigoModalidadeContratacao": codigo_mod,
                            "pagina": 1,
                            "tamanhoPagina": min(max_items, 50),
                        },
                    )
                    if resp.status_code != 200:
                        continue

                    data = resp.json()
                    registros = data if isinstance(data, list) else data.get("data", data.get("registros", []))
                    if not isinstance(registros, list):
                        continue

                    for r in registros:
                        objeto = r.get("objetoCompra", r.get("objeto", r.get("description", "")))
                        if not objeto:
                            continue
                        objeto_lower = objeto.lower()
                        if not any(w in objeto_lower for w in palavra.lower().split()):
                            continue

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
                                f"Data pub.: {r.get('dataPublicacaoPncp', r.get('data', 'N/I'))}"
                            ),
                            "fonte": "PNCP",
                            "uf": uf,
                        })

                    log(f"[PNCP/pub] mod={codigo_mod} '{palavra}': {len(registros) if isinstance(registros, list) else 0}")
            except Exception as e:
                log(f"[PNCP/pub] Erro buscando mod={codigo_mod} '{palavra}': {e}")

    return items[:max_items]


# ── Deduplicação ──────────────────────────────────────────────
def deduplicate(items: list[dict]) -> list[dict]:
    seen_urls = set()
    seen_titles = set()
    unique = []
    for item in items:
        url = item.get("url") or ""
        title_norm = re.sub(r"\s+", " ", (item.get("titulo") or "").lower().strip())
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
        text = f"{item.get('titulo') or ''}\n{item.get('descricao') or ''}"
        embedding = ollama_embed(text)
        if not embedding:
            continue
        url = item.get("url") or ""
        item_id = re.sub(r"[^a-zA-Z0-9]", "_", url[-60:]) if url else f"no_url_{len(ids)}"
        ids.append(item_id)
        embeddings.append(embedding)
        documents.append(text)
        metadatas.append({
            "titulo": (item.get("titulo") or "")[:200],
            "url": url,
            "fonte": item.get("fonte") or "",
            "uf": item.get("uf") or "",
            "data": datetime.now().strftime("%Y-%m-%d"),
            "status": item.get("status") or "desconhecido",
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

    log(f"=== Licitações Scanner v3 — {date_human} ===")
    log(f"Empresa: {empresa.get('nome', 'N/A')} | Área: {empresa.get('area', 'N/A')}")
    log(f"Portais carregados: {len(portais)} (CSV)")
    log(f"Filtro: somente licitações ABERTAS de {ANO_CORRENTE}")

    # ── 1. Coletar licitações ─────────────────────────────────
    all_items = []

    # 1a. PNCP — endpoint /proposta (somente abertas) ← PRIORITÁRIO
    log("[PNCP] Buscando licitações com propostas abertas...")
    propostas_abertas = buscar_pncp_propostas_abertas(config)
    all_items.extend(propostas_abertas)

    # 1b. PNCP — endpoint /publicacao (publicadas recentemente)
    log("[PNCP] Buscando publicações recentes...")
    publicacoes = buscar_pncp_publicacao(config)
    all_items.extend(publicacoes)

    # 1c. SearXNG — queries genéricas (já incluem ano no config.yml)
    for query in config.get("searxng_queries", []):
        results = searxng_search(query)
        all_items.extend(results)
        if results:
            log(f"[SearXNG] '{query}': {len(results)} resultados")

    # 1d. SearXNG — busca direcionada por portal (site:domínio)
    portal_items = buscar_por_portal(portais, config)
    all_items.extend(portal_items)

    log(f"[Total bruto] {len(all_items)} itens coletados")

    # ── 2. Filtrar: ano corrente + status aberto ──────────────
    all_items = filtrar_ano_corrente(all_items)
    log(f"[Filtro ano] {len(all_items)} itens do ano {ANO_CORRENTE}")

    all_items = filtrar_abertas(all_items)
    log(f"[Filtro status] {len(all_items)} itens sem indicação de encerramento")

    # ── 3. Deduplicar ─────────────────────────────────────────
    unique = deduplicate(all_items)
    log(f"[Dedup] {len(unique)} itens únicos")

    if not unique:
        log("[AVISO] Nenhuma licitação aberta encontrada hoje.")
        lic_dir = VAULT_ALFRED / "licitacoes"
        lic_dir.mkdir(parents=True, exist_ok=True)
        (lic_dir / f"{date_str}.md").write_text(
            f"---\ntitle: Licitações Abertas — {date_human}\ndate: {date_str}\n"
            f"type: licitacoes\nstatus: vazio\nfiltro: abertas_{ANO_CORRENTE}\n---\n\n"
            f"# Licitações Abertas — {date_human}\n\n"
            f"Nenhuma licitação aberta relevante encontrada para {ANO_CORRENTE}.\n",
            encoding="utf-8",
        )
        return

    # ── 4. Indexar no ChromaDB ────────────────────────────────
    collection_id = ensure_collection("licitacoes")
    if collection_id:
        upsert_chromadb(collection_id, unique)

    # ── 5. Classificar relevância com Ollama ──────────────────
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

IMPORTANTE: Todas as licitações abaixo são ABERTAS (com recebimento de propostas ativo ou publicadas recentemente em {ANO_CORRENTE}).

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

    # ── 6. Gerar relatório com Ollama ─────────────────────────
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
Crie um RELATÓRIO DE LICITAÇÕES ABERTAS em português brasileiro.

IMPORTANTE: Este relatório contém SOMENTE licitações ABERTAS (com propostas em andamento) de {ANO_CORRENTE}.

PERFIL:
{perfil_empresa}

LICITAÇÕES CLASSIFICADAS (por relevância):
{top_items_text}

DISTRIBUIÇÃO POR UF:
{stats_uf}

Formato obrigatório:

## Resumo do dia
Quantas licitações abertas analisadas, quantas relevantes, quais UFs mais ativas, tendência geral.

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

    # ── 7. Salvar no vault ────────────────────────────────────
    lic_dir = VAULT_ALFRED / "licitacoes"
    lic_dir.mkdir(parents=True, exist_ok=True)

    md_content = "\n".join([
        "---",
        f"title: Licitações Abertas — {date_human}",
        f"date: {date_str}",
        "type: licitacoes",
        f"filtro: abertas_{ANO_CORRENTE}",
        f"total_encontradas: {len(unique)}",
        f"relevantes: {len(scored_items)}",
        f"portais_consultados: {len(portais)}",
        f"tags: [licitacoes, attanotech, abertas, daily]",
        "---",
        "",
        f"# Licitações Abertas — {date_human}",
        f"> Filtro: somente licitações abertas de {ANO_CORRENTE}",
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

    # ── 8. Notificação desktop ────────────────────────────────
    try:
        import subprocess
        subprocess.run([
            "notify-send", "--icon=dialog-information",
            "Alfred — Licitações Abertas",
            f"Relatório de {date_human} pronto.\n"
            f"{len(unique)} abertas · {len(scored_items)} relevantes · {len(portais)} portais.",
        ], timeout=5, capture_output=True)
    except Exception:
        pass

    log("=== Scanner concluído ===")
    log(f"  Abertas: {len(unique)} | Relevantes: {len(scored_items)} | Portais: {len(portais)}")
    log(f"  Relatório: {md_path}")


if __name__ == "__main__":
    main()