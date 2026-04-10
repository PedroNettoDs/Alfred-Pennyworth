#!/usr/bin/env python3
"""
Alfred Tech Briefing v3.2 — templates, título via Ollama, modo ad-hoc.

Uso:
    python tech-briefing.py                    → default matinal_tech
    python tech-briefing.py matinal_tech       → briefing nomeado
    python tech-briefing.py vespertino_mercado → briefing nomeado
    python tech-briefing.py "fusão nuclear"    → modo ad-hoc (tema livre)
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
import yaml

# ── Shared libs ───────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared.lib_alfred import (
    log, slugify, ollama_generate, searxng_search,
    deduplicate_by_url_and_title, vault_write,
)
from _shared.perfis import get_briefing, list_all
from _shared.lib_templates import render_template

import edge_tts

SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
CONFIG_FILE  = SCRIPT_DIR / "config.yml"
VAULT_ALFRED = Path(os.getenv("VAULT_ALFRED", ""))

SEEN_FILE         = PROJECT_ROOT / "logs" / "briefing_seen.json"
SEEN_WINDOW_HOURS = 24


# ── Config ────────────────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Extração de título — heurística (fallback) ────────────────
# Mantida como fallback interno de generate_title_via_llm. NÃO REMOVER.
NOMES_SECAO = {
    "manchete do dia", "manchete", "destaques", "destaque",
    "tendências", "tendencias", "vale ficar de olho",
    "leitura recomendada", "leituras recomendadas",
    "análise", "analise", "resumo", "briefing",
    "tech briefing", "briefing matinal", "briefing de tecnologia",
    "notícias", "noticias", "headlines", "top stories",
    "sobre o tema", "pontos principais", "conexões interessantes",
    "para saber mais", "panorama do dia", "movimentos relevantes",
    "oportunidades sinalizadas", "para acompanhar",
}


def extrair_titulo_manchete(briefing: str) -> str:
    """Extrai título por heurística. Usado como fallback do generate_title_via_llm."""
    lines = briefing.strip().split("\n")

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        clean = re.sub(r"[*_\[\]()\"']", "", line).strip()
        if len(clean) < 10:
            continue
        if clean.lower().rstrip(":").strip() in NOMES_SECAO:
            continue
        if clean.startswith(">") or clean.startswith("-"):
            continue
        if ". " in clean:
            clean = clean[:clean.index(". ")]
        elif clean.endswith("."):
            clean = clean[:-1]
        return clean[:100]

    encontrou_manchete = False
    for line in lines:
        line = line.strip()
        if re.match(r"^#+\s*manchete", line, re.IGNORECASE):
            encontrou_manchete = True
            continue
        if encontrou_manchete and line:
            clean = re.sub(r"[*_\[\]()\"']", "", line).strip()
            if len(clean) > 10 and clean.lower().rstrip(":") not in NOMES_SECAO:
                if ". " in clean:
                    clean = clean[:clean.index(". ")]
                return clean[:100]

    return "Tech Briefing"


# ── Título via Ollama (JSON) ───────────────────────────────────
def generate_title_via_llm(briefing_md: str) -> str:
    """
    Segunda chamada Ollama: extrai título do briefing como JSON.
    Fallback pra extrair_titulo_manchete() em caso de falha no parse.
    """
    prompt = (
        "Leia o briefing abaixo e extraia apenas o título principal (manchete) "
        "da notícia ou tema mais importante. Responda em JSON válido:\n\n"
        '{"titulo": "título curto aqui, sem markdown"}\n\n'
        "Regras:\n"
        "- Máximo 80 caracteres\n"
        "- Sem markdown, sem aspas extras, sem prefixos\n"
        "- Em português brasileiro\n"
        "- Deve ser uma frase informativa e específica\n\n"
        f"BRIEFING:\n{briefing_md[:2000]}"
    )

    raw = ollama_generate(prompt, timeout=60)
    try:
        match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            titulo = (data.get("titulo") or "").strip()
            if titulo and len(titulo) > 5:
                log(f"[titulo] Via JSON: {titulo[:60]}")
                return titulo[:100]
    except Exception as e:
        log(f"[titulo] Falha no parse JSON ({e}) — usando fallback heurístico")

    log("[titulo] Usando fallback extrair_titulo_manchete()")
    return extrair_titulo_manchete(briefing_md)


# ── RSS ───────────────────────────────────────────────────────
def fetch_rss(feed_config: dict) -> list[dict]:
    name      = feed_config.get("name") or feed_config.get("nome") or "RSS"
    url       = feed_config.get("url", "")
    max_items = feed_config.get("max_items") or feed_config.get("max") or 5

    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            summary = entry.get("summary", entry.get("description", ""))
            summary = re.sub(r"<[^>]+>", "", summary)[:400]
            items.append({
                "title":   entry.get("title", ""),
                "url":     entry.get("link", ""),
                "snippet": summary,
                "source":  name,
            })
        log(f"[RSS] {name}: {len(items)} itens")
        return items
    except Exception as e:
        log(f"[RSS] Erro em {name}: {e}")
        return []


# ── TTS ───────────────────────────────────────────────────────
async def generate_tts(text: str, voice: str, output_path: Path):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))


# ── Memória deslizante de 24h ─────────────────────────────────

def load_seen() -> dict:
    """
    Carrega logs/briefing_seen.json, purgando entradas com mais de SEEN_WINDOW_HOURS.
    Retorna dict {url: iso_timestamp}. Em caso de erro ou arquivo inexistente, retorna {}.
    """
    if not SEEN_FILE.exists():
        return {}

    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log(f"[seen] Erro lendo {SEEN_FILE}: {e} — começando vazio")
        return {}

    if not isinstance(data, dict):
        log(f"[seen] {SEEN_FILE} não é um dict — começando vazio")
        return {}

    cutoff = datetime.now() - timedelta(hours=SEEN_WINDOW_HOURS)
    purged = {}
    for url, ts in data.items():
        try:
            if datetime.fromisoformat(ts) > cutoff:
                purged[url] = ts
        except (ValueError, TypeError):
            continue  # ignora entradas com timestamp malformado

    removed = len(data) - len(purged)
    if removed > 0:
        log(f"[seen] Purgado {removed} entradas > {SEEN_WINDOW_HOURS}h")

    return purged


def save_seen(seen: dict):
    """Persiste dict em logs/briefing_seen.json. Cria diretório se não existir."""
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)


def split_new_vs_continuation(
    items: list[dict], seen: dict
) -> tuple[list[dict], list[dict]]:
    """
    Separa items em (novas, continuacoes) com base no seen dict.
    URL é a chave. Itens sem URL são tratados como novas.
    """
    novas, continuacoes = [], []
    for item in items:
        url = item.get("url", "")
        if not url or url not in seen:
            novas.append(item)
        else:
            continuacoes.append(item)
    return novas, continuacoes


def mark_seen(items: list[dict], seen: dict):
    """Adiciona/atualiza URLs dos items no dict seen com timestamp ISO atual."""
    now_iso = datetime.now().isoformat()
    for item in items:
        url = item.get("url", "")
        if url:
            seen[url] = now_iso


def format_items_long(items: list[dict]) -> str:
    """Formato longo pras notícias novas — título + fonte + URL + snippet."""
    if not items:
        return ""
    return "\n\n".join(
        f"[{i+1}] {item.get('title', '')}\n"
        f"Fonte: {item.get('source', '')}\n"
        f"URL: {item.get('url', '')}\n"
        f"{item.get('snippet', '')}"
        for i, item in enumerate(items)
    )


def format_items_short(items: list[dict]) -> str:
    """Formato curto pras continuações — uma linha por item."""
    if not items:
        return ""
    return "\n".join(
        f"- [{item.get('title', 'sem título')}]({item.get('url', '')}) — {item.get('source', '')}"
        for item in items
    )


def strip_acompanhamento_section(text: str) -> str:
    """
    Remove programaticamente qualquer variação da seção Acompanhamento:
      ## Acompanhamento, **ACOMPANHAMENTO**, **Acompanhamento**, etc.
    Usada quando news_text_continuacoes está vazio.
    """
    # Padrão largo: qualquer linha que seja só o header de Acompanhamento
    # seguida pelo conteúdo até o próximo header markdown/bold ou fim
    return re.sub(
        r"\n?(?:#{1,3}\s*|(?:\*{1,2})?)Acompanhamento\b(?:\*{1,2})?"
        r".*?(?=\n#{1,3}\s|\n\*{1,2}[A-Z]|\Z)",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()


# ── Fluxo briefing nomeado ────────────────────────────────────
def _run_briefing_nomeado(briefing_cfg: dict, briefing_name: str):
    perfil = briefing_cfg["_perfil"]
    fontes = briefing_cfg["_fontes"]

    today      = datetime.now()
    date_str   = today.strftime("%Y-%m-%d")
    time_str   = today.strftime("%H:%M")
    date_human = today.strftime("%d/%m/%Y")
    timestamp  = today.strftime("%Y-%m-%d_%H%M")

    log(f"=== Briefing '{briefing_name}' (perfil: {briefing_cfg['perfil']}) — {date_human} {time_str} ===")

    config = load_config()

    # ── Coletar ───────────────────────────────────────────────
    all_items: list[dict] = []
    for fonte in fontes:
        tipo = fonte.get("tipo", "")
        if tipo == "searxng":
            for q in fonte.get("queries", []):
                results = searxng_search(q["q"], categories=q.get("cat", "news,it"), max_results=10)
                all_items.extend(results)
                log(f"[SearXNG] '{q['q']}': {len(results)} resultados")
        elif tipo == "rss":
            for feed in fonte.get("feeds", []):
                all_items.extend(fetch_rss(feed))
        else:
            log(f"[AVISO] Tipo de fonte desconhecido: {tipo!r} — pulando")

    log(f"[Total] {len(all_items)} itens coletados")

    max_items = briefing_cfg.get("max_items", config.get("max_items", 15))
    unique = deduplicate_by_url_and_title(all_items)[: max_items * 2]
    log(f"[Dedup] {len(unique)} itens únicos")

    if not unique:
        log("[ERRO] Nenhuma notícia coletada. Abortando.")
        sys.exit(1)

    # ── Memória 24h — separar novas de continuações ───────────
    seen = load_seen()
    novas, continuacoes = split_new_vs_continuation(unique, seen)
    log(f"[Seen] {len(novas)} novas, {len(continuacoes)} continuações")

    if not novas:
        log("[AVISO] Todas as notícias são continuações — briefing focará em acompanhamento")

    news_text_novas        = format_items_long(novas[:20])
    news_text_continuacoes = format_items_short(continuacoes[:15])

    # ── Síntese via template ──────────────────────────────────
    template_name = briefing_cfg.get("template_prompt")
    if not template_name:
        log(f"[ERRO] Briefing '{briefing_name}' sem template_prompt definido no perfis.yml")
        sys.exit(1)

    # Dict com todas as variáveis possíveis — cada template puxa o que declara
    template_vars = {
        "data":                    date_human,
        "perfil_descricao":        perfil.get("descricao", ""),
        "interesses_quentes":      ", ".join(perfil.get("interesses_quentes", [])),
        "interesses_mornos":       ", ".join(perfil.get("interesses_mornos", [])),
        "excluir":                 ", ".join(perfil.get("excluir", [])),
        "area":                    perfil.get("area", ""),
        "palavras_chave":          ", ".join(perfil.get("palavras_chave", [])),
        "tom":                     perfil.get("tom", ""),
        "news_text":               news_text_novas,
        "news_text_continuacoes":  news_text_continuacoes,
    }

    synthesis_prompt = render_template(template_name, template_vars)

    log("[Ollama] Gerando briefing escrito...")
    briefing_text = ollama_generate(synthesis_prompt, timeout=300)
    if not briefing_text:
        log("[ERRO] Ollama não retornou síntese. Abortando.")
        sys.exit(1)
    log(f"[Ollama] Briefing gerado ({len(briefing_text)} chars)")

    # Garante omissão da seção Acompanhamento quando não há continuações
    if not news_text_continuacoes:
        original_len = len(briefing_text)
        briefing_text = strip_acompanhamento_section(briefing_text)
        if len(briefing_text) < original_len:
            log("[seen] Seção 'Acompanhamento' removida (sem continuações)")

    # ── Título via LLM ────────────────────────────────────────
    log("[Ollama] Extraindo título...")
    titulo_manchete = generate_title_via_llm(briefing_text)
    titulo_slug     = slugify(titulo_manchete)
    filename_base   = f"{timestamp}_{briefing_name}_{titulo_slug}" if titulo_slug else f"{timestamp}_{briefing_name}"

    log(f"[Título] {titulo_manchete}")
    log(f"[Slug]   {titulo_slug}")

    # ── Salvar markdown ───────────────────────────────────────
    briefing_dir = VAULT_ALFRED / "briefings"
    briefing_dir.mkdir(parents=True, exist_ok=True)

    # Templates instruem "não coloque título na primeira linha" — usar direto
    briefing_body = briefing_text

    md_content = "\n".join([
        "---",
        f'title: "{titulo_manchete}"',
        f"date: {date_str}",
        f'time: "{time_str}"',
        f"briefing: {briefing_name}",
        f"perfil: {briefing_cfg['perfil']}",
        "type: briefing",
        f"sources: {len(unique)}",
        f"tags: [briefing, {briefing_name}]",
        "---",
        "",
        f"# {titulo_manchete}",
        "",
        f"> {briefing_name} · {date_human} às {time_str} · {len(unique)} fontes",
        "",
        briefing_body,
        "",
        "---",
        "",
        "## Fontes coletadas",
        "",
    ])
    for i, item in enumerate(unique[:20], 1):
        md_content += f"{i}. [{item['title']}]({item['url']}) — *{item['source']}*\n"

    md_path = briefing_dir / f"{filename_base}.md"
    md_path.write_text(md_content, encoding="utf-8")
    log(f"[Vault] Markdown salvo: {md_path.name}")

    # ── Áudio via template ────────────────────────────────────
    mp3_path = None
    if "audio" in briefing_cfg.get("formato", []):
        audio_template_name = briefing_cfg.get("template_audio")
        if not audio_template_name:
            log(f"[ERRO] Briefing '{briefing_name}' sem template_audio definido no perfis.yml")
            sys.exit(1)

        log("[Ollama] Gerando versão para áudio...")
        audio_prompt = render_template(audio_template_name, {"briefing": briefing_text})
        audio_text   = ollama_generate(audio_prompt, timeout=180)

        if not audio_text:
            log("[AVISO] Falha na versão áudio, usando texto original limpo.")
            audio_text = re.sub(r"[#*\[\]\(\)]", "", briefing_text)

        voice    = briefing_cfg.get("tts_voice") or config.get("tts_voice", "pt-BR-FranciscaNeural")
        mp3_path = briefing_dir / f"{filename_base}.mp3"
        log(f"[TTS] Gerando áudio com voz {voice}...")
        asyncio.run(generate_tts(audio_text, voice, mp3_path))
        log(f"[TTS] Áudio salvo: {mp3_path.name}")
    else:
        log("[Áudio] Formato não inclui áudio — pulando TTS.")

    # ── Notificação ───────────────────────────────────────────
    audio_nota = "Áudio disponível." if mp3_path else "Somente markdown."
    try:
        subprocess.run([
            "notify-send", "--icon=dialog-information", "--urgency=normal",
            f"Alfred — {briefing_name}",
            f"{titulo_manchete}\n{len(unique)} fontes · {audio_nota}",
        ], timeout=5, capture_output=True)
    except Exception:
        pass

    # ── Persistir memória ─────────────────────────────────────
    mark_seen(unique, seen)
    save_seen(seen)
    log(f"[Seen] Salvo: {len(seen)} URLs na janela de {SEEN_WINDOW_HOURS}h")

    log("=== Briefing concluído ===")
    log(f"  Briefing: {briefing_name}")
    log(f"  Título:   {titulo_manchete}")
    log(f"  Markdown: {md_path.name}")
    if mp3_path:
        log(f"  Áudio:    {mp3_path.name}")

    print(f"BRIEFING_FILE={md_path}")


# ── Fluxo ad-hoc ─────────────────────────────────────────────
def _run_adhoc(tema: str):
    """Trata argumento como tema livre: SearXNG → síntese neutra → vault + áudio."""
    config = load_config()
    today      = datetime.now()
    date_str   = today.strftime("%Y-%m-%d")
    time_str   = today.strftime("%H:%M")
    date_human = today.strftime("%d/%m/%Y")
    timestamp  = today.strftime("%Y-%m-%d_%H%M")

    slug = slugify(tema)

    log(f"=== Modo ad-hoc — tema: {tema!r} ===")
    log(f"[ad-hoc] slug: {slug}")

    # ── Coletar ───────────────────────────────────────────────
    results = searxng_search(tema, categories="general,news", max_results=15)
    log(f"[SearXNG] '{tema}': {len(results)} resultados")

    if not results:
        log(f"[ERRO] Nenhum resultado encontrado para '{tema}'. Abortando.")
        sys.exit(1)

    unique = deduplicate_by_url_and_title(results)
    log(f"[Dedup] {len(unique)} itens únicos")

    # ── Síntese via template ──────────────────────────────────
    news_text = "\n\n".join(
        f"[{i+1}] {item['title']}\nFonte: {item['source']}\nURL: {item['url']}\n{item['snippet']}"
        for i, item in enumerate(unique[:15])
    )
    synthesis_prompt = render_template("briefing_adhoc", {
        "data":      date_human,
        "tema":      tema,
        "news_text": news_text,
    })

    log("[Ollama] Gerando resumo ad-hoc...")
    resumo = ollama_generate(synthesis_prompt, timeout=300)
    if not resumo:
        log("[ERRO] Ollama não retornou síntese. Abortando.")
        sys.exit(1)
    log(f"[Ollama] Resumo gerado ({len(resumo)} chars)")

    # ── Título via LLM ────────────────────────────────────────
    log("[Ollama] Extraindo título...")
    titulo = generate_title_via_llm(resumo)
    if not titulo or titulo == "Tech Briefing":
        titulo = tema.title()
    filename_base = f"{timestamp}_adhoc_{slug}"

    log(f"[Título] {titulo}")
    log(f"[Arquivo] {filename_base}")

    # ── Salvar markdown ───────────────────────────────────────
    briefing_dir = VAULT_ALFRED / "briefings"
    briefing_dir.mkdir(parents=True, exist_ok=True)

    md_content = "\n".join([
        "---",
        f'title: "{titulo}"',
        f"date: {date_str}",
        f'time: "{time_str}"',
        "type: briefing",
        "mode: adhoc",
        f'tema: "{tema}"',
        f"sources: {len(unique)}",
        "tags: [briefing, adhoc]",
        "---",
        "",
        f"# {titulo}",
        "",
        f"> Resumo ad-hoc sobre *{tema}* — {date_human} às {time_str} · {len(unique)} fontes",
        "",
        resumo,
        "",
        "---",
        "",
        "## Fontes coletadas",
        "",
    ])
    for i, item in enumerate(unique[:15], 1):
        md_content += f"{i}. [{item['title']}]({item['url']}) — *{item['source']}*\n"

    md_path = briefing_dir / f"{filename_base}.md"
    md_path.write_text(md_content, encoding="utf-8")
    log(f"[Vault] Markdown salvo: {md_path}")

    # ── Áudio via template ────────────────────────────────────
    log("[Ollama] Gerando versão para áudio...")
    audio_prompt = render_template("audio_adhoc", {
        "briefing": resumo,
        "tema":     tema,
    })
    audio_text = ollama_generate(audio_prompt, timeout=180)

    if not audio_text:
        log("[AVISO] Falha na versão áudio, usando texto original.")
        audio_text = re.sub(r"[#*\[\]\(\)]", "", resumo)

    voice    = config.get("tts_voice", "pt-BR-FranciscaNeural")
    mp3_path = briefing_dir / f"{filename_base}.mp3"
    log(f"[TTS] Gerando áudio com voz {voice}...")
    asyncio.run(generate_tts(audio_text, voice, mp3_path))
    log(f"[TTS] Áudio salvo: {mp3_path}")

    # ── Notificação ───────────────────────────────────────────
    try:
        subprocess.run([
            "notify-send", "--icon=dialog-information", "--urgency=normal",
            "Alfred — Briefing ad-hoc",
            f"{titulo}\nTema: {tema} · {len(unique)} fontes",
        ], timeout=5, capture_output=True)
    except Exception:
        pass

    log("=== Ad-hoc concluído ===")
    log(f"  Tema:     {tema}")
    log(f"  Título:   {titulo}")
    log(f"  Markdown: {md_path.name}")
    log(f"  Áudio:    {mp3_path.name}")

    print(f"BRIEFING_FILE={md_path}")


# ── Main ──────────────────────────────────────────────────────
def main():
    argumento = sys.argv[1] if len(sys.argv) > 1 else "matinal_tech"

    try:
        briefing_cfg = get_briefing(argumento)
        _run_briefing_nomeado(briefing_cfg, argumento)
    except KeyError:
        log(f"[ad-hoc] '{argumento}' não é um briefing nomeado — tratando como tema de pesquisa")
        _run_adhoc(argumento)
    except Exception as e:
        log(f"[ERRO] Falha ao carregar perfis.yml: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
