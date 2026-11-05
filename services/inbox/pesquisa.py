"""
Pesquisa pelo Painel (aba Pesquisar): o researcher acha e lê as fontes e grava no cofre; esta tela pede e mostra.
O OpenNotebook guarda a pesquisa para conversar, anotar e gerar podcast (ele não busca na web).

    pesquisar(tema, profundo)  → POST 127.0.0.1:PORT_RESEARCHER/research (rápido: síncrono, ~40–50 s;
                                 profundo: 202 + job_id, 4–5 min em segundo plano)
    andamento(job_id)          → GET /research/{job_id} (o Painel consulta no polling)
    recentes() / obter(slug)   → o que o researcher gravou em VAULT_ALFRED/research/<slug>/ (index, synthesis, sources)
    notebooks()                → os notebooks do OpenNotebook (API em 127.0.0.1:5055, sem auth e só em localhost)
    abrir_notebook(slug, destino=None)
                               → manda cada URL de sources.md + o relatório do Alfred para um notebook novo
                                 (destino None) ou para um que já existe ("Buscar mais fontes"); o id vai para o
                                 frontmatter do index.md (`opennotebook:`), e a pesquisa reabre o mesmo notebook

Falhas que a tela mostra → ErroPesquisa (mensagem na voz do Alfred). O token do researcher fica só no cabeçalho.
"""

import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

import core
from _shared import lib_web
from _shared.lib_alfred import gravar_atomico, log

TIMEOUT_RAPIDO = 300       # s — o researcher: ~60 s de consultas + buscas + até 180 s de síntese
TIMEOUT_CURTO = 60         # s — início do profundo (o cache semântico embute o tema), andamento, OpenNotebook
TIMEOUT_LISTA = 3          # s — lista de notebooks na tela: OpenNotebook fora não pode segurar a página
RECENTES_MAX = 20
OPENNOTEBOOK_API = "http://127.0.0.1:5055"
OPENNOTEBOOK_UI = "http://127.0.0.1:8502"
CHAVE_NOTEBOOK = "opennotebook"        # chave do frontmatter do index.md com o id do notebook
ID_NOTEBOOK = re.compile(r"notebook:[\w-]{1,64}")   # id do SurrealDB do OpenNotebook

_transporte: Optional[httpx.BaseTransport] = None   # os testes trocam por httpx.MockTransport
_dns = None                                          # idem: resolver falso para lib_web.url_publica
_LOCK_NOTEBOOK = threading.Lock()                    # dois pedidos juntos não criam dois notebooks


class ErroPesquisa(Exception):
    """Falha mostrada na tela (researcher ou OpenNotebook fora do ar, resposta inesperada)."""


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
                      "modo": campos.get("modo") or "rapido", "notebook": campos.get(CHAVE_NOTEBOOK) or "",
                      "quando": datetime.fromtimestamp(mtime).astimezone()})
    itens.sort(key=lambda i: i["quando"], reverse=True)
    return itens[:limite]


def obter(slug: str) -> Optional[dict]:
    """A pesquisa para a tela: título, modo, síntese (markdown, sem frontmatter nem o "# título"), fontes e o id do
    notebook no OpenNotebook ("" se ainda não tem). `sintese_completa` é o synthesis.md sem o frontmatter."""
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
            "sintese": corpo, "sintese_completa": sintese, "fontes": _fontes(d), "quando": quando,
            "notebook": campos.get(CHAVE_NOTEBOOK) or ""}


def _gravar_notebook(slug: str, notebook_id: str) -> None:
    """Põe (ou troca) `opennotebook: "<id>"` no frontmatter do index.md, sem mexer no resto do arquivo."""
    idx = _pasta() / slug / "index.md"
    texto = _ler(idx)
    linha = f'{CHAVE_NOTEBOOK}: "{notebook_id}"'
    fim = texto.find("\n---", 3) if texto.startswith("---\n") else -1
    if fim != -1:
        cabeca = [ln for ln in texto[4:fim].splitlines() if not ln.startswith(f"{CHAVE_NOTEBOOK}:")]
        texto = "---\n" + "\n".join(cabeca + [linha]) + texto[fim:]
    else:
        texto = f"---\n{linha}\n---\n\n{texto}"
    gravar_atomico(idx, texto)


