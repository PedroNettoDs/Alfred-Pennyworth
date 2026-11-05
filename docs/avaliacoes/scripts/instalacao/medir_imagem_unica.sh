#!/usr/bin/env bash
# Build da imagem única hipotética (imagem_unica.Dockerfile) DENTRO de uma bancada descartável com
# Docker. Nada é construído no Docker do host.
# Uso: [VARIANTES="enxuta"] [CT=alfred-aval-img] medir_imagem_unica.sh <repositório git> <pasta de saída>
# Se a bancada CT não existir, sobe uma só com Docker (sem instalar o Alfred) e a remove no fim.
# VARIANTES="fiel enxuta" também constrói a fiel (torch CUDA do PyPI, ~4 GB a mais de download);
# o padrão é só a enxuta (torch CPU) — a fiel se estima pelo tamanho das wheels CUDA.
set -uo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${1:?repositório}"; OUT="${2:?pasta de saída}"
mkdir -p "$OUT"; : >"$OUT/tempos.tsv"
CT=${CT:-alfred-aval-img}
source "$AQUI/lib_bancada.sh"
PROPRIA=0
if ! docker exec "$CT" true 2>/dev/null; then
    PROPRIA=1
    bancada_subir || { echo "bancada não subiu"; exit 1; }
    passo 19-docker 'sudo apt-get update -qq && sudo apt-get install -y -qq curl docker.io >/dev/null && sudo systemctl enable --now docker && sudo usermod -aG docker $USER'
fi

# contexto limpo: git archive do HEAD + estes scripts (sem venv, .env, data/ nem logs/)
CTX=$(mktemp -d); mkdir -p "$CTX/ctx/docs/avaliacoes/scripts/instalacao"
git -C "$REPO" archive HEAD | tar -x -C "$CTX/ctx"
cp "$AQUI"/*.sh "$AQUI"/*.Dockerfile "$CTX/ctx/docs/avaliacoes/scripts/instalacao/"
docker exec "$CT" rm -rf /tmp/ctx && docker cp "$CTX/ctx" "$CT:/tmp/ctx" && docker exec "$CT" chown -R $U /tmp/ctx
rm -rf "$CTX"
F=/tmp/ctx/docs/avaliacoes/scripts/instalacao/imagem_unica.Dockerfile
PY=${PYTHON:-3.14-slim}

passo 20-pull-base "docker pull -q python:$PY && docker image inspect -f '{{index .RepoDigests 0}} {{.Size}}' python:$PY"
for v in ${VARIANTES:-enxuta}; do
    passo 21-build-$v "docker build --no-cache --build-arg PYTHON=$PY --build-arg VARIANTE=$v -t alfred-unica:$v -f $F /tmp/ctx"
    passo 22-medida-$v "docker image inspect -f 'bytes {{.Size}} camadas {{len .RootFS.Layers}}' alfred-unica:$v
        echo gzip_bytes \$(docker save alfred-unica:$v | gzip -1 | wc -c)
        docker history --format '{{.Size}}\t{{.CreatedBy}}' alfred-unica:$v | cut -c1-110
        docker run --rm --entrypoint sh alfred-unica:$v -c 'du -sm /opt/venv /opt/alfred/services/*/venv/ 2>/dev/null
            S=\$(ls -d /opt/venv/lib/python3*/site-packages /opt/alfred/services/laya/venv/lib/python3*/site-packages 2>/dev/null | head -1)
            du -sm \$S/torch \$S/nvidia \$S/triton 2>/dev/null; /opt/alfred/services/laya/venv/bin/python -m pip list 2>/dev/null | grep -iE \"^(torch|laya) \"'"
done
# partida a frio da variante enxuta (pesos do Laya baixados na 1ª partida, como no host)
passo 23-partida-enxuta "docker rm -f alfred-unica >/dev/null 2>&1
    docker run -d --name alfred-unica --network host -e TZ=America/Sao_Paulo -e SHELL_EXECUTOR_TOKEN=x \
        -e PORT_SHELL_EXECUTOR=17071 -e PORT_RESEARCHER=17072 -e PORT_INBOX=17073 -e PORT_LAYA=17074 \
        alfred-unica:enxuta >/dev/null
    t0=\$(date +%s)
    for u in 17071/health 17072/health 17073/vivo 17074/health; do
        until curl -sf -o /dev/null --max-time 2 http://127.0.0.1:\$u; do
            (( \$(date +%s) - t0 > 600 )) && { echo FALHOU \$u; docker logs --tail 20 alfred-unica; exit 1; }; sleep 1
        done; echo OK \$u \$(( \$(date +%s) - t0 ))s
    done
    docker stats --no-stream --format 'mem {{.MemUsage}} cpu {{.CPUPerc}}' alfred-unica
    docker rm -f alfred-unica >/dev/null"
[[ $PROPRIA == 1 && "${MANTER:-0}" != 1 ]] && bancada_remover
exit 0
