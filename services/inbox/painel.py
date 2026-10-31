"""
Painel do Alfred — interface web local de aprovações, servida pela própria API do inbox (porta 7073).

    http://127.0.0.1:7073/painel      (login com o mesmo SHELL_EXECUTOR_TOKEN da API)

Telas: Início, Pendências (lista + detalhe), Agenda, Limpeza e Sistema (só leitura). As ações chamam as
MESMAS funções do CLI/notificação/API — whatsapp.responder_texto, whatsapp.agendar_pendencia,
core.responder, agenda.desfazer, limpeza.aprovar/recusar —; nada aqui grava YAML.

Segurança (toda rota /painel/*, inclusive login e estáticos):
  - só 127.0.0.1/::1 (a API escuta em 0.0.0.0 por causa do Docker: containers chegam como 172.17.x.x)
    e header Host local (anti DNS-rebinding);
  - sessão sem estado no servidor: cookie `alfred_painel` = v1.<expira>.<nonce>.<HMAC(token)>, HttpOnly,
    SameSite=Strict, PAINEL_SESSAO_DIAS (padrão 7). Trocar o token invalida todas as sessões;
  - POST exige X-CSRF-Token (HMAC do nonce da sessão) e Origin/Referer da própria origem;
  - o token nunca vai para URL, JS, template ou log; CSP sem nada inline.

Templates Jinja2 em painel_web/templates/ (contrato de componentes em painel_web/COMPONENTES.md).
"""

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import subprocess
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturoTimeout
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape
from starlette.concurrency import run_in_threadpool

import agenda
import core
import limpeza
import whatsapp
from _shared import lib_whatsapp

try:
    from zoneinfo import ZoneInfo
    FUSO = ZoneInfo("America/Sao_Paulo")
except Exception:   # sem tzdata: hora local da máquina (a do Pedro já é São Paulo)
    FUSO = None

DIR_WEB = Path(__file__).parent / "painel_web"
COOKIE = "alfred_painel"
HOSTS_LOCAIS = ("127.0.0.1", "localhost", "[::1]")
CLIENTES_LOCAIS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
ATRASO_LOGIN_FALHO = 0.5          # segundos — desestimula tentativa e erro no token
CACHE_WHATSAPP_S = 15             # estado da instância (Início/Sistema): no máximo 1 consulta a cada 15 s
TIMEOUT_CHECAGEM_S = 2            # cada item da tela Sistema

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
ESTATICOS = {"painel.css": "text/css; charset=utf-8", "painel.js": "text/javascript; charset=utf-8"}

MSG_LOOPBACK = "Painel disponível apenas em 127.0.0.1."
MSG_SESSAO = "Sessão expirada. Entre de novo."
MSG_CSRF = "O servidor recusou a ação por segurança. Recarregue a página e tente de novo."


# ══════════════════════════════════════════════════════════════
#  Rede, cabeçalhos e rota
# ══════════════════════════════════════════════════════════════

def _hostname(host: str) -> str:
    """'127.0.0.1:7073' → '127.0.0.1'; '[::1]:7073' → '[::1]'."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1] if "]" in host else host
    return host.split(":")[0]


def _rede_ok(request: Request) -> bool:
    cliente = (request.client.host if request.client else "") or ""
    return cliente in CLIENTES_LOCAIS and _hostname(request.headers.get("host", "")) in HOSTS_LOCAIS


def _erro(codigo: int, texto: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=codigo, content={"erro": texto, **extra})


def _cabecalhos(request: Request, resp: Response) -> None:
    h = resp.headers
    h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "same-origin"
    h["X-Frame-Options"] = "DENY"
    if "cache-control" not in h:
        h["Cache-Control"] = "no-store"


class _RotaPainel(APIRoute):
    """Toda rota do painel passa primeiro pela checagem de rede (antes até de ler o corpo) e sai com os
    cabeçalhos de segurança — inclusive nas recusas."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            resp = _erro(403, MSG_LOOPBACK) if not _rede_ok(request) else await original(request)
            _cabecalhos(request, resp)
            return resp

        return handler


router = APIRouter(prefix="/painel", include_in_schema=False, route_class=_RotaPainel)


# ══════════════════════════════════════════════════════════════
#  Sessão (cookie assinado, sem estado no servidor) e CSRF
# ══════════════════════════════════════════════════════════════

_REVOGADOS: dict = {}            # nonce → expira (Sair); some ao reiniciar a API, mas o cookie já foi apagado
_REVOGADOS_LOCK = threading.Lock()


def _token() -> str:
    # lido na hora (não de main.TOKEN): trocar o .env e reiniciar a API invalida as sessões antigas
    return os.getenv("SHELL_EXECUTOR_TOKEN", "")


