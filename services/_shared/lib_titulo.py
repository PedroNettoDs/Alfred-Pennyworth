"""
Alfred — extração de título/manchete de um briefing em markdown.

Extraído de services/briefing/tech-briefing.py para reuso.

    from _shared.lib_titulo import generate_title_via_llm, extrair_titulo_manchete
"""

import json
import re

from _shared.lib_alfred import log, ollama_generate

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
    "edição do dia", "edicao do dia", "radar", "agenda",
}


def extrair_titulo_manchete(briefing: str, fallback: str = "Tech Briefing") -> str:
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

    return fallback


def generate_title_via_llm(briefing_md: str, fallback: str = "Tech Briefing") -> str:
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
    return extrair_titulo_manchete(briefing_md, fallback=fallback)
