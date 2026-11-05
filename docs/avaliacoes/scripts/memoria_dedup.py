"""
Avaliação da memória — deduplicação na entrada e auditoria do Alfred (lib_memoria.guardar e auditar, código de
produção, com bge-m3 e o LLM de verdade) sobre memoria_dedup.json. Também pontua a saída do harness do Odysseus
(memoria_odysseus.py deduplicar), que grava o mesmo formato.

Formato da saída: {"sistema", "modelo", "fases": {"entrada": {...}, "auditoria": {...}}, "cossenos": [...]}
cada fase: {"vivos": {id_final: {"texto", "criado_por"}}, "segundos", ...}; criado_por = id da entrada do
conjunto que criou aquela linha (fusão no lugar mantém o criador original).

Uso:
    python memoria_dedup.py --services <repo>/services --dados memoria_dedup.json --modelo gemma4:12b --saida x.json
    python memoria_dedup.py --dados memoria_dedup.json --pontuar alfred_dedup.json odysseus_dedup.json
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memoria_pontuar import casa, normalizar  # noqa: E402


def metricas(dados: dict, fase: dict) -> dict:
    """
    Compara os sobreviventes de uma fase com o ideal (1 por grupo g*, 0 nos v*/l*/x*). Duas contas de perda:
    por id (nenhuma linha criada pelo grupo sobrou) e por informação (nenhuma linha do sujeito casa com
    chaves_grupo) — fundir dois fatos distintos numa frase que mantém os dois perde o id, não a informação.
    """
    entradas = dados["entradas"]
    por_id = {e["id"]: e for e in entradas}
    grupos: dict[str, list] = {}
    for e in entradas:
        grupos.setdefault(e["grupo"], []).append(e)
    vivos_por_grupo: dict[str, list] = {g: [] for g in grupos}
    for linha in fase["vivos"].values():
        criador = por_id.get(linha["criado_por"])
        if criador:
            vivos_por_grupo[criador["grupo"]].append(linha)
    textos = [v["texto"] for v in fase["vivos"].values()]

    def presente(chaves: list, sujeito: str) -> bool:
        return any(casa(t, chaves + [[normalizar(sujeito).split()[0]]]) for t in textos)

    m = {"eliminar_esperado": 0, "eliminados_certos": 0, "perdas_id": 0, "perdas_info": 0, "perdas_info_ids": [],
         "fusoes_sem_perda": 0, "contradicoes": 0, "contradicoes_ok": 0, "vagos": 0, "vagos_removidos": 0,
         "vagos_reescritos": 0, "lixo": 0, "lixo_removido": 0, "vencidos": 0, "vencidos_removidos": 0,
         "sobreviventes": len(fase["vivos"])}
    for g, membros in grupos.items():
        vivos = vivos_por_grupo[g]
        classe, sujeito = membros[0]["classe"], membros[0]["sujeito"]
        if g.startswith("g"):
            m["eliminar_esperado"] += len(membros) - 1
            m["eliminados_certos"] += len(membros) - max(len(vivos), 1)
            info = presente(dados["chaves_grupo"][g], sujeito)
            if not vivos:
                m["perdas_id"] += 1
                m["fusoes_sem_perda"] += info
            if not info:
                m["perdas_info"] += 1
                m["perdas_info_ids"].append(g)
            if g in dados["chaves_antigas"]:
                m["contradicoes"] += 1
                velho = [t for t in textos if casa(t, dados["chaves_antigas"][g] + [[normalizar(sujeito).split()[0]]])
                         and not casa(t, dados["chaves_grupo"][g])]
                m["contradicoes_ok"] += info and not velho
        else:
            chave = {"vago": "vagos", "lixo": "lixo", "vencido": "vencidos"}[classe]
            m[chave] += 1
            if not vivos:
                m[{"vagos": "vagos_removidos", "lixo": "lixo_removido", "vencidos": "vencidos_removidos"}[chave]] += 1
            elif classe == "vago" and all(normalizar(v["texto"]) != normalizar(f"{sujeito} {membros[0]['fato']}")
                                          for v in vivos):
                m["vagos_reescritos"] += 1
    return m


def rodar_alfred(a) -> dict:
    if not os.getenv("MEMORIA_DB"):
        sys.exit("defina MEMORIA_DB (banco temporário)")
    db = Path(os.environ["MEMORIA_DB"])
    for suf in ("", "-wal", "-shm"):
        Path(str(db) + suf).unlink(missing_ok=True)
    os.environ["MODEL_CLASSIFY"] = a.modelo
    sys.path.insert(0, a.services)
    from _shared import lib_memoria

    entradas = json.loads(Path(a.dados).read_text(encoding="utf-8"))["entradas"]
    saida = {"sistema": "alfred", "modelo": a.modelo, "fases": {}, "acoes": {}, "cossenos": []}
    criado_por: dict[int, str] = {}
    chamadas = {"decidir": 0}
    decidir_original = lib_memoria._decidir

    def decidir_contado(*args, **kw):
        chamadas["decidir"] += 1
        return decidir_original(*args, **kw)

    # Aquecimento do LLM fora da medida (carrega o modelo na GPU)
    t0 = time.perf_counter()
    lib_memoria.ollama_generate_json('Responda {"ok": true}', model=a.modelo)
    saida["aquecimento_s"] = round(time.perf_counter() - t0, 2)

    t0 = time.perf_counter()
    with mock.patch.object(lib_memoria, "_decidir", decidir_contado):
        for e in entradas:
            r = lib_memoria.guardar([{k: e[k] for k in ("fato", "sujeito", "tipo", "validade")}], "bench")
            if not r:
                saida["acoes"][e["id"]] = "erro"
                continue
            r = r[0]
            saida["acoes"][e["id"]] = r["acao"]
            if r["acao"] in ("novo", "substituiu"):
                criado_por[r["id"]] = e["id"]
    seg_entrada = time.perf_counter() - t0

    def fase(segundos: float, extra: dict) -> dict:
        vivos = {str(f["id"]): {"texto": f"{f['sujeito']} {f['fato']}", "criado_por": criado_por.get(f["id"], "?")}
                 for f in lib_memoria.listar()}
        return {"vivos": vivos, "segundos": round(segundos, 2), **extra}

    saida["fases"]["entrada"] = fase(seg_entrada, {"chamadas_llm": chamadas["decidir"],
                                                   "acoes": dict(saida["acoes"])})
    t0 = time.perf_counter()
    res = lib_memoria.auditar()
    saida["fases"]["auditoria"] = fase(time.perf_counter() - t0, {"resultado": res})

    # Cosseno bge-m3 de cada par (entrada × primeira do grupo, e pares "parecidos mas distintos")
    cfg = lib_memoria._cfg()
    vet = {e["id"]: lib_memoria._embed(e["fato"], e["sujeito"], cfg) for e in entradas}
    primeira = {}
    for e in entradas:
        primeira.setdefault(e["grupo"], e["id"])
    pares = [(e["id"], primeira[e["grupo"]], e["classe"]) for e in entradas if primeira[e["grupo"]] != e["id"]]
    pares += [("a08", "a09", "relacionado"), ("a10", "a11", "relacionado"), ("a16", "a17", "relacionado"),
              ("a14", "a01", "outro_sujeito"), ("a13", "a12", "outro_sujeito"), ("a18", "a19", "relacionado")]
    for x, y, classe in (p for p in pares if p[0] in vet and p[1] in vet):
        saida["cossenos"].append({"a": x, "b": y, "classe": classe,
                                  "cos": round(lib_memoria._cos(vet[x], vet[y]), 4)})
    return saida


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--services")
    ap.add_argument("--dados", required=True)
    ap.add_argument("--modelo")
    ap.add_argument("--saida")
    ap.add_argument("--pontuar", nargs="+")
    a = ap.parse_args()
    dados = json.loads(Path(a.dados).read_text(encoding="utf-8"))
    if a.pontuar:
        for arq in a.pontuar:
            s = json.loads(Path(arq).read_text(encoding="utf-8"))
            for nome, fase in s["fases"].items():
                m = metricas(dados, fase)
                print(f"{s['sistema']} {s['modelo']} {nome}: {json.dumps(m, ensure_ascii=False)} "
                      f"segundos={fase['segundos']} chamadas_llm={fase.get('chamadas_llm', '-')}")
        return 0
    saida = rodar_alfred(a)
    Path(a.saida).write_text(json.dumps(saida, ensure_ascii=False, indent=1), encoding="utf-8")
    for nome, fase in saida["fases"].items():
        print(nome, json.dumps(metricas(dados, fase), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
