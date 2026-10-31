"""
Alfred — Conversa: o motor do chat com o Alfred (Fase 1 de docs/planos/chat-alfred.md; a aba do Painel é a Fase 3).

Um turno:
    enviar(conversa_id, texto)            grava a mensagem de Pedro e põe o turno na fila do chat: um executor
        ↓                                 próprio, 1 worker, fora do threadpool do FastAPI (em 31/10 uma rajada
        ↓                                 de webhooks esgotou o threadpool e travou a API)
    montar_contexto()                     personalidade (docs/alfred-system-prompt.md) + bloco "Agora" + memória
        ↓                                 + RAG (só pela heurística _quer_rag) + resumo + histórico
    Ollama /api/chat em stream, tools=esquemas(), think: false, num_ctx de perfis.yml → conversa:
        │ texto ──────────────────► evento "texto"
        │ ferramenta segura ──────► executar() → evento "ferramenta" (estado rodando → ok | erro)
        │ ferramenta perigosa ────► evento "aprovar" → o turno pausa até aprovar(id) / negar(id); sem resposta
        │                           em APROVACAO_S (5 min) a ação é cancelada
        └ repete até a resposta final; depois de MAX_VOLTAS voltas de ferramenta, a chamada seguinte vai sem tools
    grava a resposta → evento "fim"       (ou "erro"); parar(turno_id) interrompe e grava o que já tinha chegado

Eventos: dicts {"tipo", "turno", "n", ...} em turno.fila (queue.Queue) — a Fase 3 serve isso por SSE. O último
evento de um turno é sempre "fim" ou "erro".

GPU: enquanto houver turno na fila ou rodando, ocupado() é True e whatsapp.processar_fila segura o próximo item
(não descarta: ele sai na volta seguinte da thread da fila). Vale só dentro do mesmo processo — o CLI
(`run.sh conversa`) não segura a fila da API.

Histórico: as mensagens de Pedro e as respostas do Alfred depois do resumo vão inteiras (de HISTORICO = 12 até
HISTORICO + RESUMO_A_CADA = 32). Passou disso, o resumo é refeito (LLM, formato de máquina) cobrindo todas menos
as 12 últimas — ou seja, a cada ~20 mensagens. Chamadas e resultados de ferramentas de turnos anteriores não
voltam ao contexto: a resposta daquele turno já diz o que importava.

Persistência: VAULT_ALFRED/conversas.db (SQLite + FTS5) — conversas, mensagens (papel user | assistant | tool,
ferramenta, argumentos, resultado, modelo, tokens, tempo) e aprovacoes. Cada gravação é uma transação.
"""

import json
import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from _shared import lib_ferramentas, lib_memoria, lib_rag  # noqa: E402
from _shared.lib_alfred import DEFAULT_MODEL_CHAT, DEFAULT_OLLAMA_URL, log, ollama_generate  # noqa: E402
from _shared.lib_titulo import extrair_titulo_manchete  # noqa: E402

import core  # noqa: E402
import agenda  # noqa: E402

