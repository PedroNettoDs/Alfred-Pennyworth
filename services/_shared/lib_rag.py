"""
Alfred — RAG local sobre os cofres (substitui a Knowledge Base do Open WebUI). Nada sai da máquina.

Busca híbrida: FTS5 (lib_db.buscar_notas — palavra exata, nome próprio, sigla) + vetores no ChromaDB
(lib_chromadb, embeddings do Ollama com MODEL_EMBED — acha "carro" numa nota que só diz "automóvel"),
fundidas por Reciprocal Rank Fusion. ChromaDB ou Ollama fora do ar: fica só o FTS5, sem exceção.

    buscar(query, cofre=None, pasta="", limite=8)  → [{cofre, caminho, titulo, trecho, pontuacao, origem}]
    contexto(query, limite_chars=4000)            → bloco "[n] caminho (cofre)" + trecho, para o prompt
    indexar(cofre=None, tudo=False)               → FTS5 + vetores dos cofres do .env, incremental

Indexação: cada nota vira pedaços (seções Markdown, no máximo MAX_PEDACO caracteres; seção miúda junta com a
vizinha). Os vetores ficam numa coleção por modelo de embedding (trocar MODEL_EMBED = coleção nova, reindexada
sozinha); o estado (mtime, sha1, nº de pedaços, id da coleção) fica na tabela rag_vetores do mesmo alfred.db do
lib_db. Só nota nova ou alterada vai ao Ollama; nota apagada sai do índice. CLI: services/scripts/chromadb-indexer.py.
"""

import hashlib
import os
import re
import sqlite3
import threading
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Optional

import _shared.lib_chromadb as chroma
from _shared import lib_db
from _shared.lib_alfred import DEFAULT_MODEL_EMBED, log, ollama_embed, ollama_embed_batch

COFRES = {"alfred": "VAULT_ALFRED", "pedro": "VAULT_PEDRO"}   # rótulo no índice → variável do .env
K_RRF = 60                  # constante clássica do RRF: dilui a diferença entre as primeiras posições
MAX_PEDACO = 1200           # caracteres por pedaço (~300 tokens)
MIN_PEDACO = 300            # seção menor que isso junta com a vizinha
DISTANCIA_MAX = 0.5         # cosseno; bge-m3 nos cofres reais: acertos 0,26–0,48, pergunta sem relação ≥ 0,53
FALHAS_SEGUIDAS_MAX = 3     # notas seguidas sem embedding/upsert → Ollama ou ChromaDB caiu; para e tenta no próximo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_vetores(
    cofre TEXT, caminho TEXT, mtime REAL, hash TEXT, pedacos INTEGER, colecao TEXT, PRIMARY KEY(cofre, caminho));
