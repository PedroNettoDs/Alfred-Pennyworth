"""
Alfred — agenda: compromissos detectados em WhatsApp/e-mail → calendário do Thunderbird.

Fluxo
    WhatsApp (mensagem de contato, ou Pedro escrevendo para si mesmo) / e-mail ("vou" num evento)
        ↓ detectar_compromisso()   prefiltro por regex + Ollama (JSON Schema)
        ↓ adicionar()              VAULT_ALFRED/agenda/agenda.yml  (status: pendente)
        ↓                          notificação no PC com [Ok] [Desfazer]
    extensão "Alfred — agenda" no Thunderbird
        GET  /agenda/fila          → itens pendentes (com o VCALENDAR pronto) e itens a desfazer
        cria o evento no calendário escolhido (padrão "Agenda", o Google via CalDAV) — o Thunderbird
        sincroniza com o Google e o evento aparece também no celular
        POST /agenda/{id}/resultado → status no_calendario | erro | desfeito
    Sem a extensão: GET /agenda/alfred.ics (só 127.0.0.1) lista os itens que ainda não foram para o
    calendário — dá para assinar no Thunderbird como agenda "Na rede" (somente leitura).

Status: pendente → no_calendario | erro (tenta de novo até MAX_TENTATIVAS) ; desfazer → desfeito ; cancelado.
"""

import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

import core
from _shared.lib_alfred import log, ollama_generate_json
from _shared.lib_templates import render_template

MAX_TENTATIVAS = 5
EM_EXECUCAO_TIMEOUT = timedelta(minutes=15)
TIPOS = ("evento", "tarefa", "lembrete")

SCHEMA_COMPROMISSO = {
    "type": "object",
    "properties": {
        "existe":      {"type": "boolean"},
        "tipo":        {"type": "string", "enum": ["evento", "tarefa", "lembrete", "nenhum"]},
        "titulo":      {"type": "string"},
        "data":        {"type": "string"},
        "hora":        {"type": "string"},
        "hora_fim":    {"type": "string"},
        "local":       {"type": "string"},
        "confianca":   {"type": "number"},
        "trecho":      {"type": "string"},
    },
    "required": ["existe", "tipo", "titulo", "data", "hora", "hora_fim", "local", "confianca", "trecho"],
}

# Só chama o LLM se a mensagem tiver cara de compromisso (data, hora, dia da semana, palavras-chave).
_PREFILTRO = re.compile(
    r"\b(\d{1,2}[:h]\d{0,2}|\d{1,2}/\d{1,2}|hoje|amanha|depois de amanha|segunda|terca|quarta|quinta|sexta|"
    r"sabado|domingo|semana que vem|proxima semana|mes que vem|dia \d{1,2}|reuniao|encontro|consulta|"
    r"entrevista|prova|aula|palestra|evento|prazo|entrega|vencimento|vence|boleto|pagar|lembr\w*|"
    r"nao esquec\w*|agenda\w*|marca\w*|compromisso|aniversario|meet|call|webinar)\b", re.I)


# ══════════════════════════════════════════════════════════════
#  Armazenamento
# ══════════════════════════════════════════════════════════════

def _cfg() -> dict:
    """Seção agenda: do perfis.yml (opcional) com defaults."""
    try:
        from _shared.perfis import load
        sec = load().get("agenda") or {}
    except Exception:
        sec = {}
    return {
        "calendario":          sec.get("calendario", "Agenda"),
        "duracao_min":         int(sec.get("duracao_min", 60)),
        "lembrete_min":        int(sec.get("lembrete_min", 30)),
        "confianca_minima":    float(sec.get("confianca_minima", 0.7)),
        "auto_whatsapp":       bool(sec.get("auto_whatsapp", True)),
        "auto_email_vou":      bool(sec.get("auto_email_vou", True)),
        "notas_para_mim":      bool(sec.get("notas_para_mim", True)),
        "criar_tarefa":        bool(sec.get("criar_tarefa", True)),
        "notificar":           bool(sec.get("notificar", True)),
    }


