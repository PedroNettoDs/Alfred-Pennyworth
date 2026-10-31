"""
Alfred Inbox Assistant — núcleo.

Pipeline (função ciclo()):
    Thunderbird (mbox offline) → fetch_messages()
        ↓
    classificar() — heurísticas + Ollama (JSON): noticia | evento | contato | transacional | promocao | pessoal | outro
        ↓                                   ↓
    noticia → extrair_noticias()         evento/contato → extrair_evento()
        ↓                                   ↓
    gerar_jornal()  (cluster → rank →    criar_pendencia()  (texto explicativo + áudio
     template → Ollama → título →         "vou ou não vou?" que fica aguardando
     vault/jornal/*.md + *.mp3)           resposta em vault/pendencias/)
        ↓
    logs/inbox_seen.json  (message-ids já processados)

Este módulo não tem CLI nem HTTP — é usado por inbox-assistant.py (CLI/systemd)
e por main.py (FastAPI, para o N8N). Nenhuma função levanta exceção por falha
de LLM: degrada com log() e segue.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import unicodedata

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared.lib_alfred import (  # noqa: E402
    log, slugify, ollama_generate, ollama_generate_json, deduplicate_by_url_and_title, gravar_atomico,
)
from _shared.lib_mail import (  # noqa: E402
    find_thunderbird_profile, list_accounts, fetch_messages, imap_fetch, limpar_url,
)
from _shared import lib_db  # noqa: E402
from _shared.lib_cluster import cluster_items, rank_clusters, build_clustered_news_text  # noqa: E402
from _shared.lib_titulo import generate_title_via_llm  # noqa: E402
from _shared.lib_tts import tts_save, limpar_para_fala, tocar_audio  # noqa: E402
from _shared.lib_templates import render_template  # noqa: E402
from _shared.perfis import get_inbox, list_all  # noqa: E402

SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
SEEN_FILE    = PROJECT_ROOT / "logs" / "inbox_seen.json"
SEEN_WINDOW_DAYS = 14

CATEGORIAS = ("noticia", "evento", "contato", "transacional", "promocao", "pessoal", "outro")

# JSON Schemas — Ollama "structured outputs": o modelo só consegue emitir estas chaves.
# Evita respostas que ecoam o prompt (ex.: {"links": [...]}) e JSON truncado.
SCHEMA_CLASSIFICACAO = {
    "type": "object",
    "properties": {
        "categoria": {"type": "string", "enum": list(CATEGORIAS)},
        "confianca": {"type": "number"},
        "resumo":    {"type": "string"},
        "motivo":    {"type": "string"},
    },
    "required": ["categoria", "confianca", "resumo", "motivo"],
}
SCHEMA_NOTICIAS = {
    "type": "object",
    "properties": {
        "itens": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "titulo": {"type": "string"},
                    "resumo": {"type": "string"},
                    "url":    {"type": "string"},
                },
                "required": ["titulo", "resumo", "url"],
            },
        }
    },
    "required": ["itens"],
}
SCHEMA_EVENTO = {
    "type": "object",
    "properties": {
        "tipo":           {"type": "string", "enum": ["evento", "contato"]},
        "titulo":         {"type": "string"},
        "organizador":    {"type": "string"},
        "quem":           {"type": "string"},
        "data_inicio":    {"type": ["string", "null"]},
        "data_fim":       {"type": ["string", "null"]},
        "local":          {"type": "string"},
        "online":         {"type": ["boolean", "null"]},
        "link":           {"type": "string"},
        "custo":          {"type": "string", "enum": ["gratuito", "pago", "desconhecido"]},
        "o_que_pedem":    {"type": "string"},
        "prazo_resposta": {"type": ["string", "null"]},
        "resumo":         {"type": "string"},
        "requer_decisao": {"type": "boolean"},
    },
    "required": ["tipo", "titulo", "organizador", "quem", "data_inicio", "data_fim", "local", "online",
                 "link", "custo", "o_que_pedem", "prazo_resposta", "resumo", "requer_decisao"],
}
OPTS_JSON = {"temperature": 0.1, "num_predict": 1200}
ACIONAVEIS = ("noticia", "evento", "contato")
# Só estas categorias podem pular o LLM via lib_db (cache por remetente de envio em massa): errar uma delas
# custa uma promoção a mais no jornal, nunca uma mensagem perdida.
CACHEAVEIS = ("promocao", "transacional")
CACHE_CONFIANCA_MINIMA = 0.9
STATUS_FINAIS = ("vou", "nao_vou", "expirada", "ignorada", "respondida", "respondida_manual")


# ══════════════════════════════════════════════════════════════
#  Config / caminhos
# ══════════════════════════════════════════════════════════════

def vault() -> Path:
    v = os.getenv("VAULT_ALFRED", "").strip()
    if not v:
        raise RuntimeError("VAULT_ALFRED não definido no .env")
    return Path(v)


def vault_noticias() -> Path:
    """Cofre de notícias (jornal diário + podcast semanal). Sem VAULT_NOTICIAS → o próprio VAULT_ALFRED."""
    v = os.getenv("VAULT_NOTICIAS", "").strip()
    return Path(v) if v else vault()


def jornal_dir() -> Path:
    d = vault_noticias() / "jornal"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ultimo_jornal_md() -> Optional[Path]:
    """Última edição: VAULT_NOTICIAS/jornal, com fallback para o antigo VAULT_ALFRED/jornal."""
    for d in (vault_noticias() / "jornal", vault() / "jornal"):
        mds = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []
        if mds:
            return mds[0]
    return None


def pend_dir() -> Path:
    d = vault() / "pendencias"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pend_file() -> Path:
    return pend_dir() / "pendencias.yml"


def inbox_default() -> str:
    nome = os.getenv("INBOX_DEFAULT", "").strip()
    if nome:
        return nome
    inboxes = list_all().get("inbox", [])
    if not inboxes:
        raise RuntimeError("Nenhuma inbox definida em perfis.yml (seção inbox:)")
    return inboxes[0]


def carregar_cfg(inbox_name: Optional[str] = None) -> tuple[str, dict]:
    nome = inbox_name or inbox_default()
    return nome, get_inbox(nome)


def _now() -> datetime:
    return datetime.now().astimezone()


# ══════════════════════════════════════════════════════════════
#  Trava entre processos + gravação atômica dos YAML do vault
# ══════════════════════════════════════════════════════════════
#
# pendencias.yml, tasks.yml, agenda.yml e limpeza.yml são gravados por vários PROCESSOS: a API
# (threads do webhook, thread da fila, painel), os aguardadores destacados das notificações, o CLI
# (run.sh), o ciclo do e-mail (timer), o vigia e as tools do Open WebUI (no container, pelo bind
# mount do vault). Cada gravação é "ler → mudar → gravar"; sem trava, quem grava por último apaga o
# que o outro gravou no meio. Regras:
#   - só GRAVAÇÕES pegam a trava, e por um trecho curto (nada de LLM/TTS/rede segurando);
#   - leituras não pegam trava — por isso toda gravação é atômica (tmp + os.replace): quem lê vê o
#     arquivo antigo inteiro ou o novo inteiro, nunca pela metade;
#   - ordem (evita deadlock): whatsapp._LOCK → pendências → tasks; agenda → tasks; limpeza sozinha;
#     fila do WhatsApp sozinha. Nunca pegar pendências segurando agenda/tasks.

_TRAVAS: dict = {}                      # caminho do .lock → {"rlock", "nivel", "fd"}
_TRAVAS_REGISTRO = threading.Lock()


def _arquivo_trava(dados: Path) -> Path:
    """pendencias/pendencias.yml → pendencias/.pendencias.yml.lock (mesmo nome dentro do container)."""
    return dados.parent / f".{dados.name}.lock"


def _abrir_trava(lock_path: Path) -> int:
    """fd do .lock (não herdável — padrão do Python). Criado 0666: o host (Pedro) e o container do Open WebUI
    (root) abrem o mesmo arquivo; sem permissão de escrita, abre só leitura (flock funciona igual)."""
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
    except PermissionError:
        return os.open(str(lock_path), os.O_RDONLY)
    try:
        if os.fstat(fd).st_uid == os.geteuid():
            os.fchmod(fd, 0o666)   # umask tirou a escrita do grupo/outros
    except OSError:
        pass
    return fd


@contextmanager
def trava_arquivo(dados: Path, timeout: float = 30.0):
    """
    Trava exclusiva de `dados` entre processos (fcntl.flock no arquivo .lock ao lado) e entre threads.
    Reentrante na mesma thread: só o primeiro nível pega/solta o flock — duas threads do mesmo processo
    nunca disputam o flock entre si (a segunda espera no RLock). Passou de `timeout` → TimeoutError.
    """
    dados = Path(dados)
    lock_path = _arquivo_trava(dados)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    chave = str(lock_path.resolve())
    with _TRAVAS_REGISTRO:
        t = _TRAVAS.setdefault(chave, {"rlock": threading.RLock(), "nivel": 0, "fd": None})
    limite = time.monotonic() + max(0.0, float(timeout))
    if not t["rlock"].acquire(timeout=max(0.0, float(timeout))):
        raise TimeoutError(f"{dados.name} ocupado por outra thread há mais de {timeout:g} s — tente de novo")
    try:
        if t["nivel"] == 0:
            fd = _abrir_trava(lock_path)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= limite:
                        os.close(fd)
                        raise TimeoutError(f"{dados.name} ocupado por outro processo há mais de {timeout:g} s "
                                           f"(trava {lock_path}) — tente de novo")
                    time.sleep(0.05)
            t["fd"] = fd
        t["nivel"] += 1
    except BaseException:
        t["rlock"].release()
        raise
    try:
        yield
    finally:
        t["nivel"] -= 1
        if t["nivel"] == 0:
            fd, t["fd"] = t["fd"], None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        t["rlock"].release()


def trava_pendencias():
    """Atalho: trava de VAULT_ALFRED/pendencias/pendencias.yml."""
    return trava_arquivo(pend_file())


# gravar_atomico(f, texto) vem de _shared.lib_alfred (temporário único + fsync + os.replace) e continua
# exposto como core.gravar_atomico. O temporário não precisa mais da trava, o "ler → mudar → gravar" sim.


# ══════════════════════════════════════════════════════════════
#  Memória de message-ids processados
# ══════════════════════════════════════════════════════════════

def load_seen(path: Optional[Path] = None) -> dict:
    """Ids já processados → timestamp. `path` permite outro arquivo (ex.: whatsapp_seen.json)."""
    path = path or SEEN_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[seen] Erro lendo {path}: {e} — começando vazio")
        return {}
    if not isinstance(data, dict):
        return {}
    cutoff = datetime.now() - timedelta(days=SEEN_WINDOW_DAYS)
    kept = {}
    for mid, ts in data.items():
        try:
            if datetime.fromisoformat(ts).replace(tzinfo=None) > cutoff:
                kept[mid] = ts
        except Exception:
            continue
    return kept


def save_seen(seen: dict, path: Optional[Path] = None):
    path = path or SEEN_FILE
    gravar_atomico(path, json.dumps(seen, indent=2, ensure_ascii=False))


# ══════════════════════════════════════════════════════════════
#  Coleta
# ══════════════════════════════════════════════════════════════

def coletar(cfg: dict, janela_horas: Optional[int] = None, max_emails: Optional[int] = None) -> list[dict]:
    """Lê os e-mails da inbox configurada (Thunderbird ou IMAP)."""
    janela = int(janela_horas or cfg.get("janela_horas", 36))
    limite = int(max_emails or cfg.get("max_emails", 80))
    desde  = _now() - timedelta(hours=janela)
    pastas = cfg.get("pastas") or ["INBOX"]
    backend = (cfg.get("backend") or "thunderbird").lower()

    if backend == "imap":
        host = os.getenv("IMAP_HOST", ""); user = os.getenv("IMAP_USER", ""); pw = os.getenv("IMAP_PASSWORD", "")
        if not (host and user and pw):
            log("[coleta] backend imap sem IMAP_HOST/IMAP_USER/IMAP_PASSWORD no .env")
            return []
        msgs: list[dict] = []
        for pasta in pastas:
            msgs.extend(imap_fetch(host, user, pw, folder=pasta, desde=desde, limit=limite))
        return msgs[:limite]

    profile = find_thunderbird_profile()
    if not profile:
        log("[coleta] Perfil do Thunderbird não encontrado (defina THUNDERBIRD_PROFILE no .env)")
        return []
    accounts = list_accounts(profile)
    if not accounts:
        log(f"[coleta] Nenhuma conta de e-mail no perfil {profile}")
        return []
    log(f"[coleta] perfil={profile.name} contas={len(accounts)} janela={janela}h pastas={pastas}")
    return fetch_messages(
        accounts, pastas=pastas, desde=desde, conta=cfg.get("conta", "") or "",
        max_total=limite, incluir_lidos=not cfg.get("so_nao_lidos", False),
    )


# ══════════════════════════════════════════════════════════════
#  Classificação
# ══════════════════════════════════════════════════════════════

def _match_lista(m: dict, lista) -> bool:
    for s in lista or []:
        s = str(s).lower().strip()
        if s and (s in m.get("from_addr", "") or s in m.get("from_domain", "")):
            return True
    return False


def _modelo_classificacao() -> Optional[str]:
    return os.getenv("MODEL_CLASSIFY", "").strip() or None


def classificar(m: dict, cfg: dict) -> dict:
    """
    Retorna {"categoria", "confianca", "resumo", "motivo", "origem"}.
    origem = "heuristica" (sem LLM) ou "llm".
    """
    if _match_lista(m, cfg.get("remetentes_ignorar")):
        return {"categoria": "ignorado", "confianca": 1.0, "resumo": "remetente na lista de ignorados",
                "motivo": "remetentes_ignorar", "origem": "heuristica"}
    if m.get("ics"):
        ev = m["ics"][0]
        metodo = str(ev.get("method") or "").upper()
        if str(ev.get("status") or "").upper() == "CANCELLED" or metodo not in ("", "REQUEST", "PUBLISH"):
            # cancelamento (METHOD:CANCEL / STATUS:CANCELLED) ou resposta a convite (REPLY...): nada a decidir
            tipo = "cancelamento de convite" if (metodo == "CANCEL" or ev.get("status") == "CANCELLED") \
                else f"resposta/atualização de convite ({metodo})"
            return {"categoria": "outro", "confianca": 0.95,
                    "resumo": f"{tipo}: {ev.get('summary') or m['subject']}",
                    "motivo": f"anexo .ics não acionável ({metodo or 'STATUS:CANCELLED'})", "origem": "heuristica"}
        return {"categoria": "evento", "confianca": 0.95,
                "resumo": f"convite de calendário: {ev.get('summary') or m['subject']}",
                "motivo": "anexo .ics", "origem": "heuristica"}
    if _match_lista(m, cfg.get("remetentes_noticia")):
        return {"categoria": "noticia", "confianca": 0.9, "resumo": m["subject"],
                "motivo": "remetentes_noticia", "origem": "heuristica"}

    # Remetente de envio em massa que o LLM já classificou igual várias vezes → sem LLM (lib_db.cache_get)
    chave_cache = None
    if m.get("is_bulk") and m.get("from_addr") and cfg.get("cache_classificacao", True):
        chave_cache = m["from_addr"].lower()
        hit = lib_db.cache_get("email", chave_cache)
        if hit:
            log(f"[cache] {chave_cache} → {hit['categoria']} (sem LLM)")
            return {**hit, "resumo": m["subject"], "origem": "cache"}   # o resumo guardado é de outro e-mail

    dicas = []
    if m.get("is_bulk"):
        dicas.append("envio em massa (List-Unsubscribe/List-Id)")
    if m.get("attachments"):
        dicas.append("anexos: " + ", ".join(m["attachments"][:5]))
    if not m.get("read", True):
        dicas.append("ainda não lido")
    prompt = render_template("email_classificar", {
        "remetente": f"{m['from_name']} <{m['from_addr']}>",
        "assunto":   m["subject"] or "(sem assunto)",
        "data":      m["date_iso"] or "desconhecida",
        "dicas":     "; ".join(dicas) or "nenhum",
        "corpo":     _corpo_prompt(m, 2500) or "(corpo vazio)",
    })
    data = ollama_generate_json(prompt, model=_modelo_classificacao(), timeout=120,
                                schema=SCHEMA_CLASSIFICACAO, options={**OPTS_JSON, "num_predict": 400})
    if not isinstance(data, dict):
        # falhou=True: o ciclo não marca o e-mail como visto — tenta de novo no próximo (Ollama fora/timeout)
        return {"categoria": "outro", "confianca": 0.0, "resumo": "", "motivo": "classificador não respondeu",
                "origem": "llm", "falhou": True}

    cat = str(data.get("categoria", "outro")).lower().strip()
    if cat not in CATEGORIAS:
        cat = "outro"
    try:
        conf = max(0.0, min(1.0, float(data.get("confianca", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    motivo = str(data.get("motivo", "")).strip()
    minimo = float(cfg.get("confianca_minima", 0.55))
    if cat in ACIONAVEIS and conf < minimo:
        motivo = f"baixa confiança ({conf:.2f} < {minimo}) para '{cat}'; " + motivo
        cat = "outro"
    res = {"categoria": cat, "confianca": round(conf, 2), "resumo": str(data.get("resumo", "")).strip()[:300],
           "motivo": motivo[:200], "origem": "llm"}
    if chave_cache:
        lib_db.cache_put("email", chave_cache, res, cacheavel=cat in CACHEAVEIS and conf >= CACHE_CONFIANCA_MINIMA)
    return res


# ══════════════════════════════════════════════════════════════
#  Jornal — extração de notícias das newsletters + edição do dia
# ══════════════════════════════════════════════════════════════

_LINK_RUIM = re.compile(r"unsubscribe|descadastr|cancelar|optout|opt-out|preferenc|privacy|privacidade|"
                        r"mailto:|facebook\.com|twitter\.com|x\.com/|instagram\.com|linkedin\.com/company|"
                        r"youtube\.com/@|apps\.apple\.com|play\.google\.com", re.I)


# links de redirecionamento escondem o destino na URL — então filtra também pelo texto-âncora
_ANCORA_RUIM = re.compile(r"^(instagram|facebook|youtube|linkedin|tiktok|twitter|x|whatsapp\b.*|custom|"
                          r"cancel\w* (sua )?inscri\w*|descadastr\w*|unsubscribe|"
                          r"(visualizar|ver) (este e-mail|no navegador).*|vers[aã]o on-?line|p[aá]gina web)$", re.I)


def _link_ruim(t: str, u: str) -> bool:
    return bool(_LINK_RUIM.search(u) or _ANCORA_RUIM.match((t or "").strip()))


_LINK_MAX_LEN = 300   # URLs de rastreamento gigantes só gastam contexto


def _links_texto(m: dict, max_links: int = 40) -> tuple[str, dict[str, str]]:
    """
    Lista LINKS do prompt + {o que o LLM pode devolver → URL real}.
    URLs de rastreamento gigantes (t.rdsv2.net/ls/click?upn=...) entram com um apelido curto
    ("link3") em vez de serem descartadas — senão o e-mail fica sem nenhum link de inscrição.
    """
    linhas, permitidas = [], {}
    for t, u in m.get("links", []):
        if _link_ruim(t, u) or u in permitidas:
            continue
        mostra = u
        if len(u) > _LINK_MAX_LEN:
            mostra = f"link{len(linhas) + 1}"
            permitidas[mostra] = u
        linhas.append(f"{t or '(sem texto)'} → {mostra}")
        permitidas[u] = u
        if len(linhas) >= max_links:
            break
    return ("\n".join(linhas) or "(nenhum)"), permitidas


def _corpo_prompt(m: dict, limite: int) -> str:
    """Corpo sem as URLs longas inline (texto plano de newsletter) — sobra contexto pro conteúdo."""
    from _shared.lib_mail import compactar_urls
    return compactar_urls(m.get("text") or "")[:limite]


def extrair_noticias(m: dict, jcfg: dict) -> list[dict]:
    """Newsletter → itens no formato do briefing: {title, url, snippet, source, published, email_id}."""
    max_itens = int(jcfg.get("max_por_email", 6))
    links_txt, permitidas = _links_texto(m)
    prompt = render_template("email_noticias_extrair", {
        "remetente": f"{m['from_name']} <{m['from_addr']}>",
        "assunto":   m["subject"],
        "data":      m["date_iso"],
        "max_itens": max_itens,
        "corpo":     _corpo_prompt(m, 6000),
        "links":     links_txt,
    })
    data = ollama_generate_json(prompt, timeout=240, schema=SCHEMA_NOTICIAS, options=OPTS_JSON)
    if data is None:
        # LLM não respondeu: o ciclo não marca esta newsletter como vista (entra numa próxima edição)
        m["_extracao_falhou"] = True
        log(f"[jornal] Extração falhou para '{m['subject'][:50]}' — fica para o próximo ciclo")
        return []
    itens = []
    if isinstance(data, list):
        itens = data
    elif isinstance(data, dict):
        for k in ("itens", "noticias", "items", "news"):
            if isinstance(data.get(k), list):
                itens = data[k]
                break
        else:
            if data.get("titulo"):
                itens = [data]

    fonte = m["from_name"] or m["from_domain"]
    out: list[dict] = []
    for it in itens[:max_itens]:
        if not isinstance(it, dict):
            continue
        titulo = str(it.get("titulo") or it.get("title") or "").strip()
        if len(titulo) < 5:
            continue
        url = str(it.get("url") or "").strip()
        # anti-alucinação: só URLs que existem no e-mail (apelido → URL real); o LLM pode devolver a URL com
        # utm_* como está no corpo, e as permitidas guardam a versão limpa
        url = permitidas.get(url) or permitidas.get(limpar_url(url), "") if url else ""
        out.append({
            "title":     titulo[:160],
            "url":       url,
            "snippet":   str(it.get("resumo") or it.get("summary") or "").strip()[:400],
            "source":    fonte,
            "published": m["date_iso"],
            "email_id":  m["message_id"],
        })
    log(f"[jornal] {fonte}: {len(out)} notícia(s) extraída(s) de '{m['subject'][:50]}'")
    return out


_URL_SOLTA_RE = re.compile(r"(?<!\]\()(?<![\w/])https?://[^\s)>\]]+")


def _tirar_urls_soltas(texto: str, urls_ok: set) -> str:
    """URL "solta" (fora de [título](url)) que não veio de nenhum e-mail → removida (o LLM inventa);
    linha que fica vazia por causa disso some junto."""
    linhas = []
    for linha in texto.split("\n"):
        nova = _URL_SOLTA_RE.sub(lambda mm: mm.group(0) if mm.group(0).rstrip(".,;:!?") in urls_ok
                                 else mm.group(0)[len(mm.group(0).rstrip(".,;:!?")):], linha)
        if nova != linha and not nova.strip(" \t-*•>:.,;"):
            continue
        linhas.append(nova)
    return "\n".join(linhas)


def gerar_jornal(itens: list[dict], emails_noticia: list[dict], cfg: dict, inbox_name: str,
                 notificar: bool = True) -> Optional[dict]:
    """Monta a edição do dia a partir dos itens extraídos. Retorna metadados ou None.
    LLM sem resposta marca os e-mails com "_jornal_falhou" (o ciclo não os dá como vistos: tenta de novo)."""
    jcfg   = cfg.get("jornal") or {}
    perfil = cfg.get("_perfil") or {}
    now    = _now()
    date_str, time_str, date_human = now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), now.strftime("%d/%m/%Y")

    unique = deduplicate_by_url_and_title(itens)[: int(jcfg.get("max_noticias", 18))]
    minimo = int(jcfg.get("min_noticias", 2))
    if len(unique) < minimo:
        log(f"[jornal] Só {len(unique)} notícia(s) (< {minimo}) — edição não gerada")
        return None

    pesos = {str(k): float(v) for k, v in (jcfg.get("pesos_fontes") or {}).items()}
    clustered = cluster_items(unique)
    ranked    = rank_clusters(clustered, pesos)
    destaques, radar = build_clustered_news_text(clustered, ranked, pesos, max_destaques=8)
    fontes_lista = ", ".join(sorted({e["from_name"] or e["from_domain"] for e in emails_noticia}))

    prompt = render_template(jcfg.get("template_prompt", "jornal_email"), {
        "data":                date_human,
        "perfil_descricao":    perfil.get("descricao", ""),
        "interesses_quentes":  ", ".join(perfil.get("interesses_quentes", [])),
        "interesses_mornos":   ", ".join(perfil.get("interesses_mornos", [])),
        "excluir":             ", ".join(perfil.get("excluir", [])),
        "tom":                 perfil.get("tom", ""),
        "fontes_lista":        fontes_lista,
        "news_text_destaques": destaques,
        "news_text_radar":     radar,
    })
    log("[jornal] Gerando edição escrita...")
    texto = ollama_generate(prompt, timeout=300)
    if not texto:
        log("[jornal] Ollama não retornou a edição — as newsletters ficam para o próximo ciclo")
        for e in emails_noticia:
            e["_jornal_falhou"] = True
        return None
    if not radar:
        texto = re.sub(r"\n?#{1,3}\s*Radar\b.*?(?=\n#{1,3}\s|\Z)", "", texto, flags=re.S | re.I).strip()
    # [Título](https) / [Título]() / [Título](URL inventada) → só o título
    urls_ok = {it["url"] for it in unique if it.get("url")}
    texto = re.sub(r"\[([^\]]+)\]\(([^)]*)\)",
                   lambda mm: mm.group(0) if mm.group(2) in urls_ok else mm.group(1), texto)
    texto = _tirar_urls_soltas(texto, urls_ok)

    titulo = generate_title_via_llm(texto, fallback=f"Edição do dia — {date_human}")
    slug   = slugify(titulo)
    base   = f"{date_str}_{slug}" if slug else f"{date_str}_{inbox_name}"
    jd     = jornal_dir()

    md = "\n".join([
        "---",
        f"title: {json.dumps(titulo, ensure_ascii=False)}",   # título com aspas não quebra o YAML
        f"date: {date_str}",
        f'time: "{time_str}"',
        "type: jornal",
        f"inbox: {inbox_name}",
        f"newsletters: {len(emails_noticia)}",
        f"noticias: {len(unique)}",
        f"tags: [jornal, {inbox_name}]",
        "---",
        "",
        f"# {titulo}",
        "",
        f"> Edição do dia · {date_human} às {time_str} · {len(emails_noticia)} newsletter(s) · {len(unique)} notícia(s)",
        "",
        texto.strip(),
        "",
        "---",
        "",
        "## Newsletters desta edição",
        "",
    ])
    for e in emails_noticia:
        md += f"- **{e['from_name'] or e['from_domain']}** — {e['subject']} ({e['date_iso'][:16]})\n"
    md += "\n## Notícias extraídas\n\n"
    for i, it in enumerate(unique, 1):
        link = f"[{it['title']}]({it['url']})" if it.get("url") else it["title"]
        md += f"{i}. {link} — *{it['source']}*\n"

    md_path = jd / f"{base}.md"
    md_path.write_text(md, encoding="utf-8")
    log(f"[jornal] Markdown salvo: {md_path.name}")
    # Metadados para o podcast semanal (semanal.py): e-mails e notícias desta edição
    try:
        gravar_atomico(jd / f"{base}.json", json.dumps({
            "data": date_str,
            "emails": [{"message_id": e["message_id"], "from": f"{e['from_name']} <{e['from_addr']}>",
                        "from_name": e["from_name"] or e["from_domain"], "subject": e["subject"],
                        "date": e["date_iso"], "pasta": e.get("folder", ""), "conta": e.get("account", "")}
                       for e in emails_noticia],
            "noticias": [{k: it.get(k, "") for k in ("title", "url", "snippet", "source", "published", "email_id")}
                         for it in unique],
        }, ensure_ascii=False, indent=2))
    except Exception as e:
        log(f"[jornal] Falha ao gravar metadados JSON: {e}")

    mp3_path: Optional[Path] = None
    if "audio" in (jcfg.get("formato") or []):
        log("[jornal] Gerando roteiro de áudio...")
        roteiro = ollama_generate(render_template(jcfg.get("template_audio", "audio_jornal"),
                                                  {"briefing": texto, "data": date_human}), timeout=240)
        if not roteiro:
            log("[jornal] Sem roteiro — usando texto limpo")
            roteiro = limpar_para_fala(texto)
        mp3_path = jd / f"{base}.mp3"
        if not tts_save(roteiro, jcfg.get("tts_voice", "pt-BR-FranciscaNeural"), mp3_path):
            mp3_path = None
        else:
            log(f"[jornal] Áudio salvo: {mp3_path.name}")

    if notificar:
        _notify(f"Alfred — Jornal do dia", f"{titulo}\n{len(unique)} notícias de {len(emails_noticia)} newsletters"
                + (" · áudio pronto" if mp3_path else ""))
    return {"titulo": titulo, "md": str(md_path), "mp3": str(mp3_path) if mp3_path else None,
            "noticias": len(unique), "newsletters": len(emails_noticia), "data": date_str}


# ══════════════════════════════════════════════════════════════
#  Pendências — eventos e contatos aguardando "vou / não vou"
# ══════════════════════════════════════════════════════════════

def preservar_corrompido(f: Path, motivo: str, chave: str) -> Optional[Path]:
    """
    YAML ilegível vai ser tratado como vazio — e a próxima gravação o substitui. Antes disso, guarda uma
    cópia "<nome>.corrompido-<AAAAmmdd-HHMMSS>" ao lado (uma vez por conteúdo: se já existe cópia idêntica,
    não duplica). Sem conseguir copiar → RuntimeError: melhor falhar do que apagar o original.
    """
    f = Path(f)
    motivo = " ".join(str(motivo).split())[:300]
    try:
        bruto = f.read_bytes()
    except OSError:
        return None
    for antiga in sorted(f.parent.glob(f"{f.name}.corrompido-*")):
        try:
            if antiga.read_bytes() == bruto:
                return antiga
        except OSError:
            continue
    copia = f.with_name(f"{f.name}.corrompido-{datetime.now():%Y%m%d-%H%M%S}")
    n = 2
    while copia.exists():
        copia = f.with_name(f"{f.name}.corrompido-{datetime.now():%Y%m%d-%H%M%S}-{n}")
        n += 1
    try:
        shutil.copy2(f, copia)
    except OSError as e:
        raise RuntimeError(f"{f} ilegível ({motivo}) e não consegui salvar uma cópia ({e}) — "
                           f"corrija o arquivo antes de continuar") from e
    log(f"[{chave}] !!! ATENÇÃO: {f} ilegível ({motivo}) — cópia do original em {copia.name}; "
        f"seguindo com a lista vazia (a próxima gravação substitui o arquivo)")
    return copia


def _load_pend() -> dict:
    f = pend_file()
    if not f.exists():
        return {"pendencias": []}
    try:
        texto = f.read_text(encoding="utf-8")
        data = yaml.safe_load(texto) or {}
    except Exception as e:
        preservar_corrompido(f, f"YAML inválido: {e}", "pendencias")
        return {"pendencias": []}
    if not isinstance(data, dict) or not isinstance(data.get("pendencias"), list):
        if texto.strip() and not (isinstance(data, dict) and data.get("pendencias") is None):
            preservar_corrompido(f, "sem a lista 'pendencias:'", "pendencias")
        return {"pendencias": []}
    return data


def _save_pend(data: dict):
    """Grava pendencias.yml de forma atômica. Quem lê-muda-grava deve estar dentro de trava_pendencias()."""
    with trava_pendencias():   # reentrante: garante o .tmp exclusivo mesmo se o chamador esqueceu a trava
        gravar_atomico(pend_file(), yaml.dump(data, allow_unicode=True, sort_keys=False))


def listar_pendencias(status: Optional[str] = "aguardando") -> list[dict]:
    pend = _load_pend()["pendencias"]
    if status in (None, "", "todas"):
        return pend
    return [p for p in pend if p.get("status") == status]


def obter_pendencia(pid: str) -> Optional[dict]:
    for p in _load_pend()["pendencias"]:
        if p.get("id") == pid:
            return p
    return None


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _novo_id(titulo: str, existentes: set[str]) -> str:
    base = f"{_now():%Y%m%d}-{slugify(_ascii(titulo), 32) or 'pendencia'}"
    pid, n = base, 2
    while pid in existentes:
        pid = f"{base}-{n}"
        n += 1
    return pid


def _ids_ocupados(data: dict) -> set[str]:
    """Ids do yml + nomes dos .md em pendencias/ (um id reservado por outro processo já tem o .md)."""
    ids = {p.get("id") for p in data["pendencias"]}
    ids.update(f.stem for f in pend_dir().glob("*.md"))
    return ids


def _reservar_id(titulo: str) -> str:
    """
    Id novo sem corrida: calcula dentro da trava e cria o .md vazio como reserva. Quem cria a pendência
    gasta LLM/TTS fora da trava e só no fim anexa a entrada ao yml — outro processo criando ao mesmo
    tempo vê o .md e escolhe outro id.
    """
    with trava_pendencias():
        pid = _novo_id(titulo, _ids_ocupados(_load_pend()))
        (pend_dir() / f"{pid}.md").touch()
    return pid


def _soltar_reserva(pid: str) -> None:
    """Criação falhou no meio: apaga o .md de reserva se ainda estiver vazio."""
    try:
        md = pend_dir() / f"{pid}.md"
        if md.is_file() and md.stat().st_size == 0:
            md.unlink()
    except OSError:
        pass


def _via(origem: str) -> Optional[str]:
    """'via painel' → 'painel' (campo respondido_via); vazio → None."""
    o = (origem or "").strip()
    if o.lower() == "via" or o.lower().startswith("via "):
        o = o[3:].strip()
    return o or None


_CAMINHOS = {
    "celular":           "resposta direta pelo celular",
    "whatsapp":          "resposta citada no WhatsApp",
    "api":               "API (N8N, chat ou extensão)",
    "terminal":          "terminal",
    "notificação no pc": "notificação no PC",
    "notificação":       "notificação no PC",
    "painel":            "Painel do Alfred",
    "chat":              "chat do Open WebUI",
}


def caminho_resolucao(entry: dict) -> str:
    """Por onde a pendência foi resolvida, em texto legível ("Esta pendência já foi resolvida por <caminho>")."""
    via = str(entry.get("respondido_via") or "").strip()
    if via:
        return _CAMINHOS.get(via.lower(), via)
    status = entry.get("status")
    if status == "respondida_manual":
        return _CAMINHOS["celular"]
    if status == "expirada":
        return "expiração automática (o evento já passou)"
    nota = str(entry.get("nota") or "").strip()
    if nota.lower().startswith("via ") and nota[4:].strip():
        return _CAMINHOS.get(nota[4:].strip().lower(), nota[4:].strip())
    return "outro caminho (terminal, notificação ou chat)"


class PendenciaJaResolvida(ValueError):
    """Ação numa pendência que já saiu de 'aguardando' (outro caminho chegou antes).
    `caminho` = caminho_resolucao(entry); str(e) mantém a mensagem antiga do ValueError."""

    def __init__(self, entry: dict, mensagem: str = ""):
        self.entry = entry
        self.pid = entry.get("id")
        self.status = entry.get("status")
        self.caminho = caminho_resolucao(entry)
        super().__init__(mensagem or f"Pendência {self.pid} já está com status '{self.status}'")


def _parse_dt(s) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s.strip())
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_now().tzinfo)
    return d


def extrair_evento(m: dict, cat: dict) -> Optional[dict]:
    """E-mail evento/contato → dict estruturado. ICS tem prioridade sobre o texto."""
    links_txt, permitidas = _links_texto(m, max_links=25)
    ics_txt = "nenhum"
    if m.get("ics"):
        ics_txt = "\n".join(
            f"- {e.get('summary','')} | início: {e.get('dtstart')} | fim: {e.get('dtend')} | local: {e.get('location','')} | url: {e.get('url','')}"
            for e in m["ics"][:3]
        )
    now = _now()
    prompt = render_template("email_evento_extrair", {
        "remetente":  f"{m['from_name']} <{m['from_addr']}>",
        "assunto":    m["subject"],
        "data_email": m["date_iso"] or "desconhecida",
        "data_hoje":  now.strftime("%Y-%m-%d (%A)"),
        "corpo":      _corpo_prompt(m, 5000),
        "links":      links_txt,
        "ics":        ics_txt,
    })
    data = ollama_generate_json(prompt, timeout=240, schema=SCHEMA_EVENTO, options=OPTS_JSON)
    if not isinstance(data, dict):
        log(f"[pendencias] Extração falhou para '{m['subject'][:50]}' — usando dados mínimos")
        data = {}

    tipo = str(data.get("tipo") or cat.get("categoria") or "evento").lower()
    if tipo not in ("evento", "contato"):
        tipo = "evento" if cat.get("categoria") == "evento" else "contato"

    ev = {
        "tipo":           tipo,
        "titulo":         (str(data.get("titulo") or m["subject"]).strip() or m["subject"])[:120],
        "organizador":    str(data.get("organizador") or m["from_name"]).strip()[:120],
        "quem":           str(data.get("quem") or "").strip()[:160],
        "data_inicio":    data.get("data_inicio") or None,
        "data_fim":       data.get("data_fim") or None,
        "local":          str(data.get("local") or "").strip()[:200],
        "online":         data.get("online") if isinstance(data.get("online"), bool) else None,
        "link":           str(data.get("link") or "").strip(),
        "custo":          str(data.get("custo") or "desconhecido").strip().lower(),
        "o_que_pedem":    str(data.get("o_que_pedem") or "").strip()[:300],
        "prazo_resposta": data.get("prazo_resposta") or None,
        "resumo":         str(data.get("resumo") or cat.get("resumo") or "").strip()[:600],
        "requer_decisao": data.get("requer_decisao") if isinstance(data.get("requer_decisao"), bool) else True,
    }
    ev["link"] = permitidas.get(ev["link"], "") or _link_de_acao(m, permitidas)
    for k in ("data_inicio", "data_fim", "prazo_resposta"):
        if ev[k] is not None and not isinstance(ev[k], str):
            ev[k] = str(ev[k])
        if ev[k] in ("null", "None", ""):
            ev[k] = None

    if m.get("ics"):  # ICS vence
        i = m["ics"][0]
        ev["titulo"]      = (i.get("summary") or ev["titulo"])[:120]
        ev["data_inicio"] = i.get("dtstart") or ev["data_inicio"]
        ev["data_fim"]    = i.get("dtend") or ev["data_fim"]
        ev["local"]       = i.get("location") or ev["local"]
        ev["link"]        = i.get("url") or ev["link"]
        ev["organizador"] = i.get("organizer") or ev["organizador"]
        ev["tipo"]        = "evento"
        ev["requer_decisao"] = True
    return ev


_ACAO_RE = re.compile(r"inscr|regist|confirm|particip|rsvp|garant|vaga|reserv|agend|detalhe|saiba|evento|meetup", re.I)


def _link_de_acao(m: dict, permitidas: dict[str, str]) -> str:
    """
    Quando o LLM não devolve um link válido: o link cujo texto-âncora parece ação, senão o primeiro.
    Aqui o limite de tamanho não se aplica (ele só existe pra poupar contexto do prompt).
    """
    candidatos = [(t, u) for t, u in m.get("links", []) if not _link_ruim(t, u)]
    for t, u in candidatos:
        if _ACAO_RE.search(t or ""):
            return u
    for t, u in candidatos:
        if u in permitidas:
            return u
    return candidatos[0][1] if candidatos else ""


def _ja_na_fila(pend: list[dict], m: dict, ev: dict) -> Optional[dict]:
    t_norm = re.sub(r"\W+", " ", ev["titulo"].lower()).strip()
    for p in pend:
        # pendências de WhatsApp gravam "email": None (e podem não ter "evento") — .get(k, {}) não basta
        if (p.get("email") or {}).get("message_id") == m["message_id"]:
            return p
        if p.get("status") != "aguardando":
            continue
        if ev.get("link") and (p.get("evento") or {}).get("link") == ev["link"]:
            return p
        if t_norm and re.sub(r"\W+", " ", str(p.get("titulo", "")).lower()).strip() == t_norm:
            return p
    return None


def _dados_para_prompt(ev: dict) -> str:
    rotulos = {
        "tipo": "Tipo", "titulo": "Título", "organizador": "Organizador", "quem": "Quem escreve",
        "data_inicio": "Início", "data_fim": "Fim", "local": "Local", "online": "Online",
        "link": "Link", "custo": "Custo", "o_que_pedem": "O que pedem", "prazo_resposta": "Prazo para responder",
        "resumo": "Resumo",
    }
    linhas = []
    for k, r in rotulos.items():
        v = ev.get(k)
        if v in (None, "", "desconhecido"):
            continue
        if isinstance(v, bool):
            v = "sim" if v else "não"
        linhas.append(f"- {r}: {v}")
    return "\n".join(linhas) or "- (nenhum dado estruturado — use o trecho do e-mail)"


def _texto_fallback(ev: dict, m: dict) -> str:
    return "\n".join([
        "## O que é",
        ev.get("resumo") or f"E-mail de {m['from_name']} com o assunto \"{m['subject']}\".",
        "",
        "## Por que pode (ou não) interessar",
        "Não consegui avaliar automaticamente — o modelo não respondeu. Leia o e-mail original.",
        "",
        "## O que precisa decidir",
        ev.get("o_que_pedem") or "Confirmar presença ou responder ao remetente.",
        "",
        "## Vou ou não vou?",
        "Responda **vou** ou **não vou**.",
    ])


def criar_pendencia(m: dict, ev: dict, cfg: dict, inbox_name: str) -> dict:
    # id reservado na trava; LLM + TTS (~1 min) fora dela; o yml só é relido e gravado no fim —
    # antes, a lista carregada aqui era gravada de volta e apagava o que outro processo criou no meio
    pid = _reservar_id(ev["titulo"])
    try:
        entry = _montar_pendencia(pid, m, ev, cfg, inbox_name)
    except BaseException:
        _soltar_reserva(pid)
        raise
    with trava_pendencias():
        data = _load_pend()
        data["pendencias"].append(entry)
        _save_pend(data)
    log(f"[pendencias] Criada {pid} ({ev['tipo']}) — md{' + mp3' if entry['arquivos']['mp3'] else ''}")
    return entry


def _montar_pendencia(pid: str, m: dict, ev: dict, cfg: dict, inbox_name: str) -> dict:
    """Texto explicativo (.md) + áudio da pergunta → entrada de pendencias.yml (ainda não gravada)."""
    pcfg   = cfg.get("pendencias") or {}
    perfil = cfg.get("_perfil") or {}
    now    = _now()

    prompt = render_template(pcfg.get("template_pergunta", "pergunta_evento"), {
        "data_hoje":          now.strftime("%d/%m/%Y (%A)"),
        "perfil_descricao":   perfil.get("descricao", ""),
        "interesses_quentes": ", ".join(perfil.get("interesses_quentes", [])),
        "tom":                perfil.get("tom", ""),
        "tipo":               ev["tipo"],
        "dados":              _dados_para_prompt(ev),
        "trecho_email":       (m["text"] or "")[:1500],
    })
    log(f"[pendencias] Escrevendo pergunta para '{ev['titulo'][:50]}'...")
    texto = ollama_generate(prompt, timeout=180) or _texto_fallback(ev, m)

    pd = pend_dir()
    md_path  = pd / f"{pid}.md"
    mp3_path = pd / f"{pid}.mp3"

    def _fm(v):
        return json.dumps(v, ensure_ascii=False) if isinstance(v, str) else ("null" if v is None else str(v).lower() if isinstance(v, bool) else v)

    md = "\n".join([
        "---",
        f"id: {pid}",
        "status: aguardando",
        f"tipo: {ev['tipo']}",
        f"title: {_fm(ev['titulo'])}",
        f"data_inicio: {_fm(ev['data_inicio'])}",
        f"local: {_fm(ev['local'])}",
        f"link: {_fm(ev['link'])}",
        f"organizador: {_fm(ev['organizador'])}",
        f"criado_em: {now.isoformat(timespec='minutes')}",
        f"email_de: {_fm(m['from_addr'])}",
        f"email_assunto: {_fm(m['subject'])}",
        f"email_data: {_fm(m['date_iso'])}",
        f"tags: [pendencia, {ev['tipo']}, {inbox_name}]",
        "---",
        "",
        f"# {ev['titulo']}",
        "",
        f"> {ev['tipo']} · de **{m['from_name']}** · recebido em {m['date_iso'][:16]} · aguardando resposta",
        "",
        texto.strip(),
        "",
        "---",
        "",
        "## Dados extraídos",
        "",
        _dados_para_prompt(ev),
        "",
        "## Como responder",
        "",
        f"- Terminal: `services/inbox/run.sh responder {pid} vou` ou `... nao-vou`",
        f"- Chat (Open WebUI, tool Alfred Pendências): \"responder pendência {pid}: vou\"",
        f"- Áudio da pergunta: `services/inbox/run.sh ouvir {pid}`",
        "",
    ])
    md_path.write_text(md, encoding="utf-8")

    roteiro = ollama_generate(render_template(pcfg.get("template_audio", "audio_pergunta_evento"),
                                              {"texto": texto, "tipo": ev["tipo"]}), timeout=120)
    if not roteiro:
        roteiro = limpar_para_fala(texto)
    tem_audio = tts_save(roteiro, pcfg.get("tts_voice", "pt-BR-AntonioNeural"), mp3_path)

    entry = {
        "id":            pid,
        "tipo":          ev["tipo"],
        "status":        "aguardando",
        "titulo":        ev["titulo"],
        "criado_em":     now.isoformat(timespec="minutes"),
        "inbox":         inbox_name,
        "evento":        {k: ev[k] for k in ("organizador", "quem", "data_inicio", "data_fim", "local", "online",
                                             "link", "custo", "o_que_pedem", "prazo_resposta", "resumo")},
        "email":         {"message_id": m["message_id"], "from": m["from_addr"], "from_name": m["from_name"],
                          "subject": m["subject"], "date": m["date_iso"]},
        "arquivos":      {"md": f"pendencias/{md_path.name}", "mp3": f"pendencias/{mp3_path.name}" if tem_audio else None},
        "resposta_links": None,
        "lembrado_em":   None,
        "respondido_em": None,
        "resposta":      None,
        "nota":          None,
    }
    return entry


def _tem_gui() -> bool:
    return bool(shutil.which("kdialog") or shutil.which("zenity") or shutil.which("notify-send"))


def _iniciar_aguardador(comando: str, pid: str):
    """
    Processo destacado que fica ocioso na pergunta gráfica até Pedro clicar.

    Sob o systemd (timer oneshot do ciclo/vigia, ou a própria API), um filho comum morre junto quando a
    unit termina ou reinicia — a notificação sumia sem resposta. Nesse caso o aguardador vira uma unit
    transitória própria (`systemd-run --user`), que herda a sessão gráfica do gerenciador do usuário.
    """
    cmd = [sys.executable, str(SCRIPT_DIR / "inbox-assistant.py"), comando, pid]
    if os.getenv("INVOCATION_ID") and shutil.which("systemd-run"):
        nome = re.sub(r"[^A-Za-z0-9_.-]", "-", _ascii(f"alfred-{comando.strip('_')}-{pid}"))[:80]
        cmd = ["systemd-run", "--user", "--collect", "--quiet", f"--unit={nome}-{datetime.now():%H%M%S%f}",
               f"--working-directory={SCRIPT_DIR}", "--"] + cmd
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        log(f"[pendencias] Não foi possível iniciar o aguardador de resposta: {e}")


def notificar_pendencia(entry: dict, aguardar_resposta: bool = True):
    """Notificação desktop com botões (processo destacado, fica ocioso aguardando):
    e-mail → Vou / Não vou / Ouvir · WhatsApp → Enviar (rascunho editável) / Não enviar / Agenda / Ouvir."""
    if entry.get("tipo") == "whatsapp":
        wa = entry.get("whatsapp") or {}
        if aguardar_resposta and _tem_gui():
            # o aguardador toca o resumo junto com a notificação (botões Parar / Ouvir de novo)
            _iniciar_aguardador("_aguardar-whatsapp", entry["id"])
        else:
            _notify("Alfred — WhatsApp",
                    f"{wa.get('contato') or entry['titulo']}: {(wa.get('texto') or '')[:120]}\n"
                    f"Responda: run.sh responder-texto {entry['id']} \"...\"")
            if os.getenv("INBOX_AUTOPLAY", "false").lower() in ("1", "true", "yes") and entry.get("arquivos", {}).get("mp3"):
                tocar_audio(vault() / entry["arquivos"]["mp3"])
        return
    quando = entry.get("evento", {}).get("data_inicio") or "data não informada"
    _notify(f"Alfred — {'Convite' if entry['tipo'] == 'evento' else 'Contato profissional'}",
            f"{entry['titulo']}\n{quando}\nResponda: run.sh responder {entry['id']} vou | nao-vou")
    if os.getenv("INBOX_AUTOPLAY", "false").lower() in ("1", "true", "yes") and entry.get("arquivos", {}).get("mp3"):
        tocar_audio(vault() / entry["arquivos"]["mp3"])
    if aguardar_resposta and _tem_gui():
        _iniciar_aguardador("_aguardar-resposta", entry["id"])


def _load_tasks(f: Path) -> dict:
    """tasks.yml → {"tasks": [...]}. Ilegível: guarda cópia (preservar_corrompido) e segue vazio."""
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else None
    except Exception as e:
        preservar_corrompido(f, f"YAML inválido: {e}", "tasks")
        # os ids continuam depois do maior que havia no arquivo ilegível: pendências antigas guardam task_id
        # e não podem passar a apontar (ou cancelar) uma tarefa nova
        ids = [int(x) for x in re.findall(r"^\s*-?\s*id:\s*(\d+)\s*$", f.read_text(encoding="utf-8", errors="replace"), re.M)]
        _IDS_CORROMPIDO[str(f)] = max(ids, default=0)
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        if (data is not None and f.exists() and f.read_text(encoding="utf-8").strip()
                and not (isinstance(data, dict) and data.get("tasks") is None)):
            preservar_corrompido(f, "sem a lista 'tasks:'", "tasks")
        data = {"tasks": []}
    return data


def _cancelar_task(tid) -> bool:
    """Tarefa aberta → status "cancelled" + completed_at (Pedro desistiu do evento). True se mudou."""
    f = vault() / "tasks.yml"
    with trava_arquivo(f):
        if not f.exists():
            return False
        data = _load_tasks(f)
        t = next((t for t in data["tasks"] if t.get("id") == tid), None)
        if not t or t.get("status") != "open":
            return False
        t["status"] = "cancelled"
        t["completed_at"] = datetime.now().isoformat(timespec="minutes")
        gravar_atomico(f, yaml.dump(data, allow_unicode=True, sort_keys=False))
    return True


def _desfazer_agenda_da_pendencia(pid: str) -> list[str]:
    """Tira da agenda (e do calendário, via extensão) o que o 'vou' desta pendência agendou."""
    try:
        import agenda
        ids = [i["id"] for i in agenda.listar()
               if (i.get("origem") or {}).get("pendencia_id") == pid
               and i.get("status") not in ("desfeito", "cancelado", "desfazer")]
        for aid in ids:
            agenda.desfazer(aid)
        return ids
    except Exception as e:
        log(f"[agenda] não consegui desfazer o agendamento de {pid}: {e}")
        return []


_IDS_CORROMPIDO: dict[str, int] = {}   # tasks.yml ilegível → maior id que ele tinha (ver _load_tasks)


def _add_task(title: str, due_date: Optional[str], tags: list[str]) -> Optional[int]:
    """Cria tarefa em VAULT_ALFRED/tasks.yml no mesmo formato da tool alfred_tasks.
    Trava + gravação atômica: a tool do Open WebUI e o agendamento gravam o mesmo arquivo (ids únicos)."""
    f = vault() / "tasks.yml"
    with trava_arquivo(f):
        data = _load_tasks(f)
        tid = max(max((t.get("id", 0) for t in data["tasks"]), default=0), _IDS_CORROMPIDO.get(str(f), 0)) + 1
        data["tasks"].append({
            "id": tid, "title": title, "status": "open", "priority": "normal", "tags": tags,
            "due_date": due_date, "created_at": datetime.now().isoformat(timespec="minutes"), "completed_at": None,
        })
        gravar_atomico(f, yaml.dump(data, allow_unicode=True, sort_keys=False))
    return tid


def responder(pid: str, resposta: str, nota: str = "", origem: str = "", so_se_aguardando: bool = False) -> dict:
    """
    Registra a decisão. 'vou' também cria tarefa em tasks.yml.
    `origem` ("via terminal", "via painel"...) vira respondido_via. so_se_aguardando=True: pendência que
    já saiu de 'aguardando' levanta PendenciaJaResolvida e nada muda (painel, aguardador da notificação).
    """
    r = resposta.strip().lower().replace("-", "_").replace(" ", "_")
    if r in ("vou", "sim", "yes", "aceito"):
        r = "vou"
    elif r in ("nao_vou", "não_vou", "nao", "não", "no", "recuso"):
        r = "nao_vou"
    elif r in ("ignorar", "ignorada"):
        r = "ignorada"
    else:
        raise ValueError(f"Resposta inválida: {resposta!r} (use vou | nao-vou | ignorar)")

    # ler → checar → mudar → gravar numa transação só (tarefa em tasks.yml aninhada: ordem pendências → tasks)
    with trava_pendencias():
        data = _load_pend()
        entry = next((p for p in data["pendencias"] if p.get("id") == pid), None)
        if not entry:
            raise KeyError(f"Pendência não encontrada: {pid}")
        if entry.get("tipo") == "whatsapp" and r != "ignorada":
            raise ValueError(f"{pid} é uma pendência de WhatsApp: responda com texto livre "
                             f"(responder-texto {pid} \"...\") ou 'ignorar'")
        if so_se_aguardando and entry.get("status") != "aguardando":
            raise PendenciaJaResolvida(entry)
        anterior = entry.get("status")
        if anterior == r:
            # mesma resposta de novo (N8N reenviando o resumeUrl, duplo clique): nada muda, nada duplica
            log(f"[pendencias] {pid} já está '{r}' — nada a fazer")
            return {**entry, "_task_criada": False}

        now = _now()
        entry["status"] = r
        entry["resposta"] = r
        entry["respondido_em"] = now.isoformat(timespec="minutes")
        entry["nota"] = nota.strip() or None
        if origem:
            entry["respondido_via"] = _via(origem)

        task_id = None
        task_cancelada = None
        if r == "vou":
            due = ((entry.get("evento") or {}).get("data_inicio") or "")[:10] or None
            prefixo = "Ir: " if entry["tipo"] == "evento" else "Responder: "
            task_id = _add_task(prefixo + entry["titulo"], due, ["pendencia", entry["tipo"]])
            entry["task_id"] = task_id
        elif anterior == "vou" and entry.get("task_id"):
            # trocou de ideia (vou → não vou / ignorar): a tarefa criada pelo "vou" é cancelada
            task_cancelada = entry["task_id"]
            _cancelar_task(task_cancelada)
            entry["task_id"] = None
            entry["task_id_cancelada"] = task_cancelada
        _save_pend(data)

    md_rel = (entry.get("arquivos") or {}).get("md")
    if md_rel:
        md_path = vault() / md_rel
        if md_path.exists():
            txt = md_path.read_text(encoding="utf-8")
            txt = re.sub(r"^status: \w+$", f"status: {r}", txt, count=1, flags=re.M)
            rotulo = {"vou": "VOU", "nao_vou": "NÃO VOU", "ignorada": "IGNORADA"}[r]
            txt = txt.rstrip() + f"\n\n## Resposta\n\n**{rotulo}** — {now.strftime('%d/%m/%Y %H:%M')}"
            if nota.strip():
                txt += f"\n\nNota: {nota.strip()}"
            if task_id:
                txt += f"\n\nTarefa criada em `tasks.yml` (id {task_id})."
            if task_cancelada:
                txt += f"\n\nTarefa {task_cancelada} cancelada em `tasks.yml`."
            md_path.write_text(txt + "\n", encoding="utf-8")

    log(f"[pendencias] {pid} → {r}" + (f" (tarefa #{task_id})" if task_id else "")
        + (f" (tarefa #{task_cancelada} cancelada)" if task_cancelada else ""))
    if r == "vou":
        _agendar_evento_email(entry)
    elif anterior == "vou":
        _desfazer_agenda_da_pendencia(pid)
    # _task_criada: o CLI/API só dizem "tarefa #N criada" quando ela nasceu NESTA chamada
    return {**entry, "_task_criada": bool(task_id)}


def _agendar_evento_email(entry: dict) -> None:
    """'Vou' num evento de e-mail com data → agenda (calendário do Thunderbird via extensão)."""
    try:
        import agenda
        if not agenda._cfg()["auto_email_vou"]:
            return
        ev = entry.get("evento") or {}
        ini = ev.get("data_inicio")
        if not ini:
            return
        dia_inteiro = len(str(ini)) <= 10
        inicio = datetime.fromisoformat(str(ini) if not dia_inteiro else f"{ini}T09:00")
        if inicio.tzinfo is None:
            inicio = inicio.replace(tzinfo=_now().tzinfo)
        fim = None
        if ev.get("data_fim") and not dia_inteiro and len(str(ev["data_fim"])) > 10:
            fim = datetime.fromisoformat(str(ev["data_fim"]))
            fim = fim if fim.tzinfo else fim.replace(tzinfo=_now().tzinfo)
            if fim <= inicio:
                fim = None
        if inicio.date() < _now().date():
            return
        em = entry.get("email") or {}
        desc = "\n".join(x for x in [
            ev.get("resumo") or "", f"Link: {ev['link']}" if ev.get("link") else "",
            f"E-mail: {em.get('subject', '')} — {em.get('from_name') or em.get('from', '')}",
            f"Pendência Alfred: {entry['id']}"] if x)
        agenda.adicionar(entry.get("titulo") or "Evento", inicio, dia_inteiro=dia_inteiro, tipo="evento", fim=fim,
                         local=ev.get("local") or ("online" if ev.get("online") else ""), descricao=desc,
                         origem={"canal": "email", "pendencia_id": entry["id"], "assunto": em.get("subject", "")})
    except Exception as e:
        log(f"[agenda] não consegui agendar {entry.get('id')}: {e}")


def registrar_links_resposta(pid: str, links: dict) -> dict:
    """Guarda URLs de resposta externas (ex.: resumeUrl do Wait do N8N) na pendência."""
    with trava_pendencias():
        data = _load_pend()
        entry = next((p for p in data["pendencias"] if p.get("id") == pid), None)
        if not entry:
            raise KeyError(f"Pendência não encontrada: {pid}")
        entry["resposta_links"] = {k: str(v) for k, v in (links or {}).items() if v}
        _save_pend(data)
    return entry


def _expira_em(ev: dict) -> Optional[datetime]:
    """
    A partir de quando a pendência do evento expira. Só data ("2026-10-26", .ics de dia inteiro) → depois
    do fim do dia (do data_fim, se houver); com hora → data_fim, ou 2 h depois do início sem data_fim.
    """
    ini_s, fim_s = ev.get("data_inicio"), ev.get("data_fim")
    inicio = _parse_dt(ini_s)
    if not inicio:
        return None
    fim = _parse_dt(fim_s)
    if fim and fim < inicio:
        fim = None
    meia_noite = lambda d: d.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)  # noqa: E731
    if len(str(ini_s).strip()) <= 10:
        return meia_noite(fim or inicio)
    if fim:
        return fim if len(str(fim_s).strip()) > 10 else meia_noite(fim)
    return inicio + timedelta(hours=2)


def expirar_e_lembrar(cfg: dict) -> dict:
    """Expira pendências cujo evento já passou e re-notifica as que estão aguardando há tempo."""
    pcfg  = cfg.get("pendencias") or {}
    now   = _now()
    cada  = float(pcfg.get("lembrar_a_cada_horas", 12) or 0)
    expiradas, lembradas = [], []
    with trava_pendencias():
        data = _load_pend()
        for p in data["pendencias"]:
            if p.get("status") != "aguardando":
                continue
            limite = _expira_em(p.get("evento") or {})
            if pcfg.get("expirar_apos_evento", True) and limite and now >= limite:
                p["status"] = "expirada"
                p["respondido_em"] = now.isoformat(timespec="minutes")
                expiradas.append(p["id"])
                continue
            ultimo = _parse_dt(p.get("lembrado_em") or p.get("criado_em"))
            if cada > 0 and ultimo and now - ultimo >= timedelta(hours=cada):
                p["lembrado_em"] = now.isoformat(timespec="minutes")
                lembradas.append(p["id"])
        if expiradas or lembradas:
            _save_pend(data)
    if expiradas:
        log(f"[pendencias] Expiradas: {', '.join(expiradas)}")
    if lembradas:
        titulos = [p["titulo"] for p in data["pendencias"] if p["id"] in lembradas]
        _notify("Alfred — pendências aguardando sua resposta",
                "\n".join(f"• {t[:60]}" for t in titulos[:5]) + "\nrun.sh pendencias")
    return {"expiradas": expiradas, "lembradas": lembradas}


# ══════════════════════════════════════════════════════════════
#  Ciclo completo
# ══════════════════════════════════════════════════════════════

def _notify(titulo: str, corpo: str):
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.run(["notify-send", "--icon=mail-message-new", "--urgency=normal", "--app-name=Alfred",
                        titulo, corpo], timeout=5, capture_output=True)
    except Exception:
        pass


def indexar_cofres() -> dict:
    """
    Reindexa os cofres em lib_db (FTS5: VAULT_ALFRED e VAULT_PEDRO, o que estiver definido) e poda o histórico
    do WhatsApp além de `historico_dias` (perfis.yml, default 90; 0 = guarda tudo). Nunca levanta.
    """
    res: dict = {}
    for nome, var in (("alfred", "VAULT_ALFRED"), ("pedro", "VAULT_PEDRO")):
        caminho = os.getenv(var, "").strip()
        if caminho:
            res[nome] = lib_db.indexar_cofre(Path(caminho), nome)
    try:
        import whatsapp
        dias = float(whatsapp.carregar_cfg()[1].get("historico_dias", 90))
    except Exception:
        dias = 90.0
    res["wa_podadas"] = lib_db.podar_wa(dias)
    log(f"[indice] {res}")
    return res


def _resumo_msg(m: dict, c: dict) -> dict:
    return {
        "message_id": m["message_id"], "from": m["from_addr"], "from_name": m["from_name"],
        "subject": m["subject"], "date": m["date_iso"], "categoria": c["categoria"],
        "confianca": c["confianca"], "resumo": c.get("resumo", ""), "origem": c.get("origem", ""),
        **({"falhou": True} if c.get("falhou") else {}),
    }


def ciclo(
    inbox_name: Optional[str] = None,
    dry_run: bool = False,
    janela_horas: Optional[int] = None,
    reprocessar: bool = False,
    so_jornal: bool = False,
    so_pendencias: bool = False,
    max_emails: Optional[int] = None,
    notificar: bool = True,
) -> dict:
    nome, cfg = carregar_cfg(inbox_name)
    inicio = _now()
    log(f"=== Inbox '{nome}' (perfil {cfg.get('perfil')}) — {inicio:%d/%m/%Y %H:%M}{' [DRY-RUN]' if dry_run else ''} ===")

    msgs = coletar(cfg, janela_horas, max_emails)
    seen = load_seen()
    novos = [m for m in msgs if reprocessar or m["message_id"] not in seen]
    log(f"[ciclo] {len(msgs)} e-mails na janela, {len(novos)} ainda não processados")

    resumo: dict = {
        "inbox": nome, "inicio": inicio.isoformat(timespec="minutes"), "dry_run": dry_run,
        "emails_na_janela": len(msgs), "novos": len(novos), "classificacao": [],
        "contagem": {}, "jornal": None, "pendencias_novas": [], "pendencias_duplicadas": [], "informativos": [],
        "pendencias_aguardando": [], "manutencao": {},
    }

    emails_noticia: list[dict] = []
    eventos: list[tuple[dict, dict]] = []
    nao_marcar: set[str] = set()   # message_ids que NÃO vão para o seen: tratados num próximo ciclo
    for i, m in enumerate(novos, 1):
        c = classificar(m, cfg)
        if c.get("falhou"):
            nao_marcar.add(m["message_id"])
        resumo["classificacao"].append(_resumo_msg(m, c))
        resumo["contagem"][c["categoria"]] = resumo["contagem"].get(c["categoria"], 0) + 1
        log(f"[classif {i:>3}/{len(novos)}] {c['categoria']:<12} {c['confianca']:.2f} {m['from_domain']:<26} {m['subject'][:55]}")
        if c["categoria"] == "noticia":
            emails_noticia.append(m)
        elif c["categoria"] in ("evento", "contato"):
            eventos.append((m, c))

    if dry_run:
        log(f"[ciclo] DRY-RUN — {len(emails_noticia)} newsletter(s), {len(eventos)} evento(s)/contato(s); nada gravado")
        resumo["pendencias_aguardando"] = listar_pendencias("aguardando")
        return resumo

    if not so_pendencias and emails_noticia:
        itens: list[dict] = []
        for m in emails_noticia:
            itens.extend(extrair_noticias(m, cfg.get("jornal") or {}))
        resumo["jornal"] = gerar_jornal(itens, emails_noticia, cfg, nome, notificar=notificar)
        nao_marcar.update(m["message_id"] for m in emails_noticia if m.get("_extracao_falhou") or m.get("_jornal_falhou"))
    elif not so_pendencias:
        log("[jornal] Nenhuma newsletter nova — edição não gerada")

    if not so_jornal:
        existentes = _load_pend()["pendencias"]
        for m, c in eventos:
            ev = extrair_evento(m, c)
            if not ev:
                continue
            if not ev.get("requer_decisao", True):
                log(f"[pendencias] '{ev['titulo'][:50]}' é só informativo — sem decisão a tomar, pulando")
                resumo["informativos"].append({"titulo": ev["titulo"], "subject": m["subject"], "resumo": ev.get("resumo", "")})
                continue
            dup = _ja_na_fila(existentes, m, ev)
            if dup:
                log(f"[pendencias] '{ev['titulo'][:50]}' já na fila ({dup['id']}, {dup['status']})")
                resumo["pendencias_duplicadas"].append({"titulo": ev["titulo"], "id": dup["id"]})
                continue
            entry = criar_pendencia(m, ev, cfg, nome)
            existentes.append(entry)
            resumo["pendencias_novas"].append(entry)
            if notificar:
                notificar_pendencia(entry)

    # só marca como visto o que foi tratado: com --so-jornal os convites/contatos, e com --so-pendencias
    # as newsletters, ficam para o próximo ciclo completo (senão sumiam para sempre)
    if so_jornal:
        nao_marcar.update(m["message_id"] for m, _c in eventos)
    if so_pendencias:
        nao_marcar.update(m["message_id"] for m in emails_noticia)
    if nao_marcar:
        log(f"[ciclo] {len(nao_marcar)} e-mail(s) não marcado(s) como visto(s) — ficam para o próximo ciclo")
    for m in novos:
        if m["message_id"] not in nao_marcar:
            seen[m["message_id"]] = datetime.now().isoformat(timespec="minutes")
    save_seen(seen)
    import limpeza  # categorias → logs/inbox_categorias.json (base da limpeza pós-podcast)
    limpeza.registrar_classificacoes([c for c in resumo["classificacao"] if not c.get("falhou")])

    resumo["manutencao"] = expirar_e_lembrar(cfg)
    resumo["manutencao"]["indice"] = indexar_cofres()
    resumo["pendencias_aguardando"] = listar_pendencias("aguardando")
    resumo["fim"] = _now().isoformat(timespec="minutes")
    resumo["duracao_s"] = int((_now() - inicio).total_seconds())

    log("=== Ciclo concluído ===")
    log(f"  Classificação: {resumo['contagem']}")
    if resumo["jornal"]:
        log(f"  Jornal: {resumo['jornal']['titulo']} → {Path(resumo['jornal']['md']).name}")
    log(f"  Pendências novas: {len(resumo['pendencias_novas'])} · aguardando: {len(resumo['pendencias_aguardando'])}")
    return resumo
