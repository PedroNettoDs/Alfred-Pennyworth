"""
Alfred — o que o aplicativo do celular (app/, Flutter) precisa do Painel além das telas e rotas que já existem.

O aplicativo é um cliente do Painel: entra em POST /painel/login com o token semanal (app_token.py), recebe o
cookie de sessão e usa as rotas /painel/api/* de sempre (o CSRF vem no cabeçalho X-CSRF-Token de
GET /painel/api/contadores). Aqui ficam só a tela do token e o JSON que faltava.

GET  /painel/aplicativo             tela, só em 127.0.0.1: endereço, token da semana, validade e QR code
POST /painel/api/aplicativo/token   gera outro token (o anterior e as sessões do aplicativo deixam de valer)
GET  /painel/api/avisos             o feed de avisos (lib_db.avisos): ?desde=<id> traz só os mais novos
GET  /painel/api/avisos/fluxo       os avisos novos em SSE: é o que o aplicativo mantém aberto para notificar
GET  /painel/api/lembretes          doses de remédio abertas, recados, agenda e pendências aguardando
POST /painel/api/doses/{id}/tomei   o "Tomei" de uma dose aberta

A lista e o histórico das conversas em JSON ficam em painel_conversa.py.

Mesma segurança do Painel (rede, sessão, CSRF): usa _RotaPainel e as checagens de painel.py.
"""

import asyncio
import json
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import app_token
import painel
import remedios
import whatsapp
from _shared import lib_db

AVISOS_MAX = 200           # avisos por resposta
FLUXO_A_CADA_S = 2         # de quanto em quanto o fluxo olha o banco: os avisos nascem em vários processos
FLUXO_PING_S = 30          # comentário SSE com o fluxo parado, para a conexão não parecer morta

router = APIRouter(prefix="/painel", include_in_schema=False, route_class=painel._RotaPainel)


# ══════════════════════════════════════════════════════════════
#  Tela do token (só no computador)
# ══════════════════════════════════════════════════════════════

def _so_no_computador(request: Request) -> Optional[JSONResponse]:
    """O token é lido e trocado só em 127.0.0.1: o aplicativo (ou quem pegou a sessão dele) não gera o próximo."""
    return None if painel._classe(request) == "local" else painel._erro(403, painel.MSG_LOOPBACK)


def _qr(conteudo: str) -> str:
    """QR code em data-URI (SVG). "" sem o pacote segno (venv criado antes dele): a tela mostra só o texto."""
    try:
        import segno
    except ImportError:
        return ""
    return segno.make(conteudo, error="m").svg_data_uri(scale=6, border=2)


@router.get("/aplicativo")
def tela(request: Request):
    if recusa := _so_no_computador(request):
        return recusa
    if not painel._nonce_da_sessao(request):
        return painel._para_login(request)
    if not app_token.hosts():
        return painel._pagina(request, "aplicativo.html", "sistema", ligado=False)
    semanal = app_token.atual() or app_token.renovar()   # API recém-ligada: o laço ainda não gerou o primeiro
    url = app_token.url()
    return painel._pagina(request, "aplicativo.html", "sistema", ligado=True, url=url, token=semanal["token"],
                          validade=f"{datetime.fromtimestamp(semanal['expira_em']):%d/%m/%Y %H:%M}",
                          enviado=bool(semanal.get("enviado_em")),
                          qr_img=_qr(json.dumps({"u": url, "t": semanal["token"]})))


@router.post("/api/aplicativo/token")
async def novo_token(request: Request):
    if recusa := _so_no_computador(request) or painel._checar_post(request):
        return recusa
    if not app_token.hosts():
        return painel._erro(409, "O aplicativo está desligado: defina PAINEL_HOSTS_APP no .env e reinicie a API.")
    app_token.renovar()
    return JSONResponse({"ok": True, "mensagem": "Token novo gerado. O aplicativo foi desconectado; o token segue para "
                                                 "o seu WhatsApp em até um minuto."})


