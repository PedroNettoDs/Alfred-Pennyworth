# Do zero — saídas resumidas (03/11/2026)

Logs completos ficaram no scratchpad da sessão e se perderam no reboot de 04/11; abaixo, os números
copiados deles na hora. Ruído: load average 8,6–10,3 em 12 threads (7 avaliações em paralelo).

## 02-verificar (Ubuntu limpo)
- ✖ 6: python3+venv, curl, docker, docker compose, ollama, ffmpeg. ! 4: kdialog/zenity, python3-gi,
  thunderbird, ufw inativo.
- Pacotes apt que o script manda instalar: `python3 python3-venv curl docker.io docker-compose-v2 ffmpeg`.
- Depois do apt: falta "sem acesso ao Docker … usermod -aG docker" → passo 05 + nova sessão de login.
- Depois do grupo: só "ollama" pendente (curl|sh, fora da bancada).

## 07-instalar (660,7 s) — fases pelos mtimes
| fase | início → fim (UTC) | s |
|---|---|---|
| pré-requisitos, .env, pastas, SearXNG | 07:55:12 → 07:55:13 | ~1 |
| venv shell-executor | 07:55:13 → 07:55:22 | 9 |
| venv researcher | 07:55:23 → 07:55:31 | 8 |
| venv inbox | 07:55:32 → 07:56:02 | 30 |
| venv briefing | 07:56:03 → 07:56:26 | 23 |
| venv laya (torch CUDA do PyPI) | 07:56:27 → 08:02:36 | 369 |
| units (18 gravadas, 4 serviços + 7 timers ligados) | 08:02:36 → 08:02:38 | 2 |
| docker compose up | 08:02:38 → 08:06:13 | 215 |

- compose: searxng, postgres, chroma e evolution baixadas até 08:05:50; Open WebUI falhou com
  `failed to convert whiteout file "var/lib/apt/lists/.wh.auxfiles": operation not permitted`
  (Docker aninhado com snapshotter do containerd — artefato da bancada). O instalar.sh mostrou só
  `docker compose up falhou: ` (vazio): o grep `error|erro` não casa com `failed to extract layer`.
- Com `containerd-snapshotter: false` (overlay2): `docker pull` do Open WebUI = 193 s.

## 08-saude (logo após o 07)
executor, researcher, inbox, laya: OK em ≤ 1 s (já estavam de pé havia 215 s). Containers: falharam (ver acima).

## 09-inventario
| item | MB | pacotes |
|---|---|---|
| venv briefing | 303 | 28 |
| venv inbox | 328 | 41 |
| venv laya | 5647 | 64 |
| venv researcher | 61 | 26 |
| venv shell-executor | 58 | 21 |
| cache Hugging Face (pesos do Laya) | 648 | – |
| cache do pip deixado em ~/.cache/pip | 2970 | – |
| imagem open-webui:main (disco) | 4580 | – |
| imagens chroma / evolution / postgres / searxng (disco) | 826 / 1830 / 420 / 387 | – |

Produção (Arch, python 3.14.4), para comparar: briefing 302 MB, inbox 328, laya 5600 (torch 1,2 GB +
nvidia 3,0 GB + triton 1,0 GB), researcher 60, shell-executor 56.

## 10–13
- 10-reinstalar (160,8 s): rebaixou as 4 imagens (o store mudou para overlay2) e subiu os 5 containers.
- 11-saude: os 7 de pé em ≤ 12 s. 12-reboot da bancada: os 7 voltaram sozinhos em ≤ 13 s.
- 13-reinstalar-noop: 6,7 s, nada alterado (só "ollama" pendente).

## 14-testes-inbox (bancada, venv recém-criado)
- 464 testes, 3 falhas, 1 pulado. 2 falhas (`test_painel`: horários 3 h adiantados) somem com
  `TZ=America/Sao_Paulo` (painel 45/45 OK): a bancada estava em UTC. 1 falha de tempo
  (`test_trava`: "5 not greater than 5"), repetida ao rodar o teste sozinho com load ~10: é
  sensível a CPU disputada, não à versão dos pacotes.

## Deriva de versões: produção × instalação de hoje
180 pares serviço/pacote em comum; 25 diferentes (20 sem contar o pip do venv, 13 pacotes).
Maiores: `python-dateutil` 2.9.0.post0 → **4.0.0** (major, via icalendar), `fastapi` 0.147 → 0.162,
`starlette` 1.9 → 1.10, `anyio` 4.15 → 4.16 (5 venvs). pip do venv: 25.1.1 (Arch) × 24.0 (Ubuntu).
