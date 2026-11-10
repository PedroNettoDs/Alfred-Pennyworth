"""
Voz no Painel — microfone e fala nas telas de conversa (Conversa, Escrita e a caixinha que o Pestinha abre).

POST /painel/api/voz           áudio gravado pelo navegador (corpo cru, no Content-Type do MediaRecorder) → {ok, texto}
POST /painel/api/voz/falar     {texto} de uma frase da resposta → {ok, url}, na hora (só guarda o texto)
GET  /painel/api/voz/fala/ID.mp3   o áudio, gerado e entregue aos pedaços enquanto o navegador já toca

Ouvir: a transcrição é local e o áudio não é guardado. Motor: faster-whisper em CPU (lib_audio, motor="whisper"),
fora da fila do Ollama; sem o pacote instalado, o gemma4 de sempre. O voz.js manda o áudio quando Pedro para de
gravar e põe o texto na caixa; quem confere e envia é ele.

Falar: o mesmo mecanismo do jornal e do resumo das pendências (lib_tts: edge-tts), na voz das pendências. O texto
da resposta vai para o serviço da Microsoft, como nesses dois casos. Só com "Ativar áudio" ligado na tela. O voz.js
manda frase por frase conforme a resposta chega, e o áudio de cada uma sai em fluxo: o primeiro som vem ~0,8 s
depois da primeira frase, sem esperar a resposta nem o .mp3 inteiros. Nada vai para o disco: o texto das últimas
FALAS_GUARDADAS frases fica na memória até o navegador buscar o áudio.

Mesma segurança do Painel (só 127.0.0.1, sessão, CSRF): usa _RotaPainel e as checagens de painel.py.
"""

import os
import re
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import conversa
import painel
from _shared import lib_audio, lib_tts
from _shared.lib_alfred import log

MAX_SEGUNDOS = 120                 # o voz.js para a gravação sozinho neste tempo
MAX_BYTES = 12 * 1024 * 1024       # 2 min de webm/opus não chegam a 2 MB; mais que isto é engano
TOKENS_POR_SEGUNDO = 12            # teto da transcrição por segundo de áudio; fala rápida passa pouco de 5
SILENCIO_FINAL = 0.8               # s: sem ele, a prévia (áudio cortado no meio da fala) volta vazia
TERMOS = "Alfred, Fatec, Thunderbird, Obsidian."   # nomes que o Whisper deve reconhecer; VOZ_TERMOS no .env troca
VOZ = "pt-BR-AntonioNeural"        # a voz do Alfred nos resumos de pendência (core.py, whatsapp.py)
FALA_MAX = 1200                    # caracteres por pedido; o voz.js também para de falar depois disso numa resposta
FALAS_GUARDADAS = 40               # frases esperando o navegador buscar o áudio (uma resposta longa tem ~15)
RESTO = " O restante está na tela."
_ID = re.compile(r"^[0-9a-f]{16}$")
_FALAS: dict[str, str] = {}        # id → texto a falar

painel.ESTATICOS.update({"voz.js": "text/javascript; charset=utf-8"})

router = APIRouter(prefix="/painel", include_in_schema=False, route_class=painel._RotaPainel)


@router.post("/api/voz")
async def transcrever(request: Request):
    if recusa := painel._checar_post(request):
        return recusa
    declarado = request.headers.get("content-length", "")
    if declarado.isdigit() and int(declarado) > MAX_BYTES:
        return painel._erro(413, "Áudio grande demais. Grave até 2 minutos.")
    audio = await request.body()
    if not audio:
        return painel._erro(400, "Não chegou áudio nenhum.")
    if len(audio) > MAX_BYTES:
        return painel._erro(413, "Áudio grande demais. Grave até 2 minutos.")
    r = await run_in_threadpool(lib_audio.transcrever_audio, audio, request.headers.get("content-type", ""),
                                MAX_SEGUNDOS, tokens_por_segundo=TOKENS_POR_SEGUNDO,
                                num_ctx=conversa._cfg()["num_ctx"],   # o do chat: sem recarregar o modelo a cada fala
                                silencio_final=SILENCIO_FINAL,
                                motor="whisper", dica=os.getenv("VOZ_TERMOS", "").strip() or TERMOS)
    if not r["texto"]:
        if r["ocupado"]:
            return painel._erro(503, f"O modelo de voz não respondeu em {lib_audio.ESPERA_INTERATIVA_S} s: a placa está "
                                     "ocupada com outra tarefa do Alfred (uma pesquisa, por exemplo). Tente de novo "
                                     "em instantes.")
        if r["erro"]:
            return painel._erro(503, "A transcrição local não respondeu. Confira o Ollama (gemma4) e o ffmpeg.")
        return painel._erro(422, "Não entendi o áudio. Fale mais perto do microfone e tente de novo.")
    return JSONResponse({"ok": True, "texto": r["texto"], "cortado": r["cortado"]})


def texto_para_fala(markdown: str) -> str:
    """O que será dito: sem markdown e, se for longo, só o começo, cortado no fim de uma frase."""
    texto = " ".join(lib_tts.limpar_para_fala(markdown).split())
    if len(texto) <= FALA_MAX:
        return texto
    corte = max(texto.rfind(sinal, 0, FALA_MAX) for sinal in (". ", "! ", "? "))
    return (texto[:corte + 1] if corte > FALA_MAX // 2 else texto[:FALA_MAX].rsplit(" ", 1)[0] + ".") + RESTO


@router.post("/api/voz/falar")
async def falar(request: Request):
    if recusa := painel._checar_post(request):
        return recusa
    texto = texto_para_fala(str((await painel._corpo_json(request)).get("texto") or ""))
    if not texto:
        return painel._erro(400, "Não há o que falar.")
    fala = secrets.token_hex(8)
    _FALAS[fala] = texto
    for antiga in list(_FALAS)[:-FALAS_GUARDADAS]:
        del _FALAS[antiga]
    return JSONResponse({"ok": True, "url": f"/painel/api/voz/fala/{fala}.mp3"})


async def _audio(texto: str):
    try:
        async for pedaco in lib_tts.fluxo_tts(texto, VOZ):
            yield pedaco
    except Exception as e:     # o edge-tts caiu no meio: o áudio acaba ali e a resposta continua na tela
        log(f"[voz] edge-tts falhou: {e}")


@router.get("/api/voz/fala/{fala}.mp3")
async def ouvir(request: Request, fala: str):
    if not painel._nonce_da_sessao(request):
        return painel._erro(401, painel.MSG_SESSAO)
    texto = _FALAS.get(fala) if _ID.match(fala) else None
    if texto is None:
        return painel._erro(404, "Áudio não encontrado.")
    return StreamingResponse(_audio(texto), media_type="audio/mpeg")
