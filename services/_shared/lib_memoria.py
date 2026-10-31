"""
Alfred — memória automática: fatos duráveis sobre Pedro e seus contatos.

Fluxo (nada sai da máquina — LLM e embeddings no Ollama local):

  1. extrair(texto, fonte, sujeito) — portão barato (_vale_extrair) → LLM com JSON Schema →
     fatos {fato, sujeito, tipo, validade} → guardar().
  2. guardar(fatos, fonte) — vetor (bge-m3) de cada fato; parecido com um já guardado do mesmo sujeito
     (cosseno > limiar_duplicata) → o LLM decide manter_novo / manter_antigo / fundir.
  3. auditar() — um sujeito por vez: apaga vencidos, junta duplicatas, reescreve vagos, tira lixo. Guarda a
     impressão digital (sha256) do estado auditado e pula o LLM quando nada mudou desde a última vez.
  4. contexto(consulta, k) — bloco curto com os fatos mais relevantes (vetor + FTS5) para entrar num prompt.

Armazenamento: SQLite próprio ($MEMORIA_DB, `db` da seção memoria: do perfis.yml, ou
$VAULT_ALFRED/memoria/memoria.db) com FTS5 e o vetor normalizado (float32) como blob na mesma linha.
Espelho legível, um .md por sujeito, na pasta do banco — gravado de forma atômica (temporário + os.replace)
e sobrescrito a cada mudança: editar o .md não muda a memória.

Nenhuma função levanta exceção: sem banco ou sem Ollama, devolve vazio e o chamador segue sem memória.

CLI (de services/):
    python -m _shared.lib_memoria extrair "texto" [--fonte cli] [--sujeito Nome]   # sem texto = stdin
    python -m _shared.lib_memoria listar [--sujeito Nome]
    python -m _shared.lib_memoria auditar
    python -m _shared.lib_memoria buscar "consulta" [-k 8]
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from array import array
from contextlib import closing
from datetime import date, datetime
from itertools import groupby
from pathlib import Path
from typing import Optional

from _shared.lib_alfred import load_env, log, ollama_embed, ollama_generate_json, slugify
from _shared.lib_templates import render_template

PEDRO = "Pedro"
TIPOS = ("identidade", "preferencia", "relacao", "trabalho", "rotina", "projeto", "saude", "outro")
DECISOES = ("manter_novo", "manter_antigo", "fundir")
TOKENIZER = "unicode61 remove_diacritics 2"
CORTE_SEGURO = 0.5               # auditoria que apagaria mais que isso de um lote (com 8+ fatos) é recusada

# Portão: abaixo disso não vale uma chamada ao LLM
MIN_PALAVRAS = 3
MIN_LETRAS = 15
# Palavras que, sozinhas, não carregam fato nenhum (saudação, confirmação, despedida)
_VAZIAS = set("""
oi ola olá opa eai aí ai bom boa bons dia dias tarde noite tudo bem beleza blz td tb tbm ok okay certo
obrigado obrigada brigado brigada valeu vlw show top perfeito combinado sim nao não pode deixa comigo
abraço abraços att até mais logo tchau falou flw bjs beijo beijos e o a de
""".split())
_RISADA = re.compile(r"k+|(?:ha|he|hi|rs)+h?")
_APELIDOS_PEDRO = {"pedro", "pedro netto", "pedro henrique", "eu", "usuario", "usuário", "user"}

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS fatos(
    id INTEGER PRIMARY KEY AUTOINCREMENT, fato TEXT NOT NULL, sujeito TEXT NOT NULL, chave TEXT NOT NULL, tipo TEXT,
    validade TEXT, fonte TEXT, criado TEXT, atualizado TEXT, vetor BLOB, modelo_vetor TEXT);
CREATE INDEX IF NOT EXISTS fatos_chave ON fatos(chave);
CREATE VIRTUAL TABLE IF NOT EXISTS fatos_fts USING fts5(
    fato, sujeito, content='fatos', content_rowid='id', tokenize='{TOKENIZER}');
CREATE TRIGGER IF NOT EXISTS fatos_ai AFTER INSERT ON fatos BEGIN
    INSERT INTO fatos_fts(rowid, fato, sujeito) VALUES (new.id, new.fato, new.sujeito);
END;
CREATE TRIGGER IF NOT EXISTS fatos_ad AFTER DELETE ON fatos BEGIN
    INSERT INTO fatos_fts(fatos_fts, rowid, fato, sujeito) VALUES ('delete', old.id, old.fato, old.sujeito);
END;
CREATE TRIGGER IF NOT EXISTS fatos_au AFTER UPDATE OF fato, sujeito ON fatos BEGIN
    INSERT INTO fatos_fts(fatos_fts, rowid, fato, sujeito) VALUES ('delete', old.id, old.fato, old.sujeito);
    INSERT INTO fatos_fts(rowid, fato, sujeito) VALUES (new.id, new.fato, new.sujeito);
END;
CREATE TABLE IF NOT EXISTS estado(chave TEXT PRIMARY KEY, valor TEXT);
"""

