"""
Alfred — promessas: o que Pedro disse que ia fazer (WhatsApp e e-mail ENVIADOS) → agenda; e o que não fez → agenda de novo.

Fluxo (no fim do ciclo do e-mail, 07:30 / 18:30, e em `run.sh promessas varrer`)
    falas de Pedro                                       contexto
      WhatsApp: wa_msgs from_me (lib_db)                 as últimas falas da conversa, as do contato também
      e-mail:   pasta Enviados do Thunderbird            o trecho citado da mensagem respondida
      --retroativo: respostas de Pedro já gravadas em pendencias.yml (respondida / respondida_manual)
        ↓ prefiltro (regex de compromisso em 1ª pessoa) → Ollama (JSON Schema, promessas_detectar.md)
        ↓ o "trecho" tem de sair das falas de Pedro (não do contato) e confiança ≥ confianca_minima
        ↓ registrar()  VAULT_ALFRED/agenda/promessas.yml (status aberta)
        ↓ agenda.adicionar(tipo tarefa) no prazo prometido — sem data: prazo_padrao_dias depois da fala
    verificar() — prazo vencido:
        falas de Pedro ao mesmo contato depois da promessa (e as respostas do contato)
        ↓ nenhuma → atrasada · alguma → Ollama (promessas_verificar.md): sim → cumprida · nao/incerto → atrasada
        atrasada → agenda.adicionar("Atrasado: …", tipo tarefa) no próximo dia às 09:00
    "Desfazer" na notificação da agenda (antes do prazo) → descartada: não volta como atrasada.

Status: aberta → cumprida | atrasada | descartada ; atrasada → cumprida | descartada (CLI feita / descartar,
        Feito no painel da agenda). Cumprida → os itens dela na agenda viram feito (☑ no calendário).
        Domingo, agenda_semanal.py apaga as cumpridas e descartadas (ficam no relatório da semana).
Ordem das travas: promessas sozinha (a agenda é chamada fora dela).
"""

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

import agenda
import core
from _shared import lib_db
from _shared.lib_alfred import log, ollama_generate_json
from _shared.lib_templates import render_template
from _shared.lib_whatsapp import formatar_numero, jid_para_numero

SEEN_FILE = core.PROJECT_ROOT / "logs" / "promessas_seen.json"   # falas já analisadas (message_id / pendencia:<id>)
HORA_PADRAO = 9            # prazo sem hora e lembrete do atraso: 09:00
MAX_FALAS = 10             # falas novas de Pedro por chamada ao LLM (por conversa)
MAX_CONTEXTO = 30          # linhas da conversa que vão junto
MAX_EVIDENCIAS = 20        # mensagens depois da promessa que vão para a verificação
ABERTAS = ("aberta", "atrasada")

SCHEMA_PROMESSAS = {
    "type": "object",
    "properties": {"promessas": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "o_que":     {"type": "string"},
            "para_quem": {"type": "string"},
            "data":      {"type": "string"},
            "hora":      {"type": "string"},
            "confianca": {"type": "number"},
            "trecho":    {"type": "string"},
        },
        "required": ["o_que", "para_quem", "data", "hora", "confianca", "trecho"],
    }}},
    "required": ["promessas"],
}

SCHEMA_VEREDITO = {
    "type": "object",
    "properties": {"cumprida": {"type": "string", "enum": ["sim", "nao", "incerto"]}, "motivo": {"type": "string"}},
    "required": ["cumprida", "motivo"],
}

# Só chama o LLM se alguma fala de Pedro tiver cara de compromisso (texto já sem acento, minúsculo)
_PREFILTRO = re.compile(
    r"\b(vou|vamos|irei|farei|faco|mando|mandarei|envio|enviarei|passo|repasso|ligo|retorno|resolvo|"
    r"verifico|confirmo|te (mando|envio|passo|falo|aviso|retorno|ligo|respondo|mostro|pago|devolvo|levo|trago|"
    r"chamo|explico)|deixa comigo|pode deixar|pode ser|tenho que te|preciso te|fico de|me comprometo|prometo|"
    r"combinado|fechado|com certeza|"
    r"ate (amanha|hoje|segunda|terca|quarta|quinta|sexta|sabado|domingo|o fim|semana|dia \d))\b", re.I)

