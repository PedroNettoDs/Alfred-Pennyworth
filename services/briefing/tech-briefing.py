#!/usr/bin/env python3
"""
Alfred Tech Briefing — relatório matinal de tecnologia
Coleta notícias (SearXNG + RSS), sintetiza com Ollama, gera áudio TTS.
"""

import asyncio
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

# ── Paths ─────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
CONFIG_FILE  = SCRIPT_DIR / "config.yml"

# ── Env ───────────────────────────────────────────────────────
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


# ── SearXNG ───────────────────────────────────────────────────
def searxng_search(query: str, max_results: int = 10) -> list[dict]:
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(
                f"{SEARXNG_URL}/search",
                params={"q": query, "format": "json", "categories": "news,it"},
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])[:max_results]
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": r.get("content", r.get("snippet", ""))[:400],
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
            # Limpar HTML básico
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


# ── Deduplicação ──────────────────────────────────────────────
def deduplicate(items: list[dict]) -> list[dict]:
    seen_urls = set()
    seen_titles = set()
    unique = []

    for item in items:
        url = item.get("url", "")
        title_norm = re.sub(r"\s+", " ", item.get("title", "").lower().strip())

        # Pular se URL duplicada
        if url in seen_urls:
            continue

        # Pular se título muito similar
        skip = False
        for seen in seen_titles:
            # Similaridade simples: se 80%+ das palavras são iguais
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
    date_human = today.strftime("%d/%m/%Y")

    log(f"=== Tech Briefing — {date_human} ===")

    # ── 1. Coletar notícias ───────────────────────────────────
    all_items = []

    # SearXNG
    for query in config.get("searxng_queries", []):
        results = searxng_search(query)
        all_items.extend(results)
        log(f"[SearXNG] '{query}': {len(results)} resultados")

    # RSS
    for feed in config.get("rss_feeds", []):
        items = fetch_rss(feed)
        all_items.extend(items)

    log(f"[Total] {len(all_items)} itens coletados")

    # ── 2. Deduplicar e limitar ───────────────────────────────
    max_items = config.get("max_items", 15)
    unique = deduplicate(all_items)[:max_items * 2]  # margem para o LLM escolher
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

    # ── 5. Salvar markdown ────────────────────────────────────
    briefing_dir = VAULT_ALFRED / "briefings"
    briefing_dir.mkdir(parents=True, exist_ok=True)

    md_content = "\n".join([
        "---",
        f"title: Tech Briefing — {date_human}",
        f"date: {date_str}",
        f"type: briefing",
        f"sources: {len(unique)}",
        f"tags: [briefing, tech, daily]",
        "---",
        "",
        f"# Tech Briefing — {date_human}",
        "",
        briefing,
        "",
        "---",
        "",
        "## Fontes coletadas",
        "",
    ])

    for i, item in enumerate(unique[:20], 1):
        md_content += f"{i}. [{item['title']}]({item['url']}) — *{item['source']}*\n"

    md_path = briefing_dir / f"{date_str}.md"
    md_path.write_text(md_content, encoding="utf-8")
    log(f"[Vault] Markdown salvo: {md_path}")

    # ── 6. Gerar versão para áudio ────────────────────────────
    log("[Ollama] Gerando versão para áudio...")
    audio_prompt = config.get("audio_prompt", "Reescreva para áudio.") + \
        f"\n\nBRIEFING:\n{briefing}"

    audio_text = ollama_generate(audio_prompt, timeout=180)

    if not audio_text:
        log("[AVISO] Falha na versão áudio, usando texto original.")
        audio_text = re.sub(r"[#*\[\]\(\)]", "", briefing)

    # ── 7. Gerar MP3 via edge-tts ─────────────────────────────
    voice = config.get("tts_voice", "pt-BR-AntonioNeural")
    mp3_path = briefing_dir / f"{date_str}.mp3"

    log(f"[TTS] Gerando áudio com voz {voice}...")
    asyncio.run(generate_tts(audio_text, voice, mp3_path))
    log(f"[TTS] Áudio salvo: {mp3_path}")

    # ── 8. Notificação desktop ────────────────────────────────
    try:
        subprocess.run([
            "notify-send",
            "--icon=dialog-information",
            "--urgency=normal",
            "Alfred — Tech Briefing",
            f"Seu briefing de {date_human} está pronto.\n"
            f"{len(unique)} fontes · Áudio disponível.",
        ], timeout=5, capture_output=True)
    except Exception:
        pass  # Sem notificação não é fatal

    log("=== Briefing concluído ===")
    log(f"  Markdown: {md_path}")
    log(f"  Áudio:    {mp3_path}")


if __name__ == "__main__":
    main()
