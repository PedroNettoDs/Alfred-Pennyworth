#!/usr/bin/env python3
"""
Alfred Inbox — limpeza da caixa depois do podcast semanal.

O Python só DECIDE e PERGUNTA; quem mexe na caixa real é a extensão do Thunderbird
(services/inbox/thunderbird-ext/), que move as mensagens aprovadas para a Lixeira da própria
conta pelo cliente já autenticado (OAuth2). Decisão e alternativas em LIMPEZA.md.

Fluxo:
    candidatos()      → dry-run: e-mails que poderiam sair da caixa, cada um com motivo
    propor()          → grava um LOTE "aguardando" em VAULT_ALFRED/pendencias/limpeza.yml + .md
                        ("posso apagar estes N e-mails?") e avisa o usuário
    aprovar(id)       → só por confirmação explícita (CLI / API / tool); revalida cada item
    limpar(id)        → botão "Limpar" do Painel: a extensão executa ESTE lote de verdade (mover para a
                        Lixeira) mesmo com "Executar de verdade" desligado; lote aguardando é aprovado antes
    fila_execucao()   → a extensão busca os lotes aprovados (GET /limpeza/fila)
    registrar_resultado(id, itens) ← a extensão relata movido | ja_na_lixeira | simulado | nao_encontrado |
                        recusado | erro; lote com tudo fora da caixa = concluido ("Excluído" no Painel), que sai
                        do limpeza.yml e do cofre APAGAR_EXCLUIDOS_DIAS dias depois
    ping_extensao()   ← a extensão avisa a cada minuto que está viva (POST /limpeza/extensao/ping) e recebe o
                        "Verificar agora" do Painel (pedir_verificacao); estado_extensao() alimenta o Painel
    categorizar()     → botão "Categorizar e-mails": roda o classificador do ciclo (core.classificar) em quem
                        nunca ganhou categoria e propõe um lote com os candidatos

Regras (não configuráveis):
  - excluir = MOVER PARA A LIXEIRA; nada aqui nem na extensão faz expunge / remoção permanente
  - só entram (a) e-mails listados num manifesto do podcast semanal ou
    (b) categorias de LIMPAVEIS (promocao/transacional/ignorado) mais antigos que `dias_minimos`
  - nunca: contato, evento, pessoal, nem e-mail ligado a pendência aguardando — mesmo se estiver no manifesto
  - padrão é dry-run; sem aprovação explícita nada é executado

A categoria de cada e-mail vem de logs/inbox_categorias.json, alimentado pelo ciclo do inbox
(registrar_classificacoes). E-mail sem categoria registrada só entra se cair em `remetentes_ignorar`.
"""

import json
import os
import re
import sys
import threading
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared.lib_alfred import log  # noqa: E402
from _shared.lib_mail import (fetch_messages, find_thunderbird_profile, ids_no_indice, list_accounts,  # noqa: E402
                              resolve_folder)

import core  # noqa: E402

REGISTRO_FILE = core.PROJECT_ROOT / "logs" / "inbox_categorias.json"
REGISTRO_DIAS = 400                      # quanto tempo lembramos a categoria de um message-id

EXTENSAO_FILE = core.PROJECT_ROOT / "logs" / "limpeza_extensao.json"   # último contato/ciclo da extensão
SEM_CONTATO_MIN = 15                     # sem ping há mais que isso → Painel mostra "sem contato"
APAGAR_EXCLUIDOS_DIAS = 5                # lote Excluído (concluido) some do limpeza.yml e do cofre

LIMPAVEIS  = ("promocao", "transacional", "ignorado")
PROTEGIDAS = ("contato", "evento", "pessoal")
EXCLUIDOS = ("movido", "ja_na_lixeira")  # item fora da caixa: movido pela extensão ou já estava na Lixeira
STATUS_ITEM_OK = ("movido", "simulado", "ja_na_lixeira")
STATUS_LOTE_FINAIS = ("concluido", "simulado", "parcial", "falhou", "recusado", "expirado")
STATUS_LIMPAR = ("aguardando", "aprovado", "simulado", "parcial", "falhou")   # onde o "Limpar" do Painel vale

DEFAULTS = {
    "ativo": True,
    "dias_minimos": 30,                  # (b): idade mínima de promocao/transacional/ignorado
    "categorias": list(LIMPAVEIS),       # subconjunto de LIMPAVEIS
    "usar_manifestos": True,             # (a): e-mails que já viraram podcast
    "pastas": None,                      # default = pastas da inbox
    "max_por_lote": 200,
    "expirar_lote_dias": 7,              # lote aguardando sem resposta → expirado
}


# ══════════════════════════════════════════════════════════════
#  Config / arquivos
# ══════════════════════════════════════════════════════════════

def config(inbox_name: Optional[str] = None) -> tuple[str, dict, dict]:
    """(nome da inbox, cfg da inbox, cfg da limpeza com defaults)."""
    nome, cfg = core.carregar_cfg(inbox_name)
    lc = {**DEFAULTS, **(cfg.get("limpeza") or {})}
    cats = [str(c).lower() for c in (lc.get("categorias") or [])]
    invalidas = [c for c in cats if c not in LIMPAVEIS]
    if invalidas:
        log(f"[limpeza] categorias não permitidas ignoradas: {invalidas} (só {', '.join(LIMPAVEIS)})")
    lc["categorias"] = [c for c in cats if c in LIMPAVEIS]
    dias = lc.get("dias_minimos")
    lc["dias_minimos"] = max(0, int(DEFAULTS["dias_minimos"] if dias is None else dias))   # 0 = qualquer idade
    lc["pastas"] = lc.get("pastas") or cfg.get("pastas") or ["INBOX"]
    return nome, cfg, lc


