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
    fila_execucao()   → a extensão busca os lotes aprovados (GET /limpeza/fila)
    registrar_resultado(id, itens) ← a extensão relata movido | simulado | nao_encontrado | recusado | erro

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
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared.lib_alfred import log  # noqa: E402
from _shared.lib_mail import fetch_messages, find_thunderbird_profile, list_accounts  # noqa: E402

import core  # noqa: E402

REGISTRO_FILE = core.PROJECT_ROOT / "logs" / "inbox_categorias.json"
REGISTRO_DIAS = 400                      # quanto tempo lembramos a categoria de um message-id

LIMPAVEIS  = ("promocao", "transacional", "ignorado")
PROTEGIDAS = ("contato", "evento", "pessoal")
STATUS_ITEM_OK = ("movido", "simulado")
STATUS_LOTE_FINAIS = ("concluido", "simulado", "parcial", "falhou", "recusado", "expirado")

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
    lc["dias_minimos"] = max(1, int(lc.get("dias_minimos") or DEFAULTS["dias_minimos"]))
    lc["pastas"] = lc.get("pastas") or cfg.get("pastas") or ["INBOX"]
    return nome, cfg, lc


def lotes_file() -> Path:
    return core.pend_dir() / "limpeza.yml"


def manifestos_dir() -> Optional[Path]:
    v = os.getenv("VAULT_NOTICIAS", "").strip()
    return Path(v) / "podcast" / "manifestos" if v else None


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
        REGISTRO_FILE.parent.mkdir(parents=True, exist_ok=True)
        REGISTRO_FILE.write_text(json.dumps(reg, indent=1, ensure_ascii=False), encoding="utf-8")
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
    """Lê as pastas da inbox no mbox offline do Thunderbird (só leitura)."""
    if (cfg.get("backend") or "thunderbird").lower() != "thunderbird":
        log("[limpeza] só o backend thunderbird é suportado (a execução é pela extensão)")
        return []
    profile = find_thunderbird_profile()
    if not profile:
        log("[limpeza] perfil do Thunderbird não encontrado")
        return []
    return fetch_messages(list_accounts(profile), pastas=lc["pastas"], conta=cfg.get("conta", "") or "",
                          max_total=100_000)


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
        if m["message_id"] in em_lote:
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
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict) and isinstance(data.get("lotes"), list):
            return data
    except Exception as e:
        log(f"[limpeza] limpeza.yml ilegível ({e})")
        raise RuntimeError(f"limpeza.yml ilegível — corrija antes de continuar: {e}")
    return {"lotes": []}


def _save_lotes(data: dict):
    lotes_file().write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def listar_lotes(status: Optional[str] = None) -> list[dict]:
    _expirar()
    lotes = _load_lotes()["lotes"]
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


def _expirar():
    data = _load_lotes()
    agora, mudou = _now(), False
    for l in data["lotes"]:
        if l.get("status") == "aguardando" and (core._parse_dt(l.get("expira_em")) or agora) < agora:
            l["status"] = "expirado"
            mudou = True
    if mudou:
        _save_lotes(data)


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
    if lote["total"] > max_itens:
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


def aprovar(lid: str, nota: str = "") -> dict:
    """
    Confirmação explícita. Revalida cada item (pendência nova, categoria que mudou, manifesto removido)
    e só então libera o lote para a extensão.
    """
    data = _load_lotes()
    lote = _pegar(data, lid)
    if lote.get("status") != "aguardando":
        raise ValueError(f"{lid} está '{lote.get('status')}' — só lotes aguardando podem ser aprovados")
    if (core._parse_dt(lote.get("expira_em")) or _now()) < _now():
        lote["status"] = "expirado"
        _save_lotes(data)
        raise ValueError(f"{lid} expirou — gere um novo com `run.sh limpeza propor`")

    _nome, cfg, lc = config(lote.get("inbox"))
    registro, manifestos = carregar_registro(), carregar_manifestos()
    protegidos = _ids_protegidos_por_pendencia()
    agora = _now()
    mantidos, removidos = [], []
    for i in lote["itens"]:
        m = {"message_id": i["message_id"], "from_addr": i["from"],
             "from_domain": i["from"].split("@")[-1] if "@" in i["from"] else "",
             "date": core._parse_dt(i["date"])}
        motivo, razao = avaliar(m, cfg, lc, registro, manifestos, protegidos, agora)
        if motivo:
            mantidos.append(i)
        else:
            removidos.append({**i, "removido_por": razao})
    if not mantidos:
        raise ValueError(f"{lid}: nenhum item continua elegível na revalidação")
    lote.update({"status": "aprovado", "aprovado_em": agora.isoformat(timespec="minutes"),
                 "nota": nota.strip() or None, "itens": mantidos, "total": len(mantidos)})
    if removidos:
        lote["removidos_na_aprovacao"] = removidos
    _save_lotes(data)
    _escrever_md(lote)
    log(f"[limpeza] {lid} APROVADO ({len(mantidos)} itens; {len(removidos)} removidos na revalidação)")
    return lote


def recusar(lid: str, nota: str = "") -> dict:
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


# ══════════════════════════════════════════════════════════════
#  Execução (lado da extensão do Thunderbird)
# ══════════════════════════════════════════════════════════════

def fila_execucao() -> list[dict]:
    """Lotes aprovados para a extensão. Marca em_execucao (um lote em execução volta à fila após 1h)."""
    data = _load_lotes()
    agora, out = _now(), []
    for l in data["lotes"]:
        travado = (l.get("status") == "em_execucao"
                   and (core._parse_dt(l.get("execucao_iniciada_em")) or agora) < agora - timedelta(hours=1))
        if l.get("status") == "aprovado" or travado:
            l["status"] = "em_execucao"
            l["execucao_iniciada_em"] = agora.isoformat(timespec="minutes")
            out.append({"id": l["id"], "acao": "mover_para_lixeira",
                        "itens": [{k: i[k] for k in ("message_id", "from", "subject", "date", "pasta", "conta")}
                                  for i in l["itens"]]})
    if out:
        _save_lotes(data)
    return out


def registrar_resultado(lid: str, itens: list[dict], executor: str = "thunderbird-ext") -> dict:
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
        r = res.get(i["message_id"], {"status": "sem_resposta", "detalhe": ""})
        i["resultado"] = r["status"]
        if r["detalhe"]:
            i["resultado_detalhe"] = r["detalhe"]
        contagem[r["status"]] = contagem.get(r["status"], 0) + 1
    ok = sum(contagem.get(s, 0) for s in STATUS_ITEM_OK)
    if ok == len(lote["itens"]):
        status = "simulado" if contagem.get("simulado") == ok else "concluido"
    else:
        status = "parcial" if ok else "falhou"
    lote.update({"status": status, "executado_em": _now().isoformat(timespec="minutes"),
                 "executor": executor, "contagem": contagem})
    _save_lotes(data)
    _escrever_md(lote)
    log(f"[limpeza] {lid} → {status} {contagem}")
    return lote


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
        print(f"[limpeza] {e}", file=sys.stderr)
        return 1
    return 2
