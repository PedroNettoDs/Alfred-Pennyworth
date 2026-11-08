"""
Alfred — aba Conversa do Painel (http://127.0.0.1:7073/painel/conversa), Fase 3 de docs/planos/chat-alfred.md.

Pedro conversa com o Alfred como fazia no Open WebUI: conversas à esquerda (VAULT_ALFRED/conversas.db), mensagens no
centro com a resposta chegando por SSE, chips de ferramenta (nome, argumentos, resultado) e o cartão [Executar] [Não]
das perigosas. Tudo passa pelo motor conversa.py — aqui só há rotas, o SSE e a montagem da tela. O seletor
Conversa | Code do topo tem o Code só como lugar reservado ("em breve").

Mesma segurança do Painel (só 127.0.0.1, sessão, CSRF): usa _RotaPainel e as checagens de painel.py.
"""

import asyncio
import json
import queue
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import conversa
import painel

LISTA = 50            # conversas na coluna da esquerda
BUSCA = 60            # trechos da FTS lidos para montar a lista filtrada
TEXTO_MAX = 20000     # caracteres por mensagem (~5 mil tokens: o resto do contexto precisa caber)
PING_S = 15           # comentário SSE enquanto o modelo pensa, para a conexão não parecer morta

# rótulo do chip de uma ferramenta já gravada (o estado vivo vem pelo SSE; no banco fica só o resultado)
ROTULOS = {"ok": "feito", "erro": "erro", "nao_executada": "não executada"}

painel.ESTATICOS.update({"conversa.css": "text/css; charset=utf-8", "conversa.js": "text/javascript; charset=utf-8"})

router = APIRouter(prefix="/painel", include_in_schema=False, route_class=painel._RotaPainel)


class _Erro(Exception):
    def __init__(self, codigo: int, mensagem: str):
        super().__init__(mensagem)
        self.codigo = codigo


# ══════════════════════════════════════════════════════════════
#  Dados da tela
# ══════════════════════════════════════════════════════════════

def _item(c: dict, trecho: str = "") -> dict:
    return {"id": c["id"], "titulo": c.get("titulo") or "Conversa", "url": f"/painel/conversa/{c['id']}",
            "quando": painel.fmt_data(c.get("atualizada_em")), "relativo": painel.fmt_relativo(c.get("atualizada_em")),
            "iso": painel._iso(c.get("atualizada_em")), "trecho": trecho}


def _lista(q: str) -> list[dict]:
    """Coluna da esquerda: as conversas mais recentes ou, com `q`, as que têm mensagens com essas palavras (FTS5,
    melhor trecho primeiro)."""
    if not q:
        return [_item(c) for c in conversa.listar_conversas(LISTA)]
    vistas: dict[str, str] = {}
    for r in conversa.buscar(q, BUSCA):
        vistas.setdefault(r["conversa_id"], r["trecho"])
    por_id = {c["id"]: c for c in conversa.listar_conversas(10 ** 6)}
    return [_item(por_id[cid], trecho) for cid, trecho in vistas.items() if cid in por_id][:LISTA]


def _estado(resultado: str) -> str:
    if resultado.startswith("[erro]"):
        return "erro"
    if resultado.startswith("[não executada]"):
        return "nao_executada"
    return "ok"


def _chip(m: dict) -> dict:
    try:
        argumentos = json.dumps(json.loads(m.get("argumentos") or "{}"), ensure_ascii=False, indent=2)
    except ValueError:
        argumentos = m.get("argumentos") or ""
    resultado = m.get("resultado") or ""
    estado = _estado(resultado)
    return {"nome": m.get("ferramenta") or "", "argumentos": argumentos, "resultado": resultado,
            "estado": estado, "rotulo": ROTULOS[estado]}


def _turnos(msgs: list[dict]) -> list[dict]:
    """Mensagens do banco → turnos da tela: pergunta de Pedro, chips das ferramentas e a resposta em HTML seguro."""
    turnos: list[dict] = []
    for m in msgs:
        if m["papel"] == "user" or not turnos:
            turnos.append({"pergunta": m["texto"] if m["papel"] == "user" else "", "ferramentas": [],
                           "resposta": None, "quando": painel.fmt_data(m["criada_em"])})
            if m["papel"] == "user":
                continue
        t = turnos[-1]
        if m["papel"] == "tool":
            t["ferramentas"].append(_chip(m))
        else:
            t["resposta"] = painel._markdown_seguro(m["texto"])
            t["modelo"] = m.get("modelo") or ""
    return turnos


def _tela(request: Request, conv: Optional[dict], q: Optional[str], status: int = 200):
    q = " ".join((q or "").split())[:200]
    turnos = _turnos(conversa.mensagens(conv["id"])) if conv else []
    ativo = conversa.turno_ativo(conv["id"]) if conv else None
    return painel._pagina(request, "conversa.html", "conversa", status=status, conv=conv, lista=_lista(q), q=q,
                          turnos=turnos, turno_ativo=ativo.id if ativo else "", nao_encontrada=status == 404)


# ══════════════════════════════════════════════════════════════
#  Rotas — tela
# ══════════════════════════════════════════════════════════════