def agenda_file() -> Path:
    d = core.vault() / "agenda"
    d.mkdir(parents=True, exist_ok=True)
    return d / "agenda.yml"


def _load() -> dict:
    f = agenda_file()
    if not f.exists():
        return {"itens": []}
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log(f"[agenda] YAML ilegível ({e}) — começando vazio")
        return {"itens": []}
    if not isinstance(data, dict) or not isinstance(data.get("itens"), list):
        return {"itens": []}
    return data


def _trava():
    """Trava entre processos de agenda.yml (API, CLI, aguardador "Desfazer", ciclo do e-mail).
    Ordem: agenda → tasks (adicionar cria tarefa dentro dela); nunca pendências dentro da agenda."""
    return core.trava_arquivo(agenda_file())


def _save(data: dict) -> None:
    with _trava():   # reentrante: quem lê-muda-grava já está dentro dela
        core.gravar_atomico(agenda_file(), yaml.dump(data, allow_unicode=True, sort_keys=False))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\W+", " ", s).strip()


def listar(status: Optional[str] = None) -> list[dict]:
    itens = _load()["itens"]
    if status in (None, "", "todos", "todas"):
        return itens
    return [i for i in itens if i.get("status") == status]


def obter(aid: str) -> Optional[dict]:
    return next((i for i in _load()["itens"] if i.get("id") == aid), None)


# ══════════════════════════════════════════════════════════════
#  Criação
# ══════════════════════════════════════════════════════════════

def _novo_id(titulo: str, existentes: set) -> str:
    base = f"{core._now():%Y%m%d}-{core.slugify(core._ascii(titulo), 32) or 'compromisso'}"
    aid, n = base, 2
    while aid in existentes:
        aid, n = f"{base}-{n}", n + 1
    return aid


def adicionar(
    titulo: str,
    inicio: datetime,
    dia_inteiro: bool = False,
    tipo: str = "evento",
    fim: Optional[datetime] = None,
    local: str = "",
    descricao: str = "",
    origem: Optional[dict] = None,
    notificar: Optional[bool] = None,
) -> dict:
    """
    Põe um compromisso na fila da agenda. Idempotente: mesmo título (normalizado) no mesmo dia e mesma
    origem devolve o item existente em vez de duplicar. → item (dict) com "_novo": bool.
    """
    cfg = _cfg()
    tipo = tipo if tipo in TIPOS else "evento"
    titulo = (titulo or "").strip()[:120] or "Compromisso"
    if inicio.tzinfo is None:
        inicio = inicio.replace(tzinfo=core._now().tzinfo)
    if fim is None and not dia_inteiro:
        fim = inicio + timedelta(minutes=cfg["duracao_min"] if tipo == "evento" else 15)
    with _trava():
        data = _load()
        chave = (_norm(titulo), inicio.date().isoformat())
        for i in data["itens"]:
            if i.get("status") in ("desfeito", "cancelado", "desfazer"):
                continue
            if (_norm(i.get("titulo", "")), str(i.get("inicio", ""))[:10]) == chave:
                log(f"[agenda] já na agenda: {i['id']}")
                return {**i, "_novo": False}
        now = core._now()
        aid = _novo_id(titulo, {i.get("id") for i in data["itens"]})
        item = {
            "id":            aid,
            "uid":           f"alfred-{aid}-{uuid.uuid4().hex[:8]}@alfred-pennyworth.local",
            "tipo":          tipo,
            "titulo":        titulo,
            "inicio":        inicio.isoformat(timespec="minutes") if not dia_inteiro else inicio.date().isoformat(),
            "fim":           (fim.isoformat(timespec="minutes") if fim and not dia_inteiro else None),
            "dia_inteiro":   bool(dia_inteiro),
            "local":         (local or "").strip()[:200],
            "descricao":     (descricao or "").strip()[:1500],
            "lembrete_min":  cfg["lembrete_min"],
            "calendario":    cfg["calendario"],
            "origem":        origem or {"canal": "manual"},
            "status":        "pendente",
            "criado_em":     now.isoformat(timespec="minutes"),
            "atualizado_em": now.isoformat(timespec="minutes"),
            "tentativas":    0,
            "erro":          None,
            "task_id":       None,
        }
        if cfg["criar_tarefa"] and tipo in ("tarefa", "lembrete"):
            try:
                hora = "" if dia_inteiro else f" — {inicio:%H:%M}"
                item["task_id"] = core._add_task(f"{titulo}{hora}", inicio.strftime("%Y-%m-%d"),
                                                 ["agenda", tipo, (origem or {}).get("canal", "manual")])
            except Exception as e:
                log(f"[agenda] tarefa não criada: {e}")
        data["itens"].append(item)
        _save(data)
    quando = _quando_legivel(item)
    log(f"[agenda] + {aid} ({tipo}) {titulo!r} — {quando}")
    if notificar if notificar is not None else cfg["notificar"]:
        notificar_item(item)
    return {**item, "_novo": True}


