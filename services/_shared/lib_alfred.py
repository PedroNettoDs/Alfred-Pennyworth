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
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx


# ── Configuração via ambiente ─────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
DEFAULT_SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8889")
DEFAULT_MODEL_CHAT = os.getenv("MODEL_CHAT", "llama3.1:8b")
DEFAULT_MODEL_EMBED = os.getenv("MODEL_EMBED", "nomic-embed-text-v2-moe:latest")


# ── 1. load_env ───────────────────────────────────────────────
def load_env(env_path: Optional[Path] = None, override: bool = True) -> dict:
    """
    Carrega o .env da raiz do projeto para os.environ.

    Espelha o que os run.sh fazem em bash, mas em Python — útil para
    rodar scripts diretamente (sem passar pelo run.sh).

    Regras:
      - Ignora linhas em branco e comentários (linhas que começam com #)
      - Faz split no PRIMEIRO `=` (valores podem conter `=`)
      - NÃO suporta comentário inline (KEY=valor # comentário) — propositalmente,
        pra manter consistência com o bug documentado no README
      - Sobrescreve variáveis já existentes em os.environ (override=True, default);
        com override=False, o que já está no ambiente vence o arquivo

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
            if not override and key in os.environ:
                continue
            os.environ[key] = value
            loaded[key] = value

    return loaded


# ── 2. log ────────────────────────────────────────────────────
# Máscaras do sanitizar(), aplicadas nesta ordem: segredos, telefones (+55 antes do número cru) e e-mails.
_RE_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*")
# WEBUI_API_TOKEN=..., EVOLUTION_API_KEY: ..., ?apikey=..., "access_token": "..." — o nome fica, o valor some
_RE_CHAVE = re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|SENHA))(['\"]?\s*[=:]\s*['\"]?)[^\s'\"&,;})\]]+")
_RE_MAIS_55 = re.compile(r"\+55[\s.()-]*\d{2}[\s.()-]*\d{4,5}[\s.-]?\d{4}\b")   # +55 (11) 98765-1234
_RE_NUMERO_55 = re.compile(r"\b55\d{10,11}\b")   # 5511987651234 — cru ou no JID ...@s.whatsapp.net
# JID do WhatsApp (@s.whatsapp.net, @g.us) não é e-mail: o número já foi mascarado acima
_RE_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@(?!s\.whatsapp\.net\b|g\.us\b)"
                       r"([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")


def _mascarar_numero(digitos: str) -> str:
    """5511987651234 → 55119****1234: DDI + DDD + 1º dígito e os 4 últimos ficam legíveis."""
    return digitos[:5] + "*" * (len(digitos) - 9) + digitos[-4:]


def sanitizar(texto: str) -> str:
    """
    Tira segredos e dados pessoais de uma linha de log, mantendo o resto legível:
      - `Bearer <token>` → `Bearer ***`
      - `*_TOKEN`, `*_KEY`, `*_SECRET`, `*_PASSWORD`, `apikey` (com `=` ou `:`) → `NOME=***`
      - telefone `+55 (11) 98765-1234` → `+55119****1234`; `5511987651234[@s.whatsapp.net]` → `55119****1234[...]`
      - e-mail `pedro@gmail.com` → `p***@gmail.com`
    Função pura (sem efeito colateral) — log() passa toda mensagem por aqui.
    """
    texto = _RE_BEARER.sub(r"\1 ***", texto)
    texto = _RE_CHAVE.sub(r"\1\2***", texto)
    texto = _RE_MAIS_55.sub(lambda m: "+" + _mascarar_numero(re.sub(r"\D", "", m.group())), texto)
    texto = _RE_NUMERO_55.sub(lambda m: _mascarar_numero(m.group()), texto)
    return _RE_EMAIL.sub(r"\1***@\2", texto)


def log(msg: str):
    """Log padronizado com timestamp [HH:MM:SS]. Passa por sanitizar(): o journal não guarda segredos nem contatos."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {sanitizar(str(msg))}", flush=True)


