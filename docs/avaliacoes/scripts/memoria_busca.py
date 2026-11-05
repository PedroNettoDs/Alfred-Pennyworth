"""
Avaliação da memória — latência da busca vetorial: cosseno em Python puro (o que lib_memoria faz hoje)
contra numpy e FAISS, para N fatos sintéticos (vetores aleatórios de norma 1, dimensão do bge-m3).

Não usa Ollama nem GPU. Não toca no banco real: cria bancos temporários em --pasta.

Mede, para cada N:
  python_nucleo     lib_memoria._vetor(blob) + lib_memoria._cos + sort — o laço de contexto(), vetores já em memória
  numpy_blobs       np.frombuffer dos blobs + produto matriz·vetor + top-k (vetores vêm dos blobs a cada consulta)
  numpy_cache       matriz já montada em memória (o que um cache no processo daria)
  faiss_flat        IndexFlatIP (exato)            + tempo de construção
  faiss_hnsw        IndexHNSWFlat M=32 (aproximado) + tempo de construção + revocação@8 contra o exato
  contexto_e2e      lib_memoria.contexto() de ponta a ponta num SQLite temporário com N fatos
                    (SELECT de todas as linhas com o vetor + cosseno em Python + FTS5), ollama_embed trocado por
                    um vetor fixo — é a latência que o chat paga hoje por consulta
  sqlite_select     só o SELECT de todas as linhas com o vetor (parte do e2e que nenhum índice em memória evita)

Uso (de qualquer pasta; precisa de numpy, faiss-cpu, httpx e pyyaml no Python):
    python memoria_busca.py --services <repo>/services --pasta /tmp/x [--ns 1000,10000,100000] [--saida res.json]
"""

import argparse
import json
import os
import platform
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from unittest import mock

import numpy as np

DIM = 1024          # bge-m3
K = 8               # contexto(consulta, k=8)


def _mediana(fn, rep: int) -> float:
    """Mediana de `rep` execuções de fn(), em milissegundos."""
    tempos = []
    for _ in range(rep):
        t = time.perf_counter()
        fn()
        tempos.append((time.perf_counter() - t) * 1000)
    return statistics.median(tempos)


