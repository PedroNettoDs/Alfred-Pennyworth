# Bancada descartável da avaliação "Instalação" (docs/avaliacoes/instalacao.md).
# Ubuntu 24.04 com systemd como PID 1 (units de usuário funcionam, com linger) e um usuário
# comum com sudo. Aproxima um Ubuntu desktop recém-instalado: tem python3, openssl, wget, ss e
# notify-send; NÃO tem git, curl, python3-venv, docker, ffmpeg nem ollama.
# Roda com --privileged só porque o Docker do Alfred (SearXNG, ChromaDB, Evolution) sobe DENTRO
# dela — nenhuma porta é publicada no host e nada do Pedro é montado.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive container=docker
RUN apt-get update && apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus dbus-user-session sudo ca-certificates \
      python3 openssl wget libnotify-bin iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash -G sudo teste \
    && echo 'teste ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/teste \
    && mkdir -p /var/lib/systemd/linger && touch /var/lib/systemd/linger/teste
# Docker aninhado: o snapshotter do containerd (padrão do docker.io 29) não converte os whiteouts da
# imagem do Open WebUI ("failed to convert whiteout file ... operation not permitted"); o overlay2
# clássico converte. Artefato da bancada, não do Alfred.
RUN mkdir -p /etc/docker && echo '{"features":{"containerd-snapshotter":false}}' > /etc/docker/daemon.json
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