_SCHEMA_EXTRAIR = {
    "type": "object",
    "properties": {"fatos": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "fato": {"type": "string"},
            "sujeito": {"type": "string"},
            "tipo": {"type": "string", "enum": list(TIPOS)},
            "validade": {"type": "string"},
        },
        "required": ["fato", "sujeito", "tipo", "validade"],
    }}},
    "required": ["fatos"],
}
_SCHEMA_DECIDIR = {
    "type": "object",
    "properties": {"decisao": {"type": "string", "enum": list(DECISOES)}, "fato": {"type": "string"}},
    "required": ["decisao", "fato"],
}
_SCHEMA_AUDITAR = {
    "type": "object",
    "properties": {"fatos": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "fato": {"type": "string"},
            "tipo": {"type": "string", "enum": list(TIPOS)},
            "validade": {"type": "string"},
        },
        "required": ["id", "fato", "tipo", "validade"],
    }}},
    "required": ["fatos"],
}


# ══════════════════════════════════════════════════════════════
#  Configuração e conexão
# ══════════════════════════════════════════════════════════════

def _cfg() -> dict:
    """Seção memoria: do perfis.yml (opcional) com defaults."""
    try:
        from _shared.perfis import load
        sec = load().get("memoria") or {}
    except Exception:
        sec = {}
    return {
        "ativo":            bool(sec.get("ativo", True)),
        "db":               str(sec.get("db") or ""),
        "modelo":           str(sec.get("modelo") or ""),
        "modelo_embed":     str(sec.get("modelo_embed") or "bge-m3"),
        "limiar_duplicata": float(sec.get("limiar_duplicata", 0.85)),
        "limiar_contexto":  float(sec.get("limiar_contexto", 0.45)),
        "max_caracteres":   int(sec.get("max_caracteres", 6000)),
        "auditar_lote":     int(sec.get("auditar_lote", 60)),
    }


def _modelo(cfg: dict) -> Optional[str]:
    """Modelo de geração: `modelo` da seção, MODEL_CLASSIFY, ou None (ollama_generate usa MODEL_CHAT)."""
    return cfg["modelo"] or os.getenv("MODEL_CLASSIFY", "").strip() or None


def db_path() -> Optional[Path]:
    """MEMORIA_DB, `db` da seção memoria:, ou $VAULT_ALFRED/memoria/memoria.db; None sem nenhum deles."""
    explicito = os.getenv("MEMORIA_DB", "").strip() or _cfg()["db"]
    if explicito:
        return Path(explicito).expanduser()
    v = os.getenv("VAULT_ALFRED", "").strip()
    return Path(v) / "memoria" / "memoria.db" if v else None