def lotes_file() -> Path:
    return core.pend_dir() / "limpeza.yml"


def manifestos_dir() -> Optional[Path]:
    """Mesmo lugar onde o podcast grava (semanal): VAULT_NOTICIAS vazio → VAULT_ALFRED."""
    try:
        return core.vault_noticias() / "podcast" / "manifestos"
    except RuntimeError:   # nem VAULT_NOTICIAS nem VAULT_ALFRED
        return None


def _now() -> datetime:
    return datetime.now().astimezone()


# ══════════════════════════════════════════════════════════════
#  Registro de categorias (alimentado pelo ciclo)
# ══════════════════════════════════════════════════════════════

def carregar_registro() -> dict:
    if not REGISTRO_FILE.exists():
        return {}
    try:
        data = json.loads(REGISTRO_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f"[limpeza] registro ilegível ({e}) — começando vazio")
        return {}


def registrar_classificacoes(classificacao: list[dict]) -> int:
    """Guarda message_id → categoria (+ remetente/assunto/data). Chamado pelo ciclo; nunca levanta."""
    try:
        with core.trava_arquivo(REGISTRO_FILE):   # ciclo (timer) e "Categorizar e-mails" (API) gravam juntos
            reg = carregar_registro()
            agora = _now().isoformat(timespec="minutes")
            for c in classificacao or []:
                mid = c.get("message_id")
                if mid:
                    reg[mid] = {"categoria": c.get("categoria"), "from": c.get("from"), "subject": c.get("subject"),
                                "date": c.get("date"), "registrado_em": agora}
            corte = _now() - timedelta(days=REGISTRO_DIAS)
            reg = {k: v for k, v in reg.items()
                   if (core._parse_dt(v.get("registrado_em")) or _now()) > corte}
            core.gravar_atomico(REGISTRO_FILE, json.dumps(reg, indent=1, ensure_ascii=False))
        return len(classificacao or [])
    except Exception as e:
        log(f"[limpeza] não consegui registrar categorias: {e}")
        return 0


# ══════════════════════════════════════════════════════════════
#  Manifestos do podcast
# ══════════════════════════════════════════════════════════════

def carregar_manifestos() -> dict:
    """message_id → semana do episódio que já usou o e-mail."""
    d = manifestos_dir()
    if not d or not d.is_dir():
        return {}
    out: dict = {}
    for f in sorted(d.glob("*.json")):
        try:
            man = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"[limpeza] manifesto ilegível {f.name}: {e}")
            continue
        if not man.get("episodio_md") and not man.get("episodio_mp3"):
            continue   # episódio não gerado de fato → não autoriza limpeza
        for e in man.get("emails") or []:
            mid = (e or {}).get("message_id")
            if mid:
                out[mid] = man.get("semana") or f.stem
    return out


# ══════════════════════════════════════════════════════════════
#  Candidatos (dry-run)
# ══════════════════════════════════════════════════════════════

def _ler_caixa(cfg: dict, lc: dict) -> list[dict]:
    """
    Lê as pastas da inbox no mbox offline do Thunderbird (só leitura). Cada mensagem vem com "no_indice":
    False = fantasma do mbox (saiu da pasta no servidor, mas os bytes continuam até a compactação).
    """
    if (cfg.get("backend") or "thunderbird").lower() != "thunderbird":
        log("[limpeza] só o backend thunderbird é suportado (a execução é pela extensão)")
        return []
    profile = find_thunderbird_profile()
    if not profile:
        log("[limpeza] perfil do Thunderbird não encontrado")
        return []
    return fetch_messages(list_accounts(profile), pastas=lc["pastas"], conta=cfg.get("conta", "") or "",
                          max_total=100_000, checar_indice=True)


def _ids_protegidos_por_pendencia() -> set[str]:
    ids = set()
    for p in core.listar_pendencias("aguardando"):
        mid = (p.get("email") or {}).get("message_id")
        if mid:
            ids.add(mid)
    return ids


def _ids_em_lotes_abertos() -> set[str]:
    ids = set()
    for l in _load_lotes()["lotes"]:
        if l.get("status") in ("aguardando", "aprovado", "em_execucao"):
            ids.update(i["message_id"] for i in l.get("itens") or [])
    return ids


def avaliar(m: dict, cfg: dict, lc: dict, registro: dict, manifestos: dict,
            protegidos: set[str], agora: Optional[datetime] = None) -> tuple[Optional[str], str]:
    """
    Decide um e-mail. Retorna (motivo, "") se é candidato ou (None, razão de ficar).
    Proteções vêm antes de qualquer regra que autoriza.
    """
    agora = agora or _now()
    mid = m.get("message_id") or ""
    if not mid or mid.startswith("<sem-id"):
        return None, "sem Message-ID"
    reg = registro.get(mid) or {}
    cat = (reg.get("categoria") or "").lower()
    if not cat and core._match_lista(m, cfg.get("remetentes_ignorar")):
        cat = "ignorado"
    if cat in PROTEGIDAS:
        return None, f"categoria protegida ({cat})"
    if mid in protegidos:
        return None, "ligado a pendência aguardando"

    if lc.get("usar_manifestos") and mid in manifestos:
        return f"já virou podcast ({manifestos[mid]})", ""

    if not cat:
        return None, "sem categoria registrada"
    if cat not in lc["categorias"]:
        return None, f"categoria {cat} não é limpável"
    if m.get("date") is None:
        return None, "sem data"
    idade = (agora - m["date"]).days
    if idade < lc["dias_minimos"]:
        return None, f"{cat} com só {idade} dia(s) (< {lc['dias_minimos']})"
    return f"{cat} há {idade} dias", ""


