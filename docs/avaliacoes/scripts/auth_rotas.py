#!/usr/bin/env python3
"""
Inventário ESTÁTICO das rotas HTTP do Alfred e do tipo de autenticação de cada uma.

Só lê o código-fonte (módulo `ast`): não importa os serviços, não abre socket, não manda requisição.

Uso (da raiz do repositório):
    python3 -I docs/avaliacoes/scripts/auth_rotas.py            # tabela
    python3 -I docs/avaliacoes/scripts/auth_rotas.py --json     # JSON

Classificação por rota (olhando o corpo da função):
    bearer    chama check_auth(...)                      → Authorization: Bearer <token mestre>
    webhook   chama check_webhook_auth(...)              → apikey da Evolution OU token mestre
    painel    rota do APIRouter /painel (sessão + rede)  → login com o token mestre; só 127.0.0.1
    sessao|bearer  chama painel.sessao_valida(...)       → cookie do Painel OU Bearer
    loopback  confere request.client.host em 127.0.0.1   → sem token, só a própria máquina
    executor  compara `authorization != f"Bearer {TOKEN}"` direto no corpo
    nenhuma   nada disso (liveness/health, por desenho)
"""
import ast
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[3]
ALVOS = {
    "inbox (7073)": RAIZ / "services" / "inbox" / "main.py",
    "executor (7071)": RAIZ / "services" / "shell-executor" / "main.py",
    "researcher (7072)": RAIZ / "services" / "researcher" / "main.py",
    "painel (7073 /painel)": RAIZ / "services" / "inbox" / "painel.py",
}
METODOS = {"get", "post", "put", "delete", "patch"}


def _rotas(arvore):
    for no in ast.walk(arvore):
        if not isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in no.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in METODOS and dec.args
                    and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str)):
                yield dec.func.attr.upper(), dec.args[0].value, no


def _tipo(fn, fonte, painel):
    if painel:
        return "painel"
    chamadas = {c.func.id if isinstance(c.func, ast.Name) else c.func.attr
                for c in ast.walk(fn) if isinstance(c, ast.Call)
                and isinstance(c.func, (ast.Name, ast.Attribute))}
    corpo = ast.get_source_segment(fonte, fn) or ""
    if "sessao_valida" in chamadas:
        return "sessao|bearer"
    if "check_webhook_auth" in chamadas:
        return "webhook"
    if "check_auth" in chamadas:
        return "bearer"
    if "127.0.0.1" in corpo and "client" in corpo:
        return "loopback"
    if 'f"Bearer {TOKEN}"' in corpo:
        return "executor"
    return "nenhuma"


def inventario():
    linhas = []
    for servico, caminho in ALVOS.items():
        fonte = caminho.read_text(encoding="utf-8")
        arvore = ast.parse(fonte)
        painel = servico.startswith("painel")
        prefixo = "/painel" if painel else ""
        for metodo, rota, fn in sorted(_rotas(arvore), key=lambda t: t[2].lineno):
            linhas.append({"servico": servico, "metodo": metodo, "rota": prefixo + rota,
                           "auth": _tipo(fn, fonte, painel), "linha": fn.lineno})
    return linhas


def main():
    linhas = inventario()
    if "--json" in sys.argv:
        print(json.dumps(linhas, ensure_ascii=False, indent=1))
        return
    for l in linhas:
        print(f"{l['servico']:<22} {l['metodo']:<5} {l['rota']:<42} {l['auth']}")
    print()
    por = {}
    for l in linhas:
        por.setdefault((l["servico"], l["auth"]), 0)
        por[(l["servico"], l["auth"])] += 1
    for (srv, aut), n in sorted(por.items()):
        print(f"{srv:<22} {aut:<14} {n}")


if __name__ == "__main__":
    main()
