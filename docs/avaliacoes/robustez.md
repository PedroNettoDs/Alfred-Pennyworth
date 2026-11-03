# Avaliação — Robustez (gravação atômica, logs sem segredos, backup/restore)

Medido em 03/11/2026 na branch `claude/avaliacao-robustez-896abc` (base `laya` @ 4c30b84), contra o Odysseus
@ c21980f (`git clone --depth 1`, só leitura). Nenhum trecho do Odysseus foi copiado. A única execução de código
dele foi `redact_url` sobre 7 URLs sintéticas, com `env -i python3 -I`, para validar a reimplementação usada na
medição.

## 1. O que o Odysseus faz

- **`core/atomic_io.py`**
  - `atomic_write_json` e `atomic_write_text`: gravam num irmão `<arquivo>.tmp.<uuid>`, fazem `fsync` e trocam
    com `os.replace`. Um `finally` apaga o temporário.
  - Não faz `fsync` do diretório e não preserva as permissões do arquivo antigo.
  - `store_transaction`: decorador que serializa "ler → mudar → gravar" com um `RLock` por arquivo mais
    `flock` num `<arquivo>.lock`.
  - Uso: ~90 chamadas em 30 arquivos (sem contar testes), em `auth.json`, `sessions.json`, `settings.json` e
    outros.
- **`core/log_safety.py`**
  - Só tem `redact_url(url)`: remonta a URL com esquema, host, porta e caminho, sem `usuário:senha@`, query e
    fragmento.
  - É chamado em 9 pontos de 3 arquivos de rotas (`routes/model_routes.py`, `routes/chat_routes.py` e
    `routes/contacts/contacts_routes.py`) antes de logar endpoints configurados pelo admin.
  - Não mascara telefone, e-mail nem token solto no texto.
  - Ao lado, `src/webhook_manager.py:sanitize_error` troca IPv4/IPv6 e hosts de URL por `[redacted]` e corta a
    mensagem em 200 caracteres. Vale só para erros de webhook.
- **`scripts/odysseus-backup` e `website/backup-restore.md`**
  - `snapshot`: tar.gz de `data/` inteiro, incluindo a chave Fernet `data/.app_key`, o cofre, a memória, o
    índice RAG e a sessão do WhatsApp. Os bancos `*.db` são copiados pela API `.backup` do SQLite.
  - Pula `deep_research/` e `mail-attachments/`, a menos que se passe `--include-research` ou
    `--include-attachments`.
  - Recusa um destino dentro de `data/`.
  - `list`: mostra os backups, do mais novo ao mais velho.
  - `verify`: percorre o tar e valida caminhos sem extrair.
  - `restore --yes`: valida cada membro (recusa caminho absoluto, `..`, link e arquivo especial), **renomeia
    `data/` para `data.before-restore-<data>`** e extrai.
  - Toda saída é JSON.
  - O guia ensina cron + `scp`/`rclone` para levar o backup para fora da máquina. Também avisa que, no Docker,
    o ChromaDB montado em `/chroma/chroma` fica vazio, porque os dados vão para `/data` na camada do container.
  - `routes/backup_routes.py` (`/api/export` e `/api/import`) exporta e importa memória, presets e skills de
    cada usuário em JSON.

## 2. O que o Alfred faz hoje

- **`services/_shared/lib_alfred.py:gravar_atomico`** (`core.gravar_atomico` é o mesmo objeto)
  - Temporário oculto e único `.<nome>.<hex>.tmp`, aberto com `O_EXCL`.
  - `fsync` do arquivo, `chmod` para as permissões antigas, `os.replace` e `fsync` do diretório.
  - Aceita `str` e `bytes`.
  - "Ler → mudar → gravar" usa `core.trava_arquivo` (flock).
  - 14 chamadas, entre elas pendências, agenda, `lotes.yml`, `inbox_categorias.json`, os `seen`,
    `vault_write` e o restore. Há ainda 4 implementações próprias: `researcher/main.py:_gravar_json_atomico`,
    `lib_memoria._gravar_atomico` e as tools `alfred_pendencias` e `alfred_tasks`.
- **`lib_alfred.sanitizar`**, aplicado a toda mensagem de `log()`, mascara:
  - `Bearer <token>`;
  - `*_TOKEN`, `*_KEY`, `*_SECRET`, `*_PASSWORD`, `*_SENHA` e `apikey` seguidos de `=` ou `:`;
  - telefone `+55…` e `55` + 10 ou 11 dígitos (inclusive no JID);
  - e-mail (fica a 1ª letra e o domínio).
