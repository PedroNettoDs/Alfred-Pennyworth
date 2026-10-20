"""
Alfred — cliente da Evolution API (WhatsApp).

    from _shared.lib_whatsapp import enviar_texto, enviar_audio, baixar_midia, estado_instancia

Configuração (.env):
  EVOLUTION_URL          http://localhost:8080 — vazio = modo simulado (nada sai da máquina)
  EVOLUTION_API_KEY      AUTHENTICATION_API_KEY do container (header `apikey`)
  EVOLUTION_INSTANCE     nome da instância pareada por QR code (default: alfred)
  WHATSAPP_NUMERO_PEDRO  número de Pedro com DDI, só dígitos — recebe os resumos/áudios
  WHATSAPP_DRY_RUN       true = simula mesmo com EVOLUTION_URL configurada

No modo simulado cada envio é registrado em logs/whatsapp_enviados.jsonl e devolvido
como {"ok": True, "simulado": True}. Nenhuma função levanta exceção: erros voltam em
{"ok": False, "erro": "..."}.

Endpoints usados (Evolution API v2):
  POST /message/sendText/{instancia}              {number, text, quoted?, delay?}
  POST /message/sendWhatsAppAudio/{instancia}     {number, audio (base64|url), encoding}
  POST /chat/getBase64FromMediaMessage/{inst}     {message: {key, message}, convertToMp4}
  GET  /instance/connectionState/{instancia}
  GET  /instance/connect/{instancia}              → QR code
  POST /instance/create · POST /webhook/set/{instancia}
"""

import base64
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import httpx

from _shared.lib_alfred import log, PROJECT_ROOT

ENVIADOS_FILE = PROJECT_ROOT / "logs" / "whatsapp_enviados.jsonl"


# ── Configuração ──────────────────────────────────────────────

def url_base() -> str:
    return os.getenv("EVOLUTION_URL", "").strip().rstrip("/")


def api_key() -> str:
    return os.getenv("EVOLUTION_API_KEY", "").strip()


def instancia() -> str:
    return os.getenv("EVOLUTION_INSTANCE", "").strip() or "alfred"


def numero_pedro() -> str:
    return so_digitos(os.getenv("WHATSAPP_NUMERO_PEDRO", ""))


def dry_run() -> bool:
    return os.getenv("WHATSAPP_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


def configurado() -> bool:
    return bool(url_base() and api_key())


def simulado() -> bool:
    """True quando os envios NÃO vão para a Evolution API (sem URL/chave ou WHATSAPP_DRY_RUN)."""
    return dry_run() or not configurado()


def resumo_config() -> dict:
    return {
        "configurado": configurado(), "simulado": simulado(), "dry_run": dry_run(),
        "url": url_base() or None, "instancia": instancia(),
        "numero_pedro": numero_pedro() or None,
    }


# ── Números e JIDs ────────────────────────────────────────────

def so_digitos(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def jid_para_numero(jid) -> str:
    """'5514999998888@s.whatsapp.net' → '5514999998888'. Grupos (@g.us) devolvem o id do grupo."""
    return so_digitos(str(jid or "").split("@")[0].split(":")[0])


def numero_para_jid(numero) -> str:
    d = so_digitos(numero)
    return f"{d}@s.whatsapp.net" if d else ""


def eh_grupo(jid) -> bool:
    return str(jid or "").endswith("@g.us")


def formatar_numero(numero) -> str:
    """Formato legível para números brasileiros; outros voltam com '+'."""
    d = so_digitos(numero)
    if not d:
        return ""
    if d.startswith("55") and len(d) in (12, 13):
        ddd, resto = d[2:4], d[4:]
        meio = f"{resto[:-4]}-{resto[-4:]}"
        return f"+55 {ddd} {meio}"
    return f"+{d}"


# ── HTTP ──────────────────────────────────────────────────────

def _headers() -> dict:
    return {"apikey": api_key(), "Content-Type": "application/json"}


def _post(path: str, body: dict, timeout: int = 60) -> dict:
    if not configurado():
        return {"ok": False, "erro": "EVOLUTION_URL/EVOLUTION_API_KEY não configurados"}
    try:
        r = httpx.post(f"{url_base()}{path}", json=body, headers=_headers(), timeout=timeout)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:300]}
        ok = r.status_code < 300
        if not ok:
            log(f"[whatsapp] POST {path} → HTTP {r.status_code}: {str(data)[:200]}")
        return {"ok": ok, "status": r.status_code, "resposta": data}
    except Exception as e:
        log(f"[whatsapp] POST {path} falhou: {e}")
        return {"ok": False, "erro": str(e)}