def _hmac(msg: str) -> str:
    return hmac.new(_token().encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


SESSAO_DIAS_PADRAO = 7.0
SESSAO_DIAS_MIN = 1 / 24          # 1 hora: menos que isso o cookie nasce vencido (Max-Age=0) e o login entra em laço
SESSAO_DIAS_MAX = 3650.0          # 10 anos: acima disso (ou inf/1e400) não cabe em Max-Age e o login dava 500


def dias_sessao() -> float:
    """PAINEL_SESSAO_DIAS do .env; valor ilegível, não finito ou fora de 1 h…3650 dias → padrão de 7 dias."""
    try:
        d = float(os.getenv("PAINEL_SESSAO_DIAS", "") or SESSAO_DIAS_PADRAO)
    except ValueError:
        return SESSAO_DIAS_PADRAO
    if not math.isfinite(d) or not (SESSAO_DIAS_MIN <= d <= SESSAO_DIAS_MAX):
        return SESSAO_DIAS_PADRAO
    return d


def _nova_sessao() -> tuple[str, int]:
    """→ (valor do cookie, max_age em segundos)."""
    max_age = int(dias_sessao() * 86400)
    expira = int(time.time()) + max_age
    nonce = secrets.token_urlsafe(18)
    return f"v1.{expira}.{nonce}.{_hmac(f'painel-sessao|{expira}|{nonce}')}", max_age


def _nonce_da_sessao(request: Request) -> Optional[str]:
    """Nonce da sessão válida no cookie, ou None (sem cookie, adulterado, expirado, revogado ou sem token)."""
    if not _token():
        return None
    partes = (request.cookies.get(COOKIE) or "").split(".")
    if len(partes) != 4 or partes[0] != "v1" or not (partes[1].isascii() and partes[1].isdigit()):
        return None
    _v, expira, nonce, assinatura = partes
    # compara bytes: compare_digest(str, str) levanta TypeError com caractere não ASCII (cookie forjado → 500)
    if not hmac.compare_digest(assinatura.encode("utf-8", "surrogateescape"),
                               _hmac(f"painel-sessao|{expira}|{nonce}").encode("utf-8")):
        return None
    if int(expira) <= time.time():
        return None
    with _REVOGADOS_LOCK:
        if nonce in _REVOGADOS:
            return None
    return nonce


def sessao_valida(request: Request) -> bool:
    """Para main.py (GET /pendencias/{id}/audio): sessão do painel vinda da própria máquina."""
    return _rede_ok(request) and _nonce_da_sessao(request) is not None


def _csrf(nonce: str) -> str:
    return _hmac(f"painel-csrf|{nonce}")


def _origem_ok(request: Request) -> bool:
    """Origin (ou, sem ele, Referer) tem de ser a própria origem do painel."""
    origem = request.headers.get("origin") or request.headers.get("referer") or ""
    try:
        partes = urlsplit(origem)
    except ValueError:
        return False
    host = request.headers.get("host", "").lower()
    return (partes.scheme in ("http", "https") and partes.netloc.lower() == host
            and _hostname(partes.netloc) in HOSTS_LOCAIS)


def _checar_post(request: Request) -> Optional[JSONResponse]:
    """Sessão + CSRF + origem de um POST de ação. → resposta de recusa ou None."""
    nonce = _nonce_da_sessao(request)
    if not nonce:
        return _erro(401, MSG_SESSAO)
    enviado = request.headers.get("x-csrf-token", "")
    if (not enviado or not hmac.compare_digest(enviado.encode("utf-8", "surrogateescape"), _csrf(nonce).encode("utf-8"))
            or not _origem_ok(request)):
        return _erro(403, MSG_CSRF)
    return None


def _proximo_seguro(valor: Optional[str]) -> str:
    v = (valor or "").strip()
    if v.startswith("/painel") and not v.startswith("//") and "\\" not in v and not v.startswith("/painel/login"):
        return v
    return ""


def _para_login(request: Request) -> RedirectResponse:
    aqui = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    proximo = _proximo_seguro(aqui)
    url = "/painel/login" + (f"?proximo={quote(proximo, safe='')}" if proximo and proximo != "/painel" else "")
    return RedirectResponse(url, status_code=303)


async def _corpo_json(request: Request) -> dict:
    try:
        dados = await request.json()
    except Exception:
        return {}
    return dados if isinstance(dados, dict) else {}


# ══════════════════════════════════════════════════════════════
#  Datas (America/Sao_Paulo, DD/MM/AAAA HH:MM + relativo)
# ══════════════════════════════════════════════════════════════

def _agora() -> datetime:
    return datetime.now(FUSO) if FUSO else datetime.now().astimezone()


def _dt(valor) -> Optional[datetime]:
    if isinstance(valor, datetime):
        d = valor
    elif isinstance(valor, str) and valor.strip():
        try:
            d = datetime.fromisoformat(valor.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if d.tzinfo is None:
        d = d.astimezone()   # sem fuso no YAML = hora local da máquina
    return d.astimezone(FUSO) if FUSO else d


def _so_data(valor) -> bool:
    return isinstance(valor, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", valor.strip()))


def fmt_data(valor) -> str:
    if _so_data(valor):
        a, m, d = valor.strip().split("-")
        return f"{d}/{m}/{a}"
    d = _dt(valor)
    return d.strftime("%d/%m/%Y %H:%M") if d else ""


def fmt_relativo(valor, agora: Optional[datetime] = None) -> str:
    d = _dt(valor)
    if not d:
        return ""
    seg = ((agora or _agora()) - d).total_seconds()
    futuro, seg = seg < 0, abs(seg)
    if seg < 60:
        return "agora"
    if seg < 3600:
        n, un = int(seg // 60), "min"
    elif seg < 86400:
        n, un = int(seg // 3600), "h"
    else:
        n = int(seg // 86400)
        un = "dia" if n == 1 else "dias"
    return f"em {n} {un}" if futuro else f"há {n} {un}"


def _iso(valor) -> str:
    d = _dt(valor)
    return d.isoformat(timespec="minutes") if d else ""


# ══════════════════════════════════════════════════════════════
#  Templates
# ══════════════════════════════════════════════════════════════

def _markdown_seguro(texto: str) -> Markup:
    """
    Markdown mínimo do .md da pendência (títulos, listas, citações, negrito, itálico, código e links http/https).
    Tudo é escapado ANTES de virar tag: nenhum HTML vindo do e-mail ou do LLM chega à página.
    """
    def inline(s: str) -> str:
        s = str(escape(s))
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?![\*\w])", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                   r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', s)
        return s

    out, bloco, tipo = [], [], None

    def fechar():
        nonlocal bloco, tipo
        if bloco:
            if tipo == "ul":
                out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in bloco) + "</ul>")
            elif tipo == "quote":
                out.append("<blockquote><p>" + "<br>".join(inline(x) for x in bloco) + "</p></blockquote>")
            else:
                out.append("<p>" + "<br>".join(inline(x) for x in bloco) + "</p>")
        bloco, tipo = [], None

    for linha in (texto or "").splitlines():
        s = linha.rstrip()
        if not s.strip() or re.fullmatch(r"\s*-{3,}\s*", s):
            fechar()
            continue
        if m := re.match(r"^(#{1,6})\s+(.*)", s):
            fechar()
            nivel = min(6, len(m.group(1)) + 1)   # o h1 da página é o título da tela
            out.append(f"<h{nivel}>{inline(m.group(2))}</h{nivel}>")
            continue
        if m := re.match(r"^\s*[-*]\s+(.*)", s):
            if tipo != "ul":
                fechar(); tipo = "ul"
            bloco.append(m.group(1))
            continue
        if m := re.match(r"^\s*>\s?(.*)", s):
            if tipo != "quote":
                fechar(); tipo = "quote"
            bloco.append(m.group(1))
            continue
        if tipo != "p":
            fechar(); tipo = "p"
        bloco.append(s.strip())
    fechar()
    return Markup("\n".join(out))


def _texto_explicativo(entry: dict) -> Optional[Markup]:
    """Corpo do .md sem frontmatter, sem o título repetido e sem "Como responder" (instruções do CLI)."""
    rel = (entry.get("arquivos") or {}).get("md")
    if not rel:
        return None
    try:
        f = core.vault() / rel
        txt = f.read_text(encoding="utf-8") if f.is_file() else ""
    except OSError:
        txt = ""
    if not txt.strip():
        return None
    if txt.startswith("---"):
        fim = txt.find("\n---", 3)
        txt = txt[fim + 4:] if fim != -1 else txt
    # fora: o título (é o da tela), a linha "> evento · de … · aguardando resposta" logo abaixo dele (fica
    # velha quando a pendência é resolvida), "Dados extraídos" (o cartão de dados mostra) e "Como responder"
    linhas, pular, titulo_visto, meta = [], False, False, False
    for ln in txt.splitlines():
        if ln.startswith("# ") and not titulo_visto:
            titulo_visto = meta = True
            continue
        if meta:
            if not ln.strip() or ln.startswith(">"):
                continue
            meta = False
        if ln.startswith("## "):
            pular = ln.strip().lower() in ("## como responder", "## dados extraídos")
        if not pular:
            linhas.append(ln)
    return _markdown_seguro("\n".join(linhas))


_env = Environment(loader=FileSystemLoader(str(DIR_WEB / "templates")), autoescape=True)
_env.filters["data"] = fmt_data
_env.filters["relativo"] = fmt_relativo


def _render(nome: str, **ctx) -> str:
    return _env.get_template(nome).render(**ctx)


def _contadores() -> dict:
    aguard = core.listar_pendencias("aguardando")
    wa = sum(1 for p in aguard if p.get("tipo") == "whatsapp")
    try:
        lim = len(limpeza.listar_lotes("aguardando"))
    except Exception:   # limpeza.yml ilegível não derruba o painel
        lim = 0
    return {"aguardando": len(aguard), "whatsapp": wa, "email": len(aguard) - wa, "limpeza_aguardando": lim}


def _pagina(request: Request, nome: str, ativo: str, status: int = 200, **ctx) -> HTMLResponse:
    nonce = _nonce_da_sessao(request)
    ctx.setdefault("simulado", lib_whatsapp.simulado())
    html = _render(nome, csrf=_csrf(nonce) if nonce else "", ativo=ativo, contadores=_contadores(), **ctx)
    return HTMLResponse(html, status_code=status)


# ══════════════════════════════════════════════════════════════
#  Dados das telas
# ══════════════════════════════════════════════════════════════

ROTULO_STATUS = {
    "aguardando": "aguardando", "respondida": "respondida", "respondida_manual": "respondida pelo celular",
    "vou": "vou", "nao_vou": "não vou", "ignorada": "ignorada", "expirada": "expirada",
    "pendente": "pendente", "no_calendario": "no calendário", "em_execucao": "em execução",
    "desfazer": "remoção pendente", "desfeito": "desfeito", "cancelado": "cancelado", "erro": "erro",
    "aprovado": "aprovado", "recusado": "recusado", "expirado": "expirado", "concluido": "concluído",
    "simulado": "simulado", "parcial": "parcial", "falhou": "falhou",
}


def _ascii_baixo(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii").lower()


def _contato(p: dict) -> str:
    wa, em = p.get("whatsapp") or {}, p.get("email") or {}
    return (wa.get("contato") or em.get("from_name") or em.get("from")
            or (p.get("evento") or {}).get("organizador") or p.get("titulo") or p.get("id") or "")


def _chegada(p: dict):
    return (p.get("whatsapp") or {}).get("recebido_em") or p.get("criado_em")


def _resolvida_em(p: dict):
    return p.get("respondido_em") or p.get("lembrado_em") or p.get("criado_em")


def _ts(valor) -> float:
    d = _dt(valor)
    return d.timestamp() if d else 0.0


def _linha(p: dict, agora: datetime) -> dict:
    """Pendência → o que a linha da lista mostra."""
    wa = p.get("whatsapp") or {}
    eh_wa = p.get("tipo") == "whatsapp"
    resumo = ((p.get("evento") or {}).get("resumo") or wa.get("texto") or p.get("titulo")) if eh_wa else p.get("titulo")
    resolvida = p.get("status") != "aguardando"
    quando = _resolvida_em(p) if resolvida else _chegada(p)
    return {
        "id": p.get("id"), "tipo": p.get("tipo"), "status": p.get("status"), "contato": _contato(p),
        "resumo": " ".join(str(resumo or "").split()), "urgencia": wa.get("urgencia") if eh_wa else None,
        "iso": _iso(quando), "relativo": fmt_relativo(quando, agora), "absoluto": fmt_data(quando),
        "comparar": p.get("status") == "respondida_manual",
        "rascunho": wa.get("rascunho"), "resposta_texto": p.get("resposta_texto"),
    }


def _busca(p: dict, q: str) -> bool:
    wa, em, ev = p.get("whatsapp") or {}, p.get("email") or {}, p.get("evento") or {}
    campos = [_contato(p), p.get("titulo"), ev.get("resumo"), wa.get("texto"), wa.get("numero"),
              p.get("resposta_texto"), em.get("subject"), em.get("from"), em.get("from_name")]
    alvo = _ascii_baixo(" ".join(str(c) for c in campos if c))
    return all(t in alvo for t in _ascii_baixo(q).split())


def _filtros(aba, tipo, q) -> tuple[str, str, str]:
    aba = aba if aba in ("aguardando", "resolvidas") else "aguardando"
    tipo = tipo if tipo in ("whatsapp", "email") else ""
    return aba, tipo, (q or "").strip()[:100]


def _lista_pendencias(aba: str, tipo: str, q: str) -> list[dict]:
    agora = _agora()
    todas = core.listar_pendencias("todas")
    if tipo == "whatsapp":
        todas = [p for p in todas if p.get("tipo") == "whatsapp"]
    elif tipo == "email":
        todas = [p for p in todas if p.get("tipo") != "whatsapp"]
    if q:
        todas = [p for p in todas if _busca(p, q)]
    if aba == "aguardando":
        itens = [p for p in todas if p.get("status") == "aguardando"]
        # urgência alta primeiro, depois as mais recentes
        itens.sort(key=lambda p: (0 if (p.get("whatsapp") or {}).get("urgencia") == "alta" else 1, -_ts(_chegada(p))))
    else:
        itens = [p for p in todas if p.get("status") != "aguardando"]
        itens.sort(key=lambda p: -_ts(_resolvida_em(p)))
        itens = itens[:50]
    return [_linha(p, agora) for p in itens]


def _vazio_lista(aba: str, tipo: str, q: str) -> str:
    if q or tipo:
        return "Nada encontrado para essa busca."
    return "Nenhuma pendência aguardando, senhor." if aba == "aguardando" else "Nenhuma pendência resolvida ainda."


def _arquivo_desligado() -> Path:
    return core.PROJECT_ROOT / "logs" / "alfred_desligado"


def _estado_alfred() -> dict:
    f = _arquivo_desligado()
    if not f.exists():
        return {"ligado": True, "desde": None}
    try:
        desde = (f.read_text(encoding="utf-8").splitlines() or [""])[0].strip()
    except OSError:
        desde = ""
    return {"ligado": False, "desde": desde or None}


_CACHE_WA: dict = {}
_CACHE_WA_LOCK = threading.Lock()


def _status_whatsapp() -> dict:
    """whatsapp.status(timeout=2) com cache de 15 s (o polling do Início não martela a Evolution)."""
    chave = (lib_whatsapp.url_base(), lib_whatsapp.instancia(), lib_whatsapp.dry_run())
    with _CACHE_WA_LOCK:
        c = _CACHE_WA.get(chave)
        if c and time.monotonic() - c[0] < CACHE_WHATSAPP_S:
            return c[1]
    try:
        st = whatsapp.status(timeout=TIMEOUT_CHECAGEM_S)
    except Exception as e:
        st = {"erro": str(e)[:200], "simulado": lib_whatsapp.simulado(), "instancia": lib_whatsapp.instancia()}
    with _CACHE_WA_LOCK:
        _CACHE_WA[chave] = (time.monotonic(), st)
    return st


def _conexao_whatsapp(st: dict) -> dict:
    """→ {tom, rotulo, estado(item_sistema), detalhe}."""
    if st.get("erro"):
        return {"tom": "neutro", "rotulo": "Indisponível", "estado": "indisponivel", "detalhe": st["erro"]}
    if not st.get("configurado"):
        return {"tom": "simulado", "rotulo": "Modo simulado", "estado": "simulado",
                "detalhe": "EVOLUTION_URL não configurada: nada sai da máquina."}
    est = str(st.get("estado_instancia") or "")
    if est == "open":
        return {"tom": "sucesso", "rotulo": "Conectado", "estado": "ok", "detalhe": f"Instância {st.get('instancia')}"}
    if est == "connecting":
        return {"tom": "aguardando", "rotulo": "Conectando", "estado": "aviso", "detalhe": f"Instância {st.get('instancia')}"}
    if est in ("close", "closed", "desconhecido"):
        return {"tom": "erro", "rotulo": "Desconectado", "estado": "erro",
                "detalhe": f"Instância {st.get('instancia')}: {est}"}
    if est == "inexistente":
        return {"tom": "erro", "rotulo": "Sem instância", "estado": "erro",
                "detalhe": f"A instância {st.get('instancia')} não existe na Evolution — "
                           f"rode ./run.sh whatsapp qrcode para criar e parear"}
    if est == "outro serviço":
        return {"tom": "erro", "rotulo": "Porta ocupada", "estado": "erro",
                "detalhe": f"Quem responde em {lib_whatsapp.url_base()} não é a Evolution API — "
                           f"outro programa ocupa a porta"}
    if est in ("HTTP 401", "HTTP 403"):
        return {"tom": "erro", "rotulo": "Chave recusada", "estado": "erro",
                "detalhe": f"Evolution API respondeu {est}: confira EVOLUTION_API_KEY no .env"}
    if est.startswith("HTTP "):
        return {"tom": "erro", "rotulo": "Erro na Evolution", "estado": "erro",
                "detalhe": f"Evolution API respondeu {est[:120]}"}
    return {"tom": "neutro", "rotulo": "Indisponível", "estado": "indisponivel",
            "detalhe": f"Evolution API sem resposta ({est[:120] or 'erro'})"}


def _agenda_vm(i: dict, agora: datetime) -> dict:
    origem = i.get("origem") or {}
    canal = origem.get("canal")
    if canal == "whatsapp":
        de = "WhatsApp" + (f" · {origem.get('contato')}" if origem.get("contato") else "")
    elif canal == "whatsapp-eu":
        de = "Nota para si mesmo"
    elif canal == "email":
        de = "E-mail" + (f" · {origem.get('assunto')}" if origem.get("assunto") else "")
    else:
        de = {"terminal": "Terminal", "api": "API", "manual": "Manual"}.get(canal, canal or "Manual")
    inicio = i.get("inicio")
    quando = fmt_data(inicio) + (" (dia inteiro)" if i.get("dia_inteiro") else "")
    st = i.get("status")
    return {"id": i.get("id"), "titulo": i.get("titulo"), "quando": quando, "iso": _iso(inicio) or str(inicio or ""),
            "relativo": fmt_relativo(inicio, agora), "origem": de, "status": st, "local": i.get("local"),
            "pode_desfazer": st in AGENDA_DESFAZIVEIS, "inativo": st in ("desfeito", "cancelado"),
            "erro": i.get("erro") if st == "erro" else None}


AGENDA_DESFAZIVEIS = ("pendente", "no_calendario", "em_execucao", "erro")


def _agenda() -> tuple[list[dict], list[dict]]:
    agora = _agora()
    itens = [_agenda_vm(i, agora) for i in agenda.listar()]
    ativos = sorted([i for i in itens if not i["inativo"]], key=lambda i: _ts(i["iso"]) or 0)
    inativos = sorted([i for i in itens if i["inativo"]], key=lambda i: -(_ts(i["iso"]) or 0))
    return ativos, inativos


def _motivo_lote(lote: dict) -> str:
    por: dict = {}
    for i in lote.get("itens") or []:
        chave = re.sub(r" há \d+ dias", "", str(i.get("motivo") or ""))
        por[chave] = por.get(chave, 0) + 1
    return ", ".join(f"{n} {k}" for k, n in sorted(por.items(), key=lambda x: -x[1]))


def _limpeza() -> tuple[list[dict], Optional[str]]:
    try:
        lotes = limpeza.listar_lotes()
    except Exception as e:
        return [], f"Não foi possível ler os lotes de limpeza: {e}"
    out = []
    for l in lotes:
        out.append({
            "id": l.get("id"), "status": l.get("status"), "inbox": l.get("inbox"), "total": l.get("total") or 0,
            "criado": fmt_data(l.get("criado_em")), "expira": fmt_data(l.get("expira_em")),
            "motivo": _motivo_lote(l), "nota": l.get("nota"), "_ts": _ts(l.get("criado_em")),
            "itens": [{"de": i.get("from_name") or i.get("from"), "email": i.get("from"),
                       "assunto": i.get("subject") or "(sem assunto)", "data": fmt_data(i.get("date"))}
                      for i in l.get("itens") or []],
        })
    # aguardando primeiro, depois os mais novos
    out.sort(key=lambda l: (l["status"] != "aguardando", -l["_ts"]))
    return out, None


def _dados_inicio() -> dict:
    agora = _agora()
    aguard = _lista_pendencias("aguardando", "", "")
    recentes = sorted(aguard, key=lambda x: -_ts(x["iso"]))[:5]
    ativos, _ = _agenda()
    proximos = [i for i in ativos if i["status"] != "desfazer"   # Pedro mandou tirar do calendário
                and (_ts(i["iso"]) or 0) >= (agora - timedelta(hours=1)).timestamp()][:4]
    lotes, _erro_lim = _limpeza()
    lotes_aguard = [l for l in lotes if l["status"] == "aguardando"]
    return {
        "recentes": recentes, "alfred": _estado_alfred(), "wa": _conexao_whatsapp(_status_whatsapp()),
        "simulado": lib_whatsapp.simulado(), "proximos": proximos,
        "lotes_aguardando": len(lotes_aguard), "emails_limpeza": sum(l["total"] for l in lotes_aguard),
        "cont": _contadores(),
    }


# ── Sistema (cada checagem independente, em paralelo, com timeout curto) ──

def _http_saude(url: str) -> dict:
    try:
        r = httpx.get(url, timeout=TIMEOUT_CHECAGEM_S)
    except Exception as e:
        return {"estado": "indisponivel", "detalhe": f"Sem resposta ({type(e).__name__})"}
    if r.status_code < 300:
        return {"estado": "ok", "detalhe": "No ar"}
    return {"estado": "erro", "detalhe": f"HTTP {r.status_code}"}


def _ollama() -> dict:
    base = (os.getenv("OLLAMA_BASE_URL", "") or "http://localhost:11434").rstrip("/")
    try:
        r = httpx.get(f"{base}/api/version", timeout=TIMEOUT_CHECAGEM_S)
        r.raise_for_status()
        return {"estado": "ok", "detalhe": f"Versão {r.json().get('version', '?')} · {base}"}
    except Exception as e:
        return {"estado": "indisponivel", "detalhe": f"Sem resposta de {base} ({type(e).__name__})"}


CONTAINERS = ("alfred-evolution", "alfred-evolution-db", "alfred-webui", "alfred-searxng", "alfred-chromadb", "alfred-n8n")


def _containers() -> dict:
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.Name}}|{{.State.Status}}", *CONTAINERS],
                           capture_output=True, text=True, timeout=3)
    except Exception as e:
        return {"erro": f"docker não respondeu ({type(e).__name__})"}
    estados = {}
    for ln in r.stdout.splitlines():
        nome, _, st = ln.strip().lstrip("/").partition("|")
        estados[nome] = st
    if not estados and r.returncode != 0 and "No such object" not in r.stderr:
        return {"erro": (r.stderr.strip().splitlines() or ["docker indisponível"])[0][:160]}
    return {"itens": [{"nome": c, "estado": estados.get(c)} for c in CONTAINERS]}


def _timers() -> dict:
    try:
        r = subprocess.run(["systemctl", "--user", "list-timers", "--all", "--output=json", "--no-pager"],
                           capture_output=True, text=True, timeout=3)
        dados = json.loads(r.stdout or "[]")
    except Exception as e:
        return {"erro": f"systemctl --user não respondeu ({type(e).__name__})"}
    out = []
    for t in dados if isinstance(dados, list) else []:
        unit = str(t.get("unit") or "")
        if not unit.startswith("alfred-"):
            continue
        prox = t.get("next")
        quando = (datetime.fromtimestamp(prox / 1e6).astimezone() if isinstance(prox, (int, float)) and prox > 0 else None)
        out.append({"nome": unit, "proximo": fmt_data(quando) if quando else "",
                    "relativo": fmt_relativo(quando) if quando else ""})
    out.sort(key=lambda t: t["nome"])
    return {"itens": out}


def _dados_sistema() -> dict:
    porta_exec = os.getenv("PORT_SHELL_EXECUTOR", "") or "7071"
    porta_res = os.getenv("PORT_RESEARCHER", "") or "7072"
    tarefas = {
        "whatsapp": _status_whatsapp,
        "executor": lambda: _http_saude(f"http://127.0.0.1:{porta_exec}/health"),
        "researcher": lambda: _http_saude(f"http://127.0.0.1:{porta_res}/health"),
        "ollama": _ollama,
        "containers": _containers,
        "timers": _timers,
    }
    res = {}
    ex = ThreadPoolExecutor(max_workers=len(tarefas), thread_name_prefix="painel-sistema")
    futuros = {k: ex.submit(f) for k, f in tarefas.items()}
    limite = time.monotonic() + TIMEOUT_CHECAGEM_S + 2
    for k, fut in futuros.items():
        try:
            res[k] = fut.result(timeout=max(0.1, limite - time.monotonic()))
        except FuturoTimeout:
            res[k] = {"erro": "sem resposta a tempo"}
        except Exception as e:
            res[k] = {"erro": str(e)[:160]}
    ex.shutdown(wait=False)   # uma checagem lenta não segura a tela

    st = res["whatsapp"]
    for k in ("executor", "researcher", "ollama"):
        if "erro" in res[k]:
            res[k] = {"estado": "indisponivel", "detalhe": res[k]["erro"]}
    pend = core.listar_pendencias("aguardando")
    return {
        "alfred": _estado_alfred(), "wa": _conexao_whatsapp(st), "wa_status": st,
        "fila": len(st.get("fila_espera") or []) if not st.get("erro") else None,
        "porta_inbox": os.getenv("PORT_INBOX", "") or "7073", "porta_exec": porta_exec, "porta_res": porta_res,
        "executor": res["executor"], "researcher": res["researcher"], "ollama": res["ollama"],
        "containers": res["containers"], "timers": res["timers"], "pendencias": len(pend),
        "agora": fmt_data(_agora()),
    }


# ── Detalhe da pendência ──

def _estado_audio(p: dict) -> str:
    mp3 = (p.get("arquivos") or {}).get("mp3")
    try:
        if mp3 and (core.vault() / mp3).is_file():
            return "disponivel"
    except Exception:
        pass
    return "expirado" if (mp3 or p.get("audio_removido_em")) else "sem"


def _evento_hist(texto: str, quando, tom: str = "", detalhe: str = "") -> dict:
    return {"texto": texto, "quando": fmt_data(quando), "iso": _iso(quando), "tom": tom, "detalhe": detalhe, "_ts": _ts(quando)}


def _historico(p: dict) -> list[dict]:
    wa = p.get("whatsapp") or {}
    em = p.get("email") or {}
    ev = []
    if p.get("tipo") == "whatsapp":
        if wa.get("recebido_em"):
            ev.append(_evento_hist("Mensagem recebida", wa["recebido_em"]))
        msgs = wa.get("mensagens") or []
        if len(msgs) > 1:
            ev.append(_evento_hist(f"{len(msgs)} mensagens do contato agrupadas", wa.get("atualizado_em") or msgs[-1].get("em")))
        if wa.get("respondida_na_espera"):
            ev.append(_evento_hist("Entrou na espera antes do aviso", p.get("criado_em"), "aguardando"))
        else:
            ev.append(_evento_hist("Pendência criada", p.get("criado_em"), "aguardando"))
        if wa.get("pergunta_msg_id") or p.get("lembrado_em"):
            ev.append(_evento_hist("Resumo enviado a você pelo WhatsApp", p.get("lembrado_em")))
        ag = wa.get("agenda") or {}
        if ag:
            ev.append(_evento_hist(f"Anotado na agenda: {ag.get('titulo')}", ag.get("criado_em"), "",
                                   f"Lembrete em {fmt_data(ag.get('quando'))} · tarefa #{ag.get('task_id')}"))
    else:
        if em.get("date"):
            ev.append(_evento_hist("E-mail recebido", em["date"], "", em.get("subject") or ""))
        ev.append(_evento_hist("Pendência criada", p.get("criado_em"), "aguardando"))
        if p.get("lembrado_em"):
            ev.append(_evento_hist("Lembrete enviado", p["lembrado_em"]))
    if p.get("status") != "aguardando":
        rot = ROTULO_STATUS.get(p.get("status"), p.get("status") or "")
        ev.append(_evento_hist(f"Resolvida ({rot}) por {core.caminho_resolucao(p)}",
                               p.get("respondido_em"), "sucesso", p.get("nota") or ""))
    envio = wa.get("envio") or {}
    if envio and not envio.get("manual"):
        if envio.get("ok") is True:
            ev.append(_evento_hist("Envio simulado: nada saiu da máquina" if envio.get("simulado")
                                   else f"Enviado para {_contato(p)}", envio.get("solicitado_em"), "sucesso"))
        elif envio.get("ok") is False:
            ev.append(_evento_hist("O envio falhou", envio.get("solicitado_em"), "erro", str(envio.get("erro") or "")))
    if p.get("task_id"):
        ev.append(_evento_hist(f"Tarefa #{p['task_id']} criada em tasks.yml", p.get("respondido_em"), "sucesso"))
    if p.get("audio_removido_em"):
        ev.append(_evento_hist("Áudio de resumo apagado (regra de retenção)", p["audio_removido_em"]))
    # ordem cronológica estável (sem data → mantém a posição)
    return sorted(ev, key=lambda e: e["_ts"] or 0) if all(e["_ts"] for e in ev) else ev


def _dados_detalhe(p: dict) -> dict:
    wa = p.get("whatsapp") or {}
    eh_wa = p.get("tipo") == "whatsapp"
    aguardando = p.get("status") == "aguardando"
    contato = _contato(p)
    chegada = _chegada(p)
    d = {
        "p": p, "eh_wa": eh_wa, "aguardando": aguardando, "contato": contato,
        "numero": lib_whatsapp.formatar_numero(wa.get("numero")) if eh_wa and not wa.get("grupo") else "",
        "relativo": fmt_relativo(chegada), "chegada": fmt_data(chegada),
        "audio": _estado_audio(p), "historico": _historico(p),
        "caminho": None if aguardando else core.caminho_resolucao(p),
        "simulado_envio": lib_whatsapp.simulado(),
        "rotulo_status": ROTULO_STATUS.get(p.get("status"), p.get("status")),
        "resolvida_em": fmt_data(p.get("respondido_em")),
    }
    if eh_wa:
        transcrito = wa.get("tipo_msg") == "audio" or bool(wa.get("transcricao"))
        d.update({
            "rotulo_texto": "Transcrição do áudio" if transcrito else
                            ("Mensagens" if len(wa.get("mensagens") or []) > 1 else "Mensagem"),
            "texto": wa.get("texto") or "",
            "resumo": (p.get("evento") or {}).get("resumo") or "",
            "categoria": wa.get("categoria"),
            "confianca": f"{round(float(wa['confianca']) * 100)}%" if isinstance(wa.get("confianca"), (int, float)) else None,
            "urgencia": wa.get("urgencia"),
            "motivo": wa.get("motivo"),
            # envio anterior falhou → o texto que Pedro já tinha escrito volta para a caixa
            "texto_resposta": (p.get("resposta_texto") if aguardando and p.get("resposta_texto") else wa.get("rascunho")) or "",
            "sugestao_quando": whatsapp.sugestao_quando(p) if aguardando else "",
            "envio": wa.get("envio") or {},
            "na_espera": bool(wa.get("respondida_na_espera")),
        })
    else:
        ev = p.get("evento") or {}
        em = p.get("email") or {}
        link = ev.get("link") if isinstance(ev.get("link"), str) and re.match(r"^https?://", ev.get("link") or "") else None
        d.update({
            "explicacao": _texto_explicativo(p), "link": link, "evento": ev, "email": em,
            "dados": [("Tipo", "Convite" if p.get("tipo") == "evento" else "Contato profissional"),
                      ("Quando", fmt_data(ev.get("data_inicio")) or ev.get("data_inicio")),
                      ("Local", ev.get("local")), ("Organizador", ev.get("organizador")),
                      ("Custo", ev.get("custo") if ev.get("custo") not in ("desconhecido",) else None),
                      ("Prazo para responder", fmt_data(ev.get("prazo_resposta")) or ev.get("prazo_resposta")),
                      ("De", f"{em.get('from_name') or ''} <{em.get('from')}>" if em.get("from") else None),
                      ("Assunto", em.get("subject"))],
        })
    return d


# ══════════════════════════════════════════════════════════════
#  Rotas — login, estáticos
# ══════════════════════════════════════════════════════════════

@router.get("/static/{nome}")
def estatico(nome: str):
    tipo = ESTATICOS.get(nome)
    if not tipo:
        return _erro(404, "Arquivo não encontrado.")
    return Response((DIR_WEB / "static" / nome).read_bytes(), media_type=tipo,
                    headers={"Cache-Control": "no-cache"})   # local e pequeno: sempre a versão atual


@router.get("/login")
def login_pagina(request: Request, proximo: Optional[str] = None):
    if _nonce_da_sessao(request):
        return RedirectResponse(_proximo_seguro(proximo) or "/painel", status_code=303)
    erro = "" if _token() else "SHELL_EXECUTOR_TOKEN não definido no .env"
    return HTMLResponse(_render("login.html", proximo=_proximo_seguro(proximo), erro=erro))


@router.post("/login")
async def login(request: Request):
    if not _origem_ok(request):
        return HTMLResponse(_render("login.html", erro=MSG_CSRF), status_code=403)
    try:
        campos = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        campos = {}
    digitado = (campos.get("token") or [""])[0]
    proximo = _proximo_seguro((campos.get("proximo") or [""])[0])
    token = _token()
    if not token:
        return HTMLResponse(_render("login.html", erro="SHELL_EXECUTOR_TOKEN não definido no .env", proximo=proximo),
                            status_code=401)
    if not hmac.compare_digest(digitado.encode("utf-8"), token.encode("utf-8")):
        await asyncio.sleep(ATRASO_LOGIN_FALHO)
        # nunca ecoa o que foi digitado
        return HTMLResponse(_render("login.html", erro="Token inválido", proximo=proximo), status_code=401)
    valor, max_age = _nova_sessao()
    resp = RedirectResponse(proximo or "/painel", status_code=303)
    resp.set_cookie(COOKIE, valor, max_age=max_age, path="/", httponly=True, samesite="Strict")
    return resp


@router.post("/sair")
async def sair(request: Request):
    nonce = _nonce_da_sessao(request)
    if nonce:
        recusa = _checar_post(request)
        if recusa:
            return recusa
        with _REVOGADOS_LOCK:
            agora = time.time()
            for n, exp in list(_REVOGADOS.items()):   # limpa os já vencidos
                if exp <= agora:
                    del _REVOGADOS[n]
            _REVOGADOS[nonce] = agora + dias_sessao() * 86400
    resp = JSONResponse({"ok": True, "mensagem": "Sessão encerrada."})
    resp.delete_cookie(COOKIE, path="/", httponly=True, samesite="Strict")
    return resp


# ══════════════════════════════════════════════════════════════
#  Rotas — telas
# ══════════════════════════════════════════════════════════════

@router.get("")
def inicio(request: Request):
    if not _nonce_da_sessao(request):
        return _para_login(request)
    hoje = _agora()
    dias = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
    return _pagina(request, "inicio.html", "inicio", hoje=f"{dias[hoje.weekday()].capitalize()}, {hoje:%d/%m/%Y %H:%M}",
                   **_dados_inicio())


@router.get("/pendencias")
def pendencias(request: Request, aba: Optional[str] = None, tipo: Optional[str] = None, q: Optional[str] = None):
    if not _nonce_da_sessao(request):
        return _para_login(request)
    aba, tipo, q = _filtros(aba, tipo, q)
    itens = _lista_pendencias(aba, tipo, q)
    return _pagina(request, "pendencias.html", "pendencias", aba=aba, tipo=tipo, q=q, itens=itens,
                   vazio=_vazio_lista(aba, tipo, q),
                   poll_url="/painel/parcial/pendencias?" + urlencode({"aba": aba, "tipo": tipo, "q": q}),
                   aguardando=len(core.listar_pendencias("aguardando")))


@router.get("/pendencias/{pid}")
def pendencia(request: Request, pid: str):
    if not _nonce_da_sessao(request):
        return _para_login(request)
    p = core.obter_pendencia(pid)
    if not p:
        return _pagina(request, "nao_encontrada.html", "pendencias", status=404)
    return _pagina(request, "pendencia.html", "pendencias", **_dados_detalhe(p))


@router.get("/agenda")
def tela_agenda(request: Request):
    if not _nonce_da_sessao(request):
        return _para_login(request)
    ativos, inativos = _agenda()
    return _pagina(request, "agenda.html", "agenda", ativos=ativos, inativos=inativos)


@router.get("/limpeza")
def tela_limpeza(request: Request):
    if not _nonce_da_sessao(request):
        return _para_login(request)
    lotes, erro = _limpeza()
    return _pagina(request, "limpeza.html", "limpeza", lotes=lotes, erro_limpeza=erro)


@router.get("/sistema")
def tela_sistema(request: Request):
    if not _nonce_da_sessao(request):
        return _para_login(request)
    return _pagina(request, "sistema.html", "sistema", **_dados_sistema())


@router.get("/whatsapp")
def tela_whatsapp(request: Request):
    """Pareamento: gera o QR na hora (expira em ~40 s na Evolution; a página recarrega sozinha)."""
    if not _nonce_da_sessao(request):
        return _para_login(request)
    try:
        qr = whatsapp.obter_qrcode()
    except Exception as e:
        qr = {"ok": False, "erro": str(e)[:300]}
    _CACHE_WA.clear()   # o estado mostrado ao lado do QR é o de agora
    b64 = qr.get("base64") or ""
    if b64 and not b64.startswith("data:"):
        b64 = "data:image/png;base64," + b64
    return _pagina(request, "whatsapp.html", "sistema", qr=qr, qr_img=b64,
                   wa=_conexao_whatsapp(_status_whatsapp()), instancia=lib_whatsapp.instancia())


# ── Fragmentos do polling (mesmos parciais das páginas) ──

@router.get("/parcial/{nome}")
def parcial(request: Request, nome: str, aba: Optional[str] = None, tipo: Optional[str] = None, q: Optional[str] = None):
    if not _nonce_da_sessao(request):
        return _erro(401, MSG_SESSAO)
    if nome == "pendencias":
        aba, tipo, q = _filtros(aba, tipo, q)
        html = _render("_pendencias_lista.html", itens=_lista_pendencias(aba, tipo, q), aba=aba,
                       vazio=_vazio_lista(aba, tipo, q))
    elif nome == "inicio":
        html = _render("_inicio.html", **_dados_inicio())
    elif nome == "agenda":
        ativos, inativos = _agenda()
        html = _render("_agenda_lista.html", ativos=ativos, inativos=inativos)
    elif nome == "limpeza":
        lotes, erro = _limpeza()
        html = _render("_limpeza_lista.html", lotes=lotes, erro_limpeza=erro)
    elif nome == "sistema":
        html = _render("_sistema.html", **_dados_sistema())
    else:
        return _erro(404, "Parcial desconhecido.")
    return HTMLResponse(html)


@router.get("/api/contadores")
def api_contadores(request: Request):
    nonce = _nonce_da_sessao(request)
    if not nonce:
        return _erro(401, MSG_SESSAO)
    # X-CSRF-Token da sessão atual: se Pedro entrou de novo em outra aba (sessão expirada no meio de uma
    # edição), a página aberta passa a usar o token novo e "Tentar de novo" funciona sem recarregar.
    return JSONResponse(_contadores(), headers={"X-CSRF-Token": _csrf(nonce)})


def _ja_resolvida(p: dict) -> JSONResponse:
    return _erro(409, f"Esta pendência já foi resolvida por {core.caminho_resolucao(p)}.", status=p.get("status"))


@router.get("/api/pendencias/{pid}/estado")
def api_estado(request: Request, pid: str):
    if not _nonce_da_sessao(request):
        return _erro(401, MSG_SESSAO)
    p = core.obter_pendencia(pid)
    if not p:
        return _erro(404, "Pendência não encontrada.")
    resolvida = p.get("status") != "aguardando"
    caminho = core.caminho_resolucao(p) if resolvida else None
    return JSONResponse({"status": p.get("status"), "resolvida": resolvida, "caminho": caminho,
                         "mensagem": f"Esta pendência já foi resolvida por {caminho}." if resolvida else None})


# ══════════════════════════════════════════════════════════════
#  Rotas — ações (sempre pelas funções existentes)
# ══════════════════════════════════════════════════════════════

def _pendencia_para_acao(pid: str, whatsapp_: Optional[bool]) -> tuple[Optional[dict], Optional[JSONResponse]]:
    """Checagem prévia: existe, é do tipo certo e ainda está aguardando. As funções chamadas depois
    checam de novo dentro da trava entre processos (outro caminho pode chegar no meio)."""
    p = core.obter_pendencia(pid)
    if not p:
        return None, _erro(404, "Pendência não encontrada.")
    if whatsapp_ is True and p.get("tipo") != "whatsapp":
        return None, _erro(400, "Esta ação vale só para pendências de WhatsApp.")
    if whatsapp_ is False and p.get("tipo") == "whatsapp":
        return None, _erro(400, "Pendência de WhatsApp: responda com texto.")
    if p.get("status") != "aguardando":
        return None, _ja_resolvida(p)
    return p, None


@router.post("/api/pendencias/{pid}/responder-texto")
async def acao_responder_texto(request: Request, pid: str):
    if recusa := _checar_post(request):
        return recusa
    texto = str((await _corpo_json(request)).get("texto") or "").strip()
    if not texto:
        return _erro(400, "Escreva o texto da resposta antes de autorizar o envio.", campo="texto")
    p, erro = _pendencia_para_acao(pid, True)
    if erro:
        return erro
    try:
        # síncrona e serializada (whatsapp._LOCK + trava entre processos): roda fora do laço do servidor
        entry = await run_in_threadpool(whatsapp.responder_texto, pid, texto, True, "via painel")
    except core.PendenciaJaResolvida as e:
        return _ja_resolvida(e.entry)
    except KeyError:
        return _erro(404, "Pendência não encontrada.")
    except ValueError as e:
        return _erro(400, str(e))
    envio = (entry.get("whatsapp") or {}).get("envio") or {}
    if entry.get("status") == "ignorada":
        return JSONResponse({"ok": True, "mensagem": "Pendência fechada sem resposta. Nada foi enviado.",
                             "status": "ignorada"})
    if entry.get("status") == "aguardando" or not envio.get("ok"):
        motivo = envio.get("erro") or "a Evolution API não confirmou o envio"
        return _erro(502, f"O envio falhou: {motivo}. O texto foi guardado; tente de novo.",
                     pode_tentar_de_novo=True, status=entry.get("status"))
    msg = "Simulado: nada saiu da máquina." if envio.get("simulado") else f"Enviado para {_contato(entry)}."
    return JSONResponse({"ok": True, "mensagem": msg, "simulado": bool(envio.get("simulado")),
                         "status": entry.get("status")})


@router.post("/api/pendencias/{pid}/ignorar")
async def acao_ignorar(request: Request, pid: str):
    if recusa := _checar_post(request):
        return recusa
    p, erro = _pendencia_para_acao(pid, True)
    if erro:
        return erro
    try:
        await run_in_threadpool(whatsapp.responder_texto, pid, "ignorar", True, "via painel")
    except core.PendenciaJaResolvida as e:
        return _ja_resolvida(e.entry)
    except KeyError:
        return _erro(404, "Pendência não encontrada.")
    except ValueError as e:
        return _erro(400, str(e))
    return JSONResponse({"ok": True, "mensagem": "Pendência fechada sem resposta. Nada foi enviado.",
                         "status": "ignorada"})


@router.post("/api/pendencias/{pid}/agendar")
async def acao_agendar(request: Request, pid: str):
    if recusa := _checar_post(request):
        return recusa
    quando_txt = str((await _corpo_json(request)).get("quando") or "").strip()
    p, erro = _pendencia_para_acao(pid, True)
    if erro:
        return erro
    try:
        lido = whatsapp.interpretar_quando(quando_txt)
    except ValueError:   # data inexistente nunca vira 500
        lido = None
    if not lido:
        return _erro(400, "Não entendi a data. Use, por exemplo: 20/10 14:00, amanhã 10h, terça 15h ou 16:00.",
                     campo="quando")
    quando, tem_hora = lido
    agora = core._now()
    if (quando < agora - timedelta(minutes=1)) if tem_hora else (quando.date() < agora.date()):
        # "hoje 10h" às 18:40: tarefa e lembrete nasceriam no passado
        return _erro(400, f"Esse horário já passou ({quando.strftime('%d/%m/%Y %H:%M' if tem_hora else '%d/%m/%Y')}). "
                          "Escolha um horário futuro.", campo="quando")
    try:
        r = await run_in_threadpool(whatsapp.agendar_pendencia, pid, quando, tem_hora)
    except core.PendenciaJaResolvida as e:
        return _ja_resolvida(e.entry)
    except KeyError:
        return _erro(404, "Pendência não encontrada.")
    except ValueError as e:
        return _erro(400, str(e))
    q = quando.strftime("%d/%m/%Y %H:%M" if tem_hora else "%d/%m/%Y")
    return JSONResponse({"ok": True, "google_url": r.get("google_url"), "task_id": r.get("task_id"),
                         "mensagem": f"Anotado: {r.get('titulo')} — {q} (tarefa #{r.get('task_id')}). "
                                     f"A pendência continua aguardando e o lembrete volta nesse horário."})


@router.post("/api/pendencias/{pid}/responder")
async def acao_responder(request: Request, pid: str):
    if recusa := _checar_post(request):
        return recusa
    corpo = await _corpo_json(request)
    resposta = str(corpo.get("resposta") or "").strip().lower()
    if resposta not in ("vou", "nao_vou"):
        return _erro(400, "Resposta inválida: use Vou ou Não vou.")
    p, erro = _pendencia_para_acao(pid, False)
    if erro:
        return erro
    try:
        entry = await run_in_threadpool(lambda: core.responder(pid, resposta, str(corpo.get("nota") or ""),
                                                               origem="via painel", so_se_aguardando=True))
    except core.PendenciaJaResolvida as e:
        return _ja_resolvida(e.entry)
    except KeyError:
        return _erro(404, "Pendência não encontrada.")
    except ValueError as e:
        return _erro(400, str(e))
    if resposta == "vou":
        msg = "Registrado: vou." + (f" Tarefa #{entry['task_id']} criada em tasks.yml." if entry.get("task_id") else "")
    else:
        msg = "Registrado: não vou."
    return JSONResponse({"ok": True, "mensagem": msg, "status": entry.get("status"), "task_id": entry.get("task_id")})


@router.post("/api/agenda/{aid}/desfazer")
async def acao_desfazer(request: Request, aid: str):
    if recusa := _checar_post(request):
        return recusa

    def _fazer():
        # checar e desfazer na mesma trava de agenda.yml (reentrante: agenda.desfazer pega a mesma)
        with core.trava_arquivo(agenda.agenda_file()):
            item = agenda.obter(aid)
            if not item:
                return _erro(404, "Item da agenda não encontrado.")
            st = item.get("status")
            if st not in AGENDA_DESFAZIVEIS:
                return _erro(409, f"Este item já foi resolvido ({ROTULO_STATUS.get(st, st)}).", status=st)
            return agenda.desfazer(aid)

    r = await run_in_threadpool(_fazer)
    if isinstance(r, Response):
        return r
    if r.get("status") == "desfazer":
        msg = "Marcado para sair do calendário. A extensão do Thunderbird remove na próxima passada."
    else:
        msg = "Item cancelado. Ele não irá para o calendário."
    return JSONResponse({"ok": True, "mensagem": msg, "status": r.get("status")})


async def _acao_lote(request: Request, lid: str, aprovar: bool):
    if recusa := _checar_post(request):
        return recusa
    nota = str((await _corpo_json(request)).get("nota") or "")

    def _fazer():
        with core.trava_arquivo(limpeza.lotes_file()):   # mesma trava de limpeza.aprovar/recusar (reentrante)
            lote = limpeza.obter_lote(lid)
            if not lote:
                return _erro(404, "Lote de limpeza não encontrado.")
            st = lote.get("status")
            if st != "aguardando":
                return _erro(409, f"Este lote já foi resolvido ({ROTULO_STATUS.get(st, st)}).", status=st)
            try:
                return (limpeza.aprovar if aprovar else limpeza.recusar)(lid, nota)
            except ValueError as e:
                return _erro(400, str(e))
            except KeyError as e:   # ex.: a inbox do lote não existe mais no perfis.yml
                return _erro(400, str(e).strip("'\"") + (" — recuse o lote." if aprovar else ""))

    try:
        r = await run_in_threadpool(_fazer)
    except RuntimeError as e:   # limpeza.yml ilegível
        return _erro(500, str(e))
    if isinstance(r, Response):
        return r
    if aprovar:
        n = r.get("total")
        msg = (f"Lote aprovado: {n} e-mail{'' if n == 1 else 's'} {'irá' if n == 1 else 'irão'} para a Lixeira "
               f"quando a extensão do Thunderbird passar.")
        fora = len(r.get("removidos_na_aprovacao") or [])
        if fora:
            msg += f" {fora} {'saiu' if fora == 1 else 'saíram'} do lote na revalidação."
    else:
        msg = "Lote recusado. Nenhum e-mail será movido."
    return JSONResponse({"ok": True, "mensagem": msg, "status": r.get("status"), "total": r.get("total")})


@router.post("/api/limpeza/{lid}/aprovar")
async def acao_aprovar(request: Request, lid: str):
    return await _acao_lote(request, lid, True)


@router.post("/api/limpeza/{lid}/recusar")
async def acao_recusar(request: Request, lid: str):
    return await _acao_lote(request, lid, False)
