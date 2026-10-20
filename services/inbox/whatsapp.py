"""
Alfred Inbox Assistant — WhatsApp (Evolution API).

Mensagens e áudios recebidos no WhatsApp passam pelo mesmo tratamento do e-mail:

    webhook Evolution (messages.upsert)
        ↓ normalizar_payload()
    texto | áudio → transcrever_audio()   (gemma4 via Ollama; faster-whisper como fallback)
        ↓ classificar_whatsapp()          heurísticas + Ollama (JSON):
        ↓                                 profissional | pessoal | grupo | automatico | spam
    enfileirar_mensagem()                 espera `atraso_minutos` sem resposta de Pedro (logs/whatsapp_fila.json);
        ↓ processar_fila()                fromMe na mesma conversa cancela — nada é gerado
    criar_pendencia_whatsapp()            texto explicativo + áudio "o que você quer responder?"
        ↓                                 pendencias.yml: tipo=whatsapp, status=aguardando, resposta_texto=null
    responder_texto()                     registra a resposta de Pedro e envia pela Evolution API

A resposta de Pedro chega pelo MESMO webhook: ele cita (responde) a mensagem em que
Alfred resumiu a conversa — essa mensagem carrega "ref: <id>" — e o texto citado vira
responder_texto(). Se Pedro responder o contato direto do celular, a pendência fecha
como "respondida_manual" (ou, se ainda estava na fila, sai dela). "ignorar" fecha sem enviar nada.

Reutiliza core.py (pendencias.yml, vault, templates, TTS, notificação). Nenhuma função
levanta exceção por falha de LLM/rede: degrada com log() e segue.
"""

import base64
import functools
import json
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared.lib_alfred import log, ollama_generate, ollama_generate_json  # noqa: E402
from _shared.lib_audio import transcrever_audio  # noqa: E402
from _shared.lib_templates import render_template  # noqa: E402
from _shared.lib_tts import tts_save, limpar_para_fala  # noqa: E402
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

# O /whatsapp/webhook-async processa cada mensagem numa thread; criar_pendencia_whatsapp lê o
# pendencias.yml, gasta ~15 s em LLM + TTS e só então grava — duas mensagens ao mesmo tempo
# apagavam uma das pendências. Também garante que o eco do envio só é tratado depois de
# pergunta_msg_id gravado. RLock: processar → responder_texto / notificar_pedro reentra.
_LOCK = threading.RLock()


def _serializado(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)
    return wrapper

CATEGORIAS_WA = ("profissional", "pessoal", "grupo", "automatico", "spam")
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

def transcrever_mensagem(msg: dict, cfg: dict) -> dict:
    """Áudio do webhook (base64) ou baixado da Evolution → transcrever_audio()."""
    audio = msg.get("audio") or {}
    b64 = audio.get("base64")
    mimetype = audio.get("mimetype") or "audio/ogg"
    if not b64:
        r = baixar_midia(msg.get("key") or {}, audio.get("mensagem"))
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
    r = transcrever_audio(dados, mimetype, max_segundos=int(cfg.get("audio_max_segundos", 600)))
    if r.get("texto"):
        log(f"[whatsapp] transcrição ({r['metodo']}): {r['texto'][:100]!r}")
    return r


# ══════════════════════════════════════════════════════════════
#  Classificação (mesmo padrão de core.classificar, prompt para mensagens curtas)
# ══════════════════════════════════════════════════════════════

def _na_lista(msg: dict, lista) -> bool:
    alvo = f"{msg.get('numero','')} {msg.get('nome','')} {msg.get('remote_jid','')}".lower()
    return any(str(s).lower().strip() and str(s).lower().strip() in alvo for s in (lista or []))


