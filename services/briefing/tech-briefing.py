#!/usr/bin/env python3
"""
Alfred Tech Briefing v2 — relatório matinal de tecnologia
Coleta notícias (SearXNG + RSS + extras), sintetiza com Ollama, gera áudio TTS.

Mudanças v2:
  - Fontes em CSV unificado (fontes.csv) com coluna 'tipo' como aba virtual
  - Fix na extração do título (não usa mais "Manchete do dia" como título)
  - Prompt atualizado: LLM gera título na primeira linha
"""

import asyncio
import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import feedparser
import httpx
import yaml
import edge_tts

SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
CONFIG_FILE  = SCRIPT_DIR / "config.yml"

SEARXNG_URL  = os.getenv("SEARXNG_URL", "http://localhost:8888")
OLLAMA_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL        = os.getenv("MODEL_CHAT", "llama3.1:8b")
VAULT_ALFRED = Path(os.getenv("VAULT_ALFRED", ""))


def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def slugify(text: str, max_len: int = 60) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:max_len].strip("-")


# ── Carregamento do CSV unificado ─────────────────────────────

def load_fontes(filepath: Path) -> dict:
    """Carrega o CSV unificado e separa por tipo.

    Colunas: tipo, nome, url, query, categorias, max_items, enabled
    Tipos:   query, feed, extra

    Retorna: {"queries": [...], "feeds": [...], "extras": [...]}
    """
    result = {"queries": [], "feeds": [], "extras": []}

    if not filepath.exists():
        log(f"[CSV] {filepath} não encontrado!")
        return result

    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Checar enabled (default: yes)
            enabled = (row.get("enabled") or "yes").strip().lower()
            if enabled not in ("yes", "sim", "true", "1"):
                continue

            tipo = (row.get("tipo") or "").strip().lower()

            if tipo == "query":
                query_text = (row.get("query") or "").strip()
                if query_text:
                    result["queries"].append({
                        "query": query_text,
                        "categories": (row.get("categorias") or "news,it").strip().replace("|", ","),
                    })

            elif tipo == "feed":
                url = (row.get("url") or "").strip()
                name = (row.get("nome") or "RSS").strip()
                if url:
                    result["feeds"].append({
                        "name": name,
                        "url": url,
                        "max_items": int((row.get("max_items") or "5").strip() or "5"),
                    })

            elif tipo == "extra":
                url = (row.get("url") or "").strip()
                name = (row.get("nome") or "Extra").strip()
                if url:
                    result["extras"].append({
                        "url": url,
                        "name": name,
                    })

    log(f"[CSV] {filepath.name}: {len(result['queries'])} queries, "
        f"{len(result['feeds'])} feeds, {len(result['extras'])} extras")
    return result


# ── Extração de título (fix v2) ───────────────────────────────

# Nomes de seção que NÃO são títulos — o LLM gera esses como
# linhas soltas sem ## quando não segue o prompt direito
NOMES_SECAO = {
    "manchete do dia", "manchete", "destaques", "destaque",
    "tendências", "tendencias", "vale ficar de olho",
    "leitura recomendada", "leituras recomendadas",
    "análise", "analise", "resumo", "briefing",
    "tech briefing", "briefing matinal", "briefing de tecnologia",
    "notícias", "noticias", "headlines", "top stories",
}


def extrair_titulo_manchete(briefing: str) -> str:
    """Extrai o título real da manchete do briefing gerado.

    Estratégia (v2):
    1. Primeira linha não-vazia que NÃO seja header markdown (#)
       e NÃO seja nome de seção genérico
    2. Se não encontrar, tenta pegar o conteúdo após "## Manchete do dia"
    3. Fallback: "Tech Briefing"
    """
    lines = briefing.strip().split("\n")

    # Estratégia 1: primeira linha que parece título real
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        # Limpar formatação
        clean = re.sub(r"[*_\[\]()\"']", "", line).strip()
        if len(clean) < 10:
            continue
        if clean.lower().rstrip(":").strip() in NOMES_SECAO:
            continue
        if clean.startswith(">") or clean.startswith("-"):
            continue
        # Cortar no primeiro ponto final
        if ". " in clean:
            clean = clean[:clean.index(". ")]
        elif clean.endswith("."):
            clean = clean[:-1]
        return clean[:100]

    # Estratégia 2: conteúdo logo após "## Manchete do dia"
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


