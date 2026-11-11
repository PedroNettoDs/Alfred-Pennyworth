"""
Alfred — lembrete de remédio: de N em N horas um post-it no computador; sem o "tomei" em 10 min, o número do
Alfred pergunta no WhatsApp.

    adicionar(nome, intervalo_horas)   → VAULT_ALFRED/agenda/remedios.yml (o mesmo nome não duplica)
    pesquisar_cuidados(id)             → o researcher (pesquisa.py) acha as contraindicações e o que não fazer;
                                         o Ollama resume em frases curtas (prompt remedio_cuidados.md)
    passar()                           ← thread da API, a cada 60 s
        proxima_em venceu      → abre a dose: post-it "Tomar X" (recados() → estado.json → Pestinha)
        10 min sem o "tomei"   → o número do Alfred pergunta a Pedro: "Já tomou? sim ou não" (uma vez por dose)
    tomei(id, quando)                  ← "Tomei" no post-it, ferramenta tomei_remedio, resposta no WhatsApp
        fecha a dose; a próxima fica para quando + intervalo
    resposta_whatsapp(texto)           ← whatsapp._pedido_de_pedro, antes do motor da conversa
        "sim"  → fecha a dose na hora da resposta e pergunta "que horas mais ou menos?"; a hora dita acerta a contagem
        "não"  → espera; o próximo "sim" fecha a dose e a contagem parte dele

Só há uma dose aberta por remédio: enquanto ela espera o "tomei", não nasce outro post-it.
Ordem das travas: remedios sozinha (o WhatsApp e a pesquisa são chamados fora dela).
"""

import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

import core
import pesquisa
import whatsapp
from _shared import lib_atividade, lib_whatsapp
from _shared.lib_alfred import log, ollama_generate_json
from _shared.lib_templates import render_template

ESPERA_MIN = 10            # minutos com o post-it na tela, sem "tomei", até o WhatsApp perguntar
INTERVALO_MAX_H = 24 * 7
MAX_CUIDADOS = 5           # o detalhe do post-it tem 600 caracteres
CUIDADO_MAX = 90
SINTESE_MAX = 6000         # caracteres da pesquisa que vão ao resumo
TEMA = "{nome}: contraindicações, interações e o que não fazer durante o uso (álcool, alimentos, outros remédios)"
AVISO = "Resumo de pesquisa na web: confirme na bula."
SEM_CUIDADOS = {"pendente": "Cuidados ainda não pesquisados: confira a bula.",
                "falhou": "Não consegui pesquisar os cuidados: confira a bula."}

SCHEMA_CUIDADOS = {
    "type": "object",
    "properties": {"cuidados": {"type": "array", "items": {"type": "string"}}},
    "required": ["cuidados"],
}

# respostas de Pedro à pergunta do WhatsApp (já sem acento, minúsculas, sem pontuação nas pontas)
_SIM = {"sim", "s", "ja", "tomei", "ja tomei", "sim tomei", "tomei sim", "sim ja tomei"}
_NAO = {"nao", "n", "ainda nao", "nao ainda", "nao tomei", "ainda nao tomei"}
_AGORA = {"agora", "agorinha", "agora mesmo", "agora pouco", "acabei de tomar", "tomei agora"}
_HA = re.compile(r"(?:ha|faz)\s+(?:(\d{1,3})|uma?|(meia))\s*(min|minutos?|h|horas?)")
_HORA = re.compile(r"(?:(?:as|umas|pelas|la pelas|por volta das?|perto das?|foi|tomei)\s+)*"
                   r"(\d{1,2})(?:[:h](\d{2}))?\s*(?:h|hs|hrs?|horas?)?")


# ══════════════════════════════════════════════════════════════
#  Armazenamento
# ══════════════════════════════════════════════════════════════

def arquivo() -> Path:
    return core.vault() / "agenda" / "remedios.yml"


def _load() -> dict:
    f = arquivo()
    if not f.exists():
        return {"remedios": []}
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except Exception as e:
        core.preservar_corrompido(f, f"YAML inválido: {e}", "remedios")
        return {"remedios": []}
    if not isinstance(data, dict) or not isinstance(data.get("remedios"), list):
        return {"remedios": []}
    return data


def _trava():
    return core.trava_arquivo(arquivo())


