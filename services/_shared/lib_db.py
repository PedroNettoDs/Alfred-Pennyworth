"""
Alfred — SQLite + FTS5 compartilhado (busca local, histórico do WhatsApp, cache de classificação).

Três usos, um arquivo só ($VAULT_ALFRED/.alfred/alfred.db — o Obsidian ignora pastas com ponto,
e o container do Open WebUI já enxerga o cofre em /vaults/alfred):

  1. notas   — índice FTS5 dos .md dos cofres (DERIVADO: apague o .db e `indexar` reconstrói tudo).
  2. wa_msgs — histórico das mensagens do WhatsApp (texto/transcrição). Única tabela gravada direto;
               só alimenta a busca e o contexto do prompt. Retenção em dias (`podar_wa`).
  3. cache_classif — resultado do LLM para remetentes que sempre caem numa categoria não acionável
               (promoção, spam...). Só pula o modelo depois de `MINIMO_CONFIRMACOES` respostas iguais.

Os .md e o YAML continuam sendo a fonte de verdade. Nenhuma função levanta exceção: falha de
SQLite degrada com log() e devolve vazio/None (o chamador cai no caminho sem banco).
"""

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from _shared.lib_alfred import log
except ImportError:  # carregado por caminho, fora do pacote (testes)
    def log(msg: str):
        print(msg, flush=True)

TOKENIZER = "unicode61 remove_diacritics 2"
MINIMO_CONFIRMACOES = 3          # o LLM precisa dar a mesma categoria N vezes antes do cache pular o modelo
CACHE_TTL_DIAS = 30

_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS notas USING fts5(
    titulo, corpo, caminho UNINDEXED, cofre UNINDEXED, tokenize='{TOKENIZER}');
CREATE TABLE IF NOT EXISTS notas_meta(
    cofre TEXT, caminho TEXT, mtime REAL, fts_rowid INTEGER, PRIMARY KEY(cofre, caminho));

CREATE TABLE IF NOT EXISTS wa_msgs(
    message_id TEXT PRIMARY KEY, remote_jid TEXT, contato TEXT, grupo INTEGER, from_me INTEGER,
    tipo TEXT, ts TEXT, gravado TEXT, texto TEXT);
CREATE INDEX IF NOT EXISTS wa_msgs_jid ON wa_msgs(remote_jid);
CREATE VIRTUAL TABLE IF NOT EXISTS wa_fts USING fts5(
    texto, contato, content='wa_msgs', content_rowid='rowid', tokenize='{TOKENIZER}');
CREATE TRIGGER IF NOT EXISTS wa_ai AFTER INSERT ON wa_msgs BEGIN
    INSERT INTO wa_fts(rowid, texto, contato) VALUES (new.rowid, new.texto, new.contato);
END;
CREATE TRIGGER IF NOT EXISTS wa_ad AFTER DELETE ON wa_msgs BEGIN
    INSERT INTO wa_fts(wa_fts, rowid, texto, contato) VALUES ('delete', old.rowid, old.texto, old.contato);
END;

CREATE TABLE IF NOT EXISTS cache_classif(
    canal TEXT, chave TEXT, categoria TEXT, resultado TEXT, confirmacoes INTEGER DEFAULT 1,
    usos INTEGER DEFAULT 0, criado TEXT, confirmado TEXT, usado TEXT, PRIMARY KEY(canal, chave));
