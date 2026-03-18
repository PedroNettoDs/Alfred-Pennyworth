#!/bin/bash
TOKEN=$(grep WEBUI_API_TOKEN /mnt/SSD/alfred/.env | cut -d= -f2)
BASE="http://localhost:3000"
KB_PEDRO="b3dc99a4-f604-485d-b3b4-562be50fa2c7"
KB_ALFRED="621560c8-f240-470e-a1b8-d8a01fa0b094"

echo "=== Buscando todos os arquivos no Open WebUI ==="
ALL_FILES=$(curl -s "${BASE}/api/v1/files/" \
  -H "Authorization: Bearer ${TOKEN}")

TOTAL=$(echo "$ALL_FILES" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d))")
echo "Total de arquivos encontrados: $TOTAL"

echo ""
echo "=== Vinculando à KB Pedro (notas do vault Pedro) ==="
echo "$ALL_FILES" | python3 -c "
import sys, json, urllib.request

token = '${TOKEN}'
base = '${BASE}'
kb_pedro = '${KB_PEDRO}'
kb_alfred = '${KB_ALFRED}'

files = json.load(sys.stdin)
ok = 0
erro = 0

for f in files:
    fid = f.get('id','')
    fname = f.get('filename','')
    
    # Notas do vault alfred vão para KB alfred, resto para KB pedro
    kb = kb_alfred if '/alfred/' in f.get('meta', {}).get('path', '') else kb_pedro
    
    req = urllib.request.Request(
        f'{base}/api/v1/knowledge/{kb}/file/add',
        data=json.dumps({'file_id': fid}).encode(),
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f'  [ok] {fname}')
            ok += 1
    except Exception as e:
        print(f'  [skip] {fname}')
        erro += 1

print(f'Vinculados: {ok} | Ignorados: {erro}')
"