# Começo do trecho citado num e-mail de resposta (Thunderbird, Outlook, Gmail)
_CITACAO = re.compile(r"^(?:>|Em .{3,160}escreveu:|On .{3,160}wrote:|-{2,}\s*(?:Mensagem original|Original Message)"
                      r"|_{8,}|De:\s|From:\s)", re.M | re.I)


# ══════════════════════════════════════════════════════════════
#  Armazenamento
# ══════════════════════════════════════════════════════════════

def _cfg() -> dict:
    """Seção promessas: do perfis.yml (opcional) com defaults."""
    try:
        from _shared.perfis import load
        sec = load().get("promessas") or {}
    except Exception:
        sec = {}
    return {
        "ativo":             bool(sec.get("ativo", True)),
        "canais":            list(sec.get("canais") or ["whatsapp", "email"]),
        "pastas_enviados":   list(sec.get("pastas_enviados") or ["Sent"]),
        "janela_horas":      float(sec.get("janela_horas", 48)),
        "prazo_padrao_dias": float(sec.get("prazo_padrao_dias", 3)),
        "confianca_minima":  float(sec.get("confianca_minima", 0.7)),
    }


def arquivo() -> Path:
    return agenda.agenda_file().parent / "promessas.yml"


def _load() -> dict:
    f = arquivo()
    if not f.exists():
        return {"promessas": []}
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except Exception as e:
        core.preservar_corrompido(f, f"YAML inválido: {e}", "promessas")
        return {"promessas": []}
    if not isinstance(data, dict) or not isinstance(data.get("promessas"), list):
        return {"promessas": []}
    return data


def _trava():
    return core.trava_arquivo(arquivo())


def _save(data: dict) -> None:
    with _trava():
        core.gravar_atomico(arquivo(), yaml.dump(data, allow_unicode=True, sort_keys=False))


def listar(status: Optional[str] = None) -> list[dict]:
    itens = _load()["promessas"]
    if status in (None, "", "todas"):
        return itens
    return [p for p in itens if p.get("status") == status]


def obter(pid: str) -> Optional[dict]:
    return next((p for p in _load()["promessas"] if p.get("id") == pid), None)


def remover(ids) -> int:
    """Apaga do promessas.yml as cumpridas/descartadas com estes ids — relatório semanal. → quantas saíram."""
    ids = set(ids)
    with _trava():
        data = _load()
        antes = len(data["promessas"])
        data["promessas"] = [p for p in data["promessas"]
                             if not (p.get("id") in ids and p.get("status") in ("cumprida", "descartada"))]
        if len(data["promessas"]) != antes:
            _save(data)
    return antes - len(data["promessas"])


def _atualizar(pid: str, **campos) -> Optional[dict]:
    with _trava():
        data = _load()
        p = next((x for x in data["promessas"] if x.get("id") == pid), None)
        if not p:
            return None
        p.update(campos)
        p["atualizado_em"] = core._now().isoformat(timespec="minutes")
        _save(data)
    return p


# ══════════════════════════════════════════════════════════════
#  Detecção (Ollama)
# ══════════════════════════════════════════════════════════════

def prefiltro(texto: str) -> bool:
    return bool(_PREFILTRO.search(core._ascii(texto or "").lower()))


def _palavras(texto: str) -> set[str]:
    return {w for w in agenda._norm(texto).split() if len(w) >= 3}


def _sobreposicao(trecho: str, texto: str) -> float:
    """Fração das palavras do trecho que aparecem no texto (0 se o trecho não tem palavra de 3+ letras)."""
    pt = _palavras(trecho)
    return len(pt & _palavras(texto)) / len(pt) if pt else 0.0


