#!/usr/bin/env python3
"""
Alfred — podcast "Resumo do Alfred" (a cada N dias; padrão 3 — perfis.yml podcast.intervalo_dias).

Desde 20/10/2026 o episódio cobre um PERÍODO de N dias terminando hoje (id AAAA-MM-DD_AAAA-MM-DD);
o modo antigo por semana ISO (`--semana 2026-W43`) continua funcionando. O timer roda todo dia às
19:00 com `--se-devido`: só gera se o último episódio tiver sido gerado há N dias ou mais.

    edições diárias do jornal           seleção                  roteiro (gemma4)            áudio (edge-tts)
    VAULT_NOTICIAS/jornal/AAAA-MM-DD_*  → cluster + rank da   →  abertura + 1 segmento    →  2 vozes (ALFRED/LUCIA),
    (.md + .json com e-mails/notícias)     semana inteira        de diálogo por tema         ffmpeg concatena
                                                                                                │
    VAULT_NOTICIAS/podcast/episodios/<AAAA-Www>_resumo-semanal.md + .mp3  ◄─────────────────────┘
    VAULT_NOTICIAS/podcast/manifestos/<AAAA-Www>.json   ← contrato com a limpeza do Thunderbird:
        {"semana", "episodio_md", "episodio_mp3", "gerado_em",
         "emails": [{"message_id", "from", "subject", "date", "pasta", "conta"}]}
    Este módulo nunca apaga e-mails; só lista os que entraram no episódio.

Configuração: perfis.yml → inbox.<nome>.podcast (vozes, max_temas, palavras_por_tema...).

    from semanal import gerar_semanal
    gerar_semanal(semana="2026-W43")
"""

import asyncio
import json
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
import core  # noqa: E402
from _shared.lib_alfred import log, ollama_generate, deduplicate_by_url_and_title  # noqa: E402
from _shared.lib_cluster import cluster_items, rank_clusters  # noqa: E402
from _shared.lib_mail import find_thunderbird_profile, list_accounts, fetch_messages  # noqa: E402
from _shared.lib_templates import render_template  # noqa: E402
from _shared.lib_titulo import generate_title_via_llm  # noqa: E402
from _shared.lib_tts import limpar_para_fala  # noqa: E402

VOZES_DEFAULT = {"ALFRED": "pt-BR-AntonioNeural", "LUCIA": "pt-BR-FranciscaNeural"}
_FALA = re.compile(r"^\s*[*_#>\-\s]*(ALFRED|L[UÚ]CIA)[*_\s]*:\s*[*_]*\s*(.+?)\s*$", re.I)


# ══════════════════════════════════════════════════════════════
#  Semana / caminhos
# ══════════════════════════════════════════════════════════════

def semana_atual(hoje: Optional[date] = None) -> str:
    """Semana ISO de hoje; na segunda-feira, a semana anterior (timer de domingo atrasado / Persistent=true)."""
    hoje = hoje or date.today()
    if hoje.weekday() == 0:
        hoje -= timedelta(days=1)
    y, w, _ = hoje.isocalendar()
    return f"{y}-W{w:02d}"


def intervalo_semana(semana: str) -> tuple[date, date]:
    m = re.fullmatch(r"(\d{4})-?W(\d{1,2})", semana.strip(), re.I)
    if not m:
        raise ValueError(f"Semana inválida: {semana!r} (use AAAA-Www, ex.: 2026-W43)")
    ini = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    return ini, ini + timedelta(days=6)


def intervalo_dias(pcfg: dict) -> int:
    try:
        return max(1, int(pcfg.get("intervalo_dias", 3)))
    except (TypeError, ValueError):
        return 3


def periodo_atual(dias: int, hoje: Optional[date] = None) -> tuple[str, date, date]:
    """Últimos `dias` dias terminando hoje → (id, início, fim)."""
    fim = hoje or date.today()
    ini = fim - timedelta(days=dias - 1)
    return f"{ini:%Y-%m-%d}_{fim:%Y-%m-%d}", ini, fim


