"""
Tela /painel/ferramentas: tudo o que o Alfred faz, com a documentação de cada função.

Duas fontes:
  - painel_web/ferramentas.yml → as funções (conversa, e-mail, WhatsApp, agenda, notícias, sistema…), escritas a partir
                                 do README e dos about.md: o que fazem, como usar, quando rodam e onde está o código.
  - lib_ferramentas._REGISTRO  → as ferramentas que o Alfred usa na conversa, extraídas do código: nome, descrição,
                                 parâmetros (o JSON Schema da assinatura), se pedem aprovação e onde está a função.
                                 A área de cada uma é o título do bloco `# ═══` em que ela está no arquivo.
                                 Ferramenta nova aparece aqui sem editar nada.

A fila do Redigir continua em /painel/redigir (link da notificação do redigir.py); a navegação chega nela por aqui.
Mesma segurança do Painel (só 127.0.0.1, sessão): usa _RotaPainel e as checagens de painel.py.
"""

import inspect
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Request

import painel
from _shared import lib_ferramentas, lib_wordcraft  # noqa: F401  (importar o lib_wordcraft registra as documento_*)
from _shared.lib_alfred import PROJECT_ROOT

router = APIRouter(prefix="/painel", include_in_schema=False, route_class=painel._RotaPainel)

ARQUIVO = painel.DIR_WEB / "ferramentas.yml"
# Ferramenta fora de um bloco `# ═══` (o lib_wordcraft não tem): a área vem do módulo
AREA_DO_MODULO = {"lib_wordcraft": "Documento no WordCraft (só na janela de Escrita)"}
TIPOS = {"string": "texto", "integer": "número inteiro", "number": "número", "boolean": "sim ou não"}
_TITULO = re.compile(r"#\s{2}(\S.*?)\s*")


# ══════════════════════════════════════════════════════════════
#  Funções (ferramentas.yml)
# ══════════════════════════════════════════════════════════════

def catalogo() -> list[dict]:
    """Grupos de ferramentas.yml: [{id, grupo, funcoes: [{slug, nome, resumo, doc, …}]}]. Relido quando o arquivo muda."""
    return _ler(ARQUIVO.stat().st_mtime_ns)


@lru_cache(maxsize=1)
def _ler(_mtime: int) -> list[dict]:
    with ARQUIVO.open(encoding="utf-8") as f:
        return yaml.safe_load(f)["grupos"]


def funcao(slug: str) -> Optional[dict]:
    return next((f for g in catalogo() for f in g["funcoes"] if f["slug"] == slug), None)


# ══════════════════════════════════════════════════════════════
#  Ferramentas da conversa (extraídas do lib_ferramentas)
# ══════════════════════════════════════════════════════════════

@lru_cache(maxsize=None)
def _blocos(arquivo: str) -> list[tuple[int, str]]:
    """Títulos dos blocos `# ═══` / `#  Título` / `# ═══` de um arquivo → [(número da linha, título)]."""
    linhas = Path(arquivo).read_text(encoding="utf-8").splitlines()
    return [(i + 1, m[1]) for i, ln in enumerate(linhas)
            if i and linhas[i - 1].startswith("# ═") and (m := _TITULO.fullmatch(ln))]


def _parametros(esquema: dict) -> list[dict]:
    obrigatorios = set(esquema.get("required") or [])
    saida = []
    for nome, p in (esquema.get("properties") or {}).items():
        tipo = ("um destes: " + ", ".join(p["enum"])) if p.get("enum") else TIPOS.get(p.get("type"), p.get("type", ""))
        saida.append({"nome": nome, "tipo": tipo, "obrigatorio": nome in obrigatorios,
                      "descricao": p.get("description", "")})
    return saida


@lru_cache(maxsize=1)
def ferramentas_conversa() -> list[dict]:
    """
    [{area, detalhe, ferramentas: [{nome, descricao, perigosa, timeout, parametros, codigo}]}], na ordem do registro.
    "Pesquisa (Researcher)" vira area="Pesquisa", detalhe="Researcher".
    """
    areas: dict[str, dict] = {}
    for nome, f in lib_ferramentas._REGISTRO.items():
        fn = inspect.unwrap(f["funcao"])
        arquivo = inspect.getsourcefile(fn)
        linha = inspect.getsourcelines(fn)[1]
        titulo = next((t for n, t in reversed(_blocos(arquivo)) if n < linha), None)
        titulo = titulo or AREA_DO_MODULO.get(fn.__module__.rsplit(".", 1)[-1], "Outras")
        area, _, detalhe = titulo.partition(" (")
        grupo = areas.setdefault(titulo, {"area": area, "detalhe": detalhe.rstrip(")"), "ferramentas": []})
        try:
            codigo = f"{Path(arquivo).resolve().relative_to(PROJECT_ROOT.resolve())}:{linha}"
        except ValueError:
            codigo = f"{Path(arquivo).name}:{linha}"
        grupo["ferramentas"].append({"nome": nome, "descricao": f["descricao"], "perigosa": f["perigosa"],
                                     "timeout": int(f["timeout"]), "parametros": _parametros(f["parametros"]),
                                     "codigo": codigo})
    return list(areas.values())


def _por_nome() -> dict[str, dict]:
    return {t["nome"]: t for a in ferramentas_conversa() for t in a["ferramentas"]}


# ══════════════════════════════════════════════════════════════
#  Rotas
# ══════════════════════════════════════════════════════════════

@router.get("/ferramentas")
def tela_ferramentas(request: Request):
    if not painel._nonce_da_sessao(request):
        return painel._para_login(request)
    conversa = ferramentas_conversa()
    return painel._pagina(request, "ferramentas.html", "ferramentas", grupos=catalogo(), conversa=conversa,
                          total=sum(len(a["ferramentas"]) for a in conversa))


@router.get("/ferramentas/{slug}")
def tela_funcao(request: Request, slug: str):
    if not painel._nonce_da_sessao(request):
        return painel._para_login(request)
    f = funcao(slug)
    if not f:
        return painel._pagina(request, "ferramenta.html", "ferramentas", status=404, f=None)
    nomes = _por_nome()
    return painel._pagina(request, "ferramenta.html", "ferramentas", f=f, doc=painel._markdown_seguro(f["doc"]),
                          ligadas=[nomes[n] for n in f.get("ferramentas") or [] if n in nomes])