def classificar_whatsapp(msg: dict, cfg: dict, texto: Optional[str] = None) -> dict:
    """
    Retorna {"categoria", "confianca", "requer_resposta", "urgencia", "resumo", "motivo", "origem"}.
    categoria ∈ CATEGORIAS_WA ∪ {"ignorado", "outro"}; origem = "heuristica" | "llm".
    """
    texto = (texto if texto is not None else msg.get("texto") or "").strip()
    contato = _contato_legivel(msg)

    def h(cat, resumo, motivo, requer=False, urg="baixa"):
        return {"categoria": cat, "confianca": 1.0, "requer_resposta": requer, "urgencia": urg,
                "resumo": resumo[:300], "motivo": motivo, "origem": "heuristica"}

    if _na_lista(msg, cfg.get("contatos_ignorar")):
        return h("ignorado", "contato na lista de ignorados", "contatos_ignorar")
    if msg.get("tipo_msg") in _IGNORAR_TIPOS:
        return h("outro", f"{msg['tipo_msg']} sem texto", "tipo de mensagem sem conteúdo")
    if msg.get("grupo"):
        modo = str(cfg.get("grupos") or "ignorar")
        pedro = numero_pedro() or msg.get("dono") or ""
        mencionado = bool(pedro) and any(jid_para_numero(j) == pedro for j in msg.get("mencoes", []))
        if modo == "ignorar" or (modo == "mencao" and not mencionado):
            return h("grupo", f"mensagem de grupo ({msg.get('remote_jid')})", f"grupos: {modo}")
        if modo == "mencao":
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
                                schema=SCHEMA_CLASSIFICACAO_WA, options={**core.OPTS_JSON, "num_predict": 400})
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
    return {
        "categoria": cat, "confianca": round(conf, 2),
        "requer_resposta": bool(data.get("requer_resposta", True)),
        "urgencia": urg if urg in ("baixa", "media", "alta") else "baixa",
        "resumo": str(data.get("resumo", "")).strip()[:300] or f"{contato}: {texto[:160]}",
        "motivo": motivo[:200], "origem": "llm",
    }


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
                                options={**core.OPTS_JSON, "temperature": 0.3, "num_predict": 300})
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
    pcfg   = cfg.get("pendencias") or {}
    perfil = cfg.get("_perfil") or {}
    now    = core._now()
    data   = core._load_pend()
    contato  = _contato_legivel(msg)
    conteudo = (msg.get("texto") or (transcricao or {}).get("texto") or "").strip()
    mensagens = mensagens or [{"message_id": msg.get("message_id"), "tipo": msg.get("tipo_msg"),
                               "texto": conteudo, "em": msg.get("timestamp")}]
    desc = _descricao_tipo(msg, transcricao) if len(mensagens) == 1 else _descricao_varias(mensagens)
    pid = core._novo_id(_base_id(contato, cat.get("resumo") or conteudo), {p.get("id") for p in data["pendencias"]})
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
        "historico":        "nenhuma",
    })
    log(f"[whatsapp] Escrevendo pergunta para '{titulo[:60]}'...")
    texto = ollama_generate(prompt, timeout=180) or _texto_fallback_wa(msg, cat, conteudo)
    sugestao = gerar_rascunho(contato, conteudo, desc, cfg)

    pd = core.pend_dir()
    md_path, mp3_path = pd / f"{pid}.md", pd / f"{pid}.mp3"

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
        f"- Chat (Open WebUI, tool Alfred Pendências): \"responder pendência {pid}: <texto>\"",
        f"- Para não responder: `responder-texto {pid} ignorar`",
        f"- Áudio da pergunta: `services/inbox/run.sh ouvir {pid}`",
        "",
    ])
    md_path.write_text(md, encoding="utf-8")

    roteiro = ollama_generate(render_template(pcfg.get("template_audio", "audio_pergunta_whatsapp"),
                                              {"texto": texto, "contato": contato}), timeout=120)
    if not roteiro:
        roteiro = limpar_para_fala(texto)
    tem_audio = tts_save(roteiro, pcfg.get("tts_voice", "pt-BR-AntonioNeural"), mp3_path)

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
        "arquivos":       {"md": f"pendencias/{md_path.name}", "mp3": f"pendencias/{mp3_path.name}" if tem_audio else None},
        "resposta_links": None,
        "lembrado_em":    None,
        "respondido_em":  None,
        "resposta":       None,
        "resposta_texto": None,
        "nota":           None,
    }
    data["pendencias"].append(entry)
    core._save_pend(data)
    log(f"[whatsapp] Pendência {pid} criada ({cat.get('categoria')}, {cat.get('urgencia')}) — md{' + mp3' if tem_audio else ''}")
    return entry