def ultimo_episodio() -> Optional[dict]:
    """Manifesto mais recente (por gerado_em) ou None."""
    d = core.vault_noticias() / "podcast" / "manifestos"
    melhor = None
    for f in d.glob("*.json") if d.is_dir() else []:
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(m.get("gerado_em"))
        except Exception:
            continue
        if melhor is None or ts > melhor[0]:
            melhor = (ts, {**m, "_arquivo": str(f)})
    return melhor[1] if melhor else None


def episodio_devido(dias: int, agora: Optional[datetime] = None) -> tuple[bool, str]:
    """True se nunca houve episódio ou o último foi gerado há `dias` dias (de calendário) ou mais."""
    ult = ultimo_episodio()
    if not ult:
        return True, "nenhum episódio anterior"
    agora = agora or datetime.now().astimezone()
    gerado = datetime.fromisoformat(ult["gerado_em"])
    if gerado.tzinfo is None:
        gerado = gerado.astimezone()
    passados = (agora.date() - gerado.date()).days
    if passados >= dias:
        return True, f"último episódio há {passados} dia(s)"
    return False, f"último episódio há {passados} dia(s) (< {dias}) — {Path(ult['_arquivo']).stem}"


def podcast_dir(sub: str) -> Path:
    d = core.vault_noticias() / "podcast" / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


# ══════════════════════════════════════════════════════════════
#  Leitura das edições diárias
# ══════════════════════════════════════════════════════════════

_NEWS_LINHA = re.compile(r"^\s*-\s*\*\*(.+?)\*\*\s*—\s*(.+?)\s*\((\d{4}-\d{2}-\d{2}T[\d:]+)\)\s*$")
_ITEM_LINHA = re.compile(r"^\s*\d+\.\s*(?:\[(.+?)\]\((\S*?)\)|(.+?))\s*—\s*\*(.+?)\*\s*$")
_DESTAQUE = re.compile(r"\*\*(?:\[(.+?)\]\(\S*?\)|(.+?))\*\*\s*\n(.+?)\n\s*\*\((.+?)\)\*", re.S)


def _secao(texto: str, titulo: str) -> str:
    m = re.search(rf"^##\s*{re.escape(titulo)}\s*$(.*?)(?=^##\s|\Z)", texto, re.S | re.M)
    return m.group(1) if m else ""


def ler_edicao(md: Path) -> dict:
    """Edição diária → {arquivo, data, emails[], noticias[]}. Usa o .json irmão quando existir."""
    dia = md.name[:10]
    side = md.with_suffix(".json")
    if side.exists():
        try:
            d = json.loads(side.read_text(encoding="utf-8"))
            for it in d.get("noticias", []):
                it["_dia"] = dia
            return {"arquivo": md, "data": dia, "emails": d.get("emails", []), "noticias": d.get("noticias", [])}
        except Exception as e:
            log(f"[semanal] {side.name} ilegível ({e}) — lendo o .md")

    texto = md.read_text(encoding="utf-8")
    snippets = {}
    for mm in _DESTAQUE.finditer(texto):
        snippets[(mm.group(1) or mm.group(2)).strip().lower()] = mm.group(3).strip()
    emails = []
    for ln in _secao(texto, "Newsletters desta edição").splitlines():
        mm = _NEWS_LINHA.match(ln)
        if mm:
            emails.append({"message_id": None, "from_name": mm.group(1).strip(), "from": mm.group(1).strip(),
                           "subject": mm.group(2).strip(), "date": mm.group(3), "pasta": "", "conta": ""})
    noticias = []
    # jornal do e-mail: "Notícias extraídas"; briefing por RSS (services/briefing): "Fontes citadas"
    linhas = _secao(texto, "Notícias extraídas") or _secao(texto, "Fontes citadas") or _secao(texto, "Fontes coletadas")
    for ln in linhas.splitlines():
        mm = _ITEM_LINHA.match(ln)
        if not mm:
            continue
        titulo = (mm.group(1) or mm.group(3) or "").strip()
        noticias.append({"title": titulo, "url": (mm.group(2) or "").strip(),
                         "snippet": snippets.get(titulo.lower(), ""), "source": mm.group(4).strip(),
                         "published": dia, "email_id": None, "_dia": dia})
    return {"arquivo": md, "data": dia, "emails": emails, "noticias": noticias}


