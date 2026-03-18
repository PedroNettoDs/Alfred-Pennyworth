#!/bin/bash
TOKEN=$(grep WEBUI_API_TOKEN /mnt/SSD/alfred/.env | cut -d= -f2)
BASE="http://localhost:3000"

KB_PEDRO="b3dc99a4-f604-485d-b3b4-562be50fa2c7"
KB_ALFRED="621560c8-f240-470e-a1b8-d8a01fa0b094"

VAULT_PEDRO="/home/pedro.netto/Obsidian - MrNotte"
VAULT_ALFRED="/mnt/SSD/alfred/vaults/alfred"

upload_vault() {
  local vault="$1"
  local kb_id="$2"
  local label="$3"
  local ok=0 erro=0

  echo "=== Indexando $label ==="

  find "$vault" -name "*.md" | while read -r file; do
    # Passo 1: upload do arquivo → recebe file_id
    file_id=$(curl -s -X POST "${BASE}/api/v1/files/" \
      -H "Authorization: Bearer ${TOKEN}" \
      -F "file=@${file};type=text/plain" \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null)

    if [[ -z "$file_id" ]]; then
      echo "  [ERRO upload] $(basename "$file")"
      continue
    fi

    # Passo 2: vincular file_id à Knowledge Base
    result=$(curl -s -X POST "${BASE}/api/v1/knowledge/${kb_id}/file/add" \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "Content-Type: application/json" \
      -d "{\"file_id\": \"${file_id}\"}" \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if 'id' in d else f'ERRO: {d}')" 2>/dev/null)

    echo "  [$result] $(basename "$file")"
  done

  echo "=== $label concluído ==="
}

upload_vault "$VAULT_PEDRO" "$KB_PEDRO" "Vault Pedro"
upload_vault "$VAULT_ALFRED" "$KB_ALFRED" "Vault Alfred"