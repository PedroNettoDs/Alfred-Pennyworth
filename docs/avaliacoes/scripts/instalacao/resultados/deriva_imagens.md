# Deriva das imagens do docker-compose.yml do Alfred (digest de 12 caracteres)

Coletado com `docker image inspect` (host, só leitura) e `docker buildx imagetools inspect` (registro,
só manifesto, nada baixado).

| referência no compose | host (rodando) | registro 03/11 | registro 05/11 | mudou? |
|---|---|---|---|---|
| `searxng/searxng:latest` | f4aa660c54d0 (criada 30/10) | 892ed5710603 | 892ed5710603 | sim: o host roda outro build do que uma instalação nova baixa |
| `chromadb/chroma:latest` | 1e0b73a187a2 (05/05) | 1e0b73a187a2 | 1e0b73a187a2 | não (é a 1.5.9 que o Odysseus fixa por digest) |
| `postgres:16-alpine` | 721873c34ceb (17/09) | 0cefaabd882e | 0cefaabd882e | sim (tag "fixa" de major também anda) |
| `ghcr.io/open-webui/open-webui:main` | (não roda) | 0b399a8ba345 | 975e3965dcf5 | sim, em 2 dias |
| `n8nio/n8n:latest` | (não roda) | – | 8602f5dfacbe | – |
| `evoapicloud/evolution-api@sha256:966625…` | 966625532d90 | – | – | não pode mudar (digest) |

Imagem do Odysseus `ghcr.io/odysseus-dev/odysseus:latest` (03/11): manifest list com amd64 + arm64
(+ 2 atestados); amd64 = 25 camadas, 0,97 GB comprimido.
