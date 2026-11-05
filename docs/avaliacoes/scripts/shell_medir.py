"""Mede uma instância de TESTE do shell-executor (padrão 127.0.0.1:17071): timeout, saída enorme e concorrência.

Só usa comandos inofensivos da allowlist (tail -f /dev/null, head de um arquivo de teste, date). Só stdlib.

Uso:
    TOKEN=teste123 python3 -I docs/avaliacoes/scripts/shell_medir.py --pid <PID do uvicorn> \
        --arquivo ~/.cache/alfred-aval-shell/yes200M.txt --resposta ~/.cache/alfred-aval-shell/resposta.json \
        [--porta 17071] [--so timeout,saida,concorrencia] [--tamanhos 1,10]

O arquivo de saída enorme é criado antes com:  yes | head -c 200M > ~/.cache/alfred-aval-shell/yes200M.txt
(em disco: o /tmp desta máquina é tmpfs, ou seja, RAM).
"""

import argparse
import http.client
import json
import os
import statistics
import subprocess
import threading
import time

ARGS = None


def pedir(comando=None, timeout=15, caminho="/execute", espera=None, ler=True, destino=None):
    """(status, bytes da resposta, segundos). Lê a resposta em blocos e grava em `destino` (arquivo) ou descarta:
    nunca na saída do comando (a resposta de 200 MB tem 300 MB)."""
    c = http.client.HTTPConnection("127.0.0.1", ARGS.porta, timeout=espera or timeout + 30)
    t0 = time.monotonic()
    if comando is None:
        c.request("GET", caminho)
    else:
        corpo = json.dumps({"command": comando, "timeout": timeout})
        c.request("POST", caminho, corpo, {"Authorization": f"Bearer {os.environ['TOKEN']}",
                                           "Content-Type": "application/json"})
    r = c.getresponse()
    total = 0
    if ler:
        with open(destino or os.devnull, "wb") as f:
            while bloco := r.read(1 << 20):
                total += len(bloco)
                f.write(bloco)
    c.close()
    return r.status, total, time.monotonic() - t0


def vivos(marca):
    """Quantos processos têm a marca na linha de comando (exclui o próprio pgrep)."""
    p = subprocess.run(["pgrep", "-fc", marca], capture_output=True, text=True)
    return int(p.stdout.strip() or 0)


def status_proc(chave):
    with open(f"/proc/{ARGS.pid}/status") as f:
        for linha in f:
            if linha.startswith(chave + ":"):
                return int(linha.split()[1]) // 1024   # MiB
    return -1


def tmp_usado_mib():
    s = os.statvfs("/tmp")
    return (s.f_blocks - s.f_bfree) * s.f_frsize / 2**20


def medir_timeout():
    print("## Comando que não termina")
    casos = [("tail -n 17071 -f /dev/null", "tail -n 17071", 2),
             ("tail -n 17072 -f /dev/null | grep -c 17072x", "17072", 2),
             ("tail -n 17074 -f /dev/null", "tail -n 17074", 10)]
    for cmd, marca, t in casos:
        st, _, seg = pedir(cmd, timeout=t)
        time.sleep(0.3)
        print(f"- `{cmd}` timeout={t}: HTTP {st} em {seg:.2f} s; processos vivos depois: {vivos(marca)}")
    # Cliente desiste antes do timeout do comando
    marca = "tail -n 17073"
    try:
        pedir("tail -n 17073 -f /dev/null", timeout=6, espera=1)
    except (TimeoutError, OSError):
        pass
    time.sleep(2)
    v3 = vivos(marca)
    time.sleep(4.5)
    print(f"- cliente desiste em 1 s (comando com timeout=6): processos vivos 3 s após o pedido = {v3}; "
          f"7,5 s após = {vivos(marca)}")