"""


# ══════════════════════════════════════════════════════════════
#  Conexão
# ══════════════════════════════════════════════════════════════

def db_path() -> Optional[Path]:
    """ALFRED_DB, ou $VAULT_ALFRED/.alfred/alfred.db; None se nenhum dos dois estiver definido."""
    explicito = os.getenv("ALFRED_DB", "").strip()
    if explicito:
        return Path(explicito)
    v = os.getenv("VAULT_ALFRED", "").strip()
    return Path(v) / ".alfred" / "alfred.db" if v else None


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
    except sqlite3.Error as e:
        log(f"[db] não consegui abrir {p}: {e}")
        return None


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fts_query(texto: str, modo: str = "and") -> str:
    """
    Texto livre → consulta FTS5 segura. Cada termo vai entre aspas (nada de erro de sintaxe com - : " *);
    termos de 4+ letras casam por prefixo, compensando a falta de stemming ("pendência" acha "pendências").
    modo = "and" (todos os termos) | "or" (qualquer um).
    """
    termos = re.findall(r"\w+", texto or "", flags=re.UNICODE)
    partes = [f'"{t}"*' if len(t) >= 4 else f'"{t}"' for t in termos]
    return (" OR " if modo == "or" else " ").join(partes)


def _consultar(con: sqlite3.Connection, sql: str, query: str, extra: tuple = ()) -> list[sqlite3.Row]:
    """Roda `sql` (primeiro parâmetro = MATCH) com AND e, se vier vazio, com OR."""
    for modo in ("and", "or"):
        q = _fts_query(query, modo)
        if not q:
            return []
        rows = con.execute(sql, (q, *extra)).fetchall()
        if rows or " " not in q:
            return rows
    return []


# ══════════════════════════════════════════════════════════════
#  1. Índice do cofre
# ══════════════════════════════════════════════════════════════

def _titulo(caminho: Path, corpo: str) -> str:
    m = re.search(r"^#\s+(.+)$", corpo, flags=re.MULTILINE)
    h1 = m.group(1).strip() if m else ""
    return caminho.stem if not h1 or h1 == caminho.stem else f"{caminho.stem} {h1}"


def indexar_cofre(vault: Path, nome: str) -> dict:
    """
    Indexa os .md de `vault` sob o rótulo `nome` ("alfred", "pedro"...). Incremental por mtime;
    ignora pastas com ponto (.obsidian, .trash, .alfred). Retorna {novos, atualizados, removidos, total}.
    """
    res = {"novos": 0, "atualizados": 0, "removidos": 0, "total": 0}
    vault = Path(vault)
    if not vault.is_dir():
        log(f"[db] cofre '{nome}' não encontrado: {vault}")
        return res
    con = _conectar()
    if not con:
        return res
    try:
        with closing(con):
            conhecidos = {r["caminho"]: (r["mtime"], r["fts_rowid"]) for r in
                          con.execute("SELECT caminho, mtime, fts_rowid FROM notas_meta WHERE cofre=?", (nome,))}
            vistos = set()
            with con:
                for raiz, dirs, arquivos in os.walk(vault):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for a in arquivos:
                        if not a.endswith(".md"):
                            continue
                        f = Path(raiz) / a
                        rel = f.relative_to(vault).as_posix()
                        vistos.add(rel)
                        try:
                            mtime = f.stat().st_mtime
                        except OSError:
                            continue
                        antigo = conhecidos.get(rel)
                        if antigo and antigo[0] == mtime:
                            continue
                        try:
                            corpo = f.read_text(encoding="utf-8", errors="ignore")
                        except OSError:
                            continue
                        if antigo:
                            con.execute("DELETE FROM notas WHERE rowid=?", (antigo[1],))
                            res["atualizados"] += 1
                        else:
                            res["novos"] += 1
                        rid = con.execute("INSERT INTO notas(titulo, corpo, caminho, cofre) VALUES (?,?,?,?)",
                                          (_titulo(f, corpo), corpo, rel, nome)).lastrowid
                        con.execute("INSERT OR REPLACE INTO notas_meta VALUES (?,?,?,?)", (nome, rel, mtime, rid))
                for rel, (_, rid) in conhecidos.items():
                    if rel not in vistos:
                        con.execute("DELETE FROM notas WHERE rowid=?", (rid,))
                        con.execute("DELETE FROM notas_meta WHERE cofre=? AND caminho=?", (nome, rel))
                        res["removidos"] += 1
            res["total"] = con.execute("SELECT COUNT(*) FROM notas_meta WHERE cofre=?", (nome,)).fetchone()[0]
    except sqlite3.Error as e:
        log(f"[db] indexar_cofre('{nome}') falhou: {e}")
    return res


def buscar_notas(query: str, cofre: Optional[str] = None, pasta: str = "", limite: int = 5) -> list[dict]:
    """[{cofre, caminho, titulo, trecho, score}] por relevância (bm25; título pesa 8x). [] se sem índice."""
    con = _conectar()
    if not con:
        return []
    sql = ("SELECT cofre, caminho, titulo, snippet(notas, 1, '', '', ' … ', 24) AS trecho, "
           "bm25(notas, 8.0, 1.0) AS score FROM notas WHERE notas MATCH ?")
    extra: list = []
    if cofre:
        sql += " AND cofre = ?"
        extra.append(cofre)
    if pasta:
        sql += " AND caminho LIKE ? ESCAPE '\\'"
        extra.append(pasta.strip("/").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%")
    sql += " ORDER BY score LIMIT ?"
    extra.append(int(limite))
    try:
        with closing(con):
            return [{"cofre": r["cofre"], "caminho": r["caminho"], "titulo": r["titulo"],
                     "trecho": (r["trecho"] or "").replace("\n", " ").strip(), "score": round(-r["score"], 2)}
                    for r in _consultar(con, sql, query, tuple(extra))]
    except sqlite3.Error as e:
        log(f"[db] buscar_notas falhou: {e}")
        return []


# ══════════════════════════════════════════════════════════════
#  2. Histórico do WhatsApp
# ══════════════════════════════════════════════════════════════

def registrar_wa(message_id: str, remote_jid: str, contato: str, texto: str, *, grupo: bool = False,
                 from_me: bool = False, tipo: str = "texto", ts: Optional[str] = None) -> bool:
    """Grava uma mensagem (texto ou transcrição). Idempotente por message_id. False se nada foi gravado."""
    texto = (texto or "").strip()
    if not texto or not message_id or not remote_jid:
        return False
    con = _conectar()
    if not con:
        return False
    try:
        with closing(con), con:
            cur = con.execute("INSERT OR IGNORE INTO wa_msgs VALUES (?,?,?,?,?,?,?,?,?)",
                              (message_id, remote_jid, contato or "", int(grupo), int(from_me), tipo,
                               ts or _agora(), _agora(), texto))
            return cur.rowcount > 0
    except sqlite3.Error as e:
        log(f"[db] registrar_wa falhou: {e}")
        return False


def _hora(ts: str) -> str:
    try:
        return datetime.fromisoformat(str(ts)).strftime("%d/%m %H:%M")
    except ValueError:
        return str(ts)[:16]


def historico_contato(remote_jid: str, n: int = 5, excluir=(), max_chars: int = 1200) -> str:
    """Últimas `n` mensagens da conversa, em ordem cronológica: '[dd/mm HH:MM] Contato: texto'. '' se não há."""
    con = _conectar()
    if not con or not remote_jid:
        return ""
    excluir = [e for e in excluir if e]
    sql = "SELECT contato, from_me, ts, texto FROM wa_msgs WHERE remote_jid=?"
    if excluir:
        sql += f" AND message_id NOT IN ({','.join('?' * len(excluir))})"
    sql += " ORDER BY rowid DESC LIMIT ?"
    try:
        with closing(con):
            rows = con.execute(sql, (remote_jid, *excluir, int(n))).fetchall()
    except sqlite3.Error as e:
        log(f"[db] historico_contato falhou: {e}")
        return ""
    linhas = [f"[{_hora(r['ts'])}] {'Pedro' if r['from_me'] else (r['contato'] or 'Contato')}: "
              f"{' '.join(r['texto'].split())[:400]}" for r in reversed(rows)]
    saida = "\n".join(linhas)
    return saida[-max_chars:] if len(saida) > max_chars else saida


def buscar_wa(query: str, jid: Optional[str] = None, limite: int = 10) -> list[dict]:
    """[{message_id, remote_jid, contato, from_me, ts, trecho}] por relevância; `jid` restringe a uma conversa."""
    con = _conectar()
    if not con:
        return []
    sql = ("SELECT w.message_id, w.remote_jid, w.contato, w.from_me, w.ts, "
           "snippet(wa_fts, 0, '', '', ' … ', 24) AS trecho "
           "FROM wa_fts JOIN wa_msgs w ON w.rowid = wa_fts.rowid WHERE wa_fts MATCH ?")
    extra: list = []
    if jid:
        sql += " AND w.remote_jid = ?"
        extra.append(jid)
    sql += " ORDER BY bm25(wa_fts) LIMIT ?"
    extra.append(int(limite))
    try:
        with closing(con):
            return [{"message_id": r["message_id"], "remote_jid": r["remote_jid"], "contato": r["contato"],
                     "from_me": bool(r["from_me"]), "ts": r["ts"], "trecho": r["trecho"]}
                    for r in _consultar(con, sql, query, tuple(extra))]
    except sqlite3.Error as e:
        log(f"[db] buscar_wa falhou: {e}")
        return []


def podar_wa(dias: float) -> int:
    """Apaga mensagens gravadas há mais de `dias` dias (dias <= 0 = não apaga). Retorna quantas saíram."""
    if dias is None or float(dias) <= 0:
        return 0
    con = _conectar()
    if not con:
        return 0
    corte = (datetime.now() - timedelta(days=float(dias))).isoformat(timespec="seconds")
    try:
        with closing(con), con:
            return con.execute("DELETE FROM wa_msgs WHERE gravado < ?", (corte,)).rowcount
    except sqlite3.Error as e:
        log(f"[db] podar_wa falhou: {e}")
        return 0


# ══════════════════════════════════════════════════════════════
#  3. Cache de classificação
# ══════════════════════════════════════════════════════════════

def cache_get(canal: str, chave: str, ttl_dias: float = CACHE_TTL_DIAS,
              minimo: int = MINIMO_CONFIRMACOES) -> Optional[dict]:
    """Resultado guardado, se o LLM já deu a mesma categoria `minimo` vezes e a última confirmação é recente."""
    con = _conectar()
    if not con:
        return None
    try:
        with closing(con), con:
            r = con.execute("SELECT resultado, confirmacoes, confirmado FROM cache_classif WHERE canal=? AND chave=?",
                            (canal, chave)).fetchone()
            if not r or r["confirmacoes"] < minimo:
                return None
            if datetime.fromisoformat(r["confirmado"]) < datetime.now() - timedelta(days=ttl_dias):
                return None
            con.execute("UPDATE cache_classif SET usos=usos+1, usado=? WHERE canal=? AND chave=?",
                        (_agora(), canal, chave))
            return json.loads(r["resultado"])
    except (sqlite3.Error, ValueError) as e:
        log(f"[db] cache_get falhou: {e}")
        return None


def cache_put(canal: str, chave: str, resultado: dict, cacheavel: bool = True) -> None:
    """
    Registra a resposta do LLM. Mesma categoria da anterior → +1 confirmação; categoria nova → recomeça em 1.
    `cacheavel=False` (categoria acionável, classificador mudo...) apaga a entrada.
    """
    con = _conectar()
    if not con:
        return
    try:
        with closing(con), con:
            if not cacheavel:
                con.execute("DELETE FROM cache_classif WHERE canal=? AND chave=?", (canal, chave))
                return
            cat = str(resultado.get("categoria", ""))
            agora = _agora()
            r = con.execute("SELECT categoria, confirmacoes FROM cache_classif WHERE canal=? AND chave=?",
                            (canal, chave)).fetchone()
            if r and r["categoria"] == cat:
                con.execute("UPDATE cache_classif SET resultado=?, confirmacoes=?, confirmado=? WHERE canal=? AND chave=?",
                            (json.dumps(resultado, ensure_ascii=False), r["confirmacoes"] + 1, agora, canal, chave))
            else:
                con.execute("INSERT OR REPLACE INTO cache_classif VALUES (?,?,?,?,1,0,?,?,NULL)",
                            (canal, chave, cat, json.dumps(resultado, ensure_ascii=False), agora, agora))
    except sqlite3.Error as e:
        log(f"[db] cache_put falhou: {e}")


def estatisticas() -> dict:
    """Contagens para /health e para medir a economia do cache."""
    con = _conectar()
    if not con:
        return {}
    try:
        with closing(con):
            q = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
            return {"db": str(db_path()), "notas": q("SELECT COUNT(*) FROM notas_meta"),
                    "wa_msgs": q("SELECT COUNT(*) FROM wa_msgs"),
                    "cache_entradas": q("SELECT COUNT(*) FROM cache_classif"),
                    "cache_ativas": q(f"SELECT COUNT(*) FROM cache_classif WHERE confirmacoes >= {MINIMO_CONFIRMACOES}"),
                    "cache_usos": q("SELECT COALESCE(SUM(usos),0) FROM cache_classif")}
    except sqlite3.Error as e:
        log(f"[db] estatisticas falhou: {e}")
        return {}
