#!/usr/bin/env python3
"""
Avaliação de Pesquisa — monta a planilha de conferência de citações de um relatório.

Escolhe 10 afirmações espaçadas por igual entre as que têm citação (profundo: [n]; iterativo: [texto](url);
rápido não cita, então entram 10 frases factuais quaisquer) e põe ao lado de cada uma o que a fonte citada
diz: a nota/descoberta que o modelo recebeu e o trecho da página baixada com mais termos em comum.
A conferência (sustenta / parcial / não sustenta / sem fonte) é feita por quem lê e anotada em julgamento.json.

  python3 docs/avaliacoes/scripts/citacoes.py /tmp/bench/profundo-tecnico.json > /tmp/conferir.md
"""

import json
import re
import sys
from pathlib import Path

AMOSTRA = 10


def _frases(relatorio: str) -> list[str]:
    corpo = relatorio.split("\n## Fontes")[0]
    frases = []
    for linha in corpo.splitlines():
        linha = linha.strip()
        if not linha or linha.startswith(("#", "---", "|--", "*Sintetizado")):
            continue
        frases += [f.strip() for f in re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-Ú*\-])", linha)
                   if len(f.strip()) > 30 and not f.strip().endswith("?")]
    return frases


def _termos(texto: str) -> set[str]:
    return {t for t in re.findall(r"[a-zà-ú0-9]+", texto.lower()) if len(t) > 3}


def _melhor_trecho(texto: str, frase: str, largura: int = 600) -> str:
    if not texto:
        return "(página não guardada)"
    alvo = _termos(frase)
    melhor, pos = -1, 0
    for i in range(0, max(1, len(texto) - largura), 150):
        n = len(alvo & _termos(texto[i:i + largura]))
        if n > melhor:
            melhor, pos = n, i
    return texto[pos:pos + largura].replace("\n", " ")


def _espacadas(itens: list, n: int) -> list:
    if len(itens) <= n:
        return itens
    return [itens[round(i * (len(itens) - 1) / (n - 1))] for i in range(n)]


def main() -> int:
    caminho = Path(sys.argv[1])
    run = json.loads(caminho.read_text(encoding="utf-8"))
    paginas_arq = caminho.with_name(caminho.stem + "-paginas.json")
    paginas = json.loads(paginas_arq.read_text(encoding="utf-8")) if paginas_arq.exists() else {}
    fontes = run.get("fontes", [])
    por_n = {f["n"]: f for f in fontes}
    por_url = {f["url"]: f for f in fontes}
    frases = _frases(run.get("relatorio") or "")

    if run["modo"].startswith("profundo"):
        citadas = [f for f in frases if re.search(r"\[\d+\](?!\()", f)]
    elif run["modo"] == "iterativo":
        # o modelo às vezes cita "[Descoberta 3]" em vez da URL: o número é da janela das últimas 10 descobertas
        # que a síntese recebeu (a lista não sai no relatório), então mapeia pela última janela
        citadas = [f for f in frases if re.search(r"\]\(https?://|Descoberta \d|\[\d+(?:, ?\d+)*\](?!\()", f)]
        janela = max(0, len(fontes) - 10)
    else:
        citadas = [f for f in frases if re.search(r"\d|[A-Z][a-z]+[A-Z]|=", f)]
        if len(citadas) < AMOSTRA:
            citadas = frases

    print(f"# {run['modo']} / {run['pergunta']} — {len(citadas)} frases candidatas de {len(frases)}\n")
    for i, frase in enumerate(_espacadas(citadas, AMOSTRA), 1):
        print(f"## {i}. {frase}\n")
        if run["modo"].startswith("profundo"):
            refs = [por_n.get(int(n)) for n in dict.fromkeys(re.findall(r"\[(\d+)\](?!\()", frase))]
        elif run["modo"] == "iterativo":
            refs = [por_url.get(u.rstrip(").,")) for u in dict.fromkeys(re.findall(r"\]\((https?://[^)\s]+)\)", frase))]
            for grupo in re.findall(r"Descobertas? ([\d ,e]+)|\[(\d+(?:, ?\d+)*)\](?!\()", frase):
                grupo = "".join(grupo)
                refs += [por_n.get(janela + int(k)) for k in re.findall(r"\d+", grupo)]
        else:
            refs = fontes
        for ref in refs:
            if not ref:
                print("- fonte citada NÃO está na lista de fontes (URL ou número inventado)\n")
                continue
            if run["modo"].startswith("rapido"):
                continue
            print(f"- [{ref.get('n')}] {ref['url']}")
            if "fatos" in ref:
                print("  - notas: " + " | ".join(ref["fatos"]))
            else:
                print(f"  - summary: {ref.get('summary', '')[:700]}")
            print(f"  - página: {_melhor_trecho(paginas.get(ref['url'], {}).get('texto', ''), frase)}\n")
        if run["modo"].startswith("rapido"):
            # o prompt do rápido não pede citação; quando o modelo numera mesmo assim ([4, 5]), mostra esses trechos
            numeros = [int(k) for g in re.findall(r"\[([\d ,]+)\](?!\()", frase) for k in re.findall(r"\d+", g)]
            melhores = [por_n[k] for k in numeros if k in por_n] or sorted(
                fontes, key=lambda f: -len(_termos(frase) & _termos(f["titulo"] + " " + f["trecho"])))[:2]
            for f in melhores:
                print(f"- trecho [{f['n']}] {f['url']}: {f['trecho']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
