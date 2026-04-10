"""
Alfred — loader de templates de prompts.

Carrega arquivos .md em services/_shared/prompts/ com frontmatter YAML obrigatório.
Valida variáveis declaradas antes de renderizar.

    from _shared.lib_templates import load_template, render_template
"""

import sys
from pathlib import Path

import yaml

try:
    from _shared.lib_alfred import log
except ImportError:
    from datetime import datetime as _dt
    def log(msg: str):  # type: ignore[misc]
        print(f"[{_dt.now().strftime('%H:%M:%S')}] {msg}", flush=True)

PROMPTS_DIR = Path(__file__).parent / "prompts"
_cache: dict[str, dict] = {}


class SafeDict(dict):
    """Dict que retorna {key} se a chave não existir (não quebra .format_map)."""
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def load_template(name: str) -> dict:
    """Carrega template pelo nome (sem extensão .md). Cacheia em memória."""
    if name in _cache:
        return _cache[name]

    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Template não encontrado: {path}")

    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError(f"Template '{name}' sem frontmatter YAML")

    end = raw.find("\n---", 3)
    if end == -1:
        raise ValueError(f"Template '{name}' com frontmatter mal-formado")

    frontmatter = yaml.safe_load(raw[3:end].strip()) or {}
    body = raw[end + 4:].lstrip("\n")

    for required in ("name", "description", "variables"):
        if required not in frontmatter:
            raise ValueError(f"Template '{name}' sem campo '{required}' no frontmatter")

    if not isinstance(frontmatter["variables"], list):
        raise ValueError(f"Template '{name}' com 'variables' que não é lista")

    template = {
        "name":        frontmatter["name"],
        "description": frontmatter["description"],
        "variables":   frontmatter["variables"],
        "body":        body,
    }
    _cache[name] = template
    return template


def render_template(name: str, values: dict) -> str:
    """
    Renderiza template validando que todas as variáveis declaradas estão em values.

    Variáveis extras em values são ignoradas (com aviso de log).
    Variáveis faltantes levantam ValueError.
    """
    tpl = load_template(name)
    declared = set(tpl["variables"])
    provided = set(values.keys())

    missing = declared - provided
    if missing:
        raise ValueError(
            f"Template '{name}' requer variáveis não fornecidas: {sorted(missing)}"
        )

    extra = provided - declared
    if extra:
        log(f"[lib_templates] '{name}' recebeu variáveis extras (ignoradas): {sorted(extra)}")

    return tpl["body"].format_map(SafeDict(values))


# ── CLI rápida pra debug ──────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        templates = sorted(PROMPTS_DIR.glob("*.md"))
        print(f"=== Templates em {PROMPTS_DIR} ===")
        for t in templates:
            try:
                tpl = load_template(t.stem)
                print(f"\n  {tpl['name']}")
                print(f"    description: {tpl['description']}")
                print(f"    variables:   {tpl['variables']}")
            except Exception as e:
                print(f"  {t.stem}: ERRO — {e}")

    elif cmd == "show" and len(sys.argv) >= 3:
        tpl = load_template(sys.argv[2])
        print(f"--- {tpl['name']} ---")
        print(f"description: {tpl['description']}")
        print(f"variables:   {tpl['variables']}")
        print()
        print(tpl["body"])

    else:
        print("Uso: python3 lib_templates.py list | show <nome>")
        sys.exit(1)