- **`services/scripts/backup.py`** (`criar` e `restaurar`, com o `alfred-backup.timer` semanal)
  - Conteúdo: `logs/`, `perfis.yml` e o `VAULT_ALFRED` inteiro. SQLite é detectado pelo cabeçalho e copiado
    pela API de backup online. Leva um `MANIFESTO.json`.
  - Não entram `.env`, `data/`, symlinks, `*.tmp`, `*.lock`, `-wal` e `-shm`.
  - O arquivo é criado com modo `0600`, gravado como `.parcial` e ganha o nome final por `os.replace`.
    Guarda os 8 mais novos.
  - O restore pede confirmação ou `--dry-run` e não apaga nada a mais.
  - Cada arquivo é restaurado por `gravar_atomico`, e o `-wal`/`-shm` velho é removido ao lado de cada banco.
- Testes: `services/inbox/tests/test_robustez.py` passa com 19 testes OK em 0,05 s.

## 3. Método

Nenhuma medição usa Ollama nem a GPU, então o `flock /tmp/alfred-bench-gpu.lock` não foi necessário.

**Ruído:** a carga média de 1 minuto ficou entre 12,3 e 18,9 durante as medições: 8 avaliações em paralelo e
mais a produção, com 4 serviços e os timers. Isso pesa nas latências p95 e no tempo do backup. O
`/tmp` é tmpfs, onde `fsync` não custa nada. Por isso a injeção e o backup rodaram em pasta temporária em
`~/.cache`, no mesmo ext4 do cofre, apagada no fim.

| Medição | Como | Script |
|---|---|---|
| Cobertura | grep de toda gravação de arquivo em `services/` e `tools/`, sem venv nem testes; cada ponto lido e classificado à mão | `scripts/robustez_cobertura.sh` |
| Injeção de falha | 200 tentativas por modo e por tamanho (98 kB, como `whatsapp_seen.json`, e 9,8 kB, como `inbox_categorias.json`). O arquivo começa íntegro (versão A); um filho o regrava em laço (B, A, B…); depois de 0,2–20 ms aleatórios o pai manda SIGKILL (= `kill -9`) e confere se o arquivo é exatamente A ou B. Mede também a latência de 200 gravações sem falha | `scripts/robustez_kill9.py` |
| Logs | Cópia do journal das 11 units `alfred-*` (19/10 a 03/11, 93.998 linhas) e dos 8 arquivos de `logs/`. Detectores próprios, mais largos que o `sanitizar`, em 3 estágios: bruto, depois de `sanitizar`, e `sanitizar` + corte de usuário/query/fragmento de URL (a ideia do `redact_url`, reimplementada com `urllib` e conferida contra o original em 7 de 7 URLs). Também 20 linhas sintéticas, uma por tipo de dado. O corte do journal é 31/10 15:08, quando os serviços reiniciaram com o `sanitizar`. As cópias foram apagadas no fim | `scripts/robustez_logs.py` |
| Backup | `criar_backup()` do estado real para uma pasta temporária, leitura de todo o tar, `restaurar()` numa raiz e num cofre falsos, conferência de sha256, mtime, `PRAGMA integrity_check` e contagem de linhas de cada tabela contra a origem, e um 2º restore por cima. A pasta foi apagada no fim. Não chama `main()`, então o `.env` não é lido | `scripts/robustez_backup.py` |

Os números brutos (só contagens) estão em `docs/avaliacoes/scripts/robustez_resultados.json`.

Para reproduzir, rode da raiz do repositório, com o venv do inbox:

```bash
bash docs/avaliacoes/scripts/robustez_cobertura.sh
```

```bash
services/inbox/venv/bin/python docs/avaliacoes/scripts/robustez_kill9.py --n 200 --bytes 90000
```

```bash
services/inbox/venv/bin/python docs/avaliacoes/scripts/robustez_kill9.py --n 200 --bytes 9000
```

