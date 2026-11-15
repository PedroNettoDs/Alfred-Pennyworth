"""
Alfred — lembrete de remédio: de N em N horas um post-it no computador; sem o "tomei" em 10 min, o número do
Alfred pergunta no WhatsApp.

    adicionar(nome, intervalo_horas)   → VAULT_ALFRED/agenda/remedios.yml (o mesmo nome não duplica)
    iniciar_pesquisa(id)               → pesquisa profunda do researcher (pesquisa.py): ele lê as páginas (bulas) e
                                         acha as contraindicações e o que não fazer, em segundo plano (5 a 20 min)
    passar()                           ← thread da API, a cada 60 s
        proxima_em venceu      → abre a dose: post-it "Tomar X" (recados() → estado.json → Pestinha)
        10 min sem o "tomei"   → o número do Alfred pergunta a Pedro: "Já tomou? sim ou não" (uma vez por dose)
        pesquisa terminou      → o Ollama resume em frases curtas (prompt remedio_cuidados.md), que vão para o
                                 post-it, para a pergunta da dose e, uma vez, para o WhatsApp de Pedro
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
from _shared import lib_atividade, lib_db, lib_whatsapp
from _shared.lib_alfred import log, ollama_generate_json
from _shared.lib_templates import render_template

ESPERA_MIN = 10            # minutos com o post-it na tela, sem "tomei", até o WhatsApp perguntar
INTERVALO_MAX_H = 24 * 7
DIAS_MAX = 365             # duração máxima de um tratamento com fim marcado
MAX_CUIDADOS = 6           # o detalhe do post-it tem 600 caracteres
CUIDADO_MAX = 80
SINTESE_MAX = 12000        # caracteres do relatório da pesquisa que vão ao resumo
PESQUISA_MAX_MIN = 180     # pesquisa profunda que não termina nesse tempo conta como falha. Folgado de propósito:
                           # com Pedro usando o Alfred, a porta da GPU segura o job (até 300 s por chamada ao modelo)
TEMA = "{nome}: contraindicações, interações e o que não fazer durante o uso (álcool, alimentos, outros remédios)"
AVISO = "Resumo de pesquisa na web: confirme na bula."
SEM_CUIDADOS = {"pendente": "Cuidados ainda não pesquisados: confira a bula.",
                "andamento": "Ainda pesquisando os cuidados: confira a bula.",
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
              agora: Optional[datetime] = None, ultima: Optional[datetime] = None, dias: Optional[float] = None) -> dict:
    """Lembrete novo. A primeira dose é em `primeira` (padrão: agora); se Pedro já tomou uma (`ultima`), a próxima
    é ela + o intervalo: a conta é feita aqui, não por quem chama. `dias` é a duração do tratamento, contada da
    primeira dose: passado isso o lembrete sai sozinho (passar). Um nome que já existe não duplica: troca o
    intervalo e o que mais vier. Nome vazio, intervalo fora de 0–168 h ou dias fora de 0–365 → ValueError."""
    nome = " ".join(str(nome or "").split())[:60]
    horas = float(intervalo_horas)
    if not nome:
        raise ValueError("falta o nome do remédio")
    if not 0 < horas <= INTERVALO_MAX_H:
        raise ValueError(f"o intervalo precisa ficar entre 0 e {INTERVALO_MAX_H} horas")
    if dias is not None and not 0 < float(dias) <= DIAS_MAX:
        raise ValueError(f"a duração precisa ficar entre 0 e {DIAS_MAX} dias")
    agora = agora or core._now()
    with _trava():
        data = _load()
        r = next((x for x in data["remedios"] if _chave(x["nome"]) == _chave(nome)), None)
        if r:
            r["intervalo_horas"] = horas
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", _chave(nome)).strip("-")[:24] or "remedio"
            r = {"id": f"{slug}-{secrets.token_hex(2)}", "nome": nome, "intervalo_horas": horas,
                 "criado_em": _iso(agora), "proxima_em": _iso(agora), "cuidados": [],
                 "pesquisa": {"status": "pendente"}}
            data["remedios"].append(r)
        if ultima:
            _fechar(r, ultima, "conversa")
        elif primeira and not r.get("dose"):
            r["proxima_em"] = _iso(primeira)
        if dias is not None:
            r["ate"] = _iso((ultima or core._parse_dt(r["proxima_em"])) + timedelta(days=float(dias)))
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

def _gravar_pesquisa(rid: str, estado: dict, cuidados: Optional[list] = None) -> dict:
    """Grava o estado da pesquisa (e os cuidados, se vieram) no lembrete. → o estado."""
    with _trava():
        data = _load()
        r = _do_id(data, rid)
        if r:
            r["pesquisa"] = estado
            if cuidados is not None:
                r["cuidados"] = cuidados
            _save(data)
    return estado


def _resumir(rid: str, nome: str, slug: str, sintese: str) -> dict:
    """O relatório da pesquisa vira até MAX_CUIDADOS frases curtas (Ollama, só com o que está no relatório).
    Relatório vazio, Ollama fora do ar ou nenhum cuidado achado → pesquisa.status = "falhou"."""
    sintese = sintese or (pesquisa.obter(slug) or {}).get("sintese") or ""
    cuidados = []
    if sintese.strip():
        dados = ollama_generate_json(
            render_template("remedio_cuidados", {"nome": nome, "sintese": sintese[:SINTESE_MAX]}),
            schema=SCHEMA_CUIDADOS, timeout=180) or {}
        brutos = dados.get("cuidados") if isinstance(dados, dict) else None
        cuidados = [" ".join(str(c).split())[:CUIDADO_MAX] for c in brutos or [] if str(c).strip()][:MAX_CUIDADOS]
    return _gravar_pesquisa(rid, {"status": "pronta" if cuidados else "falhou", "slug": slug}, cuidados)


def iniciar_pesquisa(rid: str, agora: Optional[datetime] = None) -> dict:
    """Pede ao researcher a pesquisa PROFUNDA do remédio: ele lê as páginas (bulas, sites de saúde), o que o modo
    rápido não faz. Roda em segundo plano: uns 5 min com a placa livre, mais se Pedro estiver usando o Alfred
    (a porta da GPU dá a vez a ele). → o estado gravado em `pesquisa`:
      andamento  o researcher aceitou (job_id); passar() acompanha e, no fim, resume e avisa Pedro
      pronta     já havia pesquisa profunda recente deste remédio: os cuidados saem agora
      falhou     researcher fora do ar"""
    r = obter(rid)
    if not r:
        return {}
    tema = TEMA.format(nome=r["nome"])
    try:
        resp = pesquisa.pesquisar(tema, profundo=True)
        if resp.get("cache_type") == "semantic":
            # o researcher devolveu a pesquisa de um tema "parecido": a de OUTRO remédio (o tema só muda no nome).
            # Cuidados de outro remédio não servem: pesquisa este de novo, sem cache.
            resp = pesquisa.pesquisar(tema, profundo=True, refresh=True)
    except pesquisa.ErroPesquisa as e:
        log(f"[remedios] {r['nome']}: a pesquisa dos cuidados não começou ({e})")
        return _gravar_pesquisa(rid, {"status": "falhou"})
    if resp.get("job_id"):
        return _gravar_pesquisa(rid, {"status": "andamento", "job_id": resp["job_id"],
                                      "desde": _iso(agora or core._now())})
    return _resumir(rid, r["nome"], pesquisa.slug_resultado(resp), resp.get("synthesis") or "")


def _aviso_cuidados(r: dict) -> str:
    if not r.get("cuidados"):
        return f"💊 Não consegui pesquisar os cuidados do *{r['nome']}*. O lembrete vale do mesmo jeito: confira a bula."
    return (f"💊 Cuidados do *{r['nome']}* (resumo da pesquisa do Alfred; confirme na bula):\n"
            + "\n".join(f"• {c}" for c in r["cuidados"]))


def _acompanhar_pesquisas(agora: datetime) -> list[tuple[str, str]]:
    """As pesquisas em andamento: a que terminou é resumida e os cuidados vão ao WhatsApp de Pedro; a que falhou,
    sumiu ou passou de PESQUISA_MAX_MIN vira "falhou" (e Pedro é avisado). Researcher fora do ar: espera a próxima
    volta. → [("cuidados", id)] das que fecharam nesta volta."""
    feitos = []
    for r in listar():
        p = r.get("pesquisa") or {}
        if p.get("status") != "andamento":
            continue
        velha = agora - (core._parse_dt(p.get("desde")) or agora) > timedelta(minutes=PESQUISA_MAX_MIN)
        try:
            job = pesquisa.andamento(p.get("job_id") or "")
        except KeyError:
            job = {"status": "error", "erro": "o researcher não conhece mais essa pesquisa"}
        except pesquisa.ErroPesquisa as e:
            job = {"status": "error" if velha else "running", "erro": str(e)}
        if job.get("status") == "done":
            res = job.get("resultado") or {}
            _resumir(r["id"], r["nome"], res.get("slug") or "", res.get("synthesis") or "")
        elif job.get("status") != "running" or velha:
            log(f"[remedios] {r['nome']}: pesquisa dos cuidados falhou ({job.get('erro') or job.get('status')})")
            _gravar_pesquisa(r["id"], {"status": "falhou"}, [])
        else:
            continue
        atual = obter(r["id"])
        if atual:                              # o lembrete pode ter sido parado no meio da pesquisa
            if not _enviar_a_pedro(_aviso_cuidados(atual)):
                log(f"[remedios] {r['nome']}: cuidados prontos, mas não consegui mandar no WhatsApp")
            feitos.append(("cuidados", r["id"]))
    return feitos


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
    """Uma volta do laço: abre as doses vencidas, encerra o lembrete cujo tratamento acabou (`ate`), pergunta no
    WhatsApp pelas doses que esperam há ESPERA_MIN e fecha as pesquisas de cuidados que terminaram.
    → [("dose" | "fim" | "pergunta" | "cuidados", id do remédio)] desta volta."""
    agora = agora or core._now()
    feitos, abertas, acabados, perguntar = [], [], [], []
    with _trava():
        data = _load()
        for r in list(data["remedios"]):
            dose = r.get("dose")
            if not dose:
                devida = core._parse_dt(r.get("proxima_em")) or agora
                if devida > agora:
                    continue
                if r.get("ate") and devida >= core._parse_dt(r["ate"]):   # passou a duração do tratamento
                    data["remedios"].remove(r)
                    acabados.append(r)
                    continue
                r["dose"] = {"devida_em": _iso(devida), "avisada_em": _iso(agora)}
                r.pop("hora_perguntada_em", None)
                abertas.append(r)
            elif not dose.get("perguntada_em") and \
                    agora - core._parse_dt(dose["avisada_em"]) >= timedelta(minutes=ESPERA_MIN):
                perguntar.append(r)
        if abertas or acabados:
            _save(data)
    for r in acabados:   # cada aviso vai ao pet (lib_atividade) e ao feed que o aplicativo lê (lib_db.avisos)
        log(f"[remedios] {r['nome']}: fim do tratamento, lembrete encerrado")
        lib_atividade.evento("remedio", f"Fim do tratamento: {r['nome']}")
        lib_db.registrar_aviso("remedio", f"Fim do tratamento: {r['nome']}")
        feitos.append(("fim", r["id"]))
    for r in abertas:
        lib_atividade.evento("remedio", f"Tomar {r['nome']}")
        lib_db.registrar_aviso("remedio", f"Tomar {r['nome']}", _detalhe(r), ref=_id_recado(r))
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
    return feitos + _acompanhar_pesquisas(agora)


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
    A hora dita é sempre a ocorrência mais recente que já passou ('23h' às 14:30 = ontem). Hora sem minutos de 1
    a 11 pode ser da tarde ('umas 2' às 14:30 = 14:00); com minutos vale ao pé da letra ('01:30' = 01:30).
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
    solta = not m.group(2) and not m.group(1).startswith("0")   # "umas 2" pode ser 2 da tarde; "02:30" e "2:30", não
    horas = [hh, hh + 12] if solta and 1 <= hh < 12 else [hh]
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
