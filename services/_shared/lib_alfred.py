"""
Alfred — biblioteca compartilhada entre serviços.

Helpers comuns que antes estavam duplicados em briefing/, licitacoes/,
researcher/ e shell-executor/. Importe via:

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from _shared.lib_alfred import log, ollama_generate, searxng_search

Todas as funções leem configuração de variáveis de ambiente, com
fallbacks sensatos. Nenhum hardcode de URL ou modelo.
"""

import os
import re
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx


# ── Configuração via ambiente ─────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
DEFAULT_SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8888")
DEFAULT_MODEL_CHAT = os.getenv("MODEL_CHAT", "llama3.1:8b")
DEFAULT_MODEL_EMBED = os.getenv("MODEL_EMBED", "nomic-embed-text-v2-moe:latest")


# ── 1. load_env ───────────────────────────────────────────────
def load_env(env_path: Optional[Path] = None) -> dict:
    """
    Carrega o .env da raiz do projeto para os.environ.

    Espelha o que os run.sh fazem em bash, mas em Python — útil para
    rodar scripts diretamente (sem passar pelo run.sh).

    Regras:
      - Ignora linhas em branco e comentários (linhas que começam com #)
      - Faz split no PRIMEIRO `=` (valores podem conter `=`)
      - NÃO suporta comentário inline (KEY=valor # comentário) — propositalmente,
        pra manter consistência com o bug documentado no README
      - Sobrescreve variáveis já existentes em os.environ

    Retorna: dict com as variáveis carregadas (útil pra debug).
    """
    if env_path is None:
        env_path = PROJECT_ROOT / ".env"

    if not env_path.exists():
        log(f"[load_env] {env_path} não encontrado")
        return {}

    loaded = {}
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Remover aspas envolventes se houver
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ[key] = value
            loaded[key] = value

    return loaded