```bash
D=$(mktemp -d -p ~/.cache) && chmod 700 "$D" && for u in backup briefing-matinal briefing-vespertino executor inbox-api inbox laya rag researcher semanal vigia; do echo "-u alfred-$u.service"; done | xargs journalctl --user --since 2026-10-01 -o short-iso --no-pager > "$D/journal.txt" && cp logs/*.json logs/*.jsonl "$D/" && services/inbox/venv/bin/python docs/avaliacoes/scripts/robustez_logs.py --corte 2026-10-31T15:08 "$D"/*; rm -rf "$D"
```

```bash
services/inbox/venv/bin/python docs/avaliacoes/scripts/robustez_logs.py --sintetico
```

```bash
services/inbox/venv/bin/python docs/avaliacoes/scripts/robustez_backup.py --raiz "$PWD" --vault "<VAULT_ALFRED>"
```

## 4. Resultados

### 4.1 Cobertura: os 35 pontos que gravam arquivo

O grep acha 35 pontos que gravam arquivo direto (`write_text`, `open`, `fdopen` e outros), além das 14
chamadas de `gravar_atomico`. Abaixo, a classificação dos 35.

**Estado YAML/JSON**

| Ponto | Arquivo | Como grava | Risco |
|---|---|---|---|
| `services/inbox/vigia.py:45` `_salvar_estado` | `logs/vigia_estado.json` | escrita direta | **baixo**: o leitor devolve `{}` e o pior caso é um aviso repetido |
| `services/inbox/whatsapp.py:794` `_save_fila` | `logs/whatsapp_fila.json` | `.tmp` fixo + `replace`, sob trava, sem fsync | **baixo**: atômico contra `kill -9` (0/200 na injeção); só a falta de energia ameaça, e a perda são mensagens na fila sem resumo nem aviso |
| `services/_shared/lib_whatsapp.py:200` `_marcar_envio` | `logs/whatsapp_ids_enviados.json` | `.tmp` fixo + `replace`, **sem trava**, sem fsync | **baixo**: duas threads no mesmo `.tmp` podem expor arquivo parcial; a perda faz o eco do Alfred ser lido como lembrete de Pedro |
| `services/_shared/lib_whatsapp.py:165` | `logs/whatsapp_enviados.jsonl` | append (só modo simulado) | baixo: última linha cortada |
| `services/_shared/lib_memoria.py:426` | espelho `.md` da memória | `mkstemp` + `replace`, sem fsync | baixo: derivado do SQLite |
| `tools/alfred_memory.py:82` `_save` | memória YAML da tool do Open WebUI | escrita direta | dormente (o container `open-webui` não roda); se voltar, **médio**: `MemoriaIlegivel` trava a ferramenta |
| `tools/alfred_pendencias.py:113`, `tools/alfred_tasks.py:137` | pendências, `tasks.yml` | `.tmp` fixo + fsync + `replace`, sob flock | ok (dormentes) |
| `services/researcher/main.py:321-322` | jobs `*.json` | `mkstemp` + fsync + `replace` | ok |
| `services/laya/treino/rotular_emails.py:63,78,79` | `treino/dados/*.jsonl` | append e reescrita direta | baixo: offline e manual |

**`.md` do cofre: reescrita de nota existente** (truncar perde a nota inteira, inclusive o frontmatter)

| Ponto | O que reescreve | Risco |
|---|---|---|
| `services/inbox/core.py:1274` | pendência de evento: status + resposta | **médio** |
| `services/inbox/whatsapp.py:751` | pendência WhatsApp: "Mensagens posteriores" | **médio** |
| `services/inbox/whatsapp.py:1051` | pendência WhatsApp: resposta | **médio** |
| `services/inbox/whatsapp.py:1335` | pendência WhatsApp: bloco "Agenda" | **médio** |
| `services/inbox/limpeza.py:357` | `.md` do lote (refeito a partir do `lotes.yml`, que é atômico) | baixo |
| `tools/alfred_pendencias.py:298` | resposta via chat (dormente) | baixo |

**`.md` do cofre: nota nova.** São 8 pontos de risco baixo: a falha corta só a nota nova e nada antigo se perde.

- `services/inbox/core.py:638`: jornal. O `.json` ao lado é atômico.
- `services/inbox/core.py:1060`: pendência de evento.
- `services/inbox/whatsapp.py:657`: pendência do WhatsApp.
- `services/briefing/tech-briefing.py:511` e `:665`.
- `services/inbox/semanal.py:522`.
- `services/_shared/lib_ferramentas.py:1248` e `tools/alfred_vault_writer.py:78`, ambos com `open(..., "x")`.