def agrupar_mensagem(entry_id: str, msg: dict, conteudo: str) -> dict:
    """Nova mensagem do mesmo contato → entra na pendência aberta (sem novo LLM/áudio)."""
    data = core._load_pend()
    entry = next((p for p in data["pendencias"] if p.get("id") == entry_id), None)
    if not entry:
        raise KeyError(entry_id)
    wa = entry.setdefault("whatsapp", {})
    wa.setdefault("mensagens", []).append({"message_id": msg.get("message_id"), "tipo": msg.get("tipo_msg"),
                                           "texto": conteudo, "em": msg.get("timestamp")})
    wa["atualizado_em"] = core._now().isoformat(timespec="minutes")
    wa["texto"] = ((wa.get("texto") or "") + "\n" + conteudo).strip()
    wa["key"] = msg.get("key") or wa.get("key")               # a resposta cita a última mensagem
    if conteudo:
        wa["quoted_message"] = {"conversation": conteudo[:500]}
    core._save_pend(data)
    md = core.vault() / ((entry.get("arquivos") or {}).get("md") or "")
    if md.is_file():
        txt = md.read_text(encoding="utf-8").rstrip()
        if "## Mensagens posteriores" not in txt:
            txt += "\n\n## Mensagens posteriores\n"
        txt += f"\n- {msg.get('timestamp', '')[:16]} ({msg.get('tipo_msg')}): {conteudo or '(sem texto)'}"
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
# na fila. Uma mensagem fromMe na mesma conversa cancela o item; mensagens seguidas do contato
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
    fila = _load_fila()
    now = core._now()
    m = dict(msg)
    if m.get("audio"):   # já transcrito: a fila não guarda o base64 nem a mediaKey
        m["audio"] = {k: v for k, v in m["audio"].items() if k not in ("base64", "mensagem")}
    parte = {"msg": m, "conteudo": conteudo, "cat": cat, "transcricao": transcricao,
             "recebido_em": now.isoformat(timespec="seconds")}
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


def cancelar_fila(remote_jid: str) -> list[str]:
    """Pedro escreveu na conversa antes do prazo → o contato sai da fila. → message_ids cancelados."""
    fila = _load_fila()
    fora = [i for i in fila if i.get("remote_jid") == remote_jid]
    if not fora:
        return []
    _save_fila([i for i in fila if i.get("remote_jid") != remote_jid])
    ids = [p["msg"].get("message_id") for i in fora for p in i.get("mensagens") or []]
    log(f"[whatsapp] Pedro respondeu {fora[0].get('contato')} antes do prazo — {len(ids)} mensagem(ns) saíram da fila")
    return ids


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


