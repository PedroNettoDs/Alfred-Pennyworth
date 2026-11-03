"""
Alfred — blog: briefings, jornal e podcast → Blogspot pelo "Postar usando e-mail" do Blogger (BLOG.md).

Fluxo
    tech-briefing.py / core (jornal) / semanal.py gravam o .md com `Blogspot: none` no frontmatter
    extensão "Alfred — blog" no Thunderbird (thunderbird-ext-blog/)
        GET  /blog/fila                         → posts com Blogspot: none (assunto, HTML, .mp3) + destino
        compõe e envia pela conta do Thunderbird para BLOGGER_EMAIL (assunto = título do post)
        GET  /blog/{pasta}/{arquivo}/audio      → .mp3 para anexar
        POST /blog/{pasta}/{arquivo}/resultado  → Blogspot: postado AAAA-MM-DD | "erro AAAA-MM-DD: motivo"

O frontmatter é o estado (sem fila em disco). Para postar de novo, ou depois de um erro, volte a propriedade
para `none` no Obsidian.
"""

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import yaml

import core
from _shared.lib_alfred import log

PROP = "Blogspot"
_RE_PROP = re.compile(rf"^{PROP}:.*$", re.M)


def pastas() -> dict[str, Path]:
    return {
        "briefings": core.vault() / "briefings",
        "jornal": core.vault_noticias() / "jornal",
        "podcast": core.vault_noticias() / "podcast" / "episodios",
    }


def _partes(texto: str) -> Optional[tuple[str, str]]:
    """(frontmatter, corpo) ou None se o .md não começa com frontmatter."""
    if not texto.startswith("---\n"):
        return None
    fim = texto.find("\n---\n", 4)
    return (texto[4:fim], texto[fim + 5:]) if fim >= 0 else None


def _corpo_html(corpo: str) -> str:
    # mesmo markdown do Painel: tudo escapado antes de virar tag — nenhum HTML do LLM/e-mail chega ao post
    from painel import _markdown_seguro
    corpo = re.sub(r"\A\s*# .*\n", "", corpo, count=1)                 # o título já é o assunto
    corpo = re.sub(r"^\s*!\[\[[^\]]*\]\]\s*$", "", corpo, flags=re.M)   # player do .mp3 no Obsidian
    corpo = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(2) or m.group(1), corpo)
    # #end: o Blogger corta ali — a assinatura que o Thunderbird põe depois não entra no post
    return f"{_markdown_seguro(corpo)}\n<p>#end</p>"


def _item(pasta: str, md: Path) -> Optional[dict]:
    partes = _partes(md.read_text(encoding="utf-8"))
    if not partes:
        return None
    try:
        meta = yaml.safe_load(partes[0]) or {}
    except yaml.YAMLError as e:
        log(f"[blog] {md.name}: frontmatter inválido ({e})")
        return None
    if str(meta.get(PROP, "")).strip().lower() != "none":
        return None
    rota = f"/blog/{pasta}/{quote(md.name)}"
    mp3 = md.with_suffix(".mp3")
    return {
        "id": f"{pasta}/{md.name}",
        "assunto": str(meta.get("title") or md.stem),
        "html": _corpo_html(partes[1]),
        "audio": {"rota": f"{rota}/audio", "nome": mp3.name} if mp3.is_file() else None,
        "resultado": f"{rota}/resultado",
        "_ordem": str(meta.get("gerado_em") or f"{meta.get('date', '')} {meta.get('time', '')}"),
    }


def fila() -> dict:
    """Posts com Blogspot: none, do mais antigo ao mais novo (a ordem em que aparecem no blog)."""
    destino = os.getenv("BLOGGER_EMAIL", "").strip()
    if not destino:
        return {"destino": "", "itens": [], "aviso": "BLOGGER_EMAIL não definido no .env"}
    itens = [it for pasta, d in pastas().items() if d.is_dir()
             for md in d.glob("*.md") if (it := _item(pasta, md))]
    itens.sort(key=lambda it: it.pop("_ordem"))
    return {"destino": destino, "itens": itens}


def _arquivo(pasta: str, arquivo: str, sufixo: str = ".md") -> Path:
    d = pastas().get(pasta)
    if d is None or Path(arquivo).name != arquivo or not arquivo.endswith(".md"):
        raise KeyError(f"post inválido: {pasta}/{arquivo}")
    p = d / (arquivo[:-3] + sufixo)
    if not p.is_file():
        raise KeyError(f"não encontrado: {pasta}/{p.name}")
    return p


def audio(pasta: str, arquivo: str) -> Path:
    return _arquivo(pasta, arquivo, ".mp3")


def registrar_resultado(pasta: str, arquivo: str, ok: bool, erro: str = "") -> dict:
    """Troca `Blogspot: none` por postado/erro. Só mexe se ainda estiver `none` (reenvio do POST é inofensivo)."""
    md = _arquivo(pasta, arquivo)
    texto = md.read_text(encoding="utf-8")
    partes = _partes(texto)
    meta = yaml.safe_load(partes[0]) if partes else None
    if not isinstance(meta, dict) or PROP not in meta:
        raise KeyError(f"{md.name} sem a propriedade {PROP}")
    atual = str(meta[PROP])
    if atual.strip().lower() != "none":
        return {"id": f"{pasta}/{arquivo}", PROP: atual, "alterado": False}
    hoje = date.today().isoformat()
    valor = f"postado {hoje}" if ok else json.dumps(f"erro {hoje}: {erro or 'sem detalhe'}"[:300], ensure_ascii=False)
    fm = _RE_PROP.sub(lambda _: f"{PROP}: {valor}", partes[0], count=1)
    md.write_text(f"---\n{fm}\n---\n{partes[1]}", encoding="utf-8")
    log(f"[blog] {pasta}/{arquivo}: {PROP}: {valor}")
    return {"id": f"{pasta}/{arquivo}", PROP: yaml.safe_load(f"v: {valor}")["v"], "alterado": True}
