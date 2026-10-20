#!/usr/bin/env python3
"""
Alfred Inbox API — fachada HTTP do inbox-assistant para o N8N (e outros orquestradores).

Mesmo padrão do Researcher: FastAPI + Bearer SHELL_EXECUTOR_TOKEN, porta PORT_INBOX (7072).
Endpoints síncronos (rodam em threadpool — um /ciclo longo não bloqueia /pendencias).

GET  /health
POST /ciclo        {inbox?, dry_run, janela_horas?, reprocessar, so_jornal, so_pendencias, max_emails?, notificar}
POST /scan         {inbox?, janela_horas?}                 → só classificação (nada gravado)
POST /jornal       {inbox?, janela_horas?}                 → só a edição do dia
GET  /jornal/ultimo                                        → metadados + texto da última edição
GET  /jornal/ultimo/audio                                  → .mp3
POST /semanal      {semana?: "AAAA-Www", sem_audio?, dry_run?} → podcast semanal (VAULT_NOTICIAS/podcast/)
GET  /pendencias?status=aguardando|todas|vou|nao_vou
GET  /pendencias/{id}                                      → entrada + texto explicativo
GET  /pendencias/{id}/audio                                → .mp3 da pergunta
POST /pendencias/{id}/responder  {resposta: "vou"|"nao_vou"|"ignorar", nota?}
POST /pendencias/{id}/links      {vou: url, nao_vou: url}  → guarda resumeUrl do Wait do N8N

WhatsApp (Evolution API):
POST /whatsapp/webhook[?notificar=1&async=1]   ← webhook messages.upsert (texto/áudio) → pendência tipo whatsapp
                                                 auth: header apikey | Bearer | campo apikey do payload = EVOLUTION_API_KEY
POST /pendencias/{id}/responder-texto  {texto, enviar?, nota?} → registra e envia pelo WhatsApp (simulado sem EVOLUTION_URL)
POST /pendencias/{id}/notificar-whatsapp        → manda a Pedro o resumo + áudio da pergunta (via Evolution API)
GET  /whatsapp/status                            → configuração, estado da instância, fila

Com `atraso_minutos` > 0 (perfis.yml → whatsapp:) o webhook só enfileira (acao=agendada + prazo); uma thread
de fundo iniciada com a API passa a cada FILA_INTERVALO_S pela fila e cria pendência + áudio + avisos dos
itens vencidos. Rode um único processo (sem --workers / --reload) para não duplicar a thread.
"""

import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared.lib_alfred import load_env, log  # noqa: E402

load_env(override=False)

import core  # noqa: E402
import whatsapp  # noqa: E402
import semanal  # noqa: E402
from _shared.lib_mail import find_thunderbird_profile  # noqa: E402
from _shared.lib_whatsapp import resumo_config as whatsapp_config  # noqa: E402

TOKEN = os.getenv("SHELL_EXECUTOR_TOKEN", "")
FILA_INTERVALO_S = 60


def _laco_fila_whatsapp(parar: threading.Event):
    """Fila do WhatsApp: itens cujo prazo (atraso_minutos) venceu viram pendência + áudio + avisos."""
    while True:
        try:
            whatsapp.processar_fila()
        except Exception as e:
            log(f"[whatsapp] fila: erro inesperado ({e})")
        if parar.wait(FILA_INTERVALO_S):
            return


@asynccontextmanager
async def lifespan(_app: FastAPI):
    parar = threading.Event()
    threading.Thread(target=_laco_fila_whatsapp, args=(parar,), name="whatsapp-fila", daemon=True).start()
    yield
    parar.set()   # se estiver no meio de um item, ele continua na fila e sai na próxima subida


app = FastAPI(title="Alfred Inbox", version="1.1.0", lifespan=lifespan)


