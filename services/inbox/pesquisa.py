"""
Pesquisa pelo Painel (aba Pesquisar): o researcher acha e lê as fontes e grava no cofre; esta tela pede e mostra.

    pesquisar(tema, profundo)  → POST 127.0.0.1:PORT_RESEARCHER/research (rápido: síncrono, ~40–50 s;
                                 profundo: 202 + job_id, 4–5 min em segundo plano)
    andamento(job_id)          → GET /research/{job_id} (o Painel consulta no polling)
    recentes() / obter(slug)   → o que o researcher gravou em VAULT_ALFRED/research/<slug>/ (index, synthesis, sources)

Falhas que a tela mostra → ErroPesquisa (mensagem na voz do Alfred). O token do researcher fica só no cabeçalho.
"""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

import core

TIMEOUT_RAPIDO = 300       # s — o researcher: ~60 s de consultas + buscas + até 180 s de síntese
TIMEOUT_CURTO = 60         # s — início do profundo (o cache semântico embute o tema) e andamento
RECENTES_MAX = 20

_transporte: Optional[httpx.BaseTransport] = None   # os testes trocam por httpx.MockTransport


class ErroPesquisa(Exception):
    """Falha mostrada na tela (researcher fora do ar, resposta inesperada)."""


def _cliente(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=_transporte)


# ══════════════════════════════════════════════════════════════
#  Researcher
# ══════════════════════════════════════════════════════════════

def _researcher() -> str:
    return f"http://127.0.0.1:{os.getenv('PORT_RESEARCHER', '') or '7072'}"


def _auth() -> dict:
    return {"Authorization": f"Bearer {os.getenv('SHELL_EXECUTOR_TOKEN', '')}"}


def _detalhe(r: httpx.Response) -> str:
    try:
        d = r.json().get("detail")
    except Exception:
        d = None
    return d if isinstance(d, str) and d else f"HTTP {r.status_code}"


def _pedir(metodo: str, caminho: str, timeout: float, **kw) -> httpx.Response:
    try:
        with _cliente(timeout) as c:
            return c.request(metodo, f"{_researcher()}{caminho}", headers=_auth(), **kw)
    except httpx.TimeoutException:
        raise ErroPesquisa(f"O researcher não respondeu em {timeout:.0f} s. Veja journalctl --user -u alfred-researcher.")
    except httpx.HTTPError as e:
        raise ErroPesquisa(f"O researcher ({_researcher()}) não está no ar ({type(e).__name__}). "
                           "Confira systemctl --user status alfred-researcher.")


def pesquisar(tema: str, profundo: bool) -> dict:
    """POST /research. → a resposta do researcher: rápido 200 (`slug`); profundo 202 (`job_id`) ou 200 de cache.
    Acerto de cache semântico traz `matched_slug`: a síntese mostrada é a dele (ver slug_resultado)."""
    r = _pedir("POST", "/research", TIMEOUT_CURTO if profundo else TIMEOUT_RAPIDO,
               json={"topic": tema, "profundo": profundo})
    if r.status_code in (200, 202):
        return r.json()
    if r.status_code == 401:
        raise ErroPesquisa("O researcher recusou o token (SHELL_EXECUTOR_TOKEN diferente nos dois serviços?).")
    raise ErroPesquisa(f"A pesquisa falhou: {_detalhe(r)}")


def slug_resultado(resp: dict) -> str:
    """Pasta do cofre com a síntese da resposta (no cache semântico é a do tema parecido, não a do pedido)."""
    return resp.get("matched_slug") or resp.get("slug") or ""


def andamento(job_id: str) -> dict:
    """GET /research/{job_id}: status (running | done | error | interrompido), etapa, mensagem, resultado.
    ID com formato errado ou desconhecido → KeyError."""
    if not re.fullmatch(r"[0-9a-f]{12}", job_id or ""):
        raise KeyError(job_id)
    r = _pedir("GET", f"/research/{job_id}", TIMEOUT_CURTO)
    if r.status_code == 404:
        raise KeyError(job_id)
    if r.status_code != 200:
        raise ErroPesquisa(f"O researcher não informou o andamento: {_detalhe(r)}")
    return r.json()


