"""
Alfred — redigir: escreve o e-mail que Pedro está compondo no Thunderbird. Mais detalhes em REDIGIR.md.

    botão "Redigir e-mail com o Alfred" (thunderbird-ext-redigir)
        ↓ POST /redigir        destinatários, assunto, notas de Pedro, mensagem citada, eventos do calendário
        ↓ classificar()        o Laya define o contexto; termos de evento/entrevista valem mesmo sem o Laya
        ↓ verificar_agenda()   só em evento/entrevista: data citada × calendário, antes de escrever
        ↓ LLM                  notas + memória (lib_memoria) + notas dos cofres (lib_rag) + agenda
    status "pronto"      → a extensão põe o texto na janela
    status "aguardando"  → fica em VAULT_ALFRED/redigir/redacoes.yml; aviso em /painel/redigir; a extensão espera
                           Pedro liberar (GET /redigir/{id}) e só então põe o texto na janela

O Alfred nunca envia: o texto só vai para a janela de composição.
"""

import re
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

import agenda
import core
from _shared import lib_laya, lib_memoria, lib_rag
from _shared.lib_alfred import log, ollama_generate_json
from _shared.lib_templates import render_template

CONTEXTOS = {
    "evento":      "inviting, confirming or asking about an event, meeting, webinar, conference or appointment",
    "entrevista":  "scheduling, confirming or following up on a job interview",
    "candidatura": "applying for a job, internship or talent network",
    "trabalho":    "work, clients, university or study matters",
    "pessoal":     "personal matters with friends or family",
    "outro":       "anything else",
}
COM_AGENDA = ("evento", "entrevista")
_TERMOS_AGENDA = re.compile(
    r"\b(entrevista\w*|interview\w*|evento\w*|events?|reuni\w*|meetings?|convite\w*|invit\w*|palestra\w*|"
    r"webinar\w*|workshop\w*|congresso\w*|conference\w*|consultas?|appointment\w*)\b", re.I)
_ENTREVISTA = re.compile(r"\b(entrevista\w*|interview\w*)\b", re.I)

NOTAS_MAX = 4000
ORIGINAL_MAX = 6000
MEMORIA_CHARS = 4000
DIAS_SEM_DATA = 14       # sem data citada, o bloco AGENDA mostra os próximos dias
EVENTOS_MAX = 15
_DIAS_CITADOS = ("segunda|monday", "terca|tuesday", "quarta|wednesday", "quinta|thursday", "sexta|friday",
                 "sabado|saturday", "domingo|sunday")
DIAS_SEMANA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
FECHADOS_GUARDADOS = 30
ALFRED_ATIVOS = ("pendente", "em_execucao", "no_calendario", "erro")

# "idioma" vem antes de "corpo" de propósito: declarar o idioma primeiro evita o modelo cair no português do prompt.
SCHEMA = {"type": "object", "properties": {"idioma": {"type": "string"}, "corpo": {"type": "string"}},
          "required": ["idioma", "corpo"]}
SCHEMA_DATA = {
    "type": "object",
    "properties": {k: {"type": "string"} for k in ("titulo", "data", "hora", "hora_fim", "local")},
    "required": ["titulo", "data", "hora", "hora_fim", "local"],
}


class RedigirErro(Exception):
    """O LLM não devolveu texto."""


# ══════════════════════════════════════════════════════════════
#  Contexto
# ══════════════════════════════════════════════════════════════