def _get(path: str, timeout: int = 30) -> dict:
    if not configurado():
        return {"ok": False, "erro": "EVOLUTION_URL/EVOLUTION_API_KEY não configurados"}
    try:
        r = httpx.get(f"{url_base()}{path}", headers=_headers(), timeout=timeout)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:300]}
        return {"ok": r.status_code < 300, "status": r.status_code, "resposta": data}
    except Exception as e:
        log(f"[whatsapp] GET {path} falhou: {e}")
        return {"ok": False, "erro": str(e)}


def _message_id(resposta) -> Optional[str]:
    if isinstance(resposta, dict):
        key = resposta.get("key") or {}
        if isinstance(key, dict) and key.get("id"):
            return str(key["id"])
    return None


def _registrar_simulado(tipo: str, numero: str, conteudo: str, extra: Optional[dict] = None) -> str:
    mid = f"SIMULADO-{datetime.now():%Y%m%d%H%M%S%f}"
    reg = {"em": datetime.now().astimezone().isoformat(timespec="seconds"), "tipo": tipo, "numero": numero,
           "instancia": instancia(), "conteudo": conteudo, "message_id": mid, **(extra or {})}
    try:
        ENVIADOS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ENVIADOS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"[whatsapp] não consegui registrar envio simulado: {e}")
    log(f"[whatsapp] (simulado) {tipo} → {numero}: {str(conteudo)[:90]!r}")
    return mid


# ── Envio ─────────────────────────────────────────────────────

def enviar_texto(numero: str, texto: str, quoted: Optional[dict] = None, delay: int = 0) -> dict:
    """
    POST /message/sendText. `numero` = dígitos com DDI ou JID de grupo.
    `quoted` = {"key": {...}, "message": {...}} da mensagem original → aparece como resposta no WhatsApp.
    Retorna {"ok", "simulado", "message_id", "status"?, "resposta"?, "erro"?}.
    """
    destino = numero if eh_grupo(numero) else so_digitos(numero)
    texto = (texto or "").strip()
    if not destino or not texto:
        return {"ok": False, "simulado": simulado(), "erro": "número ou texto vazio"}
    body: dict = {"number": destino, "text": texto}
    if delay:
        body["delay"] = int(delay)
    if quoted and isinstance(quoted.get("key"), dict):
        body["quoted"] = {"key": quoted["key"], "message": quoted.get("message") or {}}
    if simulado():
        mid = _registrar_simulado("texto", destino, texto, {"quoted": bool(quoted)})
        return {"ok": True, "simulado": True, "message_id": mid}
    r = _post(f"/message/sendText/{instancia()}", body)
    r["simulado"] = False
    r["message_id"] = _message_id(r.get("resposta"))
    return r


def enviar_audio(numero: str, audio: Union[Path, str, bytes], ptt: bool = True) -> dict:
    """
    POST /message/sendWhatsAppAudio — `audio` pode ser caminho de arquivo, bytes, base64 ou URL.
    `encoding: true` faz a Evolution converter para OGG/Opus (aparece como mensagem de voz).
    """
    destino = numero if eh_grupo(numero) else so_digitos(numero)
    if not destino:
        return {"ok": False, "simulado": simulado(), "erro": "número vazio"}
    tamanho = None
    if isinstance(audio, bytes):
        conteudo = base64.b64encode(audio).decode("ascii"); tamanho = len(audio)
    elif isinstance(audio, Path) or (isinstance(audio, str) and Path(audio).is_file()):
        p = Path(audio)
        conteudo = base64.b64encode(p.read_bytes()).decode("ascii"); tamanho = p.stat().st_size
    else:
        conteudo = str(audio or "")
    if not conteudo:
        return {"ok": False, "simulado": simulado(), "erro": "áudio vazio"}
    if simulado():
        mid = _registrar_simulado("audio", destino, f"<áudio {tamanho or len(conteudo)} bytes>")
        return {"ok": True, "simulado": True, "message_id": mid}
    body = {"number": destino, "audio": conteudo, "encoding": bool(ptt)}
    r = _post(f"/message/sendWhatsAppAudio/{instancia()}", body, timeout=180)
    r["simulado"] = False
    r["message_id"] = _message_id(r.get("resposta"))
    return r