def _item(m: dict, motivo: str) -> dict:
    return {"message_id": m["message_id"], "from": m.get("from_addr", ""), "from_name": m.get("from_name", ""),
            "subject": m.get("subject", ""), "date": m.get("date_iso", ""), "pasta": m.get("folder", ""),
            "conta": m.get("account", ""), "motivo": motivo}


def candidatos(inbox_name: Optional[str] = None, incluir_mantidos: bool = False) -> dict:
    """Dry-run: nada é gravado. {"inbox", "candidatos":[item], "mantidos":{razão: n}, ...}."""
    nome, cfg, lc = config(inbox_name)
    msgs = _ler_caixa(cfg, lc)
    registro, manifestos = carregar_registro(), carregar_manifestos()
    protegidos = _ids_protegidos_por_pendencia()
    em_lote = _ids_em_lotes_abertos()
    agora = _now()
    cands, mantidos, vistos = [], {}, set()
    for m in msgs:
        if m["message_id"] in vistos:
            continue
        vistos.add(m["message_id"])
        if m.get("no_indice") is False:   # a extensão não acharia: relataria nao_encontrado
            motivo, razao = None, "fora do índice do Thunderbird"
        elif m["message_id"] in em_lote:
            motivo, razao = None, "já está num lote aberto"
        else:
            motivo, razao = avaliar(m, cfg, lc, registro, manifestos, protegidos, agora)
        if motivo:
            cands.append(_item(m, motivo))
        else:
            chave = re.sub(r"\d+", "N", razao)
            mantidos[chave] = mantidos.get(chave, 0) + 1
    cands.sort(key=lambda i: i["date"])
    out = {"inbox": nome, "dry_run": True, "pastas": lc["pastas"], "dias_minimos": lc["dias_minimos"],
           "categorias": lc["categorias"], "emails_lidos": len(vistos), "total": len(cands),
           "candidatos": cands[: lc["max_por_lote"]], "excedentes": max(0, len(cands) - lc["max_por_lote"]),
           "manifestos": len(set(manifestos.values())), "registro": len(registro)}
    if incluir_mantidos:
        out["mantidos"] = mantidos
    return out


# ══════════════════════════════════════════════════════════════
#  Lotes ("posso apagar estes N e-mails?")
# ══════════════════════════════════════════════════════════════

def _load_lotes() -> dict:
    f = lotes_file()
    if not f.exists():
        return {"lotes": []}
    try:
        data = core.ler_yaml(f.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict) and isinstance(data.get("lotes"), list):
            return data
    except Exception as e:
        log(f"[limpeza] limpeza.yml ilegível ({e})")
        raise RuntimeError(f"limpeza.yml ilegível — corrija antes de continuar: {e}")
    return {"lotes": []}


def _trava():
    """Trava entre processos de limpeza.yml (API/extensão, CLI, timer semanal). Sozinha na ordem de travas."""
    return core.trava_arquivo(lotes_file())


def _save_lotes(data: dict):
    with _trava():   # reentrante: quem lê-muda-grava já está dentro dela; gravação atômica (tmp + replace)
        core.gravar_atomico(lotes_file(), yaml.dump(data, allow_unicode=True, sort_keys=False))


def _lotes_leitura() -> list[dict]:
    """Lotes sem reler o arquivo se nada mudou (painel: a cada tela). Só para consultar: ver core.yml_cacheado."""
    return core.yml_cacheado(lotes_file(), _load_lotes)["lotes"]


def listar_lotes(status: Optional[str] = None) -> list[dict]:
    _expirar()
    _apagar_excluidos()
    lotes = _lotes_leitura()
    if status in (None, "", "todos"):
        return lotes
    return [l for l in lotes if l.get("status") == status]


def obter_lote(lid: str) -> Optional[dict]:
    return next((l for l in _load_lotes()["lotes"] if l.get("id") == lid), None)


def _pegar(data: dict, lid: str) -> dict:
    lote = next((l for l in data["lotes"] if l.get("id") == lid), None)
    if not lote:
        raise KeyError(f"Lote de limpeza não encontrado: {lid}")
    return lote


def _vencido(l: dict, agora: datetime) -> bool:
    return l.get("status") == "aguardando" and (core._parse_dt(l.get("expira_em")) or agora) < agora


def _expirar():
    # listar_lotes chama a cada consulta (painel: polling) — só pega a trava se houver o que expirar
    agora = _now()
    if not any(_vencido(l, agora) for l in _lotes_leitura()):
        return
    with _trava():
        data = _load_lotes()
        mudou = False
        for l in data["lotes"]:
            if _vencido(l, agora):
                l["status"] = "expirado"
                mudou = True
        if mudou:
            _save_lotes(data)


