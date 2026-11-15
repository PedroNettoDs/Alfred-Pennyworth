"""
Alfred Inbox Assistant — WhatsApp (Evolution API).

Mensagens e áudios recebidos no WhatsApp passam pelo mesmo tratamento do e-mail:

    webhook Evolution (messages.upsert)
        ↓ normalizar_payload()
        ↓ _descarte_sem_texto()           grupo ignorado / contatos_ignorar → não vira pendência, áudio nem é transcrito
    texto | áudio → transcrever_audio()   (gemma4 via Ollama; faster-whisper como fallback)
        ↓ classificar_whatsapp()          heurísticas + Ollama (JSON):
        ↓                                 profissional | pessoal | grupo | automatico | spam
    enfileirar_mensagem()                 espera `atraso_minutos` sem resposta de Pedro (logs/whatsapp_fila.json);
        ↓ processar_fila()                fromMe na mesma conversa cancela — só um registro respondida_manual
        ↓                                 (rascunho do Alfred × resposta de Pedro), sem .md/áudio/aviso/envio
    criar_pendencia_whatsapp()            texto explicativo + áudio "o que você quer responder?"
        ↓                                 pendencias.yml: tipo=whatsapp, status=aguardando, resposta_texto=null
    responder_texto()                     registra a resposta de Pedro e envia pela Evolution API

A resposta de Pedro chega pelo MESMO webhook: ele cita (responde) a mensagem em que
Alfred resumiu a conversa — essa mensagem carrega "ref: <id>" — e o texto citado vira
responder_texto(). Se Pedro responder o contato direto do celular, a pendência fecha
como "respondida_manual" (ou, se ainda estava na fila, sai dela e vira um registro respondida_manual
com o rascunho que o Alfred proporia). "ignorar" fecha sem enviar nada.

Mais de uma sessão: cada conta de perfis.yml → whatsapp: é uma instância da Evolution com um papel
(sessoes()). "pessoal" segue o fluxo acima; "assistente" é o número do Alfred (_atender): Pedro manda
ordens para a agenda e os outros são recepcionados — ver "Sessão assistente" mais abaixo.

Reutiliza core.py (pendencias.yml, vault, templates, TTS, notificação). Nenhuma função
levanta exceção por falha de LLM/rede: degrada com log() e segue.
"""

import base64
import difflib
import functools
import json
import os
import re
import sys
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared.lib_alfred import log, ollama_generate, ollama_generate_json  # noqa: E402
from _shared import lib_atividade, lib_db, lib_porta  # noqa: E402
from _shared.lib_audio import transcrever_audio  # noqa: E402
from _shared.lib_templates import render_template  # noqa: E402
from _shared.lib_tts import limpar_para_fala  # noqa: E402
from _shared.lib_whatsapp import (  # noqa: E402
    enviar_texto, enviar_audio, baixar_midia, jid_para_numero, eh_grupo, formatar_numero,
    numero_pedro, simulado, instancia as instancia_padrao,
    enviado_pelo_alfred, segundos_desde_ultimo_envio,
)
from _shared.perfis import get_whatsapp, list_all  # noqa: E402

import core  # noqa: E402
import agenda  # noqa: E402

SEEN_FILE = core.PROJECT_ROOT / "logs" / "whatsapp_seen.json"
FILA_FILE = core.PROJECT_ROOT / "logs" / "whatsapp_fila.json"   # mensagens esperando atraso_minutos

# O /whatsapp/webhook-async processa cada mensagem numa thread: _LOCK serializa o fluxo dentro da API
# (garante que o eco do envio só é tratado depois de pergunta_msg_id / envio.message_id gravados).
# RLock: processar → responder_texto / notificar_pedro reentra. As gravações de pendencias.yml usam
# também core.trava_pendencias() (entre PROCESSOS: CLI, aguardadores, ciclo, tools) — ordem: _LOCK → trava.
_LOCK = threading.RLock()


def _serializado(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)
    return wrapper

CATEGORIAS_WA = ("profissional", "pessoal", "grupo", "automatico", "spam")
# Só estas podem pular o LLM via lib_db (cache por contato+tipo): perder uma mensagem de gente é pior que gastar GPU.
CACHEAVEIS_WA = ("automatico", "spam")
CACHE_CONFIANCA_MINIMA = 0.9
SCHEMA_CLASSIFICACAO_WA = {
    "type": "object",
    "properties": {
        "categoria":       {"type": "string", "enum": list(CATEGORIAS_WA)},
        "confianca":       {"type": "number"},
        "requer_resposta": {"type": "boolean"},
        "urgencia":        {"type": "string", "enum": ["baixa", "media", "alta"]},
        "resumo":          {"type": "string"},
        "motivo":          {"type": "string"},
    },
    "required": ["categoria", "confianca", "requer_resposta", "urgencia", "resumo", "motivo"],
}

SCHEMA_RASCUNHO_WA = {
    "type": "object",
    "properties": {
        "rascunho":    {"type": "string"},
        "compromisso": {"type": "object", "properties": {
            "titulo": {"type": "string"}, "data": {"type": "string"}, "hora": {"type": "string"}},
            "required": ["titulo", "data", "hora"]},
    },
    "required": ["rascunho", "compromisso"],
}

REF_RE     = re.compile(r"ref:\s*([0-9]{8}-[a-z0-9-]+)", re.I)
IGNORAR_RE = re.compile(r"^\s*(ignorar|ignora|não responder|nao responder|pular|skip)\s*[.!]?\s*$", re.I)

_TIPOS_MSG = {
    "conversation": "texto", "extendedTextMessage": "texto", "audioMessage": "audio",
    "imageMessage": "imagem", "videoMessage": "video", "documentMessage": "documento",
    "documentWithCaptionMessage": "documento", "stickerMessage": "figurinha",
    "contactMessage": "contato", "locationMessage": "localizacao", "liveLocationMessage": "localizacao",
    "reactionMessage": "reacao", "pollCreationMessage": "enquete", "protocolMessage": "protocolo",
}
_IGNORAR_TIPOS = ("reacao", "protocolo", "figurinha")


# ══════════════════════════════════════════════════════════════
#  Config
# ══════════════════════════════════════════════════════════════

def whatsapp_default() -> str:
    import os
    nome = os.getenv("WHATSAPP_DEFAULT", "").strip()
    if nome:
        return nome
    contas = list_all().get("whatsapp", [])
    if not contas:
        raise RuntimeError("Nenhuma conta definida em perfis.yml (seção whatsapp:)")
    return contas[0]


def carregar_cfg(nome: Optional[str] = None) -> tuple[str, dict]:
    nome = nome or whatsapp_default()
    return nome, get_whatsapp(nome)


# ══════════════════════════════════════════════════════════════
#  Sessões — uma conta de perfis.yml = uma instância da Evolution (um número pareado) + um papel
# ══════════════════════════════════════════════════════════════
#   pessoal    — o número de Pedro: mensagens viram pendência "o que você quer responder?"
#   assistente — o número do Alfred: Pedro manda ordens (agenda); os outros são recepcionados
# O papel vem de perfis.yml (`papel:`); a escolha feita no Painel fica em SESSOES_FILE e vale mais.
# SESSOES_FILE guarda também o número conectado em cada sessão (campo `sender` do webhook): é como uma
# sessão reconhece a conversa com a outra e não trata o aviso do Alfred como mensagem de contato.

PAPEIS = ("pessoal", "assistente")
SESSOES_FILE = core.PROJECT_ROOT / "logs" / "whatsapp_sessoes.json"


def _load_sessoes() -> dict:
    try:
        data = json.loads(SESSOES_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data = data if isinstance(data, dict) else {}
    return {k: dict(data.get(k) or {}) for k in ("papeis", "numeros", "conversa")}


def _save_sessoes(data: dict) -> None:
    SESSOES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSOES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SESSOES_FILE)


def sessoes() -> list[dict]:
    """→ [{nome, instancia, papel, numero}] — uma por conta de perfis.yml. Sem `instancia:` na conta, a conta
    padrão usa EVOLUTION_INSTANCE e as outras usam o próprio nome."""
    nomes = list_all().get("whatsapp", [])
    if not nomes:
        return []
    est, padrao = _load_sessoes(), whatsapp_default()
    out = []
    for nome in nomes:
        cfg = get_whatsapp(nome)
        papel = est["papeis"].get(nome) or cfg.get("papel")
        out.append({"nome": nome,
                    "instancia": str(cfg.get("instancia") or "").strip() or (instancia_padrao() if nome == padrao else nome),
                    "papel": papel if papel in PAPEIS else "pessoal",
                    "numero": est["numeros"].get(nome) or ""})
    return out


def obter_sessao(nome: Optional[str] = None) -> dict:
    """Sessão pelo nome da conta (None = a padrão). KeyError se não existe."""
    nome = nome or whatsapp_default()
    s = next((s for s in sessoes() if s["nome"] == nome), None)
    if not s:
        raise KeyError(f"Sessão de WhatsApp não encontrada em perfis.yml: {nome}")
    return s


def _sessao_da_instancia(inst) -> Optional[dict]:
    return next((s for s in sessoes() if s["instancia"] == inst), None)


def definir_papel(nome: str, papel: str) -> list[dict]:
    """Grava o papel da sessão. Só uma é a assistente (é dela que saem os avisos a Pedro): escolher outra
    devolve a anterior a "pessoal". KeyError (sessão) / ValueError (papel). → sessoes()."""
    if papel not in PAPEIS:
        raise ValueError(f"Papel inválido: {papel} (use {' ou '.join(PAPEIS)})")
    with core.trava_arquivo(SESSOES_FILE):
        todas = sessoes()
        if nome not in [s["nome"] for s in todas]:
            raise KeyError(f"Sessão de WhatsApp não encontrada em perfis.yml: {nome}")
        est = _load_sessoes()
        for s in todas:
            if s["nome"] == nome:
                est["papeis"][nome] = papel
            elif papel == "assistente" and s["papel"] == "assistente":
                est["papeis"][s["nome"]] = "pessoal"
        _save_sessoes(est)
    log(f"[whatsapp] sessão {nome} → papel {papel}")
    return sessoes()


def _anotar_numero(sessao: dict, dono: str) -> None:
    if not dono or sessao.get("numero") == dono:
        return
    with core.trava_arquivo(SESSOES_FILE):
        est = _load_sessoes()
        est["numeros"][sessao["nome"]] = dono
        _save_sessoes(est)


# ══════════════════════════════════════════════════════════════
#  Payload da Evolution API → mensagem normalizada
# ══════════════════════════════════════════════════════════════

def _desembrulhar(message: dict) -> dict:
    """ephemeralMessage / viewOnceMessage / documentWithCaptionMessage embrulham a mensagem real."""
    for _ in range(4):
        for k in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "viewOnceMessageV2Extension",
                  "documentWithCaptionMessage", "editedMessage"):
            inner = message.get(k)
            if isinstance(inner, dict) and isinstance(inner.get("message"), dict):
                message = inner["message"]
                break
        else:
            return message
    return message


def _tipo_e_conteudo(message: dict, data: dict) -> tuple[str, str, Optional[dict]]:
    """Retorna (tipo_msg, texto, audio). texto = texto da mensagem ou legenda da mídia."""
    message = _desembrulhar(message or {})
    if "conversation" in message and isinstance(message["conversation"], str):
        return "texto", message["conversation"].strip(), None
    ext = message.get("extendedTextMessage")
    if isinstance(ext, dict) and ext.get("text") is not None:
        return "texto", str(ext.get("text") or "").strip(), None
    am = message.get("audioMessage")
    if isinstance(am, dict):
        b64 = message.get("base64") or data.get("base64") or am.get("base64")
        audio = {
            "base64":   b64 if isinstance(b64, str) and b64 else None,
            "mimetype": str(am.get("mimetype") or "audio/ogg"),
            "segundos": int(am.get("seconds") or 0),
            "ptt":      bool(am.get("ptt")),
            "url":      am.get("url"),
            # bloco original (mediaKey, directPath...) para getBase64FromMediaMessage — a v2.3.7 só põe
            # `base64` no webhook se ele já estava configurado quando a instância conectou
            "mensagem": {"audioMessage": am},
        }
        return "audio", "", audio
    for chave, tipo in _TIPOS_MSG.items():
        bloco = message.get(chave)
        if isinstance(bloco, dict):
            legenda = str(bloco.get("caption") or bloco.get("fileName") or bloco.get("displayName") or "").strip()
            return tipo, legenda, None
    # messageContextInfo sozinho, senderKeyDistributionMessage etc.
    chaves = [k for k in message.keys() if k not in ("messageContextInfo", "senderKeyDistributionMessage", "base64")]
    return (chaves[0] if chaves else "vazio"), "", None


def _contextos(message: dict, ctx_data: Optional[dict] = None) -> list[dict]:
    """contextInfo de cada bloco da mensagem + o de data.contextInfo — a Evolution v2.3.7 converte
    extendedTextMessage em `conversation` e sobe o contextInfo (citação, menções) para fora de `message`."""
    message = _desembrulhar(message or {})
    out = [b["contextInfo"] for b in message.values() if isinstance(b, dict) and isinstance(b.get("contextInfo"), dict)]
    if isinstance(ctx_data, dict):
        out.append(ctx_data)
    return out


def _extrair_citacao(message: dict, ctx_data: Optional[dict] = None) -> Optional[dict]:
    """Mensagem citada (reply do WhatsApp): {stanza_id, texto, participant}."""
    for ctx in _contextos(message, ctx_data):
        quoted = ctx.get("quotedMessage")
        if not (ctx.get("stanzaId") or quoted):
            continue
        texto = ""
        if isinstance(quoted, dict):
            q = _desembrulhar(quoted)
            texto = (q.get("conversation") or (q.get("extendedTextMessage") or {}).get("text")
                     or (q.get("imageMessage") or {}).get("caption") or "")
        return {"stanza_id": ctx.get("stanzaId"), "texto": str(texto or "").strip(),
                "participant": ctx.get("participant"), "mencoes": list(ctx.get("mentionedJid") or [])}
    return None


def _mencoes(message: dict, ctx_data: Optional[dict] = None) -> list[str]:
    out: list[str] = []
    for ctx in _contextos(message, ctx_data):
        out.extend(str(j) for j in (ctx.get("mentionedJid") or []) if str(j) not in out)
    return out


def _timestamp_iso(ts) -> str:
    try:
        if isinstance(ts, dict):   # protobuf Long {low, high, unsigned}
            ts = int(ts.get("low", 0)) + (int(ts.get("high", 0)) << 32)
        ts = int(ts)
        if ts > 10_000_000_000:    # milissegundos
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="minutes")
    except Exception:
        return core._now().isoformat(timespec="minutes")


def _sem_lid(jid, alternativo) -> str:
    """Em grupos a v2.3.7 manda key.participant = '<id>@lid' e o número em key.participantAlt."""
    jid = str(jid or "")
    return str(alternativo) if jid.endswith("@lid") and alternativo else jid


def normalizar_payload(payload: dict) -> Optional[dict]:
    """
    Webhook da Evolution (envelope {event, instance, data, sender...}) ou só o `data`
    → dict normalizado. None se não houver chave de mensagem.
    """
    if not isinstance(payload, dict):
        return None
    evento = str(payload.get("event") or "").lower().replace("_", ".")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if isinstance(data, list):   # algumas versões mandam lista em messages.upsert
        data = data[0] if data else {}
    key = data.get("key") if isinstance(data.get("key"), dict) else None
    if not key or not key.get("remoteJid"):
        return None
    if evento and evento not in ("messages.upsert", "send.message"):
        return {"ignorar": f"evento '{evento}' não tratado", "evento": evento}

    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    remote_jid = str(key.get("remoteJid") or "")
    grupo = eh_grupo(remote_jid)
    numero = jid_para_numero(remote_jid)
    if remote_jid.endswith("@lid"):   # ids "lid" não são número: Evolution manda alternativas
        for alt in (key.get("remoteJidAlt"), key.get("senderPn"), data.get("senderPn"), data.get("remoteJidAlt")):
            if alt:
                numero = jid_para_numero(alt)
                break
    tipo_msg, texto, audio = _tipo_e_conteudo(message, data)
    return {
        "evento":       evento or "messages.upsert",
        "instancia":    str(payload.get("instance") or data.get("instance") or instancia_padrao()),
        "dono":         jid_para_numero(payload.get("sender") or ""),   # número conectado na instância
        "message_id":   str(key.get("id") or ""),
        "remote_jid":   remote_jid,
        "numero":       numero,
        "grupo":        grupo,
        "participante": jid_para_numero(_sem_lid(key.get("participant"), key.get("participantAlt"))
                                        or data.get("participant") or ""),
        "nome":         str(data.get("pushName") or "").strip(),
        "from_me":      bool(key.get("fromMe")),
        "tipo_msg":     tipo_msg,
        "texto":        texto,
        "audio":        audio,
        "citacao":      _extrair_citacao(message, data.get("contextInfo")),
        "mencoes":      _mencoes(message, data.get("contextInfo")),
        "timestamp":    _timestamp_iso(data.get("messageTimestamp")),
        "key":          {k: key[k] for k in ("id", "remoteJid", "fromMe", "participant") if k in key},
    }