def _reps(n: int, base: int) -> int:
    """Menos repetições para os casos lentos (Python puro com 100 mil leva segundos)."""
    return max(3, base if n <= 10_000 else base // 4)


def _criar_banco(lib_memoria, caminho: Path, vetores: np.ndarray) -> None:
    """SQLite com o schema de lib_memoria e N fatos sintéticos (texto curto + vetor float32 em blob)."""
    caminho.unlink(missing_ok=True)
    con = sqlite3.connect(str(caminho))
    con.executescript(lib_memoria._SCHEMA)
    temas = ["café", "reunião", "viagem", "projeto", "academia", "cliente", "família", "livro", "música", "código"]
    linhas = []
    for i, v in enumerate(vetores):
        sujeito = f"Pessoa{i % 500}"
        linhas.append((f"fato sintético {i} sobre {temas[i % len(temas)]} e assunto{i % 997}", sujeito,
                       lib_memoria._chave(sujeito), "outro", "permanente", "bench", "2026-11-03T00:00:00",
                       "2026-11-03T00:00:00", v.astype(np.float32).tobytes(), "bge-m3"))
    with con:
        con.executemany("INSERT INTO fatos(fato, sujeito, chave, tipo, validade, fonte, criado, atualizado, vetor,"
                        " modelo_vetor) VALUES (?,?,?,?,?,?,?,?,?,?)", linhas)
    con.close()


def medir(n: int, rep: int, pasta: Path, lib_memoria, faiss) -> dict:
    rng = np.random.default_rng(42 + n)
    vetores = rng.standard_normal((n, DIM)).astype(np.float32)
    vetores /= np.linalg.norm(vetores, axis=1, keepdims=True)
    q = rng.standard_normal(DIM).astype(np.float32)
    q /= np.linalg.norm(q)
    blobs = [v.tobytes() for v in vetores]
    q_arr = lib_memoria._normalizar(q.tolist())
    r = {"n": n, "rep": _reps(n, rep)}

    # Python puro — mesmo código de contexto(): decodifica o blob, cosseno por soma de produtos, ordena
    def python_nucleo():
        sims = sorted(((lib_memoria._cos(q_arr, lib_memoria._vetor(b)), i) for i, b in enumerate(blobs)), reverse=True)
        return sims[:K]
    r["python_nucleo_ms"] = _mediana(python_nucleo, r["rep"])

    def numpy_blobs():
        m = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(n, DIM)
        s = m @ q
        top = np.argpartition(-s, K)[:K]
        return top[np.argsort(-s[top])]
    r["numpy_blobs_ms"] = _mediana(numpy_blobs, rep)

    def numpy_cache():
        s = vetores @ q
        top = np.argpartition(-s, K)[:K]
        return top[np.argsort(-s[top])]
    r["numpy_cache_ms"] = _mediana(numpy_cache, rep)

    t = time.perf_counter()
    flat = faiss.IndexFlatIP(DIM)
    flat.add(vetores)
    r["faiss_flat_construir_ms"] = (time.perf_counter() - t) * 1000
    qq = q.reshape(1, DIM)
    r["faiss_flat_ms"] = _mediana(lambda: flat.search(qq, K), rep)
    _, exato = flat.search(qq, K)

    t = time.perf_counter()
    hnsw = faiss.IndexHNSWFlat(DIM, 32, faiss.METRIC_INNER_PRODUCT)
    hnsw.hnsw.efSearch = 64
    hnsw.add(vetores)
    r["faiss_hnsw_construir_ms"] = (time.perf_counter() - t) * 1000
    r["faiss_hnsw_ms"] = _mediana(lambda: hnsw.search(qq, K), rep)
    # Revocação@8 do HNSW contra o exato, em 50 consultas aleatórias
    qs = rng.standard_normal((50, DIM)).astype(np.float32)
    qs /= np.linalg.norm(qs, axis=1, keepdims=True)
    _, e = flat.search(qs, K)
    _, a = hnsw.search(qs, K)
    r["faiss_hnsw_revocacao8"] = float(np.mean([len(set(x) & set(y)) / K for x, y in zip(e, a)]))

    # Ponta a ponta: lib_memoria.contexto() num banco temporário
    banco = pasta / f"busca_{n}.db"
    _criar_banco(lib_memoria, banco, vetores)
    r["banco_mb"] = round(banco.stat().st_size / 1e6, 1)
    os.environ["MEMORIA_DB"] = str(banco)
    with mock.patch.object(lib_memoria, "ollama_embed", lambda *a, **k: q.tolist()):
        saida = lib_memoria.contexto("reunião de projeto com cliente", K)
        assert saida, "contexto() voltou vazio"
        r["contexto_e2e_ms"] = _mediana(lambda: lib_memoria.contexto("reunião de projeto com cliente", K), r["rep"])

    def sqlite_select():
        con = sqlite3.connect(str(banco))
        con.execute("SELECT id, fato, sujeito, vetor, modelo_vetor FROM fatos"
                    " WHERE validade = 'permanente' OR validade >= ?", ("2026-11-03",)).fetchall()
        con.close()
    r["sqlite_select_ms"] = _mediana(sqlite_select, r["rep"])
    r["ram_vetores_mb"] = round(vetores.nbytes / 1e6, 1)
    banco.unlink(missing_ok=True)
    for sufixo in ("-wal", "-shm"):
        Path(str(banco) + sufixo).unlink(missing_ok=True)
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--services", required=True, help="pasta services/ do Alfred (para importar _shared.lib_memoria)")
    ap.add_argument("--pasta", required=True, help="pasta temporária para os bancos SQLite")
    ap.add_argument("--ns", default="1000,10000,100000")
    ap.add_argument("--rep", type=int, default=20)
    ap.add_argument("--saida", help="grava o resultado em JSON")
    a = ap.parse_args()

    sys.path.insert(0, a.services)
    import faiss
    from _shared import lib_memoria

    pasta = Path(a.pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    faiss.omp_set_num_threads(1)      # comparação justa com o Python puro (1 núcleo) e com outras avaliações rodando
    res = {"maquina": {"python": platform.python_version(), "numpy": np.__version__, "faiss": faiss.__version__,
                       "cpus": os.cpu_count(), "carga_inicio": os.getloadavg()},
           "medidas": []}
    for n in (int(x) for x in a.ns.split(",")):
        m = medir(n, a.rep, pasta, lib_memoria, faiss)
        res["medidas"].append(m)
        print(json.dumps(m, ensure_ascii=False), flush=True)
    res["maquina"]["carga_fim"] = os.getloadavg()
    if a.saida:
        Path(a.saida).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