# ══════════════════════════════════════════════════════════════
#  Avisos
# ══════════════════════════════════════════════════════════════

@router.get("/api/avisos")
def avisos(request: Request, desde: Optional[int] = None, limite: int = 50):
    """→ {avisos: [{id, quando (epoch), tipo, titulo, texto, link, ref}]}, em ordem de chegada. Sem `desde`, os
    `limite` mais recentes; com ele, os de id maior."""
    if not painel._nonce_da_sessao(request):
        return painel._erro(401, painel.MSG_SESSAO)
    return JSONResponse({"avisos": lib_db.avisos(desde, max(1, min(limite, AVISOS_MAX)))})


async def _fluxo(request: Request, desde: Optional[int]):
    """Um `data: {aviso}` por aviso novo. Sem `desde`, começa do que chegar a partir de agora. Acaba quando o
    cliente cai ou a sessão deixa de valer: um fluxo aberto não sobrevive ao token da semana."""
    if desde is None:
        ultimo = await run_in_threadpool(lib_db.avisos, None, 1)
        desde = ultimo[-1]["id"] if ultimo else 0
    yield ": ok\n\n"
    falou = time.monotonic()
    while not await request.is_disconnected() and painel._nonce_da_sessao(request):
        novos = await run_in_threadpool(lib_db.avisos, desde, AVISOS_MAX)
        for a in novos:
            desde = a["id"]
            yield f"data: {json.dumps(a, ensure_ascii=False)}\n\n"
        if novos:
            falou = time.monotonic()
        elif time.monotonic() - falou > FLUXO_PING_S:
            falou = time.monotonic()
            yield ": ping\n\n"
        await asyncio.sleep(FLUXO_A_CADA_S)


@router.get("/api/avisos/fluxo")
async def fluxo(request: Request, desde: Optional[int] = None):
    if not painel._nonce_da_sessao(request):
        return painel._erro(401, painel.MSG_SESSAO)
    return StreamingResponse(_fluxo(request, desde), media_type="text/event-stream")


# ══════════════════════════════════════════════════════════════
#  Lembretes
# ══════════════════════════════════════════════════════════════

@router.get("/api/lembretes")
def lembretes(request: Request):
    """O que espera Pedro, como o Início do Painel e os post-its do Pestinha mostram: doses de remédio abertas
    (remedios.recados), recados deixados no número do Alfred (whatsapp.recados), os próximos itens da agenda e as
    outras pendências aguardando. Cada um se resolve pela rota que o Painel já usa: /api/doses/{id}/tomei,
    /api/pendencias/{id}/ignorar | responder-texto e /api/agenda/{id}/feito | desfazer."""
    if not painel._nonce_da_sessao(request):
        return painel._erro(401, painel.MSG_SESSAO)
    recados = whatsapp.recados()
    dos_recados = {r["id"] for r in recados}
    pendencias, _ = painel._lista_pendencias("aguardando", "", "")
    proximos, _, _ = painel._agenda()
    return JSONResponse({"doses": remedios.recados(), "recados": recados,
                         "agenda": [{k: v for k, v in i.items() if not k.startswith("_")} for i in proximos],
                         "pendencias": [p for p in pendencias if p["id"] not in dos_recados]})


@router.post("/api/doses/{recado}/tomei")
async def tomei(request: Request, recado: str):
    """`recado` = o id da dose em /api/lembretes ("remedio:<id>:<epoch>"). 409 se a dose já foi fechada."""
    if recusa := painel._checar_post(request):
        return recusa
    partes = recado.split(":")
    r = await run_in_threadpool(remedios.tomei, partes[1], None, "app", recado) if len(partes) == 3 else None
    if not r:
        return painel._erro(409, "Essa dose já foi anotada ou o lembrete não existe mais.")
    return JSONResponse({"ok": True, "mensagem": remedios.anotado([r])})
