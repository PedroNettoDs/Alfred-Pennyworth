"""
Avaliação da memória — roda a extração do Alfred (lib_memoria.extrair, código de produção) sobre
memoria_trechos.json e grava os fatos extraídos e o tempo de cada trecho.

guardar() é trocado pela identidade: mede-se só a extração (portão + prompt + LLM + _limpar), sem embeddings
nem deduplicação. MEMORIA_DB aponta para um banco temporário (nunca o real); o .env não é lido — as
variáveis de Ollama vêm do ambiente de quem chama (ver memoria_rodar.sh).

Uso:
    python memoria_extracao.py --services <repo>/services --trechos memoria_trechos.json \
        --modelo gemma4:12b --rodadas 3 --saida alfred_12b.json
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock


def texto_do_trecho(t: dict) -> str:
    """Conversa vira transcrição 'Pedro: ... / Alfred: ...'; mensagem e e-mail vão como vieram."""
    if "turnos" not in t:
        return t["texto"]
    nomes = {"user": "Pedro", "assistant": "Alfred"}
    return "\n".join(f"{nomes[papel]}: {conteudo}" for papel, conteudo in t["turnos"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--services", required=True)
    ap.add_argument("--trechos", required=True)
    ap.add_argument("--modelo", required=True)
    ap.add_argument("--rodadas", type=int, default=3)
    ap.add_argument("--saida", required=True)
    a = ap.parse_args()

    if not os.getenv("MEMORIA_DB"):
        sys.exit("defina MEMORIA_DB (banco temporário)")
    os.environ["MODEL_CLASSIFY"] = a.modelo          # _modelo(): seção memoria: vazia → MODEL_CLASSIFY
    sys.path.insert(0, a.services)
    from _shared import lib_memoria

    trechos = json.loads(Path(a.trechos).read_text(encoding="utf-8"))["trechos"]
    saida = {"sistema": "alfred", "modelo": a.modelo, "variante": "lib_memoria.extrair", "rodadas": []}
    with mock.patch.object(lib_memoria, "guardar", lambda fatos, fonte="": fatos):
        # Aquecimento: carrega o modelo na GPU; o tempo de carga fica registrado à parte
        t0 = time.perf_counter()
        lib_memoria.extrair("Eu moro em uma cidade do interior e trabalho com software.", "aquecimento", "Pedro")
        saida["aquecimento_s"] = round(time.perf_counter() - t0, 2)
        for n in range(a.rodadas):
            rod = {"inicio": time.time(), "trechos": {}}
            for t in trechos:
                texto = texto_do_trecho(t)
                portao = lib_memoria._vale_extrair(texto)
                t0 = time.perf_counter()
                fatos = lib_memoria.extrair(texto, t["fonte"], t["sujeito"])
                dt = time.perf_counter() - t0
                rod["trechos"][t["id"]] = {"fatos": [{"fato": f["fato"], "sujeito": f["sujeito"], "tipo": f["tipo"],
                                                      "validade": f["validade"]} for f in fatos],
                                           "segundos": round(dt, 3), "chamou_llm": portao}
                print(f"[{n + 1}] {t['id']} {dt:5.2f}s {len(fatos)} fato(s)", flush=True)
            rod["fim"] = time.time()
            saida["rodadas"].append(rod)
    Path(a.saida).write_text(json.dumps(saida, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