"""
_trava = threading.Lock()   # uma indexação por vez neste processo (o researcher dispara em thread)


def colecao() -> str:
    """Nome da coleção no ChromaDB para o MODEL_EMBED atual ("bge-m3:latest" → "cofres-bge-m3")."""
    modelo = os.getenv("MODEL_EMBED", DEFAULT_MODEL_EMBED).removesuffix(":latest")
    return "cofres-" + (re.sub(r"[^a-z0-9]+", "-", modelo.lower()).strip("-") or "embed")


def _conectar() -> Optional[sqlite3.Connection]:
    p = lib_db.db_path()
    if not p:
        return None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(p), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_SCHEMA)
        return con
    except sqlite3.Error as e:
        log(f"[rag] não consegui abrir {p}: {e}")
        return None


def _vault(cofre: str) -> Optional[Path]:
    v = os.getenv(COFRES.get(cofre, ""), "").strip()
    return Path(v) if v else None


# ══════════════════════════════════════════════════════════════
#  Pedaços
# ══════════════════════════════════════════════════════════════

_RE_SECAO = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_RE_FRONTMATTER = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)", flags=re.S)
# imagem colada em base64 (data:image/png;base64,...), JSON comprimido do Excalidraw, URL gigante: ruído que
# nos cofres reais chega a 99% do tamanho de algumas notas e só gastaria embedding
_RE_RUIDO = re.compile(r"\S{200,}")


def _secoes(corpo: str) -> list[tuple[str, str]]:
    """[(título da seção, texto com o próprio título)]; '#' dentro de bloco de código não abre seção."""
    secoes, atual, linhas, cerca = [], "", [], False
    for linha in corpo.splitlines():
        if linha.lstrip().startswith(("```", "~~~")):
            cerca = not cerca
        m = None if cerca else _RE_SECAO.match(linha)
        if m:
            if "\n".join(linhas).strip():
                secoes.append((atual, "\n".join(linhas).strip()))
            atual, linhas = m.group(1), []
        linhas.append(linha)
    if "\n".join(linhas).strip():
        secoes.append((atual, "\n".join(linhas).strip()))
    return secoes


def _partir(texto: str, maximo: int) -> list[str]:
    """Texto grande → partes de até `maximo` caracteres, cortando entre parágrafos (ou palavras, se preciso)."""
    partes, atual = [], ""
    for par in re.split(r"\n\s*\n", texto):
        par = par.strip()
        while len(par) > maximo:
            corte = par.rfind(" ", 0, maximo)
            corte = corte if corte > maximo // 2 else maximo
            if atual:
                partes.append(atual)
                atual = ""
            partes.append(par[:corte].strip())
            par = par[corte:].strip()
        if not par:
            continue
        if atual and len(atual) + 2 + len(par) > maximo:
            partes.append(atual)
            atual = par
        else:
            atual = f"{atual}\n\n{par}" if atual else par
    if atual:
        partes.append(atual)
    return partes


def pedacos(texto: str, maximo: int = MAX_PEDACO) -> list[dict]:
    """Nota → [{secao, texto}] sem o frontmatter: uma entrada por seção, partida se passar de `maximo`."""
    texto = _RE_RUIDO.sub("", _RE_FRONTMATTER.sub("", texto, count=1))
    blocos = [(secao, parte) for secao, corpo in _secoes(texto) for parte in _partir(corpo, maximo)]
    res: list[dict] = []
    for secao, parte in blocos:
        if res and len(res[-1]["texto"]) + 2 + len(parte) <= maximo \
                and (len(res[-1]["texto"]) < MIN_PEDACO or len(parte) < MIN_PEDACO):
            res[-1]["texto"] += "\n\n" + parte
        else:
            res.append({"secao": secao, "texto": parte})
    return res


# ══════════════════════════════════════════════════════════════
#  Indexação
# ══════════════════════════════════════════════════════════════

def _id(cofre: str, caminho: str, i: int) -> str:
    return f"{cofre}:{caminho}#{i}"


def _notas(vault: Path):
    """(arquivo, caminho relativo) dos .md, sem pastas com ponto — o mesmo recorte do lib_db.indexar_cofre."""
    for raiz, dirs, arquivos in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for a in arquivos:
            if a.endswith(".md"):
                f = Path(raiz) / a
                yield f, f.relative_to(vault).as_posix()


def _apagar(cid: str, cofre: str, caminho: str, indices: range) -> bool:
    return all(chroma.delete_document(cid, _id(cofre, caminho, i)) for i in indices)


def _gravar_nota(cid: str, cofre: str, caminho: str, texto: str, pedacos_antes: int) -> Optional[int]:
    """Embeda e grava os pedaços da nota e apaga os que sobraram da versão anterior. Nº de pedaços, ou None."""
    titulo = lib_db._titulo(Path(caminho), texto)
    partes = pedacos(texto)
    vetores = ollama_embed_batch(["\n".join(x for x in (titulo, p["secao"]) if x) + "\n\n" + p["texto"]
                                  for p in partes])
    if not all(vetores):
        return None
    for i, (p, v) in enumerate(zip(partes, vetores)):
        meta = {"cofre": cofre, "caminho": caminho, "titulo": titulo, "secao": p["secao"], "pedaco": i}
        if not chroma.upsert_document(cid, _id(cofre, caminho, i), p["texto"], meta, v):
            return None
    if not _apagar(cid, cofre, caminho, range(len(partes), pedacos_antes)):
        return None
    return len(partes)


def _indexar_vetores(vault: Path, nome: str, tudo: bool) -> dict:
    res = {"novas": 0, "alteradas": 0, "removidas": 0, "pedacos": 0, "falhas": 0, "total": 0, "offline": False}
    if not vault.is_dir():
        return res
    cid = chroma.ensure_collection(colecao())
    if not cid:
        log(f"[rag] ChromaDB fora do ar — cofre '{nome}': só o FTS5 foi atualizado")
        res["offline"] = True
        return res
    con = _conectar()
    if not con:
        return res
    try:
        with closing(con):
            conhecidos = {r["caminho"]: r for r in con.execute("SELECT * FROM rag_vetores WHERE cofre=?", (nome,))}
            vistos, seguidas, interrompido = set(), 0, False
            for f, rel in _notas(vault):
                vistos.add(rel)
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                antigo = conhecidos.get(rel)
                if antigo and antigo["colecao"] != cid:   # coleção recriada (ChromaDB zerado): vetores sumiram
                    antigo = None
                if antigo and not tudo and antigo["mtime"] == mtime:
                    continue
                try:
                    bruto = f.read_bytes()
                except OSError:
                    continue
                sha = hashlib.sha1(bruto).hexdigest()
                if antigo and not tudo and antigo["hash"] == sha:   # só o mtime mudou (touch, sync)
                    with con:
                        con.execute("UPDATE rag_vetores SET mtime=? WHERE cofre=? AND caminho=?", (mtime, nome, rel))
                    continue
                n = _gravar_nota(cid, nome, rel, bruto.decode("utf-8", errors="ignore"),
                                 antigo["pedacos"] if antigo else 0)
                if n is None:
                    res["falhas"] += 1
                    seguidas += 1
                    if seguidas >= FALHAS_SEGUIDAS_MAX:
                        log(f"[rag] {seguidas} notas seguidas sem embedding/gravação — Ollama ou ChromaDB caiu; "
                            f"cofre '{nome}' fica para a próxima rodada")
                        interrompido = True
                        break
                    continue
                seguidas = 0
                res["alteradas" if antigo else "novas"] += 1
                res["pedacos"] += n
                with con:
                    con.execute("INSERT OR REPLACE INTO rag_vetores VALUES (?,?,?,?,?,?)",
                                (nome, rel, mtime, sha, n, cid))
                if (res["novas"] + res["alteradas"]) % 100 == 0:
                    log(f"[rag] cofre '{nome}': {res['novas'] + res['alteradas']} notas embedadas…")
            if not interrompido:   # com a varredura incompleta, "não visto" não quer dizer "apagado"
                for rel, r in conhecidos.items():
                    if rel in vistos:
                        continue
                    if r["colecao"] == cid and not _apagar(cid, nome, rel, range(r["pedacos"])):
                        res["falhas"] += 1
                        continue
                    with con:
                        con.execute("DELETE FROM rag_vetores WHERE cofre=? AND caminho=?", (nome, rel))
                    res["removidas"] += 1
            res["total"] = con.execute("SELECT COUNT(*) FROM rag_vetores WHERE cofre=? AND colecao=?",
                                       (nome, cid)).fetchone()[0]
    except sqlite3.Error as e:
        log(f"[rag] indexar '{nome}' falhou: {e}")
    return res


def indexar_cofre(vault: Path, nome: str, tudo: bool = False) -> dict:
    """
    FTS5 (lib_db) + vetores de um cofre, incremental. `tudo` recalcula todos os vetores.
    Retorna {fts: {...}, vetores: {novas, alteradas, removidas, pedacos, falhas, total, offline}}. Nunca levanta.
    """
    vault = Path(vault)
    with _trava:
        return {"fts": lib_db.indexar_cofre(vault, nome), "vetores": _indexar_vetores(vault, nome, tudo)}


def indexar(cofre: Optional[str] = None, tudo: bool = False) -> dict:
    """indexar_cofre() para os cofres do .env (VAULT_ALFRED, VAULT_PEDRO) ou só para `cofre`."""
    res = {}
    for nome in ([cofre] if cofre else COFRES):
        vault = _vault(nome)
        if vault:
            res[nome] = indexar_cofre(vault, nome, tudo)
        else:
            log(f"[rag] {COFRES.get(nome, nome)} não definido — cofre '{nome}' ignorado")
    log(f"[rag] indexação: {res}")
    return res


# ══════════════════════════════════════════════════════════════
#  Busca
# ══════════════════════════════════════════════════════════════

def _buscar_vetores(query: str, cofre: Optional[str], pasta: str, n: int) -> list[dict]:
    """Notas mais próximas da pergunta (melhor pedaço de cada uma), em ordem; [] sem ChromaDB ou sem Ollama."""
    cid = chroma.ensure_collection(colecao())
    if not cid:
        return []
    vetor = ollama_embed(query)
    if not vetor:
        return []
    prefixo = pasta.strip("/") + "/" if pasta.strip("/") else ""
    notas, vistas = [], set()
    for a in chroma.query_similar(cid, vetor, n_results=n * 3, where={"cofre": cofre} if cofre else None):
        m = a.get("metadata") or {}
        chave = (m.get("cofre", ""), m.get("caminho", ""))
        if not chave[1] or chave in vistas or not chave[1].startswith(prefixo) \
                or a.get("distance", 1.0) > DISTANCIA_MAX:
            continue
        vistas.add(chave)
        notas.append({"cofre": chave[0], "caminho": chave[1], "titulo": m.get("titulo", ""),
                      "trecho": a.get("document") or ""})
        if len(notas) >= n:
            break
    return notas


def _fundir(fts: list[dict], vetor: list[dict], limite: int) -> list[dict]:
    """Reciprocal Rank Fusion: cada lista soma 1/(K_RRF + posição) à nota; a que está nas duas vira "ambos"."""
    notas: dict[tuple, dict] = {}
    for origem, lista in (("fts", fts), ("vetor", vetor)):
        for pos, r in enumerate(lista, 1):
            chave = (r["cofre"], r["caminho"])
            if chave not in notas:
                notas[chave] = {"cofre": r["cofre"], "caminho": r["caminho"], "titulo": r["titulo"],
                                "trecho": r["trecho"], "pontuacao": 0.0, "origem": origem}
            else:
                notas[chave]["origem"] = "ambos"
                notas[chave]["trecho"] = r["trecho"]   # o pedaço inteiro do vetor rende mais que o snippet do FTS
            notas[chave]["pontuacao"] += 1 / (K_RRF + pos)
    res = sorted(notas.values(), key=lambda r: r["pontuacao"], reverse=True)[:limite]
    for r in res:
        r["pontuacao"] = round(r["pontuacao"], 4)
    return res


def buscar(query: str, cofre: Optional[str] = None, pasta: str = "", limite: int = 8) -> list[dict]:
    """
    Notas dos cofres mais relevantes para `query`: [{cofre, caminho, titulo, trecho, pontuacao, origem}],
    origem = "fts" | "vetor" | "ambos". `cofre` ("alfred"/"pedro") e `pasta` (prefixo do caminho) filtram.
    Sem ChromaDB ou sem Ollama, só o FTS5. Nunca levanta; [] sem índice.
    """
    if not (query or "").strip() or limite <= 0:
        return []
    n = max(limite * 2, 10)
    fts = lib_db.buscar_notas(query, cofre=cofre, pasta=pasta, limite=n)
    try:
        vetor = _buscar_vetores(query, cofre, pasta, n)
    except Exception as e:   # resposta estranha do ChromaDB não pode derrubar a busca
        log(f"[rag] busca vetorial falhou, seguindo só com o FTS5: {e}")
        vetor = []
    return _fundir(fts, vetor, limite)


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto.lower()) if not unicodedata.combining(c))


def _melhor_pedaco(cofre: str, caminho: str, query: str) -> str:
    """Pedaço da nota com mais termos da pergunta — o snippet do FTS5 tem só ~24 palavras. "" se não der."""
    vault = _vault(cofre)
    if not vault:
        return ""
    try:
        texto = (vault / caminho).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    termos = {t for t in re.findall(r"\w+", _sem_acento(query)) if len(t) >= 3}
    partes = pedacos(texto)
    if not partes:
        return ""
    return max(partes, key=lambda p: sum(t in _sem_acento(p["texto"]) for t in termos))["texto"]


def contexto(query: str, limite_chars: int = 4000) -> str:
    """
    Trechos das notas mais relevantes para `query`, prontos para o prompt, com citação numerada:
        [1] caminho/da/nota.md (cofre)
        trecho...
    Corta no `limite_chars`. "" se nada for achado.
    """
    blocos = ["Trechos das notas dos cofres (cite a fonte como [n]):"]
    total = len(blocos[0])
    for n, r in enumerate(buscar(query), 1):
        texto = r["trecho"] if r["origem"] != "fts" else (_melhor_pedaco(r["cofre"], r["caminho"], query)
                                                          or r["trecho"])
        bloco = f"[{n}] {r['caminho']} ({r['cofre']})\n{texto.strip()}"
        sobra = limite_chars - total - 2
        if len(bloco) > sobra:
            if sobra >= 200:
                blocos.append(bloco[:sobra - 1].rstrip() + "…")
            break
        blocos.append(bloco)
        total += len(bloco) + 2
    return "\n\n".join(blocos) if len(blocos) > 1 else ""
