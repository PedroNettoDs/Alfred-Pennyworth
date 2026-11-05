#!/usr/bin/env python3
"""Reprodutibilidade estática de um repositório: o que fica sem versão fixa.

Uso: python3 -I auditar_pins.py <raiz do repositório>   (só lê arquivos; não importa nada do repositório)

Conta:
  * linhas de requirements*.txt: exata (== ou URL com --hash), piso (>=, ~=, <...) ou solta (só o nome);
  * imagens dos docker-compose*.yml: digest (@sha256), tag fixa (versão) ou móvel (latest/main/dev/sem tag);
  * modelos Ollama citados no .env.example e nos padrões do instalar.sh: o Ollama não aceita
    `pull` por digest, então toda referência é móvel; marca à parte as sem tag ou :latest.
"""
import re
import sys
from pathlib import Path

IGNORAR = {"venv", ".venv", "node_modules", ".git", "site-packages"}
MOVEIS = {"latest", "main", "dev", "stable", "nightly", "edge"}


def arquivos(raiz, padrao):
    return sorted(p for p in raiz.rglob(padrao) if not IGNORAR & set(p.relative_to(raiz).parts))


def requirements(raiz):
    tabela = []
    for arq in arquivos(raiz, "requirements*.txt"):
        texto = arq.read_text(encoding="utf-8").replace("\\\n", " ")
        for linha in texto.splitlines():
            linha = linha.split(" #")[0].strip()
            if not linha or linha.startswith(("#", "-r", "-c", "--")):
                continue
            if "--hash" in linha or re.search(r"(?<![<>=!~])==(?!=)", linha):
                tipo = "exata"
            elif re.search(r"[<>~!]=?|===", linha):
                tipo = "piso"
            else:
                tipo = "solta"
            tabela.append((str(arq.relative_to(raiz)), linha.split(";")[0].split(" ")[0], tipo))
    return tabela


def imagens(raiz):
    tabela = []
    for arq in arquivos(raiz, "docker-compose*.yml") + arquivos(raiz, "*compose*.yaml"):
        for m in re.finditer(r"^\s*image:\s*[\"']?([^\"'\s#]+)", arq.read_text(encoding="utf-8"), re.M):
            ref = m.group(1)
            padrao = re.fullmatch(r"\$\{[A-Z_]+:-(.+)\}", ref)     # ${VAR:-padrão}: vale o padrão
            ref = padrao.group(1) if padrao else ref
            if "@sha256:" in ref:
                tipo = "digest"
            else:
                nome = ref.rsplit("/", 1)[-1]
                tag = nome.split(":", 1)[1] if ":" in nome else ""
                tipo = "móvel" if (not tag or tag in MOVEIS) else "tag fixa"
            tabela.append((str(arq.relative_to(raiz)), ref, tipo))
    return tabela


def modelos(raiz):
    refs = set()
    exemplo = raiz / ".env.example"
    if exemplo.exists():
        for m in re.finditer(r"^MODEL_[A-Z_]+=(\S+)", exemplo.read_text(encoding="utf-8"), re.M):
            refs.add((".env.example", m.group(1)))
    for arq in arquivos(raiz, "instalar.sh"):
        for m in re.finditer(r"\$\{ALFRED_MODEL_[A-Z_]+:-([^}$]+)\}", arq.read_text(encoding="utf-8")):
            refs.add((str(arq.relative_to(raiz)), m.group(1)))
    return sorted((origem, ref, "sem tag/latest" if ":" not in ref or ref.endswith(":latest") else "tag nomeada")
                  for origem, ref in refs)


def main():
    raiz = Path(sys.argv[1]).resolve()
    reqs, imgs, mods = requirements(raiz), imagens(raiz), modelos(raiz)

    def resumo(nome, tabela, tipos):
        cont = {t: sum(1 for *_, x in tabela if x == t) for t in tipos}
        print(f"{nome}: {len(tabela)} — " + ", ".join(f"{t} {n}" for t, n in cont.items()))

    print(f"# {raiz.name}")
    resumo("requirements", reqs, ("exata", "piso", "solta"))
    resumo("imagens", imgs, ("digest", "tag fixa", "móvel"))
    resumo("modelos Ollama", mods, ("tag nomeada", "sem tag/latest"))
    for titulo, tabela in (("requirements", reqs), ("imagens", imgs), ("modelos", mods)):
        print(f"\n## {titulo}")
        for linha in tabela:
            print("  " + " | ".join(linha))


if __name__ == "__main__":
    main()