PROMPT_FILE = Path(__file__).resolve().parent.parent.parent / "docs" / "alfred-system-prompt.md"
MAX_VOLTAS = 6            # voltas de ferramenta por turno; a chamada seguinte vai sem tools e tem de responder
APROVACAO_S = 300         # ferramenta perigosa sem clique de Pedro nisso → cancelada
HISTORICO = 12            # mensagens que ficam inteiras quando o resumo é refeito
RESUMO_A_CADA = 20        # mensagens novas fora do resumo (além das 12) que disparam um resumo novo
RAG_CHARS = 1500          # teto do bloco de RAG automático
NUM_CTX_PADRAO = 16384
TIMEOUT_OLLAMA = 300      # s sem chegar nada do stream (carregar o gemma4:12b leva ~12 s)
GUARDAR_TURNO_S = 1800    # turno terminado continua em obter_turno() por isso (o SSE da Fase 3 pode reconectar)
WA_CACHE_S = 60           # estado do WhatsApp no bloco "Agora": no máximo uma consulta à Evolution por minuto
WA_TIMEOUT_S = 1.5        # ...e curta: Evolution lenta vira "sem resposta", não atraso no turno
TOKENIZER = "unicode61 remove_diacritics 2"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS conversas(
    id TEXT PRIMARY KEY, titulo TEXT NOT NULL DEFAULT '', criada_em TEXT NOT NULL, atualizada_em TEXT NOT NULL,
    resumo TEXT NOT NULL DEFAULT '', resumo_ate INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS mensagens(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversa_id TEXT NOT NULL REFERENCES conversas(id) ON DELETE CASCADE,
    turno_id TEXT, papel TEXT NOT NULL CHECK (papel IN ('user', 'assistant', 'tool')),
    texto TEXT NOT NULL DEFAULT '', ferramenta TEXT, argumentos TEXT, resultado TEXT,
    modelo TEXT, tokens_entrada INTEGER, tokens_saida INTEGER, tempo_ms INTEGER, criada_em TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mensagens_conversa ON mensagens(conversa_id, id);
CREATE VIRTUAL TABLE IF NOT EXISTS mensagens_fts USING fts5(
    texto, content='mensagens', content_rowid='id', tokenize='{TOKENIZER}');
CREATE TRIGGER IF NOT EXISTS mensagens_ai AFTER INSERT ON mensagens BEGIN
    INSERT INTO mensagens_fts(rowid, texto) VALUES (new.id, new.texto);
END;
CREATE TRIGGER IF NOT EXISTS mensagens_ad AFTER DELETE ON mensagens BEGIN
    INSERT INTO mensagens_fts(mensagens_fts, rowid, texto) VALUES ('delete', old.id, old.texto);
END;
CREATE TABLE IF NOT EXISTS aprovacoes(
    id TEXT PRIMARY KEY, conversa_id TEXT NOT NULL REFERENCES conversas(id) ON DELETE CASCADE,
    turno_id TEXT NOT NULL, ferramenta TEXT NOT NULL, argumentos TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('aguardando', 'aprovada', 'negada', 'expirada', 'cancelada')),
    criada_em TEXT NOT NULL, decidida_em TEXT, resultado TEXT);
"""
_COLUNAS = ("ferramenta", "argumentos", "resultado", "modelo", "tokens_entrada", "tokens_saida", "tempo_ms")

# o que o modelo recebe no lugar do resultado de uma ferramenta perigosa que não rodou
_NAO_EXECUTADA = {
    "negada":    "[não executada] Pedro negou a execução de {nome}. Nada foi feito.",
    "expirada":  "[não executada] Pedro não aprovou {nome} em 5 minutos; a ação foi cancelada. Nada foi feito.",
    "cancelada": "[não executada] o turno foi interrompido antes de {nome} rodar. Nada foi feito.",
}


# ══════════════════════════════════════════════════════════════
#  Configuração e banco
# ══════════════════════════════════════════════════════════════

def _cfg() -> dict:
    """Seção conversa: do perfis.yml (opcional) com defaults. modelo vazio = MODEL_CHAT."""
    try:
        from _shared.perfis import load
        sec = load().get("conversa") or {}
    except Exception:
        sec = {}
    return {
        "modelo":             str(sec.get("modelo") or "").strip()
                              or os.getenv("MODEL_CHAT", "").strip() or DEFAULT_MODEL_CHAT,
        "modelo_ferramentas": str(sec.get("modelo_ferramentas") or "").strip(),
        "num_ctx":            int(sec.get("num_ctx") or NUM_CTX_PADRAO),
    }


def db_path() -> Path:
    return core.vault() / "conversas.db"


def _conectar() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(_SCHEMA)
    return con


def _agora() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _curto(e) -> str:
    texto = " ".join(str(e).split()) or type(e).__name__
    return texto if len(texto) <= 300 else texto[:299] + "…"


def _gravar_mensagem(con: sqlite3.Connection, conversa_id: str, papel: str, texto: str = "",
                     turno_id: Optional[str] = None, **extra) -> int:
    """Insere uma mensagem e atualiza a conversa. Chame dentro de `with con:` (uma transação)."""
    agora = _agora()
    cur = con.execute(
        f"INSERT INTO mensagens(conversa_id, turno_id, papel, texto, {', '.join(_COLUNAS)}, criada_em)"
        f" VALUES (?, ?, ?, ?, {', '.join('?' * len(_COLUNAS))}, ?)",
        (conversa_id, turno_id, papel, texto or "", *(extra.get(c) for c in _COLUNAS), agora))
    con.execute("UPDATE conversas SET atualizada_em = ? WHERE id = ?", (agora, conversa_id))
    return cur.lastrowid


def _titulo(pergunta: str) -> str:
    """Título da conversa pela heurística do lib_titulo (sem LLM: não disputa a GPU com o turno)."""
    linha = " ".join(pergunta.split())
    return extrair_titulo_manchete(pergunta, fallback=linha[:60] or "Conversa")[:80]


def nova_conversa(titulo: str = "") -> dict:
    cid = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    agora = _agora()
    with closing(_conectar()) as con, con:
        con.execute("INSERT INTO conversas(id, titulo, criada_em, atualizada_em) VALUES (?, ?, ?, ?)",
                    (cid, titulo, agora, agora))
    return {"id": cid, "titulo": titulo, "criada_em": agora, "atualizada_em": agora, "resumo": "", "resumo_ate": 0}


def obter_conversa(conversa_id: str) -> Optional[dict]:
    with closing(_conectar()) as con:
        r = con.execute("SELECT * FROM conversas WHERE id = ?", (conversa_id,)).fetchone()
    return dict(r) if r else None


def listar_conversas(limite: int = 50) -> list[dict]:
    with closing(_conectar()) as con:
        return [dict(r) for r in con.execute(
            "SELECT id, titulo, criada_em, atualizada_em FROM conversas ORDER BY atualizada_em DESC, rowid DESC"
            " LIMIT ?",
            (limite,))]


def mensagens(conversa_id: str) -> list[dict]:
    with closing(_conectar()) as con:
        return [dict(r) for r in con.execute("SELECT * FROM mensagens WHERE conversa_id = ? ORDER BY id",
                                             (conversa_id,))]


def buscar(consulta: str, limite: int = 20) -> list[dict]:
    """FTS5 nas mensagens de Pedro e do Alfred → [{conversa_id, titulo, mensagem_id, papel, trecho}]."""
    palavras = list(dict.fromkeys(re.findall(r"\w{2,}", (consulta or "").lower())))[:20]
    termos = " ".join(f'"{p}"*' for p in palavras)   # todas as palavras, por prefixo
    if not termos:
        return []
    with closing(_conectar()) as con:
        return [dict(r) for r in con.execute(
            "SELECT m.conversa_id, c.titulo, m.id AS mensagem_id, m.papel,"
            " snippet(mensagens_fts, 0, '[', ']', '…', 12) AS trecho"
            " FROM mensagens_fts JOIN mensagens m ON m.id = mensagens_fts.rowid"
            " JOIN conversas c ON c.id = m.conversa_id"
            " WHERE mensagens_fts MATCH ? ORDER BY bm25(mensagens_fts) LIMIT ?", (termos, limite))]


# ══════════════════════════════════════════════════════════════
#  Contexto: personalidade, bloco "Agora", memória, RAG, histórico
# ══════════════════════════════════════════════════════════════

_DIAS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
_DIAS_CURTOS = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
_MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
          "novembro", "dezembro")


def _personalidade() -> str:
    """docs/alfred-system-prompt.md como está, sem a linha /no_think (o payload leva think: false). A revisão do
    texto para a pilha nova é a Fase 4."""
    try:
        texto = PROMPT_FILE.read_text(encoding="utf-8")
    except OSError as e:
        log(f"[conversa] {PROMPT_FILE.name} ilegível ({e}) — personalidade mínima")
        return "Você é Alfred Pennyworth, assistente pessoal de Pedro Netto. Responda em português brasileiro."
    return "\n".join(l for l in texto.splitlines() if l.strip() != "/no_think").strip()


def _data(valor) -> Optional[date]:
    """date do YAML, "AAAA-MM-DD[...]" ou "DD/MM/AAAA" → date; o resto → None."""
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    txt = str(valor or "").strip()
    try:
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", txt)
        return date(int(m[3]), int(m[2]), int(m[1])) if m else date.fromisoformat(txt[:10])
    except ValueError:
        return None


def _agenda_48h(agora: datetime) -> str:
    fim = agora + timedelta(hours=48)
    itens = []
    for i in agenda.listar():
        if i.get("status") in ("desfeito", "cancelado", "desfazer"):
            continue
        try:
            if i.get("dia_inteiro"):
                d = date.fromisoformat(str(i.get("inicio"))[:10])
                if not agora.date() <= d <= fim.date():
                    continue
                chave, quando = datetime(d.year, d.month, d.day, tzinfo=agora.tzinfo), \
                    f"{_DIAS_CURTOS[d.weekday()]} {d:%d/%m}, dia inteiro"
            else:
                ini = datetime.fromisoformat(str(i.get("inicio")))
                ini = ini if ini.tzinfo else ini.replace(tzinfo=agora.tzinfo)
                if not agora <= ini <= fim:
                    continue
                chave, quando = ini, f"{_DIAS_CURTOS[ini.weekday()]} {ini:%d/%m %H:%M}"
        except (TypeError, ValueError):
            continue
        local = f" ({i['local']})" if i.get("local") else ""
        itens.append((chave, f"- {quando} — {i.get('titulo')}{local}"))
    if not itens:
        return "Compromissos nas próximas 48 h: nenhum."
    itens.sort(key=lambda x: x[0])
    return "Compromissos nas próximas 48 h:\n" + "\n".join(linha for _, linha in itens[:8])


def _tarefas_hoje(agora: datetime) -> str:
    f = core.vault() / "tasks.yml"
    dados = yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else None
    tarefas = dados.get("tasks") if isinstance(dados, dict) else None
    hoje, itens = agora.date(), []
    for t in tarefas if isinstance(tarefas, list) else []:
        if not isinstance(t, dict) or t.get("status") != "open":
            continue
        venc = _data(t.get("due_date")) if t.get("due_date") else None
        if venc is None or venc > hoje:
            continue
        quando = "hoje" if venc == hoje else f"venceu em {venc:%d/%m} ({(hoje - venc).days} dia(s) atrás)"
        itens.append((venc, f"- #{t.get('id')} {t.get('title')} — {quando}"))
    if not itens:
        return "Tarefas vencidas ou para hoje: nenhuma."
    itens.sort(key=lambda x: x[0])
    resto = f"\n- (mais {len(itens) - 8})" if len(itens) > 8 else ""
    return "Tarefas vencidas ou para hoje:\n" + "\n".join(linha for _, linha in itens[:8]) + resto


def _ts(valor) -> float:
    try:
        return datetime.fromisoformat(str(valor)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _pendencias(agora: datetime) -> str:
    """Total aguardando + as 3 mais urgentes, na ordem do Painel: urgência alta primeiro, depois as mais recentes."""
    pend = core.listar_pendencias("aguardando")
    if not pend:
        return "Pendências aguardando Pedro: nenhuma."
    pend = sorted(pend, key=lambda p: (0 if (p.get("whatsapp") or {}).get("urgencia") == "alta" else 1,
                                       -_ts((p.get("whatsapp") or {}).get("recebido_em") or p.get("criado_em"))))
    linhas = [f"Pendências aguardando Pedro: {len(pend)}. As mais urgentes:"]
    for p in pend[:3]:
        wa, ev = p.get("whatsapp") or {}, p.get("evento") or {}
        if p.get("tipo") == "whatsapp":
            resumo = " ".join(str(ev.get("resumo") or wa.get("texto") or "").split())
            urg = " (urgente)" if wa.get("urgencia") == "alta" else ""
            linhas.append(f"- [{p.get('id')}] WhatsApp · {wa.get('contato') or p.get('titulo')}{urg}: {resumo[:100]}")
        else:
            data_ev = f" — {ev['data_inicio']}" if ev.get("data_inicio") else ""
            linhas.append(f"- [{p.get('id')}] {p.get('tipo') or 'e-mail'} · {p.get('titulo')}{data_ev}")
    return "\n".join(linhas)


_WA_CACHE: dict = {}


def _whatsapp(agora: datetime) -> str:
    from _shared import lib_whatsapp
    if not lib_whatsapp.configurado():
        return "WhatsApp: modo simulado (Evolution API não configurada; nada sai da máquina)."
    c = _WA_CACHE.get("estado")
    if c and time.monotonic() - c[0] < WA_CACHE_S:
        return c[1]
    est = lib_whatsapp.estado_instancia(timeout=WA_TIMEOUT_S)
    estado = est.get("estado") if est.get("ok") else None
    texto = ("WhatsApp: conectado." if estado == "open" else "WhatsApp: conectando." if estado == "connecting"
             else f"WhatsApp: desconectado ({estado})." if estado else "WhatsApp: a Evolution API não respondeu agora.")
    _WA_CACHE["estado"] = (time.monotonic(), texto)
    return texto


def bloco_agora(agora: Optional[datetime] = None) -> str:
    """Bloco curto (~400 tokens) com data/hora, agenda de 48 h, tarefas, pendências e WhatsApp. Uma parte que
    falha some do bloco (com log), sem derrubar o turno."""
    agora = agora or datetime.now().astimezone()
    linhas = [f"## Agora\n{_DIAS[agora.weekday()]}, {agora.day} de {_MESES[agora.month - 1]} de {agora.year}, "
              f"{agora:%H:%M}."]
    for parte in (_agenda_48h, _tarefas_hoje, _pendencias, _whatsapp):
        try:
            linhas.append(parte(agora))
        except Exception as e:
            log(f"[conversa] bloco Agora: {parte.__name__} falhou ({_curto(e)})")
    return "\n".join(l for l in linhas if l)


_RE_RAG = re.compile(
    r"\b(minhas? (notas?|anotac\w*|resumos?|pesquisas?)|anotei|anotac\w*|cofres?|vault|obsidian|estudei|"
    r"(no|do) curso|(na|da) aula|materia|apostila|certificac\w*|o que (eu )?(escrevi|anotei)|"
    r"(voce|alfred) (ja )?pesquis\w*)\b")


def _quer_rag(pergunta: str) -> bool:
    """
    Heurística: a pergunta é claramente sobre o conteúdo dos cofres (notas, estudos, cursos, pesquisas salvas)?
    Só então lib_rag.contexto entra sozinho no contexto; no resto o modelo usa buscar_cofre / ler_nota se precisar,
    sem gastar contexto à toa. Isolada de propósito: o Laya assume esta decisão depois (Fase 5).
    """
    return bool(_RE_RAG.search(core._ascii(pergunta or "").lower()))


def _historico(conversa_id: str, ate_id: Optional[int] = None) -> tuple[str, list[dict]]:
    """(resumo, mensagens de Pedro/Alfred depois do resumo e antes de `ate_id`) — no máximo HISTORICO + RESUMO_A_CADA
    (se um resumo falhou, as mais antigas ficam de fora até o próximo dar certo)."""
    with closing(_conectar()) as con:
        conv = con.execute("SELECT resumo, resumo_ate FROM conversas WHERE id = ?", (conversa_id,)).fetchone()
        if not conv:
            return "", []
        rows = con.execute(
            "SELECT id, papel, texto FROM mensagens WHERE conversa_id = ? AND papel IN ('user', 'assistant')"
            " AND texto != '' AND id > ? AND id < ? ORDER BY id",
            (conversa_id, conv["resumo_ate"], ate_id or 2 ** 62)).fetchall()
    return conv["resumo"], [dict(r) for r in rows][-(HISTORICO + RESUMO_A_CADA):]


def montar_contexto(conversa_id: str, pergunta: str, ate_id: Optional[int] = None) -> list[dict]:
    """
    Mensagens do /api/chat: [system, histórico..., user]. O system junta personalidade, bloco "Agora", memória
    (lib_memoria.contexto), RAG (só se _quer_rag) e o resumo das mensagens antigas. `ate_id` = id da mensagem de
    Pedro deste turno (o histórico pega só as anteriores a ela).
    """
    partes = [_personalidade(), bloco_agora()]
    try:
        partes.append(lib_memoria.contexto(pergunta))
    except Exception as e:
        log(f"[conversa] memória fora do contexto ({_curto(e)})")
    if _quer_rag(pergunta):
        try:
            partes.append(lib_rag.contexto(pergunta, RAG_CHARS))
        except Exception as e:
            log(f"[conversa] RAG fora do contexto ({_curto(e)})")
    resumo, historico = _historico(conversa_id, ate_id)
    if resumo:
        partes.append(f"## Resumo da conversa até aqui\n{resumo}")
    msgs = [{"role": "system", "content": "\n\n".join(p for p in partes if p)}]
    msgs += [{"role": m["papel"], "content": m["texto"]} for m in historico]
    msgs.append({"role": "user", "content": pergunta})
    return msgs


def _resumir(anterior: str, novas: list[dict], modelo: str, num_ctx: int) -> str:
    linhas = [f"{'Pedro' if m['papel'] == 'user' else 'Alfred'}: {' '.join(m['texto'].split())[:1500]}"
              for m in novas]
    prompt = ("Atualize o resumo de uma conversa entre Pedro e o assistente Alfred. O resumo é só para o próprio "
              "Alfred lembrar do contexto nas próximas mensagens: formato de máquina, tópicos curtos, sem floreio. "
              "Guarde assuntos tratados, fatos e números citados, pedidos e decisões de Pedro e o que ficou "
              "pendente. Máximo de 1200 caracteres. Responda só com o resumo.\n\n"
              f"RESUMO ANTERIOR:\n{anterior or '(nenhum)'}\n\nMENSAGENS NOVAS:\n" + "\n".join(linhas))
    # mesmo modelo e num_ctx do chat: o Ollama não recarrega o modelo para resumir
    return ollama_generate(prompt, model=modelo, think=False, timeout=180,
                           options={"num_ctx": num_ctx, "temperature": 0.2, "num_predict": 700}).strip()[:2000]


def _atualizar_resumo(conversa_id: str, modelo: str, num_ctx: int) -> bool:
    """Mensagens fora do resumo passaram de HISTORICO + RESUMO_A_CADA → resumo novo cobrindo todas menos as
    HISTORICO últimas. True se gravou."""
    with closing(_conectar()) as con:
        conv = con.execute("SELECT resumo, resumo_ate FROM conversas WHERE id = ?", (conversa_id,)).fetchone()
        rows = [dict(r) for r in con.execute(
            "SELECT id, papel, texto FROM mensagens WHERE conversa_id = ? AND papel IN ('user', 'assistant')"
            " AND texto != '' AND id > ? ORDER BY id", (conversa_id, conv["resumo_ate"] if conv else 0))]
    if not conv or len(rows) <= HISTORICO + RESUMO_A_CADA:
        return False
    cobrir = rows[:-HISTORICO]
    novo = _resumir(conv["resumo"], cobrir, modelo, num_ctx)
    if not novo:
        log(f"[conversa] resumo de {conversa_id} falhou — fica para o próximo turno")
        return False
    with closing(_conectar()) as con, con:
        con.execute("UPDATE conversas SET resumo = ?, resumo_ate = ? WHERE id = ? AND resumo_ate = ?",
                    (novo, cobrir[-1]["id"], conversa_id, conv["resumo_ate"]))
    log(f"[conversa] resumo de {conversa_id} refeito: {len(cobrir)} mensagens, {len(novo)} caracteres")
    return True


# ══════════════════════════════════════════════════════════════
#  Turnos: fila própria, eventos, aprovação, parar
# ══════════════════════════════════════════════════════════════

class Turno:
    """Um pedido de Pedro e a resposta do Alfred. Estado: na_fila → rodando ⇄ aguardando_aprovacao →
    fim | parado | erro."""

    def __init__(self, conversa_id: str, pergunta: str, modelo: str):
        self.id = secrets.token_hex(8)
        self.conversa_id, self.pergunta, self.modelo = conversa_id, pergunta, modelo
        self.mensagem_id: Optional[int] = None
        self.fila: queue.Queue = queue.Queue()
        self.parar = threading.Event()
        self.estado = "na_fila"
        self.terminado: Optional[float] = None
        self._n = 0


class _Aprovacao:
    def __init__(self, turno: Turno, nome: str, argumentos: dict):
        self.id = secrets.token_hex(8)
        self.turno, self.nome, self.argumentos = turno, nome, argumentos
        self.evento = threading.Event()
        self.decisao: Optional[str] = None


_LOCK = threading.Lock()
_TURNOS: dict[str, Turno] = {}
_APROVACOES: dict[str, _Aprovacao] = {}
_ATIVOS = 0
_EXECUTOR: Optional[ThreadPoolExecutor] = None


def ocupado() -> bool:
    """True com turno na fila ou rodando neste processo — whatsapp.processar_fila segura o próximo item."""
    return _ATIVOS > 0


def obter_turno(turno_id: str) -> Optional[Turno]:
    with _LOCK:
        return _TURNOS.get(turno_id)


def _emitir(turno: Turno, tipo: str, **dados) -> None:
    turno._n += 1   # só o worker do turno emite
    turno.fila.put({"tipo": tipo, "turno": turno.id, "n": turno._n, **dados})


def enviar(conversa_id: str, texto: str, modelo: Optional[str] = None) -> Turno:
    """
    Grava a mensagem de Pedro e põe o turno na fila do chat. → Turno (eventos em turno.fila).
    Conversa inexistente → KeyError; texto vazio → ValueError; já há turno em andamento nesta conversa →
    RuntimeError (uma pergunta por vez: a ordem do histórico depende disso).
    """
    global _ATIVOS, _EXECUTOR
    texto = (texto or "").strip()
    if not texto:
        raise ValueError("mensagem vazia")
    with _LOCK:
        if any(t.conversa_id == conversa_id and t.terminado is None for t in _TURNOS.values()):
            raise RuntimeError("já há um turno em andamento nesta conversa")
    turno = Turno(conversa_id, texto, modelo or _cfg()["modelo"])
    with closing(_conectar()) as con, con:
        conv = con.execute("SELECT titulo FROM conversas WHERE id = ?", (conversa_id,)).fetchone()
        if not conv:
            raise KeyError(f"conversa não encontrada: {conversa_id}")
        if not conv["titulo"]:
            con.execute("UPDATE conversas SET titulo = ? WHERE id = ?", (_titulo(texto), conversa_id))
        turno.mensagem_id = _gravar_mensagem(con, conversa_id, "user", texto, turno.id)
    with _LOCK:
        for tid in [tid for tid, t in _TURNOS.items()
                    if t.terminado is not None and time.monotonic() - t.terminado > GUARDAR_TURNO_S]:
            del _TURNOS[tid]
        _TURNOS[turno.id] = turno
        _ATIVOS += 1
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="conversa")
        _EXECUTOR.submit(_rodar, turno)
    return turno


def _decidir(aprovacao_id: str, decisao: str) -> bool:
    """Primeira decisão vence; aprovação desconhecida, já decidida ou expirada → False."""
    with _LOCK:
        ap = _APROVACOES.get(aprovacao_id)
        if not ap or ap.decisao is not None:
            return False
        ap.decisao = decisao
    ap.evento.set()
    return True


def aprovar(aprovacao_id: str) -> bool:
    """Pedro clicou [Executar]: a ferramenta perigosa roda e o turno continua."""
    return _decidir(aprovacao_id, "aprovada")


def negar(aprovacao_id: str) -> bool:
    """Pedro clicou [Não]: nada roda; o modelo é avisado e o turno continua."""
    return _decidir(aprovacao_id, "negada")


def parar(turno_id: str) -> bool:
    """Interrompe o turno: corta o stream na próxima linha, cancela a aprovação pendente e grava o que já veio.
    Uma ferramenta já rodando termina antes (não dá para matá-la). False se o turno não existe ou já acabou."""
    with _LOCK:
        turno = _TURNOS.get(turno_id)
        if not turno or turno.terminado is not None:
            return False
        turno.parar.set()
        pendentes = [a.id for a in _APROVACOES.values() if a.turno is turno]
    for aid in pendentes:
        _decidir(aid, "cancelada")
    return True


def _pedir_aprovacao(turno: Turno, nome: str, argumentos: dict) -> _Aprovacao:
    """Grava a aprovação, emite "aprovar" e espera a decisão (ou APROVACAO_S → expirada)."""
    ap = _Aprovacao(turno, nome, argumentos)
    expira = datetime.now().astimezone() + timedelta(seconds=APROVACAO_S)
    with closing(_conectar()) as con, con:
        con.execute("INSERT INTO aprovacoes(id, conversa_id, turno_id, ferramenta, argumentos, status, criada_em)"
                    " VALUES (?, ?, ?, ?, ?, 'aguardando', ?)",
                    (ap.id, turno.conversa_id, turno.id, nome, json.dumps(argumentos, ensure_ascii=False), _agora()))
    with _LOCK:
        _APROVACOES[ap.id] = ap
    if turno.parar.is_set():   # parar() chegou antes do registro acima
        _decidir(ap.id, "cancelada")
    else:
        turno.estado = "aguardando_aprovacao"
        _emitir(turno, "aprovar", id=ap.id, nome=nome, argumentos=argumentos,
                expira_em=expira.isoformat(timespec="seconds"))
    ap.evento.wait(APROVACAO_S)
    _decidir(ap.id, "expirada")   # sem efeito se Pedro já decidiu
    with _LOCK:
        _APROVACOES.pop(ap.id, None)
    turno.estado = "rodando"
    with closing(_conectar()) as con, con:
        con.execute("UPDATE aprovacoes SET status = ?, decidida_em = ? WHERE id = ?", (ap.decisao, _agora(), ap.id))
    log(f"[conversa] aprovação de {nome}: {ap.decisao}")
    return ap


def _argumentos(bruto) -> dict:
    """Argumentos como o modelo mandou (dict ou texto JSON) → dict para o evento e o banco."""
    if isinstance(bruto, str):
        try:
            bruto = json.loads(bruto) if bruto.strip() else {}
        except ValueError:
            return {"_texto": bruto}
    return bruto if isinstance(bruto, dict) else {"_valor": bruto}


def _usar_ferramenta(turno: Turno, nome: str, bruto, modelo: str) -> str:
    """Segura: executa. Perigosa: pede aprovação antes. → texto para a mensagem role "tool"."""
    argumentos = _argumentos(bruto)
    perigosa = lib_ferramentas.perigosa(nome)
    ap = _pedir_aprovacao(turno, nome, argumentos) if perigosa else None
    if ap and ap.decisao != "aprovada":
        resultado, estado, ms = _NAO_EXECUTADA[ap.decisao].format(nome=nome), ap.decisao, None
    else:
        _emitir(turno, "ferramenta", nome=nome, argumentos=argumentos, estado="rodando", perigosa=perigosa)
        t0 = time.monotonic()
        resultado = lib_ferramentas.executar(nome, bruto)
        ms = int((time.monotonic() - t0) * 1000)
        estado = "erro" if resultado.startswith("[erro]") else "ok"
        log(f"[conversa] {nome}: {estado} em {ms} ms")
    _emitir(turno, "ferramenta", nome=nome, argumentos=argumentos, estado=estado, perigosa=perigosa,
            resultado=resultado, tempo_ms=ms)
    with closing(_conectar()) as con, con:
        _gravar_mensagem(con, turno.conversa_id, "tool", "", turno.id, ferramenta=nome,
                         argumentos=json.dumps(argumentos, ensure_ascii=False), resultado=resultado,
                         modelo=modelo, tempo_ms=ms)
        if ap:
            con.execute("UPDATE aprovacoes SET resultado = ? WHERE id = ?", (resultado, ap.id))
    return resultado


# ══════════════════════════════════════════════════════════════
#  Ollama e o laço do turno
# ══════════════════════════════════════════════════════════════

def _stream_ollama(payload: dict) -> Iterator[dict]:
    """POST /api/chat em stream → um dict por linha. HTTP != 200, {"error"} ou Ollama fora do ar → RuntimeError."""
    url = os.getenv("OLLAMA_BASE_URL", "").strip() or DEFAULT_OLLAMA_URL
    try:
        with httpx.stream("POST", f"{url}/api/chat", json=payload,
                          timeout=httpx.Timeout(TIMEOUT_OLLAMA, connect=5)) as r:
            if r.status_code != 200:
                r.read()
                raise RuntimeError(f"Ollama respondeu {r.status_code}: {r.text[:200]}")
            for linha in r.iter_lines():
                if not linha.strip():
                    continue
                pedaco = json.loads(linha)
                if pedaco.get("error"):
                    raise RuntimeError(f"Ollama: {str(pedaco['error'])[:200]}")
                yield pedaco
    except httpx.ConnectError:
        raise RuntimeError(f"Ollama fora do ar ({url})") from None
    except httpx.TimeoutException:
        raise RuntimeError(f"Ollama não respondeu em {TIMEOUT_OLLAMA} s") from None


def _chamar(turno: Turno, msgs: list[dict], modelo: str, ferramentas: Optional[list], num_ctx: int,
            emitir: bool) -> dict:
    """Uma chamada ao /api/chat. → {texto, tool_calls, entrada, saida, parado}. `emitir`: texto vira evento."""
    payload = {"model": modelo, "messages": msgs, "stream": True, "think": False, "options": {"num_ctx": num_ctx}}
    if ferramentas:
        payload["tools"] = ferramentas
    partes, chamadas, r = [], [], {"entrada": 0, "saida": 0, "parado": False}
    pedacos = _stream_ollama(payload)
    try:
        for pedaco in pedacos:
            if turno.parar.is_set():
                r["parado"] = True
                break
            msg = pedaco.get("message") or {}
            if msg.get("content"):
                partes.append(msg["content"])
                if emitir:
                    _emitir(turno, "texto", texto=msg["content"])
            chamadas += msg.get("tool_calls") or []
            if pedaco.get("done"):
                r["entrada"], r["saida"] = int(pedaco.get("prompt_eval_count") or 0), int(pedaco.get("eval_count") or 0)
    finally:
        pedacos.close()   # parar no meio fecha a conexão: o Ollama para de gerar
    return {**r, "texto": "".join(partes), "tool_calls": chamadas}


def _terminar(turno: Turno, estado: str, tipo: str, **dados) -> None:
    """Último evento do turno ("fim" ou "erro"). Daqui em diante a conversa já aceita a próxima pergunta, mesmo
    com o resumo ainda rodando no worker."""
    with _LOCK:
        turno.estado, turno.terminado = estado, time.monotonic()
    _emitir(turno, tipo, **dados)


def _rodar(turno: Turno) -> None:
    global _ATIVOS
    try:
        _turno(turno)
    except Exception as e:
        log(f"[conversa] turno {turno.id} falhou: {type(e).__name__}: {_curto(e)}")
        _terminar(turno, "erro", "erro", erro=_curto(e))
    finally:
        with _LOCK:
            turno.terminado = turno.terminado or time.monotonic()
            _ATIVOS -= 1


def _turno(turno: Turno) -> None:
    if turno.parar.is_set():   # parado ainda na fila
        _terminar(turno, "parado", "fim", mensagem_id=None, texto="", parado=True, modelo=turno.modelo,
                  tokens_entrada=0, tokens_saida=0, tempo_ms=0, voltas=0)
        return
    turno.estado = "rodando"
    cfg = _cfg()
    t0 = time.monotonic()
    msgs = montar_contexto(turno.conversa_id, turno.pergunta, ate_id=turno.mensagem_id)
    ferramentas = lib_ferramentas.esquemas()
    # modelo_ferramentas (perfis.yml) diferente: ele decide as ferramentas sem mostrar texto, e o modelo do turno
    # escreve a resposta final sem tools — ver about.md (na 3060 os dois não cabem juntos)
    modelo_f = cfg["modelo_ferramentas"] or turno.modelo
    textos, entrada, saida, voltas = [], 0, 0, 0
    while True:
        com_ferramentas = voltas < MAX_VOLTAS
        modelo = modelo_f if com_ferramentas else turno.modelo
        r = _chamar(turno, msgs, modelo, ferramentas if com_ferramentas else None, cfg["num_ctx"],
                    emitir=modelo == turno.modelo)
        entrada, saida = r["entrada"] or entrada, saida + r["saida"]
        if not r["tool_calls"] and not r["parado"] and modelo != turno.modelo:
            r = _chamar(turno, msgs, turno.modelo, None, cfg["num_ctx"], emitir=True)
            modelo = turno.modelo
            entrada, saida = r["entrada"] or entrada, saida + r["saida"]
        if modelo == turno.modelo:
            textos.append(r["texto"])
        if r["parado"] or not r["tool_calls"]:
            break
        msgs.append({"role": "assistant", "content": r["texto"], "tool_calls": r["tool_calls"]})
        for chamada in r["tool_calls"]:
            fn = chamada.get("function") or {}
            nome = str(fn.get("name") or "")
            resultado = _usar_ferramenta(turno, nome, fn.get("arguments"), modelo)
            msgs.append({"role": "tool", "content": resultado, "tool_name": nome})
            if turno.parar.is_set():
                break
        voltas += 1
        if turno.parar.is_set():
            break
    parado = turno.parar.is_set()
    texto = "\n\n".join(t.strip() for t in textos if t and t.strip())
    if not texto and not parado:
        raise RuntimeError(f"{turno.modelo} devolveu uma resposta vazia")
    tempo_ms = int((time.monotonic() - t0) * 1000)
    mid = None
    if texto:
        with closing(_conectar()) as con, con:
            mid = _gravar_mensagem(con, turno.conversa_id, "assistant", texto, turno.id, modelo=turno.modelo,
                                   tokens_entrada=entrada, tokens_saida=saida, tempo_ms=tempo_ms)
    log(f"[conversa] turno {turno.id} ({turno.modelo}): {voltas} volta(s) de ferramenta, {entrada} tokens de "
        f"entrada, {saida} de saída, {tempo_ms / 1000:.1f} s" + (" — interrompido" if parado else ""))
    _terminar(turno, "parado" if parado else "fim", "fim", mensagem_id=mid, texto=texto, parado=parado,
              modelo=turno.modelo, tokens_entrada=entrada, tokens_saida=saida, tempo_ms=tempo_ms, voltas=voltas)
    try:   # depois do "fim": Pedro já tem a resposta; o worker (e ocupado()) segura só mais um pouco
        _atualizar_resumo(turno.conversa_id, turno.modelo, cfg["num_ctx"])
    except Exception as e:
        log(f"[conversa] resumo falhou ({_curto(e)})")


# ══════════════════════════════════════════════════════════════
#  CLI:  run.sh conversa "pergunta" [--conversa ID] [--modelo M]
# ══════════════════════════════════════════════════════════════

def adicionar_parser(sub) -> None:
    sp = sub.add_parser("conversa", help='conversa com o Alfred (motor da aba Conversa): conversa "pergunta" '
                                         '[--conversa ID]')
    sp.add_argument("texto", nargs="+")
    sp.add_argument("--conversa", default=None, help="id de uma conversa para continuar (sem isso, abre uma nova)")
    sp.add_argument("--modelo", default=None, help="modelo do Ollama (padrão: conversa.modelo do perfis.yml → "
                                                   "MODEL_CHAT)")


def _uma_linha(texto: str, n: int = 160) -> str:
    texto = " ".join(str(texto or "").split())
    return texto if len(texto) <= n else texto[:n - 1] + "…"


def cmd_conversa(args) -> int:
    cid = args.conversa or nova_conversa()["id"]
    try:
        turno = enviar(cid, " ".join(args.texto), modelo=args.modelo)
    except (KeyError, ValueError, RuntimeError) as e:
        print(f"Erro: {e.args[0] if e.args else e}")
        return 1
    while True:
        try:
            ev = turno.fila.get()
        except KeyboardInterrupt:
            parar(turno.id)
            print("\n[parando…]", flush=True)
            continue
        if ev["tipo"] == "texto":
            print(ev["texto"], end="", flush=True)
        elif ev["tipo"] == "ferramenta":
            args_txt = json.dumps(ev["argumentos"], ensure_ascii=False)
            if ev["estado"] == "rodando":
                print(f"\n  ⚙ {ev['nome']} {args_txt}", flush=True)
            else:
                print(f"  ↳ {ev['estado']}: {_uma_linha(ev['resultado'])}", flush=True)
        elif ev["tipo"] == "aprovar":
            print(f"\n  ⚠ {ev['nome']} {json.dumps(ev['argumentos'], ensure_ascii=False)} precisa da sua aprovação.")
            try:
                r = input("  Executar? [s/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                r = ""
            (aprovar if r in ("s", "sim", "y", "yes") else negar)(ev["id"])
        elif ev["tipo"] == "fim":
            print(f"\n\n— conversa {cid} · {ev['modelo']} · {ev['voltas']} volta(s) de ferramenta · "
                  f"{ev['tokens_entrada']} tokens de entrada, {ev['tokens_saida']} de saída · "
                  f"{ev['tempo_ms'] / 1000:.1f} s" + (" · interrompido" if ev["parado"] else ""))
            return 0
        elif ev["tipo"] == "erro":
            print(f"\n[erro] {ev['erro']}")
            return 1
