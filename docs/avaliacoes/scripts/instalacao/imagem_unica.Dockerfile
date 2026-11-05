# Imagem única HIPOTÉTICA do Alfred, só para medir tamanho e tempo de build (avaliação "Instalação").
# Não é usada em produção. Contexto do build: um `git archive` limpo do repositório (sem venv nem .env).
#   --build-arg VARIANTE=fiel    → um venv por serviço, como o instalar.sh (torch do PyPI, com CUDA)
#   --build-arg VARIANTE=enxuta  → um venv só e torch da página CPU do PyTorch (o Laya roda na CPU)
# Ficam de fora, como hoje: Ollama (GPU, no host), SearXNG/ChromaDB/Evolution (containers próprios),
# Thunderbird, notificações D-Bus e os cofres (montados como volume).
ARG PYTHON=3.14-slim
FROM python:${PYTHON}
ARG VARIANTE=fiel
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl tini \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/alfred
COPY services/briefing/requirements.txt services/briefing/
COPY services/inbox/requirements.txt services/inbox/
COPY services/laya/requirements.txt services/laya/
COPY services/researcher/requirements.txt services/researcher/
COPY services/shell-executor/requirements.txt services/shell-executor/
RUN set -e; S="briefing inbox laya researcher shell-executor"; \
    if [ "$VARIANTE" = enxuta ]; then \
        python -m venv /opt/venv; \
        /opt/venv/bin/pip install -q --index-url https://download.pytorch.org/whl/cpu torch; \
        /opt/venv/bin/pip install -q $(for s in $S; do echo -r services/$s/requirements.txt; done); \
        for s in $S; do ln -s /opt/venv services/$s/venv; done; \
    else \
        for s in $S; do python -m venv services/$s/venv \
            && services/$s/venv/bin/pip install -q -r services/$s/requirements.txt; done; \
    fi
COPY . .
COPY docs/avaliacoes/scripts/instalacao/imagem_unica_entrypoint.sh /usr/local/bin/alfred-entrypoint
ENTRYPOINT ["tini", "--", "alfred-entrypoint"]
