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
GET  /pendencias?status=aguardando|todas|vou|nao_vou
GET  /pendencias/{id}                                      → entrada + texto explicativo
GET  /pendencias/{id}/audio                                → .mp3 da pergunta
POST /pendencias/{id}/responder  {resposta: "vou"|"nao_vou"|"ignorar", nota?}
POST /pendencias/{id}/links      {vou: url, nao_vou: url}  → guarda resumeUrl do Wait do N8N
"""

import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared.lib_alfred import load_env, log  # noqa: E402

load_env(override=False)

import core  # noqa: E402
from _shared.lib_mail import find_thunderbird_profile  # noqa: E402

app = FastAPI(title="Alfred Inbox", version="1.0.0")
TOKEN = os.getenv("SHELL_EXECUTOR_TOKEN", "")


def check_auth(authorization: Optional[str]):
    if not TOKEN or authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


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
        "thunderbird_profile": str(prof) if prof else None,
        "pendencias_aguardando": aguardando,
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
    d = core.vault() / "jornal"
    if not d.is_dir():
        return None
    mds = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return mds[0] if mds else None


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT_INBOX", "7072")))
