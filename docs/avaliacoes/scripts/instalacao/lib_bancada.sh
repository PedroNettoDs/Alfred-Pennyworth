# Funções comuns das medições de instalação (use com `source`). Exige AQUI (pasta destes scripts) e OUT.
# Tudo roda dentro de um container descartável; nada no host além do próprio container e do volume dele.
IMG_BANCADA=alfred-aval-bancada:24.04
CT=${CT:-alfred-aval-zero}
VOL=${VOL:-$CT-docker}
U=teste
UID_T=1001

bancada_subir() {   # constrói a imagem da bancada e inicia um container limpo (systemd como PID 1)
    docker build -q -t "$IMG_BANCADA" -f "$AQUI/testbed.Dockerfile" "$AQUI" >/dev/null || return 1
    bancada_remover
    docker run -d --name "$CT" --hostname bancada --privileged --cgroupns=private \
        --tmpfs /run --tmpfs /run/lock -v "$VOL:/var/lib/docker" "$IMG_BANCADA" >/dev/null || return 1
    docker exec "$CT" systemctl is-system-running --wait >/dev/null 2>&1   # 'degraded' também serve
    docker exec "$CT" timeout 60 bash -c "until [ -S /run/user/$UID_T/bus ]; do sleep 0.5; done"
}

bancada_remover() {
    docker rm -f "$CT" >/dev/null 2>&1
    docker volume rm "$VOL" >/dev/null 2>&1
    return 0
}

repo_para_bancada() {   # repo_para_bancada <repositório git no host>: bundle do HEAD (e do ramo) em /tmp/alfred.bundle
    local ramo; ramo=$(git -C "$1" branch --show-current)
    git -C "$1" bundle create "$OUT/alfred.bundle" HEAD ${ramo:+"$ramo"} >/dev/null 2>&1 || return 1
    docker cp "$OUT/alfred.bundle" "$CT:/tmp/alfred.bundle" && docker exec "$CT" chmod 644 /tmp/alfred.bundle
}

como_usuario() {   # sessão de login nova do usuário (grupos relidos: equivale a sair e entrar de novo)
    docker exec -u $U -w /home/$U -e HOME=/home/$U -e USER=$U -e XDG_RUNTIME_DIR=/run/user/$UID_T \
        -e DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UID_T/bus "$CT" bash -lc "$1"
}

passo() {   # passo <nome> <comando>: cronometra, guarda o log em $OUT/<nome>.log e a linha em tempos.tsv
    local nome="$1" t0 t1 rc
    t0=$(date +%s.%N)
    como_usuario "$2" >"$OUT/$nome.log" 2>&1; rc=$?
    t1=$(date +%s.%N)
    printf '%s\t%s\t%s\n' "$nome" "$(LC_ALL=C awk "BEGIN{printf \"%.1f\", $t1-$t0}")" "$rc" | tee -a "$OUT/tempos.tsv"
    return $rc
}

# Espera os serviços responderem (até $2 s) e grava em $OUT/<nome>.log quando cada um ficou de pé.
# Portas lidas do .env da bancada (sem a chave: padrão do código atual); Ollama fica de fora (GPU
# fora da bancada). SERVICOS limita a lista (ex.: versão antiga sem Laya).
esperar_saude() {
    local nome="$1" limite="$2" lista=${SERVICOS:-'${!url[@]}'}
    passo "$nome" "cd ~/Alfred-Pennyworth; p() { v=\$(grep -E \"^\$1=\" .env | tail -1 | cut -d= -f2-); echo \${v:-\$2}; }
        declare -A url=(
          [executor]=http://127.0.0.1:\$(p PORT_SHELL_EXECUTOR 7071)/health
          [researcher]=http://127.0.0.1:\$(p PORT_RESEARCHER 7072)/health
          [inbox]=http://127.0.0.1:\$(p PORT_INBOX 7073)/health
          [laya]=http://127.0.0.1:\$(p PORT_LAYA 7074)/health
          [searxng]=http://127.0.0.1:\$(p PORT_SEARXNG 8889)/healthz
          [chromadb]=http://127.0.0.1:\$(p PORT_CHROMADB 8001)/api/v2/heartbeat
          [evolution]=http://127.0.0.1:\$(p PORT_EVOLUTION 8081)/ )
        t0=\$(date +%s); falta=($lista)
        while ((\${#falta[@]})) && (( \$(date +%s) - t0 < $limite )); do
          resto=()
          for s in \${falta[@]}; do
            if curl -sf -o /dev/null --max-time 2 \${url[\$s]}; then echo \"OK \$s \$(( \$(date +%s) - t0 ))s \${url[\$s]}\"
            else resto+=(\$s); fi
          done
          falta=(\${resto[@]}); sleep 2
        done
        for s in \${falta[@]}; do echo \"FALHOU \$s \${url[\$s]}\"; done
        ((\${#falta[@]} == 0))"
}

# Medidas de disco e versões resolvidas (sem conteúdo pessoal: só nomes de pacote e tamanhos).
inventario() {
    local nome="$1"
    passo "$nome" 'cd ~/Alfred-Pennyworth
        for v in services/*/venv; do s=$(basename $(dirname $v))
          echo "venv $s $(du -sm $v | cut -f1) MB $($v/bin/python -m pip list --format=freeze --disable-pip-version-check 2>/dev/null | wc -l) pacotes"
          $v/bin/python -m pip list --format=freeze --disable-pip-version-check 2>/dev/null | sed "s/^/pip $s /"
        done
        echo "hf_cache $(du -sm ~/.cache/huggingface 2>/dev/null | cut -f1) MB"
        echo "pip_cache $(du -sm ~/.cache/pip 2>/dev/null | cut -f1) MB"
        docker images --digests --format "img {{.Repository}}:{{.Tag}} {{.Digest}} {{.Size}}"
        sudo du -sm /var/lib/docker | sed "s/^/docker_dir_mb /"
        systemctl --user list-units "alfred-*" --all --no-legend --plain | sed "s/^/unit /"'
}