def _prazo(data, hora, ref: datetime, dias_padrao: float) -> tuple[datetime, bool, bool]:
    """→ (prazo, dia_inteiro, assumido). Sem data válida (ou anterior à fala): ref + dias_padrao, dia inteiro."""
    dia, hora = str(data or "").strip(), str(hora or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", dia):
        tem_hora = bool(re.fullmatch(r"\d{1,2}:\d{2}", hora))
        try:
            y, m, d = (int(x) for x in dia.split("-"))
            hh, mm = (int(x) for x in hora.split(":")) if tem_hora else (HORA_PADRAO, 0)
            prazo = datetime(y, m, d, hh, mm, tzinfo=ref.tzinfo)
            if prazo.date() >= ref.date():
                return prazo, not tem_hora, False
        except ValueError:
            pass
    base = ref + timedelta(days=dias_padrao)
    return base.replace(hour=HORA_PADRAO, minute=0, second=0, microsecond=0), True, True


def detectar_promessas(falas: list[str], conversa: str, canal: str, ref: datetime) -> Optional[list[dict]]:
    """
    `falas` = o que Pedro escreveu (as linhas com » em `conversa`). → [{o_que, para_quem, prazo: datetime,
    dia_inteiro, prazo_assumido, confianca, trecho}]; [] sem promessa; None se o LLM falhou (tenta no próximo ciclo).
    """
    if not any(prefiltro(f) for f in falas):
        return []
    cfg = _cfg()
    prompt = render_template("promessas_detectar", {
        "data_ref": ref.strftime("%Y-%m-%d (%A) %H:%M"), "canal": canal, "conversa": conversa[-6000:]})
    data = ollama_generate_json(prompt, timeout=180, schema=SCHEMA_PROMESSAS,
                                options={**core.OPTS_JSON, "num_predict": 600})
    if not isinstance(data, dict) or not isinstance(data.get("promessas"), list):
        return None
    ditas = " ".join(falas)
    out = []
    for p in data["promessas"]:
        if not isinstance(p, dict):
            continue
        o_que = str(p.get("o_que") or "").strip()[:120]
        trecho = str(p.get("trecho") or "").strip()[:300]
        try:
            conf = float(p.get("confianca", 0))
        except (TypeError, ValueError):
            conf = 0.0
        if not o_que or conf < cfg["confianca_minima"]:
            log(f"[promessas] descartada (confiança {conf:.2f}): {o_que!r}")
            continue
        if _sobreposicao(trecho, ditas) < 0.6:   # trecho do contato (ou inventado) não é promessa de Pedro
            log(f"[promessas] descartada (trecho fora das falas de Pedro): {trecho!r}")
            continue
        prazo, dia_inteiro, assumido = _prazo(p.get("data"), p.get("hora"), ref, cfg["prazo_padrao_dias"])
        out.append({"o_que": o_que, "para_quem": str(p.get("para_quem") or "").strip()[:80], "prazo": prazo,
                    "dia_inteiro": dia_inteiro, "prazo_assumido": assumido, "confianca": round(conf, 2),
                    "trecho": trecho})
    return out


# ══════════════════════════════════════════════════════════════
#  Registro → agenda
# ══════════════════════════════════════════════════════════════

def _titulo(p: dict) -> str:
    return f"{p['o_que']} ({p['para_quem']})" if p.get("para_quem") else p["o_que"]


def _prazo_dt(p: dict) -> Optional[datetime]:
    return core._parse_dt(p.get("prazo"))


def _vencida(p: dict, agora: datetime) -> bool:
    prazo = _prazo_dt(p)
    if not prazo:
        return False
    return agora.date() > prazo.date() if p.get("dia_inteiro") else agora > prazo


def _quando(p: dict) -> str:
    prazo = _prazo_dt(p)
    if not prazo:
        return "?"
    return f"{prazo:%d/%m}" + ("" if p.get("dia_inteiro") else f" {prazo:%H:%M}") + (" (assumido)" if p.get("prazo_assumido") else "")


def _chave(origem: dict) -> str:
    return origem.get("remote_jid") or ",".join(sorted(origem.get("para") or [])) or origem.get("contato") or ""


def _descricao(p: dict) -> str:
    dito = core._parse_dt(p.get("dito_em"))
    via = {"whatsapp": "WhatsApp", "email": "e-mail"}.get((p.get("origem") or {}).get("canal"), "mensagem")
    return (f"Você prometeu a {p.get('para_quem') or 'alguém'} ({via}, {dito:%d/%m %H:%M}): “{p.get('trecho')}”\n"
            f"Prazo: {_quando(p)}" + ("\nSem data na mensagem: prazo padrão do Alfred." if p.get("prazo_assumido") else "")
            + f"\nPromessa Alfred: {p['id']}")


def registrar(achada: dict, origem: dict, dito_em: datetime, notificar: Optional[bool] = None,
              agora: Optional[datetime] = None) -> Optional[dict]:
    """
    Promessa detectada → promessas.yml + tarefa na agenda no prazo (se o prazo ainda não passou; senão a
    verificação cuida dela). None se a mesma promessa ao mesmo contato já está aberta ou atrasada.
    """
    agora = agora or core._now()
    with _trava():
        data = _load()
        chave = (_chave(origem), agenda._norm(achada["o_que"]))
        for x in data["promessas"]:
            if x.get("status") in ABERTAS and (_chave(x.get("origem") or {}), agenda._norm(x.get("o_que", ""))) == chave:
                log(f"[promessas] já registrada: {x['id']}")
                return None
        base = f"{dito_em:%Y%m%d}-{core.slugify(core._ascii(achada['o_que']), 32) or 'promessa'}"
        pid, n, ids = base, 2, {x.get("id") for x in data["promessas"]}
        while pid in ids:
            pid, n = f"{base}-{n}", n + 1
        item = {
            "id":             pid,
            "status":         "aberta",
            "o_que":          achada["o_que"],
            "para_quem":      achada.get("para_quem") or origem.get("contato") or "",
            "trecho":         achada["trecho"],
            "prazo":          achada["prazo"].isoformat(timespec="minutes"),
            "dia_inteiro":    bool(achada["dia_inteiro"]),
            "prazo_assumido": bool(achada["prazo_assumido"]),
            "confianca":      achada["confianca"],
            "dito_em":        dito_em.isoformat(timespec="minutes"),
            "origem":         origem,
            "agenda_id":      None,
            "agenda_atraso_id": None,
            "motivo":         None,
            "criado_em":      agora.isoformat(timespec="minutes"),
            "atualizado_em":  agora.isoformat(timespec="minutes"),
        }
        data["promessas"].append(item)
        _save(data)
    log(f"[promessas] + {pid}: {_titulo(item)!r} — prazo {_quando(item)}")
    if not _vencida(item, agora):   # prazo já passou (retroativo, "hoje às 10h" visto às 18h): verificar() cuida
        try:
            ag = agenda.adicionar(_titulo(item), achada["prazo"], dia_inteiro=item["dia_inteiro"], tipo="tarefa",
                                  descricao=_descricao(item), notificar=notificar,
                                  origem={"canal": "promessa", "contato": item["para_quem"], "promessa_id": pid,
                                          "via": origem.get("canal")})
            item = _atualizar(pid, agenda_id=ag["id"]) or item
        except Exception as e:
            log(f"[promessas] {pid} não foi para a agenda: {e}")
    return item


# ══════════════════════════════════════════════════════════════
#  Varredura
# ══════════════════════════════════════════════════════════════

def _linha(m: dict, contato: str, nova: bool) -> str:
    quando = core._parse_dt(m.get("ts")) or core._now()
    return (f"{'» ' if nova else '  '}[{quando:%d/%m %H:%M}] {'Pedro' if m['from_me'] else contato}: "
            f"{' '.join((m.get('texto') or '').split())[:400]}")


def _fala_do_trecho(trecho: str, falas: list[dict]) -> dict:
    return max(falas, key=lambda m: _sobreposicao(trecho, m.get("texto") or ""))


def varrer_whatsapp(seen: dict, desde: datetime, resumo: dict, notificar: Optional[bool] = None) -> None:
    """Falas de Pedro (wa_msgs from_me) ainda não analisadas, por conversa, com o contexto da conversa."""
    agora = core._now().isoformat(timespec="minutes")
    enviadas = [m for m in lib_db.mensagens_wa(from_me=True, desde=desde.isoformat(timespec="minutes"), limite=1000)
                if m["message_id"] not in seen]
    por_conversa: dict[str, list[dict]] = {}
    for m in enviadas:
        por_conversa.setdefault(m["remote_jid"], []).append(m)
    for jid, falas_jid in por_conversa.items():
        for i in range(0, len(falas_jid), MAX_FALAS):
            falas = falas_jid[i:i + MAX_FALAS]
            novas = {m["message_id"] for m in falas}
            # o que veio antes da 1ª fala (a pergunta do contato) + tudo entre a 1ª e a última
            antes = [m for m in lib_db.mensagens_wa(jid, ate=falas[0]["ts"], limite=8) if m["message_id"] not in novas]
            entre = lib_db.mensagens_wa(jid, desde=falas[0]["ts"], ate=falas[-1]["ts"], limite=MAX_CONTEXTO)
            vistos = {m["message_id"] for m in entre}
            ctx = [m for m in antes if m["message_id"] not in vistos] + entre
            contato = (next((m["contato"] for m in reversed(ctx) if not m["from_me"] and m["contato"]), "")
                       or formatar_numero(jid_para_numero(jid)) or jid)
            conversa = "\n".join(_linha(m, contato, m["message_id"] in novas) for m in ctx)
            ref = core._parse_dt(falas[-1]["ts"]) or core._now()
            achadas = detectar_promessas([m["texto"] for m in falas], conversa, f"conversa de WhatsApp com {contato}", ref)
            if achadas is None:
                resumo["falhas"] += 1
                continue
            for a in achadas:
                m = _fala_do_trecho(a["trecho"], falas)
                p = registrar(a, {"canal": "whatsapp", "contato": contato, "remote_jid": jid,
                                  "message_id": m["message_id"]}, core._parse_dt(m["ts"]) or ref, notificar=notificar)
                if p:
                    resumo["novas"].append(p["id"])
            for m in falas:
                seen[m["message_id"]] = agora


def varrer_pendencias(seen: dict, desde: datetime, resumo: dict, notificar: Optional[bool] = None) -> None:
    """Retroativo: respostas de Pedro já gravadas nas pendências de WhatsApp (antes de wa_msgs guardar as dele)."""
    agora = core._now().isoformat(timespec="minutes")
    for p in core.listar_pendencias("todas"):
        if p.get("tipo") != "whatsapp" or p.get("status") not in ("respondida", "respondida_manual"):
            continue
        chave, texto = f"pendencia:{p['id']}", (p.get("resposta_texto") or "").strip()
        quando = core._parse_dt(p.get("respondido_em"))
        if chave in seen or not texto or texto.startswith("(") or not quando or quando < desde:
            continue
        wa = p.get("whatsapp") or {}
        contato = wa.get("contato") or "Contato"
        do_contato = [m.get("texto") for m in (wa.get("mensagens") or []) if m.get("texto")] or [wa.get("texto") or ""]
        conversa = "\n".join([f"  {contato}: {' '.join(t.split())[:400]}" for t in do_contato if t]
                             + [f"» [{quando:%d/%m %H:%M}] Pedro: {' '.join(texto.split())[:600]}"])
        achadas = detectar_promessas([texto], conversa, f"conversa de WhatsApp com {contato}", quando)
        if achadas is None:
            resumo["falhas"] += 1
            continue
        for a in achadas:
            n = registrar(a, {"canal": "whatsapp", "contato": contato, "remote_jid": wa.get("remote_jid"),
                              "message_id": (wa.get("envio") or {}).get("message_id"), "pendencia_id": p["id"]},
                          quando, notificar=notificar)
            if n:
                resumo["novas"].append(n["id"])
        seen[chave] = agora


def separar_citacao(texto: str) -> tuple[str, str]:
    """Corpo de um e-mail enviado → (o que Pedro escreveu, trecho citado da mensagem respondida)."""
    m = _CITACAO.search(texto or "")
    return ((texto[:m.start()].strip(), texto[m.start():].strip()) if m else ((texto or "").strip(), ""))


def enviados_email(inbox_cfg: dict, desde: datetime) -> list[dict]:
    """E-mails da pasta Enviados (pastas_enviados) desde `desde` — uma leitura serve à varredura e à verificação."""
    horas = max(1, int((core._now() - desde).total_seconds() // 3600) + 1)
    return core.coletar({**inbox_cfg, "pastas": _cfg()["pastas_enviados"], "so_nao_lidos": False},
                        janela_horas=horas, max_emails=300)


def varrer_email(msgs: list[dict], seen: dict, resumo: dict, notificar: Optional[bool] = None) -> None:
    agora = core._now()
    for m in msgs:
        if m["message_id"] in seen or m.get("is_bulk"):
            continue
        proprio, citado = separar_citacao(m.get("text") or "")
        para = m.get("to") or []
        ref = m.get("date") or agora
        if proprio and para:
            conversa = (f"Assunto: {m.get('subject')}\nPara: {', '.join(para)}\n\n"
                        f"» [{ref:%d/%m %H:%M}] Pedro: {proprio[:3000]}"
                        + (f"\n\nMENSAGEM RESPONDIDA (contexto):\n{citado[:1500]}" if citado else ""))
            achadas = detectar_promessas([proprio], conversa, f"e-mail enviado para {', '.join(para[:3])}", ref)
            if achadas is None:
                resumo["falhas"] += 1
                continue
            for a in achadas:
                p = registrar(a, {"canal": "email", "contato": a["para_quem"] or para[0], "para": para,
                                  "assunto": m.get("subject"), "message_id": m["message_id"]}, ref, notificar=notificar)
                if p:
                    resumo["novas"].append(p["id"])
        seen[m["message_id"]] = agora.isoformat(timespec="minutes")


# ══════════════════════════════════════════════════════════════
#  Verificação: prazo vencido → cumprida ou atrasada
# ══════════════════════════════════════════════════════════════

def _evidencias(p: dict, msgs_email: list[dict]) -> list[str]:
    """Linhas trocadas com o mesmo contato depois da promessa. [] = Pedro não mandou nada a ele depois."""
    o = p.get("origem") or {}
    dito = core._parse_dt(p.get("dito_em"))
    if o.get("canal") == "email":
        para = set(o.get("para") or [])
        depois = [m for m in msgs_email if m.get("date") and m["date"] > dito and para & set(m.get("to") or [])
                  and m.get("message_id") != o.get("message_id")]
        return [f"[{m['date']:%d/%m %H:%M}] Pedro (e-mail, assunto “{m.get('subject')}”"
                + (f", anexos: {', '.join(m['attachments'][:5])}" if m.get("attachments") else "")
                + f"): {' '.join(separar_citacao(m.get('text') or '')[0].split())[:600]}" for m in depois[-MAX_EVIDENCIAS:]]
    if not o.get("remote_jid"):
        return []
    depois = [m for m in lib_db.mensagens_wa(o["remote_jid"], desde=p.get("dito_em"), limite=MAX_EVIDENCIAS + 1)
              if m["message_id"] != o.get("message_id")]
    if not any(m["from_me"] for m in depois):
        return []
    return [_linha(m, p.get("para_quem") or "Contato", False).strip() for m in depois]


def _julgar(p: dict, evidencias: list[str], agora: datetime) -> Optional[dict]:
    prompt = render_template("promessas_verificar", {
        "data_hoje": agora.strftime("%Y-%m-%d (%A) %H:%M"), "o_que": p["o_que"],
        "para_quem": p.get("para_quem") or "um contato", "dito_em": p.get("dito_em", "")[:16].replace("T", " "),
        "trecho": p.get("trecho", ""), "prazo": _quando(p), "mensagens": "\n".join(evidencias)[-5000:]})
    data = ollama_generate_json(prompt, timeout=120, schema=SCHEMA_VEREDITO, options={**core.OPTS_JSON, "num_predict": 200})
    if not isinstance(data, dict) or data.get("cumprida") not in ("sim", "nao", "incerto"):
        return None
    return {"cumprida": data["cumprida"], "motivo": str(data.get("motivo") or "").strip()[:200]}


def _proximo_lembrete(agora: datetime) -> datetime:
    h = agora.replace(hour=HORA_PADRAO, minute=0, second=0, microsecond=0)
    return h if agora < h else h + timedelta(days=1)


def verificar(agora: Optional[datetime] = None, msgs_email: Optional[list[dict]] = None, inbox_cfg: Optional[dict] = None,
              notificar: Optional[bool] = None) -> dict:
    """
    Promessas abertas com prazo vencido → cumprida (sinal nas mensagens depois dela) ou atrasada (volta à agenda).
    `msgs_email` = pasta Enviados já lida; None → lê agora se houver `inbox_cfg`, senão as de e-mail esperam.
    """
    agora = agora or core._now()
    res: dict = {"cumpridas": [], "atrasadas": [], "descartadas": [], "falhas": 0}
    abertas = listar("aberta")
    vencidas_email = [p for p in abertas if (p.get("origem") or {}).get("canal") == "email" and _vencida(p, agora)]
    if msgs_email is None and vencidas_email and inbox_cfg is not None:
        try:
            msgs_email = enviados_email(inbox_cfg, min(core._parse_dt(p["dito_em"]) for p in vencidas_email))
        except Exception as e:
            log(f"[promessas] não consegui ler os e-mails enviados: {e}")
    for p in abertas:
        ag = agenda.obter(p["agenda_id"]) if p.get("agenda_id") else None
        if ag and ag.get("status") in ("desfazer", "desfeito", "cancelado"):
            _atualizar(p["id"], status="descartada", motivo="Desfeita na agenda")
            res["descartadas"].append(p["id"])
            continue
        if not _vencida(p, agora):
            continue
        if (p.get("origem") or {}).get("canal") == "email" and msgs_email is None:
            continue                    # sem a pasta Enviados não dá para saber: não vira atrasada à toa
        evid = _evidencias(p, msgs_email or [])
        veredito = (_julgar(p, evid, agora) if evid else
                    {"cumprida": "nao", "motivo": "nenhuma mensagem sua a esse contato depois da promessa"})
        if veredito is None:
            res["falhas"] += 1          # LLM fora do ar: tenta de novo no próximo ciclo
            continue
        if veredito["cumprida"] == "sim":
            _atualizar(p["id"], status="cumprida", motivo=veredito["motivo"], verificada_em=agora.isoformat(timespec="minutes"))
            _fechar_na_agenda(p, "cumprida", "o Alfred viu que você cumpriu")
            res["cumpridas"].append(p["id"])
            log(f"[promessas] {p['id']} cumprida: {veredito['motivo']}")
            continue
        atraso_id = None
        try:
            item = agenda.adicionar(f"Atrasado: {_titulo(p)}", _proximo_lembrete(agora), tipo="tarefa",
                                    descricao=_descricao(p) + f"\nVerificação do Alfred: {veredito['motivo']}",
                                    notificar=notificar,
                                    origem={"canal": "promessa", "contato": p.get("para_quem"), "promessa_id": p["id"],
                                            "via": (p.get("origem") or {}).get("canal"), "atraso": True})
            atraso_id = item["id"]
        except Exception as e:
            log(f"[promessas] {p['id']}: atraso não foi para a agenda: {e}")
        _atualizar(p["id"], status="atrasada", motivo=veredito["motivo"], agenda_atraso_id=atraso_id,
                   verificada_em=agora.isoformat(timespec="minutes"))
        res["atrasadas"].append(p["id"])
        log(f"[promessas] {p['id']} ATRASADA ({veredito['cumprida']}): {veredito['motivo']}")
    return res


# ══════════════════════════════════════════════════════════════
#  Ciclo (chamado pelo core.ciclo) e ações manuais
# ══════════════════════════════════════════════════════════════

def ciclo(inbox_cfg: dict, retroativo_dias: Optional[float] = None, notificar: bool = True) -> dict:
    """Varre as falas de Pedro (janela_horas, ou os últimos `retroativo_dias`) e verifica os prazos vencidos."""
    cfg = _cfg()
    if not cfg["ativo"]:
        return {"ativo": False}
    agora = core._now()
    desde = agora - timedelta(hours=retroativo_dias * 24 if retroativo_dias else cfg["janela_horas"])
    notif = None if notificar and not retroativo_dias else False   # retroativo: lista no fim, sem uma notificação por item
    resumo: dict = {"novas": [], "falhas": 0, "desde": desde.isoformat(timespec="minutes")}
    seen = core.load_seen(SEEN_FILE)
    msgs_email: Optional[list[dict]] = None
    if "email" in cfg["canais"]:
        abertas = [core._parse_dt(p.get("dito_em")) for p in listar("aberta") if (p.get("origem") or {}).get("canal") == "email"]
        try:
            msgs_email = enviados_email(inbox_cfg, min([desde, *[d for d in abertas if d]]))
        except Exception as e:
            log(f"[promessas] não consegui ler os e-mails enviados: {e}")
    try:
        if "whatsapp" in cfg["canais"]:
            varrer_whatsapp(seen, desde, resumo, notificar=notif)
            if retroativo_dias:
                varrer_pendencias(seen, desde, resumo, notificar=notif)
        if msgs_email:
            varrer_email([m for m in msgs_email if not m.get("date") or m["date"] >= desde], seen, resumo, notificar=notif)
    finally:
        core.save_seen(seen, SEEN_FILE)
    resumo["verificacao"] = verificar(agora, msgs_email, notificar=notif)
    log(f"[promessas] {len(resumo['novas'])} nova(s) · atrasadas {len(resumo['verificacao']['atrasadas'])} · "
        f"cumpridas {len(resumo['verificacao']['cumpridas'])}" + (f" · {resumo['falhas']} falha(s) do LLM" if resumo["falhas"] else ""))
    return resumo


def _fechar_na_agenda(p: dict, status: str, via: str) -> None:
    """cumprida → os itens da promessa na agenda viram feito (☑ no calendário) · descartada → sai da agenda
    o que ainda não aconteceu."""
    agora = core._now()
    for aid in (p.get("agenda_id"), p.get("agenda_atraso_id")):
        item = agenda.obter(aid) if aid else None
        if not item or item.get("status") in ("feito", "desfazer", "desfeito", "cancelado"):
            continue
        if status == "cumprida":
            agenda.concluir(aid, via=via, propagar=False)
        elif (core._parse_dt(str(item.get("inicio"))) or agora) > agora:
            agenda.desfazer(aid)


def marcar(pid: str, status: str, motivo: str = "") -> dict:
    """CLI feita / painel Feito → cumprida · descartar → descartada; a agenda acompanha (_fechar_na_agenda)."""
    p = obter(pid)
    if not p:
        raise KeyError(f"Promessa não encontrada: {pid}")
    if p.get("status") == status:
        return p
    agora = core._now()
    motivo = motivo or f"marcada pelo terminal em {agora:%d/%m %H:%M}"
    _fechar_na_agenda(p, status, motivo)
    return _atualizar(pid, status=status, motivo=motivo)


# ══════════════════════════════════════════════════════════════
#  CLI:  run.sh promessas [listar|varrer|verificar|feita ID|descartar ID]
# ══════════════════════════════════════════════════════════════

def adicionar_parser(sub) -> None:
    sp = sub.add_parser("promessas", help="o que você disse que ia fazer: listar | varrer | verificar | feita ID | descartar ID")
    sp.add_argument("acao", nargs="?", default="listar", choices=("listar", "varrer", "verificar", "feita", "descartar"))
    sp.add_argument("arg", nargs="*", default=[])
    sp.add_argument("--retroativo", type=float, default=None, metavar="DIAS",
                    help="varrer: lê os últimos DIAS (inclui as respostas já gravadas nas pendências)")
    sp.add_argument("--inbox", default=None)
    sp.add_argument("--todas", action="store_true", help="listar: inclui cumpridas e descartadas")
    sp.add_argument("--sem-notificar", action="store_true")
    sp.add_argument("--json", action="store_true")


def _imprimir(itens: list[dict]) -> None:
    if not itens:
        print("Nenhuma promessa.")
        return
    print(f"\n{'id':<42} {'status':<10} {'prazo':<20} {'para':<18} o quê")
    print("-" * 120)
    for p in sorted(itens, key=lambda x: str(x.get("prazo"))):
        print(f"{p['id']:<42} {p['status']:<10} {_quando(p):<20} {(p.get('para_quem') or '')[:17]:<18} {p['o_que'][:45]}")
    print()


def cmd_promessas(args) -> int:
    import json
    if args.acao == "listar":
        itens = listar() if args.todas else [p for p in listar() if p.get("status") in ABERTAS]
        if args.json:
            print(json.dumps(itens, ensure_ascii=False, indent=2, default=str))
        else:
            _imprimir(itens)
        return 0
    if args.acao in ("varrer", "verificar"):
        _, inbox_cfg = core.carregar_cfg(args.inbox)
        if args.acao == "varrer":
            res = ciclo(inbox_cfg, retroativo_dias=args.retroativo, notificar=not args.sem_notificar)
        else:
            res = verificar(inbox_cfg=inbox_cfg, notificar=False if args.sem_notificar else None)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        else:
            print(json.dumps({k: v for k, v in res.items() if k != "novas"}, ensure_ascii=False, indent=2, default=str))
            _imprimir([p for p in listar() if p["id"] in set(res.get("novas") or [])
                       or p["id"] in set((res.get("verificacao") or res).get("atrasadas") or [])])
        return 0
    if not args.arg:
        print(f"Uso: promessas {args.acao} <id>")
        return 1
    try:
        p = marcar(args.arg[0], "cumprida" if args.acao == "feita" else "descartada", " ".join(args.arg[1:]))
    except KeyError as e:
        print(f"Erro: {e}")
        return 1
    print(f"{p['id']} → {p['status']}")
    return 0
