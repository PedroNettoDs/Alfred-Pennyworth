"""
Avaliação da memória — amostrador de ruído: a cada --intervalo segundos grava (JSONL) os modelos carregados
no Ollama (/api/ps), uso e memória da GPU (nvidia-smi) e a carga da CPU. Roda em paralelo à medição para
mostrar quando a produção (ou outra avaliação) usou o Ollama no meio dela. Só lê; não chama modelo nenhum.

Uso:  python memoria_ruido.py --saida ruido.jsonl [--ollama http://localhost:11434] [--intervalo 2]
      (termina com SIGTERM/SIGINT)
      python memoria_ruido.py --resumo ruido.jsonl --esperado gemma4:12b
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request


def amostra(ollama: str) -> dict:
    a = {"t": round(time.time(), 1), "carga": os.getloadavg()[0]}
    try:
        with urllib.request.urlopen(f"{ollama}/api/ps", timeout=3) as r:
            a["modelos"] = [m["name"] for m in json.load(r).get("models", [])]
    except Exception as e:
        a["modelos_erro"] = str(e)[:80]
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip().split(",")
        a["gpu_pct"], a["vram_mib"] = int(out[0]), int(out[1])
    except Exception:
        pass
    return a


def resumo(arq: str, esperados: list) -> dict:
    linhas = [json.loads(x) for x in open(arq, encoding="utf-8") if x.strip()]
    alheio = lambda m: m.split(":")[0] != "bge-m3" and m not in esperados  # noqa: E731 — bge-m3 é da medição
    alheias = [x for x in linhas if any(alheio(m) for m in x.get("modelos", []))]
    return {"amostras": len(linhas), "com_modelo_alheio": len(alheias),
            "modelos_alheios": sorted({m for x in alheias for m in x["modelos"] if alheio(m)}),
            "carga_cpu_mediana": sorted(x["carga"] for x in linhas)[len(linhas) // 2] if linhas else None,
            "vram_max_mib": max((x.get("vram_mib", 0) for x in linhas), default=None)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida")
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--intervalo", type=float, default=2.0)
    ap.add_argument("--resumo")
    ap.add_argument("--esperado", action="append", default=[])
    a = ap.parse_args()
    if a.resumo:
        print(json.dumps(resumo(a.resumo, a.esperado), ensure_ascii=False))
        return 0
    parar = []
    signal.signal(signal.SIGTERM, lambda *_: parar.append(1))
    signal.signal(signal.SIGINT, lambda *_: parar.append(1))
    with open(a.saida, "a", encoding="utf-8") as fh:
        while not parar:
            fh.write(json.dumps(amostra(a.ollama)) + "\n")
            fh.flush()
            time.sleep(a.intervalo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