_DIAS = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")


def _quando_legivel(item: dict) -> str:
    if item.get("dia_inteiro"):
        d = datetime.fromisoformat(str(item["inicio"])[:10])
        return f"{d:%d/%m} ({_DIAS[d.weekday()]}), dia inteiro"
    d = datetime.fromisoformat(item["inicio"])
    return f"{d:%d/%m} ({_DIAS[d.weekday()]}) {d:%H:%M}"


# ══════════════════════════════════════════════════════════════
#  Detecção (Ollama)
# ══════════════════════════════════════════════════════════════

def prefiltro(texto: str) -> bool:
    return bool(_PREFILTRO.search(core._ascii(texto or "")))


def detectar_compromisso(texto: str, contexto: str = "", data_ref: Optional[datetime] = None,
                         para_mim: bool = False) -> Optional[dict]:
    """
    Texto livre → {"tipo", "titulo", "inicio": datetime, "dia_inteiro", "fim", "local", "confianca", "trecho"}
    ou None. Só devolve compromisso com data resolvida, no futuro (ou hoje) e confiança ≥ confianca_minima.
    """
    if not texto or not texto.strip() or not prefiltro(texto):
        return None
    ref = data_ref or core._now()
    prompt = render_template("agenda_detectar", {
        "data_hoje": ref.strftime("%Y-%m-%d (%A) %H:%M"),
        "contexto":  contexto or "mensagem recebida",
        "quem":      "o próprio Pedro, anotando algo para ele mesmo" if para_mim else "outra pessoa, escrevendo para Pedro",
        "texto":     texto[:2500],
    })
    data = ollama_generate_json(prompt, timeout=120, schema=SCHEMA_COMPROMISSO,
                                options={**core.OPTS_JSON, "num_predict": 300})
    if not isinstance(data, dict) or not data.get("existe") or data.get("tipo") == "nenhum":
        return None
    try:
        conf = float(data.get("confianca", 0))
    except (TypeError, ValueError):
        conf = 0.0
    if conf < _cfg()["confianca_minima"]:
        log(f"[agenda] compromisso descartado (confiança {conf:.2f}): {data.get('titulo')!r}")
        return None
    dia = str(data.get("data") or "").strip()
    hora = str(data.get("hora") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dia):
        return None
    tem_hora = bool(re.fullmatch(r"\d{1,2}:\d{2}", hora))
    try:
        y, m, d = (int(x) for x in dia.split("-"))
        hh, mm = (int(x) for x in hora.split(":")) if tem_hora else (9, 0)
        inicio = datetime(y, m, d, hh, mm, tzinfo=ref.tzinfo)
    except ValueError:
        return None
    if inicio.date() < ref.date():
        log(f"[agenda] compromisso no passado ignorado: {data.get('titulo')!r} {dia}")
        return None
    fim = None
    hf = str(data.get("hora_fim") or "").strip()
    if tem_hora and re.fullmatch(r"\d{1,2}:\d{2}", hf):
        try:
            fh, fm = (int(x) for x in hf.split(":"))
            fim = inicio.replace(hour=fh, minute=fm)
            if fim <= inicio:
                fim = None
        except ValueError:
            fim = None
    tipo = data.get("tipo") if data.get("tipo") in TIPOS else "evento"
    return {"tipo": tipo, "titulo": str(data.get("titulo") or "").strip()[:120], "inicio": inicio,
            "dia_inteiro": not tem_hora, "fim": fim, "local": str(data.get("local") or "").strip()[:200],
            "confianca": round(conf, 2), "trecho": str(data.get("trecho") or "").strip()[:300]}