# ── SearXNG ───────────────────────────────────────────────────
def searxng_search(query: str, categories: str = "news,it", max_results: int = 10) -> list[dict]:
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(
                f"{SEARXNG_URL}/search",
                params={"q": query, "format": "json", "categories": categories},
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])[:max_results]
                return [
                    {
                        "title": r.get("title") or "",
                        "url": r.get("url") or "",
                        "snippet": (r.get("content") or r.get("snippet") or "")[:400],
                        "source": "SearXNG",
                    }
                    for r in results if r.get("url")
                ]
    except Exception as e:
        log(f"[SearXNG] Erro na query '{query}': {e}")
    return []


# ── RSS ───────────────────────────────────────────────────────
def fetch_rss(feed_config: dict) -> list[dict]:
    name = feed_config["name"]
    url = feed_config["url"]
    max_items = feed_config.get("max_items", 5)

    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            summary = entry.get("summary", entry.get("description", ""))
            summary = re.sub(r"<[^>]+>", "", summary)[:400]
            items.append({
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "snippet": summary,
                "source": name,
            })
        log(f"[RSS] {name}: {len(items)} itens")
        return items
    except Exception as e:
        log(f"[RSS] Erro em {name}: {e}")
        return []


# ── Extras (web scraping leve) ────────────────────────────────
def fetch_extra_url(url: str, name: str) -> list[dict]:
    """Busca uma URL e tenta extrair títulos/links de notícias."""
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                log(f"[Extra] {name}: HTTP {resp.status_code}")
                return []

            html = resp.text
            items = []
            links = re.findall(
                r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{15,200})</a>',
                html,
                re.IGNORECASE,
            )
            seen = set()
            for href, title in links:
                title = re.sub(r"\s+", " ", title).strip()
                if title.lower() in seen:
                    continue
                if any(skip in title.lower() for skip in [
                    "home", "about", "contact", "login", "sign", "menu",
                    "cookie", "privacy", "terms", "©", "subscribe",
                ]):
                    continue
                if href.startswith("/"):
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    href = f"{parsed.scheme}://{parsed.netloc}{href}"
                elif not href.startswith("http"):
                    continue

                seen.add(title.lower())
                items.append({
                    "title": title,
                    "url": href,
                    "snippet": "",
                    "source": name,
                })
                if len(items) >= 10:
                    break

            log(f"[Extra] {name}: {len(items)} itens extraídos")
            return items
    except Exception as e:
        log(f"[Extra] {name}: Erro: {e}")
        return []


# ── Deduplicação ──────────────────────────────────────────────
def deduplicate(items: list[dict]) -> list[dict]:
    seen_urls = set()
    seen_titles = set()
    unique = []
    for item in items:
        url = item.get("url") or ""
        title_norm = re.sub(r"\s+", " ", (item.get("title") or "").lower().strip())
        if url in seen_urls:
            continue
        skip = False
        for seen in seen_titles:
            words_new = set(title_norm.split())
            words_seen = set(seen.split())
            if len(words_new) > 2 and len(words_new & words_seen) / max(len(words_new), 1) > 0.8:
                skip = True
                break
        if not skip:
            seen_urls.add(url)
            seen_titles.add(title_norm)
            unique.append(item)
    return unique


# ── Ollama ────────────────────────────────────────────────────
def ollama_generate(prompt: str, timeout: int = 300) -> str:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": MODEL, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
    except Exception as e:
        log(f"[Ollama] Erro: {e}")
        return ""


# ── TTS ───────────────────────────────────────────────────────
async def generate_tts(text: str, voice: str, output_path: Path):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))