def _save(data: dict) -> None:
    core.gravar_atomico(arquivo(), yaml.dump(data, allow_unicode=True, sort_keys=False))


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _chave(texto) -> str:
    """Texto para comparar: sem acento, minúsculo, espaços simples."""
    return " ".join(core._ascii(str(texto or "")).lower().split())


def _do_id(data: dict, rid: str) -> Optional[dict]:
    return next((r for r in data["remedios"] if r.get("id") == rid), None)


def listar() -> list[dict]:
    return _load()["remedios"]


def obter(rid: str) -> Optional[dict]:
    return _do_id(_load(), rid)


def achar(nome: str) -> Optional[dict]:
    """O remédio com este nome (sem olhar acento nem maiúscula)."""
    return next((r for r in listar() if _chave(r["nome"]) == _chave(nome)), None)


def adicionar(nome: str, intervalo_horas: float, primeira: Optional[datetime] = None,
              agora: Optional[datetime] = None) -> dict:
    """Lembrete novo, com a primeira dose em `primeira` (padrão: agora). Um nome que já existe só troca o intervalo
    (e a próxima dose, se `primeira` veio). Nome vazio ou intervalo fora de 0–168 h → ValueError."""
    nome = " ".join(str(nome or "").split())[:60]
    horas = float(intervalo_horas)
    if not nome:
        raise ValueError("falta o nome do remédio")
    if not 0 < horas <= INTERVALO_MAX_H:
        raise ValueError(f"o intervalo precisa ficar entre 0 e {INTERVALO_MAX_H} horas")
    agora = agora or core._now()
    with _trava():
        data = _load()
        r = next((x for x in data["remedios"] if _chave(x["nome"]) == _chave(nome)), None)
        if r:
            r["intervalo_horas"] = horas
            if primeira and not r.get("dose"):
                r["proxima_em"] = _iso(primeira)
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", _chave(nome)).strip("-")[:24] or "remedio"
            r = {"id": f"{slug}-{secrets.token_hex(2)}", "nome": nome, "intervalo_horas": horas,
                 "criado_em": _iso(agora), "proxima_em": _iso(primeira or agora), "cuidados": [],
                 "pesquisa": {"status": "pendente"}}
            data["remedios"].append(r)
        _save(data)
    return r


def parar(nome: str) -> Optional[dict]:
    """Apaga o lembrete (e a dose aberta, com o post-it dela). → o remédio que saiu, ou None se não existia."""
    with _trava():
        data = _load()
        r = next((x for x in data["remedios"] if _chave(x["nome"]) == _chave(nome)), None)
        if r:
            data["remedios"].remove(r)
            _save(data)
    return r


# ══════════════════════════════════════════════════════════════
#  Cuidados: contraindicações e o que não fazer (researcher + Ollama)
# ══════════════════════════════════════════════════════════════

def pesquisar_cuidados(rid: str) -> list[str]:
    """Pesquisa o remédio (modo rápido do researcher, até 5 min; a pesquisa fica no cofre) e guarda até MAX_CUIDADOS
    frases curtas no lembrete. Researcher ou Ollama fora do ar → [] e pesquisa.status = "falhou"."""
    r = obter(rid)
    if not r:
        return []
    cuidados, slug = [], ""
    try:
        resp = pesquisa.pesquisar(TEMA.format(nome=r["nome"]), profundo=False)
        slug = pesquisa.slug_resultado(resp)
        sintese = resp.get("synthesis") or (pesquisa.obter(slug) or {}).get("sintese") or ""
        if sintese.strip():
            dados = ollama_generate_json(
                render_template("remedio_cuidados", {"nome": r["nome"], "sintese": sintese[:SINTESE_MAX]}),
                schema=SCHEMA_CUIDADOS, timeout=180) or {}
            brutos = dados.get("cuidados") if isinstance(dados, dict) else None
            cuidados = [" ".join(str(c).split())[:CUIDADO_MAX] for c in brutos or [] if str(c).strip()][:MAX_CUIDADOS]
    except pesquisa.ErroPesquisa as e:
        log(f"[remedios] {r['nome']}: pesquisa dos cuidados falhou ({e})")
    with _trava():
        data = _load()
        atual = _do_id(data, rid)
        if atual:
            atual["cuidados"] = cuidados
            atual["pesquisa"] = {"status": "pronta" if cuidados else "falhou", "slug": slug}
            _save(data)
    return cuidados


# ══════════════════════════════════════════════════════════════
#  Doses: post-it, pergunta no WhatsApp e o "tomei"
# ══════════════════════════════════════════════════════════════