def apagar_em(l: dict) -> Optional[datetime]:
    """Quando um lote Excluído (concluido) sai do limpeza.yml e do cofre. None = não sai."""
    feito = core._parse_dt(l.get("executado_em"))
    if l.get("status") != "concluido" or not feito:
        return None
    return feito + timedelta(days=APAGAR_EXCLUIDOS_DIAS)


def _apagar_excluidos():
    # como _expirar: listar_lotes chama a cada consulta — só pega a trava se houver o que apagar.
    # Os e-mails continuam na Lixeira do Thunderbird; some só o registro do lote (yml + .md no cofre).
    agora = _now()

    def vencido(l: dict) -> bool:
        return (apagar_em(l) or agora) < agora

    if not any(vencido(l) for l in _lotes_leitura()):
        return
    with _trava():
        data = _load_lotes()
        velhos = [l for l in data["lotes"] if vencido(l)]
        if not velhos:
            return
        data["lotes"] = [l for l in data["lotes"] if not vencido(l)]
        _save_lotes(data)
    for l in velhos:
        md = core.vault() / ((l.get("arquivos") or {}).get("md") or "")
        try:
            if md.suffix == ".md" and md.resolve().parent == core.pend_dir().resolve():
                md.unlink(missing_ok=True)
        except Exception as e:
            log(f"[limpeza] não consegui apagar {md}: {e}")
        log(f"[limpeza] {l['id']} apagado ({APAGAR_EXCLUIDOS_DIAS} dias depois de excluído)")


def _texto_lote(lote: dict) -> str:
    linhas = [
        "---", f"tipo: limpeza", f"id: {lote['id']}", f"status: {lote['status']}",
        f"criado_em: {lote['criado_em']}", f"total: {lote['total']}", "---", "",
        f"# Posso apagar estes {lote['total']} e-mails?", "",
        "Eles vão para a **Lixeira** da conta (nada é apagado de forma permanente). "
        f"Aprovar: `run.sh limpeza aprovar {lote['id']}` · recusar: `run.sh limpeza recusar {lote['id']}`.", "",
        "| Data | Remetente | Assunto | Motivo |", "|---|---|---|---|",
    ]
    for i in lote["itens"]:
        assunto = (i["subject"] or "(sem assunto)").replace("|", "/")[:80]
        linhas.append(f"| {i['date'][:10]} | {i['from']} | {assunto} | {i['motivo']} |")
    return "\n".join(linhas) + "\n"


def _escrever_md(lote: dict):
    try:
        (core.vault() / lote["arquivos"]["md"]).write_text(_texto_lote(lote), encoding="utf-8")
    except Exception as e:
        log(f"[limpeza] não consegui escrever o .md do lote: {e}")


def resumo_lote(lote: dict, max_itens: int = 10) -> str:
    por_motivo: dict = {}
    for i in lote.get("itens") or []:
        chave = re.sub(r" há \d+ dias", "", i["motivo"])
        por_motivo[chave] = por_motivo.get(chave, 0) + 1
    partes = ", ".join(f"{n} {k}" for k, n in sorted(por_motivo.items(), key=lambda x: -x[1]))
    txt = f"Posso apagar estes {lote['total']} e-mails (vão para a Lixeira)? {partes}."
    for i in (lote.get("itens") or [])[:max_itens]:
        txt += f"\n• {i['date'][:10]} {i['from']} — {(i['subject'] or '')[:60]}"
    if max_itens > 0 and lote["total"] > max_itens:   # max_itens=0: quem chama lista os itens à parte
        txt += f"\n… e mais {lote['total'] - max_itens}."
    return txt


def propor(inbox_name: Optional[str] = None, notificar: bool = True) -> Optional[dict]:
    """Cria um lote aguardando aprovação com os candidatos atuais. None se não há candidatos."""
    nome, _cfg, lc = config(inbox_name)
    if not lc.get("ativo", True):
        log(f"[limpeza] desativada em perfis.yml (inbox {nome})")
        return None
    c = candidatos(nome)
    if not c["candidatos"]:
        log("[limpeza] nenhum candidato — nenhum lote criado")
        return None
    agora = _now()
    with _trava():   # candidatos() (leitura da caixa, lenta) fica fora; ler-anexar-gravar dentro
        data = _load_lotes()
        existentes = {l["id"] for l in data["lotes"]}
        lid, n = f"limpeza-{agora:%Y%m%d-%H%M}", 1
        while lid in existentes:
            n += 1
            lid = f"limpeza-{agora:%Y%m%d-%H%M}-{n}"
        lote = {
            "id": lid, "tipo": "limpeza", "inbox": nome, "status": "aguardando",
            "criado_em": agora.isoformat(timespec="minutes"),
            "expira_em": (agora + timedelta(days=int(lc["expirar_lote_dias"]))).isoformat(timespec="minutes"),
            "total": len(c["candidatos"]), "itens": c["candidatos"],
            "arquivos": {"md": f"pendencias/{lid}.md"},
        }
        data["lotes"].append(lote)
        _save_lotes(data)
    _escrever_md(lote)
    log(f"[limpeza] lote {lid} criado com {lote['total']} e-mail(s) — aguardando aprovação")
    if notificar:
        core._notify("Alfred — limpeza da caixa", resumo_lote(lote, 5) + f"\nrun.sh limpeza aprovar {lid}")
    return lote