# ── Main ──────────────────────────────────────────────────────
def main():
    config = load_config()
    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d")
    time_str = today.strftime("%H:%M")
    date_human = today.strftime("%d/%m/%Y")
    timestamp = today.strftime("%Y-%m-%d_%H%M")

    log(f"=== Tech Briefing v2 — {date_human} {time_str} ===")

    # ── 0. Carregar fontes do CSV unificado ───────────────────
    csv_path = SCRIPT_DIR / config.get("csv_fontes", "fontes.csv")
    fontes = load_fontes(csv_path)

    # ── 1. Coletar notícias ───────────────────────────────────
    all_items = []

    for q in fontes["queries"]:
        results = searxng_search(q["query"], q.get("categories", "news,it"))
        all_items.extend(results)
        log(f"[SearXNG] '{q['query']}': {len(results)} resultados")

    for feed in fontes["feeds"]:
        items = fetch_rss(feed)
        all_items.extend(items)

    for extra in fontes["extras"]:
        items = fetch_extra_url(extra["url"], extra["name"])
        all_items.extend(items)

    log(f"[Total] {len(all_items)} itens coletados")

    # ── 2. Deduplicar e limitar ───────────────────────────────
    max_items = config.get("max_items", 15)
    unique = deduplicate(all_items)[:max_items * 2]
    log(f"[Dedup] {len(unique)} itens únicos")

    if not unique:
        log("[ERRO] Nenhuma notícia coletada. Abortando.")
        sys.exit(1)

    # ── 3. Formatar para o prompt ─────────────────────────────
    news_text = "\n\n".join(
        f"[{i+1}] {item['title']}\n"
        f"Fonte: {item['source']}\n"
        f"URL: {item['url']}\n"
        f"{item['snippet']}"
        for i, item in enumerate(unique[:20])
    )

    # ── 4. Síntese com Ollama ─────────────────────────────────
    log("[Ollama] Gerando briefing escrito...")
    synthesis_prompt = config.get("synthesis_prompt", "Resuma as notícias.") + \
        f"\n\nDATA: {date_human}\n\nNOTÍCIAS:\n{news_text}"

    briefing = ollama_generate(synthesis_prompt, timeout=300)

    if not briefing:
        log("[ERRO] Ollama não retornou síntese. Abortando.")
        sys.exit(1)

    log(f"[Ollama] Briefing gerado ({len(briefing)} chars)")

    # ── 5. Extrair título da manchete ─────────────────────────
    titulo_manchete = extrair_titulo_manchete(briefing)
    titulo_slug = slugify(titulo_manchete)
    filename_base = f"{timestamp}_{titulo_slug}" if titulo_slug else timestamp

    log(f"[Título] {titulo_manchete}")
    log(f"[Slug]   {titulo_slug}")

    # ── 6. Salvar markdown ────────────────────────────────────
    briefing_dir = VAULT_ALFRED / "briefings"
    briefing_dir.mkdir(parents=True, exist_ok=True)

    # Separar título da primeira linha se o LLM seguiu o prompt
    briefing_body = briefing
    first_line = briefing.strip().split("\n")[0].strip()
    first_clean = re.sub(r"[*_#\[\]()\"']", "", first_line).strip()
    if first_clean.lower().rstrip(":") not in NOMES_SECAO and len(first_clean) > 10:
        briefing_body = "\n".join(briefing.strip().split("\n")[1:]).strip()

    md_content = "\n".join([
        "---",
        f"title: \"{titulo_manchete}\"",
        f"date: {date_str}",
        f"time: \"{time_str}\"",
        f"type: briefing",
        f"sources: {len(unique)}",
        f"tags: [briefing, tech, daily]",
        "---",
        "",
        f"# {titulo_manchete}",
        "",
        f"> Briefing de {date_human} às {time_str} · {len(unique)} fontes",
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
    log(f"[Vault] Markdown salvo: {md_path}")

    # ── 7. Gerar versão para áudio ────────────────────────────
    log("[Ollama] Gerando versão para áudio...")
    audio_prompt = config.get("audio_prompt", "Reescreva para áudio.") + \
        f"\n\nBRIEFING:\n{briefing}"

    audio_text = ollama_generate(audio_prompt, timeout=180)

    if not audio_text:
        log("[AVISO] Falha na versão áudio, usando texto original.")
        audio_text = re.sub(r"[#*\[\]\(\)]", "", briefing)

    # ── 8. Gerar MP3 via edge-tts ─────────────────────────────
    voice = config.get("tts_voice", "pt-BR-AntonioNeural")
    mp3_path = briefing_dir / f"{filename_base}.mp3"

    log(f"[TTS] Gerando áudio com voz {voice}...")
    asyncio.run(generate_tts(audio_text, voice, mp3_path))
    log(f"[TTS] Áudio salvo: {mp3_path}")

    # ── 9. Notificação desktop ────────────────────────────────
    try:
        subprocess.run([
            "notify-send", "--icon=dialog-information", "--urgency=normal",
            "Alfred — Tech Briefing",
            f"{titulo_manchete}\n{len(unique)} fontes · Áudio disponível.",
        ], timeout=5, capture_output=True)
    except Exception:
        pass

    log("=== Briefing concluído ===")
    log(f"  Título:   {titulo_manchete}")
    log(f"  Markdown: {md_path.name}")
    log(f"  Áudio:    {mp3_path.name}")

    print(f"BRIEFING_FILE={md_path}")


if __name__ == "__main__":
    main()