# ══════════════════════════════════════════════════════════════
#  OpenNotebook
# ══════════════════════════════════════════════════════════════

def url_notebook(notebook_id: str) -> str:
    return f"{OPENNOTEBOOK_UI}/notebooks/{quote(notebook_id, safe='')}"


def id_valido(notebook_id) -> bool:
    return isinstance(notebook_id, str) and bool(ID_NOTEBOOK.fullmatch(notebook_id))


def _json(r: httpx.Response):
    try:
        return r.json()
    except ValueError:
        return None


def _on(c: httpx.Client, metodo: str, caminho: str, **kw) -> httpx.Response:
    try:
        return c.request(metodo, f"{OPENNOTEBOOK_API}{caminho}", **kw)
    except httpx.HTTPError as e:
        raise ErroPesquisa(f"O OpenNotebook não respondeu em {OPENNOTEBOOK_API} ({type(e).__name__}). Ele está no ar?")


def notebooks() -> list[dict]:
    """Notebooks não arquivados, do mais recente para o mais antigo → [{id, nome, fontes, url}]."""
    with _cliente(TIMEOUT_LISTA) as c:
        r = _on(c, "GET", "/api/notebooks", params={"archived": "false", "order_by": "updated desc"})
    dados = _json(r) if r.status_code == 200 else None
    if not isinstance(dados, list):
        raise ErroPesquisa(f"O OpenNotebook não devolveu a lista de notebooks (HTTP {r.status_code}).")
    return [{"id": n["id"], "nome": n.get("name") or n["id"], "fontes": n.get("source_count") or 0,
             "url": url_notebook(n["id"])}
            for n in dados if isinstance(n, dict) and id_valido(n.get("id")) and not n.get("archived")]


def _urls_no_notebook(c: httpx.Client, nid: str) -> set[str]:
    """URLs que o notebook já tem (as 100 fontes mais recentes), para "Buscar mais fontes" não repetir página.
    Sem resposta → conjunto vazio: no pior caso uma página entra duas vezes."""
    try:
        r = _on(c, "GET", "/api/sources", params={"notebook_id": nid, "limit": 100})
    except ErroPesquisa:
        return set()
    dados = _json(r) if r.status_code == 200 else None
    if not isinstance(dados, list):
        return set()
    return {u for f in dados if isinstance(f, dict) and (u := (f.get("asset") or {}).get("url"))}


