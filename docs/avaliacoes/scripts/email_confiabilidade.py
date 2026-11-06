"""
Avaliação E-mail — confiabilidade da cópia offline do Thunderbird (só leitura).

Para cada pasta mbox de cada conta, compara o mbox com o índice .msf (leitor Mork do lib_mail):
  mbox            mensagens no mbox (sem fragmentos)
  expunged        com X-Mozilla-Status 0x0008 (o lib_mail já descarta)
  vivas           mbox − expunged (o que fetch_messages devolveria)
  sem_mid         vivas sem Message-ID (id "gen-…", não conferível)
  mids            Message-IDs distintos entre as vivas
  dup             cópias extras: vivas com Message-ID − mids
  indice          Message-IDs distintos no .msf (o que o Thunderbird mostra)
  linhas_indice   linhas da tabela de mensagens do .msf
  fantasmas       mids fora do índice (já saíram da pasta no servidor, bytes ainda no mbox)
  faltantes       índice − mids (o Thunderbird lista, mas o corpo não está offline: o Alfred não vê)
  mb / mb_mortos  tamanho do mbox e bytes de fantasmas + cópias extras + expunged (recuperáveis ao compactar)

Uso:  python3 docs/avaliacoes/scripts/email_confiabilidade.py [--json saida.json]
"""

import argparse
import json
import sys
from collections import Counter

import email_comum as c


def medir_pasta(path) -> dict:
    n = exp = sem_mid = 0
    mids: Counter = Counter()
    bytes_por_mid: dict = {}
    bytes_exp = 0
    for _sep, raw in c.iter_mbox(path):
        if not raw.strip():
            continue
        h = c.cabecalhos(raw)
        if c.e_fragmento(h):
            continue
        n += 1
        if c.status_mozilla(h) & 0x0008:
            exp += 1
            bytes_exp += len(raw)
            continue
        mid = c.message_id(h)
        if not mid:
            sem_mid += 1
            continue
        mids[mid] += 1
        bytes_por_mid.setdefault(mid, []).append(len(raw))

    idx = c.linhas_indice(path)
    r = {
        "mbox": n, "expunged": exp, "vivas": n - exp, "sem_mid": sem_mid,
        "mids": len(mids), "dup": sum(mids.values()) - len(mids),
        "mids_com_copia": sum(1 for v in mids.values() if v > 1),
        "mb": round(path.stat().st_size / 1e6, 2),
    }
    mortos = bytes_exp + sum(sum(b[1:]) for b in bytes_por_mid.values())
    if idx is None:
        r.update({"indice": None, "linhas_indice": None, "fantasmas": None, "faltantes": None})
    else:
        fant = [m for m in mids if m not in idx["ids"]]
        mortos += sum(bytes_por_mid[m][0] for m in fant)
        r.update({
            "indice": len(idx["ids"]), "linhas_indice": idx["linhas"],
            "fantasmas": len(fant), "faltantes": len(idx["ids"] - set(mids)),
            "fantasmas_dup": sum(1 for m in fant if mids[m] > 1),
        })
    r["mb_mortos"] = round(mortos / 1e6, 2)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="grava o resultado em JSON")
    args = ap.parse_args()

    prof = c.perfil()
    vistos: dict = {}
    linhas = []
    for conta in c.contas(prof):
        for nome, path in c.pastas_mbox(conta):
            if path.stat().st_size == 0:
                continue
            r = medir_pasta(path)
            r["conta"] = conta["rotulo"]
            r["pasta"] = c.rotulo_pasta(nome, vistos)
            linhas.append(r)

    cols = ["conta", "pasta", "mb", "mbox", "expunged", "vivas", "sem_mid", "mids", "dup", "indice",
            "linhas_indice", "fantasmas", "faltantes", "mb_mortos"]
    print("\t".join(cols))
    for r in linhas:
        print("\t".join(str(r.get(k)) for k in cols))
    tot = {k: sum((r.get(k) or 0) for r in linhas) for k in cols[2:]}
    print("\t".join(["TOTAL", ""] + [str(round(tot[k], 2)) for k in cols[2:]]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(linhas, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main())
