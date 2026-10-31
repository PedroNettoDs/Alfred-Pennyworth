#!/usr/bin/env python3
"""
Alfred Tech Briefing v3.3 — templates, título via Ollama, modo ad-hoc.
Clustering, TTS e título vivem em services/_shared (compartilhados com o inbox).

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
    log, slugify, ollama_generate, ollama_embed_batch, searxng_search,
    deduplicate_by_url_and_title, vault_write, gravar_atomico,
)
from _shared.perfis import get_briefing, list_all
from _shared.lib_templates import render_template
from _shared.lib_cluster import cluster_items, rank_clusters, build_clustered_news_text
from _shared.lib_titulo import generate_title_via_llm, extrair_titulo_manchete, NOMES_SECAO
from _shared.lib_tts import generate_tts


SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
CONFIG_FILE  = SCRIPT_DIR / "config.yml"
VAULT_ALFRED = Path(os.getenv("VAULT_ALFRED", ""))

SEEN_FILE         = PROJECT_ROOT / "logs" / "briefing_seen.json"
SEEN_WINDOW_HOURS = 24


# ── Pesos de fontes para ranqueamento de clusters ─────────────
# Fonte não listada usa default 0.5. Migra pro perfis.yml no PR seguinte.
PESOS_FONTES: dict[str, float] = {
    "MIT News":        1.0,
    "Ars Technica":    0.95,
    "Hacker News":     0.9,
    "The Verge":       0.85,
    "InfoQ":           0.85,
    "MIT Tech Review": 0.85,
    "TechCrunch":      0.75,
    "Lobsters":        0.75,
    "TLDR":            0.7,
    "Tecnoblog":       0.7,
}
_PESO_DEFAULT = 0.5


# ── Clustering / TTS / título: migrados para services/_shared ──
# lib_cluster.py, lib_tts.py e lib_titulo.py. Wrappers finos abaixo
# preservam os nomes usados no fluxo deste script.
def _cluster_items(items: list[dict]) -> list[dict]:
    return cluster_items(items)


def _rank_clusters(items: list[dict], pesos_fontes: dict[str, float]) -> list[int]:
    return rank_clusters(items, pesos_fontes)


def _build_clustered_news_text(
    items: list[dict], ranked_cluster_ids: list[int], max_destaques: int = 10,
) -> tuple[str, str]:
    return build_clustered_news_text(items, ranked_cluster_ids, PESOS_FONTES, max_destaques)


# ── Config ────────────────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


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
    """Persiste dict em logs/briefing_seen.json (gravação atômica). Cria diretório se não existir."""
    gravar_atomico(SEEN_FILE, json.dumps(seen, indent=2, ensure_ascii=False))


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


# ── Extração de tópicos do briefing gerado ───────────────────
def _extrair_topicos_do_briefing(briefing_text: str, n: int = 3) -> list[str]:
    """
    Extrai N conceitos/termos-chave CURTOS do briefing para definição posterior.
    Retorna nomes próprios, tecnologias ou conceitos específicos — nunca títulos completos.
    """
    prompt = (
        f"Leia o briefing abaixo e extraia exatamente {n} CONCEITOS-CHAVE curtos (1 a 3 palavras cada).\n"
        f"Regras:\n"
        f"- Extraia NOMES PRÓPRIOS, TECNOLOGIAS ou CONCEITOS ESPECÍFICOS mencionados\n"
        f"- NUNCA copie títulos ou frases longas — apenas o conceito central\n"
        f"- Prefira termos que um leitor possa não conhecer e que merecem explicação\n"
        f'- Responda APENAS um JSON array. Exemplo: ["Artemis II", "Windows Insider", "mantle plume"]\n\n'
        f"BRIEFING:\n{briefing_text[:2000]}"
    )
    raw = ollama_generate(prompt, timeout=60).strip()
    try:
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if match:
            topicos = json.loads(match.group())
            if isinstance(topicos, list):
                # Filtra entradas longas (mais de 5 palavras = provavelmente um título)
                return [str(t).strip() for t in topicos if t and len(str(t).split()) <= 5][:n]
    except Exception as e:
        log(f"[Palavras-chave] Falha ao extrair tópicos: {e}")
    return []


# ── Seção de palavras-chave (substitui Radar vazio) ──────────
def _build_keywords_section(interesses: list[str], max_termos: int = 3) -> str:
    """
    Quando não há itens de Radar, para cada interesse do perfil:
      1. Busca 1 resultado no SearXNG com query "o que é: <termo>"
      2. Usa o snippet como contexto e pede ao LLM uma definição concisa
    Retorna string vazia se SearXNG e LLM não responderem.
    """
    if not interesses:
        return ""

    termos = interesses[:max_termos]
    blocos: list[str] = []

    for termo in termos:
        resultados = searxng_search(f"o que é: {termo}", categories="general,it", max_results=1)

        snippet_ctx = ""
        link_ref    = ""
        if resultados:
            r = resultados[0]
            snippet_ctx = r.get("snippet", "").strip()[:400]
            url   = r.get("url", "")
            title = r.get("title", "").strip()
            if url and title:
                link_ref = f"\n\n*Referência: [{title}]({url})*"

        ctx_bloco = (
            f"\n\nContexto encontrado na web (use SOMENTE se for claramente sobre '{termo}' — "
            f"se o conteúdo não tiver relação direta, IGNORE completamente e use seu próprio conhecimento):\n{snippet_ctx}"
            if snippet_ctx else ""
        )
        prompt = (
            f"Em 2-3 frases diretas, explique o que é '{termo}' para um desenvolvedor."
            f"{ctx_bloco}"
            f"\n\nResponda apenas a definição, sem introdução nem prefixo."
        )

        definicao = ollama_generate(prompt, timeout=60).strip()
        if not definicao:
            continue

        blocos.append(f"### {termo}\n\n{definicao}{link_ref}")

    if not blocos:
        return ""

    return "## Palavras-chave\n\n" + "\n\n".join(blocos)


# ── Seleção equilibrada entre fontes ──────────────────────────
def _intercalar_fontes(itens: list[dict], grupos: list[list[dict]]) -> list[dict]:
    """Reordena `itens` em rodízio entre os grupos de coleta (cada feed RSS e cada query do SearXNG):
    1º de cada fonte, depois o 2º de cada uma... O corte em max_items*2 vem depois — antes era pela ordem de
    coleta e só as primeiras fontes do perfis.yml entravam (6 dos 10 feeds RSS nunca apareciam)."""
    grupo_de = {id(it): gi for gi, g in enumerate(grupos) for it in g}
    filas: dict[int, list[dict]] = {}
    for it in itens:
        filas.setdefault(grupo_de.get(id(it), -1), []).append(it)
    saida, rodada = [], 0
    while len(saida) < len(itens):
        for fila in filas.values():
            if rodada < len(fila):
                saida.append(fila[rodada])
        rodada += 1
    return saida


def _gerar_audio(texto: str, voz: str, mp3_path: Path) -> bool:
    """TTS que não derruba o briefing: em erro (edge-tts fora, voz inválida) loga, apaga o .mp3 parcial
    (antes ficava um arquivo de 0 byte que a tool listava como "com áudio") e devolve False."""
    try:
        asyncio.run(generate_tts(texto, voz, mp3_path))
        if mp3_path.exists() and mp3_path.stat().st_size > 0:
            return True
        log("[AVISO] TTS não gerou áudio — seguindo sem áudio")
    except Exception as e:
        log(f"[AVISO] Falha no TTS ({e}) — seguindo sem áudio")
    try:
        mp3_path.unlink(missing_ok=True)
    except OSError:
        pass
    return False


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
    grupos: list[list[dict]] = []      # um grupo por query/feed — para o rodízio antes do corte
    for fonte in fontes:
        tipo = fonte.get("tipo", "")
        if tipo == "searxng":
            for q in fonte.get("queries", []):
                results = searxng_search(q["q"], categories=q.get("cat", "news,it"), max_results=10)
                all_items.extend(results)
                grupos.append(results)
                log(f"[SearXNG] '{q['q']}': {len(results)} resultados")
        elif tipo == "rss":
            for feed in fonte.get("feeds", []):
                itens_feed = fetch_rss(feed)
                all_items.extend(itens_feed)
                grupos.append(itens_feed)
        else:
            log(f"[AVISO] Tipo de fonte desconhecido: {tipo!r} — pulando")

    log(f"[Total] {len(all_items)} itens coletados")

    max_items = briefing_cfg.get("max_items", config.get("max_items", 15))
    todos = _intercalar_fontes(deduplicate_by_url_and_title(all_items), grupos)
    # ── Memória 24h — separar novas de continuações ───────────
    # O corte em max_items*2 vem DEPOIS da memória: notícias novas primeiro, as continuações só completam.
    # Cortando antes, a 2ª execução do dia recebia os mesmos itens (todos "continuações") e os demais nunca entravam.
    seen = load_seen()
    novas_todas, cont_todas = split_new_vs_continuation(todos, seen)
    unique = (novas_todas + cont_todas)[: max_items * 2]
    log(f"[Dedup] {len(todos)} itens únicos, {len(unique)} usados")

    if not unique:
        log("[ERRO] Nenhuma notícia coletada. Abortando.")
        sys.exit(1)

    novas, continuacoes = split_new_vs_continuation(unique, seen)
    log(f"[Seen] {len(novas)} novas, {len(continuacoes)} continuações")

    if not novas:
        log("[AVISO] Todas as notícias são continuações — briefing focará em acompanhamento")

    # ── Clusterizar e ranquear notícias novas ─────────────────
    novas_para_cluster = novas[:20]
    novas_clusterizadas = _cluster_items(novas_para_cluster)
    ranked_ids = _rank_clusters(novas_clusterizadas, PESOS_FONTES)
    news_text_destaques, news_text_radar = _build_clustered_news_text(
        novas_clusterizadas, ranked_ids, max_destaques=10,
    )
    news_text_continuacoes = format_items_short(continuacoes[:15])

    # ── Síntese via template ──────────────────────────────────
    template_name = briefing_cfg.get("template_prompt")
    if not template_name:
        log(f"[ERRO] Briefing '{briefing_name}' sem template_prompt definido no perfis.yml")
        sys.exit(1)

    # Dict com todas as variáveis possíveis — cada template puxa o que declara.
    # news_text mantido como alias de news_text_destaques para compat com briefing_executivo.
    template_vars = {
        "data":                    date_human,
        "perfil_descricao":        perfil.get("descricao", ""),
        "interesses_quentes":      ", ".join(perfil.get("interesses_quentes", [])),
        "interesses_mornos":       ", ".join(perfil.get("interesses_mornos", [])),
        "excluir":                 ", ".join(perfil.get("excluir", [])),
        "area":                    perfil.get("area", ""),
        "palavras_chave":          ", ".join(perfil.get("palavras_chave", [])),
        "tom":                     perfil.get("tom", ""),
        "news_text":               news_text_destaques,   # alias — compat briefing_executivo
        "news_text_destaques":     news_text_destaques,
        "news_text_radar":         news_text_radar,
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

    # ── Palavras-chave (substitui Radar quando vazio) ─────────
    keywords_section = ""
    if not news_text_radar:
        log("[Palavras-chave] Radar vazio — extraindo tópicos do briefing...")
        topicos = _extrair_topicos_do_briefing(briefing_text, n=3)
        log(f"[Palavras-chave] Tópicos extraídos: {topicos}")
        if topicos:
            keywords_section = _build_keywords_section(topicos, max_termos=3)
            if keywords_section:
                log(f"[Palavras-chave] Seção gerada ({len(keywords_section)} chars)")
            else:
                log("[Palavras-chave] SearXNG sem resultados — seção omitida")

    # ── Título via LLM ────────────────────────────────────────
    log("[Ollama] Extraindo título...")
    titulo_manchete = generate_title_via_llm(briefing_text)
    titulo_slug     = slugify(titulo_manchete)
    # AAAA-MM-DD_HHMM_<briefing>_<slug> (about.md): matinal e vespertino do mesmo dia não se sobrescrevem
    filename_base   = f"{timestamp}_{briefing_name}_{titulo_slug}" if titulo_slug else f"{timestamp}_{briefing_name}"

    log(f"[Título] {titulo_manchete}")
    log(f"[Slug]   {titulo_slug}")

    # ── Salvar markdown ───────────────────────────────────────
    briefing_dir = VAULT_ALFRED / "briefings"
    briefing_dir.mkdir(parents=True, exist_ok=True)

    # Templates instruem "não coloque título na primeira linha" — usar direto
    briefing_body = briefing_text
    if keywords_section:
        briefing_body = briefing_body.rstrip() + "\n\n" + keywords_section

    # ── Fontes citadas — somente as que aparecem no briefing ────
    urls_citadas = set(re.findall(r'https?://[^\s\)\]"]+', briefing_body))
    fontes_citadas = [
        item for item in unique
        if item.get("url") in urls_citadas
    ]
    # Fallback: se nenhuma URL do briefing bateu (LLM não usou links),
    # usa os itens dos clusters de destaque (novas_clusterizadas)
    if not fontes_citadas:
        urls_destaques = set(re.findall(r'https?://[^\s\)\]"]+', news_text_destaques))
        fontes_citadas = [item for item in unique if item.get("url") in urls_destaques]

    n_citadas = len(fontes_citadas)
    log(f"[Fontes] {n_citadas}/{len(unique)} itens citados no briefing")

    md_content = "\n".join([
        "---",
        f'title: "{titulo_manchete}"',
        f"date: {date_str}",
        f'time: "{time_str}"',
        f"briefing: {briefing_name}",
        f"perfil: {briefing_cfg['perfil']}",
        "type: briefing",
        f"sources: {n_citadas}",
        f"tags: [briefing, {briefing_name}]",
        "---",
        "",
        f"# {titulo_manchete}",
        "",
        f"> {briefing_name} · {date_human} às {time_str} · {n_citadas} fonte(s) citada(s)",
        "",
        briefing_body,
        "",
        "---",
        "",
        "## Fontes citadas",
        "",
    ])
    for i, item in enumerate(fontes_citadas, 1):
        md_content += f"{i}. [{item['title']}]({item['url']}) — *{item['source']}*\n"

    md_path = briefing_dir / f"{filename_base}.md"
    md_path.write_text(md_content, encoding="utf-8")
    log(f"[Vault] Markdown salvo: {md_path.name}")

    # ── Persistir memória (logo após o .md: falha no áudio não faz repetir as notícias) ──
    mark_seen(unique, seen)
    save_seen(seen)
    log(f"[Seen] Salvo: {len(seen)} URLs na janela de {SEEN_WINDOW_HOURS}h")

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
        if _gerar_audio(audio_text, voice, mp3_path):
            log(f"[TTS] Áudio salvo: {mp3_path.name}")
        else:
            mp3_path = None
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
    # Usa query qualificada para reduzir resultados irrelevantes por polissemia
    query_qualificada = f'"{tema}" o que é'
    results = searxng_search(query_qualificada, categories="general,news", max_results=15)
    log(f"[SearXNG] '{query_qualificada}': {len(results)} resultados")

    # Filtra resultados que não mencionam o tema no título ou snippet
    tema_lower = tema.lower()
    results_filtrados = [
        r for r in results
        if tema_lower in (r.get("title", "") + r.get("snippet", "")).lower()
    ]
    log(f"[Filtro] {len(results_filtrados)}/{len(results)} resultados com '{tema}' no conteúdo")

    # Fallback: se o filtro zerou, usa os resultados originais
    if not results_filtrados:
        results_filtrados = results

    if not results_filtrados:
        log(f"[ERRO] Nenhum resultado encontrado para '{tema}'. Abortando.")
        sys.exit(1)

    unique = deduplicate_by_url_and_title(results_filtrados)
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
    filename_base = f"{timestamp}_adhoc_{slug}" if slug else f"{timestamp}_adhoc"

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
    if _gerar_audio(audio_text, voice, mp3_path):
        log(f"[TTS] Áudio salvo: {mp3_path}")
    else:
        mp3_path = None

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
    log(f"  Áudio:    {mp3_path.name if mp3_path else '(sem áudio)'}")

    print(f"BRIEFING_FILE={md_path}")


# ── Main ──────────────────────────────────────────────────────
def main():
    argumento = sys.argv[1] if len(sys.argv) > 1 else "matinal_tech"
    if argumento in ("-h", "--help", "help", "ajuda"):
        # antes virava um briefing ad-hoc sobre o tema "--help" (LLM + TTS + arquivo no vault)
        print("Uso: run.sh [nome_do_briefing | \"tema livre\"]   (padrão: matinal_tech)\n"
              "Briefings nomeados: seção briefings: do services/_shared/perfis.yml (ex.: matinal_tech, vespertino_mercado)")
        return

    # Só o get_briefing decide entre nomeado e ad-hoc: um KeyError dentro do fluxo não vira "tema de pesquisa"
    try:
        briefing_cfg = get_briefing(argumento)
    except KeyError:
        briefing_cfg = None
    except Exception as e:
        log(f"[ERRO] Falha ao carregar perfis.yml: {e}")
        sys.exit(1)

    try:
        if briefing_cfg is None:
            log(f"[ad-hoc] '{argumento}' não é um briefing nomeado — tratando como tema de pesquisa")
            _run_adhoc(argumento)
        else:
            _run_briefing_nomeado(briefing_cfg, argumento)
    except Exception as e:
        log(f"[ERRO] Falha no briefing '{argumento}': {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