def medir_saida():
    print("## Saída enorme (head -c N de um arquivo 'y\\n')")
    print("| N | HTTP | bytes da resposta | s | pico RSS do serviço (MiB) | RSS depois (MiB) | pico /tmp (MiB) |")
    print("|---|---|---|---|---|---|---|")
    for mb in ARGS.tamanhos:
        with open(f"/proc/{ARGS.pid}/clear_refs", "w") as f:
            f.write("5")   # zera o VmHWM (pico de RSS)
        base_rss, base_tmp = status_proc("VmRSS"), tmp_usado_mib()
        pico_tmp, parar = [0.0], threading.Event()

        def amostrar():
            while not parar.is_set():
                pico_tmp[0] = max(pico_tmp[0], tmp_usado_mib() - base_tmp)
                time.sleep(0.02)
        th = threading.Thread(target=amostrar)
        th.start()
        st, total, seg = pedir(f"head -c {mb}M {ARGS.arquivo}", timeout=120, destino=ARGS.resposta)
        parar.set()
        th.join()
        time.sleep(0.5)
        print(f"| {mb} MB | {st} | {total:,} | {seg:.2f} | {status_proc('VmHWM')} (base {base_rss}) "
              f"| {status_proc('VmRSS')} | {pico_tmp[0]:.0f} |")


def rajada(comandos, timeout=15):
    """Dispara todos ao mesmo tempo; devolve [(status, segundos)] na ordem."""
    res = [None] * len(comandos)
    largada = threading.Barrier(len(comandos))

    def um(i, cmd):
        largada.wait()
        try:
            st, _, seg = pedir(cmd, timeout=timeout, caminho="/health" if cmd is None else "/execute")
        except Exception as e:  # noqa: BLE001 — a medição registra qualquer falha
            st, seg = type(e).__name__, -1
        res[i] = (st, seg)
    ths = [threading.Thread(target=um, args=(i, c)) for i, c in enumerate(comandos)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return res


def resumo(res):
    segs = sorted(s for _, s in res if s >= 0)
    sts = {}
    for st, _ in res:
        sts[st] = sts.get(st, 0) + 1
    p95 = segs[max(0, int(len(segs) * 0.95) - 1)]
    return f"p50 {statistics.median(segs)*1000:.0f} ms, p95 {p95*1000:.0f} ms, máx {segs[-1]*1000:.0f} ms, status {sts}"


def em_fundo(n, timeout):
    """n comandos lentos em threads (não espera)."""
    ths = [threading.Thread(target=pedir, args=(f"tail -n 1707{5 + i % 3} -f /dev/null",), kwargs={"timeout": timeout})
           for i in range(n)]
    for t in ths:
        t.start()
    return ths


def medir_concorrencia():
    print("## Concorrência")
    pedir("date")
    for n in (1, 10, 25, 50):
        t0 = time.monotonic()
        res = rajada(["date"] * n)
        print(f"- {n} × `date` simultâneos: {resumo(res)}; parede {time.monotonic() - t0:.2f} s")
    for lentos in (1, 39, 40, 45):
        ths = em_fundo(lentos, 6)
        time.sleep(1.0)
        res = rajada([None] * 3 + ["date"] * 5)   # mesma rajada: /health e date chegam juntos
        res_h, res_d = res[:3], res[3:]
        print(f"- com {lentos} × `tail -f` (timeout 6 s) rodando: /health {resumo(res_h)}; date {resumo(res_d)}")
        for t in ths:
            t.join()
        time.sleep(0.5)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=17071)
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--arquivo", required=True)
    ap.add_argument("--resposta", help="arquivo (em disco, não em /tmp, que é tmpfs) onde gravar cada resposta")
    ap.add_argument("--tamanhos", type=lambda s: [int(x) for x in s.split(",")], default=[1, 10, 50, 200])
    ap.add_argument("--so", default="timeout,saida,concorrencia")
    ARGS = ap.parse_args()
    print(f"carga antes: {open('/proc/loadavg').read().split()[:3]}")
    for parte in ARGS.so.split(","):
        {"timeout": medir_timeout, "saida": medir_saida, "concorrencia": medir_concorrencia}[parte]()
    print(f"carga depois: {open('/proc/loadavg').read().split()[:3]}")