# ── 2b. gravar_atomico ────────────────────────────────────────
def gravar_atomico(caminho: Path, conteudo: str | bytes) -> None:
    """
    Grava `conteudo` (texto vira UTF-8; bytes vão como estão) em `caminho` sem nunca deixar o arquivo pela
    metade: escreve um temporário oculto e único
    (`.<nome>.<aleatório>.tmp`) no MESMO diretório, fsync, e troca com os.replace (atômico no mesmo sistema
    de arquivos). Se a escrita falhar no meio (disco cheio, exceção, queda), o arquivo antigo fica intacto e o
    temporário é apagado. Mantém as permissões do arquivo trocado; fsync do diretório para a troca sobreviver
    a um desligamento.

    O nome único dispensa trava só para o temporário: "ler → mudar → gravar" com mais de um processo ainda
    precisa da trava do arquivo (core.trava_arquivo).
    """
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(f".{caminho.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)   # 0666 & ~umask, como um open() comum
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(conteudo.encode("utf-8") if isinstance(conteudo, str) else conteudo)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, caminho.stat().st_mode & 0o777)   # mantém as permissões do arquivo que vai ser trocado
        except OSError:
            pass
        os.replace(tmp, caminho)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        dfd = os.open(caminho.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


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
def _ollama_think_default() -> Optional[bool]:
    """
    Lê OLLAMA_THINK do ambiente: "false"/"0" → False, "true"/"1" → True,
    vazio/ausente → None (campo não é enviado — compatível com modelos sem thinking).
    Modelos como gemma4/qwen3 gastam o orçamento de tokens "pensando" se não
    for desligado, e devolvem resposta vazia em prompts curtos.
    """
    raw = os.getenv("OLLAMA_THINK", "").strip().lower()
    if raw in ("false", "0", "no", "off"):
        return False
    if raw in ("true", "1", "yes", "on"):
        return True
    return None


def ollama_generate(
    prompt: str,
    model: Optional[str] = None,
    timeout: int = 180,
    base_url: Optional[str] = None,
    fmt: Optional[object] = None,
    think: Optional[bool] = None,
    options: Optional[dict] = None,
    system: Optional[str] = None,
    images: Optional[list[str]] = None,
) -> str:
    """
    Wrapper do endpoint /api/generate do Ollama.

    :param prompt: O prompt completo
    :param model: Nome do modelo (default: MODEL_CHAT do .env)
    :param timeout: Timeout em segundos
    :param base_url: URL do Ollama (default: OLLAMA_BASE_URL do .env)
    :param fmt: "json" ou um JSON Schema (dict) para saída estruturada
    :param think: True/False força o campo "think"; None usa OLLAMA_THINK do .env
    :param options: dict de opções do Ollama (num_ctx, temperature, num_predict...).
                    OLLAMA_NUM_CTX do .env entra como num_ctx se não vier em options.
    :param system: system prompt opcional
    :param images: lista de mídias em base64. O Ollama usa este mesmo campo para
                   imagens E áudio (WAV/MP3) em modelos com capacidade "audio",
                   como o gemma4 — é assim que o inbox transcreve áudios do WhatsApp.
    :return: Resposta do modelo (string vazia em caso de erro)
    """
    model = model or os.getenv("MODEL_CHAT", DEFAULT_MODEL_CHAT)
    base_url = base_url or os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL)

    payload: dict = {"model": model, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    if images:
        payload["images"] = list(images)
    if fmt:
        payload["format"] = fmt

    think_val = think if think is not None else _ollama_think_default()
    if think_val is not None:
        payload["think"] = think_val

    opts = dict(options or {})
    num_ctx_env = os.getenv("OLLAMA_NUM_CTX", "").strip()
    if "num_ctx" not in opts and num_ctx_env.isdigit():
        opts["num_ctx"] = int(num_ctx_env)
    if opts:
        payload["options"] = opts

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{base_url}/api/generate", json=payload)
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
    except Exception as e:
        log(f"[ollama_generate] Erro: {e}")
        return ""


# ── 4b. ollama_generate_json ──────────────────────────────────
def ollama_generate_json(
    prompt: str,
    model: Optional[str] = None,
    timeout: int = 120,
    schema: Optional[dict] = None,
    options: Optional[dict] = None,
) -> Optional[object]:
    """
    Chama ollama_generate com format=json (ou JSON Schema) e devolve o objeto
    parseado (dict ou list). Em falha de parse tenta extrair o primeiro
    {...} ou [...] da resposta. Retorna None se nada aproveitável.
    """
    raw = ollama_generate(
        prompt, model=model, timeout=timeout,
        fmt=schema or "json", options=options,
    )
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    for pattern in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pattern, raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                continue
    log(f"[ollama_generate_json] Resposta não é JSON: {raw[:120]!r}")
    return None


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


# ── 5b. ollama_embed_batch ────────────────────────────────────
def ollama_embed_batch(
    texts: list[str],
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> list[list[float]]:
    """
    Gera embeddings para múltiplos textos via Ollama.

    Reutiliza um único httpx.Client para economizar handshake TLS entre
    chamadas (Ollama não tem batch real no /api/embeddings).

    Retorna lista na mesma ordem de entrada — alinhamento de índice garantido.
    Texto vazio ou erro vira [] na posição correspondente.
    Nunca levanta exceção — degradação graciosa com log().

    :param texts:    Lista de textos para embedar
    :param model:    Nome do modelo (default: MODEL_EMBED do .env)
    :param base_url: URL do Ollama (default: OLLAMA_BASE_URL do .env)
    :return:         list[list[float]], mesma ordem de entrada
    """
    model    = model    or os.getenv("MODEL_EMBED",    DEFAULT_MODEL_EMBED)
    base_url = base_url or os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL)

    results: list[list[float]] = []
    try:
        with httpx.Client(timeout=60) as client:
            for text in texts:
                if not text or not text.strip():
                    results.append([])
                    continue
                try:
                    resp = client.post(
                        f"{base_url}/api/embeddings",
                        json={"model": model, "prompt": text},
                    )
                    if resp.status_code == 200:
                        results.append(resp.json().get("embedding", []))
                    else:
                        log(f"[ollama_embed_batch] HTTP {resp.status_code} — {text[:40]!r}")
                        results.append([])
                except Exception as e:
                    log(f"[ollama_embed_batch] Erro em item: {e}")
                    results.append([])
    except Exception as e:
        log(f"[ollama_embed_batch] Erro ao inicializar client: {e}")
        while len(results) < len(texts):
            results.append([])

    return results


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
    gravar_atomico(filepath, body)
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
    log(f"OLLAMA_THINK: {_ollama_think_default()}")
    log(f"slugify('Olá Mundo!'): {slugify('Olá Mundo!')}")
    log("OK")