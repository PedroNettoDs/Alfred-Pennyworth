"""
Alfred — artigos científicos em bases abertas (OpenAlex e Semantic Scholar), sem LLM.

Usada pelo modo profundo do researcher (subpergunta que o plano marcou como acadêmica) e pela ferramenta
buscar_artigos da conversa. Ideia trazida do Feynman (github.com/Companion-Inc/feynman, MIT) — ver
docs/planos/feynman-ideias.md, Fase 1. Cada resultado tem o formato de searxng_search (title, url, snippet,
source), para entrar no dedup e no filtro de relevância do researcher sem mudança, mais os identificadores
estáveis (doi, arxiv_id) e url_html (a versão HTML do arXiv, quando o artigo é de lá).

    buscar(consulta, fonte, limite)         → list[dict]; base fora, 429 ou resposta estranha → ErroAcademico
    buscar_todas(consulta, fontes, limite)  → junta as fontes (a que falhar é pulada, com log), sem duplicatas
    identificador(resultado)                → "arXiv:1803.03635" | "doi:10.1038/nature17946" | ""

Só a consulta sai da máquina; nada do cofre ou das conversas. Nenhuma chave: OPENALEX_MAILTO (opcional) só põe o
pedido no pool "educado" do OpenAlex. O Semantic Scholar anônimo divide um pool com todo mundo e devolve 429 com
frequência: uma segunda tentativa depois de ESPERA_429 segundos, e então a fonte é pulada.
"""

import os
import re
import time
from typing import Optional

import httpx

from _shared.lib_alfred import deduplicate_by_url_and_title, log

FONTES       = ("openalex", "semanticscholar")
TIMEOUT      = 20            # s por pedido
ESPERA_429   = 1.0           # s antes da segunda tentativa no Semantic Scholar
LIMITE_MAX   = 25
RESUMO_MAX   = 600           # caracteres do resumo no snippet (o filtro de relevância embute título + trecho)
OPENALEX_URL = "https://api.openalex.org/works"
S2_URL       = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_FIELDS    = "title,abstract,year,citationCount,externalIds,url,authors"
USER_AGENT   = "AlfredResearcher/3.3 (+local)"
NOME         = {"openalex": "OpenAlex", "semanticscholar": "Semantic Scholar"}

# 1803.03635 (com ou sem v2) ou o formato antigo hep-th/9901001 — em URLs abs/, pdf/ ou html/ do arXiv
_ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.I)

_transporte: Optional[httpx.BaseTransport] = None   # os testes trocam por httpx.MockTransport


class ErroAcademico(Exception):
    """Base fora do ar, limite de pedidos (429) ou resposta que não é a esperada."""


def identificador(resultado: dict) -> str:
    if resultado.get("arxiv_id"):
        return f"arXiv:{resultado['arxiv_id']}"
    if resultado.get("doi"):
        return f"doi:{resultado['doi']}"
    return ""


def buscar(consulta: str, fonte: str = "openalex", limite: int = 10) -> list[dict]:
    """Artigos de uma base, na ordem de relevância dela. Consulta vazia → []."""
    consulta = re.sub(r"\s+", " ", consulta or "").strip()
    if not consulta:
        return []
    if fonte not in FONTES:
        raise ErroAcademico(f"fonte desconhecida: {fonte!r} (use {' | '.join(FONTES)})")
    limite = max(1, min(int(limite), LIMITE_MAX))
    with httpx.Client(timeout=TIMEOUT, transport=_transporte, headers={"User-Agent": USER_AGENT}) as client:
        if fonte == "openalex":
            return _openalex(client, consulta, limite)
        return _semanticscholar(client, consulta, limite)


def buscar_todas(consulta: str, fontes=FONTES, limite: int = 10) -> list[dict]:
    """As fontes em ordem; a que falhar é pulada (log). Sem duplicatas: o mesmo artigo nas duas bases tem a
    mesma URL (arxiv.org/abs/<id> ou doi.org/<doi>), e deduplicate_by_url_and_title fica com a primeira."""
    todos: list[dict] = []
    for fonte in fontes:
        try:
            achados = buscar(consulta, fonte, limite)
        except ErroAcademico as e:
            log(f"[academico] {e} — pulando {fonte}")
            continue
        log(f"[academico] {NOME.get(fonte, fonte)}: {len(achados)} artigo(s) para {consulta!r}")
        todos.extend(achados)
    return deduplicate_by_url_and_title(todos)


# ── Pedido e normalização ─────────────────────────────────────
def _pedir(client: httpx.Client, nome: str, url: str, params: dict, tentativas_429: int = 0) -> dict:
    for tentativa in range(tentativas_429 + 1):
        try:
            resp = client.get(url, params=params)
        except httpx.HTTPError as e:
            raise ErroAcademico(f"{nome} não respondeu: {e!r}") from None
        if resp.status_code == 429 and tentativa < tentativas_429:
            log(f"[academico] {nome} limitou (429) — tentando de novo em {ESPERA_429} s")
            time.sleep(ESPERA_429)
            continue
        if resp.status_code != 200:
            raise ErroAcademico(f"{nome} respondeu HTTP {resp.status_code}")
        try:
            dados = resp.json()
        except ValueError:
            raise ErroAcademico(f"{nome} devolveu algo que não é JSON") from None
        if not isinstance(dados, dict):
            raise ErroAcademico(f"{nome} devolveu JSON inesperado")
        return dados
    raise ErroAcademico(f"{nome} limitou (429)")


