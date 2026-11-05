"""Conta quantas tentativas de encadeamento passam pela allowlist do shell-executor. Nada é executado.

Chama só `is_allowed()` de services/shell-executor/main.py, a mesma análise (`_analisar`) que o POST /execute faz
antes de rodar qualquer coisa. O segundo comando das tentativas é sempre `id` (inofensivo).

Uso (precisa de fastapi/pydantic, porque importa o main.py; o venv do executor serve, só é lido):
    /caminho/do/venv/bin/python -I docs/avaliacoes/scripts/shell_fuga.py services/shell-executor
"""

import importlib.util
import sys
from pathlib import Path

TENTATIVAS = {
    "; (sequência)": ["date; id", "date ;id", "date;id", "date ; id"],
    "&& / || / &": ["date && id", "date || id", "date & id", "date&id"],
    "$() e crase": ["date $(id)", "date `id`", 'date "$(id)"', "echo $(id)", "$(id)"],
    "variáveis": ["date $HOME", "date ${HOME}", 'date "$USER"', "PATH=/tmp date", "env id", "$SHELL -c id"],
    "quebra de linha e escape": ["date\nid", "date\rid", "date \\; id", "date\\\nid"],
    "redirecionamento": ["date > /tmp/x", "date >> /tmp/x", "date < /dev/null", "date 2>&1", "id 2>/dev/null"],
    "pipe para interpretador": ["date | sh", "date | bash", "ls | xargs id", "date |& id", "date | env id"],
    "caminhos relativos e absolutos": ["./run.sh", "../briefing/run.sh", "./../briefing/run.sh",
                                       "bash ../briefing/run.sh", "/usr/bin/id", "/bin/sh -c date",
                                       "../../services/briefing/run.sh"],
    "aspas (sem shell, viram texto literal)": ["d'a't'e'", 'da""te', "date '$(id)'", "date ';' id",
                                               "date '&&' id"],
}

# Das que passam, quais rodam só o comando permitido com o resto como texto (sem shell não há expansão).
LITERAIS = {"d'a't'e'", 'da""te', "date '$(id)'", "date ';' id", "date '&&' id"}


def main(pasta):
    spec = importlib.util.spec_from_file_location("executor_main", Path(pasta) / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    total = passou = literal = 0
    print("| categoria | tentativas | passam | passam como texto literal |")
    print("|---|---|---|---|")
    for cat, cmds in TENTATIVAS.items():
        ok = [c for c in cmds if mod.is_allowed(c)]
        lit = [c for c in ok if c in LITERAIS]
        total, passou, literal = total + len(cmds), passou + len(ok), literal + len(lit)
        print(f"| {cat} | {len(cmds)} | {len(ok)} | {len(lit)} |")
        for c in ok:
            print(f"|  ↳ passou: `{c!r}` | | | {'sim' if c in LITERAIS else 'NÃO'} |")
    print(f"| **total** | **{total}** | **{passou}** | **{literal}** |")


if __name__ == "__main__":
    main(sys.argv[1])
