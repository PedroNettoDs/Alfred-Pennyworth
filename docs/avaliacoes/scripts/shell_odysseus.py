"""Mede o ShellService do Odysseus (services/shell/service.py) isolado, com comandos inofensivos.

O arquivo do Odysseus é carregado pelo caminho (só usa stdlib); nada do Alfred é lido. Rode de um diretório vazio
do scratchpad, com -I, sem o .env no ambiente:

    git clone --depth 1 https://github.com/odysseus-dev/odysseus <scratch>/odysseus-src/odysseus
    cd <scratch> && python3 -I <repo>/docs/avaliacoes/scripts/shell_odysseus.py \
        <scratch>/odysseus-src/odysseus/services/shell/service.py [--so timeout,saida,concorrencia,injecao] \
        [--tamanhos 1,10,50,200]

Cada tamanho de saída roda num processo novo, para o pico de RSS (ru_maxrss) ser só daquele pedido.
"""

import argparse
import asyncio
import importlib.util
import json
import os
import resource
import statistics
import subprocess
import sys
import time


def carregar(caminho):
    spec = importlib.util.spec_from_file_location("_odysseus_shell_service", caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ShellService(timeout=30, max_output=200_000)


def vivos(marca):
    p = subprocess.run(["pgrep", "-fc", marca], capture_output=True, text=True)
    return int(p.stdout.strip() or 0)


async def medir_timeout(svc, cwd):
    print("## Comando que não termina (timeout=2)")
    # Marcas ancoradas no início da linha de comando: sem "^", o pgrep casa também o shell que rodou este script.
    for cmd, marca in [("sleep 30.17081", "^sleep 30.17081"),
                       ("sleep 30.17082; true", "^sleep 30.17082"),
                       ("sleep 30.17083 | cat", "^sleep 30.17083")]:
        async def vivos_aos_3s():
            await asyncio.sleep(3)
            return vivos(marca)
        t0 = time.monotonic()
        espiao = asyncio.create_task(vivos_aos_3s())
        r = await svc.execute(cmd, timeout=2, cwd=cwd)
        seg = time.monotonic() - t0
        aos_3s = await espiao
        await asyncio.sleep(0.3)
        print(f"- `{cmd}`: timed_out={r.timed_out}, execute() voltou em {seg:.2f} s; processos vivos aos 3 s: "
              f"{aos_3s}; depois de voltar: {vivos(marca)}")
    # Limpa os órfãos desta medição (nunca "pkill -f" solto: mataria o shell que chamou o script)
    sobra = subprocess.run(["pgrep", "-f", "^sleep 30.1708"], capture_output=True, text=True).stdout.split()
    for pid in sobra:
        os.kill(int(pid), 15)


def medir_saida(caminho, tamanhos):
    print("## Saída enorme (`yes | head -c N`)")
    print("| N | len(stdout) devolvido | s | pico RSS do processo (MiB) |")
    print("|---|---|---|---|")
    for mb in tamanhos:
        p = subprocess.run([sys.executable, "-I", __file__, caminho, "--um", str(mb)],
                           capture_output=True, text=True, cwd=os.getcwd())
        d = json.loads(p.stdout)
        print(f"| {mb} MB | {d['len']:,} | {d['seg']:.2f} | {d['pico']} (base {d['base']}) |")


async def um_tamanho(svc, mb, cwd):
    base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    t0 = time.monotonic()
    r = await svc.execute(f"yes | head -c {mb}M", timeout=120, cwd=cwd)
    seg = time.monotonic() - t0
    pico = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print(json.dumps({"len": len(r.stdout), "seg": seg, "pico": pico, "base": base}))


async def medir_concorrencia(svc, cwd):
    print("## Concorrência")

    async def cronometrar(cmd):
        t0 = time.monotonic()
        await svc.execute(cmd, cwd=cwd)
        return time.monotonic() - t0
    for n in (10, 25, 50):
        t0 = time.monotonic()
        segs = sorted(await asyncio.gather(*[cronometrar("date") for _ in range(n)]))
        print(f"- {n} × `date`: p50 {statistics.median(segs)*1000:.0f} ms, máx {segs[-1]*1000:.0f} ms; "
              f"parede {time.monotonic() - t0:.2f} s")
    t0 = time.monotonic()
    await asyncio.gather(*[svc.execute("sleep 1", cwd=cwd) for _ in range(50)])
    print(f"- 50 × `sleep 1` simultâneos: parede {time.monotonic() - t0:.2f} s")


async def medir_injecao(svc, cwd):
    print("## Encadeamento (o ShellService não tem allowlist: roda em /bin/sh -c)")
    casos = ["date; echo INJ", "date && echo INJ", "false || echo INJ", "echo $(echo INJ)", "echo `echo INJ`",
             "X=INJ; echo $X", "echo ${HOME:+INJ}", "date\necho INJ"]
    passaram = 0
    for cmd in casos:
        r = await svc.execute(cmd, cwd=cwd)
        ok = "INJ" in r.stdout
        passaram += ok
        print(f"- {cmd!r}: {'executou o 2º comando' if ok else 'não'}")
    print(f"- total: {passaram}/{len(casos)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("service")
    ap.add_argument("--so", default="timeout,saida,concorrencia,injecao")
    ap.add_argument("--tamanhos", type=lambda s: [int(x) for x in s.split(",")], default=[1, 10, 50, 200])
    ap.add_argument("--um", type=int)
    a = ap.parse_args()
    svc = carregar(a.service)
    cwd = os.getcwd()
    if a.um:
        asyncio.run(um_tamanho(svc, a.um, cwd))
        sys.exit()
    for parte in a.so.split(","):
        if parte == "saida":
            medir_saida(a.service, a.tamanhos)
        else:
            asyncio.run({"timeout": medir_timeout, "concorrencia": medir_concorrencia,
                         "injecao": medir_injecao}[parte](svc, cwd))