def edicoes_da_semana(ini: date, fim: date, incluir_briefings: bool = False) -> list[dict]:
    """Edições do jornal do e-mail no período; com `incluir_briefings`, também os briefings por RSS
    (VAULT_ALFRED/briefings/AAAA-MM-DD_*.md, gerados por services/briefing)."""
    vistos, out = set(), []
    pastas = [core.jornal_dir(), core.vault() / "jornal"] + ([core.vault() / "briefings"] if incluir_briefings else [])
    for d in pastas:
        if not d.is_dir():
            continue
        for md in sorted(d.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_*.md")):
            try:
                dia = date.fromisoformat(md.name[:10])
            except ValueError:
                continue
            if ini <= dia <= fim and md.name not in vistos:
                vistos.add(md.name)
                out.append(ler_edicao(md))
    return out


# ══════════════════════════════════════════════════════════════
#  Seleção dos temas da semana
# ══════════════════════════════════════════════════════════════

def selecionar_temas(itens: list[dict], pcfg: dict, pesos: dict) -> list[dict]:
    """Agrupa as notícias da semana por assunto e devolve os melhores temas.

    Score = (nº de notícias × peso médio das fontes) × (1 + 0,5 por dia extra em que o assunto apareceu)
    + bônus de 0,3 por fonte distinta — assunto recorrente e citado por várias newsletters sobe.
    """
    if not itens:
        return []
    clustered = cluster_items(itens)
    ordem = rank_clusters(clustered, pesos)
    por_cid = defaultdict(list)
    for it in clustered:
        por_cid[it.get("cluster_id", -1)].append(it)

    temas = []
    for pos, cid in enumerate(ordem):
        grupo = por_cid.get(cid) or []
        if not grupo:
            continue
        dias = sorted({it["_dia"] for it in grupo})
        fontes = sorted({it["source"] for it in grupo})
        w = sum(float(pesos.get(it["source"], 0.5)) for it in grupo) / len(grupo)
        score = len(grupo) * w * (1 + 0.5 * (len(dias) - 1)) + 0.3 * len(fontes) - 0.01 * pos
        ancora = max(grupo, key=lambda it: (bool(it.get("url")), len(it.get("snippet") or "")))
        temas.append({"tema": ancora["title"], "itens": grupo, "dias": dias, "fontes": fontes, "score": score})
    temas.sort(key=lambda t: -t["score"])
    return temas[: int(pcfg.get("max_temas", 7))]


# ══════════════════════════════════════════════════════════════
#  Roteiro (diálogo)
# ══════════════════════════════════════════════════════════════

def parse_dialogo(texto: str) -> list[tuple[str, str]]:
    falas = []
    for ln in (texto or "").splitlines():
        m = _FALA.match(ln)
        if m:
            quem = "ALFRED" if m.group(1).upper() == "ALFRED" else "LUCIA"
            fala = re.sub(r"https?://\S+", "o link está nas notas do episódio", m.group(2)).replace("**", "").strip()
            if fala:
                falas.append((quem, fala))
        elif falas and ln.strip() and not ln.strip().startswith(("#", "---", "(", "[")):
            falas[-1] = (falas[-1][0], falas[-1][1] + " " + ln.strip())  # fala quebrada em duas linhas
    return falas


def _fallback(texto: str, quem: str = "ALFRED") -> list[tuple[str, str]]:
    limpo = limpar_para_fala(texto or "").strip()
    return [(quem, p.strip()) for p in re.split(r"\n\s*\n", limpo) if p.strip()]


def _dia_humano(iso: str) -> str:
    nomes = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    d = date.fromisoformat(iso)
    return f"{nomes[d.weekday()]} ({d.strftime('%d/%m')})"


def roteiro_segmento(tema: dict, n: int, total: int, semana_humana: str, pcfg: dict, perfil: dict) -> list[tuple[str, str]]:
    noticias = "\n".join(
        f"- {it['title']} ({it['source']}, {_dia_humano(it['_dia'])})" + (f": {it['snippet']}" if it.get("snippet") else "")
        for it in tema["itens"][:6])
    prompt = render_template(pcfg.get("template_segmento", "podcast_semanal_segmento"), {
        "semana": semana_humana, "numero": n, "total": total, "tema": tema["tema"], "noticias": noticias,
        "fontes": ", ".join(tema["fontes"]), "dias": ", ".join(_dia_humano(d) for d in tema["dias"]),
        "recorrencia": (f'O assunto apareceu em {len(tema["dias"])} dias diferentes: comente que ele "rendeu" nesses dias.'
                        if len(tema["dias"]) > 1 else "Apareceu uma vez só: não diga que o assunto rendeu ou se repetiu."),
        "palavras": int(pcfg.get("palavras_por_tema", 220)),
        "interesses": ", ".join(perfil.get("interesses_quentes", [])) or "tecnologia",
    })
    texto = ollama_generate(prompt, timeout=300) or ""
    falas = parse_dialogo(texto)
    if len(falas) < 2:
        log(f"[semanal] Segmento {n}: diálogo não reconhecido — narração simples")
        falas = _fallback(texto) or [("ALFRED", f"{tema['tema']}. " + " ".join(
            it.get("snippet") or it["title"] for it in tema["itens"][:3]))]
    return falas


def roteiro_moldura(temas: list[dict], semana_humana: str, periodo: str, n_noticias: int,
                    fontes: list[str], pcfg: dict) -> tuple[list, list]:
    prompt = render_template(pcfg.get("template_moldura", "podcast_semanal_moldura"), {
        "semana": semana_humana, "periodo": periodo, "n_noticias": n_noticias, "n_newsletters": len(fontes),
        "fontes": ", ".join(fontes[:12]), "temas": "\n".join(f"{i}. {t['tema']}" for i, t in enumerate(temas, 1)),
    })
    texto = ollama_generate(prompt, timeout=240) or ""
    partes = re.split(r"^\s*-{3,}\s*$", texto, maxsplit=1, flags=re.M)
    abertura = parse_dialogo(partes[0])
    fecho = parse_dialogo(partes[1]) if len(partes) > 1 else []
    if not abertura:
        abertura = [("ALFRED", f"Bem-vindo ao Resumo do Alfred, {semana_humana}. Comigo, como sempre, a Lúcia."),
                    ("LUCIA", f"Olá! Hoje são {len(temas)} temas, tirados de {n_noticias} notícias das suas newsletters.")]
    if not fecho:
        fecho = [("LUCIA", "Os links de tudo que falamos estão nas notas do episódio, no Obsidian."),
                 ("ALFRED", "Até o próximo episódio.")]
    return abertura, fecho


# ══════════════════════════════════════════════════════════════
#  Áudio (edge-tts, 2 vozes + ffmpeg)
# ══════════════════════════════════════════════════════════════

async def _tts_falas(falas: list[tuple[str, str]], vozes: dict, tmp: Path, paralelo: int = 4) -> list[Path]:
    import edge_tts
    sem = asyncio.Semaphore(paralelo)

    async def um(i: int, quem: str, texto: str) -> Optional[Path]:
        out = tmp / f"fala_{i:03d}.mp3"
        async with sem:
            for tentativa in range(3):
                try:
                    await edge_tts.Communicate(texto, vozes.get(quem) or VOZES_DEFAULT[quem]).save(str(out))
                    if out.exists() and out.stat().st_size > 0:
                        return out
                except Exception as e:
                    log(f"[semanal] TTS fala {i} tentativa {tentativa + 1}: {e}")
                    await asyncio.sleep(2 * (tentativa + 1))
        return None

    res = await asyncio.gather(*(um(i, q, t) for i, (q, t) in enumerate(falas)))
    return [p for p in res if p]


def _ffmpeg(*args: str) -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"[semanal] ffmpeg: {r.stderr.strip()[:300]}")
    return r.returncode == 0