**Fora de escopo.** São 6 pontos de mídia ou temporário regenerável: `semanal.py:308` e `:363`,
`lib_tts.py:27`, `lib_audio.py:158` e `:165` (dentro de `TemporaryDirectory`) e o QR em
`inbox-assistant.py:204`. Mais 2 pontos são o próprio mecanismo atômico: `lib_alfred.py:139` e
`backup.py:110`.

**Na produção hoje:** 0 temporários órfãos e 0 cópias `*.corrompido-*` no cofre e em `logs/`. Nenhum incidente
foi registrado até agora.

### 4.2 Injeção de falha com `kill -9` (N=200 por modo)

| Modo | Tamanho | Íntegro | Vazio | Truncado | **Corrompido** | Temporário órfão | Latência mediana / p95 |
|---|---|---|---|---|---|---|---|
| escrita direta (`write_text`) | 98 kB | 58 | 136 | 6 | **142 (71 %)** | 0 | 0,24 / 0,42 ms |
| `.tmp` fixo + `replace`, sem fsync | 98 kB | 200 | 0 | 0 | **0** | 1 | 0,21 / 0,68 ms |
| `gravar_atomico` | 98 kB | 200 | 0 | 0 | **0** | 125 (62 %) | 3,59 / 4,99 ms |
| escrita direta | 9,8 kB | 48 | 144 | 8 | **152 (76 %)** | 0 | 0,26 / 1,84 ms |
| `.tmp` fixo + `replace`, sem fsync | 9,8 kB | 200 | 0 | 0 | **0** | 0 | 0,27 / 1,10 ms |
| `gravar_atomico` | 9,8 kB | 200 | 0 | 0 | **0** | 112 (56 %) | 6,88 / 17,21 ms |

- Quando o `kill -9` cai durante uma escrita direta, 3 em 4 arquivos ficam vazios ou cortados.
- Com troca atômica, nenhum arquivo foi corrompido em 400 tentativas.
- `gravar_atomico` deixa um `.<nome>.<hex>.tmp` órfão em ~60 % dos kills. O `except` não roda depois de um
  SIGKILL, e o `atomic_io` do Odysseus tem o mesmo comportamento. Os órfãos são ocultos para o Obsidian e o
  backup os ignora.
- **Na prática:** a janela vulnerável é o tempo de uma escrita, ~0,25 ms. Com dezenas de gravações por dia, a
  chance de um reinício ou OOM cair dentro dela é da ordem de 10⁻⁶ por evento.
- O `kill -9` não simula falta de energia. Contra ela, o que conta é o `fsync`, que falta nos três pontos de
  `.tmp` fixo. No ext4, o `auto_da_alloc` atenua os padrões `replace` e truncar-e-gravar.

### 4.3 Logs: o que ainda vazaria

**Journal das units `alfred-*`, contagem de ocorrências**

| Tipo | Antes de 31/10 15:08 (16.857 linhas): bruto → `sanitizar` → +corte de URL | Depois (77.130 linhas): bruto → `sanitizar` → +corte de URL |
|---|---|---|
| Telefone (+55, 55 cru, sem 55) | 0 → 0 → 0 | 0 → 0 → 0 (2 já mascarados) |
| E-mail | 7 → **0** → 0 | 0 → 0 → 0 |
| Token, chave ou senha (Bearer, `NOME=valor`, JWT, `usuário:senha@`) | 0 → 0 → 0 | 0 → 0 → 0 (os 454 `KEY = …` brutos são linhas de código em tracebacks, falso positivo) |
| CPF | 1 → 1 → 1 | 0 → 0 → 0 |
| WhatsApp `@lid` (pseudônimo do contato) | 8 → 8 → 8 | 0 → 0 → 0 |
| Convite de grupo `chat.whatsapp.com/<código>` | 0 → 0 → 0 | 4 → 4 → 4 |
| URL com query (parâmetros de rastreio `igsh`, `si`, `fbclid`, de links que chegaram por mensagem) | 68 → 68 → **0** | 65 → 65 → **0** |
| IPv4 | 4.120 | 22.932, todos privados, loopback ou `0.0.0.0` (0 públicos) |
| Origem das linhas com vazamento | 82 de `log()` | 65 de `log()`; 0 do uvicorn, de print ou de traceback |

**Arquivos de `logs/`** (são estado, não log, mas entram no backup)

