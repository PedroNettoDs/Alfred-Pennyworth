#!/usr/bin/env python3
"""
Sync incremental do vault Obsidian para a Knowledge Base do Open WebUI.
Só re-indexa arquivos modificados desde a última execução.

Variáveis de ambiente obrigatórias:
  WEBUI_API_TOKEN, RESEARCHER_KB_PEDRO_ID, RESEARCHER_KB_ALFRED_ID

Usa httpx (está nos venvs do researcher e do inbox; nenhum Python do Alfred tem a lib requests):
  services/researcher/venv/bin/python services/scripts/sync-knowledge.py
"""

import os
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ERRO: módulo httpx ausente — rode com o Python de um venv do Alfred, ex.: "
          "services/researcher/venv/bin/python services/scripts/sync-knowledge.py", file=sys.stderr)
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent.parent
BASE         = os.getenv("WEBUI_URL", "http://localhost:3001")
TOKEN        = os.getenv("WEBUI_API_TOKEN", "")
TIMESTAMP    = PROJECT_ROOT / "logs" / "alfred_last_sync"
KB_PEDRO     = os.getenv("RESEARCHER_KB_PEDRO_ID", "")
KB_ALFRED    = os.getenv("RESEARCHER_KB_ALFRED_ID", "")
VAULT_PEDRO  = Path(os.getenv("VAULT_PEDRO", ""))
VAULT_ALFRED = Path(os.getenv("VAULT_ALFRED", ""))

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def get_last_sync():
    if TIMESTAMP.exists():
        return TIMESTAMP.stat().st_mtime
    return 0


def find_modified(vault: Path, since: float):
    if not vault.exists():
        return []
    return [f for f in vault.rglob("*.md") if f.stat().st_mtime > since]


def upload_file(filepath: Path):
    with open(filepath, "rb") as f:
        r = httpx.post(
            f"{BASE}/api/v1/files/",
            headers=HEADERS,
            files={"file": (filepath.name, f, "text/plain")},
            timeout=30,
        )
    return r.json().get("id") if r.is_success else None


def link_to_kb(file_id: str, kb_id: str):
    httpx.post(
        f"{BASE}/api/v1/knowledge/{kb_id}/file/add",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"file_id": file_id},
        timeout=15,
    )


def delete_file(file_id: str):
    httpx.delete(f"{BASE}/api/v1/files/{file_id}", headers=HEADERS, timeout=15)


def get_all_files():
    r = httpx.get(f"{BASE}/api/v1/files/", headers=HEADERS, timeout=30)
    return r.json() if r.is_success else []


def sync_vault(vault: Path, kb_id: str, label: str, since: float, existing: dict):
    if not kb_id:
        print(f"[{label}] KB ID não configurado, pulando.")
        return

    modified = find_modified(vault, since)
    if not modified:
        print(f"[{label}] Nenhuma alteração desde o último sync.")
        return

    print(f"[{label}] {len(modified)} arquivo(s) modificado(s):")
    for filepath in modified:
        name = filepath.name
        if name in existing:
            delete_file(existing[name])
            print(f"  [del] {name}")

        file_id = upload_file(filepath)
        if not file_id:
            print(f"  [ERRO upload] {name}")
            continue

        link_to_kb(file_id, kb_id)
        print(f"  [ok] {name}")


def main():
    if not TOKEN:
        print("ERRO: WEBUI_API_TOKEN não definido — gere a chave no Open WebUI (Settings → Account → API Keys), "
              "ponha no .env e passe no ambiente (o researcher já passa a do .env). Nada foi sincronizado.",
              file=sys.stderr)
        sys.exit(1)

    since = get_last_sync()
    if since == 0:
        print("Primeira execução — sync completo.")

    existing_files = get_all_files()
    existing = {f.get("filename", ""): f.get("id", "") for f in existing_files}

    sync_vault(VAULT_ALFRED, KB_ALFRED, "alfred", since, existing)
    sync_vault(VAULT_PEDRO, KB_PEDRO, "pedro", since, existing)

    TIMESTAMP.parent.mkdir(parents=True, exist_ok=True)
    TIMESTAMP.touch()
    print("Sync concluído.")


if __name__ == "__main__":
    main()
