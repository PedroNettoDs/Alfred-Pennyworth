# Imagem única hipotética — 05/11/2026 ~02:00 (load 4,7–5,8; swap 8,2/8 GB cheio; 1 build por vez)

Variante **enxuta** medida (`VARIANTES=enxuta`, padrão): um venv só, torch da página CPU do PyTorch.
A **fiel** (5 venvs, torch CUDA do PyPI) não foi refeita por falta de memória; estimada abaixo.

| medida | enxuta (medida) |
|---|---|
| base `python:3.14-slim` (3.14.10, `sha256:930e1abb…`) | 120 MB, pull 7,7 s |
| build `--no-cache` (apt + pip + código) | 166,1 s |
| imagem (`docker image inspect .Size`) | 2.013.118.761 B = 2,01 GB, 14 camadas |
| `docker save | gzip -1` (≈ transferência) | 707.957.163 B = 0,71 GB |
| camada apt (ffmpeg, curl, tini) | 452 MB |
| camada do venv (`/opt/venv`) | 1,44 GB (du 1475 MB) |
| torch | 2.15.0+cpu, 817 MB (laya 0.4.19) |
| código | 3,7 MB |
| partida a frio: executor / researcher / inbox / laya | 2 s / 2 s / 3 s / 75 s (o Laya baixa os pesos, 648 MB) |
| memória dos 4 serviços de pé | 2,0 GiB |

Estimativa da fiel (mesmo Python, 5 venvs como o instalar.sh), a partir dos venvs medidos na bancada
do zero (Python 3.12) e na produção (Python 3.14):
- torch CUDA no venv do Laya: torch 1,2 GB + nvidia 3,0 GB + triton 1,0 GB = 5,2 GB, contra 0,82 GB
  do torch CPU → **+4,4 GB**;
- 5 venvs separados duplicam fastapi/httpx/numpy/scikit-learn etc.: soma dos 4 venvs sem o Laya =
  750 MB (bancada) contra uma cópia só no venv único → ~+0,6 GB;
- fiel ≈ 120 MB + 452 MB + 6,4 GB (venvs da bancada) + 4 MB ≈ **7,0 GB** (≈ 2,0 + 4,4 + 0,6);
- tempo: o pip dos 5 venvs levou 443 s na bancada do zero (Laya 369 s) contra 166 s do build enxuto inteiro.
