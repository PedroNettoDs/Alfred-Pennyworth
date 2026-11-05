#!/usr/bin/env python3
"""
Avaliação de Pesquisa — junta resultados/metricas.jsonl, resultados/lock.jsonl e o julgamento.json em tabelas
Markdown. Se um modo-pergunta aparece mais de uma vez em metricas.jsonl, vale a última linha.

julgamento.json (feito à mão, lendo a saída de citacoes.py), uma chave por "<modo>-<pergunta>":
  "citacoes":    10 letras — S sustenta, P parcial, N a fonte citada não diz aquilo, F fonte inexistente
                 (rápido não cita: S = algum trecho do SearXNG diz aquilo, N = nenhum diz)
  "cobertura":   4 notas (1, 0.5 ou 0), uma por aspecto de perguntas.json
  "alucinacoes": afirmações conferidas que nenhuma fonte da execução sustenta E que são falsas ou inverificáveis

  python3 docs/avaliacoes/scripts/resumo.py docs/avaliacoes/scripts/resultados docs/avaliacoes/scripts/julgamento.json
"""

import json
import sys
from pathlib import Path

MODOS = ("rapido", "rapido-general", "profundo", "profundo-general", "iterativo")
PERGUNTAS = ("tecnico", "noticia", "produtos", "academico", "brasil")


def _modelos(run: dict) -> str:
    return ", ".join(f"{m} ×{v['chamadas']}" for m, v in run["medidas"]["llm_por_modelo"].items())


def main() -> int:
    pasta = Path(sys.argv[1])
    julg = json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv) > 2 and Path(sys.argv[2]).exists() else {}
    lidas = {}
    for linha in (pasta / "metricas.jsonl").read_text().splitlines():
        d = json.loads(linha)
        lidas[f"{d['modo']}-{d['pergunta']}"] = d
    espera = {k: d.get("espera_lock_s") for k, d in lidas.items()}
    if (pasta / "lock.jsonl").exists():
        for linha in (pasta / "lock.jsonl").read_text().splitlines():
            d = json.loads(linha)
            espera[f"{d['modo']}-{d['pergunta']}"] = d["espera_lock_s"]
    runs = {f"{m}-{p}": lidas[f"{m}-{p}"] for p in PERGUNTAS for m in MODOS if f"{m}-{p}" in lidas}

    print("### Por execução\n")
    print("| modo | pergunta | status | tempo (s) | buscas | URLs buscadas | páginas lidas | fontes no relatório "
          "| tokens entrada | tokens saída | modelos (chamadas) | carga de modelo (s) | espera lock (s) | palavras |")
    print("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|")
    for chave, r in runs.items():
        m = r["medidas"]
        print(f"| {r['modo']} | {r['pergunta']} | {r['status']} | {r['tempo_total_s']} | {m['buscas']} | "
              f"{m['urls_buscadas']} | {m['paginas_baixadas']} | {m['fontes_usadas']} | {m['tokens_prompt']} | "
              f"{m['tokens_saida']} | {_modelos(r)} | {m['carga_modelo_s']} | {'' if espera.get(chave) is None else espera[chave]} | "
              f"{m['palavras_relatorio']} |")

    print("\n### Médias por modo\n")
    print("Custo (tempo, URLs, páginas, tokens): média das execuções ok. Qualidade: todas as execuções julgadas — uma "
          "falha conta cobertura 0 e não entra nas citações.\n")
    print("| modo | ok / total | tempo (s) | URLs buscadas | páginas lidas | fontes | tokens entrada | tokens saída | "
          "citações sustentadas | cobertura | alucinações por relatório |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for modo in MODOS:
        todas = [r for r in runs.values() if r["modo"] == modo]
        ok = [r for r in todas if r["status"] == "ok"]
        if not todas:
            continue
        med = lambda f: round(sum(f(r) for r in ok) / len(ok), 1) if ok else "—"
        js = [julg[f"{modo}-{r['pergunta']}"] for r in todas if f"{modo}-{r['pergunta']}" in julg]
        if js:
            letras = "".join("".join(j["citacoes"]) for j in js)
            sust = (f"{round(100 * (letras.count('S') + 0.5 * letras.count('P')) / len(letras))}% ({len(letras)})"
                    if letras else "—")
            cob = f"{round(100 * sum(sum(j['cobertura']) for j in js) / (4 * len(js)))}% ({len(js)})"
            aluc = round(sum(j["alucinacoes"] for j in js) / len(js), 1)
        else:
            sust = cob = aluc = "—"
        print(f"| {modo} | {len(ok)} / {len(todas)} | {med(lambda r: r['tempo_total_s'])} | "
              f"{med(lambda r: r['medidas']['urls_buscadas'])} | {med(lambda r: r['medidas']['paginas_baixadas'])} | "
              f"{med(lambda r: r['medidas']['fontes_usadas'])} | {med(lambda r: r['medidas']['tokens_prompt'])} | "
              f"{med(lambda r: r['medidas']['tokens_saida'])} | {sust} | {cob} | {aluc} |")

    if julg:
        print("\n### Qualidade por execução\n")
        print("| execução | citações (S/P/N/F) | cobertura (4 aspectos) | alucinações | observação |")
        print("|---|---|---|---:|---|")
        for chave in runs:
            if chave in julg:
                j = julg[chave]
                c = "".join(j["citacoes"])
                print(f"| {chave} | {c.count('S')}/{c.count('P')}/{c.count('N')}/{c.count('F')} | "
                      f"{' '.join(str(x) for x in j['cobertura'])} | {j['alucinacoes']} | {j.get('obs', '')} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