def _indices(itens: list[dict]) -> dict:
    """
    (conta, pasta) dos itens → Message-IDs no .msf da pasta. Contas com o mesmo hostname somam os índices.
    None = sem índice legível (perfil, conta, pasta ou .msf faltando): a revalidação não filtra por ele.
    """
    profile = find_thunderbird_profile()
    contas = list_accounts(profile) if profile else []
    out: dict = {}
    for i in itens:
        chave = (i.get("conta", ""), i.get("pasta", ""))
        if chave in out:
            continue
        ids: Optional[set] = set()
        pastas = [resolve_folder(a, chave[1]) for a in contas if a.get("hostname") == chave[0]]
        for p in pastas or [None]:
            parcial = ids_no_indice(p) if p else None
            if parcial is None:
                ids = None
                break
            ids |= parcial
        out[chave] = ids
    return out


def _revalidar(itens: list[dict], inbox: Optional[str]) -> tuple[list[dict], list[dict]]:
    """
    (mantidos, removidos): reaplica avaliar() aos itens (pendência nova, categoria que mudou, manifesto
    removido) e tira quem saiu do índice .msf desde a proposta (apagado no celular/webmail).
    """
    _nome, cfg, lc = config(inbox)
    registro, manifestos = carregar_registro(), carregar_manifestos()
    protegidos = _ids_protegidos_por_pendencia()
    indices = _indices(itens)
    agora = _now()
    mantidos, removidos = [], []
    for i in itens:
        ids = indices[(i.get("conta", ""), i.get("pasta", ""))]
        if ids is not None and not i["message_id"].startswith("gen-") and i["message_id"] not in ids:
            removidos.append({**i, "removido_por": "fora do índice do Thunderbird"})
            continue
        m = {"message_id": i["message_id"], "from_addr": i["from"],
             "from_domain": i["from"].split("@")[-1] if "@" in i["from"] else "",
             "date": core._parse_dt(i["date"])}
        motivo, razao = avaliar(m, cfg, lc, registro, manifestos, protegidos, agora)
        if motivo:
            mantidos.append(i)
        else:
            removidos.append({**i, "removido_por": razao})
    return mantidos, removidos


def aprovar(lid: str, nota: str = "") -> dict:
    """
    Confirmação explícita. Revalida cada item (pendência nova, categoria que mudou, manifesto removido)
    e só então libera o lote para a extensão.
    """
    with _trava():
        data = _load_lotes()
        lote = _pegar(data, lid)
        if lote.get("status") != "aguardando":
            raise ValueError(f"{lid} está '{lote.get('status')}' — só lotes aguardando podem ser aprovados")
        if (core._parse_dt(lote.get("expira_em")) or _now()) < _now():
            lote["status"] = "expirado"
            _save_lotes(data)
            raise ValueError(f"{lid} expirou — gere um novo com `run.sh limpeza propor`")

        mantidos, removidos = _revalidar(lote["itens"], lote.get("inbox"))
        if not mantidos:
            raise ValueError(f"{lid}: nenhum item continua elegível na revalidação")
        agora = _now()
        lote.update({"status": "aprovado", "aprovado_em": agora.isoformat(timespec="minutes"),
                     "nota": nota.strip() or None, "itens": mantidos, "total": len(mantidos)})
        if removidos:
            lote["removidos_na_aprovacao"] = removidos
        _save_lotes(data)
        _escrever_md(lote)
        log(f"[limpeza] {lid} APROVADO ({len(mantidos)} itens; {len(removidos)} removidos na revalidação)")
        return lote


def recusar(lid: str, nota: str = "") -> dict:
    with _trava():
        data = _load_lotes()
        lote = _pegar(data, lid)
        if lote.get("status") not in ("aguardando", "aprovado"):
            raise ValueError(f"{lid} está '{lote.get('status')}' — não dá mais para recusar")
        lote.update({"status": "recusado", "respondido_em": _now().isoformat(timespec="minutes"),
                     "nota": nota.strip() or None})
        _save_lotes(data)
        _escrever_md(lote)
        log(f"[limpeza] {lid} recusado")
        return lote


def limpar(lid: str, nota: str = "") -> dict:
    """
    Botão "Limpar" do Painel (confirmação explícita): manda a extensão executar ESTE lote de verdade,
    movendo para a Lixeira, mesmo com "Executar de verdade" desligado nas opções dela (`limpar: true`
    → `executar` na fila). Lote aguardando é aprovado antes; aprovado/simulado/parcial/falhou volta à
    fila só com os itens ainda não movidos, revalidados de novo.
    """
    with _trava():
        anterior = obter_lote(lid) or {}
        if anterior.get("status") == "aguardando":
            aprovar(lid, nota)   # mesma revalidação e as mesmas regras da aprovação
        data = _load_lotes()
        lote = _pegar(data, lid)
        if lote.get("status") not in STATUS_LIMPAR:
            raise ValueError(f"{lid} está '{lote.get('status')}' — não dá para limpar")
        mantidos, removidos = _revalidar([i for i in lote["itens"] if i.get("resultado") not in EXCLUIDOS],
                                         lote.get("inbox"))
        if not mantidos:
            raise ValueError(f"{lid}: nenhum item a mover continua elegível na revalidação")
        if removidos:
            fora = {r["message_id"] for r in removidos}
            lote["itens"] = [i for i in lote["itens"] if i["message_id"] not in fora]
            lote["removidos_na_aprovacao"] = (lote.get("removidos_na_aprovacao") or []) + removidos
        lote.update({"status": "aprovado", "limpar": True, "limpar_pedido_em": _now().isoformat(timespec="minutes"),
                     "total": len(lote["itens"])})
        _save_lotes(data)
        _escrever_md(lote)
        fora_agora = len(lote.get("removidos_na_aprovacao") or []) - len(anterior.get("removidos_na_aprovacao") or [])
        log(f"[limpeza] {lid} LIMPAR pedido ({len(mantidos)} a mover; {fora_agora} removidos na revalidação)")
        return {**lote, "a_mover": len(mantidos), "removidos_agora": fora_agora}


