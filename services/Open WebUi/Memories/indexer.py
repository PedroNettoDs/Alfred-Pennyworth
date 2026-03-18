#!/usr/bin/env python3
"""
Indexador dos vaults do Obsidian no Open WebUI (memories).
Uso: python3 indexer.py [pedro|alfred|both]
"""

import os
import sys
import requests
from pathlib import Path

WEBUI_URL = os.getenv("WEBUI_URL", "http://localhost:3000")
WEBUI_TOKEN = os.getenv("WEBUI_TOKEN", "")

VAULTS = {
    "pedro": {
        "path": os.getenv("VAULT_PEDRO", "/home/pedro.netto/Obsidian - MrNotte"),
    },
    "alfred": {
        "path": os.getenv("VAULT_ALFRED", "/mnt/SSD/alfred/vaults/alfred"),
    },
}

def get_headers():
    return {
        "Authorization": f"Bearer {WEBUI_TOKEN}",
        "Content-Type": "application/json",
    }

def already_indexed(content_preview: str) -> bool:
    """Verifica se o conteúdo já existe nas memories para evitar duplicatas."""
    try:
        resp = requests.get(
            f"{WEBUI_URL}/api/v1/memories/",
            headers=get_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            memories = resp.json()
            for m in memories:
                if content_preview in m.get("content", ""):
                    return True
    except Exception:
        pass
    return False

def index_vault(name: str, config: dict):
    vault_path = Path(config["path"])
    if not vault_path.exists():
        print(f"[{name}] Vault não encontrado: {vault_path}")
        return

    md_files = list(vault_path.rglob("*.md"))
    print(f"[{name}] {len(md_files)} arquivos .md encontrados em {vault_path}")

    ok = 0
    skip = 0
    duplicate = 0

    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore").strip()
            if len(content) < 50:
                skip += 1
                continue

            full_content = f"# {md_file.stem}\n\n{content}"
            preview = md_file.stem

            if already_indexed(preview):
                duplicate += 1
                continue

            resp = requests.post(
                f"{WEBUI_URL}/api/v1/memories/add",
                headers=get_headers(),
                json={"content": full_content},
                timeout=30,
            )
            if resp.status_code in (200, 201):
                ok += 1
                print(f"  [ok] {md_file.name}")
            else:
                print(f"  [warn] {md_file.name}: {resp.status_code} — {resp.text[:80]}")
        except Exception as e:
            print(f"  [erro] {md_file.name}: {e}")

    print(f"[{name}] Indexados: {ok} | Duplicatas ignoradas: {duplicate} | Curtos ignorados: {skip}")

if __name__ == "__main__":
    if not WEBUI_TOKEN:
        print("[erro] WEBUI_TOKEN não definido. Exporte a variável antes de rodar.")
        sys.exit(1)

    target = sys.argv[1] if len(sys.argv) > 1 else "both"

    if target in ("pedro", "both"):
        index_vault("pedro", VAULTS["pedro"])
    if target in ("alfred", "both"):
        index_vault("alfred", VAULTS["alfred"])

    print("Indexação concluída.")