def _hm(iso: str) -> str:
    d = core._parse_dt(iso)
    return f"{d:%H:%M}" if d else "?"


def _horas(r: dict) -> str:
    return f"{r['intervalo_horas']:g} h"


def _id_recado(r: dict) -> str:
    """Id do post-it da dose aberta: muda a cada dose, então o post-it dispensado de uma não esconde a seguinte."""
    return f"remedio:{r['id']}:{int(core._parse_dt(r['dose']['devida_em']).timestamp())}"


def _detalhe(r: dict) -> str:
    linhas = [f"Dose das {_hm(r['dose']['devida_em'])} · a cada {_horas(r)}"]
    if r.get("cuidados"):
        linhas += ["Cuidados:", *(f"• {c}" for c in r["cuidados"]), AVISO]
    else:
        linhas.append(SEM_CUIDADOS.get((r.get("pesquisa") or {}).get("status"), SEM_CUIDADOS["pendente"]))
    return "\n".join(linhas)


def recados() -> list[dict]:
    """As doses abertas no formato do estado.json: um post-it por dose, com o horário em que ele foi criado."""
    return [{"id": _id_recado(r), "tipo": "remedio", "texto": f"Tomar {r['nome']}", "detalhe": _detalhe(r),
             "quando": core._parse_dt(r["dose"]["avisada_em"]).timestamp()} for r in listar() if r.get("dose")]


def _pergunta(r: dict) -> str:
    texto = f"💊 Hora do *{r['nome']}* (dose das {_hm(r['dose']['devida_em'])}). Já tomou? Responda *sim* ou *não*."
    if r.get("cuidados"):
        texto += "\nCuidados: " + "; ".join(r["cuidados"][:3]) + "."
    return texto


def _enviar_a_pedro(texto: str) -> bool:
    """Manda ao WhatsApp de Pedro pelo número do Alfred (a sessão de papel assistente). Sem ela → False."""
    inst = next((s["instancia"] for s in whatsapp.sessoes() if s["papel"] == "assistente"), None)
    numero = lib_whatsapp.numero_pedro()
    return bool(inst and numero and lib_whatsapp.enviar_texto(numero, texto, inst=inst).get("ok"))


def passar(agora: Optional[datetime] = None) -> list[tuple[str, str]]:
    """Uma volta do laço: abre as doses vencidas e pergunta no WhatsApp pelas que esperam há ESPERA_MIN.
    → [("dose" | "pergunta", id do remédio)] do que aconteceu nesta volta."""
    agora = agora or core._now()
    feitos, abertas, perguntar = [], [], []
    with _trava():
        data = _load()
        for r in data["remedios"]:
            dose = r.get("dose")
            if not dose:
                if (core._parse_dt(r.get("proxima_em")) or agora) <= agora:
                    r["dose"] = {"devida_em": r.get("proxima_em") or _iso(agora), "avisada_em": _iso(agora)}
                    r.pop("hora_perguntada_em", None)
                    abertas.append(r)
            elif not dose.get("perguntada_em") and \
                    agora - core._parse_dt(dose["avisada_em"]) >= timedelta(minutes=ESPERA_MIN):
                perguntar.append(r)
        if abertas:
            _save(data)
    for r in abertas:
        lib_atividade.evento("remedio", f"Tomar {r['nome']}")
        feitos.append(("dose", r["id"]))
    for r in perguntar:
        enviou = _enviar_a_pedro(_pergunta(r))
        with _trava():
            data = _load()
            dose = (_do_id(data, r["id"]) or {}).get("dose")
            if not dose or dose.get("devida_em") != r["dose"]["devida_em"]:
                continue                       # Pedro marcou o "tomei" enquanto a mensagem saía
            if enviou:
                dose["perguntada_em"] = _iso(agora)
                feitos.append(("pergunta", r["id"]))
            elif dose.get("sem_whatsapp"):
                continue                       # já registrado; tenta de novo na próxima volta
            else:
                dose["sem_whatsapp"] = True
                log(f"[remedios] {r['nome']}: não consegui perguntar no WhatsApp (sessão assistente ou envio)")
            _save(data)
    return feitos


def _fechar(r: dict, quando: datetime, via: str) -> None:
    r.pop("dose", None)
    r.pop("hora_perguntada_em", None)
    r["ultima"] = {"tomada_em": _iso(quando), "via": via}
    r["proxima_em"] = _iso(quando + timedelta(hours=r["intervalo_horas"]))