def check_auth(authorization: Optional[str]):
    if not TOKEN or authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def check_webhook_auth(apikey: Optional[str], authorization: Optional[str], payload: object):
    """
    O webhook da Evolution pode ser validado de três jeitos (qualquer um basta):
      - header `apikey: <EVOLUTION_API_KEY>`  (configure em webhook.headers)
      - header `Authorization: Bearer <EVOLUTION_API_KEY | SHELL_EXECUTOR_TOKEN>`  (N8N no meio)
      - campo `apikey` do próprio payload (a Evolution manda o token da instância)
    """
    chave = os.getenv("EVOLUTION_API_KEY", "").strip()
    aceitos = {f"Bearer {t}" for t in (chave, TOKEN) if t}
    if chave and apikey == chave:
        return
    if authorization and authorization in aceitos:
        return
    if chave and isinstance(payload, dict) and str(payload.get("apikey") or "") == chave:
        return
    raise HTTPException(status_code=401, detail="Unauthorized (EVOLUTION_API_KEY / token inválido ou ausente)")


class CicloRequest(BaseModel):
    inbox: Optional[str] = None
    dry_run: bool = False
    janela_horas: Optional[int] = None
    reprocessar: bool = False
    so_jornal: bool = False
    so_pendencias: bool = False
    max_emails: Optional[int] = None
    notificar: bool = True


class RespostaRequest(BaseModel):
    resposta: str
    nota: Optional[str] = ""


class LinksRequest(BaseModel):
    vou: Optional[str] = None
    nao_vou: Optional[str] = None
    ignorar: Optional[str] = None


class RespostaTextoRequest(BaseModel):
    texto: str
    enviar: bool = True
    nota: Optional[str] = ""


@app.get("/health")
def health():
    prof = find_thunderbird_profile()
    try:
        aguardando = len(core.listar_pendencias("aguardando"))
    except Exception:
        aguardando = None
    return {
        "status": "ok",
        "inbox_default": os.getenv("INBOX_DEFAULT", ""),
        "model_chat": os.getenv("MODEL_CHAT", ""),
        "vault": os.getenv("VAULT_ALFRED", ""),
        "vault_noticias": str(core.vault_noticias()) if os.getenv("VAULT_ALFRED") else "",
        "thunderbird_profile": str(prof) if prof else None,
        "pendencias_aguardando": aguardando,
        "whatsapp": whatsapp_config(),
    }