def de_compromisso_whatsapp(comp: dict, contato: str, texto: str, pid: Optional[str]) -> Optional[dict]:
    """compromisso {titulo, data, hora, tipo?} extraído junto do rascunho → item da agenda (ou None)."""
    if not comp or not comp.get("data"):
        return None
    try:
        y, m, d = (int(x) for x in comp["data"].split("-"))
        if comp.get("hora"):
            hh, mm = (int(x) for x in comp["hora"].split(":"))
            inicio, dia_inteiro = datetime(y, m, d, hh, mm, tzinfo=core._now().tzinfo), False
        else:
            inicio, dia_inteiro = datetime(y, m, d, 9, 0, tzinfo=core._now().tzinfo), True
    except (ValueError, AttributeError):
        return None
    if inicio.date() < core._now().date():
        return None
    titulo = comp.get("titulo") or f"Compromisso com {contato}"
    return adicionar(titulo, inicio, dia_inteiro=dia_inteiro, tipo=comp.get("tipo") or "evento",
                     descricao=f"Mensagem de {contato} (WhatsApp): “{texto[:600]}”"
                               + (f"\nPendência Alfred: {pid}" if pid else ""),
                     origem={"canal": "whatsapp", "contato": contato, "pendencia_id": pid})


# ══════════════════════════════════════════════════════════════
#  iCalendar
# ══════════════════════════════════════════════════════════════

def _esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: linhas de no máximo 75 octetos, continuação com espaço."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    partes, atual = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(atual) + len(b) > (75 if not partes else 74):
            partes.append(atual.decode("utf-8"))
            atual = b""
        atual += b
    partes.append(atual.decode("utf-8"))
    return "\r\n ".join(partes)


def _utc(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=core._now().tzinfo)
    return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def vevent(item: dict) -> list[str]:
    linhas = ["BEGIN:VEVENT", f"UID:{item['uid']}",
              f"DTSTAMP:{_utc(item.get('criado_em') or core._now().isoformat())}"]
    if item.get("dia_inteiro"):
        d = datetime.fromisoformat(str(item["inicio"])[:10])
        linhas += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
    else:
        linhas.append(f"DTSTART:{_utc(item['inicio'])}")
        linhas.append(f"DTEND:{_utc(item.get('fim') or item['inicio'])}")
    prefixo = {"tarefa": "☐ ", "lembrete": "⏰ "}.get(item.get("tipo"), "")
    linhas.append(f"SUMMARY:{_esc(prefixo + item['titulo'])}")
    if item.get("local"):
        linhas.append(f"LOCATION:{_esc(item['local'])}")
    desc = (item.get("descricao") or "") + f"\n\n— anotado pelo Alfred ({item['id']})"
    linhas.append(f"DESCRIPTION:{_esc(desc.strip())}")
    linhas.append(f"CATEGORIES:Alfred,{item.get('tipo', 'evento')}")
    if item.get("lembrete_min") and not item.get("dia_inteiro"):
        linhas += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_esc(item['titulo'])}",
                   f"TRIGGER:-PT{int(item['lembrete_min'])}M", "END:VALARM"]
    linhas.append("END:VEVENT")
    return linhas


def vcalendar(itens: list[dict], nome: str = "Alfred") -> str:
    linhas = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Alfred-Pennyworth//agenda//PT-BR",
              "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{_esc(nome)}"]
    for i in itens:
        linhas += vevent(i)
    linhas.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in linhas) + "\r\n"