@_serializado
def processar_fila(todos: bool = False, enviar: Optional[bool] = None) -> list[dict]:
    """
    Itens com prazo vencido (todos=True: a fila inteira) → pendência + áudio + avisos, com as opções que o
    webhook pediu (enviar=False sobrepõe: nunca chama a Evolution). O item só sai da fila depois da pendência
    gravada; se der erro, tenta de novo na próxima volta (até 3 vezes).
    """
    fila = _load_fila()
    agora = core._now()
    resultados = []
    for item in [i for i in fila if todos or (core._parse_dt(i.get("prazo")) or agora) <= agora]:
        try:
            resultados.append(_criar_do_item(item, enviar))
            fila.remove(item)
        except Exception as e:
            item["tentativas"] = int(item.get("tentativas") or 0) + 1
            log(f"[whatsapp] fila: erro criando a pendência de {item.get('contato')} "
                f"(tentativa {item['tentativas']}/3): {e}")
            if item["tentativas"] >= 3:
                fila.remove(item)
                resultados.append({"acao": "erro", "fila": {"id": item.get("id")}, "motivo": str(e)[:200]})
        _save_fila(fila)
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
    mp3 = (entry.get("arquivos") or {}).get("mp3")
    return {
        "numero":       pedro or None,
        "texto":        texto,
        "pendencia_id": entry["id"],
        "audio_url":    f"/pendencias/{entry['id']}/audio" if (mp3 and not agrupada) else None,
        "audio_path":   str(core.vault() / mp3) if (mp3 and not agrupada) else None,
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
    r = enviar_texto(destino, notif["texto"])
    res["envio_texto"] = r
    if r.get("ok") and r.get("message_id") and not agrupada:
        entry.setdefault("whatsapp", {})["pergunta_msg_id"] = r["message_id"]
        entry["lembrado_em"] = core._now().isoformat(timespec="minutes")
        core._save_pend(data)
    if notif.get("audio_path") and Path(notif["audio_path"]).exists():
        res["envio_audio"] = enviar_audio(destino, Path(notif["audio_path"]))
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
    Levanta KeyError (id) / ValueError (tipo, status, texto vazio).
    """
    texto = (texto or "").strip()
    data = core._load_pend()
    entry = next((p for p in data["pendencias"] if p.get("id") == pid), None)
    if not entry:
        raise KeyError(f"Pendência não encontrada: {pid}")
    if entry.get("tipo") != "whatsapp":
        raise ValueError(f"{pid} não é pendência de WhatsApp — use responder {pid} vou|nao-vou")
    if entry.get("status") != "aguardando":
        raise ValueError(f"Pendência {pid} já está com status '{entry.get('status')}'")
    if not texto:
        raise ValueError("Texto da resposta vazio")

    now = core._now()
    wa = entry.setdefault("whatsapp", {})
    entry["respondido_em"] = now.isoformat(timespec="minutes")
    entry["nota"] = (nota or "").strip() or None

    if IGNORAR_RE.match(texto):
        entry["status"] = "ignorada"
        entry["resposta"] = "ignorada"
        core._save_pend(data)
        _anexar_resposta_md(entry, "IGNORADA", None, now, origem)
        log(f"[whatsapp] {pid} → ignorada")
        return entry

    entry["resposta_texto"] = texto
    entry["resposta"] = "texto"
    entry["status"] = "respondida"
    wa["envio"] = {"solicitado_em": now.isoformat(timespec="minutes"), "enviar": bool(enviar),
                   "ok": None, "simulado": None, "message_id": None, "erro": None}
    core._save_pend(data)   # salva ANTES de enviar: o eco fromMe do próprio envio não deve reabrir nada

    if enviar:
        destino = wa.get("remote_jid") if wa.get("grupo") else (wa.get("numero") or jid_para_numero(wa.get("remote_jid")))
        quoted = {"key": wa.get("key"), "message": wa.get("quoted_message") or {}} if wa.get("key") else None
        r = enviar_texto(destino, texto, quoted=quoted)
        wa["envio"].update({
            "ok": bool(r.get("ok")), "simulado": bool(r.get("simulado")), "message_id": r.get("message_id"),
            "erro": None if r.get("ok") else (r.get("erro") or str(r.get("resposta"))[:200]),
            "destino": destino,
        })
        if not r.get("ok"):
            entry["status"] = "aguardando"    # resposta guardada; repetir o comando reenvia
            entry["resposta"] = None
            core._save_pend(data)
            log(f"[whatsapp] {pid}: envio FALHOU ({wa['envio']['erro']}) — pendência continua aguardando")
            return entry
        core._save_pend(data)
        rot = "RESPONDIDA (envio simulado)" if r.get("simulado") else "RESPONDIDA (enviada pelo WhatsApp)"
    else:
        rot = "RESPONDIDA (sem envio)"
    _anexar_resposta_md(entry, rot, texto, now, origem)
    log(f"[whatsapp] {pid} → respondida" + (" (simulado)" if wa["envio"].get("simulado") else ""))
    return entry


# ══════════════════════════════════════════════════════════════
#  Agenda ("anotar para mais tarde") — botão Agenda da notificação no PC
# ══════════════════════════════════════════════════════════════

_DIAS_SEMANA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def interpretar_quando(texto: str, agora: Optional[datetime] = None) -> Optional[tuple[datetime, bool]]:
    """
    '20/10 14:00' · '20/10/2026' · '2026-10-20 09:30' · 'amanhã 10h' · 'hoje 18:00' · 'terça 15h' · '16:00'
    → (datetime local, tem_hora). None se não entender. Datas sem ano que já passaram vão para o ano seguinte.
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
        if not m.group(3) and datetime(ano, dia[1], dia[2]).date() < agora.date():
            dia = (ano + 1, dia[1], dia[2])
    elif "depois de amanha" in t_sem_hora:
        d = agora.date() + timedelta(days=2); dia = (d.year, d.month, d.day)
    elif "amanha" in t_sem_hora:
        d = agora.date() + timedelta(days=1); dia = (d.year, d.month, d.day)
    elif "hoje" in t_sem_hora or (tem_hora and not re.search(r"[a-z]{4,}", t_sem_hora)):
        dia = (agora.year, agora.month, agora.day)
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
    whatsapp.lembrar_em (a notificação volta nesse horário; a pendência continua aguardando) e devolve o
    link do Google Agenda. → {"task_id", "lembrar_em", "google_url", "titulo"}.
    """
    data = core._load_pend()
    entry = next((p for p in data["pendencias"] if p.get("id") == pid), None)
    if not entry:
        raise KeyError(f"Pendência não encontrada: {pid}")
    if entry.get("status") != "aguardando":
        raise ValueError(f"Pendência {pid} já está com status '{entry.get('status')}'")
    wa = entry.setdefault("whatsapp", {})
    contato = wa.get("contato") or entry.get("titulo")
    titulo = (titulo or ((wa.get("compromisso") or {}).get("titulo")) or f"Responder {contato} (WhatsApp)").strip()
    resumo = (entry.get("evento") or {}).get("resumo") or ""
    detalhes = f"{resumo}\n\nMensagem de {contato}: “{(wa.get('texto') or '')[:400]}”\nPendência Alfred: {pid}"
    tid = core._add_task(f"{titulo}" + (f" — {quando:%H:%M}" if tem_hora else ""),
                         quando.strftime("%Y-%m-%d"), ["whatsapp", "alfred", core._ascii(contato).lower()[:30]])
    url = google_agenda_url(titulo, quando, tem_hora, detalhes)
    wa["lembrar_em"] = quando.isoformat(timespec="minutes")
    wa["agenda"] = {"task_id": tid, "quando": wa["lembrar_em"], "tem_hora": tem_hora, "titulo": titulo,
                    "criado_em": core._now().isoformat(timespec="minutes")}
    core._save_pend(data)
    _anexar_agenda_md(entry, titulo, quando, tem_hora, tid)
    log(f"[whatsapp] {pid} → agenda: tarefa #{tid}, lembrete {wa['lembrar_em']}")
    return {"task_id": tid, "lembrar_em": wa["lembrar_em"], "google_url": url, "titulo": titulo}


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


def _fechar_manual(pid: str, texto: str, msg: dict) -> dict:
    """Pedro respondeu o contato direto do celular → fecha a pendência sem enviar nada."""
    data = core._load_pend()
    entry = next((p for p in data["pendencias"] if p.get("id") == pid), None)
    now = core._now()
    entry["status"] = "respondida_manual"
    entry["resposta"] = "texto"
    entry["resposta_texto"] = texto or f"({msg.get('tipo_msg')} enviado direto pelo celular)"
    entry["respondido_em"] = now.isoformat(timespec="minutes")
    entry.setdefault("whatsapp", {})["envio"] = {"manual": True, "message_id": msg.get("message_id")}
    core._save_pend(data)
    _anexar_resposta_md(entry, "RESPONDIDA DIRETO NO CELULAR", texto, now, "fromMe")
    log(f"[whatsapp] {pid} → respondida_manual (Pedro respondeu direto)")
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
    if True:   # @_serializado: mesmo lock do processamento de mensagens
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


def _nota_para_mim(msg: dict, cfg: dict, res: dict, enviar: bool, notificar: bool) -> dict:
    """Pedro escreveu para si mesmo: se for lembrete/tarefa/evento com data, vai para a agenda."""
    if _eco_do_alfred(msg):
        res["motivo"] = "eco de mensagem enviada pelo Alfred (conversa você)"
        return res
    texto = msg.get("texto") or ""
    if msg.get("tipo_msg") == "audio":
        tr = transcrever_mensagem(msg, cfg)
        res["transcricao"] = {k: tr.get(k) for k in ("texto", "metodo", "duracao_s", "erro")}
        texto = tr.get("texto") or ""
    ag = _detectar_e_agendar(texto, "Pedro", msg, para_mim=True)
    if not ag:
        res["motivo"] = "nota de Pedro para si mesmo sem compromisso com data"
        return res
    res["agenda"] = {k: ag.get(k) for k in ("id", "titulo", "inicio", "status", "_novo")}
    res.update(acao="agenda", motivo="lembrete anotado por Pedro")
    conf = (f"📅 Anotado na agenda: *{ag['titulo']}* — {agenda._quando_legivel(ag)}"
            + ("" if ag.get("_novo") else " (já estava)"))
    destino = msg.get("dono") or numero_pedro()
    res["notificacao"] = {"numero": destino, "texto": conf, "pendencia_id": None, "audio_url": None}
    if notificar and enviar and destino:
        res["envio_confirmacao"] = enviar_texto(destino, conf)
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
    vencido — com as mesmas opções enviar/notificar/notificar_desktop.
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
    nome, cfg = carregar_cfg(cfg_name)
    mid = msg.get("message_id") or ""

    # ── eco de algo que o próprio Alfred enviou ─────────────────
    if msg.get("from_me") and mid:
        for p in core.listar_pendencias("todas"):
            wa = p.get("whatsapp") or {}
            if mid in (wa.get("pergunta_msg_id"), (wa.get("envio") or {}).get("message_id")):
                res["motivo"] = "eco de mensagem enviada pelo Alfred"
                return res

    # ── Pedro escreveu na conversa: o que esperava na fila daquele contato não vira pendência ──
    # (o eco de uma resposta enviada pelo Alfred não conta: Pedro respondeu a pendência anterior e
    #  pode nem ter visto a mensagem nova, que ainda não foi avisada)
    cancelados = cancelar_fila(msg.get("remote_jid")) if msg.get("from_me") else []
    if cancelados:
        res["fila_cancelada"] = cancelados

    def _sem_pendencia(motivo: str) -> dict:
        if cancelados:
            res.update(acao="fila_cancelada", motivo=f"Pedro respondeu a conversa antes do prazo — "
                                                     f"{len(cancelados)} mensagem(ns) saíram da fila")
        else:
            res["motivo"] = motivo
        return res

    # ── mensagem de Pedro ───────────────────────────────────────
    if _eh_pedro(msg):
        ref = _ref_citada(msg)
        if ref:
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
                res["envio_confirmacao"] = enviar_texto(res["notificacao"]["numero"], conf)
            return res
        if _conversa_comigo(msg):
            return _nota_para_mim(msg, cfg, res, enviar=enviar, notificar=notificar)
        if msg.get("from_me") and not msg.get("grupo"):
            p = _pendencia_aguardando_do_jid(msg.get("remote_jid"))
            if p:
                res["pendencia"] = _fechar_manual(p["id"], msg.get("texto") or "", msg)
                res.update(acao="respondida_manual", motivo="Pedro respondeu o contato direto do celular")
                return res
        return _sem_pendencia("mensagem de Pedro sem citação de pendência")

    if msg.get("from_me"):
        p = _pendencia_aguardando_do_jid(msg.get("remote_jid")) if not msg.get("grupo") else None
        if p:
            res["pendencia"] = _fechar_manual(p["id"], msg.get("texto") or "", msg)
            res.update(acao="respondida_manual", motivo="Pedro respondeu o contato direto do celular")
            return res
        return _sem_pendencia("mensagem enviada por mim (fromMe) sem pendência aberta")

    # ── dedup ───────────────────────────────────────────────────
    seen = core.load_seen(SEEN_FILE)
    existentes = core._load_pend()["pendencias"]
    if mid and (mid in seen or _ja_processada(existentes, mid)):
        res.update(acao="duplicada", motivo="mensagem já processada")
        return res

    # ── transcrição ─────────────────────────────────────────────
    conteudo = msg.get("texto") or ""
    transcricao = None
    if msg.get("tipo_msg") == "audio":
        transcricao = transcrever_mensagem(msg, cfg)
        res["transcricao"] = {k: transcricao.get(k) for k in ("texto", "metodo", "duracao_s", "segmentos", "cortado", "erro")}
        conteudo = transcricao.get("texto") or ""

    # ── classificação ───────────────────────────────────────────
    cat = classificar_whatsapp(msg, cfg, texto=conteudo)
    res["classificacao"] = cat
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
    aberta = _pendencia_aberta_do_contato(existentes, msg, cfg)
    if aberta:
        entry = agrupar_mensagem(aberta["id"], msg, conteudo)
        agr = {"tipo_msg": msg.get("tipo_msg"), "texto": conteudo}
        res.update(acao="agrupada", motivo=f"contato já tem a pendência {aberta['id']} aberta", pendencia=entry,
                   notificacao=mensagem_para_pedro(entry, agrupada=agr))
        if notificar and enviar:
            res["envio"] = notificar_pedro(entry["id"], agrupada=agr, enviar=True)
        return res

    # ── espera atraso_minutos: se Pedro responder antes, nada é gerado ──
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


def status() -> dict:
    """Resumo para /health e CLI."""
    from _shared.lib_whatsapp import resumo_config, estado_instancia
    cfg = resumo_config()
    if cfg["configurado"]:
        est = estado_instancia()
        cfg["estado_instancia"] = est.get("estado") or est.get("erro") or f"HTTP {est.get('status')}"
    aguard = [p for p in core.listar_pendencias("aguardando") if p.get("tipo") == "whatsapp"]
    cfg["pendencias_whatsapp_aguardando"] = len(aguard)
    cfg["fila_espera"] = listar_fila()
    return cfg