| Arquivo | Bruto | Depois de `sanitizar` |
|---|---|---|
| `inbox_categorias.json`, `inbox_seen.json` | 86 e-mails | 0 |
| `whatsapp_enviados.jsonl` | 8 telefones | 0 |
| `whatsapp_seen.json` | 2 candidatos a telefone sem 55 (provável id) | 2 |
| `briefing_seen.json` | 5 URLs com query | 5 (0 com corte de URL) |
| os outros 4 | 0 | 0 |

**Casos sintéticos** (20 tipos; o trecho sensível some ou não)

- **`sanitizar`: 9/20.** Cobre `+55`, JID, e-mail, Bearer, `NOME_KEY=`, `password=`, `x-api-key:`,
  `?token=` e `usuário:senha@host.tld`. Este último é coberto por acaso, pela máscara de e-mail, e o usuário
  continua visível.
- **`sanitizar` + corte de URL: 13/20.** O que a ideia do `log_safety` cobre a mais:
  - `usuário:senha@IP`;
  - `?code=` (OAuth);
  - `?sig=` (URL assinada);
  - query de rastreio.
- **Nenhum dos dois cobre 7 tipos:**
  - telefone sem 55 (formatado e cru);
  - `@lid`;
  - CPF;
  - `Basic` auth;
  - JWT solto;
  - código de convite de grupo, que fica no caminho da URL.
- **Custo por linha:** `sanitizar` leva 12,5 µs; o corte de URL soma 3,2 µs (+26 %), medido nas 93.998 linhas.
- O grep acha ~9 chamadas `log(f"…{conteudo|texto|assunto…}")` que põem trechos de mensagem no journal. Esse
  conteúdo não é coberto por nenhum dos dois sanitizadores. É de lá que vêm os 65 links.

### 4.4 Backup e restore

| Medida | Valor |
|---|---|
| `criar` (com carga 18,9) | 4,20 s de relógio, 3,33 s de CPU, pico de 71 MB de RSS. Na produção, em 01/11: 2,58 s, 2,40 s de CPU, 136 arquivos, 58,1 MB |
| Tamanho | 65,2 MB comprimido, 80,9 MB descomprimido (razão 0,81: muito áudio já comprimido); modo `0600` |
| Conteúdo | 165 membros (12 de `repo/` e 153 de `vault/`), 2 bancos SQLite |
| Ler o tar inteiro (equivalente ao `verify`) | 0,88 s |
| `restaurar` numa pasta vazia | 1,95 s, 1,10 s de CPU; 165 arquivos novos |
| Conferência | 165/165 sha256 iguais ao tar; 165/165 mtime iguais; 0 faltando; 2/2 `integrity_check` ok; 24/24 tabelas com a mesma contagem da origem |
| 2º restore por cima | 1,21 s; 165 sobrescritos, sem cópia do que foi sobrescrito |

**Estado fora do backup, comparado ao Odysseus**

| Estado | Tamanho | O Odysseus inclui? | Custo de perder no Alfred |
|---|---|---|---|
| ChromaDB: dados em `/data`, na camada gravável do container; o bind `./data/chromadb → /chroma/chroma` está **vazio** | 18 MB, 1.595 pedaços | nativo sim; no Docker não, e o guia documenta **este mesmo erro de caminho** | recriar o container apaga o índice. O `lib_rag` percebe pelo id da coleção e reindexa sozinho no próximo `alfred-rag`, ~1.595 × 0,7 s ≈ 19 min de CPU; até lá só o FTS5 responde |
| Laya, `treino/dados`: rótulos `*.jsonl` | 2,2 MB, 533 e-mails rotulados pelo LLM | não se aplica | rodar `rotular_emails.py` de novo no LLM (GPU) |
| Laya, pesos `*.safetensors` | 1.288 MB | não se aplica | retreino de ~20 min na RTX 3060 |
| `data/evolution/instances` (sessão do WhatsApp) | 32 MB | sim (sessão do WhatsApp em `data/`) | parear de novo por QR |
| `data/evolution/postgres` | ilegível sem root | — | histórico da Evolution |
| `.env` | — | sim (chave Fernet) | recriar tokens. A exclusão é proposital e melhor que o Odysseus |
| Cópia fora do disco | — | guia com cron + `scp`/`rclone` | o backup fica no mesmo disco do original |

**O Odysseus tem a mais:**