def _conectar() -> Optional[sqlite3.Connection]:
    p = db_path()
    if not p:
        return None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(p), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_SCHEMA)
        return con
    except (sqlite3.Error, OSError) as e:
        log(f"[memoria] não consegui abrir {p}: {e}")
        return None


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _estado(con: sqlite3.Connection, chave: str) -> Optional[str]:
    r = con.execute("SELECT valor FROM estado WHERE chave=?", (chave,)).fetchone()
    return r["valor"] if r else None


def _salvar_estado(con: sqlite3.Connection, chave: str, valor: str) -> None:
    with con:
        con.execute("INSERT OR REPLACE INTO estado VALUES (?, ?)", (chave, valor))


# ══════════════════════════════════════════════════════════════
#  Normalização e vetores
# ══════════════════════════════════════════════════════════════

def _sujeito(nome) -> str:
    """Nome de exibição: apelidos de Pedro ("eu", "pedro netto"...) viram PEDRO; o resto fica como veio."""
    nome = re.sub(r"\s+", " ", str(nome or "")).strip()
    return PEDRO if not nome or nome.lower() in _APELIDOS_PEDRO else nome[:80]


def _chave(sujeito: str) -> str:
    """Agrupa grafias do mesmo sujeito ("Carlos", "carlos") e dá nome ao .md do espelho."""
    return slugify(sujeito) or "sem-nome"