def _doi(valor) -> str:
    """'https://doi.org/10.1038/X' ou '10.1038/X' → '10.1038/x'. Outra coisa → ''."""
    d = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(valor or "").strip(), flags=re.I).lower()
    return d if d.startswith("10.") else ""


def _arxiv_id(*urls) -> str:
    for u in urls:
        m = _ARXIV.search(str(u or ""))
        if m:
            return m.group(1)
    return ""


def _resultado(fonte: str, titulo, resumo, ano, citacoes, autores: list, doi: str, arxiv_id: str,
               url_reserva) -> Optional[dict]:
    """Um resultado no formato comum. URL canônica: arxiv.org/abs/<id> > doi.org/<doi> > a URL que a base deu."""
    titulo = re.sub(r"\s+", " ", str(titulo or "")).strip()
    if not titulo:
        return None
    if arxiv_id:
        url, url_html = f"https://arxiv.org/abs/{arxiv_id}", f"https://arxiv.org/html/{arxiv_id}"
    elif doi:
        url, url_html = f"https://doi.org/{doi}", ""
    else:
        url, url_html = str(url_reserva or "").strip(), ""
    if not url.startswith(("http://", "https://")):
        return None
    return {
        "title":    titulo,
        "url":      url,
        "snippet":  re.sub(r"\s+", " ", str(resumo or "")).strip()[:RESUMO_MAX],
        "source":   fonte,
        "doi":      doi,
        "arxiv_id": arxiv_id,
        "url_html": url_html,
        "ano":      ano if isinstance(ano, int) else None,
        "citacoes": citacoes if isinstance(citacoes, int) else None,
        "autores":  [str(a).strip() for a in autores if a and str(a).strip()][:3],
    }


# ── OpenAlex ──────────────────────────────────────────────────
def _abstract_openalex(invertido) -> str:
    """abstract_inverted_index ({palavra: [posições]}) → texto na ordem."""
    if not isinstance(invertido, dict):
        return ""
    posicoes = sorted((p, palavra) for palavra, ps in invertido.items()
                      if isinstance(ps, list) for p in ps if isinstance(p, int))
    return " ".join(palavra for _, palavra in posicoes)


def _openalex(client: httpx.Client, consulta: str, limite: int) -> list[dict]:
    params = {"search": consulta, "per-page": limite,
              "select": "id,doi,title,publication_year,cited_by_count,authorships,primary_location,locations,"
                        "abstract_inverted_index"}
    mailto = os.getenv("OPENALEX_MAILTO", "").strip()
    if mailto:
        params["mailto"] = mailto
    dados = _pedir(client, NOME["openalex"], OPENALEX_URL, params)
    saida = []
    for w in dados.get("results") or []:
        if not isinstance(w, dict):
            continue
        principal = w.get("primary_location") if isinstance(w.get("primary_location"), dict) else {}
        locais = [principal] + [l for l in (w.get("locations") or []) if isinstance(l, dict)]
        urls = [l.get("landing_page_url") for l in locais] + [l.get("pdf_url") for l in locais]
        autores = [(a.get("author") or {}).get("display_name") for a in (w.get("authorships") or [])
                   if isinstance(a, dict)]
        r = _resultado(NOME["openalex"], w.get("title"), _abstract_openalex(w.get("abstract_inverted_index")),
                       w.get("publication_year"), w.get("cited_by_count"), autores, _doi(w.get("doi")),
                       _arxiv_id(*urls), principal.get("landing_page_url") or w.get("id"))
        if r:
            saida.append(r)
    return saida


# ── Semantic Scholar ──────────────────────────────────────────
def _semanticscholar(client: httpx.Client, consulta: str, limite: int) -> list[dict]:
    dados = _pedir(client, NOME["semanticscholar"], S2_URL,
                   {"query": consulta, "limit": limite, "fields": S2_FIELDS}, tentativas_429=1)
    saida = []
    for p in dados.get("data") or []:
        if not isinstance(p, dict):
            continue
        ids = p.get("externalIds") if isinstance(p.get("externalIds"), dict) else {}
        autores = [a.get("name") for a in (p.get("authors") or []) if isinstance(a, dict)]
        r = _resultado(NOME["semanticscholar"], p.get("title"), p.get("abstract"), p.get("year"),
                       p.get("citationCount"), autores, _doi(ids.get("DOI")), str(ids.get("ArXiv") or "").strip(),
                       p.get("url"))
        if r:
            saida.append(r)
    return saida