def abrir_notebook(slug: str, destino: Optional[str] = None) -> dict:
    """Leva a pesquisa ao OpenNotebook → {"url", "id", "criado", "fontes" (enviadas), "repetidas", "falhas"}.

    destino None ("Abrir um notebook sobre isso"): a pesquisa já tem notebook com fontes → só devolve o link; tem
    notebook sem nenhuma fonte (o envio falhou antes) → manda de novo para ele; não tem (ou foi apagado lá) → cria
    um com o título da pesquisa e grava o id na hora (repetir não cria outro).
    destino = id ("Buscar mais fontes" / "Adicionar a outro notebook"): o notebook precisa existir; a pesquisa que já
    foi para ele não vai de novo; URLs que ele já tem ficam de fora (`repetidas`); o id vai para o index.md depois
    que alguma fonte entra.
    Só vai URL http/https de host público: o OpenNotebook baixa a página e roda com a rede do host. O processamento
    das fontes é assíncrono lá; uma fonte recusada entra em `falhas` e não para as outras. Slug desconhecido → KeyError."""
    with _LOCK_NOTEBOOK:
        p = obter(slug)
        if not p:
            raise KeyError(slug)
        with _cliente(TIMEOUT_CURTO) as c:
            criado, relatorio = False, "Relatório do Alfred"
            if destino:
                if p["notebook"] == destino:
                    return {"url": url_notebook(destino), "id": destino, "criado": False, "fontes": 0,
                            "repetidas": 0, "falhas": []}
                r = _on(c, "GET", f"/api/notebooks/{quote(destino, safe='')}")
                if r.status_code == 404:
                    raise ErroPesquisa("Esse notebook não existe mais no OpenNotebook. Recarregue a página.")
                if r.status_code != 200:
                    raise ErroPesquisa(f"O OpenNotebook respondeu HTTP {r.status_code} ao procurar o notebook.")
                nid, relatorio = destino, f"Relatório do Alfred — {p['titulo']}"
            else:
                nid = p["notebook"]
                if nid:
                    r = _on(c, "GET", f"/api/notebooks/{quote(nid, safe='')}")
                    dados = _json(r) if r.status_code == 200 else None
                    if isinstance(dados, dict) and dados.get("source_count"):
                        return {"url": url_notebook(nid), "id": nid, "criado": False, "fontes": 0,
                                "repetidas": 0, "falhas": []}
                    if r.status_code == 404:
                        log(f"[pesquisa] notebook {nid} não existe mais no OpenNotebook — criando outro para {slug}")
                        nid = ""
                    elif r.status_code != 200:
                        raise ErroPesquisa(f"O OpenNotebook respondeu HTTP {r.status_code} ao procurar o notebook.")
                if not nid:
                    modo = "profunda" if p["modo"] == "profundo" else "rápida"
                    r = _on(c, "POST", "/api/notebooks", json={
                        "name": p["titulo"],
                        "description": f"Pesquisa {modo} do Alfred — research/{slug}/ no cofre ({len(p['fontes'])} fontes)",
                    })
                    dados = _json(r) if r.status_code in (200, 201) else None
                    nid = dados.get("id") if isinstance(dados, dict) else None
                    if not id_valido(nid):
                        raise ErroPesquisa(f"O OpenNotebook não criou o notebook (HTTP {r.status_code}). Nada foi gravado.")
                    _gravar_notebook(slug, nid)
                    criado = True
                    log(f"[pesquisa] notebook {nid} criado para research/{slug}/")

            ja_tem = set() if criado else _urls_no_notebook(c, nid)
            falhas, fontes, repetidas = [], [], 0
            for f in p["fontes"]:
                if f["url"] in ja_tem:
                    repetidas += 1
                elif lib_web.url_publica(f["url"], dns=_dns):
                    fontes.append({"type": "link", "url": f["url"], "title": f["titulo"]})
                else:
                    falhas.append(f["url"])
            fontes.append({"type": "text", "content": p["sintese_completa"], "title": relatorio})
            enviadas = 0
            for fonte in fontes:
                nome = fonte.get("url") or fonte["title"]
                try:
                    r = _on(c, "POST", "/api/sources/json",
                            json={**fonte, "notebooks": [nid], "embed": True, "async_processing": True})
                    ok = r.status_code in (200, 201, 202)
                    motivo = f"HTTP {r.status_code}"
                except ErroPesquisa as e:
                    ok, motivo = False, str(e)
                if ok:
                    enviadas += 1
                else:
                    falhas.append(nome)
                    log(f"[pesquisa] OpenNotebook recusou a fonte {nome} ({motivo})")
            if not enviadas:
                raise ErroPesquisa("O OpenNotebook não aceitou nenhuma fonte. Tente de novo: o mesmo notebook é usado.")
            if destino:
                _gravar_notebook(slug, nid)
                log(f"[pesquisa] research/{slug}/ enviada ao notebook {nid} ({enviadas} fontes, {repetidas} repetidas)")
    return {"url": url_notebook(nid), "id": nid, "criado": criado, "fontes": enviadas, "repetidas": repetidas,
            "falhas": falhas}