def feed_ics() -> str:
    """Itens que ainda não chegaram ao calendário real (fallback sem extensão)."""
    return vcalendar([i for i in listar() if i.get("status") in ("pendente", "erro", "em_execucao")],
                     nome="Alfred (pendentes)")


# ══════════════════════════════════════════════════════════════
#  Extensão do Thunderbird
# ══════════════════════════════════════════════════════════════

def fila_extensao() -> list[dict]:
    """Itens para a extensão: criar (pendente/erro com tentativas) e remover (desfazer)."""
    with _trava():
        data = _load()
        now = core._now()
        out, mudou = [], False
        for i in data["itens"]:
            st = i.get("status")
            if st == "em_execucao":
                try:
                    if now - datetime.fromisoformat(i.get("atualizado_em")) < EM_EXECUCAO_TIMEOUT:
                        continue
                except (TypeError, ValueError):
                    pass
                st = "pendente"
            if st in ("pendente", "erro") and int(i.get("tentativas") or 0) < MAX_TENTATIVAS:
                i["status"] = "em_execucao"
                i["atualizado_em"] = now.isoformat(timespec="minutes")
                mudou = True
                out.append({"id": i["id"], "acao": "criar", "uid": i["uid"], "calendario": i.get("calendario"),
                            "titulo": i["titulo"], "ics": vcalendar([i])})
            elif st == "desfazer":
                out.append({"id": i["id"], "acao": "remover", "uid": i["uid"], "calendario": i.get("calendario"),
                            "titulo": i["titulo"]})
        if mudou:
            _save(data)
        return out


def registrar_resultado(aid: str, acao: str, ok: bool, calendario: str = "", erro: str = "") -> dict:
    with _trava():
        data = _load()
        item = next((i for i in data["itens"] if i.get("id") == aid), None)
        if not item:
            raise KeyError(f"Item da agenda não encontrado: {aid}")
        now = core._now().isoformat(timespec="minutes")
        if acao == "remover":
            item["status"] = "desfeito" if ok else "desfazer"
        elif ok:
            item["status"] = "no_calendario"
            item["no_calendario_em"] = now
            if calendario:
                item["calendario"] = calendario
            item["erro"] = None
        else:
            item["status"] = "erro"
            item["tentativas"] = int(item.get("tentativas") or 0) + 1
            item["erro"] = (erro or "erro desconhecido")[:300]
        item["atualizado_em"] = now
        _save(data)
    log(f"[agenda] {aid} ← extensão: {acao} {'ok' if ok else 'ERRO ' + (erro or '')[:80]}")
    if ok and acao == "criar":
        core._notify("Alfred — agenda", f"No calendário “{item.get('calendario')}”: {item['titulo']} — {_quando_legivel(item)}")
    return item


def desfazer(aid: str) -> dict:
    with _trava():
        data = _load()
        item = next((i for i in data["itens"] if i.get("id") == aid), None)
        if not item:
            raise KeyError(f"Item da agenda não encontrado: {aid}")
        st = item.get("status")
        if st in ("desfeito", "cancelado"):
            return item
        item["status"] = "desfazer" if st in ("no_calendario", "em_execucao") else "cancelado"
        item["atualizado_em"] = core._now().isoformat(timespec="minutes")
        _save(data)
    log(f"[agenda] {aid} → {item['status']}")
    return item


# ══════════════════════════════════════════════════════════════
#  Notificação
# ══════════════════════════════════════════════════════════════

def notificar_item(item: dict) -> None:
    """Notificação com [Ok] [Desfazer] num processo destacado (mesmo esquema das pendências)."""
    if core._tem_gui():
        core._iniciar_aguardador("_aguardar-agenda", item["id"])
    else:
        core._notify("Alfred — anotado na agenda", f"{item['titulo']} — {_quando_legivel(item)}")


