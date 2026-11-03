#!/usr/bin/env python3
"""
Avaliação de Robustez — injeção de falha com kill -9.

Para cada modo de gravação, N tentativas. Em cada tentativa:
  1. o arquivo de estado já existe com a versão A (JSON válido);
  2. um processo filho regrava o arquivo em laço, alternando as versões B e A, do jeito do modo;
  3. depois de um atraso aleatório (0,2–20 ms após o filho avisar que começou), o pai manda SIGKILL (= kill -9);
  4. o pai lê o arquivo: "ok" se é exatamente A ou B; senão "vazio" (0 bytes) ou "truncado" (JSON inválido ou outro
     conteúdo). Também conta temporários órfãos que sobraram na pasta.

Modos:
  direto     Path.write_text(json)                          — vigia.py:_salvar_estado, .md do cofre
  tmp_fixo   <arq>.tmp fixo + Path.replace, sem fsync        — whatsapp.py:_save_fila, lib_whatsapp._marcar_envio
  atomico    lib_alfred.gravar_atomico                       — temporário único + fsync + os.replace + fsync da pasta

Mede também a latência de uma gravação sem falha (mediana e p95) em cada modo.

kill -9 simula a queda do processo (OOM, systemctl kill, travamento), não a falta de energia: o cache de páginas
do kernel sobrevive, então o fsync não entra no resultado da injeção, só na latência.

Rode num sistema de arquivos de verdade (o /tmp desta máquina é tmpfs, onde fsync não custa nada):
    services/inbox/venv/bin/python docs/avaliacoes/scripts/robustez_kill9.py --n 200 --bytes 90000
Sem dados do Alfred: o conteúdo é sintético, numa pasta temporária em ~/.cache apagada no fim.
"""

import argparse
import json
import os
import random
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[3]
MODOS = ("direto", "tmp_fixo", "atomico")


def _conteudo(versao: str, n_bytes: int) -> str:
    """JSON sintético com cara de estado do Alfred (lista de ids vistos), com ~n_bytes."""
    item = lambda i: {"id": f"{versao}-{i:08d}", "visto_em": "2026-11-03T04:00:00"}
    por_item = len(json.dumps(item(0), indent=1)) + 4   # indentação e vírgula dentro da lista
    return json.dumps({"versao": versao, "ids": [item(i) for i in range(max(1, n_bytes // por_item))]}, indent=1)


def _gravador(modo: str):
    if modo == "direto":
        return lambda p, t: p.write_text(t, encoding="utf-8")
    if modo == "tmp_fixo":
        def tmp_fixo(p: Path, t: str):
            tmp = p.with_suffix(".tmp")
            tmp.write_text(t, encoding="utf-8")
            tmp.replace(p)
        return tmp_fixo
    sys.path.insert(0, str(RAIZ / "services"))
    from _shared.lib_alfred import gravar_atomico
    return gravar_atomico


def filho(modo: str, arq: Path, n_bytes: int) -> None:
    """Regrava `arq` em laço até levar SIGKILL. Escreve um byte no stdout quando começa."""
    gravar = _gravador(modo)
    versoes = (_conteudo("B", n_bytes), _conteudo("A", n_bytes))
    sys.stdout.buffer.write(b"1")
    sys.stdout.buffer.flush()
    i = 0
    while True:
        gravar(arq, versoes[i % 2])
        i += 1


def _classificar(arq: Path, validos: set) -> str:
    try:
        dados = arq.read_bytes()
    except FileNotFoundError:
        return "sumiu"
    if not dados:
        return "vazio"
    return "ok" if dados.decode("utf-8", "replace") in validos else "truncado"


def injetar(modo: str, n: int, n_bytes: int, base: Path) -> dict:
    pasta = Path(tempfile.mkdtemp(prefix=f"{modo}-", dir=base))
    arq = pasta / "estado.json"
    a, b = _conteudo("A", n_bytes), _conteudo("B", n_bytes)
    validos = {a, b}
    cont = {"ok": 0, "vazio": 0, "truncado": 0, "sumiu": 0}
    for _ in range(n):
        arq.write_text(a, encoding="utf-8")   # estado anterior, íntegro
        p = subprocess.Popen([sys.executable, __file__, "--filho", modo, str(arq), str(n_bytes)],
                             stdout=subprocess.PIPE)
        p.stdout.read(1)                        # o filho já importou tudo e entrou no laço
        time.sleep(random.uniform(0.0002, 0.02))
        os.kill(p.pid, signal.SIGKILL)          # kill -9
        p.wait()
        cont[_classificar(arq, validos)] += 1
    orfaos = sum(1 for f in pasta.iterdir() if f.name != arq.name)
    shutil.rmtree(pasta)
    return {"modo": modo, "n": n, "bytes": len(a), **cont, "temporarios_orfaos": orfaos}


def latencia(modo: str, n: int, n_bytes: int, base: Path) -> dict:
    pasta = Path(tempfile.mkdtemp(prefix=f"lat-{modo}-", dir=base))
    arq = pasta / "estado.json"
    gravar = _gravador(modo)
    texto = _conteudo("A", n_bytes)
    gravar(arq, texto)
    tempos = []
    for _ in range(n):
        t0 = time.perf_counter()
        gravar(arq, texto)
        tempos.append((time.perf_counter() - t0) * 1000)
    shutil.rmtree(pasta)
    tempos.sort()
    return {"modo": modo, "mediana_ms": round(statistics.median(tempos), 3),
            "p95_ms": round(tempos[int(len(tempos) * 0.95) - 1], 3)}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--filho":
        filho(sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]))
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--bytes", type=int, default=90000, help="tamanho do JSON (90 kB ~ logs/whatsapp_seen.json)")
    ap.add_argument("--dir", type=Path, default=Path.home() / ".cache", help="onde criar a pasta temporária")
    ap.add_argument("--semente", type=int, default=20261103)
    a = ap.parse_args()
    random.seed(a.semente)
    a.dir.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="alfred-bench-robustez-", dir=a.dir))
    try:
        res = {"injecao": [injetar(m, a.n, a.bytes, base) for m in MODOS],
               "latencia": [latencia(m, a.n, a.bytes, base) for m in MODOS],
               "fs": subprocess.run(["stat", "-f", "-c", "%T", str(base)], capture_output=True, text=True).stdout.strip(),
               "carga_1min": os.getloadavg()[0]}
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
