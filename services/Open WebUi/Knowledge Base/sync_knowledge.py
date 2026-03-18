#!/usr/bin/env python3
"""
Sync incremental do vault Obsidian para a Knowledge Base do Open WebUI.
Só re-indexa arquivos modificados desde a última execução.
"""

import os, sys, json, requests
from pathlib import Path
from datetime import datetime

BASE        = os.getenv("WEBUI_URL", "http://localhost:3000")
TOKEN       = os.getenv("WEBUI_TOKEN", "")
TIMESTAMP   = Path("/tmp/alfred_last_sync")
KB_PEDRO    = "b3dc99a4-f604-485d-b3b4-562be50fa2c7"
KB_ALFRED   = "621560c8-f240-470e-a1b8-d8a01fa0b094"
VAULT_PEDRO = Path("/home/pedro.netto/Obsidian - MrNotte")
VAULT_ALFRED = Path("/mnt/SSD/alfred/vaults/alfred")

HEADERS = {"Authorization": f"Bearer {TOKEN}"}

def get_last_sync():
    if TIMESTAMP.exists():
        return TIMESTAMP.stat().st_mtime
    return 0  # nunca rodou — sync completo

def find_modified(vault: Path, since: float):
    return [f for f in vault.rglob("*.md") if f.stat().st_mtime > since]

def get_all_files():
    r = requests.get(f"{BASE}/api/v1/files/", headers=HEADERS, timeout=30)
    return r.json() if r.ok else []

def delete_file(file_id: str):
    requests.delete(f"{BASE}/api/v1/files/{file_id}", headers=HEADERS, timeout=15)

def upload_file(filepath: Path):
    with open(filepath, "rb") as f:
        r = requests.post(
            f"{BASE}/api/v1/files/",
            headers=HEADERS,
            files={"file": (filepath.name, f, "text/plain")},
            timeout=30
        )
    if r.ok:
        return r.json().get("id")
    return None

def link_to_kb(file_id: str, kb_id: str):
    requests.post(
        f"{BASE}/api/v1/knowledge/{kb_id}/file/add",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"file_id": file_id},
        timeout=15
    )

def sync_vault(vault: Path, kb_id: str, label: str, since: float, existing: dict):
    modified = find_modified(vault, since)
    if not modified:
        print(f"[{label}] Nenhuma alteração desde o último sync.")
        return

    print(f"[{label}] {len(modified)} arquivo(s) modificado(s):")
    for filepath in modified:
        name = filepath.name

        # Deletar versão antiga se existir
        if name in existing:
            delete_file(existing[name])
            print(f"  [del] {name}")

        # Upload novo
        file_id = upload_file(filepath)
        if not file_id:
            print(f"  [ERRO upload] {name}")
            continue

        # Vincular à KB
        link_to_kb(file_id, kb_id)
        print(f"  [ok] {name}")

def main():
    if not TOKEN:
        print("ERRO: WEBUI_TOKEN não definido.")
        sys.exit(1)

    since = get_last_sync()
    if since == 0:
        print("Primeira execução — sync completo.")
    else:
        print(f"Sync incremental desde: {datetime.fromtimestamp(since).strftime('%Y-%m-%d %H:%M')}")

    # Mapear arquivos existentes por nome
    all_files = get_all_files()
    existing = {f["filename"]: f["id"] for f in all_files if f.get("filename","").endswith(".md")}

    sync_vault(VAULT_PEDRO, KB_PEDRO, "Vault Pedro", since, existing)
    sync_vault(VAULT_ALFRED, KB_ALFRED, "Vault Alfred", since, existing)

    # Atualizar timestamp
    TIMESTAMP.touch()
    print("\nSync concluído.")

if __name__ == "__main__":
    main()