# ══════════════════════════════════════════════════════════════
#  Execução (lado da extensão do Thunderbird)
# ══════════════════════════════════════════════════════════════

def fila_execucao() -> list[dict]:
    """
    Lotes aprovados para a extensão. Marca em_execucao (um lote em execução volta à fila após 1h).
    Buscar a fila é a passada da extensão: conta como verificação e atende um "Verificar agora" pendente.
    """
    _gravar_extensao(ultimo_contato_em=_agora_iso(), ultima_verificacao_em=_agora_iso(), verificar_pedido_em=None)
    with _trava():
        data = _load_lotes()
        agora, out = _now(), []
        for l in data["lotes"]:
            travado = (l.get("status") == "em_execucao"
                       and (core._parse_dt(l.get("execucao_iniciada_em")) or agora) < agora - timedelta(hours=1))
            if l.get("status") == "aprovado" or travado:
                l["status"] = "em_execucao"
                l["execucao_iniciada_em"] = agora.isoformat(timespec="minutes")
                # executar: Pedro mandou limpar pelo Painel → a extensão move de verdade (limpar())
                out.append({"id": l["id"], "acao": "mover_para_lixeira", "executar": bool(l.get("limpar")),
                            "itens": [{k: i[k] for k in ("message_id", "from", "subject", "date", "pasta", "conta")}
                                      for i in l["itens"] if i.get("resultado") not in EXCLUIDOS]})
        if out:
            _save_lotes(data)
        return out


def registrar_resultado(lid: str, itens: list[dict], executor: str = "thunderbird-ext") -> dict:
    with _trava():
        data = _load_lotes()
        lote = _pegar(data, lid)
        if lote.get("status") not in ("em_execucao", "aprovado"):
            raise ValueError(f"{lid} está '{lote.get('status')}' — resultado não esperado")
        validos = {i["message_id"] for i in lote["itens"]}
        res = {}
        for r in itens or []:
            mid = (r or {}).get("message_id")
            if mid in validos:
                res[mid] = {"status": str(r.get("status") or "erro"), "detalhe": str(r.get("detalhe") or "")[:300]}
        contagem: dict = {}
        for i in lote["itens"]:
            r = res.get(i["message_id"])
            if r is None and i.get("resultado") in EXCLUIDOS:   # fora da caixa numa passada anterior (fora desta fila)
                r = {"status": i["resultado"], "detalhe": i.get("resultado_detalhe", "")}
            r = r or {"status": "sem_resposta", "detalhe": ""}
            i["resultado"] = r["status"]
            if r["detalhe"]:
                i["resultado_detalhe"] = r["detalhe"]
            else:
                i.pop("resultado_detalhe", None)   # detalhe de uma passada anterior não vale mais
            contagem[r["status"]] = contagem.get(r["status"], 0) + 1
        ok = sum(contagem.get(s, 0) for s in STATUS_ITEM_OK)
        if sum(contagem.get(s, 0) for s in EXCLUIDOS) == len(lote["itens"]):
            status = "concluido"
        elif contagem.get("simulado") == len(lote["itens"]):
            status = "simulado"
        else:   # inclui movidos de uma passada anterior + simulados agora (Limpar com extensão antiga)
            status = "parcial" if ok else "falhou"
        lote.update({"status": status, "executado_em": _now().isoformat(timespec="minutes"),
                     "executor": executor, "contagem": contagem})
        _save_lotes(data)
        _escrever_md(lote)
        log(f"[limpeza] {lid} → {status} {contagem}")
        return lote


# ══════════════════════════════════════════════════════════════
#  Estado da extensão (Painel: "última verificação / com erro" e "Verificar agora")
# ══════════════════════════════════════════════════════════════