@router.get("/conversa")
def tela(request: Request, q: Optional[str] = None):
    """Sem conversa aberta: a próxima mensagem cria uma. `?q=` filtra a lista pela FTS das mensagens."""
    if not painel._nonce_da_sessao(request):
        return painel._para_login(request)
    return _tela(request, None, q)


@router.get("/conversa/{cid}")
def tela_conversa(request: Request, cid: str, q: Optional[str] = None):
    if not painel._nonce_da_sessao(request):
        return painel._para_login(request)
    conv = conversa.obter_conversa(cid)
    if not conv:
        return _tela(request, None, q, status=404)
    return _tela(request, conv, q)


# ══════════════════════════════════════════════════════════════
#  Rotas — mensagens, SSE, parar, aprovação, apagar
# ══════════════════════════════════════════════════════════════

def _abrir_turno(cid: Optional[str], texto: str) -> dict:
    """Thread: cria a conversa quando ainda não há uma e põe o turno na fila do chat."""
    nova = cid is None
    if nova:
        cid = conversa.nova_conversa()["id"]
    try:
        turno = conversa.enviar(cid, texto)
    except KeyError:
        raise _Erro(404, "Conversa não encontrada. Comece uma nova.") from None
    except RuntimeError:
        raise _Erro(409, "Alfred ainda está respondendo a mensagem anterior.") from None
    conv = conversa.obter_conversa(cid) or {}
    return {"ok": True, "conversa": cid, "titulo": conv.get("titulo") or "Conversa", "turno": turno.id,
            "url": f"/painel/conversa/{cid}", "nova": nova}


async def _mensagem(request: Request, cid: Optional[str]):
    if recusa := painel._checar_post(request):
        return recusa
    texto = str((await painel._corpo_json(request)).get("texto") or "").strip()
    if not texto:
        return painel._erro(400, "Escreva a mensagem.", campo="texto")
    if len(texto) > TEXTO_MAX:
        return painel._erro(400, f"Mensagem longa demais: o limite é {TEXTO_MAX} caracteres.", campo="texto")
    try:
        return JSONResponse(await run_in_threadpool(_abrir_turno, cid, texto))
    except _Erro as e:
        return painel._erro(e.codigo, str(e))


@router.post("/api/conversas")
async def nova_conversa(request: Request):
    """{texto}: cria a conversa com essa primeira mensagem → {conversa, titulo, turno, url}."""
    return await _mensagem(request, None)


@router.post("/api/conversas/{cid}/mensagens")
async def mensagem(request: Request, cid: str):
    """{texto} numa conversa existente → {conversa, turno}. 409 com turno em andamento (uma pergunta por vez)."""
    return await _mensagem(request, cid)


async def _fluxo(request: Request, turno: conversa.Turno):
    """Eventos do turno em SSE. Só espia a fila, sem prender thread: o modelo pode levar minutos. Turno já acabado
    com a fila vazia (os eventos foram consumidos por outra página) → "fim" com `recarregar`: a tela lê do banco."""
    ultimo = time.monotonic()
    while True:
        try:
            ev = turno.fila.get_nowait()
        except queue.Empty:
            if turno.terminado is not None:
                yield f"data: {json.dumps({'tipo': 'fim', 'turno': turno.id, 'recarregar': True})}\n\n"
                return
            if await request.is_disconnected():
                return
            if time.monotonic() - ultimo > PING_S:
                ultimo = time.monotonic()
                yield ": ping\n\n"
            await asyncio.sleep(0.05)
            continue
        if ev["tipo"] == "fim":
            ev = {**ev, "html": str(painel._markdown_seguro(ev.get("texto") or ""))}
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        ultimo = time.monotonic()
        if ev["tipo"] in ("fim", "erro"):
            return


@router.get("/api/turnos/{turno_id}/eventos")
async def eventos(request: Request, turno_id: str):
    if not painel._nonce_da_sessao(request):
        return painel._erro(401, painel.MSG_SESSAO)
    turno = conversa.obter_turno(turno_id)
    if not turno:
        return painel._erro(404, "Turno não encontrado.")
    return StreamingResponse(_fluxo(request, turno), media_type="text/event-stream")


@router.post("/api/turnos/{turno_id}/parar")
async def parar(request: Request, turno_id: str):
    if recusa := painel._checar_post(request):
        return recusa
    return JSONResponse({"ok": conversa.parar(turno_id)})


@router.post("/api/aprovacoes/{aprovacao_id}/{decisao}")
async def decidir(request: Request, aprovacao_id: str, decisao: str):
    if recusa := painel._checar_post(request):
        return recusa
    if decisao not in ("executar", "negar"):
        return painel._erro(400, "Decisão inválida.")
    if not (conversa.aprovar if decisao == "executar" else conversa.negar)(aprovacao_id):
        return painel._erro(409, "Esse pedido já foi decidido ou expirou.")
    return JSONResponse({"ok": True})


@router.post("/api/conversas/{cid}/apagar")
async def apagar(request: Request, cid: str):
    if recusa := painel._checar_post(request):
        return recusa
    try:
        ok = await run_in_threadpool(conversa.apagar_conversa, cid)
    except RuntimeError:
        return painel._erro(409, "Alfred ainda está respondendo nesta conversa. Pare o turno antes de apagar.")
    if not ok:
        return painel._erro(404, "Conversa não encontrada.")
    return JSONResponse({"ok": True, "mensagem": "Conversa apagada."})