def tomei(rid: str, quando: Optional[datetime] = None, via: str = "", recado: Optional[str] = None) -> Optional[dict]:
    """Pedro tomou às `quando` (padrão: agora): fecha a dose aberta e a contagem recomeça dali. Com `recado`, só
    vale se o post-it for o da dose aberta. → o remédio, ou None se não existe (ou o post-it é de outra dose)."""
    with _trava():
        data = _load()
        r = _do_id(data, rid)
        if not r or (recado and (not r.get("dose") or _id_recado(r) != recado)):
            return None
        _fechar(r, quando or core._now(), via)
        _save(data)
    return r


def recado_feito(recado_id: str, agora: Optional[datetime] = None) -> bool:
    """O "Tomei" do post-it (pedido recado_feito do Pestinha)."""
    partes = str(recado_id).split(":")
    return len(partes) == 3 and tomei(partes[1], agora, "postit", recado=recado_id) is not None


def anotado(rs: list[dict]) -> str:
    """Confirmação para Pedro: o que foi anotado e quando é a próxima dose de cada remédio."""
    return "\n".join(f"✔ Anotado: {r['nome']} às {_hm(r['ultima']['tomada_em'])}. "
                     f"Próxima dose às {_hm(r['proxima_em'])}." for r in rs)


def hora_tomada(texto: str, agora: Optional[datetime] = None) -> Optional[datetime]:
    """'agora' · 'há 20 min' · 'faz meia hora' · '14:10' · '14h10' · 'às 13h' · 'umas 2' → quando Pedro tomou.
    A hora dita é sempre a ocorrência mais recente que já passou ('umas 2' às 14:30 = 14:00; '23h' = ontem).
    None se o texto inteiro não for uma hora."""
    agora = agora or core._now()
    t = _chave(texto).strip(" .!?,")
    if t in _AGORA:
        return agora
    if m := _HA.fullmatch(t):
        n = int(m.group(1)) if m.group(1) else (0.5 if m.group(2) else 1)
        return agora - (timedelta(hours=n) if m.group(3).startswith("h") else timedelta(minutes=n))
    m = _HORA.fullmatch(t)
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        return None
    hh, base = int(m.group(1)), agora.replace(hour=0, minute=int(m.group(2) or 0), second=0, microsecond=0)
    horas = [hh, hh + 12] if 1 <= hh < 12 else [hh]          # "umas 2" pode ser 2 da tarde
    passadas = [c for h in horas for c in (base.replace(hour=h), base.replace(hour=h) - timedelta(days=1))
                if c <= agora + timedelta(minutes=5)]
    return min(max(passadas), agora)


def resposta_whatsapp(texto: str, agora: Optional[datetime] = None) -> Optional[str]:
    """Pedro escreveu ao número do Alfred. Se for a resposta à pergunta de uma dose ("sim", "não") ou a hora em
    que tomou, anota e devolve o que responder a ele; senão None (a mensagem segue para o motor da conversa)."""
    agora = agora or core._now()
    palavra = _chave(texto).strip(" .!?,")
    quando = hora_tomada(texto, agora)
    with _trava():
        data = _load()
        sem_hora = [r for r in data["remedios"] if r.get("hora_perguntada_em")]
        if sem_hora and quando:                # a resposta de "que horas mais ou menos?"
            for r in sem_hora:
                _fechar(r, quando, "whatsapp")
            _save(data)
            return anotado(sem_hora)
        perguntadas = [r for r in data["remedios"] if (r.get("dose") or {}).get("perguntada_em")]
        if not perguntadas or palavra not in _SIM | _NAO:
            return None
        if palavra in _NAO:
            for r in perguntadas:
                r["dose"]["resposta"] = "nao"
            _save(data)
            return "Certo. Quando tomar, responda *sim* e a contagem recomeça dali."
        falta_hora = [r for r in perguntadas if r["dose"].get("resposta") != "nao"]   # depois do "não", o "sim" é a hora
        for r in perguntadas:
            _fechar(r, agora, "whatsapp")
        for r in falta_hora:
            r["hora_perguntada_em"] = _iso(agora)
        _save(data)
    if not falta_hora:
        return anotado(perguntadas)
    return (f"✔ {', '.join(r['nome'] for r in falta_hora)}: anotado. Que horas mais ou menos você tomou? "
            "(ex.: 14:30, \"agora\", \"há 20 min\")")