def _agora_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _carregar_extensao() -> dict:
    try:
        data = json.loads(EXTENSAO_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _gravar_extensao(**campos) -> dict:
    """Mescla `campos` no estado da extensão (None apaga a chave). Nunca levanta: o estado é só informativo."""
    try:
        with core.trava_arquivo(EXTENSAO_FILE):
            e = _carregar_extensao()
            for k, v in campos.items():
                if v is None:
                    e.pop(k, None)
                else:
                    e[k] = v
            core.gravar_atomico(EXTENSAO_FILE, json.dumps(e, indent=1, ensure_ascii=False))
            return e
    except Exception as ex:
        log(f"[limpeza] não consegui gravar o estado da extensão: {ex}")
        return {}


def ping_extensao(versao: str = "", ultimo_ciclo: Optional[dict] = None) -> dict:
    """
    A extensão chama a cada minuto. Grava o contato e o último ciclo dela (com o erro, se houve) e devolve
    {"verificar": True} quando Pedro pediu "Verificar agora" no Painel: ela faz a passada na hora.
    """
    ciclo = None
    if isinstance(ultimo_ciclo, dict) and ultimo_ciclo.get("em"):
        em = core._parse_dt(str(ultimo_ciclo["em"]))
        ciclo = {"em": (em.astimezone() if em else _now()).isoformat(timespec="seconds"),
                 "erro": str(ultimo_ciclo.get("erro") or "")[:300]}
    with core.trava_arquivo(EXTENSAO_FILE):   # ler o pedido e apagá-lo juntos: só um ping o recebe
        pedido = _carregar_extensao().get("verificar_pedido_em")
        _gravar_extensao(ultimo_contato_em=_agora_iso(), versao=str(versao or "")[:20] or None,
                         ultimo_ciclo=ciclo, verificar_pedido_em=None)
    return {"verificar": bool(pedido)}


def pedir_verificacao() -> dict:
    """Botão "Verificar agora" do Painel: o próximo ping da extensão (até 1 min) devolve verificar=True."""
    _gravar_extensao(verificar_pedido_em=_agora_iso())
    return estado_extensao()


def estado_extensao(agora: Optional[datetime] = None) -> dict:
    """
    Para o Painel. estado: nunca (nenhum contato registrado) | sem_contato (nada há mais de SEM_CONTATO_MIN)
    | erro (o último ciclo da extensão falhou) | ok.
    """
    agora = agora or _now()
    e = _carregar_extensao()
    contato = core._parse_dt(e.get("ultimo_contato_em"))
    ciclo = e.get("ultimo_ciclo") or {}
    if not contato:
        estado = "nunca"
    elif contato < agora - timedelta(minutes=SEM_CONTATO_MIN):
        estado = "sem_contato"
    elif ciclo.get("erro"):
        estado = "erro"
    else:
        estado = "ok"
    return {"estado": estado, "ultimo_contato_em": e.get("ultimo_contato_em"),
            "ultima_verificacao_em": e.get("ultima_verificacao_em"), "versao": e.get("versao") or "",
            "erro": ciclo.get("erro") or "", "erro_em": ciclo.get("em") if ciclo.get("erro") else None,
            "verificar_pedido_em": e.get("verificar_pedido_em")}


# ══════════════════════════════════════════════════════════════
#  "Categorizar e-mails" (Painel): classifica quem o ciclo nunca viu
# ══════════════════════════════════════════════════════════════
# O ciclo só classifica o que chega na janela dele (janela_horas). E-mails mais antigos que o registro
# (logs/inbox_categorias.json) ficam "sem categoria registrada" e nunca entram na limpeza. Roda numa thread
# da API, uma por vez; o andamento fica em memória e o Painel o mostra pelo polling.

FALHAS_SEGUIDAS_MAX = 3                  # classificador fora (Ollama) → para em vez de esperar o timeout de cada e-mail

_CATEGORIZACAO: dict = {}
_CATEGORIZACAO_LOCK = threading.Lock()


def sem_categoria(inbox_name: Optional[str] = None) -> list[dict]:
    """E-mails da caixa (no índice do Thunderbird, com Message-ID) que não estão no registro de categorias."""
    _nome, cfg, lc = config(inbox_name)
    registro = carregar_registro()
    out, vistos = [], set()
    for m in _ler_caixa(cfg, lc):
        mid = m.get("message_id") or ""
        if (not mid or mid.startswith("<sem-id") or mid in vistos or mid in registro
                or m.get("no_indice") is False):
            continue
        vistos.add(mid)
        out.append(m)
    return out


def categorizar(inbox_name: Optional[str] = None, andamento=None) -> dict:
    """
    Classifica com core.classificar (o mesmo do ciclo: heurísticas, cache e LLM) os e-mails sem categoria e grava
    cada um no registro. Só registra: não cria pendência nem jornal. No fim propõe um lote com os candidatos
    (aguardando aprovação, sem notificar). `andamento(feitos, total)` é chamado a cada e-mail.
    """
    nome, cfg, _lc = config(inbox_name)
    msgs = sem_categoria(nome)
    contagem: dict = {}
    falhas = seguidas = 0
    interrompido = ""
    if andamento:
        andamento(0, len(msgs))
    for n, m in enumerate(msgs, 1):
        c = core.classificar(m, cfg)
        if c.get("falhou"):   # fica sem categoria e entra na próxima categorização
            falhas += 1
            seguidas += 1
            if seguidas >= FALHAS_SEGUIDAS_MAX:
                interrompido = f"o classificador não respondeu {seguidas} vezes seguidas (Ollama no ar?)"
                break
        else:
            seguidas = 0
            registrar_classificacoes([core._resumo_msg(m, c)])
            contagem[c["categoria"]] = contagem.get(c["categoria"], 0) + 1
        if andamento:
            andamento(n, len(msgs))
    lote = propor(nome, notificar=False)
    log(f"[limpeza] categorização: {len(msgs)} sem categoria → {contagem}, {falhas} falha(s)"
        f"{' — ' + interrompido if interrompido else ''}; lote {lote['id'] if lote else '—'}")
    return {"total": len(msgs), "contagem": contagem, "falhas": falhas, "interrompido": interrompido,
            "lote": lote["id"] if lote else None, "lote_total": lote["total"] if lote else 0}


def iniciar_categorizacao(inbox_name: Optional[str] = None) -> bool:
    """Começa categorizar() numa thread. False se já há uma em andamento."""
    with _CATEGORIZACAO_LOCK:
        if _CATEGORIZACAO.get("rodando"):
            return False
        _CATEGORIZACAO.clear()
        _CATEGORIZACAO.update(rodando=True, inicio=_agora_iso(), feitos=0, total=None)
    threading.Thread(target=_rodar_categorizacao, args=(inbox_name,), name="limpeza-categorizar",
                     daemon=True).start()
    return True


def _rodar_categorizacao(inbox_name: Optional[str]):
    def andamento(feitos: int, total: int):
        _CATEGORIZACAO.update(feitos=feitos, total=total)

    try:
        _CATEGORIZACAO.update(categorizar(inbox_name, andamento))
    except Exception as e:
        log(f"[limpeza] categorização falhou: {e}")
        _CATEGORIZACAO["erro"] = str(e)[:300]
    finally:
        _CATEGORIZACAO.update(rodando=False, fim=_agora_iso())


def andamento_categorizacao() -> dict:
    """{} = não rodou desde que a API subiu; senão rodando, feitos/total, contagem, falhas, lote, erro, inicio/fim."""
    return dict(_CATEGORIZACAO)


# ══════════════════════════════════════════════════════════════
#  CLI (run.sh limpeza ...)
# ══════════════════════════════════════════════════════════════

def adicionar_parser(sub):
    sp = sub.add_parser("limpeza", help="limpeza pós-podcast: candidatos | propor | lotes | ver ID | aprovar ID | recusar ID")
    sp.add_argument("acao", nargs="?", default="candidatos",
                    choices=("candidatos", "propor", "lotes", "ver", "aprovar", "recusar"))
    sp.add_argument("id", nargs="?", default=None)
    sp.add_argument("--inbox", default=None)
    sp.add_argument("--nota", default="")
    sp.add_argument("--todos", action="store_true", help="candidatos: lista todos (não só os 50 primeiros)")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--sem-notificar", action="store_true")


def _imprimir_itens(itens: list[dict], limite: Optional[int]):
    for i in itens[:limite] if limite else itens:
        print(f"  {i['date'][:10]}  {i['from'][:32]:<32}  {(i['subject'] or '')[:50]:<50}  {i['motivo']}")
    if limite and len(itens) > limite:
        print(f"  … e mais {len(itens) - limite} (use --todos)")


def cmd_limpeza(args) -> int:
    def _saida(obj) -> int:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
        return 0

    try:
        if args.acao == "candidatos":
            c = candidatos(args.inbox, incluir_mantidos=True)
            if args.json:
                return _saida(c)
            print(f"[DRY-RUN] {c['total']} candidato(s) entre {c['emails_lidos']} e-mails em {c['pastas']} "
                  f"(≥{c['dias_minimos']} dias: {', '.join(c['categorias'])}; manifestos: {c['manifestos']}; "
                  f"categorias registradas: {c['registro']})")
            _imprimir_itens(c["candidatos"], None if args.todos else 50)
            print("Ficam na caixa:")
            for razao, n in sorted(c["mantidos"].items(), key=lambda x: -x[1]):
                print(f"  {n:>5}  {razao}")
            print("Nada foi alterado. Para pedir aprovação: run.sh limpeza propor")
            return 0
        if args.acao == "propor":
            lote = propor(args.inbox, notificar=not args.sem_notificar)
            if not lote:
                print("Nenhum candidato — nenhum lote criado.")
                return 0
            if args.json:
                return _saida(lote)
            print(resumo_lote(lote))
            print(f"\nLOTE_LIMPEZA={lote['id']}\nAprovar: run.sh limpeza aprovar {lote['id']}  ·  "
                  f"recusar: run.sh limpeza recusar {lote['id']}")
            return 0
        if args.acao == "lotes":
            lotes = listar_lotes(None)
            if args.json:
                return _saida(lotes)
            for l in lotes:
                print(f"  {l['id']:<26} {l['status']:<12} {l['total']:>4} e-mails  criado {l['criado_em']}")
            if not lotes:
                print("Nenhum lote de limpeza.")
            return 0
        if not args.id:
            print(f"uso: run.sh limpeza {args.acao} ID", file=sys.stderr)
            return 2
        if args.acao == "ver":
            lote = obter_lote(args.id)
            if not lote:
                print(f"Lote não encontrado: {args.id}", file=sys.stderr)
                return 1
            if args.json:
                return _saida(lote)
            print(f"{lote['id']} — {lote['status']}\n{resumo_lote(lote, 0)}")
            _imprimir_itens(lote["itens"], None)
            return 0
        if args.acao == "aprovar":
            lote = aprovar(args.id, args.nota)
            print(f"✔ {lote['id']} aprovado: {lote['total']} e-mail(s) irão para a Lixeira na próxima "
                  f"passada da extensão do Thunderbird.")
            for r in lote.get("removidos_na_aprovacao") or []:
                print(f"  (retirado: {r['subject'][:50]} — {r['removido_por']})")
            return 0
        if args.acao == "recusar":
            recusar(args.id, args.nota)
            print(f"✔ {args.id} recusado — nada será apagado.")
            return 0
    except (KeyError, ValueError, RuntimeError) as e:
        print(f"[limpeza] {e.args[0] if isinstance(e, KeyError) and e.args else e}", file=sys.stderr)
        return 1
    return 2
