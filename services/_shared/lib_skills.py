"""
Alfred — skills: procedimentos reutilizáveis em texto (memória procedural), não código executável.

Uma skill é um arquivo `<categoria>/<nome>/SKILL.md` com frontmatter YAML (name, description) e um corpo em
Markdown (quando usar, procedimento, armadilhas, verificação). A skill não roda nada: o modelo lê o texto e
segue os passos com as ferramentas que já tem. O catálogo não vai inteiro no prompt — o modelo descobre o que
precisa por divulgação progressiva: listar_skills / buscar_skill (índice: nome + descrição) e depois ver_skill
(o procedimento inteiro).

Loja: SKILLS_DIR do .env, ou `skills/` na raiz do projeto. Layout `<categoria>/<nome>/SKILL.md`. A busca é
léxica (Jaccard sobre nome + descrição + corpo), sem embeddings e sem modelo a frio.

    listar(categoria="")        → str com "nome — descrição (categoria)", uma por linha
    buscar(consulta, limite=5)  → [{nome, descricao, categoria, pontuacao}]
    ver(nome)                   → corpo inteiro do SKILL.md (sem frontmatter), ou None
"""

import os
import re
from collections import Counter
from pathlib import Path
from typing import Optional

import yaml

from _shared.lib_alfred import PROJECT_ROOT

# cache: assinatura (caminhos + mtime) → catálogo já parseado
_cache: dict = {"assinatura": None, "skills": {}}


def _raiz() -> Path:
    v = os.getenv("SKILLS_DIR", "").strip()
    return Path(v) if v else PROJECT_ROOT / "skills"


def _tokenizar(texto: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (texto or "").lower()) if len(t) >= 3]


def _frontmatter(texto: str) -> tuple[dict, str]:
    """({name, description, ...}, corpo). Sem frontmatter válido → ({}, texto inteiro)."""
    if texto.startswith("---"):
        fim = texto.find("\n---", 3)
        if fim > 0:
            try:
                meta = yaml.safe_load(texto[3:fim]) or {}
            except yaml.YAMLError:
                meta = {}
            if isinstance(meta, dict):
                return meta, texto[fim + 4:].lstrip("\n")
    return {}, texto


def _assinatura(arquivos: list[Path]) -> tuple:
    return tuple(sorted((str(a), a.stat().st_mtime_ns) for a in arquivos))


def carregar() -> dict[str, dict]:
    """{nome: {nome, descricao, categoria, caminho, corpo, _tok}}. Recarrega só quando algum SKILL.md muda."""
    raiz = _raiz()
    if not raiz.is_dir():
        _cache["assinatura"], _cache["skills"] = None, {}
        return {}
    arquivos = sorted(raiz.glob("*/*/SKILL.md"))
    assinatura = _assinatura(arquivos)
    if assinatura == _cache["assinatura"]:
        return _cache["skills"]
    skills: dict[str, dict] = {}
    for md in arquivos:
        try:
            texto = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        meta, corpo = _frontmatter(texto)
        nome = str(meta.get("name") or md.parent.name).strip()
        descricao = " ".join(str(meta.get("description") or "").split())
        categoria = md.parent.parent.name
        if nome in skills:
            continue
        skills[nome] = {
            "nome": nome,
            "descricao": descricao,
            "categoria": categoria,
            "caminho": str(md),
            "corpo": corpo,
            "_nome_tok": Counter(_tokenizar(f"{nome} {categoria}")),
            "_desc_tok": Counter(_tokenizar(descricao)),
            "_corpo_tok": set(_tokenizar(corpo)),
        }
    _cache["assinatura"], _cache["skills"] = assinatura, skills
    return skills


def listar(categoria: str = "") -> list[dict]:
    """[{nome, descricao, categoria}] ordenado por nome; filtra por categoria se dada."""
    skills = carregar()
    cat = (categoria or "").strip().lower()
    saida = [{"nome": s["nome"], "descricao": s["descricao"], "categoria": s["categoria"]}
             for s in skills.values() if not cat or s["categoria"].lower() == cat]
    return sorted(saida, key=lambda s: s["nome"])


def categorias() -> list[str]:
    return sorted({s["categoria"] for s in carregar().values()})


def buscar(consulta: str, limite: int = 5) -> list[dict]:
    """Top-N por score léxico: acerto no nome/categoria pesa mais que na descrição, e o corpo só como desempate.
    Conta ocorrências (nome e descrição), então a skill que mais fala do assunto ganha; o corpo entra só por
    presença, para um texto longo não afogar os demais. Reforço se a consulta inteira aparece no nome/descrição."""
    qt = set(_tokenizar(consulta))
    if not qt:
        return []
    q = " ".join((consulta or "").lower().split())
    achados = []
    for s in carregar().values():
        nt, dt, ct = s["_nome_tok"], s["_desc_tok"], s["_corpo_tok"]
        pont = 0.0
        for t in qt:
            if t in nt:
                pont += 5 + nt[t]
            if t in dt:
                pont += 2 + min(dt[t], 4)
            elif t in ct:
                pont += 1
        if pont <= 0:
            continue
        if q in s["nome"].lower():
            pont += 10
        elif q in s["descricao"].lower():
            pont += 4
        achados.append({"nome": s["nome"], "descricao": s["descricao"],
                        "categoria": s["categoria"], "pontuacao": round(pont, 2)})
    achados.sort(key=lambda a: (a["pontuacao"], a["nome"]), reverse=True)
    return achados[:max(1, limite)]


def ver(nome: str) -> Optional[str]:
    """Corpo inteiro do SKILL.md (sem frontmatter). Nome desconhecido → None."""
    s = carregar().get((nome or "").strip())
    return s["corpo"] if s else None