# ══════════════════════════════════════════════════════════════
#  Cofre: VAULT_ALFRED/research/<slug>/
# ══════════════════════════════════════════════════════════════

def _pasta() -> Path:
    return core.vault() / "research"


def _dir(slug: str) -> Optional[Path]:
    """Pasta da pesquisa, ou None (slug inválido ou inexistente). slugify só gera letras, dígitos e hífen."""
    if not re.fullmatch(r"[\w-]{1,80}", slug or ""):
        return None
    d = _pasta() / slug
    return d if (d / "index.md").is_file() or (d / "synthesis.md").is_file() else None


def _ler(caminho: Path) -> str:
    try:
        return caminho.read_text(encoding="utf-8")
    except OSError:
        return ""


def _separar(texto: str) -> tuple[dict, str]:
    """Frontmatter do vault_write (uma chave por linha) → (campos, corpo). Lido linha a linha, sem YAML: um título
    com '#' ou '[' não pode derrubar a lista."""
    if not texto.startswith("---\n"):
        return {}, texto
    fim = texto.find("\n---", 3)
    if fim == -1:
        return {}, texto
    campos = {}
    for ln in texto[4:fim].splitlines():
        if m := re.match(r"^([\w-]+):\s*(.*)$", ln):
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] == '"':
                v = v[1:-1].replace('\\"', '"')
            campos[m.group(1)] = v
    return campos, texto[fim + 4:].lstrip("\n")


def _fontes(d: Path) -> list[dict]:
    """[{titulo, url}] de sources.md (rápido: "## 3. Título"; profundo: "## [3] Título"; os dois com "- **URL**:")."""
    fontes, vistas, titulo = [], set(), ""
    for ln in _ler(d / "sources.md").splitlines():
        if m := re.match(r"^## (?:\[\d+\]|\d+\.)\s*(.*)$", ln):
            titulo = m.group(1).strip()
        elif (m := re.match(r"^- \*\*URL\*\*:\s*(\S+)", ln)) and m.group(1) not in vistas:
            vistas.add(m.group(1))
            fontes.append({"titulo": titulo or m.group(1), "url": m.group(1)})
    return fontes


def recentes(limite: int = RECENTES_MAX) -> list[dict]:
    """As pesquisas do cofre, da mais nova para a mais velha (mtime do index.md)."""
    try:
        indices = list(_pasta().glob("*/index.md"))
    except RuntimeError:   # VAULT_ALFRED não definido
        return []
    itens = []
    for idx in indices:
        campos, _ = _separar(_ler(idx))
        try:
            mtime = idx.stat().st_mtime
        except OSError:
            continue
        itens.append({"slug": idx.parent.name, "titulo": campos.get("title") or idx.parent.name,
                      "modo": campos.get("modo") or "rapido",
                      "quando": datetime.fromtimestamp(mtime).astimezone()})
    itens.sort(key=lambda i: i["quando"], reverse=True)
    return itens[:limite]


def obter(slug: str) -> Optional[dict]:
    """A pesquisa para a tela: título, modo, síntese (markdown, sem frontmatter nem o "# título") e fontes."""
    d = _dir(slug)
    if not d:
        return None
    campos, _ = _separar(_ler(d / "index.md"))
    fm_sintese, sintese = _separar(_ler(d / "synthesis.md"))
    corpo = re.sub(r"^# [^\n]*\n+", "", sintese, count=1)   # o título vira o h1 da tela
    try:
        quando = datetime.fromtimestamp((d / "synthesis.md").stat().st_mtime).astimezone()
    except OSError:
        quando = None
    return {"slug": slug, "titulo": campos.get("title") or slug,
            "modo": fm_sintese.get("modo") or campos.get("modo") or "rapido",
            "sintese": corpo, "fontes": _fontes(d), "quando": quando}