# ── 2. log ────────────────────────────────────────────────────
def log(msg: str):
    """Log padronizado com timestamp [HH:MM:SS]."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── 3. slugify ────────────────────────────────────────────────
def slugify(text: str, max_len: int = 60) -> str:
    """
    Converte texto em slug seguro para nome de arquivo/diretório.
    Idêntico ao que estava em briefing/researcher.
    """
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:max_len].strip("-")


# ── 4. ollama_generate ────────────────────────────────────────
def ollama_generate(
    prompt: str,
    model: Optional[str] = None,
    timeout: int = 180,
    base_url: Optional[str] = None,
) -> str:
    """
    Wrapper do endpoint /api/generate do Ollama.

    :param prompt: O prompt completo
    :param model: Nome do modelo (default: MODEL_CHAT do .env)
    :param timeout: Timeout em segundos
    :param base_url: URL do Ollama (default: OLLAMA_BASE_URL do .env)
    :return: Resposta do modelo (string vazia em caso de erro)
    """
    model = model or os.getenv("MODEL_CHAT", DEFAULT_MODEL_CHAT)
    base_url = base_url or os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL)

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{base_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
    except Exception as e:
        log(f"[ollama_generate] Erro: {e}")
        return ""


# ── 5. ollama_embed ───────────────────────────────────────────
def ollama_embed(
    text: str,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> list[float]:
    """
    Gera embeddings via Ollama.

    :return: Lista de floats (vazia em caso de erro)
    """
    model = model or os.getenv("MODEL_EMBED", DEFAULT_MODEL_EMBED)
    base_url = base_url or os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL)

    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{base_url}/api/embeddings",
                json={"model": model, "prompt": text},
            )
            if resp.status_code == 200:
                return resp.json().get("embedding", [])
    except Exception as e:
        log(f"[ollama_embed] Erro: {e}")
    return []


# ── 6. searxng_search ─────────────────────────────────────────
def searxng_search(
    query: str,
    categories: str = "news,it",
    max_results: int = 10,
    base_url: Optional[str] = None,
) -> list[dict]:
    """
    Busca no SearXNG e retorna resultados normalizados.

    Formato de retorno:
        [{"title": ..., "url": ..., "snippet": ..., "source": "SearXNG"}, ...]

    Já filtra resultados sem URL e remove tags HTML do snippet.
    """
    base_url = base_url or os.getenv("SEARXNG_URL", DEFAULT_SEARXNG_URL)

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(
                f"{base_url}/search",
                params={"q": query, "format": "json", "categories": categories},
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])[:max_results]
                normalized = []
                for r in results:
                    url = r.get("url") or ""
                    if not url:
                        continue
                    snippet = r.get("content") or r.get("snippet") or ""
                    snippet = re.sub(r"<[^>]+>", "", snippet)[:400]
                    normalized.append({
                        "title": r.get("title") or "",
                        "url": url,
                        "snippet": snippet,
                        "source": "SearXNG",
                    })
                return normalized
    except Exception as e:
        log(f"[searxng_search] Erro na query '{query}': {e}")
    return []


# ── 7. vault_write ────────────────────────────────────────────
def vault_write(
    vault_path: Path,
    folder: str,
    filename: str,
    content: str,
    frontmatter: Optional[dict] = None,
) -> Path:
    """
    Escreve um arquivo no vault com frontmatter YAML opcional.

    :param vault_path: Path do vault (ex: Path("/vaults/alfred"))
    :param folder: Subpasta dentro do vault (ex: "research/kubernetes")
    :param filename: Nome do arquivo (com ou sem .md)
    :param content: Conteúdo markdown (sem frontmatter)
    :param frontmatter: Dict que vira YAML no topo do arquivo
    :return: Path do arquivo criado
    """
    if not filename.endswith(".md"):
        filename += ".md"

    target_dir = vault_path / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    filepath = target_dir / filename

    body = ""
    if frontmatter:
        body += "---\n"
        for key, value in frontmatter.items():
            if isinstance(value, list):
                body += f"{key}: [{', '.join(str(v) for v in value)}]\n"
            elif isinstance(value, str) and (":" in value or '"' in value):
                escaped = value.replace('"', '\\"')
                body += f'{key}: "{escaped}"\n'
            else:
                body += f"{key}: {value}\n"
        body += "---\n\n"

    body += content
    filepath.write_text(body, encoding="utf-8")
    return filepath


# ── 8. deduplicate_by_url_and_title ───────────────────────────
def deduplicate_by_url_and_title(
    items: list[dict],
    title_key: str = "title",
    url_key: str = "url",
    similarity_threshold: float = 0.8,
) -> list[dict]:
    """
    Remove itens duplicados por URL exata ou título similar.

    Dois títulos são considerados duplicados se compartilham mais de
    `similarity_threshold` (default 80%) das palavras.

    Funciona com qualquer dict que tenha as chaves title/url — pode
    receber `title_key="titulo"` para suportar o licitacoes.
    """
    seen_urls = set()
    seen_titles = []
    unique = []

    for item in items:
        url = item.get(url_key) or ""
        title_norm = re.sub(r"\s+", " ", (item.get(title_key) or "").lower().strip())

        if url and url in seen_urls:
            continue

        words_new = set(title_norm.split())
        is_dup = False
        for seen in seen_titles:
            if len(words_new) > 2:
                overlap = len(words_new & seen) / max(len(words_new), 1)
                if overlap > similarity_threshold:
                    is_dup = True
                    break

        if not is_dup:
            if url:
                seen_urls.add(url)
            seen_titles.append(words_new)
            unique.append(item)

    return unique


# ── Self-test rápido ──────────────────────────────────────────
if __name__ == "__main__":
    log("lib_alfred — self-test")
    log(f"PROJECT_ROOT: {PROJECT_ROOT}")
    log(f"OLLAMA: {DEFAULT_OLLAMA_URL}")
    log(f"SEARXNG: {DEFAULT_SEARXNG_URL}")
    log(f"MODEL_CHAT: {DEFAULT_MODEL_CHAT}")
    log(f"slugify('Olá Mundo!'): {slugify('Olá Mundo!')}")
    log("OK")