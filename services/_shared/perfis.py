"""
Alfred — loader de perfis.yml.

Carrega a fonte única de verdade e expõe acessores tipados:
    from _shared.perfis import get_perfil, get_briefing, get_scanner, get_fonte

Validação leve: verifica que referências cruzadas existem (um briefing
não pode apontar pra um perfil ou fonte inexistente).
"""

import sys
from pathlib import Path
from typing import Optional

import yaml

PERFIS_PATH = Path(__file__).parent / "perfis.yml"
_cache: Optional[dict] = None


def load(force_reload: bool = False) -> dict:
    """Carrega perfis.yml (com cache em memória)."""
    global _cache
    if _cache is not None and not force_reload:
        return _cache

    if not PERFIS_PATH.exists():
        raise FileNotFoundError(f"perfis.yml não encontrado em {PERFIS_PATH}")

    with open(PERFIS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    _validate(data)
    _cache = data
    return data


def _validate(data: dict):
    """Validação leve: checa referências cruzadas."""
    perfis = data.get("perfis", {})
    fontes = data.get("fontes", {})
    erros = []

    for nome, briefing in data.get("briefings", {}).items():
        perfil_ref = briefing.get("perfil")
        if perfil_ref and perfil_ref not in perfis:
            erros.append(f"briefing '{nome}' referencia perfil inexistente: {perfil_ref}")
        for fonte_ref in briefing.get("fontes", []):
            if fonte_ref not in fontes:
                erros.append(f"briefing '{nome}' referencia fonte inexistente: {fonte_ref}")

    for nome, scanner in data.get("scanners", {}).items():
        perfil_ref = scanner.get("perfil")
        if perfil_ref and perfil_ref not in perfis:
            erros.append(f"scanner '{nome}' referencia perfil inexistente: {perfil_ref}")
        for fonte_ref in scanner.get("fontes", []):
            if fonte_ref not in fontes:
                erros.append(f"scanner '{nome}' referencia fonte inexistente: {fonte_ref}")

    if erros:
        raise ValueError("perfis.yml tem erros:\n  - " + "\n  - ".join(erros))


def get_perfil(nome: str) -> dict:
    data = load()
    if nome not in data.get("perfis", {}):
        raise KeyError(f"Perfil não encontrado: {nome}")
    return data["perfis"][nome]


def get_fonte(nome: str) -> dict:
    data = load()
    if nome not in data.get("fontes", {}):
        raise KeyError(f"Fonte não encontrada: {nome}")
    return data["fontes"][nome]


def get_briefing(nome: str) -> dict:
    data = load()
    if nome not in data.get("briefings", {}):
        raise KeyError(f"Briefing não encontrado: {nome}")
    briefing = data["briefings"][nome].copy()
    # Resolve referências
    briefing["_perfil"] = get_perfil(briefing["perfil"])
    briefing["_fontes"] = [get_fonte(f) for f in briefing.get("fontes", [])]
    return briefing


def get_scanner(nome: str) -> dict:
    data = load()
    if nome not in data.get("scanners", {}):
        raise KeyError(f"Scanner não encontrado: {nome}")
    scanner = data["scanners"][nome].copy()
    scanner["_perfil"] = get_perfil(scanner["perfil"])
    scanner["_fontes"] = [get_fonte(f) for f in scanner.get("fontes", [])]
    return scanner


def list_all() -> dict:
    """Retorna um sumário de tudo que está definido (útil pra debug/CLI)."""
    data = load()
    return {
        "perfis": list(data.get("perfis", {}).keys()),
        "fontes": list(data.get("fontes", {}).keys()),
        "briefings": list(data.get("briefings", {}).keys()),
        "scanners": list(data.get("scanners", {}).keys()),
    }


# ── CLI rápida pra debug ──────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        sumario = list_all()
        print("=== perfis.yml carregado ===")
        for categoria, items in sumario.items():
            print(f"\n{categoria}:")
            for item in items:
                print(f"  - {item}")

    elif cmd == "perfil" and len(sys.argv) >= 3:
        import json
        print(json.dumps(get_perfil(sys.argv[2]), indent=2, ensure_ascii=False))

    elif cmd == "briefing" and len(sys.argv) >= 3:
        import json
        b = get_briefing(sys.argv[2])
        # Remove os _resolvidos pra ficar legível
        b.pop("_perfil", None)
        b.pop("_fontes", None)
        print(json.dumps(b, indent=2, ensure_ascii=False))

    elif cmd == "scanner" and len(sys.argv) >= 3:
        import json
        s = get_scanner(sys.argv[2])
        s.pop("_perfil", None)
        s.pop("_fontes", None)
        print(json.dumps(s, indent=2, ensure_ascii=False))

    else:
        print("Uso:")
        print("  python3 perfis.py list")
        print("  python3 perfis.py perfil <nome>")
        print("  python3 perfis.py briefing <nome>")
        print("  python3 perfis.py scanner <nome>")
        sys.exit(1)