def duracao_s(mp3: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip() or 0)
    except Exception:
        return 0.0


def gerar_audio(falas: list[tuple[str, str]], vozes: dict, destino: Path, pausa_ms: int = 350,
                vinheta: str = "") -> bool:
    with tempfile.TemporaryDirectory(prefix="alfred_semanal_") as td:
        tmp = Path(td)
        partes = asyncio.run(_tts_falas(falas, vozes, tmp))
        if not partes:
            log("[semanal] Nenhuma fala sintetizada")
            return False
        if len(partes) < len(falas):
            log(f"[semanal] {len(falas) - len(partes)} fala(s) falharam no TTS — seguindo sem elas")
        silencio = tmp / "silencio.mp3"
        _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", f"{pausa_ms / 1000:.3f}",
                "-c:a", "libmp3lame", "-b:a", "48k", str(silencio))
        ordem: list[Path] = []
        vin = None
        if vinheta and Path(vinheta).expanduser().exists():
            vin = tmp / "vinheta.mp3"
            if not _ffmpeg("-i", str(Path(vinheta).expanduser()), "-ar", "24000", "-ac", "1",
                           "-c:a", "libmp3lame", "-b:a", "48k", str(vin)):
                vin = None
        if vin:
            ordem += [vin, silencio]
        for p in partes:
            ordem += [p, silencio]
        if vin:
            ordem.append(vin)
        lista = tmp / "lista.txt"
        lista.write_text("".join(f"file '{p}'\n" for p in ordem), encoding="utf-8")
        destino.parent.mkdir(parents=True, exist_ok=True)
        return _ffmpeg("-f", "concat", "-safe", "0", "-i", str(lista), "-ar", "24000", "-ac", "1",
                       "-c:a", "libmp3lame", "-b:a", "64k", str(destino))


