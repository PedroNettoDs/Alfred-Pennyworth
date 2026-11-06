"""
Avaliação E-mail — custo de uma varredura do lib_mail sobre o perfil do Thunderbird (só leitura, sem GPU).

Cenários (cada repetição roda num subprocesso novo, para o pico de RSS ser só daquele cenário):
  ciclo       o que core.coletar faz no timer: INBOX da conta, janela de 36 h, máx. 80
  limpeza     o que limpeza.candidatos lê: INBOX inteira + índice .msf (checar_indice=True)
  indice      só ids_no_indice (leitor Mork) da INBOX
  completa    todas as contas × todas as pastas mbox, sem janela, com índice

Mede tempo de parede, CPU (user+sys) e pico de RSS. O mbox costuma estar no cache de páginas (o Thunderbird
acabou de usá-lo); o script não limpa o cache (exige root), então os números são de leitura "quente".

Uso:  python3 docs/avaliacoes/scripts/email_custo.py [--rep 5] [--conta outlook.office365.com]
"""

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta


def rodar(cenario: str, conta: str) -> dict:
    import email_comum as c
    lm = c.lib_mail
    prof = c.perfil()
    t0, c0 = time.perf_counter(), time.process_time()
    accs = lm.list_accounts(prof)
    n = 0
    mb = 0.0
    if cenario == "ciclo":
        desde = datetime.now().astimezone() - timedelta(hours=36)
        n = len(lm.fetch_messages(accs, pastas=["INBOX"], desde=desde, conta=conta, max_total=80))
    elif cenario == "limpeza":
        n = len(lm.fetch_messages(accs, pastas=["INBOX"], conta=conta, max_total=100_000, checar_indice=True))
    elif cenario == "indice":
        acc = next(a for a in accs if conta in a["hostname"])
        n = len(lm.ids_no_indice(lm.resolve_folder(acc, "INBOX")) or ())
    elif cenario == "completa":
        for acc in c.contas(prof):
            pastas = [nome for nome, p in c.pastas_mbox(acc) if p.stat().st_size > 0]
            if not pastas:
                continue
            n += len(lm.fetch_messages([acc], pastas=pastas, conta=acc["hostname"] or acc["name"],
                                       max_total=10**9, checar_indice=True))
    for acc in accs:   # MB lidos (só os mbox que o cenário abre)
        if cenario == "completa":
            mb += sum(p.stat().st_size for _, p in c.pastas_mbox(dict(acc))) / 1e6
        elif conta in acc["hostname"]:
            mb += (lm.resolve_folder(acc, "INBOX") or prof).stat().st_size / 1e6
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return {"cenario": cenario, "msgs": n, "mb": round(mb, 1),
            "parede_s": round(time.perf_counter() - t0, 3), "cpu_s": round(time.process_time() - c0, 3),
            "rss_mb": round(ru.ru_maxrss / 1024, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rep", type=int, default=5)
    ap.add_argument("--conta", default="outlook.office365.com")
    ap.add_argument("--um", help=argparse.SUPPRESS)       # uso interno: roda um cenário e imprime JSON
    ap.add_argument("--cenarios", default="ciclo,limpeza,indice,completa")
    args = ap.parse_args()
    if args.um:
        print(json.dumps(rodar(args.um, args.conta)))
        return

    print(f"# load average no início: {os.getloadavg()}; CPUs: {os.cpu_count()}")
    for cen in args.cenarios.split(","):
        res = []
        for _ in range(args.rep):
            p = subprocess.run([sys.executable, __file__, "--um", cen, "--conta", args.conta],
                               capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
            if p.returncode:
                sys.exit(p.stderr)
            res.append(json.loads(p.stdout.strip().splitlines()[-1]))
        med = {k: statistics.median(r[k] for r in res) for k in ("parede_s", "cpu_s", "rss_mb")}
        print(json.dumps({"cenario": cen, "msgs": res[0]["msgs"], "mb": res[0]["mb"], "rep": len(res),
                          "parede_s_mediana": med["parede_s"],
                          "parede_s_min_max": [min(r["parede_s"] for r in res), max(r["parede_s"] for r in res)],
                          "cpu_s_mediana": med["cpu_s"], "rss_mb_mediana": med["rss_mb"]}, ensure_ascii=False))
    print(f"# load average no fim: {os.getloadavg()}")


if __name__ == "__main__":
    main()