- `list` e `verify`;
- `data.before-restore-*`: o estado atual guardado antes do restore;
- `--include-*`;
- saída JSON;
- export/import JSON de memória, presets e skills por usuário.

**O Alfred tem a mais:**

- retenção (8);
- modo `0600`;
- `.parcial` + `replace`;
- SQLite detectado pelo cabeçalho;
- restore por troca atômica com limpeza de `-wal`/`-shm`;
- `--dry-run`;
- confirmação;
- manifesto;
- timer systemd;
- `.env` fora do backup.

**`atomic_io` contra `gravar_atomico`:**

- o Alfred faz fsync do diretório, preserva as permissões, usa temporário oculto e aceita `bytes`;
- o Odysseus não faz nenhuma dessas coisas;
- `store_transaction` equivale ao `core.trava_arquivo` que o Alfred já tem.

## 5. Pontuação

| Critério | Valor |
|---|---|
| **Ganho para Pedro** | **1/5**. O núcleo (atômico, sanitizar, backup) já foi portado e mede bem: 0/400 corrupções e restore 165/165. O que sobra do Odysseus é marginal: 65 linhas com query de rastreio em ~2,6 dias, 0 segredos, e um backup que já restaura tudo |
| **Custo de implementar** | **~4 h** para o pacote "adaptar" do item 6, em 7 arquivos: `services/inbox/core.py`, `services/inbox/whatsapp.py`, `services/inbox/vigia.py`, `services/_shared/lib_whatsapp.py`, `services/_shared/lib_alfred.py`, `services/scripts/backup.py` e `services/inbox/tests/test_robustez.py`. O ajuste do ChromaDB em `docker-compose.yml` soma +0,5 h, com migração |
| **Risco** | **1/5**. São trocas locais cobertas por teste. O corte de query pode esconder um parâmetro útil para depuração |
| **Custo de recursos** | VRAM 0. CPU: +3,2 µs por linha de log. Latência: +3,4 a 6,6 ms (mediana) por gravação nos 7 pontos que viram atômicos, de baixa frequência. Disco: +2,2 MB por backup se os rótulos da Laya entrarem (os pesos de 1,3 GB, não) |
| **Nota final** | **3/10**. O que o Odysseus tem de robustez já está no Alfred, e em vários pontos melhor. O que falta são retoques baratos, quase todos achados do próprio Alfred, e não do Odysseus |

## 6. Veredito: adaptar (pequeno)

1. **Aplicar** `gravar_atomico` nos 4 pontos que reescrevem uma pendência `.md` existente
   (`core.py:1274` e `whatsapp.py:751`, `:1051`, `:1335`) e em `vigia.py:45`. Na injeção, a escrita direta
   corrompeu 71–76 % dos arquivos quando a falha caiu no meio. Os dois `.tmp` fixos (`_save_fila` e
   `_marcar_envio`) passam a chamar `gravar_atomico` e ganham o fsync que falta.
2. **Adaptar do `log_safety`:** cortar a query, o fragmento e o `usuário:senha` de toda URL dentro de
   `sanitizar`. Isso dá +4/20 casos e zera as 65 linhas reais. Mais útil que isso, e fora do Odysseus: mascarar
   `@lid`, CPF, telefone sem 55 e o código de `chat.whatsapp.com/…`, que hoje passam pelos dois.
3. **Adaptar do backup:**
   - antes de restaurar, fazer um `criar_backup()` do estado atual, a ideia do `data.before-restore`;
   - incluir os rótulos `*.jsonl` da Laya.
4. **Não aplicar:**
   - `list` e `verify`: o `--dry-run` já lista, e ler o tar leva 0,88 s;
   - incluir o `.env`;
   - `store_transaction`: a `trava_arquivo` já cobre;
   - export JSON por usuário: o Alfred tem um usuário só.
5. **Repassar à avaliação de Instalação:** corrigir o bind do ChromaDB para `/data`. O impacto é baixo porque o
   `lib_rag` reindexa sozinho.

**Menor próximo passo (~1 h):** trocar `md_path.write_text` e `md.write_text` por `gravar_atomico` em
`services/inbox/core.py:1274`, `services/inbox/whatsapp.py:751`, `:1051` e `:1335`, e
`ESTADO_FILE.write_text` em `services/inbox/vigia.py:45`. Depois, um teste em `test_robustez.py` que simula
falha na escrita e confere que a nota antiga continua inteira.