def texto_notificacao(item: dict) -> tuple[str, str]:
    tipo = {"evento": "Evento", "tarefa": "Tarefa", "lembrete": "Lembrete"}.get(item.get("tipo"), "Compromisso")
    origem = item.get("origem") or {}
    de = ""
    if origem.get("canal") == "whatsapp":
        de = f"\nDe: {origem.get('contato')} (WhatsApp)"
    elif origem.get("canal") == "whatsapp-eu":
        de = "\nVocê anotou no WhatsApp"
    elif origem.get("canal") == "email":
        de = f"\nDo e-mail: {origem.get('assunto', '')[:60]}"
    corpo = f"{item['titulo']}\n{_quando_legivel(item)}" + (f" · {item['local']}" if item.get("local") else "") + de
    corpo += f"\n\nVai para o calendário “{item.get('calendario')}” do Thunderbird."
    return f"Alfred — {tipo} anotado na agenda", corpo


# ══════════════════════════════════════════════════════════════
#  CLI:  run.sh agenda [listar|anotar "texto"|desfazer ID|ics|fila]
# ══════════════════════════════════════════════════════════════

def adicionar_parser(sub) -> None:
    sp = sub.add_parser("agenda", help="compromissos do Alfred: listar | anotar \"texto\" | desfazer ID | ics | fila")
    sp.add_argument("acao", nargs="?", default="listar", choices=("listar", "anotar", "desfazer", "ics", "fila"))
    sp.add_argument("arg", nargs="*", default=[])
    sp.add_argument("--todos", action="store_true", help="listar: inclui desfeitos/cancelados")
    sp.add_argument("--sem-notificar", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("_aguardar-agenda")
    sp.add_argument("id")


def cmd_agenda(args) -> int:
    import json
    if args.acao == "listar":
        itens = listar() if args.todos else [i for i in listar() if i.get("status") not in ("desfeito", "cancelado")]
        if args.json:
            print(json.dumps(itens, ensure_ascii=False, indent=2, default=str))
            return 0
        if not itens:
            print("Agenda do Alfred vazia.")
            return 0
        print(f"\n{'id':<40} {'status':<14} {'tipo':<9} {'quando':<24} título")
        print("-" * 120)
        for i in sorted(itens, key=lambda x: str(x.get("inicio"))):
            print(f"{i['id']:<40} {i['status']:<14} {i['tipo']:<9} {_quando_legivel(i):<24} {i['titulo'][:45]}")
        print()
        return 0
    if args.acao == "anotar":
        texto = " ".join(args.arg).strip()
        if not texto:
            print('Uso: agenda anotar "reunião com a Attano sexta às 14h"')
            return 1
        comp = detectar_compromisso(texto, contexto="Pedro anotando pelo terminal", para_mim=True)
        if not comp:
            print("Não achei data/compromisso nesse texto (diga o dia, ex.: 'amanhã 10h', 'sexta', '25/10 14:00').")
            return 1
        item = adicionar(comp["titulo"] or texto[:60], comp["inicio"], dia_inteiro=comp["dia_inteiro"],
                         tipo=comp["tipo"], fim=comp["fim"], local=comp["local"], descricao=f"Anotado no terminal: “{texto}”",
                         origem={"canal": "terminal"}, notificar=not args.sem_notificar)
        print(f"{'Anotado' if item.get('_novo') else 'Já estava'}: {item['titulo']} — {_quando_legivel(item)} "
              f"[{item['tipo']}] ({item['id']}, {item['status']})")
        return 0
    if args.acao == "desfazer":
        if not args.arg:
            print("Uso: agenda desfazer <id>")
            return 1
        try:
            item = desfazer(args.arg[0])
        except KeyError as e:
            print(f"Erro: {e}")
            return 1
        print(f"{item['id']} → {item['status']}")
        return 0
    if args.acao == "ics":
        print(feed_ics(), end="")
        return 0
    if args.acao == "fila":
        print(json.dumps([{k: v for k, v in i.items() if k != "ics"} for i in listar() if i.get("status") in ("pendente", "erro", "em_execucao", "desfazer")],
                         ensure_ascii=False, indent=2, default=str))
        return 0
    return 1
