"""Tela /painel/redigir: e-mails de evento/entrevista que esperam Pedro liberar. Mesma segurança do Painel (painel.py)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import painel
import redigir

router = APIRouter(prefix="/painel", include_in_schema=False, route_class=painel._RotaPainel)


@router.get("/redigir")
def tela_redigir(request: Request):
    if not painel._nonce_da_sessao(request):
        return painel._para_login(request)
    itens = redigir.listar()
    return painel._pagina(request, "redigir.html", "redigir",
                          aguardando=[i for i in itens if i.get("status") == "aguardando"],
                          resolvidos=[i for i in itens if i.get("status") != "aguardando"][:10])


async def _resolver(request: Request, rid: str, status: str, mensagem: str, texto=None) -> JSONResponse:
    item = await run_in_threadpool(redigir.resolver, rid, status, texto)
    if not item:
        return painel._erro(404, "Rascunho não encontrado.")
    if not item.get("_mudou"):
        return painel._erro(409, "Este rascunho já foi resolvido.", status=item.get("status"))
    return JSONResponse({"ok": True, "mensagem": mensagem, "status": status})


@router.post("/api/redigir/{rid}/liberar")
async def acao_liberar(request: Request, rid: str):
    if recusa := painel._checar_post(request):
        return recusa
    corpo = await request.json()
    texto = str(corpo.get("texto") or "").strip()
    if not texto:
        return painel._erro(422, "O texto está vazio.")
    return await _resolver(request, rid, "liberado", "Liberado: o texto vai para a janela do Thunderbird.", texto)


@router.post("/api/redigir/{rid}/recusar")
async def acao_recusar(request: Request, rid: str):
    if recusa := painel._checar_post(request):
        return recusa
    return await _resolver(request, rid, "recusado", "Recusado. Nada vai para a janela do Thunderbird.")