# ── Mídia recebida ────────────────────────────────────────────

def baixar_midia(key: dict, message: Optional[dict] = None) -> dict:
    """
    POST /chat/getBase64FromMediaMessage → {"ok", "base64", "mimetype"}.
    Usado quando o webhook veio sem `base64`. `message` = bloco original do payload (com mediaKey);
    sem ele a Evolution busca a mensagem no próprio banco pelo key.id (DATABASE_SAVE_DATA_NEW_MESSAGE).
    Nunca mande `message: {}` — a Evolution tenta decifrar a mensagem vazia ("empty media key").
    """
    if not isinstance(key, dict) or not key.get("id"):
        return {"ok": False, "erro": "key.id ausente"}
    alvo: dict = {"key": key}
    if message:
        alvo["message"] = message
    body = {"message": alvo, "convertToMp4": False}
    r = _post(f"/chat/getBase64FromMediaMessage/{instancia()}", body, timeout=120)
    if not r.get("ok"):
        return r
    resp = r.get("resposta") or {}
    return {"ok": bool(resp.get("base64")), "base64": resp.get("base64"), "mimetype": resp.get("mimetype") or "",
            "nome": resp.get("fileName") or ""}


# ── Instância (pareamento / webhook) ──────────────────────────

def estado_instancia() -> dict:
    r = _get(f"/instance/connectionState/{instancia()}")
    if r.get("ok"):
        inst = (r.get("resposta") or {}).get("instance") or {}
        r["estado"] = inst.get("state") or "desconhecido"
    return r


def qrcode_instancia() -> dict:
    """GET /instance/connect → {"code": texto do QR, "base64": PNG data-URI, "pairingCode"?}."""
    r = _get(f"/instance/connect/{instancia()}", timeout=60)
    if r.get("ok"):
        resp = r.get("resposta") or {}
        r.update({"code": resp.get("code"), "base64": resp.get("base64"), "pairingCode": resp.get("pairingCode")})
    return r


def criar_instancia(webhook_url: Optional[str] = None, webhook_headers: Optional[dict] = None,
                    numero: Optional[str] = None) -> dict:
    """
    POST /instance/create — integração Baileys (QR code). O `token` da instância é a própria
    EVOLUTION_API_KEY, assim o campo `apikey` que vem no webhook é validado pelo /whatsapp/webhook.
    """
    body: dict = {"instanceName": instancia(), "integration": "WHATSAPP-BAILEYS", "qrcode": True,
                  "token": api_key()}
    if numero:
        body["number"] = so_digitos(numero)
    if webhook_url:
        body["webhook"] = _webhook_cfg(webhook_url, webhook_headers)
    return _post("/instance/create", body, timeout=120)


def _webhook_cfg(url: str, headers: Optional[dict] = None, base64_midia: bool = True) -> dict:
    return {"enabled": True, "url": url, "byEvents": False, "base64": base64_midia,
            "headers": headers or {}, "events": ["MESSAGES_UPSERT"]}


def configurar_webhook(url: str, headers: Optional[dict] = None, base64_midia: bool = True) -> dict:
    """POST /webhook/set/{instancia}: só MESSAGES_UPSERT, com mídia em base64 no próprio payload."""
    return _post(f"/webhook/set/{instancia()}", {"webhook": _webhook_cfg(url, headers, base64_midia)})


def ultimos_envios(n: int = 20) -> list[dict]:
    """Envios simulados registrados em logs/whatsapp_enviados.jsonl (útil em testes)."""
    if not ENVIADOS_FILE.exists():
        return []
    linhas = ENVIADOS_FILE.read_text(encoding="utf-8").strip().splitlines()
    out = []
    for ln in linhas[-n:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


if __name__ == "__main__":
    print(json.dumps(resumo_config(), ensure_ascii=False, indent=2))
    if configurado():
        print(json.dumps(estado_instancia(), ensure_ascii=False, indent=2))