# ══════════════════════════════════════════════════════════════
#  E-mails do episódio (manifesto para a limpeza do Thunderbird)
# ══════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def resolver_emails(temas: list[dict], edicoes: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    """E-mails cujas notícias entraram no episódio → (resolvidos com message_id, não resolvidos).

    Edições novas têm o .json com message_id/pasta/conta. Nas antigas (só .md) o e-mail é achado
    procurando no Thunderbird pelo assunto + remetente listados na edição.
    """
    usados_ids, usados_fontes = set(), defaultdict(set)   # fontes por dia (edições sem email_id)
    for t in temas:
        for it in t["itens"]:
            if it.get("email_id"):
                usados_ids.add(it["email_id"])
            else:
                usados_fontes[it["_dia"]].add(_norm(it["source"]))

    resolvidos: dict[str, dict] = {}
    pendentes: list[dict] = []
    for ed in edicoes:
        for e in ed["emails"]:
            mid = e.get("message_id")
            if mid and mid in usados_ids:
                resolvidos[mid] = e
            elif not mid and _norm(e.get("from_name") or e.get("from")) in usados_fontes.get(ed["data"], set()):
                pendentes.append(e)

    nao_resolvidos = []
    if pendentes:
        msgs = _buscar_no_thunderbird(pendentes, cfg)
        for e in pendentes:
            achou = next((m for m in msgs if _norm(m["subject"]) == _norm(e["subject"])
                          and _norm(m["from_name"] or m["from_domain"]) == _norm(e["from_name"])), None)
            if achou:
                resolvidos[achou["message_id"]] = {
                    "message_id": achou["message_id"], "from": f"{achou['from_name']} <{achou['from_addr']}>",
                    "subject": achou["subject"], "date": achou["date_iso"],
                    "pasta": achou.get("folder", ""), "conta": achou.get("account", "")}
            else:
                nao_resolvidos.append({k: e.get(k) for k in ("from", "subject", "date")})

    emails = [{k: e.get(k, "") for k in ("message_id", "from", "subject", "date", "pasta", "conta")}
              for e in resolvidos.values()]
    emails.sort(key=lambda e: e["date"] or "")
    return emails, nao_resolvidos


def _buscar_no_thunderbird(pendentes: list[dict], cfg: dict) -> list[dict]:
    if (cfg.get("backend") or "thunderbird").lower() != "thunderbird":
        return []
    profile = find_thunderbird_profile()
    accounts = list_accounts(profile) if profile else []
    if not accounts:
        log("[semanal] Thunderbird indisponível — e-mails das edições antigas ficam sem message_id")
        return []
    datas = [datetime.fromisoformat(e["date"]) for e in pendentes if e.get("date")]
    desde = (min(datas) if datas else datetime.now()) - timedelta(days=1)
    if desde.tzinfo is None:
        desde = desde.astimezone()
    log(f"[semanal] Procurando {len(pendentes)} newsletter(s) no Thunderbird desde {desde:%d/%m}...")
    return fetch_messages(accounts, pastas=cfg.get("pastas") or ["INBOX"], desde=desde,
                          conta=cfg.get("conta", "") or "", max_total=5000, incluir_lidos=True)


# ══════════════════════════════════════════════════════════════
#  Episódio
# ══════════════════════════════════════════════════════════════

def gerar_semanal(semana: Optional[str] = None, inbox_name: Optional[str] = None, com_audio: bool = True,
                  dry_run: bool = False, dias: Optional[int] = None, hoje: Optional[date] = None) -> Optional[dict]:
    """
    `semana` ("AAAA-Www") → modo antigo, semana ISO. Sem `semana` → últimos `dias` dias (padrão
    perfis.yml podcast.intervalo_dias = 3) terminando `hoje`. O id do período nomeia .md/.mp3/manifesto.
    """
    nome, cfg = core.carregar_cfg(inbox_name)
    pcfg = cfg.get("podcast") or {}
    perfil = cfg.get("_perfil") or {}
    if semana:
        ini, fim = intervalo_semana(semana)
        _, num, _ = ini.isocalendar()
        semana_humana = f"semana {num} de {ini.year}"
        sufixo = "resumo-semanal"
    else:
        semana, ini, fim = periodo_atual(dias or intervalo_dias(pcfg), hoje)
        semana_humana = (f"edição de {ini:%d/%m} a {fim:%d/%m}" if ini != fim else f"edição de {fim:%d/%m}")
        sufixo = "resumo-do-alfred"
    periodo = f"{ini:%d/%m} a {fim:%d/%m/%Y}"

    edicoes = edicoes_da_semana(ini, fim, incluir_briefings=bool(pcfg.get("incluir_briefings", True)))
    itens = deduplicate_by_url_and_title([it for ed in edicoes for it in ed["noticias"]])
    log(f"[semanal] {semana}: {len(edicoes)} edição(ões), {len(itens)} notícia(s) únicas")
    if len(itens) < int(pcfg.get("min_noticias", 3)):
        log("[semanal] Notícias insuficientes — episódio não gerado")
        return None

    pesos = {str(k): float(v) for k, v in ((cfg.get("jornal") or {}).get("pesos_fontes") or {}).items()}
    temas = selecionar_temas(itens, pcfg, pesos)
    fontes = sorted({it["source"] for it in itens})
    if dry_run:
        return {"semana": semana, "periodo": periodo, "edicoes": [str(e["arquivo"]) for e in edicoes],
                "temas": [{"tema": t["tema"], "score": round(t["score"], 2), "dias": t["dias"], "fontes": t["fontes"],
                           "noticias": [it["title"] for it in t["itens"]]} for t in temas]}

    log(f"[semanal] Roteiro: abertura + {len(temas)} segmento(s) + encerramento")
    abertura, fecho = roteiro_moldura(temas, semana_humana, periodo, len(itens), fontes, pcfg)
    segmentos = [roteiro_segmento(t, i, len(temas), semana_humana, pcfg, perfil) for i, t in enumerate(temas, 1)]
    falas = abertura + [f for seg in segmentos for f in seg] + fecho

    titulo = generate_title_via_llm("\n".join(t["tema"] for t in temas),
                                    fallback=f"Resumo do Alfred — {semana_humana}")
    base = f"{semana}_{sufixo}"
    ep_dir = podcast_dir("episodios")
    md_path, mp3_path = ep_dir / f"{base}.md", ep_dir / f"{base}.mp3"

    audio_ok, dur = False, 0.0
    if com_audio and "audio" in (pcfg.get("formato") or ["markdown", "audio"]):
        log(f"[semanal] Sintetizando {len(falas)} falas em 2 vozes...")
        vozes = {**VOZES_DEFAULT, **{str(k).upper(): v for k, v in (pcfg.get("vozes") or {}).items()}}
        audio_ok = gerar_audio(falas, vozes, mp3_path, int(pcfg.get("pausa_ms", 350)), pcfg.get("vinheta", ""))
        dur = duracao_s(mp3_path) if audio_ok else 0.0
        log(f"[semanal] Áudio: {mp3_path.name} ({dur / 60:.1f} min)" if audio_ok else "[semanal] Áudio falhou")

    emails, nao_resolvidos = resolver_emails(temas, edicoes, cfg)
    agora = core._now().isoformat(timespec="seconds")
    palavras = sum(len(t.split()) for _, t in falas)

    md = ["---", f'title: "{titulo}"', f"semana: {semana}", f'periodo: "{periodo}"', "type: podcast",
          f"inbox: {nome}", f"edicoes: {len(edicoes)}", f"noticias: {len(itens)}", f"temas: {len(temas)}",
          f"emails: {len(emails)}", f"palavras: {palavras}",
          f"duracao_min: {dur / 60:.1f}" if audio_ok else "duracao_min: 0", f"gerado_em: {agora}",
          f"tags: [podcast, {sufixo}]", "---", "",
          f"# {titulo}", "",
          f"> Resumo do Alfred · {semana_humana} ({periodo}) · {len(temas)} temas de {len(itens)} notícias"
          f" em {len(edicoes)} edição(ões) do jornal" + (f" · {dur / 60:.0f} min" if audio_ok else ""), ""]
    if audio_ok:
        md += [f"![[{mp3_path.name}]]", ""]
    md += ["## Temas do episódio", ""]
    for i, t in enumerate(temas, 1):
        md.append(f"### {i}. {t['tema']}")
        md.append(f"*{', '.join(t['fontes'])} · {', '.join(_dia_humano(d) for d in t['dias'])}*")
        md.append("")
        for it in t["itens"]:
            md.append(f"- [{it['title']}]({it['url']})" if it.get("url") else f"- {it['title']}")
        md.append("")
    md += ["## Edições do jornal usadas", ""] + [f"- [[{e['arquivo'].stem}]]" for e in edicoes] + [""]
    md += ["## Transcrição", ""] + [f"**{'Alfred' if q == 'ALFRED' else 'Lúcia'}:** {t}  " for q, t in falas] + [""]
    md_path.write_text("\n".join(md), encoding="utf-8")
    log(f"[semanal] Markdown: {md_path}")

    manifesto = {
        "semana": semana,                     # id do período (AAAA-Www ou AAAA-MM-DD_AAAA-MM-DD)
        "periodo_inicio": ini.isoformat(),
        "periodo_fim": fim.isoformat(),
        "episodio_md": str(md_path),
        "episodio_mp3": str(mp3_path) if audio_ok else None,
        "gerado_em": agora,
        "emails": emails,
    }
    if nao_resolvidos:
        manifesto["nao_resolvidos"] = nao_resolvidos   # sem message_id: a limpeza deve ignorá-los
    man_path = podcast_dir("manifestos") / f"{semana}.json"
    man_path.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[semanal] Manifesto: {man_path} ({len(emails)} e-mail(s), {len(nao_resolvidos)} sem message_id)")

    core._notify("Alfred — Resumo do Alfred (podcast)", f"{titulo}\n{len(temas)} temas" + (f" · {dur / 60:.0f} min" if audio_ok else ""))
    return {"semana": semana, "titulo": titulo, "md": str(md_path), "mp3": str(mp3_path) if audio_ok else None,
            "manifesto": str(man_path), "duracao_s": round(dur), "temas": len(temas), "noticias": len(itens),
            "edicoes": len(edicoes), "emails": len(emails), "falas": len(falas), "palavras": palavras}


def cmd_semanal(args) -> int:
    if getattr(args, "se_devido", False) and not args.semana:
        _, cfg = core.carregar_cfg(args.inbox)
        n = getattr(args, "dias", None) or intervalo_dias(cfg.get("podcast") or {})
        devido, motivo = episodio_devido(n)
        log(f"[podcast] {'gerando' if devido else 'ainda não'}: {motivo}")
        if not devido:
            return 0
    res = gerar_semanal(semana=args.semana, inbox_name=args.inbox, com_audio=not args.sem_audio,
                        dry_run=args.dry_run, dias=getattr(args, "dias", None))
    if not res:
        return 0 if getattr(args, "se_devido", False) else 1
    if args.json or args.dry_run:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"EPISODIO_FILE={res['md']}")
    if res.get("mp3"):
        print(f"EPISODIO_AUDIO={res['mp3']}")
    print(f"MANIFESTO={res['manifesto']}")
    return 0