@app.post("/ciclo")
def ciclo(req: CicloRequest, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    try:
        return core.ciclo(
            inbox_name=req.inbox, dry_run=req.dry_run, janela_horas=req.janela_horas,
            reprocessar=req.reprocessar, so_jornal=req.so_jornal, so_pendencias=req.so_pendencias,
            max_emails=req.max_emails, notificar=req.notificar,
        )
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/scan")
def scan(req: CicloRequest, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return core.ciclo(inbox_name=req.inbox, dry_run=True, janela_horas=req.janela_horas,
                      reprocessar=True, max_emails=req.max_emails)


@app.post("/jornal")
def jornal(req: CicloRequest, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return core.ciclo(inbox_name=req.inbox, janela_horas=req.janela_horas, reprocessar=req.reprocessar,
                      so_jornal=True, max_emails=req.max_emails, notificar=req.notificar)


def _ultimo_jornal() -> Optional[Path]:
    return core.ultimo_jornal_md()


class SemanalRequest(BaseModel):
    semana: Optional[str] = None     # "AAAA-Www"; default = semana ISO de hoje
    inbox: Optional[str] = None
    sem_audio: bool = False
    dry_run: bool = False


@app.post("/semanal")
def semanal_post(req: SemanalRequest, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    res = semanal.gerar_semanal(semana=req.semana, inbox_name=req.inbox, com_audio=not req.sem_audio,
                                dry_run=req.dry_run)
    if not res:
        raise HTTPException(status_code=404, detail="Sem edições do jornal nessa semana")
    return res


@app.get("/jornal/ultimo")
def jornal_ultimo(authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    md = _ultimo_jornal()
    if not md:
        raise HTTPException(status_code=404, detail="Nenhuma edição gerada ainda")
    mp3 = md.with_suffix(".mp3")
    texto = md.read_text(encoding="utf-8")
    titulo = md.stem
    for line in texto.splitlines()[:12]:
        if line.startswith("title:"):
            titulo = line[6:].strip().strip('"')
            break
    return {"titulo": titulo, "md": str(md), "mp3": str(mp3) if mp3.exists() else None, "texto": texto}


@app.get("/jornal/ultimo/audio")
def jornal_ultimo_audio(authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    md = _ultimo_jornal()
    mp3 = md.with_suffix(".mp3") if md else None
    if not mp3 or not mp3.exists():
        raise HTTPException(status_code=404, detail="Sem áudio")
    return FileResponse(str(mp3), media_type="audio/mpeg", filename=mp3.name)


@app.get("/pendencias")
def pendencias(status: str = "aguardando", authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return {"pendencias": core.listar_pendencias(status)}


@app.get("/pendencias/{pid}")
def pendencia(pid: str, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    p = core.obter_pendencia(pid)
    if not p:
        raise HTTPException(status_code=404, detail="Pendência não encontrada")
    md = core.vault() / ((p.get("arquivos") or {}).get("md") or "")
    texto = md.read_text(encoding="utf-8") if md.is_file() else ""
    return {**p, "texto": texto}


@app.get("/pendencias/{pid}/audio")
def pendencia_audio(pid: str, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    p = core.obter_pendencia(pid)
    mp3 = (p or {}).get("arquivos", {}).get("mp3")
    path = core.vault() / mp3 if mp3 else None
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Sem áudio")
    return FileResponse(str(path), media_type="audio/mpeg", filename=path.name)


@app.post("/pendencias/{pid}/responder")
def pendencia_responder(pid: str, req: RespostaRequest, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    try:
        return core.responder(pid, req.resposta, req.nota or "")
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/pendencias/{pid}/links")
def pendencia_links(pid: str, req: LinksRequest, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    try:
        return core.registrar_links_resposta(pid, req.model_dump())
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ══════════════════════════════════════════════════════════════
#  WhatsApp (Evolution API)
# ══════════════════════════════════════════════════════════════

def _processar_webhook(payload: dict, notificar: bool, enviar: bool) -> dict:
    try:
        return whatsapp.processar_mensagem_whatsapp(payload, notificar=notificar, enviar=enviar)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/whatsapp/webhook")
@app.post("/whatsapp/webhook/{evento}")
def whatsapp_webhook(
    payload: dict = Body(...),
    evento: Optional[str] = None,
    notificar: bool = False,
    enviar: bool = True,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """
    Webhook da Evolution API (messages.upsert) — síncrono: devolve a ação tomada (para o N8N).
      notificar=1 → o próprio Python manda a Pedro o resumo + áudio (sem N8N)
      enviar=0    → nunca chama a Evolution (testes)
    Com atraso_minutos > 0 a resposta é acao=agendada + prazo, sem `notificacao`: quando o prazo vence
    ninguém está esperando a resposta HTTP, então só o próprio Python avisa Pedro — chame com notificar=1.
    LLM + TTS levam ~1 min; para a Evolution chamar direto use /whatsapp/webhook-async.
    A rota /whatsapp/webhook/{evento} existe para webhook.byEvents=true (…/messages-upsert).
    """
    check_webhook_auth(apikey, authorization, payload)
    return _processar_webhook(payload, notificar, enviar)


@app.post("/whatsapp/webhook-async")
@app.post("/whatsapp/webhook-async/{evento}")
def whatsapp_webhook_async(
    background: BackgroundTasks,
    payload: dict = Body(...),
    evento: Optional[str] = None,
    notificar: bool = True,
    enviar: bool = True,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Mesmo que /whatsapp/webhook, mas responde 202 na hora e processa em segundo plano —
    para a Evolution chamar direto, sem N8N (por padrão o Python avisa Pedro: notificar=1)."""
    check_webhook_auth(apikey, authorization, payload)
    background.add_task(whatsapp.processar_mensagem_whatsapp, payload, None, enviar, notificar)
    return JSONResponse(status_code=202, content={"status": "aceito", "notificar": notificar, "enviar": enviar})


@app.post("/pendencias/{pid}/responder-texto")
def pendencia_responder_texto(pid: str, req: RespostaTextoRequest, authorization: Optional[str] = Header(None)):
    """Resposta em texto livre para pendência de WhatsApp: registra em pendencias.yml e envia pela Evolution API."""
    check_auth(authorization)
    try:
        entry = whatsapp.responder_texto(pid, req.texto, enviar=req.enviar, origem="via API", nota=req.nota or "")
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    envio = (entry.get("whatsapp") or {}).get("envio") or {}
    if req.enviar and entry.get("status") == "aguardando":
        raise HTTPException(status_code=502, detail={"erro": envio.get("erro") or "envio falhou", "pendencia": entry})
    return entry


@app.post("/pendencias/{pid}/notificar-whatsapp")
def pendencia_notificar_whatsapp(pid: str, authorization: Optional[str] = Header(None)):
    """Manda a Pedro (WHATSAPP_NUMERO_PEDRO) o resumo + áudio da pergunta pela Evolution API."""
    check_auth(authorization)
    try:
        return whatsapp.notificar_pedro(pid)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/whatsapp/status")
def whatsapp_status(authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return whatsapp.status()


# ══════════════════════════════════════════════════════════════
#  Limpeza pós-podcast (LIMPEZA.md) — a extensão do Thunderbird executa
# ══════════════════════════════════════════════════════════════
# GET  /limpeza/candidatos            → dry-run (nada gravado)
# POST /limpeza/propor                → cria lote aguardando ("posso apagar estes N e-mails?")
# GET  /limpeza/lotes?status=         → lotes
# GET  /limpeza/fila                  → lotes APROVADOS para a extensão (marca em_execucao)
# GET  /limpeza/{id}
# POST /limpeza/{id}/aprovar {nota?}  → confirmação explícita do usuário
# POST /limpeza/{id}/recusar {nota?}
# POST /limpeza/{id}/resultado {itens:[{message_id,status,detalhe}], executor?}  ← extensão

import limpeza  # noqa: E402


class LimpezaNotaRequest(BaseModel):
    nota: Optional[str] = ""


class LimpezaResultadoRequest(BaseModel):
    itens: list[dict]
    executor: str = "thunderbird-ext"


def _limpeza_erros(fn, *a):
    try:
        return fn(*a)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/limpeza/candidatos")
def limpeza_candidatos(inbox: Optional[str] = None, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return limpeza.candidatos(inbox, incluir_mantidos=True)


@app.post("/limpeza/propor")
def limpeza_propor(inbox: Optional[str] = None, notificar: bool = True, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    lote = limpeza.propor(inbox, notificar=notificar)
    return {"lote": lote, "pergunta": limpeza.resumo_lote(lote) if lote else None}


@app.get("/limpeza/lotes")
def limpeza_lotes(status: Optional[str] = None, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return {"lotes": limpeza.listar_lotes(status)}


@app.get("/limpeza/fila")
def limpeza_fila(authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return {"lotes": limpeza.fila_execucao()}


@app.get("/limpeza/{lid}")
def limpeza_lote(lid: str, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    lote = limpeza.obter_lote(lid)
    if not lote:
        raise HTTPException(status_code=404, detail="Lote não encontrado")
    return {**lote, "pergunta": limpeza.resumo_lote(lote)}


@app.post("/limpeza/{lid}/aprovar")
def limpeza_aprovar(lid: str, req: LimpezaNotaRequest, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return _limpeza_erros(limpeza.aprovar, lid, req.nota or "")


@app.post("/limpeza/{lid}/recusar")
def limpeza_recusar(lid: str, req: LimpezaNotaRequest, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return _limpeza_erros(limpeza.recusar, lid, req.nota or "")


@app.post("/limpeza/{lid}/resultado")
def limpeza_resultado(lid: str, req: LimpezaResultadoRequest, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return _limpeza_erros(limpeza.registrar_resultado, lid, req.itens, req.executor)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT_INBOX", "7072")))
