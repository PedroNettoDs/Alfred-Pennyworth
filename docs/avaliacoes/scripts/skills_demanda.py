"""
Avaliação Skills — demanda real: sequências de passos que se repetem e poderiam virar skill.

Só lê e só imprime números e cabeças de comando genéricas (nada de texto de mensagem, e-mail ou nota).

Fontes:
  1. conversas.db (VAULT_ALFRED): turnos da aba Conversa, ferramentas por turno, sequências repetidas.
     Critério do Odysseus para tentar extrair skill: turno com >= 2 voltas ou >= 2 chamadas de ferramenta.
  2. journal do executor (alfred-executor): quantos POST /execute, por status.
  3. journal do inbox: linhas "[ferramentas]" (falha ou timeout de ferramenta).
  4. histórico do bash: comandos ligados ao Alfred/Ollama/Docker/systemd; n-gramas (2 a 4 comandos
     consecutivos) que se repetem >= 3 vezes e se cada comando passa na allowlist do executor.

Uso (da raiz do repositório; o venv do executor tem o fastapi que o main.py importa):
    services/shell-executor/venv/bin/python docs/avaliacoes/scripts/skills_demanda.py \
        --conversas "$VAULT_ALFRED/conversas.db" --historico ~/.bash_history
"""

import argparse
import collections
import importlib.util
import json
import re
import sqlite3
import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[3]
RELACIONADO = re.compile(r"alfred|707[1-4]|ollama|systemctl|journalctl|nvidia-smi|docker|curl http://(localhost|127)", re.I)


def _executor():
    spec = importlib.util.spec_from_file_location("executor_main", RAIZ / "services" / "shell-executor" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def conversas(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = lambda s: con.execute(s).fetchall()  # noqa: E731
    seqs = collections.Counter()
    for (turno,) in q("SELECT DISTINCT turno_id FROM mensagens WHERE papel = 'tool'"):
        nomes = [r[0] for r in con.execute("SELECT ferramenta FROM mensagens WHERE turno_id = ? AND papel = 'tool' "
                                           "ORDER BY id", (turno,))]
        seqs[" > ".join(nomes)] += 1
    return {
        "conversas": q("SELECT count(*) FROM conversas")[0][0],
        "dias_com_uso": q("SELECT count(DISTINCT substr(criada_em, 1, 10)) FROM mensagens WHERE papel = 'user'")[0][0],
        "pedidos": q("SELECT count(*) FROM mensagens WHERE papel = 'user'")[0][0],
        "chamadas_ferramenta": q("SELECT count(*) FROM mensagens WHERE papel = 'tool'")[0][0],
        "por_ferramenta": dict(q("SELECT ferramenta, count(*) FROM mensagens WHERE papel = 'tool' GROUP BY 1 ORDER BY 2 DESC")),
        "turnos_2_ou_mais_ferramentas": q("SELECT count(*) FROM (SELECT turno_id FROM mensagens WHERE papel = 'tool' "
                                          "GROUP BY turno_id HAVING count(*) >= 2)")[0][0],
        "sequencias_repetidas_2x": {s: n for s, n in seqs.items() if n >= 2 and " > " in s},
        "aprovacoes": q("SELECT count(*) FROM aprovacoes")[0][0],
        "tokens_entrada_medio": round(q("SELECT avg(tokens_entrada) FROM mensagens WHERE papel = 'assistant'")[0][0] or 0),
    }


def journal(unidade: str, padrao: str) -> int:
    out = subprocess.run(["journalctl", "--user", "-u", unidade, "--no-pager", "-o", "cat"],
                         capture_output=True, text=True).stdout
    return sum(1 for linha in out.splitlines() if re.search(padrao, linha))


def _cabeca(linha: str) -> str:
    """Até 3 palavras; argumento com /, @, = ou dígito vira <arg> (nada de caminho, host ou nome próprio)."""
    palavras = []
    for w in linha.split()[:3]:
        palavras.append("<arg>" if re.search(r"[/@=\d'\"]", w) and not w.startswith("-") else w)
    return " ".join(palavras)


def historico(arquivo: Path, executor) -> dict:
    linhas = [l.strip() for l in arquivo.read_text(errors="replace").splitlines() if l.strip()]
    rel = [l for l in linhas if RELACIONADO.search(l)]
    permitidas = sum(1 for l in rel if executor.is_allowed(l))
    cabecas = collections.Counter(_cabeca(l) for l in rel)
    # n-gramas de comandos consecutivos (no histórico inteiro, pela cabeça), repetidos >= 3 vezes
    seq = [_cabeca(l) for l in linhas]
    ngramas = {}
    for n in (2, 3, 4):
        c = collections.Counter(tuple(seq[i:i + n]) for i in range(len(seq) - n + 1))
        for g, k in c.items():
            if k >= 3 and len(set(g)) > 1 and any(RELACIONADO.search(x) for x in g):
                ngramas[" > ".join(g)] = k
    # cada n-grama: os comandos originais da primeira ocorrência passam na allowlist?
    na_allowlist = {}
    for g in ngramas:
        partes = g.split(" > ")
        for i in range(len(seq) - len(partes) + 1):
            if seq[i:i + len(partes)] == partes:
                na_allowlist[g] = all(executor.is_allowed(linhas[i + j]) for j in range(len(partes)))
                break
    return {
        "comandos": len(linhas),
        "ligados_ao_alfred_ollama_docker_systemd": len(rel),
        "desses_passam_na_allowlist": permitidas,
        "cabecas_mais_comuns": dict(cabecas.most_common(15)),
        "ngramas_repetidos_3x": dict(sorted(ngramas.items(), key=lambda x: -x[1])),
        "ngramas_todos_na_allowlist": na_allowlist,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversas", type=Path, required=True)
    ap.add_argument("--historico", type=Path, default=Path.home() / ".bash_history")
    a = ap.parse_args()
    executor = _executor()
    print(json.dumps({
        "conversas_db": conversas(a.conversas),
        "executor_post_execute": journal("alfred-executor", r"POST /execute"),
        "executor_execute_403": journal("alfred-executor", r"POST /execute.* 403"),
        "inbox_ferramentas_log": journal("alfred-inbox", r"\[ferramentas\]"),
        "inbox_conversa_log": journal("alfred-inbox", r"/conversa|\[conversa\]"),
        "historico_bash": historico(a.historico, executor),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