def _sem_acento(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")


def classificar(assunto: str, notas: str, original: str) -> str:
    """
    Contexto do e-mail. O Laya decide; termos explícitos de evento/entrevista no assunto, nas notas ou no começo da
    mensagem citada vencem qualquer outro rótulo, porque deixar passar um compromisso custa mais que um aviso a mais.
    """
    trecho = f"{assunto}\n{notas}\n{original[:1500]}"
    # Só campos preenchidos: "Notas:" e "Mensagem citada:" vazios levam o Laya a responder "outro".
    estado = "\n".join(f"{k}: {v}" for k, v in (("Assunto", assunto), ("Notas", notas), ("Mensagem citada", original[:1500])) if v)
    rotulo = lib_laya.escolher(estado, "What is this outgoing e-mail about?", CONTEXTOS)
    if rotulo not in COM_AGENDA and _TERMOS_AGENDA.search(_sem_acento(trecho)):
        return "entrevista" if _ENTREVISTA.search(_sem_acento(trecho)) else "evento"
    return rotulo or "outro"


# ══════════════════════════════════════════════════════════════
#  Agenda
# ══════════════════════════════════════════════════════════════

def _momento(valor) -> Optional[datetime]:
    """ISO (com ou sem fuso) → datetime no fuso local; dia inteiro vira 00:00. None se ilegível."""
    try:
        d = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None
    tz = core._now().tzinfo
    return d.replace(tzinfo=tz) if d.tzinfo is None else d.astimezone(tz)


def _normalizar(bruto: dict, origem: str) -> Optional[dict]:
    inicio = _momento(bruto.get("inicio"))
    if not inicio:
        return None
    dia_inteiro = bool(bruto.get("dia_inteiro"))
    fim = _momento(bruto.get("fim")) if bruto.get("fim") else None
    return {"titulo": str(bruto.get("titulo") or "(sem título)")[:120], "inicio": inicio, "fim": fim,
            "dia_inteiro": dia_inteiro, "local": str(bruto.get("local") or "")[:120], "origem": origem}


def _eventos(do_calendario: Optional[list]) -> list[dict]:
    """Eventos do Thunderbird + itens que o Alfred anotou e ainda não apareceram lá, em ordem de início."""
    lista = [e for e in (_normalizar(b, "Thunderbird") for b in do_calendario or []) if e]
    vistos = {(agenda._norm(e["titulo"]), e["inicio"].date()) for e in lista}
    for item in agenda.listar():
        if item.get("status") not in ALFRED_ATIVOS:
            continue
        e = _normalizar(item, "Alfred")
        if e and (agenda._norm(e["titulo"]), e["inicio"].date()) not in vistos:
            lista.append(e)
    return sorted(lista, key=lambda e: e["inicio"])


def _choca(e: dict, inicio: datetime, fim: datetime) -> bool:
    if e["dia_inteiro"]:
        return False
    return e["inicio"] < fim and (e["fim"] or e["inicio"] + timedelta(hours=1)) > inicio


def _rotulo(e: dict) -> str:
    quando = e["inicio"].strftime("%d/%m/%Y") + (" (dia inteiro)" if e["dia_inteiro"] else e["inicio"].strftime(" %H:%M"))
    return f"{quando} · {e['titulo']}" + (f" · {e['local']}" if e["local"] else "")


def _proximos_dias(ref: datetime) -> str:
    """Calendário dos próximos 14 dias para o prompt: sozinho, o modelo erra "próxima terça" por uma semana."""
    return "\n".join(f"{(ref + timedelta(days=n)):%Y-%m-%d} = {DIAS_SEMANA[(ref + timedelta(days=n)).weekday()]}" for n in range(14))


def _primeira_ocorrencia(inicio: datetime, texto: str, ref: datetime) -> datetime:
    """
    "próxima terça" sem data numérica é a primeira terça depois de hoje. O modelo oscila entre esta e a seguinte, e
    errar aqui esconde um choque de horário. Só age se o dia devolvido for mesmo o citado ("segunda entrevista" não conta).
    """
    limpo = _sem_acento(texto).lower()
    if re.search(r"\b\d{1,2}/\d{1,2}\b|\bdia \d{1,2}\b|\b\d{1,2} de [a-z]+", limpo):
        return inicio
    nomes = _DIAS_CITADOS[inicio.weekday()]
    if not re.search(rf"\b({nomes})\b", limpo):
        return inicio
    salto = ((inicio.weekday() - ref.weekday() - 1) % 7) + 1
    return inicio.replace(year=(ref + timedelta(days=salto)).year, month=(ref + timedelta(days=salto)).month,
                          day=(ref + timedelta(days=salto)).day)


def data_citada(texto: str) -> Optional[dict]:
    """
    Data do evento citado no e-mail → {"titulo", "inicio", "fim", "dia_inteiro", "local"} ou None.
    Não usa agenda.detectar_compromisso: aquele recusa convite ainda não aceito, justamente o caso a checar aqui.
    """
    ref = core._now()
    data = ollama_generate_json(render_template("email_data_citada", {"data_hoje": ref.strftime("%Y-%m-%d %H:%M"), "dias": _proximos_dias(ref), "texto": texto[:2500]}),
                                timeout=120, schema=SCHEMA_DATA, options={**core.OPTS_JSON, "num_predict": 200})
    if not isinstance(data, dict):
        return None
    dia, hora, hora_fim = (str(data.get(k) or "").strip() for k in ("data", "hora", "hora_fim"))
    try:
        inicio = datetime.strptime(f"{dia} {hora or '00:00'}", "%Y-%m-%d %H:%M").replace(tzinfo=ref.tzinfo)
    except ValueError:
        return None
    inicio = _primeira_ocorrencia(inicio, texto, ref)
    dia = inicio.strftime("%Y-%m-%d")
    if inicio.date() < ref.date():
        return None
    fim = None
    if hora and hora_fim:
        try:
            fim = datetime.strptime(f"{dia} {hora_fim}", "%Y-%m-%d %H:%M").replace(tzinfo=ref.tzinfo)
        except ValueError:
            pass
    return {"titulo": str(data.get("titulo") or "")[:60], "inicio": inicio, "fim": fim, "dia_inteiro": not hora,
            "local": str(data.get("local") or "")[:120]}


def verificar_agenda(assunto: str, notas: str, original: str, do_calendario: Optional[list]) -> dict:
    """
    → {"compromisso", "conflitos", "eventos", "calendario_lido", "resumo"} (só strings e listas: vai para o YAML).
    `do_calendario` None = a extensão não conseguiu ler o calendário; o resumo diz isso.
    """
    texto = f"Assunto: {assunto}\n{notas}\n{original}".strip()
    achado = data_citada(texto)
    eventos = _eventos(do_calendario)
    lido = do_calendario is not None
    conflitos, compromisso = [], None
    if achado:
        inicio = achado["inicio"]
        fim = achado.get("fim") or inicio + timedelta(hours=1)
        dia_inteiro = bool(achado.get("dia_inteiro"))
        compromisso = {"titulo": achado.get("titulo", ""), "quando": inicio.strftime("%d/%m/%Y") + ("" if dia_inteiro else inicio.strftime(" %H:%M")),
                       "local": achado.get("local", "")}
        do_dia = [e for e in eventos if e["inicio"].date() == inicio.date()]
        if not dia_inteiro:
            conflitos = [e for e in do_dia if _choca(e, inicio, fim)]
        lista = do_dia
        resumo = (f"Choque de horário em {compromisso['quando']}: " + "; ".join(_rotulo(e) for e in conflitos) if conflitos
                  else f"Nenhum choque em {compromisso['quando']}. Eventos no dia: {len(do_dia)}.")
    else:
        limite = core._now() + timedelta(days=DIAS_SEM_DATA)
        lista = [e for e in eventos if core._now() <= e["inicio"] <= limite][:EVENTOS_MAX]
        resumo = f"Não achei data no e-mail. Próximos {DIAS_SEM_DATA} dias: {len(lista)} evento(s)."
    if not lido:
        resumo += " Atenção: não consegui ler o calendário do Thunderbird; só vi o que o Alfred anotou."
    return {"compromisso": compromisso, "conflitos": [_rotulo(e) for e in conflitos],
            "eventos": [_rotulo(e) for e in lista], "calendario_lido": lido, "resumo": resumo}


def _bloco_agenda(v: Optional[dict]) -> str:
    if not v:
        return "(não se aplica a este e-mail)"
    linhas = [v["resumo"]]
    if v["eventos"]:
        linhas.append("Eventos de Pedro:\n" + "\n".join(f"- {e}" for e in v["eventos"]))
    return "\n".join(linhas)


# ══════════════════════════════════════════════════════════════
#  Escrita
# ══════════════════════════════════════════════════════════════

def _perfil() -> dict:
    try:
        return core.carregar_cfg()[1].get("_perfil") or {}
    except Exception:
        return {}


def _notas_pedro(consulta: str) -> str:
    blocos, total = [], 0
    for r in lib_rag.buscar(consulta, cofre="pedro", limite=6):
        bloco = f"[{r['caminho']}]\n{r['trecho'].strip()}"
        total += len(bloco)
        if total > MEMORIA_CHARS:
            break
        blocos.append(bloco)
    return "\n\n".join(blocos)


def _memoria(de_quem: str, assunto: str, notas: str, original: str) -> tuple[str, str]:
    """→ (fatos guardados sobre Pedro e seus contatos, trechos das notas dele). Só o cofre de Pedro: o do Alfred é noticiário."""
    consulta = f"{assunto} {de_quem} {notas} {original[:300]}".strip()
    return (lib_memoria.contexto(consulta) or "(nada encontrado)",
            _notas_pedro(consulta) or "(nada encontrado)")


def _notas_para_prompt(notas: str, verificacao: Optional[dict]) -> str:
    """Com choque de horário a agenda vence as notas: como regra geral do prompt, o modelo confirmava do mesmo jeito."""
    texto = notas or "(Pedro não escreveu notas)"
    if verificacao and verificacao["conflitos"]:
        texto += ("\n\nCORREÇÃO DE ALFRED, que vale mais que as notas acima: Pedro NÃO está livre nesse horário. Não confirme. "
                  "Agradeça, diga que nesse horário já tem um compromisso e peça outra opção de horário. Não diga qual é o compromisso.")
    return texto


def _escrever(pedido: dict, contexto: str, verificacao: Optional[dict]) -> str:
    perfil = _perfil()
    assinatura = ("Termine na despedida, sem o nome: o Thunderbird acrescenta a assinatura."
                  if pedido["tem_assinatura"] else "Assine como Pedro Netto.")
    fatos, cofres = _memoria(", ".join(pedido["para"]), pedido["assunto"], pedido["notas"], pedido["original"])
    prompt = render_template("email_redigir", {
        "data_hoje":        core._now().strftime("%Y-%m-%d (%A)"),
        "perfil_descricao": perfil.get("descricao", ""),
        "tom":              perfil.get("tom", ""),
        "contexto":         contexto,
        "remetente":        pedido["de"],
        "destinatarios":    ", ".join(pedido["para"]),
        "assunto":          pedido["assunto"],
        "notas":            _notas_para_prompt(pedido["notas"], verificacao),
        "fatos":            fatos,
        "cofres":           cofres,
        "base_idioma":      (pedido["notas"] or pedido["original"] or pedido["assunto"])[:300],
        "agenda":           _bloco_agenda(verificacao),
        "original":         pedido["original"] or "(não há: é um e-mail novo)",
        "assinatura":       assinatura,
    })
    data = ollama_generate_json(prompt, timeout=180, schema=SCHEMA, options={"temperature": 0.4, "num_predict": 1200})
    corpo = str(data.get("corpo") or "").strip() if isinstance(data, dict) else ""
    if not corpo:
        raise RedigirErro("O modelo não devolveu texto. Veja se o Ollama está no ar.")
    return corpo


def redigir(de: str, para: list[str], assunto: str, notas: str, original: str, tem_assinatura: bool,
            eventos: Optional[list]) -> dict:
    """→ {"status": "pronto", "contexto", "texto"} ou a entrada "aguardando" gravada para o painel."""
    pedido = {"de": de, "para": para, "assunto": assunto.strip(), "notas": notas.strip()[:NOTAS_MAX],
              "original": original.strip()[:ORIGINAL_MAX], "tem_assinatura": tem_assinatura}
    contexto = classificar(pedido["assunto"], pedido["notas"], pedido["original"])
    log(f"[redigir] contexto: {contexto}")
    if contexto not in COM_AGENDA:
        return {"status": "pronto", "contexto": contexto, "texto": _escrever(pedido, contexto, None)}

    verificacao = verificar_agenda(pedido["assunto"], pedido["notas"], pedido["original"], eventos)
    texto = _escrever(pedido, contexto, verificacao)
    item = {"id": f"{core._now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}", "status": "aguardando",
            "criado": core._now().isoformat(timespec="seconds"), "contexto": contexto, "para": para,
            "assunto": pedido["assunto"], "notas": pedido["notas"], "agenda": verificacao, "texto": texto}
    with _trava():
        data = _load()
        data["itens"].insert(0, item)
        _save(data)
    core._notify("Alfred — e-mail aguardando", f"{pedido['assunto']}\n{verificacao['resumo']}\nhttp://127.0.0.1:7073/painel/redigir")
    return item


def reformular(trecho: str, instrucao: str, assunto: str, corpo: str) -> str:
    """Reescreve só o trecho selecionado. Sem passar pelo painel: quem pediu está olhando para o texto."""
    consulta_corpo = f"{instrucao} {trecho}"[:NOTAS_MAX]
    fatos, notas_pedro = _memoria("", assunto, consulta_corpo, "")
    perfil = _perfil()
    prompt = render_template("email_reformular", {
        "perfil_descricao": perfil.get("descricao", ""),
        "tom":              perfil.get("tom", ""),
        "assunto":          assunto.strip(),
        "instrucao":        instrucao.strip() or "(sem pedido: deixe mais claro e natural)",
        "corpo":            corpo.strip()[:ORIGINAL_MAX],
        "fatos":            fatos,
        "notas_pedro":      notas_pedro,
        "trecho":           trecho.strip()[:NOTAS_MAX],
    })
    data = ollama_generate_json(prompt, timeout=180, schema=SCHEMA, options={"temperature": 0.4, "num_predict": 1200})
    texto = str(data.get("corpo") or "").strip() if isinstance(data, dict) else ""
    if not texto:
        raise RedigirErro("O modelo não devolveu texto. Veja se o Ollama está no ar.")
    return texto


# ══════════════════════════════════════════════════════════════
#  Fila do painel
# ══════════════════════════════════════════════════════════════

def arquivo() -> Path:
    d = core.vault() / "redigir"
    d.mkdir(parents=True, exist_ok=True)
    return d / "redacoes.yml"


def _load() -> dict:
    f = arquivo()
    if not f.exists():
        return {"itens": []}
    try:
        data = core.ler_yaml(f.read_text(encoding="utf-8")) or {}
    except Exception as e:
        core.preservar_corrompido(f, f"YAML inválido: {e}", "redigir")
        return {"itens": []}
    return data if isinstance(data, dict) and isinstance(data.get("itens"), list) else {"itens": []}


def _trava():
    return core.trava_arquivo(arquivo())


def _save(data: dict) -> None:
    fechados = [i for i in data["itens"] if i.get("status") != "aguardando"]
    sobra = {i["id"] for i in fechados[FECHADOS_GUARDADOS:]}
    data["itens"] = [i for i in data["itens"] if i["id"] not in sobra]
    core.gravar_atomico(arquivo(), yaml.dump(data, allow_unicode=True, sort_keys=False))


def listar(status: Optional[str] = None) -> list[dict]:
    itens = _load()["itens"]
    return [i for i in itens if i.get("status") == status] if status else itens


def obter(rid: str) -> Optional[dict]:
    return next((i for i in _load()["itens"] if i.get("id") == rid), None)


def resolver(rid: str, status: str, texto: Optional[str] = None) -> Optional[dict]:
    """aguardando → liberado | recusado. → item; None se não existe. Já resolvido: devolve sem mudar (o chamador vê o status)."""
    with _trava():
        data = _load()
        item = next((i for i in data["itens"] if i.get("id") == rid), None)
        if item and item.get("status") == "aguardando":
            item["status"] = status
            item["resolvido"] = core._now().isoformat(timespec="seconds")
            if texto is not None:
                item["texto"] = texto
            _save(data)
            item = {**item, "_mudou": True}
    return item