def _contato_legivel(msg: dict) -> str:
    return msg.get("nome") or formatar_numero(msg.get("numero")) or msg.get("remote_jid") or "desconhecido"


# ══════════════════════════════════════════════════════════════
#  Transcrição
# ══════════════════════════════════════════════════════════════

def transcrever_mensagem(msg: dict, cfg: dict, motor: str = "ollama", dica: str = "") -> dict:
    """Áudio do webhook (base64) ou baixado da Evolution → transcrever_audio(). `motor`/`dica`: ver lib_audio
    (o canal direto de Pedro usa motor="whisper")."""
    audio = msg.get("audio") or {}
    b64 = audio.get("base64")
    mimetype = audio.get("mimetype") or "audio/ogg"
    if not b64:
        r = baixar_midia(msg.get("key") or {}, audio.get("mensagem"), inst=msg.get("instancia"))
        if r.get("ok"):
            b64, mimetype = r.get("base64"), r.get("mimetype") or mimetype
        else:
            return {"texto": "", "metodo": "", "erro": "áudio sem base64 no webhook e download falhou: "
                    + str(r.get("erro") or r.get("resposta") or "")[:160]}
    try:
        raw = b64.split(",", 1)[1] if b64.startswith("data:") else b64
        dados = base64.b64decode(raw)
    except Exception as e:
        return {"texto": "", "metodo": "", "erro": f"base64 inválido: {e}"}
    log(f"[whatsapp] transcrevendo áudio de {_contato_legivel(msg)} ({audio.get('segundos') or '?'}s, {len(dados)} bytes)")
    r = transcrever_audio(dados, mimetype, max_segundos=int(cfg.get("audio_max_segundos", 600)),
                          **core.ctx_do_chat(os.getenv("MODEL_TRANSCRIBE", "").strip()),
                          **({"motor": motor, "dica": dica} if motor != "ollama" else {}))
    if r.get("texto"):
        log(f"[whatsapp] transcrição ({r['metodo']}): {r['texto'][:100]!r}")
    return r


# ══════════════════════════════════════════════════════════════
#  Classificação (mesmo padrão de core.classificar, prompt para mensagens curtas)
# ══════════════════════════════════════════════════════════════

def _na_lista(msg: dict, lista) -> bool:
    alvo = f"{msg.get('numero','')} {msg.get('nome','')} {msg.get('remote_jid','')}".lower()
    return any(str(s).lower().strip() and str(s).lower().strip() in alvo for s in (lista or []))


def _heuristica(cat, resumo, motivo, requer=False, urg="baixa") -> dict:
    return {"categoria": cat, "confianca": 1.0, "requer_resposta": requer, "urgencia": urg,
            "resumo": resumo[:300], "motivo": motivo, "origem": "heuristica"}


def _descarte_sem_texto(msg: dict, cfg: dict) -> Optional[dict]:
    """
    Heurísticas que descartam a mensagem sem olhar o texto (contatos_ignorar, reação/figurinha, grupo fora
    do modo `grupos`) → classificação que nunca vira pendência; None = a decisão depende do texto.
    processar_mensagem_whatsapp consulta antes de transcrever: áudio de grupo ignorado não passa pelo gemma4.
    """
    if _na_lista(msg, cfg.get("contatos_ignorar")):
        return _heuristica("ignorado", "contato na lista de ignorados", "contatos_ignorar")
    if msg.get("tipo_msg") in _IGNORAR_TIPOS:
        return _heuristica("outro", f"{msg['tipo_msg']} sem texto", "tipo de mensagem sem conteúdo")
    if msg.get("grupo"):
        modo = str(cfg.get("grupos") or "ignorar")
        pedro = numero_pedro() or msg.get("dono") or ""
        mencionado = bool(pedro) and any(jid_para_numero(j) == pedro for j in msg.get("mencoes", []))
        if modo == "ignorar" or (modo == "mencao" and not mencionado):
            return _heuristica("grupo", f"mensagem de grupo ({msg.get('remote_jid')})", f"grupos: {modo}")
    return None