def _validade(v) -> str:
    """'permanente' ou uma data AAAA-MM-DD válida; qualquer outra coisa vira 'permanente'."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(v or ""))
    if m:
        try:
            return date.fromisoformat(m.group()).isoformat()
        except ValueError:
            pass
    return "permanente"


def _limpar(bruto, padrao: Optional[str]) -> Optional[dict]:
    """Item cru do LLM → {fato, sujeito, tipo, validade}; None se não houver fato aproveitável."""
    if not isinstance(bruto, dict):
        return None
    fato = re.sub(r"\s+", " ", str(bruto.get("fato") or "")).strip().rstrip(".")
    if len(fato) < 3:
        return None
    tipo = str(bruto.get("tipo") or "").strip().lower()
    sujeito = _sujeito(bruto.get("sujeito") or padrao)
    if padrao and set(_chave(sujeito).split("-")) <= set(_chave(padrao).split("-")):
        sujeito = padrao     # "Carlos" quando quem escreveu é "Carlos Silva": mesma pessoa, mesmo arquivo
    return {
        "fato": fato[:300],
        "sujeito": sujeito,
        "tipo": tipo if tipo in TIPOS else "outro",
        "validade": _validade(bruto.get("validade")),
    }


def _texto_igual(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"\W+", " ", s.lower()).strip()  # noqa: E731
    return norm(a) == norm(b)


def _normalizar(v) -> Optional[array]:
    """Vetor → float32 de norma 1 (cosseno vira produto escalar); None se vazio ou nulo."""
    n = math.sqrt(sum(x * x for x in v or []))
    return array("f", (x / n for x in v)) if n else None


def _vetor(blob: Optional[bytes]) -> Optional[array]:
    if not blob:
        return None
    a = array("f")
    a.frombytes(blob)
    return a


def _cos(a: array, b: Optional[array]) -> float:
    return sum(x * y for x, y in zip(a, b)) if b is not None and len(a) == len(b) else 0.0


def _embed(fato: str, sujeito: str, cfg: dict) -> Optional[array]:
    # O sujeito entra no texto do vetor: "Carlos: prefere café" e "Pedro: prefere café" não se confundem na busca
    return _normalizar(ollama_embed(f"{sujeito}: {fato}", model=cfg["modelo_embed"]))


# ══════════════════════════════════════════════════════════════
#  1. Extração
# ══════════════════════════════════════════════════════════════

def _vale_extrair(texto: str) -> bool:
    """
    Portão barato antes do LLM: vale gastar uma chamada procurando fatos neste texto?

    Hoje é heurística — mínimo de palavras e letras, e pelo menos uma palavra que não seja saudação,
    confirmação ou risada. Será trocada por uma decisão sim/não do Laya (classificador local servido em
    127.0.0.1:7073) quando o modelo treinado for aprovado: mantenha a assinatura texto → bool para a
    troca ficar só aqui.
    """
    palavras = re.findall(r"[^\W\d_]+", (texto or "").lower())
    if len(palavras) < MIN_PALAVRAS or sum(map(len, palavras)) < MIN_LETRAS:
        return False
    return any(p not in _VAZIAS and not _RISADA.fullmatch(p) for p in palavras)


def extrair(texto: str, fonte: str, sujeito: Optional[str] = None) -> list[dict]:
    """
    Extrai fatos duráveis do texto e já os guarda (deduplicados). `sujeito` = quem escreveu / de quem é a
    conversa (o "eu" do texto); sem ele o LLM deduz e, na dúvida, o fato fica com Pedro.
    Devolve o que guardar() devolve. Memória desligada, sem banco, portão negou, Ollama fora do ar ou
    resposta inválida → [].
    """
    cfg = _cfg()
    if not cfg["ativo"] or not db_path() or not _vale_extrair(texto):
        return []
    padrao = _sujeito(sujeito) if sujeito else None
    prompt = render_template("memoria_extrair", {
        "fonte": fonte or "?",
        "sujeito": padrao or "não informado — deduza pelo texto",
        "hoje": date.today().isoformat(),
        "texto": texto.strip()[:cfg["max_caracteres"]],
    })
    resp = ollama_generate_json(prompt, model=_modelo(cfg), schema=_SCHEMA_EXTRAIR,
                                options={"temperature": 0.1})
    brutos = resp.get("fatos") if isinstance(resp, dict) else None
    if not isinstance(brutos, list):
        if resp is not None:
            log(f"[memoria] extração sem lista de fatos: {str(resp)[:120]!r}")
        return []
    fatos = [f for f in (_limpar(b, padrao) for b in brutos) if f]
    return guardar(fatos, fonte) if fatos else []


# ══════════════════════════════════════════════════════════════
#  2. Armazenamento e deduplicação
# ══════════════════════════════════════════════════════════════

def _parecido(con: sqlite3.Connection, f: dict, vetor: Optional[array], cfg: dict) -> tuple:
    """
    Fato guardado do mesmo sujeito que bate com `f`: (linha, True) se o texto é igual; (linha, False) para o
    mais parecido com cosseno acima do limiar; (None, False) se nenhum.
    """
    rows = con.execute("SELECT * FROM fatos WHERE chave=?", (_chave(f["sujeito"]),)).fetchall()
    for r in rows:
        if _texto_igual(r["fato"], f["fato"]):
            return r, True
    if vetor is None:
        return None, False
    sims = [(r, _cos(vetor, _vetor(r["vetor"]))) for r in rows if r["modelo_vetor"] == cfg["modelo_embed"]]
    melhor = max(sims, key=lambda x: x[1], default=None)
    return (melhor[0], False) if melhor and melhor[1] > cfg["limiar_duplicata"] else (None, False)


def _decidir(antigo: str, novo: str, sujeito: str, cfg: dict) -> tuple[Optional[str], str]:
    """LLM escolhe entre os dois fatos → (decisao, fato fundido); (None, "") se falhar."""
    prompt = render_template("memoria_deduplicar", {"sujeito": sujeito, "antigo": antigo, "novo": novo})
    resp = ollama_generate_json(prompt, model=_modelo(cfg), schema=_SCHEMA_DECIDIR,
                                options={"temperature": 0})
    if not isinstance(resp, dict) or resp.get("decisao") not in DECISOES:
        return None, ""
    fundido = re.sub(r"\s+", " ", str(resp.get("fato") or "")).strip().rstrip(".")
    if resp["decisao"] == "fundir" and len(fundido) < 3:
        return None, ""
    return resp["decisao"], fundido[:300]


def _inserir(con: sqlite3.Connection, f: dict, fonte: str, vetor: Optional[array], cfg: dict) -> int:
    agora = _agora()
    with con:
        cur = con.execute(
            "INSERT INTO fatos(fato, sujeito, chave, tipo, validade, fonte, criado, atualizado, vetor, modelo_vetor)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f["fato"], f["sujeito"], _chave(f["sujeito"]), f["tipo"], f["validade"], fonte, agora, agora,
             vetor.tobytes() if vetor is not None else None, cfg["modelo_embed"] if vetor is not None else None))
    return cur.lastrowid


def guardar(fatos: list[dict], fonte: str = "") -> list[dict]:
    """
    Grava fatos {fato, sujeito, tipo, validade}, deduplicando contra os já guardados do mesmo sujeito:
    texto igual → descartado sem LLM; cosseno > limiar_duplicata → o LLM decide. Se o LLM não decidir,
    o novo entra assim mesmo (a auditoria junta depois — melhor duplicado que perdido).
    Devolve cada fato com `id` e `acao` (novo | fundido | substituiu | descartado). Sem banco → [].
    """
    con = _conectar()
    if not con:
        return []
    cfg = _cfg()
    saida, mexidos = [], set()
    try:
        with closing(con):
            for f in filter(None, (_limpar(b, None) for b in fatos or [])):
                vetor = _embed(f["fato"], f["sujeito"], cfg)
                antigo, igual = _parecido(con, f, vetor, cfg)
                acao, fid = "novo", None
                if igual:
                    acao, fid = "descartado", antigo["id"]
                elif antigo is not None:
                    decisao, fundido = _decidir(antigo["fato"], f["fato"], f["sujeito"], cfg)
                    if decisao == "manter_antigo":
                        acao, fid = "descartado", antigo["id"]
                    elif decisao == "fundir":
                        v = _embed(fundido, f["sujeito"], cfg)
                        with con:
                            con.execute("UPDATE fatos SET fato=?, atualizado=?, vetor=?, modelo_vetor=? WHERE id=?",
                                        (fundido, _agora(), v.tobytes() if v is not None else None,
                                         cfg["modelo_embed"] if v is not None else None, antigo["id"]))
                        f = {**f, "fato": fundido, "tipo": antigo["tipo"], "validade": antigo["validade"]}
                        acao, fid = "fundido", antigo["id"]
                    elif decisao == "manter_novo":
                        with con:
                            con.execute("DELETE FROM fatos WHERE id=?", (antigo["id"],))
                        acao = "substituiu"
                if fid is None:
                    fid = _inserir(con, f, fonte, vetor, cfg)
                if acao != "descartado":
                    mexidos.add(_chave(f["sujeito"]))
                saida.append({**f, "id": fid, "acao": acao})
            for chave in mexidos:
                _espelhar(con, chave)
    except sqlite3.Error as e:
        log(f"[memoria] guardar falhou: {e}")
    return saida


def listar(sujeito: Optional[str] = None) -> list[dict]:
    """Fatos guardados (todos ou de um sujeito), sem o vetor."""
    con = _conectar()
    if not con:
        return []
    campos = "id, fato, sujeito, tipo, validade, fonte, criado, atualizado"
    try:
        with closing(con):
            if sujeito:
                rows = con.execute(f"SELECT {campos} FROM fatos WHERE chave=? ORDER BY id",
                                   (_chave(_sujeito(sujeito)),)).fetchall()
            else:
                rows = con.execute(f"SELECT {campos} FROM fatos ORDER BY chave, id").fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error as e:
        log(f"[memoria] listar falhou: {e}")
        return []


# ══════════════════════════════════════════════════════════════
#  Espelho legível no cofre
# ══════════════════════════════════════════════════════════════

def _gravar_atomico(arq: Path, texto: str) -> None:
    """Temporário na mesma pasta + os.replace: o Obsidian nunca vê o arquivo pela metade."""
    fd, tmp = tempfile.mkstemp(dir=arq.parent, prefix=f".{arq.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(texto)
        os.replace(tmp, arq)
    except OSError as e:
        log(f"[memoria] não consegui gravar {arq}: {e}")
        Path(tmp).unlink(missing_ok=True)


def _espelhar(con: sqlite3.Connection, chave: str) -> None:
    """Regrava <pasta do banco>/<chave>.md com os fatos do sujeito; sem fatos, apaga o arquivo."""
    arq = db_path().parent / f"{chave}.md"
    rows = con.execute("SELECT * FROM fatos WHERE chave=? ORDER BY tipo, id", (chave,)).fetchall()
    if not rows:
        arq.unlink(missing_ok=True)
        return
    sujeito = rows[-1]["sujeito"]
    linhas = ["---", f"sujeito: {json.dumps(sujeito, ensure_ascii=False)}", f"atualizado: {_agora()}",
              f"fatos: {len(rows)}", "---", f"# {sujeito}", "",
              "> Gerado por `_shared/lib_memoria.py` a cada mudança — editar aqui não muda a memória.", ""]
    for tipo, grupo in groupby(rows, key=lambda r: r["tipo"] or "outro"):
        linhas.append(f"## {tipo}")
        for r in grupo:
            ate = f" (até {r['validade']})" if r["validade"] != "permanente" else ""
            linhas.append(f"- {r['fato']}{ate} · _{r['fonte'] or '?'}, {(r['atualizado'] or '')[:10]}_")
        linhas.append("")
    _gravar_atomico(arq, "\n".join(linhas))


# ══════════════════════════════════════════════════════════════
#  3. Auditoria
# ══════════════════════════════════════════════════════════════

def _impressao(rows) -> str:
    """sha256 do estado de um sujeito: independe da ordem, muda com qualquer inclusão, edição ou remoção."""
    estado = sorted([r["id"], r["fato"], r["tipo"] or "", r["validade"] or ""] for r in rows)
    return hashlib.sha256(json.dumps(estado, ensure_ascii=False).encode("utf-8")).hexdigest()


def _completar_vetores(con: sqlite3.Connection, cfg: dict) -> int:
    """Fatos sem vetor (Ollama fora na hora de guardar) ou de outro modelo de embedding ganham vetor novo."""
    rows = con.execute("SELECT id, fato, sujeito FROM fatos WHERE vetor IS NULL OR modelo_vetor IS NOT ?",
                       (cfg["modelo_embed"],)).fetchall()
    novos = []
    for r in rows:
        v = _embed(r["fato"], r["sujeito"], cfg)
        if v is None:
            break                # Ollama fora: não insiste fato por fato
        novos.append((v.tobytes(), cfg["modelo_embed"], r["id"]))
    if novos:
        with con:
            con.executemany("UPDATE fatos SET vetor=?, modelo_vetor=? WHERE id=?", novos)
    return len(novos)


def _auditar_lote(con: sqlite3.Connection, lote: list, cfg: dict) -> Optional[tuple[int, int]]:
    """Um lote de fatos de um sujeito → LLM → aplica. (reescritos, removidos), ou None se nada foi aplicado."""
    sujeito = lote[0]["sujeito"]
    entrada = [{"id": r["id"], "fato": r["fato"], "tipo": r["tipo"], "validade": r["validade"],
                "atualizado": (r["atualizado"] or "")[:10]} for r in lote]
    prompt = render_template("memoria_auditar", {
        "sujeito": sujeito, "hoje": date.today().isoformat(),
        "fatos": json.dumps(entrada, ensure_ascii=False, indent=1)})
    resp = ollama_generate_json(prompt, model=_modelo(cfg), timeout=300, schema=_SCHEMA_AUDITAR,
                                options={"temperature": 0.1})
    itens = resp.get("fatos") if isinstance(resp, dict) else None
    if not isinstance(itens, list):
        log(f"[memoria] auditoria de {sujeito}: LLM não devolveu a lista")
        return None

    originais = {r["id"]: r for r in lote}
    manter: dict[int, dict] = {}
    for it in itens:
        try:
            fid = int(it.get("id"))
        except (AttributeError, TypeError, ValueError):
            continue
        if fid not in originais or fid in manter:      # id inventado ou repetido
            continue
        limpo = _limpar({**it, "sujeito": sujeito}, None)
        if limpo:
            manter[fid] = limpo
    if len(lote) >= 8 and len(manter) < len(lote) * CORTE_SEGURO:
        log(f"[memoria] auditoria de {sujeito} cortaria {len(lote)} → {len(manter)} fatos; recusada")
        return None

    apagar, mudar = [], []
    for fid, r in originais.items():
        novo = manter.get(fid)
        if novo is None:
            apagar.append((fid,))
        elif (novo["fato"], novo["tipo"], novo["validade"]) != (r["fato"], r["tipo"], r["validade"]):
            v = _embed(novo["fato"], sujeito, cfg) if novo["fato"] != r["fato"] else _vetor(r["vetor"])
            mudar.append((novo["fato"], novo["tipo"], novo["validade"], _agora(),
                          v.tobytes() if v is not None else None,
                          cfg["modelo_embed"] if v is not None else None, fid))
    with con:
        con.executemany("DELETE FROM fatos WHERE id=?", apagar)
        con.executemany("UPDATE fatos SET fato=?, tipo=?, validade=?, atualizado=?, vetor=?, modelo_vetor=?"
                        " WHERE id=?", mudar)
    return len(mudar), len(apagar)


def auditar() -> dict:
    """
    Faxina da memória: apaga fatos vencidos (sem LLM), completa vetores que faltam e manda ao LLM, em lotes,
    os fatos de cada sujeito que mudou desde a última auditoria — ele junta duplicatas, reescreve vagos e
    tira lixo. Sujeito com a mesma impressão digital da última auditoria é pulado sem chamar o LLM.
    Devolve {antes, depois, expirados, reescritos, removidos, auditados, pulados}; sem banco → {}.
    """
    con = _conectar()
    if not con:
        return {}
    cfg = _cfg()
    res = {"antes": 0, "depois": 0, "expirados": 0, "reescritos": 0, "removidos": 0,
           "auditados": [], "pulados": []}
    try:
        with closing(con):
            res["antes"] = con.execute("SELECT COUNT(*) FROM fatos").fetchone()[0]
            hoje = date.today().isoformat()
            vencido = "validade != 'permanente' AND validade < ?"
            mexidos = {r[0] for r in con.execute(f"SELECT DISTINCT chave FROM fatos WHERE {vencido}", (hoje,))}
            with con:
                res["expirados"] = con.execute(f"DELETE FROM fatos WHERE {vencido}", (hoje,)).rowcount
            _completar_vetores(con, cfg)

            for chave in [r[0] for r in con.execute("SELECT DISTINCT chave FROM fatos ORDER BY chave")]:
                rows = con.execute("SELECT * FROM fatos WHERE chave=? ORDER BY tipo, fato", (chave,)).fetchall()
                if _estado(con, f"auditoria:{chave}") == _impressao(rows):
                    res["pulados"].append(chave)
                    continue
                ok, n = True, max(1, cfg["auditar_lote"])
                for i in range(0, len(rows), n):
                    feito = _auditar_lote(con, rows[i:i + n], cfg)
                    if feito is None:
                        ok = False
                        continue
                    res["reescritos"] += feito[0]
                    res["removidos"] += feito[1]
                    if any(feito):
                        mexidos.add(chave)
                if ok:   # lote que falhou deixa o sujeito sem impressão: a próxima auditoria tenta de novo
                    depois = con.execute("SELECT * FROM fatos WHERE chave=?", (chave,)).fetchall()
                    _salvar_estado(con, f"auditoria:{chave}", _impressao(depois))
                    res["auditados"].append(chave)

            for chave in mexidos:
                _espelhar(con, chave)
            res["depois"] = con.execute("SELECT COUNT(*) FROM fatos").fetchone()[0]
    except sqlite3.Error as e:
        log(f"[memoria] auditar falhou: {e}")
    return res


# ══════════════════════════════════════════════════════════════
#  4. Contexto para prompts
# ══════════════════════════════════════════════════════════════

def _consulta_fts(texto: str) -> str:
    """Termos de 4+ letras, por prefixo, com OR — palavras curtas ("de", "que") casariam com tudo."""
    termos = dict.fromkeys(t.lower() for t in re.findall(r"\w{4,}", texto or ""))
    return " OR ".join(f'"{t}"*' for t in list(termos)[:30])


def contexto(consulta: str, k: int = 8) -> str:
    """
    Bloco curto com os k fatos mais relevantes para `consulta`, pronto para entrar num prompt; "" se não
    houver nada. Junta a busca vetorial (cosseno ≥ limiar_contexto) e a FTS5 por fusão de rankings (RRF);
    sem Ollama, fica só a FTS5. Fatos vencidos ficam de fora.
    """
    if not (consulta or "").strip() or k <= 0:
        return ""
    con = _conectar()
    if not con:
        return ""
    cfg = _cfg()
    pontos: dict[int, float] = {}
    try:
        with closing(con):
            fatos = {r["id"]: r for r in con.execute(
                "SELECT id, fato, sujeito, vetor, modelo_vetor FROM fatos"
                " WHERE validade = 'permanente' OR validade >= ?", (date.today().isoformat(),))}
            if not fatos:
                return ""
            q = _normalizar(ollama_embed(consulta[:2000], model=cfg["modelo_embed"]))
            if q is not None:
                sims = sorted(((_cos(q, _vetor(r["vetor"])), fid) for fid, r in fatos.items()
                               if r["modelo_vetor"] == cfg["modelo_embed"]), reverse=True)
                for pos, (_, fid) in enumerate(s for s in sims if s[0] >= cfg["limiar_contexto"]):
                    pontos[fid] = pontos.get(fid, 0.0) + 1 / (60 + pos)
            termos = _consulta_fts(consulta)
            if termos:
                rows = con.execute("SELECT rowid FROM fatos_fts WHERE fatos_fts MATCH ?"
                                   " ORDER BY bm25(fatos_fts, 1.0, 2.0) LIMIT 50", (termos,)).fetchall()
                for pos, (fid,) in enumerate(r for r in rows if r[0] in fatos):
                    pontos[fid] = pontos.get(fid, 0.0) + 1 / (60 + pos)
    except sqlite3.Error as e:
        log(f"[memoria] contexto falhou: {e}")
        return ""
    melhores = sorted(pontos, key=pontos.get, reverse=True)[:k]
    if not melhores:
        return ""
    linhas = ["Fatos que Alfred já sabe (memória automática; podem estar desatualizados):"]
    linhas += [f"- [{fatos[fid]['sujeito']}] {fatos[fid]['fato']}" for fid in melhores]
    return "\n".join(linhas)


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m _shared.lib_memoria", description="Memória automática do Alfred")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("extrair", help='extrai e guarda fatos de um texto (sem texto ou "-" = stdin)')
    p.add_argument("texto", nargs="?", default="-")
    p.add_argument("--fonte", default="cli")
    p.add_argument("--sujeito", help="quem escreveu o texto (padrão: o LLM deduz; na dúvida, Pedro)")
    p = sub.add_parser("listar", help="fatos guardados")
    p.add_argument("--sujeito")
    sub.add_parser("auditar", help="faxina: vencidos, duplicatas, vagos e lixo")
    p = sub.add_parser("buscar", help="o bloco de contexto que iria para um prompt")
    p.add_argument("consulta")
    p.add_argument("-k", type=int, default=8)
    a = ap.parse_args(argv)

    load_env(override=False)
    if a.cmd == "buscar":
        print(contexto(a.consulta, a.k) or "(nada na memória para isso)")
        return 0
    if a.cmd == "extrair":
        out = extrair(sys.stdin.read() if a.texto == "-" else a.texto, a.fonte, a.sujeito)
    elif a.cmd == "listar":
        out = listar(a.sujeito)
    else:
        out = auditar()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
