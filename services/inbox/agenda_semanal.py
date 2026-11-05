"""
Alfred — fechamento semanal da agenda e das promessas (domingo, alfred-semanal.timer 19:00 com --se-devido).

    agenda.yml     feito (☑ já no calendário) · desfeito · cancelado
    promessas.yml  cumprida · descartada
        ↓ relatório da semana  VAULT_ALFRED/agenda/relatorios/AAAA-MM-DD_AAAA-MM-DD.md
        ↓ treino do Laya       services/laya/treino/dados/{agenda,promessas}.jsonl (fora do git, formato do laya-train)
        ↓ apaga esses itens dos dois yml — os eventos continuam no calendário do Google
    Fica o que ainda está aberto: pendente, no calendário, erro, remoção pendente, ☑ a caminho; promessas
    abertas e atrasadas.

Rótulos do Laya — a ação de Pedro é o rótulo:
    agenda     feito → "sim" (era compromisso) · desfeito antes do horário → "nao" (alarme falso do Alfred).
               Desfeito depois do horário é limpeza da lista, não opinião sobre a detecção: fica de fora.
               Só itens que o Alfred detectou sozinho (WhatsApp, e-mail); o que Pedro digitou não ensina nada.
    promessas  cumprida → "sim" · descartada → "nao".
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import agenda
import core
import promessas
from _shared.lib_alfred import log

DIAS_SEMANA = 7
CANAIS_DETECTADOS = ("whatsapp", "whatsapp-eu", "email")

PERGUNTA_AGENDA = {"compromisso": {
    "type": "choice",
    "instructions": "Does this message contain a commitment that should go to Pedro's calendar?",
    "criteria": {
        "sim": "a real event, task or reminder with a date that Pedro has to attend or do",
        "nao": "nothing for the calendar: chat, a past event, an invitation not accepted, or nothing to do",
    },
}}
PERGUNTA_PROMESSA = {"promessa": {
    "type": "choice",
    "instructions": "In this message Pedro wrote, does he commit to doing something for the other person?",
    "criteria": {
        "sim": "Pedro promises to do, send, check, pay or answer something",
        "nao": "no promise: courtesy, agreement, a question, or something already done",
    },
}}

_META = re.compile(r"^(Pendência Alfred|Promessa Alfred|Prazo|Sem data na mensagem|Verificação do Alfred):.*$", re.M)


def estado_file() -> Path:
    return core.PROJECT_ROOT / "logs" / "agenda_semanal.json"


def dados_laya() -> Path:
    return core.PROJECT_ROOT / "services" / "laya" / "treino" / "dados"


def relatorios_dir() -> Path:
    return agenda.agenda_file().parent / "relatorios"


def _ultimo() -> Optional[datetime]:
    try:
        return datetime.fromisoformat(json.loads(estado_file().read_text(encoding="utf-8"))["ultimo"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def devido(agora: Optional[datetime] = None) -> bool:
    """Domingo (uma vez por semana ISO); noutro dia, só se o último fechamento tiver 7 dias ou mais (PC desligado)."""
    agora = agora or core._now()
    ultimo = _ultimo()
    if ultimo is None:
        return agora.weekday() == 6
    if agora.weekday() == 6:
        return ultimo.isocalendar()[:2] != agora.isocalendar()[:2]
    return (agora - ultimo).days >= DIAS_SEMANA


def _fechados() -> tuple[list[dict], list[dict]]:
    """Itens que saem do registro. Feito com o ☑ ainda a caminho do calendário espera (salvo se esgotou as tentativas)."""
    itens = [i for i in agenda.listar() if i.get("status") in ("desfeito", "cancelado")
             or (i.get("status") == "feito"
                 and (not i.get("atualizar") or int(i.get("tentativas") or 0) >= agenda.MAX_TENTATIVAS))]
    return itens, [p for p in promessas.listar() if p.get("status") in ("cumprida", "descartada")]


def _dt(valor) -> Optional[datetime]:
    d = core._parse_dt(str(valor)) if valor else None
    if d and d.tzinfo is None:
        d = d.replace(tzinfo=core._now().tzinfo)
    return d


def _laya_agenda(item: dict) -> Optional[dict]:
    if (item.get("origem") or {}).get("canal") not in CANAIS_DETECTADOS:
        return None
    if item["status"] == "feito":
        rotulo = "sim"
    else:
        desfeito, inicio = _dt(item.get("desfeito_em") or item.get("atualizado_em")), _dt(item.get("inicio"))
        if not desfeito or not inicio or desfeito >= inicio:
            return None
        rotulo = "nao"
    estado = _META.sub("", item.get("descricao") or "").strip()
    if not estado:
        return None
    return {"state": estado, "questions": PERGUNTA_AGENDA, "expected": {"compromisso": rotulo}}


def _laya_promessa(p: dict) -> Optional[dict]:
    if not (p.get("trecho") or "").strip():
        return None
    return {"state": p["trecho"].strip(), "questions": PERGUNTA_PROMESSA,
            "expected": {"promessa": "sim" if p["status"] == "cumprida" else "nao"}}


def _linha_item(i: dict) -> str:
    rot = {"feito": "feito", "desfeito": "desfeito", "cancelado": "cancelado"}[i["status"]]
    quando = _dt(i.get("feito_em") if i["status"] == "feito" else (i.get("desfeito_em") or i.get("atualizado_em")))
    extra = " · ".join(x for x in [
        f"urgência {i['urgencia']}" if i.get("urgencia") else "",
        f"prioridade {i['prioridade']}" if i.get("prioridade") else ""] if x)
    linha = (f"- **{agenda._quando_legivel(i)}** · {i.get('titulo')} — {agenda._origem_legivel(i)} · "
             f"{rot}{f' em {quando:%d/%m %H:%M}' if quando else ''}{f' · {extra}' if extra else ''}")
    return linha + (f"\n  {i['resumo']}" if i.get("resumo") else "")


def _linha_promessa(p: dict) -> str:
    rot = "cumprida" if p["status"] == "cumprida" else "descartada"
    return (f"- {p.get('o_que')} ({p.get('para_quem') or '?'}) — {rot}"
            + (f": {p['motivo']}" if p.get("motivo") else "") + (f"\n  “{p['trecho']}”" if p.get("trecho") else ""))


def _relatorio(ini: datetime, fim: datetime, itens: list[dict], proms: list[dict], ex_ag: list, ex_pr: list) -> str:
    feitos = [i for i in itens if i["status"] == "feito"]
    desfeitos = [i for i in itens if i["status"] != "feito"]
    alarmes = sum(1 for e in ex_ag if e["expected"]["compromisso"] == "nao")
    abertos = [i for i in agenda.listar() if i.get("status") not in agenda.FECHADOS]
    proms_abertas = [p for p in promessas.listar() if p.get("status") in promessas.ABERTAS]
    linhas = [
        f"# Agenda — {ini:%d/%m} a {fim:%d/%m/%Y}", "",
        f"Fechamento do Alfred em {fim:%d/%m/%Y %H:%M}. Estes itens saíram do registro (agenda.yml e promessas.yml); "
        "os eventos continuam no calendário.", "",
        "## Resumo", "",
        f"- Feitos: {len(feitos)}",
        f"- Desfeitos ou cancelados: {len(desfeitos)} ({alarmes} antes do horário: alarme falso do Alfred)",
        f"- Promessas: {sum(1 for p in proms if p['status'] == 'cumprida')} cumpridas, "
        f"{sum(1 for p in proms if p['status'] == 'descartada')} descartadas",
        f"- Continuam abertos: {len(abertos)} na agenda, {len(proms_abertas)} promessas "
        f"({sum(1 for p in proms_abertas if p.get('status') == 'atrasada')} atrasadas)",
        f"- Treino do Laya: {len(ex_ag)} exemplos de agenda e {len(ex_pr)} de promessas",
    ]
    for titulo, grupo, fmt in (("Feitos", feitos, _linha_item), ("Desfeitos e cancelados", desfeitos, _linha_item),
                               ("Promessas", proms, _linha_promessa)):
        if grupo:
            linhas += ["", f"## {titulo}", ""] + [fmt(x) for x in grupo]
    if not itens and not proms:
        linhas += ["", "Nada foi concluído nem desfeito nesta semana."]
    return "\n".join(linhas) + "\n"


def processar(agora: Optional[datetime] = None, simular: bool = False) -> dict:
    """Relatório + exemplos do Laya + apaga os fechados. simular=True só devolve o texto."""
    agora = agora or core._now()
    itens, proms = _fechados()
    ex_ag = [e for e in map(_laya_agenda, itens) if e]
    ex_pr = [e for e in map(_laya_promessa, proms) if e]
    ini = _ultimo() or agora - timedelta(days=DIAS_SEMANA)
    texto = _relatorio(ini, agora, itens, proms, ex_ag, ex_pr)
    destino = relatorios_dir() / f"{ini:%Y-%m-%d}_{agora:%Y-%m-%d}.md"
    n = 2
    while destino.exists():
        destino = destino.with_name(f"{ini:%Y-%m-%d}_{agora:%Y-%m-%d}-{n}.md")
        n += 1
    res = {"relatorio": str(destino), "agenda": len(itens), "promessas": len(proms),
           "laya": {"agenda": len(ex_ag), "promessas": len(ex_pr)}}
    if simular:
        return {**res, "texto": texto}
    destino.parent.mkdir(parents=True, exist_ok=True)
    core.gravar_atomico(destino, texto)
    for nome, exemplos in (("agenda.jsonl", ex_ag), ("promessas.jsonl", ex_pr)):
        if exemplos:
            dados_laya().mkdir(parents=True, exist_ok=True)
            with (dados_laya() / nome).open("a", encoding="utf-8") as f:
                f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in exemplos)
    res["apagados"] = {"agenda": agenda.remover(i["id"] for i in itens),
                       "promessas": promessas.remover(p["id"] for p in proms)}
    estado_file().parent.mkdir(parents=True, exist_ok=True)
    core.gravar_atomico(estado_file(), json.dumps({"ultimo": agora.isoformat(timespec="minutes")}))
    log(f"[agenda-semanal] {destino.name}: {len(itens)} da agenda, {len(proms)} promessas, "
        f"Laya +{len(ex_ag)}/+{len(ex_pr)}")
    return res


def cmd(args) -> int:
    if getattr(args, "se_devido", False) and not devido():
        print("Fechamento semanal da agenda: ainda não é domingo (ou já rodou nesta semana).")
        return 0
    r = processar(simular=getattr(args, "simular", False))
    if r.get("texto"):
        print(r["texto"])
        print(f"(simulação: nada gravado nem apagado; o relatório iria para {r['relatorio']})")
        return 0
    print(f"Relatório: {r['relatorio']}\nSaíram do registro: {r['apagados']['agenda']} da agenda, "
          f"{r['apagados']['promessas']} promessas · Laya: +{r['laya']['agenda']} agenda, +{r['laya']['promessas']} promessas")
    return 0