def classificar_whatsapp(msg: dict, cfg: dict, texto: Optional[str] = None) -> dict:
    """
    Retorna {"categoria", "confianca", "requer_resposta", "urgencia", "resumo", "motivo", "origem"}.
    categoria ∈ CATEGORIAS_WA ∪ {"ignorado", "outro"}; origem = "heuristica" | "llm".
    """
    texto = (texto if texto is not None else msg.get("texto") or "").strip()
    contato = _contato_legivel(msg)
    h = _heuristica

    descarte = _descarte_sem_texto(msg, cfg)
    if descarte:
        return descarte
    if msg.get("grupo") and str(cfg.get("grupos") or "ignorar") == "mencao":   # sem menção já saiu acima
        return h("grupo", f"{contato} mencionou Pedro no grupo: {texto[:120]}", "menção em grupo",
                 requer=True, urg="media")
    if _na_lista(msg, cfg.get("contatos_sempre")):
        return h("profissional", f"{contato}: {texto[:160]}" if texto else f"{msg.get('tipo_msg')} de {contato}",
                 "contatos_sempre", requer=True, urg="media")
    if not texto:
        if msg.get("tipo_msg") == "audio":
            # sem transcrição não dá pra classificar; áudio de pessoa real merece atenção
            return h("profissional", f"áudio de {contato} (não transcrito)", "áudio sem transcrição",
                     requer=True, urg="media")
        return h("outro", f"{msg.get('tipo_msg')} de {contato} sem legenda", "sem texto para classificar")

    # Contato que o LLM já classificou como automático/spam várias vezes seguidas → sem LLM (lib_db.cache_get)
    chave_cache = None
    if cfg.get("cache_classificacao", True) and msg.get("remote_jid") and not msg.get("grupo"):
        chave_cache = f"{msg['remote_jid']}|{msg.get('tipo_msg', 'texto')}"
        hit = lib_db.cache_get("whatsapp", chave_cache)
        if hit:
            log(f"[cache] {contato[:30]} → {hit['categoria']} (sem LLM)")
            return {**hit, "resumo": f"{contato}: {texto[:160]}", "origem": "cache"}

    dicas = []
    if msg.get("tipo_msg") == "audio":
        dicas.append(f"áudio de {msg['audio'].get('segundos') or '?'}s transcrito automaticamente")
    elif msg.get("tipo_msg") != "texto":
        dicas.append(f"{msg['tipo_msg']} com legenda")
    if msg.get("grupo"):
        dicas.append("mensagem de grupo")
    if not msg.get("nome"):
        dicas.append("número sem nome salvo (pushName vazio)")
    if msg.get("citacao"):
        dicas.append("responde a uma mensagem anterior")
    perfil = cfg.get("_perfil") or {}
    prompt = render_template("whatsapp_classificar", {
        "contato":          msg.get("nome") or "(sem nome)",
        "numero":           formatar_numero(msg.get("numero")) or msg.get("remote_jid", ""),
        "tipo_msg":         msg.get("tipo_msg", "texto"),
        "data":             msg.get("timestamp") or "desconhecida",
        "dicas":            "; ".join(dicas) or "nenhum",
        "texto":            texto[:3000],
        "perfil_descricao": perfil.get("descricao", ""),
    })
    data = ollama_generate_json(prompt, model=core._modelo_classificacao(), timeout=120,
                                schema=SCHEMA_CLASSIFICACAO_WA,
                                options={**core.OPTS_JSON, "num_predict": 400,
                                         **core.ctx_do_chat(core._modelo_classificacao())})
    if not isinstance(data, dict):
        # classificador mudo: trata como profissional que requer resposta — perder mensagem é pior
        return {"categoria": "profissional", "confianca": 0.0, "requer_resposta": True, "urgencia": "media",
                "resumo": f"{contato}: {texto[:160]}", "motivo": "classificador não respondeu", "origem": "llm"}

    cat = str(data.get("categoria", "")).lower().strip()
    if cat not in CATEGORIAS_WA:
        cat = "outro"
    try:
        conf = max(0.0, min(1.0, float(data.get("confianca", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    motivo = str(data.get("motivo", "")).strip()
    minimo = float(cfg.get("confianca_minima", 0.5))
    if cat in ("profissional", "pessoal") and conf < minimo:
        motivo = f"baixa confiança ({conf:.2f} < {minimo}) para '{cat}'; " + motivo
        cat = "outro"
    urg = str(data.get("urgencia", "baixa")).lower()
    res = {
        "categoria": cat, "confianca": round(conf, 2),
        "requer_resposta": bool(data.get("requer_resposta", True)),
        "urgencia": urg if urg in ("baixa", "media", "alta") else "baixa",
        "resumo": str(data.get("resumo", "")).strip()[:300] or f"{contato}: {texto[:160]}",
        "motivo": motivo[:200], "origem": "llm",
    }
    if chave_cache:
        lib_db.cache_put("whatsapp", chave_cache, res, cacheavel=cat in CACHEAVEIS_WA and conf >= CACHE_CONFIANCA_MINIMA)
    return res


def _relevante(cat: dict, cfg: dict) -> tuple[bool, str]:
    c = cat["categoria"]
    if c == "profissional":
        ok = True
    elif c == "pessoal":
        ok = bool(cfg.get("pessoal_vira_pendencia", True))
    elif c == "grupo":
        ok = cat.get("requer_resposta", False) and str(cfg.get("grupos")) in ("mencao", "todos")
    else:
        ok = False
    if not ok:
        return False, f"categoria '{c}' não gera pendência"
    if cfg.get("so_se_requer_resposta", True) and not cat.get("requer_resposta", True):
        return False, "mensagem não pede resposta"
    return True, ""


# ══════════════════════════════════════════════════════════════
#  Pendência tipo "whatsapp"
# ══════════════════════════════════════════════════════════════

def _descricao_tipo(msg: dict, transcricao: Optional[dict]) -> str:
    if msg.get("tipo_msg") == "audio":
        seg = (msg.get("audio") or {}).get("segundos")
        base = f"áudio de {seg}s" if seg else "áudio"
        if transcricao and transcricao.get("texto"):
            return base + f" (transcrito com {transcricao.get('metodo') or 'LLM'})"
        return base + " (não transcrito)"
    return {"texto": "texto"}.get(msg.get("tipo_msg"), f"{msg.get('tipo_msg')} com legenda")


def _descricao_varias(mensagens: list[dict]) -> str:
    """Mensagens seguidas que esperaram juntas na fila: '3 mensagens seguidas (texto, áudio)'."""
    tipos = dict.fromkeys({"audio": "áudio"}.get(m.get("tipo"), m.get("tipo") or "texto") for m in mensagens)
    return f"{len(mensagens)} mensagens seguidas ({', '.join(tipos)})"


def _texto_fallback_wa(msg: dict, cat: dict, conteudo: str) -> str:
    return "\n".join([
        "## Quem mandou e o que quer",
        f"{_contato_legivel(msg)} ({formatar_numero(msg.get('numero'))}) mandou {_descricao_tipo(msg, None)}: "
        + (f"\"{conteudo[:300]}\"" if conteudo else "sem texto legível."),
        "",
        "## Minha leitura",
        "Não consegui avaliar automaticamente — o modelo não respondeu. " + (cat.get("resumo") or ""),
        "",
        "## O que você quer responder?",
        "Responda com o texto que devo enviar, ou **ignorar**.",
    ])


def _pendencia_aberta_do_contato(pend: list[dict], msg: dict, cfg: dict) -> Optional[dict]:
    """Pendência aguardando do mesmo contato criada há menos de `agrupar_minutos`."""
    janela = float(cfg.get("agrupar_minutos", 30) or 0)
    if janela <= 0:
        return None
    limite = core._now() - timedelta(minutes=janela)
    for p in pend:
        if p.get("tipo") != "whatsapp" or p.get("status") != "aguardando":
            continue
        if (p.get("whatsapp") or {}).get("remote_jid") != msg.get("remote_jid"):
            continue
        # janela contada pelo relógio do Alfred (criação/última agrupada), não pelo timestamp do remetente
        ultimo = core._parse_dt(p["whatsapp"].get("atualizado_em") or p.get("criado_em"))
        if ultimo and ultimo >= limite:
            return p
    return None


def _ja_processada(pend: list[dict], message_id: str) -> Optional[dict]:
    for p in pend:
        wa = p.get("whatsapp") or {}
        if wa.get("message_id") == message_id or any(m.get("message_id") == message_id for m in wa.get("mensagens") or []):
            return p
    return None


def _base_id(contato: str, resumo: str) -> str:
    """'Carlos Fatec' + 'Carlos da Fatec pede o orçamento...' → 'carlos-fatec-pede-orcamento-servidor-ate'."""
    nomes = {w for w in re.findall(r"\w+", core._ascii(contato).lower())}
    palavras = [w for w in re.findall(r"\w+", core._ascii(resumo).lower()) if len(w) > 2 and w not in nomes]
    return f"{contato} {' '.join(palavras[:4])}".strip()


def gerar_rascunho(contato: str, conteudo: str, tipo_msg: str, cfg: dict) -> dict:
    """
    Rascunho da resposta de Pedro (botão "Enviar" da notificação) + compromisso citado na mensagem
    (sugestão do botão "Agenda"). → {"rascunho": str, "compromisso": {titulo, data, hora} | None}.
    Sem texto ou com LLM fora do ar devolve rascunho vazio — a notificação abre a caixa em branco.
    """
    if not conteudo.strip():
        return {"rascunho": "", "compromisso": None}
    perfil = cfg.get("_perfil") or {}
    prompt = render_template("whatsapp_rascunho", {
        "data_hoje":        core._now().strftime("%Y-%m-%d (%A)"),
        "perfil_descricao": perfil.get("descricao", ""),
        "tom":              perfil.get("tom", ""),
        "contato":          contato,
        "tipo_msg":         tipo_msg,
        "mensagem":         conteudo[:2000],
    })
    data = ollama_generate_json(prompt, timeout=120, schema=SCHEMA_RASCUNHO_WA,
                                options={**core.OPTS_JSON, "temperature": 0.3, "num_predict": 300,
                                         **core.ctx_do_chat()})
    if not isinstance(data, dict):
        return {"rascunho": "", "compromisso": None}
    comp = data.get("compromisso") if isinstance(data.get("compromisso"), dict) else {}
    data_c = str(comp.get("data") or "").strip()
    hora_c = str(comp.get("hora") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_c):
        data_c = ""
    if not re.fullmatch(r"\d{1,2}:\d{2}", hora_c):
        hora_c = ""
    compromisso = ({"titulo": str(comp.get("titulo") or "").strip()[:80], "data": data_c or None, "hora": hora_c or None}
                   if (data_c or hora_c) else None)
    return {"rascunho": str(data.get("rascunho") or "").strip()[:600], "compromisso": compromisso}


def criar_pendencia_whatsapp(msg: dict, cat: dict, cfg: dict, nome: str, transcricao: Optional[dict] = None,
                             mensagens: Optional[list[dict]] = None) -> dict:
    """`mensagens` = [{message_id, tipo, texto, em}] quando várias mensagens seguidas esperaram juntas na
    fila (msg["texto"] já traz os textos combinados); sem ela, a pendência nasce de uma mensagem só."""
    contato  = _contato_legivel(msg)
    conteudo = (msg.get("texto") or (transcricao or {}).get("texto") or "").strip()
    # id reservado na trava entre processos; LLM + TTS fora dela; o yml só é relido e gravado no fim
    pid = core._reservar_id(_base_id(contato, cat.get("resumo") or conteudo))
    try:
        entry = _montar_pendencia_whatsapp(pid, msg, cat, cfg, nome, transcricao, mensagens, contato, conteudo)
    except BaseException:
        core._soltar_reserva(pid)
        raise
    with core.trava_pendencias():
        data = core._load_pend()
        data["pendencias"].append(entry)
        core._save_pend(data)
    log(f"[whatsapp] Pendência {pid} criada ({cat.get('categoria')}, {cat.get('urgencia')}) — "
        f"md{' + roteiro de voz' if entry['fala'] else ''}")
    return entry


def _montar_pendencia_whatsapp(pid: str, msg: dict, cat: dict, cfg: dict, nome: str, transcricao: Optional[dict],
                               mensagens: Optional[list[dict]], contato: str, conteudo: str) -> dict:
    """Pergunta (.md) + rascunho + áudio → entrada de pendencias.yml (ainda não gravada)."""
    pcfg   = cfg.get("pendencias") or {}
    perfil = cfg.get("_perfil") or {}
    now    = core._now()
    mensagens = mensagens or [{"message_id": msg.get("message_id"), "tipo": msg.get("tipo_msg"),
                               "texto": conteudo, "em": msg.get("timestamp")}]
    desc = _descricao_tipo(msg, transcricao) if len(mensagens) == 1 else _descricao_varias(mensagens)
    titulo = f"{contato}: {(cat.get('resumo') or conteudo or desc)[:90]}"

    prompt = render_template(pcfg.get("template_pergunta", "pergunta_whatsapp"), {
        "data_hoje":        now.strftime("%d/%m/%Y (%A)"),
        "perfil_descricao": perfil.get("descricao", ""),
        "tom":              perfil.get("tom", ""),
        "contato":          contato,
        "numero":           formatar_numero(msg.get("numero")) or msg.get("remote_jid", ""),
        "tipo_msg":         desc,
        "categoria":        cat.get("categoria", ""),
        "urgencia":         cat.get("urgencia", "baixa"),
        "mensagem":         conteudo[:3000] or "(sem texto legível)",
        "historico":        lib_db.historico_contato(
            msg.get("remote_jid"), n=5, excluir=[x.get("message_id") for x in mensagens]) or "nenhuma",
    })
    log(f"[whatsapp] Escrevendo pergunta para '{titulo[:60]}'...")
    texto = ollama_generate(prompt, timeout=180, options=core.ctx_do_chat()) or _texto_fallback_wa(msg, cat, conteudo)
    sugestao = gerar_rascunho(contato, conteudo, desc, cfg)

    pd = core.pend_dir()
    md_path = pd / f"{pid}.md"

    def _fm(v):
        return json.dumps(v, ensure_ascii=False) if isinstance(v, str) else ("null" if v is None else str(v).lower() if isinstance(v, bool) else v)

    md = "\n".join([
        "---",
        f"id: {pid}",
        "status: aguardando",
        "tipo: whatsapp",
        f"title: {_fm(titulo)}",
        f"contato: {_fm(contato)}",
        f"numero: {_fm(msg.get('numero') or '')}",
        f"categoria: {cat.get('categoria')}",
        f"urgencia: {cat.get('urgencia')}",
        f"criado_em: {now.isoformat(timespec='minutes')}",
        f"mensagem_id: {_fm(msg.get('message_id'))}",
        f"mensagem_em: {_fm(msg.get('timestamp'))}",
        f"tags: [pendencia, whatsapp, {nome}]",
        "---",
        "",
        f"# {titulo}",
        "",
        f"> WhatsApp · de **{contato}** ({formatar_numero(msg.get('numero'))}) · {desc} · "
        f"recebido em {msg.get('timestamp', '')[:16]} · aguardando resposta",
        "",
        texto.strip(),
        "",
        "---",
        "",
        "## Mensagem original" if len(mensagens) == 1 else "## Mensagens originais",
        "",
        "\n>\n".join("> " + ((m.get("texto") or "").replace("\n", "\n> ") or "(sem texto)") for m in mensagens),
        "",
    ])
    if transcricao and transcricao.get("erro"):
        md += f"\n*Transcrição falhou: {transcricao['erro']}*\n"
    if sugestao["rascunho"]:
        md += f"\n## Sugestão de resposta\n\n> {sugestao['rascunho']}\n"
    if sugestao["compromisso"]:
        c = sugestao["compromisso"]
        md += f"\n**Compromisso citado:** {c['titulo'] or 'sem título'} — {c['data'] or 'data?'} {c['hora'] or ''}\n"
    md += "\n".join([
        "",
        "## Como responder",
        "",
        "- Notificação no PC: **Enviar** (abre o rascunho para editar) · **Não enviar** · **Agenda** (tarefa + lembrete + Google Agenda) · **Ouvir**",
        f"- WhatsApp: responda **citando** a mensagem de Alfred (ela termina com `ref: {pid}`)",
        f"- Terminal: `services/inbox/run.sh responder-texto {pid} \"texto da resposta\"`",
        f"- Conversa: `services/inbox/run.sh conversa \"responder pendência {pid}: <texto>\"`",
        f"- Para não responder: `responder-texto {pid} ignorar`",
        f"- Áudio da pergunta: `services/inbox/run.sh ouvir {pid}`",
        "",
    ])
    md_path.write_text(md, encoding="utf-8")

    roteiro = ollama_generate(render_template(pcfg.get("template_audio", "audio_pergunta_whatsapp"),
                                              {"texto": texto, "contato": contato}), timeout=120,
                              options=core.ctx_do_chat())
    if not roteiro:
        roteiro = limpar_para_fala(texto)

    entry = {
        "id":        pid,
        "tipo":      "whatsapp",
        "status":    "aguardando",
        "titulo":    titulo,
        "criado_em": now.isoformat(timespec="minutes"),
        "inbox":     nome,
        "evento": {   # mesmas chaves das pendências de e-mail — listagens e tools continuam funcionando
            "organizador": contato, "quem": f"{contato} ({formatar_numero(msg.get('numero'))})",
            "data_inicio": None, "data_fim": None, "local": "", "online": None, "link": "",
            "custo": "desconhecido", "o_que_pedem": "responder a mensagem", "prazo_resposta": None,
            "resumo": cat.get("resumo") or conteudo[:300],
        },
        "email": None,
        "whatsapp": {
            "instancia":   msg.get("instancia"),
            "remote_jid":  msg.get("remote_jid"),
            "numero":      msg.get("numero"),
            "contato":     contato,
            "grupo":       bool(msg.get("grupo")),
            "message_id":  msg.get("message_id"),
            "tipo_msg":    msg.get("tipo_msg"),
            "texto":       conteudo,
            "transcricao": ({k: transcricao.get(k) for k in ("metodo", "duracao_s", "segmentos", "cortado", "erro")}
                            if transcricao else None),
            "categoria":   cat.get("categoria"), "confianca": cat.get("confianca"),
            "urgencia":    cat.get("urgencia"), "motivo": cat.get("motivo"),
            "recebido_em": msg.get("timestamp"),
            "key":         msg.get("key"),   # várias mensagens: a última (a resposta cita ela)
            "quoted_message": {"conversation": mensagens[-1]["texto"][:500]} if mensagens[-1].get("texto") else None,
            "mensagens":   mensagens,
            "atualizado_em": now.isoformat(timespec="minutes"),
            "pergunta_msg_id": None,
            "envio":       None,
            "rascunho":    sugestao["rascunho"] or None,
            "compromisso": sugestao["compromisso"],
            "agenda":      None,
        },
        "arquivos":       {"md": f"pendencias/{md_path.name}", "mp3": None},
        # o .mp3 nasce deste roteiro só na hora de enviar ou de ouvir (core.audio_da_pendencia)
        "fala":           {"texto": roteiro, "voz": pcfg.get("tts_voice", "pt-BR-AntonioNeural")} if roteiro.strip() else None,
        "resposta_links": None,
        "lembrado_em":    None,
        "respondido_em":  None,
        "resposta":       None,
        "resposta_texto": None,
        "nota":           None,
    }
    return entry


def agrupar_mensagem(entry_id: str, msg: dict, conteudo: str, erro: str = "") -> Optional[dict]:
    """
    Nova mensagem do mesmo contato → entra na pendência aberta (sem novo LLM/áudio).
    A decisão de agrupar vem de uma leitura feita antes da transcrição/classificação (segundos a minutos):
    dentro da trava a pendência é conferida de novo. Se outro processo (notificação no PC, CLI, tool) já a
    resolveu, ou ela não é mais do mesmo contato, devolve None sem gravar nada — quem chamou segue o fluxo
    normal (fila ou pendência nova) e a mensagem não se perde. KeyError se o id não existe.
    `erro`: por que o áudio não foi transcrito (vai para whatsapp.mensagens e para o .md).
    """
    with core.trava_pendencias():
        data = core._load_pend()
        entry = next((p for p in data["pendencias"] if p.get("id") == entry_id), None)
        if not entry:
            raise KeyError(entry_id)
        wa = entry.get("whatsapp") or {}
        if entry.get("status") != "aguardando" or wa.get("remote_jid") != msg.get("remote_jid"):
            log(f"[whatsapp] {entry_id} não está mais aguardando ({entry.get('status')}) — mensagem nova não agrupada")
            return None
        wa = entry.setdefault("whatsapp", {})
        nova = {"message_id": msg.get("message_id"), "tipo": msg.get("tipo_msg"), "texto": conteudo,
                "em": msg.get("timestamp")}
        if erro and not conteudo:
            nova["erro"] = erro
        wa.setdefault("mensagens", []).append(nova)
        wa["atualizado_em"] = core._now().isoformat(timespec="minutes")
        wa["texto"] = ((wa.get("texto") or "") + "\n" + conteudo).strip()
        if msg.get("key"):   # a resposta cita a última mensagem: key e quoted_message sempre da mesma
            wa["key"] = msg["key"]
            wa["quoted_message"] = {"conversation": conteudo[:500]} if conteudo else None
        core._save_pend(data)
    md = core.vault() / ((entry.get("arquivos") or {}).get("md") or "")
    if md.is_file():
        txt = md.read_text(encoding="utf-8").rstrip()
        if "## Mensagens posteriores" not in txt:
            txt += "\n\n## Mensagens posteriores\n"
        sem_texto = f"áudio não transcrito: {erro}" if erro else "(sem texto)"
        txt += f"\n- {msg.get('timestamp', '')[:16]} ({msg.get('tipo_msg')}): {conteudo or sem_texto}"
        md.write_text(txt + "\n", encoding="utf-8")
    log(f"[whatsapp] Mensagem agrupada na pendência {entry_id}")
    return entry


# ══════════════════════════════════════════════════════════════
#  Fila de espera — só resume e avisa depois de `atraso_minutos` sem resposta de Pedro
# ══════════════════════════════════════════════════════════════
#
# Com a instância no número de Pedro, toda conversa passa pelo Alfred — inclusive as que ele está
# respondendo em tempo real. Receber (transcrever + classificar) é barato e acontece na hora; a parte
# cara e barulhenta (pergunta + rascunho no LLM, TTS, notificação no PC, aviso pelo WhatsApp) espera
# na fila. Uma mensagem fromMe na mesma conversa cancela o item (registrar_resposta_direta guarda só o
# rascunho que o Alfred proporia, sem .md/áudio/aviso); mensagens seguidas do contato
# entram no mesmo item (o prazo continua contado da primeira). Quem dispara ao vencer é a thread de
# fundo da API (main.py, a cada ~60 s) ou `inbox-assistant.py whatsapp processar-fila`.

_URGENCIAS = ("baixa", "media", "alta")


def _atraso_minutos(cfg: dict) -> float:
    try:
        return max(0.0, float(cfg.get("atraso_minutos", 0) or 0))
    except (TypeError, ValueError):
        log(f"[whatsapp] atraso_minutos inválido ({cfg.get('atraso_minutos')!r}) — sem espera")
        return 0.0


def _load_fila() -> list[dict]:
    if not FILA_FILE.exists():
        return []
    try:
        data = json.loads(FILA_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[whatsapp] {FILA_FILE.name} ilegível ({e}) — fila vazia")
        return []
    itens = data.get("itens") if isinstance(data, dict) else None
    return itens if isinstance(itens, list) else []


def _save_fila(itens: list[dict]) -> None:
    FILA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILA_FILE.with_suffix(".tmp")   # a thread da API e o CLI leem o arquivo: nunca meio escrito
    tmp.write_text(json.dumps({"itens": itens}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(FILA_FILE)


def enfileirar_mensagem(msg: dict, cat: dict, transcricao: Optional[dict], conteudo: str, nome: str,
                        atraso: float, opcoes: dict) -> tuple[dict, bool]:
    """
    Mensagem relevante → item da fila do contato (remote_jid). Já existe item → a mensagem entra nele
    e o prazo não muda. `opcoes` = {enviar, notificar, notificar_desktop} do webhook, usadas quando o
    prazo vencer. → (item, juntou).
    """
    now = core._now()
    m = dict(msg)
    if m.get("audio"):   # já transcrito: a fila não guarda o base64 nem a mediaKey
        m["audio"] = {k: v for k, v in m["audio"].items() if k not in ("base64", "mensagem")}
    parte = {"msg": m, "conteudo": conteudo, "cat": cat, "transcricao": transcricao,
             "recebido_em": now.isoformat(timespec="seconds")}
    with core.trava_arquivo(FILA_FILE):   # API e CLI (simular / processar-fila) mexem na mesma fila
        fila = _load_fila()
        item = next((i for i in fila if i.get("remote_jid") == msg.get("remote_jid")), None)
        if item:
            item["mensagens"].append(parte)
        else:
            item = {"id": msg.get("message_id") or f"{msg.get('remote_jid')}-{now:%H%M%S}", "cfg": nome,
                    "remote_jid": msg.get("remote_jid"), "contato": _contato_legivel(msg),
                    "criado_em": now.isoformat(timespec="seconds"),
                    "prazo": (now + timedelta(minutes=atraso)).isoformat(timespec="seconds"),
                    **opcoes, "tentativas": 0, "mensagens": [parte]}
            fila.append(item)
        _save_fila(fila)
    log(f"[whatsapp] {item['contato']} na fila até {item['prazo'][11:16]} ({len(item['mensagens'])} mensagem(ns))")
    return item, len(item["mensagens"]) > 1


def retirar_da_fila(remote_jid: str) -> list[dict]:
    """Pedro escreveu na conversa antes do prazo → os itens do contato saem da fila. → itens retirados."""
    with core.trava_arquivo(FILA_FILE):
        fila = _load_fila()
        fora = [i for i in fila if i.get("remote_jid") == remote_jid]
        if not fora:
            return []
        _save_fila([i for i in fila if i.get("remote_jid") != remote_jid])
    n = sum(len(i.get("mensagens") or []) for i in fora)
    log(f"[whatsapp] Pedro respondeu {fora[0].get('contato')} antes do prazo — {n} mensagem(ns) saíram da fila")
    return fora


def _ids_do_item(item: dict) -> list[str]:
    return [p["msg"].get("message_id") for p in item.get("mensagens") or []]


def cancelar_fila(remote_jid: str) -> list[str]:
    """Como retirar_da_fila, mas devolve só os message_ids cancelados."""
    return [mid for i in retirar_da_fila(remote_jid) for mid in _ids_do_item(i)]


def listar_fila() -> list[dict]:
    """Resumo da fila (CLI `whatsapp fila`, /whatsapp/status)."""
    agora = core._now()
    out = []
    for i in _load_fila():
        prazo = core._parse_dt(i.get("prazo"))
        out.append({
            "id": i.get("id"), "contato": i.get("contato"), "remote_jid": i.get("remote_jid"),
            "mensagens": len(i.get("mensagens") or []), "prazo": i.get("prazo"),
            "faltam_min": round(max(0.0, (prazo - agora).total_seconds() / 60), 1) if prazo else None,
            "textos": [(p.get("conteudo") or f"({p['msg'].get('tipo_msg')})")[:80] for p in i.get("mensagens") or []],
        })
    return out


def _combinar(item: dict) -> tuple[dict, dict, Optional[dict], list[dict]]:
    """Item da fila → (msg, cat, transcricao, mensagens) para criar_pendencia_whatsapp. Várias mensagens viram
    um texto só; a resposta de Pedro cita a última; urgência = a maior; resumos juntos."""
    partes = item["mensagens"]
    mensagens = [{"message_id": p["msg"].get("message_id"), "tipo": p["msg"].get("tipo_msg"),
                  "texto": p.get("conteudo") or "", "em": p["msg"].get("timestamp")} for p in partes]
    primeira = partes[0]
    if len(partes) == 1:
        return primeira["msg"], primeira["cat"], primeira.get("transcricao"), mensagens
    msg = dict(primeira["msg"], texto="\n".join(m["texto"] for m in mensagens if m["texto"]),
               key=partes[-1]["msg"].get("key"))
    cat = dict(primeira["cat"])
    cat["urgencia"] = max((p["cat"].get("urgencia") or "baixa" for p in partes),
                          key=lambda u: _URGENCIAS.index(u) if u in _URGENCIAS else 0)
    cat["resumo"] = " · ".join(dict.fromkeys(p["cat"]["resumo"] for p in partes if p["cat"].get("resumo")))[:300]
    transcricao = next((p["transcricao"] for p in partes if p.get("transcricao")), None)
    return msg, cat, transcricao, mensagens


def _criar_do_item(item: dict, enviar: Optional[bool]) -> dict:
    msg, cat, transcricao, mensagens = _combinar(item)
    res = {"acao": "pendencia_criada", "fila": {"id": item.get("id"), "mensagens": len(mensagens)},
           "motivo": f"sem resposta de Pedro desde {item.get('criado_em', '')[11:16]}",
           "mensagem": _resumo_msg(msg), "classificacao": cat, "pendencia": None, "notificacao": None, "envio": None}
    pend = core._load_pend()["pendencias"]
    if any(m["message_id"] and _ja_processada(pend, m["message_id"]) for m in mensagens):
        res.update(acao="duplicada", motivo="mensagem já virou pendência")   # processo morreu entre criar e tirar da fila
        return res
    nome, cfg = carregar_cfg(item.get("cfg"))
    enviar = bool(item.get("enviar", True)) if enviar is None else enviar
    entry = criar_pendencia_whatsapp(msg, cat, cfg, nome, transcricao, mensagens=mensagens)
    res.update(pendencia=entry, notificacao=mensagem_para_pedro(entry))
    if item.get("notificar_desktop", True):
        core.notificar_pendencia(entry)
    if item.get("notificar") and enviar:
        res["envio"] = notificar_pedro(entry["id"], enviar=True)
    return res


def _conversa_ocupada() -> bool:
    """Turno da Conversa com o Alfred (conversa.py) na fila ou rodando neste processo: a GPU é do chat."""
    conversa = sys.modules.get("conversa")
    return bool(conversa and conversa.ocupado())


@_serializado
def processar_fila(todos: bool = False, enviar: Optional[bool] = None) -> list[dict]:
    """
    Itens com prazo vencido (todos=True: a fila inteira) → pendência + áudio + avisos, com as opções que o
    webhook pediu (enviar=False sobrepõe: nunca chama a Evolution). O item só sai da fila depois da pendência
    gravada; se der erro, tenta de novo na próxima volta (até 3 vezes). No fim, disparar_lembretes().
    Com turno da Conversa em andamento, o próximo item espera a volta seguinte (não é descartado).
    """
    agora = core._now()
    resultados = []
    for item in [i for i in _load_fila() if todos or (core._parse_dt(i.get("prazo")) or agora) <= agora]:
        if _conversa_ocupada():
            log("[whatsapp] fila: conversa com o Alfred em andamento — o próximo item espera")
            break
        sai = True
        try:
            resultados.append(_criar_do_item(item, enviar))
        except Exception as e:
            item["tentativas"] = int(item.get("tentativas") or 0) + 1
            log(f"[whatsapp] fila: erro criando a pendência de {item.get('contato')} "
                f"(tentativa {item['tentativas']}/3): {e}")
            sai = item["tentativas"] >= 3
            if sai:
                resultados.append({"acao": "erro", "fila": {"id": item.get("id")}, "motivo": str(e)[:200]})
        # a fila é relida na trava: outro processo pode ter mexido nela enquanto o LLM trabalhava
        with core.trava_arquivo(FILA_FILE):
            fila = _load_fila()
            atual = next((i for i in fila if i.get("id") == item.get("id")
                          and i.get("remote_jid") == item.get("remote_jid")), None)
            if atual is not None:
                if not sai:
                    atual["tentativas"] = item["tentativas"]
                else:
                    # mensagens que chegaram depois de o item ser lido continuam esperando
                    resto = [p for p in atual.get("mensagens") or []
                             if p["msg"].get("message_id") not in set(_ids_do_item(item))]
                    if resto:
                        atual["mensagens"] = resto
                    else:
                        fila.remove(atual)
                _save_fila(fila)
    try:   # mesma volta da thread da API: lembretes do "Agendar" (painel / notificação) que venceram
        disparar_lembretes(agora)
    except Exception as e:
        log(f"[whatsapp] lembretes: erro ({e})")
    return resultados


# ══════════════════════════════════════════════════════════════
#  Mensagem para Pedro (resumo + instrução) e resposta de Pedro
# ══════════════════════════════════════════════════════════════

def mensagem_para_pedro(entry: dict, agrupada: Optional[dict] = None) -> dict:
    """Texto (markdown do WhatsApp) que Alfred manda a Pedro. Termina com 'ref: <id>' — é o que liga a citação à pendência."""
    wa = entry.get("whatsapp") or {}
    ev = entry.get("evento") or {}
    contato = wa.get("contato") or entry.get("titulo")
    numero = formatar_numero(wa.get("numero"))
    if agrupada:
        texto = (f"🎩 *{contato}* mandou mais uma mensagem ({agrupada.get('tipo_msg')}):\n"
                 f"“{(agrupada.get('texto') or '(sem texto)')[:600]}”\n\n"
                 f"Mesma pendência de antes — responda citando esta mensagem ou a anterior.\n"
                 f"ref: {entry['id']}")
    else:
        tipo = wa.get("tipo_msg")
        desc = "áudio transcrito" if tipo == "audio" and wa.get("texto") else ("áudio (não transcrito)" if tipo == "audio" else tipo)
        if len(wa.get("mensagens") or []) > 1:
            desc = _descricao_varias(wa["mensagens"])
        urg = {"alta": " · ⚠️ urgente", "media": "", "baixa": ""}.get(wa.get("urgencia") or "baixa", "")
        linhas = [f"🎩 *Alfred* — mensagem de *{contato}* no WhatsApp ({desc}{urg})"]
        if numero:
            linhas.append(f"📱 {numero}")
        linhas.append("")
        if wa.get("texto"):
            linhas.append(f"“{wa['texto'][:900]}”")
            linhas.append("")
        if ev.get("resumo"):
            linhas.append(ev["resumo"][:400])
            linhas.append("")
        linhas += ["*O que você quer responder?*",
                   "Responda *citando esta mensagem* (deslize para responder) e eu envio ao contato. "
                   "Para não responder, cite com *ignorar*.",
                   "",
                   f"ref: {entry['id']}"]
        texto = "\n".join(linhas)
    pedro = numero_pedro()
    return {
        "numero":       pedro or None,
        "texto":        texto,
        "pendencia_id": entry["id"],
        "audio_url":    f"/pendencias/{entry['id']}/audio" if (core.tem_audio(entry) and not agrupada) else None,
    }


@_serializado
def notificar_pedro(pid: str, agrupada: Optional[dict] = None, enviar: bool = True) -> dict:
    """Envia a Pedro (via Evolution API) o resumo + áudio da pergunta e guarda o id da mensagem enviada."""
    data = core._load_pend()
    entry = next((p for p in data["pendencias"] if p.get("id") == pid), None)
    if not entry:
        raise KeyError(f"Pendência não encontrada: {pid}")
    notif = mensagem_para_pedro(entry, agrupada)
    res = {"notificacao": notif, "envio_texto": None, "envio_audio": None}
    if not enviar:
        return res
    destino = notif["numero"] or (entry.get("whatsapp") or {}).get("dono")
    if not destino:
        log("[whatsapp] WHATSAPP_NUMERO_PEDRO vazio — não sei para quem mandar o resumo")
        res["erro"] = "WHATSAPP_NUMERO_PEDRO não definido"
        return res
    inst = (entry.get("whatsapp") or {}).get("instancia")   # a sessão que recebeu a mensagem é a que avisa
    r = enviar_texto(destino, notif["texto"], inst=inst, aviso=False)   # rede fora da trava
    core.avisar_pendencia(entry, agrupada)   # no feed vai a pendência (com o link), não a cópia do texto do WhatsApp
    res["envio_texto"] = r
    if r.get("ok") and r.get("message_id") and not agrupada:
        with core.trava_pendencias():
            data = core._load_pend()
            atual = next((p for p in data["pendencias"] if p.get("id") == pid), None)
            if atual is not None:
                atual.setdefault("whatsapp", {})["pergunta_msg_id"] = r["message_id"]
                atual["lembrado_em"] = core._now().isoformat(timespec="minutes")
                core._save_pend(data)
    mp3 = core.audio_da_pendencia(entry) if notif.get("audio_url") else None   # gerado agora, do roteiro
    if mp3:
        res["envio_audio"] = enviar_audio(destino, mp3, inst=inst)
        if mp3.parent == core.FALA_DIR:   # já está no WhatsApp de Pedro; para ouvir de novo, gera na hora
            mp3.unlink(missing_ok=True)
    return res


def _anexar_resposta_md(entry: dict, rotulo: str, texto: Optional[str], now: datetime, origem: str, extra: str = ""):
    md_rel = (entry.get("arquivos") or {}).get("md")
    if not md_rel:
        return
    md_path = core.vault() / md_rel
    if not md_path.exists():
        return
    txt = md_path.read_text(encoding="utf-8")
    txt = re.sub(r"^status: aguardando$", f"status: {entry['status']}", txt, count=1, flags=re.M)
    txt = txt.rstrip() + f"\n\n## Resposta\n\n**{rotulo}** — {now.strftime('%d/%m/%Y %H:%M')}" + (f" ({origem})" if origem else "")
    if texto:
        txt += f"\n\n> {texto.replace(chr(10), chr(10) + '> ')}"
    if extra:
        txt += f"\n\n{extra}"
    md_path.write_text(txt + "\n", encoding="utf-8")


@_serializado
def responder_texto(pid: str, texto: str, enviar: bool = True, origem: str = "", nota: str = "") -> dict:
    """
    Registra a resposta de Pedro (texto livre) e a envia ao contato pela Evolution API.
    'ignorar' fecha a pendência sem enviar. Falha no envio devolve a pendência para
    'aguardando' com o texto guardado em resposta_texto (basta repetir o comando).
    Levanta KeyError (id) / ValueError (tipo, texto vazio) / core.PendenciaJaResolvida (status ≠ aguardando,
    subclasse de ValueError com a mesma mensagem de antes).

    Reservar → enviar → concluir: a pendência sai de 'aguardando' numa transação curta na trava entre
    processos ANTES do envio; o envio (rede, pode demorar) fica fora da trava; o resultado é gravado numa
    segunda transação. Dois cliques / duas origens ao mesmo tempo (painel, notificação, CLI, WhatsApp) só
    enviam uma vez: o segundo encontra o status já mudado e levanta PendenciaJaResolvida.
    """
    texto = (texto or "").strip()
    now = core._now()
    with core.trava_pendencias():
        data = core._load_pend()
        entry = next((p for p in data["pendencias"] if p.get("id") == pid), None)
        if not entry:
            raise KeyError(f"Pendência não encontrada: {pid}")
        if entry.get("tipo") != "whatsapp":
            raise ValueError(f"{pid} não é pendência de WhatsApp — use responder {pid} vou|nao-vou")
        if entry.get("status") != "aguardando":
            raise core.PendenciaJaResolvida(entry)
        if not texto:
            raise ValueError("Texto da resposta vazio")

        wa = entry.setdefault("whatsapp", {})
        entry["respondido_em"] = now.isoformat(timespec="minutes")
        entry["nota"] = (nota or "").strip() or None
        entry["respondido_via"] = core._via(origem)
        ignorar = bool(IGNORAR_RE.match(texto))
        if ignorar:
            entry["status"] = "ignorada"
            entry["resposta"] = "ignorada"
        else:
            entry["resposta_texto"] = texto
            entry["resposta"] = "texto"
            entry["status"] = "respondida"
            wa["envio"] = {"solicitado_em": now.isoformat(timespec="minutes"), "enviar": bool(enviar),
                           "ok": None, "simulado": None, "message_id": None, "erro": None}
        core._save_pend(data)   # salva ANTES de enviar: o eco fromMe do próprio envio não deve reabrir nada

    if ignorar:
        _anexar_resposta_md(entry, "IGNORADA", None, now, origem)
        log(f"[whatsapp] {pid} → ignorada")
        return entry

    if enviar:
        destino = wa.get("remote_jid") if wa.get("grupo") else (wa.get("numero") or jid_para_numero(wa.get("remote_jid")))
        quoted = {"key": wa.get("key"), "message": wa.get("quoted_message") or {}} if wa.get("key") else None
        r = enviar_texto(destino, texto, quoted=quoted, inst=wa.get("instancia"))
        resultado = {
            "ok": bool(r.get("ok")), "simulado": bool(r.get("simulado")), "message_id": r.get("message_id"),
            "erro": None if r.get("ok") else (r.get("erro") or str(r.get("resposta"))[:200]),
            "destino": destino,
        }
        with core.trava_pendencias():
            data = core._load_pend()
            atual = next((p for p in data["pendencias"] if p.get("id") == pid), None)
            if atual is not None:   # relida: preserva o que outro processo gravou entre as transações
                entry = atual
            wa = entry.setdefault("whatsapp", {})
            wa["envio"] = {**(wa.get("envio") or {}), **resultado}
            if not r.get("ok"):
                entry["status"] = "aguardando"    # resposta guardada; repetir o comando reenvia
                entry["resposta"] = None
                entry["respondido_via"] = None
            if atual is not None:
                core._save_pend(data)
        if not r.get("ok"):
            log(f"[whatsapp] {pid}: envio FALHOU ({wa['envio']['erro']}) — pendência continua aguardando")
            return entry
        rot = "RESPONDIDA (envio simulado)" if r.get("simulado") else "RESPONDIDA (enviada pelo WhatsApp)"
    else:
        rot = "RESPONDIDA (sem envio)"
    _anexar_resposta_md(entry, rot, texto, now, origem)
    log(f"[whatsapp] {pid} → respondida" + (" (simulado)" if (wa.get("envio") or {}).get("simulado") else ""))
    return entry


# ══════════════════════════════════════════════════════════════
#  Agenda ("anotar para mais tarde") — botão Agenda da notificação no PC
# ══════════════════════════════════════════════════════════════

_DIAS_SEMANA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def interpretar_quando(texto: str, agora: Optional[datetime] = None) -> Optional[tuple[datetime, bool]]:
    """
    '20/10 14:00' · '20/10/2026' · '2026-10-20 09:30' · 'amanhã 10h' · 'hoje 18:00' · 'terça 15h' · '16:00'
    · 'dia 30 às 14h' → (datetime local, tem_hora). None se não entender ou se a data não existir (31/02,
    12/13, 29/02 fora de ano bissexto). Datas sem ano que já passaram vão para o ano seguinte; 'dia N' é o
    próximo dia N (este mês ou o seguinte que o tenha); só a hora, já passada hoje, vai para amanhã.
    """
    agora = agora or core._now()
    t = core._ascii((texto or "").strip().lower())
    if not t:
        return None
    hora = re.search(r"\b(\d{1,2})(?::(\d{2})|h(\d{2})?)\b", t)
    tem_hora = bool(hora)
    hh, mm = (int(hora.group(1)), int(hora.group(2) or hora.group(3) or 0)) if hora else (9, 0)
    if hh > 23 or mm > 59:
        return None
    t_sem_hora = t.replace(hora.group(0), " ") if hora else t
    dia = None
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t_sem_hora):
        dia = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    elif m := re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t_sem_hora):
        ano = int(m.group(3)) if m.group(3) else agora.year
        ano += 2000 if ano < 100 else 0
        dia = (ano, int(m.group(2)), int(m.group(1)))
        try:
            if not m.group(3) and datetime(*dia).date() < agora.date():
                dia = (ano + 1, dia[1], dia[2])
        except ValueError:   # 31/02, 12/13, 29/02 em ano não bissexto...
            return None
    elif m := re.search(r"\bdia\s+(\d{1,2})\b", t_sem_hora):
        n = int(m.group(1))
        if not 1 <= n <= 31:
            return None
        a, mes = agora.year, agora.month
        for _ in range(13):   # este mês ou o próximo que tenha o dia N (dia 31 pula meses de 30)
            try:
                cand = datetime(a, mes, n, hh, mm, tzinfo=agora.tzinfo)
                if cand.date() > agora.date() or (cand.date() == agora.date() and (not tem_hora or cand > agora)):
                    dia = (a, mes, n)
                    break
            except ValueError:
                pass
            a, mes = (a + 1, 1) if mes == 12 else (a, mes + 1)
    elif "depois de amanha" in t_sem_hora:
        d = agora.date() + timedelta(days=2); dia = (d.year, d.month, d.day)
    elif "amanha" in t_sem_hora:
        d = agora.date() + timedelta(days=1); dia = (d.year, d.month, d.day)
    elif "hoje" in t_sem_hora:
        dia = (agora.year, agora.month, agora.day)
    elif tem_hora and not re.search(r"[a-z]{4,}", t_sem_hora):   # só a hora: hoje, ou amanhã se já passou
        d = agora.date() + timedelta(days=1 if (hh, mm) <= (agora.hour, agora.minute) else 0)
        dia = (d.year, d.month, d.day)
    else:
        for i, nome in enumerate(_DIAS_SEMANA):
            if core._ascii(nome) in t_sem_hora:
                delta = (i - agora.weekday()) % 7 or 7
                d = agora.date() + timedelta(days=delta); dia = (d.year, d.month, d.day)
                break
    if not dia:
        return None
    try:
        return datetime(*dia, hh, mm, tzinfo=agora.tzinfo), tem_hora
    except ValueError:
        return None


def sugestao_quando(entry: dict) -> str:
    """Texto inicial da caixa "Quando?": o compromisso extraído da mensagem ou amanhã às 09:00."""
    comp = (entry.get("whatsapp") or {}).get("compromisso") or {}
    if comp.get("data"):
        a, m, d = comp["data"].split("-")
        return f"{d}/{m}/{a}" + (f" {comp['hora']}" if comp.get("hora") else "")
    if comp.get("hora"):
        return f"hoje {comp['hora']}"
    return (core._now() + timedelta(days=1)).strftime("%d/%m/%Y 09:00")


def google_agenda_url(titulo: str, inicio: datetime, tem_hora: bool, detalhes: str = "", minutos: int = 60) -> str:
    """Link "criar evento" do Google Agenda já preenchido — abre no navegador e Pedro só clica Salvar
    (sem credencial nem API; nada vai para o Google até ele salvar)."""
    from urllib.parse import urlencode
    if tem_hora:
        fim = inicio + timedelta(minutes=minutos)
        datas = f"{inicio:%Y%m%dT%H%M%S}/{fim:%Y%m%dT%H%M%S}"
    else:
        datas = f"{inicio:%Y%m%d}/{inicio + timedelta(days=1):%Y%m%d}"
    q = {"action": "TEMPLATE", "text": titulo[:120], "dates": datas, "details": detalhes[:800],
         "ctz": "America/Sao_Paulo"}
    return "https://calendar.google.com/calendar/render?" + urlencode(q)


@_serializado
def agendar_pendencia(pid: str, quando: datetime, tem_hora: bool = True, titulo: str = "") -> dict:
    """
    "Anotar na agenda para mais tarde": cria tarefa em VAULT_ALFRED/tasks.yml (due_date = dia), guarda
    whatsapp.lembrar_em (a notificação volta nesse horário — disparar_lembretes(), na thread da fila da API;
    a pendência continua aguardando) e devolve o link do Google Agenda.
    Reagendar atualiza a tarefa anterior (título/due_date) em vez de criar outra, se ela ainda estiver aberta.
    → {"task_id", "lembrar_em", "google_url", "titulo"}.
    """
    with core.trava_pendencias():   # tarefa em tasks.yml aninhada (ordem pendências → tasks)
        data = core._load_pend()
        entry = next((p for p in data["pendencias"] if p.get("id") == pid), None)
        if not entry:
            raise KeyError(f"Pendência não encontrada: {pid}")
        if entry.get("status") != "aguardando":
            raise core.PendenciaJaResolvida(entry)
        wa = entry.setdefault("whatsapp", {})
        contato = wa.get("contato") or entry.get("titulo")
        titulo = (titulo or ((wa.get("compromisso") or {}).get("titulo")) or f"Responder {contato} (WhatsApp)").strip()
        resumo = (entry.get("evento") or {}).get("resumo") or ""
        detalhes = f"{resumo}\n\nMensagem de {contato}: “{(wa.get('texto') or '')[:400]}”\nPendência Alfred: {pid}"
        titulo_task = f"{titulo}" + (f" — {quando:%H:%M}" if tem_hora else "")
        tid = (wa.get("agenda") or {}).get("task_id")
        if not (tid and _atualizar_tarefa(tid, titulo_task, quando.strftime("%Y-%m-%d"))):   # reagendar: mesma tarefa
            tid = core._add_task(titulo_task, quando.strftime("%Y-%m-%d"),
                                 ["whatsapp", "alfred", core._ascii(contato).lower()[:30]])
        url = google_agenda_url(titulo, quando, tem_hora, detalhes)
        wa["lembrar_em"] = quando.isoformat(timespec="minutes")
        wa["agenda"] = {"task_id": tid, "quando": wa["lembrar_em"], "tem_hora": tem_hora, "titulo": titulo,
                        "criado_em": core._now().isoformat(timespec="minutes")}
        core._save_pend(data)
    _anexar_agenda_md(entry, titulo, quando, tem_hora, tid)
    log(f"[whatsapp] {pid} → agenda: tarefa #{tid}, lembrete {wa['lembrar_em']}")
    return {"task_id": tid, "lembrar_em": wa["lembrar_em"], "google_url": url, "titulo": titulo}


def _atualizar_tarefa(tid, titulo: str, due_date: str) -> bool:
    """Tarefa aberta #tid de tasks.yml → novo título/due_date (mesma trava de core._add_task). False se ela não
    existe mais ou já foi concluída (aí quem chamou cria outra)."""
    import yaml
    f = core.vault() / "tasks.yml"
    with core.trava_arquivo(f):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else None
        except Exception:
            return False
        tasks = data.get("tasks") if isinstance(data, dict) else None
        t = next((t for t in tasks or [] if isinstance(t, dict) and t.get("id") == tid), None)
        if not t or t.get("status") != "open":
            return False
        t["title"], t["due_date"] = titulo, due_date
        core.gravar_atomico(f, yaml.dump(data, allow_unicode=True, sort_keys=False))
    return True


def marcar_lembrete(pid: str, quando_iso: Optional[str] = None) -> bool:
    """
    "Reserva" o lembrete agendado da pendência: dentro da trava, se ela ainda aguarda, tem whatsapp.lembrar_em
    (igual a `quando_iso`, se dado) e ninguém o disparou para esse horário, grava lembrete_disparado_em =
    lembrar_em e devolve True. Assim a thread da API e o aguardador da notificação (que dorme até o horário)
    não mostram o mesmo lembrete duas vezes.
    """
    with core.trava_pendencias():
        data = core._load_pend()
        entry = next((p for p in data["pendencias"] if p.get("id") == pid), None)
        wa = (entry or {}).get("whatsapp") or {}
        lembrar = wa.get("lembrar_em")
        if (not entry or entry.get("status") != "aguardando" or not lembrar
                or (quando_iso and lembrar != quando_iso) or wa.get("lembrete_disparado_em") == lembrar):
            return False
        wa["lembrete_disparado_em"] = lembrar
        core._save_pend(data)
    return True


def disparar_lembretes(agora: Optional[datetime] = None) -> list[str]:
    """Pendências de WhatsApp aguardando com whatsapp.lembrar_em vencido e ainda não lembradas para esse horário
    → a notificação com botões volta (core.notificar_pendencia). Chamada por processar_fila (thread da API)."""
    agora = agora or core._now()
    vencidas = []
    for p in core.listar_pendencias("aguardando"):
        wa = p.get("whatsapp") or {}
        lembrar = wa.get("lembrar_em")
        if p.get("tipo") != "whatsapp" or not lembrar or wa.get("lembrete_disparado_em") == lembrar:
            continue
        dt = core._parse_dt(lembrar)
        if dt and dt <= agora and marcar_lembrete(p["id"], lembrar):
            vencidas.append(p)
    for p in vencidas:
        log(f"[whatsapp] {p['id']}: lembrete agendado para {p['whatsapp']['lembrar_em']} — notificação de volta")
        try:
            core.notificar_pendencia(core.obter_pendencia(p["id"]) or p)
        except Exception as e:
            log(f"[whatsapp] {p['id']}: lembrete não notificado ({e})")
    return [p["id"] for p in vencidas]


def _anexar_agenda_md(entry: dict, titulo: str, quando: datetime, tem_hora: bool, tid) -> None:
    md = core.vault() / ((entry.get("arquivos") or {}).get("md") or "")
    if md.is_file():
        q = quando.strftime("%d/%m/%Y %H:%M" if tem_hora else "%d/%m/%Y")
        md.write_text(md.read_text(encoding="utf-8").rstrip()
                      + f"\n\n## Agenda\n\n**{titulo}** — {q} · tarefa #{tid} · lembrete no PC nesse horário\n",
                      encoding="utf-8")


# ══════════════════════════════════════════════════════════════
#  Entrada: webhook → ação
# ══════════════════════════════════════════════════════════════

def _eh_pedro(msg: dict) -> bool:
    """Mensagem escrita por Pedro? Vale para instância separada (Pedro → número do Alfred) e para a
    instância no próprio número de Pedro (fromMe, conversa consigo mesmo)."""
    if msg.get("grupo"):
        return False
    pedro = numero_pedro()
    if pedro and msg.get("numero") == pedro:
        return True
    if msg.get("from_me"):
        if msg.get("dono") and msg.get("numero") == msg["dono"]:
            return True            # conversa "você" (Pedro falando com o próprio número)
        if _ref_citada(msg):
            return True            # qualquer reply de Pedro citando uma pergunta do Alfred
    return False


def _ref_citada(msg: dict) -> Optional[str]:
    """Id da pendência citada na resposta (pelo 'ref: <id>' do texto citado ou pelo id da mensagem citada)."""
    cit = msg.get("citacao")
    if not cit:
        return None
    m = REF_RE.search(cit.get("texto") or "")
    if m:
        pid = m.group(1).lower()
        if core.obter_pendencia(pid):
            return pid
    sid = cit.get("stanza_id")
    if sid:
        for p in core.listar_pendencias("todas"):
            if (p.get("whatsapp") or {}).get("pergunta_msg_id") == sid:
                return p["id"]
    return None


def _pendencia_aguardando_do_jid(remote_jid: str) -> Optional[dict]:
    for p in core.listar_pendencias("aguardando"):
        if p.get("tipo") == "whatsapp" and (p.get("whatsapp") or {}).get("remote_jid") == remote_jid:
            return p
    return None


def _fechar_manual(pid: str, texto: str, msg: dict) -> Optional[dict]:
    """
    Pedro respondeu o contato direto do celular → fecha a pendência sem enviar nada. O rascunho do Alfred
    (whatsapp.rascunho) fica e a resposta real vai para resposta_texto — o painel mostra os dois lado a lado.
    None se a pendência já saiu de 'aguardando' por outro caminho no meio tempo.
    """
    now = core._now()
    with core.trava_pendencias():
        data = core._load_pend()
        entry = next((p for p in data["pendencias"] if p.get("id") == pid), None)
        if not entry or entry.get("status") != "aguardando":
            return None
        entry["status"] = "respondida_manual"
        entry["resposta"] = "texto"
        entry["resposta_texto"] = texto or f"({msg.get('tipo_msg')} enviado direto pelo celular)"
        entry["respondido_em"] = now.isoformat(timespec="minutes")
        entry["respondido_via"] = "celular"
        entry.setdefault("whatsapp", {})["envio"] = {"manual": True, "message_id": msg.get("message_id")}
        core._save_pend(data)
    _anexar_resposta_md(entry, "RESPONDIDA DIRETO NO CELULAR", texto, now, "fromMe")
    log(f"[whatsapp] {pid} → respondida_manual (Pedro respondeu direto)")
    return entry


def registrar_resposta_direta(item_fila: dict, msg_pedro: dict) -> dict:
    """
    Pedro respondeu pelo celular enquanto a conversa esperava `atraso_minutos`: nada de pendência aguardando,
    .md, áudio, notificação ou envio — só um registro `respondida_manual` com a mensagem do contato, o
    rascunho que o Alfred teria proposto (mesma lógica de whatsapp_rascunho.md, gerado agora) e a resposta
    que Pedro deu. LLM fora do ar → rascunho None e o registro sai assim mesmo. → entrada gravada.
    Só chega aqui o que entrou na fila (já passou por grupos / contatos_ignorar / confiança / relevância).
    """
    msg, cat, transcricao, mensagens = _combinar(item_fila)
    nome, cfg = carregar_cfg(item_fila.get("cfg"))
    contato = _contato_legivel(msg)
    conteudo = (msg.get("texto") or (transcricao or {}).get("texto") or "").strip()
    desc = _descricao_tipo(msg, transcricao) if len(mensagens) == 1 else _descricao_varias(mensagens)
    try:   # LLM fora da trava (até ~2 min no pior caso)
        sugestao = gerar_rascunho(contato, conteudo, desc, cfg)
    except Exception as e:
        log(f"[whatsapp] rascunho da resposta direta falhou: {e}")
        sugestao = {"rascunho": "", "compromisso": None}
    now = core._now()
    criado = core._parse_dt(item_fila.get("criado_em"))
    entry = {
        "id":        None,   # calculado dentro da trava
        "tipo":      "whatsapp",
        "status":    "respondida_manual",
        "titulo":    f"{contato}: {(cat.get('resumo') or conteudo or desc)[:90]}",
        "criado_em": criado.isoformat(timespec="minutes") if criado else now.isoformat(timespec="minutes"),
        "inbox":     nome,
        "evento": {
            "organizador": contato, "quem": f"{contato} ({formatar_numero(msg.get('numero'))})",
            "data_inicio": None, "data_fim": None, "local": "", "online": None, "link": "",
            "custo": "desconhecido", "o_que_pedem": "responder a mensagem", "prazo_resposta": None,
            "resumo": cat.get("resumo") or conteudo[:300],
        },
        "email": None,
        "whatsapp": {
            "instancia":   msg.get("instancia"),
            "remote_jid":  msg.get("remote_jid"),
            "numero":      msg.get("numero"),
            "contato":     contato,
            "grupo":       bool(msg.get("grupo")),
            "message_id":  msg.get("message_id"),
            "tipo_msg":    msg.get("tipo_msg"),
            "texto":       conteudo,
            "transcricao": ({k: transcricao.get(k) for k in ("metodo", "duracao_s", "segmentos", "cortado", "erro")}
                            if transcricao else None),
            "categoria":   cat.get("categoria"), "confianca": cat.get("confianca"),
            "urgencia":    cat.get("urgencia"), "motivo": cat.get("motivo"),
            "recebido_em": msg.get("timestamp"),
            "key":         msg.get("key"),
            "quoted_message": {"conversation": mensagens[-1]["texto"][:500]} if mensagens[-1].get("texto") else None,
            "mensagens":   mensagens,
            "atualizado_em": now.isoformat(timespec="minutes"),
            "pergunta_msg_id": None,
            "envio":       {"manual": True, "message_id": msg_pedro.get("message_id")},
            "rascunho":    sugestao.get("rascunho") or None,
            "compromisso": sugestao.get("compromisso"),
            "agenda":      None,
            "respondida_na_espera": True,   # a pendência nunca chegou a existir
        },
        "arquivos":       {"md": None, "mp3": None},
        "resposta_links": None,
        "lembrado_em":    None,
        "respondido_em":  now.isoformat(timespec="minutes"),
        "resposta":       "texto",
        "resposta_texto": msg_pedro.get("texto") or f"({msg_pedro.get('tipo_msg')} enviado direto pelo celular)",
        "respondido_via": "celular",
        "nota":           None,
    }
    with core.trava_pendencias():
        data = core._load_pend()
        dup = next((d for d in (_ja_processada(data["pendencias"], m["message_id"])
                                for m in mensagens if m.get("message_id")) if d), None)
        if dup:   # outro processo já transformou estas mensagens em pendência
            log(f"[whatsapp] mensagens de {contato} já estão na pendência {dup.get('id')} — sem novo registro")
            return dup
        entry["id"] = core._novo_id(_base_id(contato, cat.get("resumo") or conteudo), core._ids_ocupados(data))
        data["pendencias"].append(entry)
        core._save_pend(data)
    log(f"[whatsapp] {entry['id']} → respondida_manual na espera (Pedro respondeu {contato} pelo celular; "
        f"rascunho {'registrado' if entry['whatsapp']['rascunho'] else 'indisponível'})")
    return entry


def _resumo_msg(msg: dict) -> dict:
    return {k: msg.get(k) for k in ("message_id", "remote_jid", "numero", "nome", "from_me", "grupo",
                                    "tipo_msg", "timestamp")} | {"texto": (msg.get("texto") or "")[:200]}


# ══════════════════════════════════════════════════════════════
#  Agenda automática + conversa "você" (Pedro anotando para si)
# ══════════════════════════════════════════════════════════════

_MARCAS_ALFRED = re.compile(r"(🎩|^\s*[✔✖⏰📅]|\bref:\s*[0-9]{8}-)", re.M)


def _conversa_comigo(msg: dict) -> bool:
    """Pedro escrevendo para si mesmo (conversa "você") ou, numa instância separada, para o número do Alfred."""
    if msg.get("grupo"):
        return False
    if msg.get("from_me"):
        return bool(msg.get("dono")) and msg.get("numero") == msg.get("dono")
    pedro = numero_pedro()
    return bool(pedro) and msg.get("numero") == pedro


# sendText leva no máximo ~60 s (timeout do lib_whatsapp); com folga, e solicitado_em tem precisão de minuto
ENVIO_EM_CURSO_MAX = timedelta(minutes=5)


def _envio_em_curso(msg: dict) -> bool:
    """
    fromMe que é o próprio envio de responder_texto ainda sem message_id gravado: a T1 já marcou a pendência
    do mesmo contato como 'respondida' com envio.ok=None (envio em curso, fora da trava) e o texto é o mesmo.
    Cobre o eco que a Evolution entrega antes até de lib_whatsapp registrar o id enviado.
    Só vale por ENVIO_EM_CURSO_MAX: se o processo morreu entre a T1 e a T2, o envio.ok=None fica para sempre e
    uma mensagem manual de Pedro com o mesmo texto não pode ser confundida com eco depois disso.
    """
    texto = " ".join((msg.get("texto") or "").split())
    if not texto or msg.get("grupo"):
        return False
    limite = core._now() - ENVIO_EM_CURSO_MAX
    for p in core.listar_pendencias("todas"):
        wa = p.get("whatsapp") or {}
        envio = wa.get("envio") or {}
        solicitado = core._parse_dt(envio.get("solicitado_em")) if envio else None
        if (p.get("tipo") == "whatsapp" and p.get("status") == "respondida" and envio
                and envio.get("ok") is None and envio.get("enviar") and not envio.get("manual")
                and solicitado and solicitado >= limite
                and wa.get("remote_jid") == msg.get("remote_jid")
                and " ".join((p.get("resposta_texto") or "").split()) == texto):
            return True
    return False


def _eco_do_alfred(msg: dict) -> bool:
    """Mensagem que o próprio Alfred mandou para Pedro e voltou pelo webhook."""
    if enviado_pelo_alfred(msg.get("message_id")):
        return True
    if _MARCAS_ALFRED.search(msg.get("texto") or ""):
        return True
    seg = segundos_desde_ultimo_envio()
    return msg.get("tipo_msg") == "audio" and seg is not None and seg < 45


def _detectar_e_agendar(conteudo: str, contato: str, msg: dict, pid: Optional[str] = None,
                        para_mim: bool = False) -> Optional[dict]:
    """Compromisso com data no texto → item da agenda (notificação [Ok] [Desfazer]). None se nada."""
    acfg = agenda._cfg()
    if msg.get("grupo") or not (acfg["notas_para_mim"] if para_mim else acfg["auto_whatsapp"]):
        return None
    try:
        comp = agenda.detectar_compromisso(
            conteudo, contexto="nota que Pedro mandou para si mesmo no WhatsApp" if para_mim
            else f"mensagem de WhatsApp de {contato}", para_mim=para_mim)
    except Exception as e:
        log(f"[agenda] detecção falhou: {e}")
        return None
    if not comp:
        return None
    desc = (("Você anotou no WhatsApp" if para_mim else f"Mensagem de {contato} (WhatsApp)")
            + f": “{conteudo[:600]}”" + (f"\nPendência Alfred: {pid}" if pid else ""))
    return agenda.adicionar(
        comp["titulo"] or f"Compromisso com {contato}", comp["inicio"], dia_inteiro=comp["dia_inteiro"],
        tipo=comp["tipo"], fim=comp["fim"], local=comp["local"], descricao=desc,
        origem={"canal": "whatsapp-eu" if para_mim else "whatsapp", "contato": contato, "pendencia_id": pid,
                "message_id": msg.get("message_id"), "confianca": comp["confianca"]})


# ══════════════════════════════════════════════════════════════
#  Áudios de resumo: apagados depois de N dias (já ficaram no WhatsApp de Pedro)
# ══════════════════════════════════════════════════════════════

@_serializado
def limpar_audios(dias: Optional[float] = None, simular: bool = False) -> dict:
    """
    Apaga os .mp3 de resumo das pendências de WhatsApp criados há mais de `dias` (perfis.yml →
    whatsapp.<nome>.audio_reter_dias, padrão 2). O .md e a pendência ficam; arquivos.mp3 vira None.
    Só toca em arquivos referenciados por pendências tipo "whatsapp" dentro de VAULT_ALFRED/pendencias/.
    """
    if dias is None:
        try:
            dias = float((carregar_cfg()[1] or {}).get("audio_reter_dias", 2))
        except Exception:
            dias = 2.0
    limite = datetime.now() - timedelta(days=dias)
    pd = core.pend_dir().resolve()
    removidos, mantidos, bytes_liberados = [], 0, 0
    with core.trava_pendencias():   # só stat/unlink de arquivos locais dentro da trava (trecho curto)
        data = core._load_pend()
        mudou = False
        for p in data["pendencias"]:
            if p.get("tipo") != "whatsapp":
                continue
            rel = (p.get("arquivos") or {}).get("mp3")
            if not rel:
                continue
            f = (core.vault() / rel).resolve()
            if f.parent != pd or f.suffix != ".mp3":
                continue
            if not f.exists():
                p["arquivos"]["mp3"] = None
                mudou = True
                continue
            criado = datetime.fromtimestamp(f.stat().st_mtime)
            try:
                criado = min(criado, datetime.fromisoformat(p.get("criado_em")).replace(tzinfo=None))
            except (TypeError, ValueError):
                pass
            if criado > limite:
                mantidos += 1
                continue
            tam = f.stat().st_size
            if not simular:
                f.unlink()
                p["arquivos"]["mp3"] = None
                p["audio_removido_em"] = core._now().isoformat(timespec="minutes")
                mudou = True
            removidos.append(p["id"])
            bytes_liberados += tam
        if mudou:
            core._save_pend(data)
    if removidos:
        log(f"[whatsapp] áudios de resumo {'a apagar' if simular else 'apagados'} (> {dias:g} dias): "
            f"{len(removidos)} · {bytes_liberados / 1e6:.1f} MB")
    return {"dias": dias, "removidos": removidos, "mantidos": mantidos, "bytes": bytes_liberados,
            "simulado": simular}


def _nota_para_mim(msg: dict, cfg: dict, res: dict, enviar: bool, notificar: bool, assistente: bool = False) -> dict:
    """Pedro escreveu para si mesmo: se for lembrete/tarefa/evento com data, vai para a agenda.
    `assistente` = Pedro escreveu para o número do Alfred: é ordem — sem data vira lembrete de hoje, e a
    confirmação (ou o aviso de que não deu para entender) volta para o número de Pedro."""
    if not assistente and _eco_do_alfred(msg):
        res["motivo"] = "eco de mensagem enviada pelo Alfred (conversa você)"
        return res
    texto = msg.get("texto") or ""
    if msg.get("tipo_msg") == "audio":
        tr = transcrever_mensagem(msg, cfg)
        res["transcricao"] = {k: tr.get(k) for k in ("texto", "metodo", "duracao_s", "erro")}
        texto = tr.get("texto") or ""
    ag = _detectar_e_agendar(texto, "Pedro", msg, para_mim=True)
    destino = msg.get("numero") if assistente else (msg.get("dono") or numero_pedro())
    if not ag and assistente:
        if texto.strip() and agenda._cfg()["notas_para_mim"]:
            ag = agenda.adicionar(texto.strip()[:120], core._now(), dia_inteiro=True, tipo="lembrete",
                                  descricao=f"Você pediu ao Alfred no WhatsApp: “{texto[:600]}”",
                                  origem={"canal": "whatsapp-eu", "contato": "Pedro", "pendencia_id": None,
                                          "message_id": msg.get("message_id"), "confianca": None})
        elif not texto.strip():
            res["motivo"] = "pedido de Pedro sem texto legível"
            if notificar and enviar and destino:
                res["envio_confirmacao"] = enviar_texto(destino, "✖ Não consegui entender. Pode mandar em texto?",
                                                        inst=msg.get("instancia"))
            return res
    if not ag:
        res["motivo"] = "nota de Pedro para si mesmo sem compromisso com data"
        return res
    res["agenda"] = {k: ag.get(k) for k in ("id", "titulo", "inicio", "status", "_novo")}
    res.update(acao="agenda", motivo="lembrete anotado por Pedro")
    conf = (f"📅 Anotado na agenda: *{ag['titulo']}* — {agenda._quando_legivel(ag)}"
            + ("" if ag.get("_novo") else " (já estava)"))
    res["notificacao"] = {"numero": destino, "texto": conf, "pendencia_id": None, "audio_url": None}
    if notificar and enviar and destino:
        res["envio_confirmacao"] = enviar_texto(destino, conf, inst=msg.get("instancia"))
    return res


def _registrar_historico(msg: dict, texto: str, cfg: dict) -> None:
    """Grava a mensagem no histórico FTS5 (lib_db). Pula o que `_descarte_sem_texto` descarta (grupo ignorado,
    contatos_ignorar, reação/figurinha); `historico: false` em perfis.yml desliga a gravação."""
    if not cfg.get("historico", True) or _descarte_sem_texto(msg, cfg):
        return
    lib_db.registrar_wa(msg.get("message_id") or "", msg.get("remote_jid") or "", _contato_legivel(msg), texto,
                        grupo=bool(msg.get("grupo")), from_me=bool(msg.get("from_me")),
                        tipo=msg.get("tipo_msg") or "texto", ts=msg.get("timestamp"))


def _registrar_enviada(msg: dict, cfg: dict) -> None:
    """Mensagem de Pedro a um contato (direto do celular ou enviada pelo Alfred) → histórico, para a varredura de
    promessas (promessas.py). O que vai para o próprio Pedro (conversa "você", resumos do Alfred) fica de fora.
    Mídia sem legenda vira "(imagem enviada)": a verificação da promessa precisa saber que algo foi mandado."""
    if _conversa_comigo(msg) or (numero_pedro() and msg.get("numero") == numero_pedro()):
        return
    _registrar_historico(msg, msg.get("texto") or f"({msg.get('tipo_msg') or 'mensagem'} enviada)", cfg)


def _resposta_de_pedro(msg: dict, cfg: dict, res: dict, ref: str, enviar: bool, notificar: bool) -> dict:
    """Pedro respondeu citando a pergunta do Alfred (`ref` = id da pendência): envia ao contato e confirma."""
    texto = msg.get("texto") or ""
    if msg.get("tipo_msg") == "audio":
        tr = transcrever_mensagem(msg, cfg)
        res["transcricao"] = tr
        texto = tr.get("texto") or ""
        if not texto:
            res.update(acao="ignorada", motivo="áudio de resposta não transcrito — responda em texto")
            return res
    try:
        entry = responder_texto(ref, texto, enviar=enviar, origem="via WhatsApp")
    except (KeyError, ValueError) as e:
        res.update(acao="ignorada", motivo=f"resposta não aceita: {e}")
        return res
    res["pendencia"] = entry
    wa = entry.get("whatsapp") or {}
    if entry["status"] == "ignorada":
        res.update(acao="pendencia_ignorada", motivo="Pedro pediu para ignorar")
        conf = f"✔ Ok, não respondo a {wa.get('contato')}.\nref: {ref}"
    elif entry["status"] == "respondida":
        res.update(acao="resposta_registrada", envio=wa.get("envio"))
        conf = (f"✔ Enviado a *{wa.get('contato')}*" + (" (simulado)" if (wa.get('envio') or {}).get('simulado') else "")
                + f":\n“{texto[:300]}”\nref: {ref}")
    else:
        res.update(acao="ignorada", motivo=f"envio falhou: {(wa.get('envio') or {}).get('erro')}", envio=wa.get("envio"))
        conf = f"✖ Não consegui enviar a {wa.get('contato')}: {(wa.get('envio') or {}).get('erro')}\nref: {ref}"
    res["notificacao"] = {"numero": numero_pedro() or msg.get("dono") or None, "texto": conf,
                          "pendencia_id": ref, "audio_url": None}
    if notificar and res["notificacao"]["numero"] and enviar:
        res["envio_confirmacao"] = enviar_texto(res["notificacao"]["numero"], conf, inst=msg.get("instancia"))
    return res


# ══════════════════════════════════════════════════════════════
#  Sessão assistente — o número do Alfred
# ══════════════════════════════════════════════════════════════
#
# Pedro escreve (ou fala) para o número do Alfred → é ordem: lembrete/tarefa/evento vai para a agenda.
# Outra pessoa escreve → o Alfred recepciona com UMA saudação. Quando a conversa se mostra séria (quem julga
# é o LLM, com o perfil e a memória de Pedro), ele olha a agenda, passa a previsão de retorno + o site, abre
# a pendência e avisa Pedro. Depois disso fica em silêncio por `silencio_horas`: o que chegar só é repassado.
# O texto que vai ao contato é montado aqui (perfis.yml → assistente:); do LLM só entra a previsão (uma
# linha, ≤ 80 caracteres, sem link) — agenda e memória de Pedro não vazam para um desconhecido.

ATENDIMENTOS_FILE = core.PROJECT_ROOT / "logs" / "whatsapp_atendimentos.json"   # {remote_jid: {contato, estagio, em, mensagens}}
SAUDACAO_PADRAO = "Olá! Aqui é o Alfred, assistente do Pedro Netto. Como posso ajudar?"
RETORNO_PADRAO = ("O Pedro não está disponível agora. Pela agenda dele, a previsão de retorno é {previsao}. "
                  "Já avisei ele da sua mensagem.")
SITE_PADRAO = "Enquanto isso, você encontra mais informações em {site}"
ASSINATURA = "🎩 *Alfred:* "   # abre toda mensagem do Alfred a um contato: quem fala é o assistente, não Pedro
TRAVOU = "Entendi. Vou passar a sua mensagem direto para o Pedro."   # a conversa não anda: Pedro é chamado
FALAS_NO_PROMPT = 8           # só as últimas falas vão ao modelo; o que veio antes fica na `situacao` que ele escreve
RESPOSTAS_MAX = 8             # respostas do Alfred por contato além da saudação (assistente.respostas_max)
SCHEMA_ATENDIMENTO = {
    "type": "object",
    "properties": {
        "seria":    {"type": "boolean"},
        "resposta": {"type": "string"},
        "situacao": {"type": "string"},
        "previsao": {"type": "string"},
        "resumo":   {"type": "string"},
        "motivo":   {"type": "string"},
        "urgencia": {"type": "string", "enum": list(_URGENCIAS)},
    },
    "required": ["seria", "resposta", "situacao", "previsao", "resumo", "motivo", "urgencia"],
}


def _load_atendimentos() -> dict:
    try:
        data = json.loads(ATENDIMENTOS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _salvar_atendimento(jid: str, dados: dict) -> None:
    with core.trava_arquivo(ATENDIMENTOS_FILE):
        limite = core._now() - timedelta(days=7)
        todos = {j: a for j, a in _load_atendimentos().items() if (core._parse_dt(a.get("em")) or limite) > limite}
        todos[jid] = dados
        ATENDIMENTOS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ATENDIMENTOS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(ATENDIMENTOS_FILE)


def _ocupacao(agora: datetime, dias: int = 3) -> str:
    """Horários ocupados de Pedro nos próximos dias, SEM título nem local: o prompt lê texto de desconhecidos."""
    fim_janela = agora + timedelta(days=dias)
    duracao = timedelta(minutes=agenda._cfg()["duracao_min"])
    linhas = []
    for i in agenda.listar():
        if i.get("status") in ("desfeito", "cancelado", "desfazer") or i.get("tipo") != "evento":
            continue
        try:
            if i.get("dia_inteiro"):
                d = datetime.fromisoformat(str(i.get("inicio"))[:10]).date()
                if agora.date() <= d <= fim_janela.date():
                    linhas.append((d.isoformat(), f"- {d:%d/%m} o dia todo"))
                continue
            ini = datetime.fromisoformat(str(i.get("inicio")))
            fim = datetime.fromisoformat(str(i["fim"])) if i.get("fim") else ini + duracao
        except (TypeError, ValueError):
            continue
        ini = ini if ini.tzinfo else ini.replace(tzinfo=agora.tzinfo)
        fim = fim if fim.tzinfo else fim.replace(tzinfo=agora.tzinfo)
        if fim >= agora and ini <= fim_janela:
            linhas.append((ini.isoformat(), f"- {ini:%d/%m} das {ini:%H:%M} às {fim:%H:%M}"))
    return "\n".join(l for _, l in sorted(linhas)) or "nenhum compromisso marcado"


def avaliar_atendimento(contato: str, mensagens: list[dict], cfg: dict, situacao: str = "") -> Optional[dict]:
    """A conversa é séria? Quando Pedro retorna? O que o Alfred responde sobre Pedro? →
    {seria, resposta, situacao, previsao, resumo, motivo, urgencia}; None = LLM fora. `situacao` = o resumo curto
    do chat que o próprio modelo escreveu na vez anterior e reescreve agora (a memória da conversa: o prompt leva
    ela + as últimas FALAS_NO_PROMPT falas, não o histórico inteiro). O prompt NÃO leva a memória de Pedro:
    `resposta` é texto livre que vai a um desconhecido, e só pode sair do perfil e de assistente.apresentacao."""
    # com as falas do próprio Alfred: sem elas o modelo repetia a mesma resposta a cada mensagem (10/11)
    conversa = "\n".join(f"{'ALFRED' if m.get('de') == 'alfred' else 'CONTATO'}: {m.get('texto') or '(mensagem sem texto)'}"
                         for m in mensagens[-FALAS_NO_PROMPT:])[-3000:]
    acfg = cfg.get("assistente") or {}
    agora = core._now()
    prompt = render_template("whatsapp_assistente", {
        "agora":            agora.strftime("%Y-%m-%d (%A) %H:%M"),
        "perfil_descricao": (cfg.get("_perfil") or {}).get("descricao", ""),
        "apresentacao":     str(acfg.get("apresentacao") or "").strip() or "nada além do contexto acima",
        "ocupacao":         _ocupacao(agora),
        "contato":          contato,
        "situacao":         situacao or "conversa começando agora",
        "conversa":         conversa,
    })
    data = ollama_generate_json(prompt, model=str(acfg.get("modelo") or "").strip() or None, timeout=120,
                                schema=SCHEMA_ATENDIMENTO,
                                options={**core.OPTS_JSON, "num_predict": 400,
                                         **core.ctx_do_chat(str(acfg.get("modelo") or "").strip())})
    if not isinstance(data, dict):
        return None
    resposta = " ".join(re.sub(r"(https?://|www\.)\S+", "", str(data.get("resposta") or "")).split())[:500]
    previsao = " ".join(str(data.get("previsao") or "").split())
    previsao = re.sub(r"^a previsão de retorno é\s*", "", previsao, flags=re.I)[:80]   # o gemma4:e4b repete o molde
    if not previsao or re.search(r"https?:|www\.", previsao, re.I):
        previsao = "assim que possível"
    return {"seria": bool(data.get("seria")), "resposta": resposta, "previsao": previsao,
            "situacao": " ".join(re.sub(r"(https?://|www\.)\S+", "", str(data.get("situacao") or "")).split())[:400],
            "resumo": str(data.get("resumo") or "").strip()[:300], "motivo": str(data.get("motivo") or "").strip()[:200],
            "urgencia": data.get("urgencia") if data.get("urgencia") in _URGENCIAS else "media"}


def _abrir_pendencia(msg: dict, cat: dict, cfg: dict, nome: str, transcricao: Optional[dict], mensagens: list[dict],
                     res: dict, enviar: bool, notificar: bool, notificar_desktop: bool) -> dict:
    junta = {**msg, "texto": "\n".join(m["texto"] for m in mensagens if m.get("texto"))}
    entry = criar_pendencia_whatsapp(junta, cat, cfg, nome, transcricao, mensagens=mensagens)
    res.update(acao="pendencia_criada", pendencia=entry, notificacao=mensagem_para_pedro(entry))
    if notificar_desktop:
        core.notificar_pendencia(entry)
    if notificar and enviar:
        res["envio"] = notificar_pedro(entry["id"], enviar=True)   # sai do número do Alfred para o de Pedro
    return res


@lib_porta.origem("reply to a whatsapp message from a contact")
def _recepcionar(msg: dict, nome: str, cfg: dict, res: dict, enviar: bool, notificar: bool,
                 notificar_desktop: bool) -> dict:
    """Outra pessoa escreveu para o número do Alfred. Ações: saudacao | pendencia_criada | agrupada | ignorada."""
    acfg = cfg.get("assistente") or {}
    descarte = _descarte_sem_texto(msg, cfg)   # contatos_ignorar, reação, figurinha
    if descarte:
        res["motivo"] = descarte["resumo"]
        return res
    conteudo, transcricao = msg.get("texto") or "", None
    if msg.get("tipo_msg") == "audio":
        transcricao = transcrever_mensagem(msg, cfg)
        res["transcricao"] = {k: transcricao.get(k) for k in ("texto", "metodo", "duracao_s", "segmentos", "cortado", "erro")}
        conteudo = transcricao.get("texto") or ""
    _registrar_historico(msg, conteudo, cfg)
    contato, jid, agora = _contato_legivel(msg), msg.get("remote_jid"), core._now()
    nova = {"message_id": msg.get("message_id"), "tipo": msg.get("tipo_msg"), "texto": conteudo,
            "em": msg.get("timestamp")}
    at = _load_atendimentos().get(jid)
    silencio = timedelta(hours=float(acfg.get("silencio_horas", 12) or 0))
    if at and agora - (core._parse_dt(at.get("em")) or agora) > silencio:
        at = None   # conversa antiga: recomeça pela saudação

    if at and at.get("estagio") == "informado":   # já tem a previsão: o Alfred não fala mais, só repassa
        aberta = _pendencia_aguardando_do_jid(jid)
        erro_tr = "" if conteudo else str((transcricao or {}).get("erro") or "")
        entry = agrupar_mensagem(aberta["id"], msg, conteudo, erro=erro_tr) if aberta else None
        if not entry:
            cat = _heuristica("profissional", conteudo or _descricao_tipo(msg, transcricao),
                              "contato já atendido pelo Alfred", requer=True, urg="media")
            return _abrir_pendencia(msg, cat, cfg, nome, transcricao, [nova], res, enviar, notificar, notificar_desktop)
        agr = {"tipo_msg": msg.get("tipo_msg"),
               "texto": conteudo or (f"(áudio não transcrito: {erro_tr})" if erro_tr else "")}
        res.update(acao="agrupada", motivo="contato já atendido pelo Alfred — repassado a Pedro", pendencia=entry,
                   notificacao=mensagem_para_pedro(entry, agrupada=agr))
        if notificar and enviar:
            res["envio"] = notificar_pedro(entry["id"], agrupada=agr, enviar=True)
        return res

    mensagens = ((at or {}).get("mensagens") or []) + [nova]
    aval = avaliar_atendimento(contato, mensagens, cfg, situacao=(at or {}).get("situacao") or "")
    res["classificacao"] = aval
    respostas = int((at or {}).get("respostas") or 0)
    if at and aval and not aval["seria"]:
        # enrosco: o Alfred ia repetir o que já disse, ou já falou o que podia e a pessoa continua → Pedro decide
        ditas = [m.get("texto") or "" for m in mensagens if m.get("de") == "alfred"]
        repete = aval["resposta"] and any(
            difflib.SequenceMatcher(None, aval["resposta"].lower(), d.lower()).ratio() > 0.7 for d in ditas)
        if repete or respostas >= int(acfg.get("respostas_max", RESPOSTAS_MAX)):
            aval = {**aval, "seria": True, "resposta": TRAVOU, "urgencia": "media",
                    "resumo": f"{aval['resumo']} — a conversa com o Alfred travou e a pessoa insiste",
                    "motivo": "o Alfred ia se repetir" if repete else "o Alfred já respondeu o que podia"}
            res["classificacao"] = aval
    seria = aval is None or aval["seria"]   # LLM fora do ar: Pedro é avisado, o contato não recebe previsão
    partes = [] if at else [str(acfg.get("saudacao") or SAUDACAO_PADRAO)]
    if at and aval and aval["resposta"]:
        partes.append(aval["resposta"])   # o Alfred responde como assistente: apresenta Pedro, não bate papo
        respostas += 1
    if aval and aval["seria"]:
        partes.append(str(acfg.get("retorno") or RETORNO_PADRAO).replace("{previsao}", aval["previsao"]))
        if acfg.get("site"):
            partes.append(SITE_PADRAO.replace("{site}", str(acfg["site"]).strip()))
    texto = ASSINATURA + "\n\n".join(partes) if partes else ""
    do_contato = mensagens   # o que vai para a pendência de Pedro: só o que o contato escreveu
    if partes:
        mensagens = mensagens + [{"de": "alfred", "texto": "\n\n".join(partes), "em": agora.isoformat(timespec="minutes")}]
    res["resposta_ao_contato"] = texto or None
    if texto and enviar:
        res["envio_contato"] = enviar_texto(msg.get("numero"), texto, inst=msg.get("instancia"))
        log(f"[whatsapp] assistente → {contato[:24]}: {texto[:160]!r}")
    _salvar_atendimento(jid, {"contato": contato, "estagio": "informado" if seria else "saudado",
                              "em": agora.isoformat(timespec="minutes") if (texto or not at) else at["em"],
                              "respostas": respostas, "mensagens": mensagens[-20:],
                              "situacao": (aval or {}).get("situacao") or (at or {}).get("situacao") or ""})
    log(f"[whatsapp] assistente: {contato[:24]} → {'séria' if seria else 'não séria'}"
        + (f" ({aval['motivo'][:80]})" if aval else " (modelo fora do ar)"))
    if not seria:
        res.update(acao="saudacao" if not at else "apresentacao" if texto else "ignorada",
                   motivo="primeira mensagem: só a saudação" if not at else
                          "o Alfred apresentou Pedro" if texto else f"conversa ainda não é séria: {aval['motivo']}")
        return res
    if aval:
        resumo = f"{aval['resumo']} — o Alfred passou a previsão de retorno: {aval['previsao']}."
    else:
        resumo = (conteudo[:200] or _descricao_tipo(msg, transcricao)) + " — o Alfred não avaliou (modelo fora do ar) e não passou previsão."
    cat = _heuristica("profissional", resumo, (aval or {}).get("motivo") or "sem avaliação do modelo",
                      requer=True, urg=(aval or {}).get("urgencia") or "media")
    return _abrir_pendencia(msg, cat, cfg, nome, transcricao, [m for m in do_contato if m.get("de") != "alfred"],
                            res, enviar, notificar, notificar_desktop)


def recados() -> list[dict]:
    """Recados deixados no número do Alfred que ainda esperam Pedro: as pendências aguardando da sessão assistente,
    da mais nova para a mais antiga. → [{id, titulo, contato, texto, quando}] (Início do Painel e post-its do Pestinha)."""
    insts = {s["instancia"] for s in sessoes() if s["papel"] == "assistente"}
    saida = []
    for p in core.listar_pendencias("aguardando"):
        wa = p.get("whatsapp") or {}
        if p.get("tipo") == "whatsapp" and wa.get("instancia") in insts:
            saida.append({"id": p["id"], "titulo": p.get("titulo") or "", "contato": wa.get("contato") or "",
                          "texto": (p.get("evento") or {}).get("resumo") or wa.get("texto") or "",
                          "quando": p.get("criado_em") or ""})
    return sorted(saida, key=lambda r: r["quando"], reverse=True)


def _atender(msg: dict, nome: str, cfg: dict, res: dict, enviar: bool, notificar: bool, notificar_desktop: bool) -> dict:
    """Mensagem que chegou na sessão assistente (o número do Alfred)."""
    if msg.get("from_me"):
        res["motivo"] = "mensagem enviada pelo número do Alfred"
        return res
    if msg.get("grupo"):
        res["motivo"] = "a sessão assistente não atende grupos"
        return res
    mid = msg.get("message_id") or ""
    seen = core.load_seen(SEEN_FILE)
    if mid and mid in seen:
        res.update(acao="duplicada", motivo="mensagem já processada")
        return res
    if mid:
        seen[mid] = datetime.now().isoformat(timespec="minutes")
        core.save_seen(seen, SEEN_FILE)
    if numero_pedro() and msg.get("numero") == numero_pedro():
        lib_atividade.notado("whatsapp")
        ref = _ref_citada(msg)
        if ref:
            return _resposta_de_pedro(msg, cfg, res, ref, enviar, notificar)
        res.update(acao="pedido", motivo="pedido de Pedro ao Alfred")
        if not PEDIDO_EM_THREAD:
            return _pedido_de_pedro(msg, cfg, res, enviar, notificar)
        # fora da trava do webhook: o turno leva de segundos a minutos e o "sim" da aprovação chega por outro webhook
        threading.Thread(target=_pedido_de_pedro, args=(msg, cfg, dict(res), enviar, notificar),
                         name="whatsapp-pedido", daemon=True).start()
        return res
    return _recepcionar(msg, nome, cfg, res, enviar, notificar, notificar_desktop)


# ── Pedro → número do Alfred: canal direto do motor da conversa (conversa.py) ──
PEDIDO_EM_THREAD = True     # os testes desligam: o pedido roda dentro da chamada
PEDIDO_ESPERA_S = 900       # teto de um pedido (inclui os 5 min de uma aprovação)
CONVERSA_OCIOSA_H = 6       # parado há mais que isso, o próximo pedido abre outra conversa
TERMOS_VOZ = "Alfred, Fatec, Thunderbird, Obsidian."   # o mesmo padrão do microfone do Painel (VOZ_TERMOS no .env)
# ferramentas perigosas que rodam sem perguntar neste canal (perfis.yml → assistente.sem_aprovacao troca)
SEM_APROVACAO_PADRAO = ("criar_compromisso", "completar_tarefa", "salvar_nota")
_SIM = {"sim", "s", "pode", "ok", "executar", "execute", "manda", "confirmo"}
_NAO = {"não", "nao", "n", "cancela", "cancelar", "negativo"}
# o modelo às vezes responde "tarefa adicionada" sem ter chamado ferramenta nenhuma (visto em 10/11 com o gemma4:12b)
_DIZ_QUE_FEZ = re.compile(r"\b(adicionad|anotad|agendad|criad|registrad|marcad|salv[oa]|lembrarei|lembrete)", re.I)
COBRANCA = ("Nenhuma ferramenta foi chamada, então nada foi gravado. Chame agora a ferramenta certa para o meu "
            "pedido anterior e só depois responda.")
_APROVACAO_WA: dict = {}    # {"id": aprovação do conversa.py esperando o "sim"/"não" de Pedro}


def _conversa_do_whatsapp() -> str:
    """Id da conversa (conversa.py) em que caem os pedidos de Pedro pelo WhatsApp; nova depois de CONVERSA_OCIOSA_H."""
    import conversa
    with core.trava_arquivo(SESSOES_FILE):
        est = _load_sessoes()
        c = conversa.obter_conversa(est["conversa"].get("id") or "")
        try:
            velha = (datetime.now().astimezone() - datetime.fromisoformat(c["atualizada_em"])
                     > timedelta(hours=CONVERSA_OCIOSA_H))
        except Exception:
            velha = True
        if velha:
            c = conversa.nova_conversa("WhatsApp")
            est["conversa"] = {"id": c["id"]}
            _save_sessoes(est)
    return c["id"]


def _acompanhar(turno, dizer, livres) -> tuple[Optional[str], bool]:
    """Espera o turno acabar. Ferramenta perigosa: roda direto se está em `livres`; senão pergunta a Pedro e
    espera o "sim"/"não" (que chega por outro webhook). → (resposta | None se o turno falhou, usou ferramenta?)."""
    import conversa
    limite, usou = time.monotonic() + PEDIDO_ESPERA_S, False
    try:
        while True:
            try:
                ev = turno.fila.get(timeout=max(1.0, limite - time.monotonic()))
            except queue.Empty:
                conversa.parar(turno.id)
                return "✖ O pedido demorou demais e foi interrompido.", usou
            if ev["tipo"] == "aprovar":
                if ev["nome"] in livres:
                    conversa.aprovar(ev["id"])
                else:
                    _APROVACAO_WA["id"] = ev["id"]
                    args = json.dumps(ev.get("argumentos") or {}, ensure_ascii=False)[:300]
                    risco = f" Risco: {ev['motivo']}." if ev.get("motivo") else ""
                    dizer(f"⚠ Para executar *{ev['nome']}* {args}, responda *sim* ou *não* (5 min).{risco}")
            elif ev["tipo"] == "ferramenta" and ev.get("estado") == "ok":
                usou = True
            elif ev["tipo"] == "fim":
                return ev.get("texto") or ("Interrompido." if ev.get("parado") else "Feito."), usou
            elif ev["tipo"] == "erro":
                log(f"[whatsapp] pedido de Pedro: o turno falhou ({ev.get('erro')})")
                return None, usou
    finally:
        _APROVACAO_WA.clear()


@lib_porta.origem("whatsapp message the owner sent to Alfred", dono=True)
def _pedido_de_pedro(msg: dict, cfg: dict, res: dict, enviar: bool, notificar: bool) -> dict:
    """Pedro escreveu ou falou com o número do Alfred: transcreve (Whisper) e entrega ao motor da conversa, que usa
    as ferramentas e responde no próprio WhatsApp. Motor fora do ar e nada executado → cai na agenda direta."""
    destino, inst = msg.get("numero"), msg.get("instancia")

    def dizer(texto: str):
        if notificar and enviar and destino:
            return enviar_texto(destino, texto[:3500], inst=inst)
        return None

    texto = msg.get("texto") or ""
    if msg.get("tipo_msg") == "audio":
        tr = transcrever_mensagem(msg, cfg, motor="whisper", dica=os.getenv("VOZ_TERMOS", "").strip() or TERMOS_VOZ)
        res["transcricao"] = {k: tr.get(k) for k in ("texto", "metodo", "duracao_s", "erro")}
        texto = tr.get("texto") or ""
    texto = texto.strip()
    if not texto:
        res["motivo"] = "pedido de Pedro sem texto legível"
        res["envio_confirmacao"] = dizer("✖ Não consegui entender. Pode mandar em texto?")
        return res
    import conversa
    pendente = _APROVACAO_WA.get("id")
    palavra = texto.lower().strip(" .!?")
    if pendente and palavra in _SIM | _NAO:
        _APROVACAO_WA.clear()
        if not (conversa.aprovar if palavra in _SIM else conversa.negar)(pendente):
            dizer("Esse pedido de confirmação já expirou.")
        res.update(acao="aprovacao", motivo="Pedro respondeu a um pedido de confirmação")
        return res
    import remedios
    try:   # "sim" / "não" / a hora, em resposta à pergunta de uma dose (remedios.passar)
        sobre_dose = remedios.resposta_whatsapp(texto)
    except Exception as e:
        log(f"[whatsapp] pedido de Pedro: não consegui olhar os remédios ({e})")
        sobre_dose = None
    if sobre_dose:
        res.update(acao="remedio", motivo="Pedro respondeu sobre a dose de um remédio", resposta=sobre_dose)
        res["envio_confirmacao"] = dizer(sobre_dose)
        return res
    log(f"[whatsapp] pedido de Pedro: {texto[:100]!r}")
    try:
        turno = conversa.enviar(_conversa_do_whatsapp(), texto, origem="whatsapp")   # sem `contexto`: com ele o gemma4 parou de usar as ferramentas (10/11)
    except RuntimeError:
        res["motivo"] = "pedido anterior ainda em andamento"
        res["envio_confirmacao"] = dizer("Ainda estou no pedido anterior. Mande de novo em instantes.")
        return res
    livres = (cfg.get("assistente") or {}).get("sem_aprovacao")
    livres_ = SEM_APROVACAO_PADRAO if livres is None else livres
    resposta, usou = _acompanhar(turno, dizer, livres_)
    if resposta and not usou and _DIZ_QUE_FEZ.search(resposta):   # cobra uma vez antes de desistir do motor
        log(f"[whatsapp] pedido de Pedro: o modelo disse que fez sem usar ferramenta ({resposta[:80]!r}) → cobrando")
        try:
            conversa.pensar(turno.conversa_id, turno.id, "whatsapp", "cobranca",
                            "a resposta diz que fez, mas nenhuma ferramenta rodou: cobrando num turno novo", resposta=resposta)
            resposta, usou = _acompanhar(conversa.enviar(turno.conversa_id, COBRANCA, origem="whatsapp"), dizer, livres_)
        except Exception as e:
            log(f"[whatsapp] pedido de Pedro: a cobrança falhou ({e})")
        if not usou:
            log("[whatsapp] pedido de Pedro: de novo sem ferramenta → agenda direta")
            resposta = None
    if resposta is None and not usou:   # motor fora do ar ou resposta sem ação: ao menos o lembrete não se perde
        conversa.pensar(turno.conversa_id, turno.id, "whatsapp", "queda",
                        "o motor da conversa não agiu: o pedido vai para a agenda direta como lembrete", pedido=texto)
        return _nota_para_mim({**msg, "texto": texto, "tipo_msg": "texto"}, cfg, res, enviar, notificar, assistente=True)
    resposta = resposta or "✖ Deu erro depois de eu começar a executar. Confira no Painel antes de repetir."
    res["resposta"] = resposta
    res["notificacao"] = {"numero": destino, "texto": resposta, "pendencia_id": None, "audio_url": None}
    res["envio_confirmacao"] = dizer(resposta)
    return res


@_serializado
def processar_mensagem_whatsapp(
    payload: dict,
    cfg_name: Optional[str] = None,
    enviar: bool = True,
    notificar: bool = False,
    notificar_desktop: bool = True,
    atraso_minutos: Optional[float] = None,
) -> dict:
    """
    Webhook da Evolution API → ação. Retorna:
      {"acao": pendencia_criada | agendada | agrupada | resposta_registrada | pendencia_ignorada |
               respondida_manual | fila_cancelada | duplicada | ignorada,
       "motivo", "mensagem", "classificacao", "transcricao", "pendencia", "notificacao", "envio"}
    `notificacao` = {numero, texto, audio_url, pendencia_id} para o orquestrador (N8N) mandar a Pedro.
    `notificar=True` faz o próprio Python mandar (sem N8N); `enviar=False` nunca chama a Evolution.
    Com `atraso_minutos` (perfis.yml; o parâmetro sobrepõe) > 0, mensagem relevante sem pendência aberta do
    contato vira acao=agendada + "prazo": pendência, áudio e avisos só quando processar_fila() achar o prazo
    vencido — com as mesmas opções enviar/notificar/notificar_desktop. Um fromMe de Pedro na conversa antes
    do prazo tira o contato da fila ("fila_cancelada": [message_ids]) e grava um item respondida_manual
    ("respondidas_na_espera": [ids]; no caso só-fila, "pendencia" = esse item).
    A sessão é a da instância que mandou o webhook (sessoes()); na de papel "assistente" quem trata é
    _atender(): acao = saudacao | pendencia_criada | agrupada | agenda | resposta_registrada | ignorada, com
    "resposta_ao_contato" / "envio_contato" quando o Alfred falou com a pessoa.
    """
    res: dict = {"acao": "ignorada", "motivo": "", "mensagem": None, "classificacao": None,
                 "transcricao": None, "pendencia": None, "notificacao": None, "envio": None}
    msg = normalizar_payload(payload)
    if not msg:
        res["motivo"] = "payload sem data.key/remoteJid"
        return res
    if msg.get("ignorar"):
        res["motivo"] = msg["ignorar"]
        return res
    res["mensagem"] = _resumo_msg(msg)
    sessao = None if cfg_name else _sessao_da_instancia(msg.get("instancia"))   # instância desconhecida → conta padrão
    nome, cfg = carregar_cfg(cfg_name or (sessao or {}).get("nome"))
    mid = msg.get("message_id") or ""
    if sessao:
        _anotar_numero(sessao, msg.get("dono"))
        if sessao["papel"] == "assistente":
            return _atender(msg, nome, cfg, res, enviar, notificar, notificar_desktop)
        if not msg.get("grupo") and msg.get("numero") in [s["numero"] for s in sessoes()
                                                          if s["numero"] and s["nome"] != sessao["nome"]]:
            res["motivo"] = "conversa com outra sessão do Alfred (tratada por ela)"
            return res

    # ── eco de algo que o próprio Alfred enviou ─────────────────
    if msg.get("from_me") and mid:
        for p in core.listar_pendencias("todas"):
            wa = p.get("whatsapp") or {}
            if mid in (wa.get("pergunta_msg_id"), (wa.get("envio") or {}).get("message_id")):
                _registrar_enviada(msg, cfg)   # resposta de Pedro enviada pelo Alfred (o resumo a Pedro fica de fora)
                res["motivo"] = "eco de mensagem enviada pelo Alfred"
                return res
        # envio feito por OUTRO processo (notificação no PC, CLI) cujo eco chega antes de o message_id ir
        # para pendencias.yml (T2 de responder_texto). A conversa "você" fica de fora: _nota_para_mim já
        # trata o eco dela do jeito dela.
        if not _conversa_comigo(msg) and (enviado_pelo_alfred(mid) or _envio_em_curso(msg)):
            _registrar_enviada(msg, cfg)
            res["motivo"] = "eco de mensagem enviada pelo Alfred"
            return res

    # ── Pedro escreveu na conversa: o que esperava na fila daquele contato não vira pendência aguardando ──
    # nem notificação; vira um registro respondida_manual com o rascunho que o Alfred teria proposto e a
    # resposta que Pedro deu (nada é enviado). O eco de uma resposta enviada pelo Alfred não chega aqui
    # (retorna acima): Pedro respondeu a pendência anterior e pode nem ter visto a mensagem nova.
    retirados = retirar_da_fila(msg.get("remote_jid")) if msg.get("from_me") else []
    cancelados = [mid_ for i in retirados for mid_ in _ids_do_item(i)]
    registrados: list[dict] = []
    if cancelados:
        res["fila_cancelada"] = cancelados
        for item in retirados:
            try:
                e = registrar_resposta_direta(item, msg)
                if (e.get("whatsapp") or {}).get("respondida_na_espera"):
                    registrados.append(e)
            except Exception as e:   # nunca derruba o webhook: a fila já foi cancelada, como antes
                log(f"[whatsapp] não consegui registrar a resposta direta a {item.get('contato')}: {e}")
        res["respondidas_na_espera"] = [e["id"] for e in registrados]

    def _sem_pendencia(motivo: str) -> dict:
        if cancelados:
            res.update(acao="fila_cancelada", motivo=f"Pedro respondeu a conversa antes do prazo — "
                                                     f"{len(cancelados)} mensagem(ns) saíram da fila")
            if registrados:
                res["pendencia"] = registrados[-1]
        else:
            res["motivo"] = motivo
        return res

    # ── mensagem de Pedro ───────────────────────────────────────
    if _eh_pedro(msg):
        lib_atividade.notado("whatsapp")
        ref = _ref_citada(msg)
        if ref:
            return _resposta_de_pedro(msg, cfg, res, ref, enviar, notificar)
        if _conversa_comigo(msg):
            return _nota_para_mim(msg, cfg, res, enviar=enviar, notificar=notificar)
        if msg.get("from_me") and not msg.get("grupo"):
            _registrar_historico(msg, msg.get("texto") or "", cfg)
            p = _pendencia_aguardando_do_jid(msg.get("remote_jid"))
            fechada = _fechar_manual(p["id"], msg.get("texto") or "", msg) if p else None
            if fechada:
                res["pendencia"] = fechada
                res.update(acao="respondida_manual", motivo="Pedro respondeu o contato direto do celular")
                return res
        return _sem_pendencia("mensagem de Pedro sem citação de pendência")

    if msg.get("from_me"):
        _registrar_enviada(msg, cfg)
        p = _pendencia_aguardando_do_jid(msg.get("remote_jid")) if not msg.get("grupo") else None
        fechada = _fechar_manual(p["id"], msg.get("texto") or "", msg) if p else None
        if fechada:
            res["pendencia"] = fechada
            res.update(acao="respondida_manual", motivo="Pedro respondeu o contato direto do celular")
            return res
        return _sem_pendencia("mensagem enviada por mim (fromMe) sem pendência aberta")

    # ── dedup ───────────────────────────────────────────────────
    seen = core.load_seen(SEEN_FILE)
    existentes = core._load_pend()["pendencias"]
    if mid and (mid in seen or _ja_processada(existentes, mid)):
        res.update(acao="duplicada", motivo="mensagem já processada")
        return res

    # ── transcrição — só se a mensagem puder virar pendência ───
    # (áudio de grupo ignorado / contatos_ignorar: o descarte não depende do texto — nada de GPU nem
    #  trecho da conversa no journal; classificar_whatsapp abaixo chega à mesma heurística)
    conteudo = msg.get("texto") or ""
    transcricao = None
    if msg.get("tipo_msg") == "audio" and not _descarte_sem_texto(msg, cfg):
        transcricao = transcrever_mensagem(msg, cfg)
        res["transcricao"] = {k: transcricao.get(k) for k in ("texto", "metodo", "duracao_s", "segmentos", "cortado", "erro")}
        conteudo = transcricao.get("texto") or ""

    # ── classificação ───────────────────────────────────────────
    cat = classificar_whatsapp(msg, cfg, texto=conteudo)
    res["classificacao"] = cat
    _registrar_historico(msg, conteudo, cfg)
    log(f"[whatsapp] {cat['categoria']:<12} {cat['confianca']:.2f} resp={cat.get('requer_resposta')} "
        f"{_contato_legivel(msg)[:24]:<24} {(conteudo or msg.get('tipo_msg'))[:60]!r}")
    if mid:
        seen[mid] = datetime.now().isoformat(timespec="minutes")
        core.save_seen(seen, SEEN_FILE)

    ag = _detectar_e_agendar(conteudo, _contato_legivel(msg), msg)
    if ag:
        res["agenda"] = {k: ag.get(k) for k in ("id", "titulo", "inicio", "status", "_novo")}

    ok, motivo = _relevante(cat, cfg)
    if not ok:
        res["motivo"] = motivo
        return res

    # ── agrupar na pendência aberta do mesmo contato ────────────
    # `existentes` foi lido antes da transcrição/classificação: agrupar_mensagem confere de novo na trava e
    # devolve None se outro processo resolveu a pendência no meio → segue para a fila / pendência nova.
    aberta = _pendencia_aberta_do_contato(existentes, msg, cfg)
    try:
        erro_tr = "" if conteudo else str((transcricao or {}).get("erro") or "")
        entry = agrupar_mensagem(aberta["id"], msg, conteudo, erro=erro_tr) if aberta else None
    except KeyError:   # some do yml no meio (não acontece hoje, mas nunca perde a mensagem)
        entry = None
    if entry:
        agr = {"tipo_msg": msg.get("tipo_msg"),
               "texto": conteudo or (f"(áudio não transcrito: {erro_tr})" if erro_tr else "")}
        res.update(acao="agrupada", motivo=f"contato já tem a pendência {aberta['id']} aberta", pendencia=entry,
                   notificacao=mensagem_para_pedro(entry, agrupada=agr))
        if notificar and enviar:
            res["envio"] = notificar_pedro(entry["id"], agrupada=agr, enviar=True)
        return res

    # ── espera atraso_minutos: se Pedro responder antes, só o registro respondida_manual (sem aviso) ──
    atraso = _atraso_minutos(cfg if atraso_minutos is None else {"atraso_minutos": atraso_minutos})
    if atraso > 0:
        tr = {k: transcricao.get(k) for k in ("texto", "metodo", "duracao_s", "segmentos", "cortado", "erro")} \
            if transcricao else None
        item, juntou = enfileirar_mensagem(msg, cat, tr, conteudo, nome, atraso, {
            "enviar": enviar, "notificar": notificar, "notificar_desktop": notificar_desktop})
        n = len(item["mensagens"])
        res.update(acao="agendada", prazo=item["prazo"],
                   fila={"id": item["id"], "mensagens": n, "prazo": item["prazo"]},
                   motivo=(f"entrou no item da fila de {item['contato']} ({n} mensagens)" if juntou else
                           f"espera {atraso:g} min sem resposta de Pedro antes de resumir"))
        return res

    # ── nova pendência ──────────────────────────────────────────
    entry = criar_pendencia_whatsapp(msg, cat, cfg, nome, transcricao)
    res.update(acao="pendencia_criada", pendencia=entry, notificacao=mensagem_para_pedro(entry))
    if notificar_desktop:
        core.notificar_pendencia(entry)   # Enviar / Não enviar / Agenda / Ouvir no PC
    if notificar and enviar:
        res["envio"] = notificar_pedro(entry["id"], enviar=True)
    return res


def status(timeout: Optional[float] = None) -> dict:
    """Resumo para /health e CLI. `timeout` (s) limita a consulta à Evolution (painel: ~2 s); None = padrão (30 s)."""
    from _shared.lib_whatsapp import resumo_config, estado_instancia
    cfg = resumo_config()
    if cfg["configurado"]:
        est = estado_instancia() if timeout is None else estado_instancia(timeout=timeout)
        # 404 = a Evolution respondeu, mas a instância não existe (falta ./run.sh whatsapp qrcode);
        # "erro" (sem status) = rede/timeout; outros códigos ficam "HTTP nnn" (401/403: chave recusada)
        cfg["estado_instancia"] = _estado_legivel(est)
        # demais sessões (cada uma é uma instância): mesma leitura de estado
        cfg["sessoes"] = [{**s, "estado": cfg["estado_instancia"] if s["instancia"] == cfg["instancia"] else
                           _estado_legivel(estado_instancia(inst=s["instancia"], **({} if timeout is None else {"timeout": timeout})))}
                          for s in sessoes()]
    else:
        cfg["sessoes"] = [{**s, "estado": None} for s in sessoes()]
    aguard = [p for p in core.listar_pendencias("aguardando") if p.get("tipo") == "whatsapp"]
    cfg["pendencias_whatsapp_aguardando"] = len(aguard)
    cfg["fila_espera"] = listar_fila()
    return cfg


def _estado_legivel(est: dict) -> str:
    return est.get("estado") or ("outro serviço" if _nao_e_evolution(est) else
                                 "inexistente" if est.get("status") == 404 else
                                 est.get("erro") or f"HTTP {est.get('status')}")


def _nao_e_evolution(r: dict) -> bool:
    """404 com corpo que não é JSON: outro programa responde na EVOLUTION_URL (ex.: túnel ssh na mesma porta)."""
    return r.get("status") == 404 and "raw" in (r.get("resposta") or {})


def _garantir_webhook(inst: Optional[str]) -> None:
    """Instância de outra sessão sem webhook: copia o da instância padrão (sem ele nenhuma mensagem chega ao Alfred)."""
    from _shared import lib_whatsapp as lw
    if not inst or inst == lw.instancia() or lw.obter_webhook(inst).get("url"):
        return
    padrao = lw.obter_webhook()
    if not padrao.get("url"):
        log(f"[whatsapp] instância {inst} sem webhook e a padrão também: rode ./run.sh whatsapp webhook URL --sessao …")
        return
    r = lw.configurar_webhook(padrao["url"], headers=padrao.get("headers") or {},
                              base64_midia=bool(padrao.get("webhookBase64", True)), inst=inst)
    log(f"[whatsapp] webhook da instância {inst} → {padrao['url']} ({'ok' if r.get('ok') else 'falhou'})")


def obter_qrcode(sessao: Optional[str] = None) -> dict:
    """
    QR code para parear a instância (CLI `whatsapp qrcode`, painel e botão do vigia). `sessao` = conta de
    perfis.yml (None = a padrão); KeyError se não existe.
    → {"ok", "erro"?, "criando"?, "conectado"?, "base64"?, "code"?, "pairingCode"?}; "erro" já vem em texto para Pedro.
    """
    from _shared import lib_whatsapp as lw
    inst = obter_sessao(sessao)["instancia"] if sessao else None
    if not lw.configurado():
        return {"ok": False, "erro": "EVOLUTION_URL / EVOLUTION_API_KEY não configurados no .env"}
    est = lw.estado_instancia(inst=inst)
    if not est.get("ok") and est.get("status") in (401, 403):
        return {"ok": False, "erro": f"A Evolution API recusou a chave (HTTP {est['status']}) — "
                                     f"confira EVOLUTION_API_KEY no .env."}
    if _nao_e_evolution(est):
        return {"ok": False, "erro": f"Quem responde em {lw.url_base()} não é a Evolution API (resposta HTML) — "
                                     f"outro programa ocupa a porta; confira `ss -ltnp` e o container alfred-evolution."}
    if not est.get("ok") and est.get("status") != 404:   # rede/timeout ou outro erro: não é "instância não existe"
        return {"ok": False, "erro": f"Evolution API inacessível em {lw.url_base()}: "
                                     f"{est.get('erro') or ('HTTP ' + str(est.get('status')))} — "
                                     f"confira se o container está no ar."}
    criando = not est.get("ok")
    if criando:
        r = lw.criar_instancia(inst=inst)
        if not r.get("ok"):
            return {"ok": False, "criando": True, "erro": f"Falha ao criar instância: {r.get('erro') or r.get('resposta')}"}
    _garantir_webhook(inst)
    if not criando and est.get("estado") == "open":
        return {"ok": True, "conectado": True}
    r = lw.qrcode_instancia(inst=inst)
    if not r.get("ok"):
        return {"ok": False, "criando": criando, "erro": f"Falha ao obter QR code: {r.get('erro') or r.get('resposta')}"}
    return {"ok": True, "criando": criando, "base64": r.get("base64") or "", "code": r.get("code") or "",
            "pairingCode": r.get("pairingCode") or "", "resposta": r.get("resposta")}
