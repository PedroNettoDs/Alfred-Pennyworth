# Plano — Integrações do Alfred: o Pestinha (futuro BatFamilia) na tela

Status: fases 0 a 3 feitas em 10/11/2026 (falta aceitar o PR e reiniciar a API para a tela aparecer); fases 4 a 6 planejadas, não começadas.

## Objetivo

O Alfred aparece na área de trabalho como um pet de tela (estilo VPet): mostra a fila de rotinas (estilo The
Sims), deixa pôr uma rotina na frente, encena o que está fazendo e avisa das pendências. O Painel ganha uma
tela **Integrações**, que instala, liga, abre e fecha o pet sem terminal.

## Onde estamos (10/11/2026)

| Parte | Estado |
|---|---|
| Contrato por arquivos (`logs/atividade/`) | Feito: `_shared/lib_atividade.py`, 12 testes |
| Despachante da fila na `alfred-inbox-api` | Feito e ligado; uma ronda pedida pela fila rodou (`alfred-vigia.service`, `Result=success`) |
| Passos do ciclo de e-mail e aviso de pendência | Feito (`core.ciclo`, `core.notificar_pendencia`) |
| Pet: leitor, cena, balão, fila, persona Alfred | Feito em `~/dev/pestinha`, ramificação `alfred`; 408 testes; visto rodando no KDE |
| `./run.sh fila` | Feito |
| PR no fork | Aberto: [PedroNettoDs/pestinha#1](https://github.com/PedroNettoDs/pestinha/pull/1), `alfred` → `main`, sem conflito |
| `integracoes.py`, lançador, unit, `./run.sh integracoes` | Feito, 17 testes; unit gravada nesta máquina, não habilitada |
| Tela Integrações no Painel | Feita e vista numa instância de teste; na real, só depois de reiniciar a `alfred-inbox-api` |
| Abrir e fechar pela unit nesta máquina | Não testado: o pet estava aberto à mão |

Medido com o pet parado: ~27% de um núcleo e 110 a 130 MB, igual com ou sem a ligação do Alfred. Cerca de 13
desses pontos são `_substep` em `pestinha/core/nervous_system.py` (~1000 chamadas por segundo).

## Decisões

1. **O pet fica fora deste repositório**, no fork `PedroNettoDs/pestinha`.
   - Licença: o Pestinha é GPL-3.0 e o Alfred não tem licença definida. Juntos no mesmo repositório, a GPL
     passaria a valer para a distribuição do Alfred. Separados e conversando por arquivo, não.
   - Peso: 68 MB de histórico, com um `.exe` de 61 MB.
   - Ciclo próprio: venv com PySide6 fixado, 408 testes e as atualizações do projeto original.
2. **A ligação é só a pasta `logs/atividade/`.** Sem rede, sem token, sem trava. Formato em
   [PESTINHA.md](../../services/inbox/PESTINHA.md).
3. **O Alfred só baixa de um endereço fixo no código**: `REPO` e `RAMO` em `services/inbox/integracoes.py`. A
   tela não tem campo de URL. Atualizar só avança (`git pull --ff-only`) e recusa se a pasta do pet tiver
   alterações locais.
4. **O pet abre por uma unit de usuário** (`alfred-pestinha.service`, `graphical-session.target`). O Painel roda
   dentro de um serviço, sem janela; ele só chama `systemctl --user start/stop/enable/disable`.
5. **Nada com `sudo` pela tela.** O que precisar (`xprintidle`) aparece como comando para copiar.
6. **Uma lógica, três entradas**: Painel, `./run.sh integracoes` e `instalar.sh` usam o mesmo módulo e a mesma
   unit.

## Fases

### Fase 0 — Contrato e pet (feito)

Ver "Onde estamos". Documentos: `services/inbox/PESTINHA.md` e `~/dev/pestinha/docs/ALFRED.md`.

### Fase 1 — PR no fork

1. Commit único na ramificação `alfred` de `~/dev/pestinha` (20 arquivos).
2. `git push origin alfred` (o fork é público: o código fica público).
3. PR `alfred` → `main` em `PedroNettoDs/pestinha`. Não vai PR para o projeto original.

Pronto quando: o PR existe com o diff da integração. Depois do merge, trocar `RAMO` para `main` em
`integracoes.py`.

### Fase 2 — Módulo de integrações, lançador e unit

| Arquivo | Papel |
|---|---|
| `services/inbox/integracoes.py` | `estado()`, `instalar()`, `atualizar()`, `abrir()`, `fechar()`, `no_login()`; tarefa longa em thread com passo atual; subcomando do CLI |
| `services/scripts/alfred-pestinha.sh` | Lançador: `PESTINHA_DIR` (padrão `~/dev/pestinha`), roda `venv/bin/python main.py` |
| `services/scripts/alfred-pestinha.service` | Modelo da unit (`@ALFRED_DIR@`), instalado também pelo `instalar.sh` como as outras |
| `services/inbox/tests/test_integracoes.py` | Testes offline: comandos simulados, pasta temporária |

`instalar()`, na ordem, e pode rodar de novo sem estragar nada:

1. Sem `main.py` na pasta: `git clone --depth 1 --branch RAMO --filter=blob:none --sparse REPO` e
   `sparse-checkout set pestinha pets assets` (fica de fora o `.exe`). Pasta existente com outra coisa: erro.
2. `venv` e `pip install -r requirements.txt` (PySide6, numpy; ~100 MB na primeira vez).
3. `memory/alfred.json` apontando para `logs/atividade` deste repositório.
4. Unit em `~/.config/systemd/user/` e `daemon-reload`. Não habilita no login.

`estado()` devolve: código presente, venv, ramificação e commit, ligado ao Alfred, unit instalada, rodando (pela
unit ou aberto à mão), abre no login, `xprintidle`, sessão gráfica e se o Alfred está publicando.

```bash
./run.sh integracoes                 # estado
./run.sh integracoes instalar        # ou atualizar | abrir | fechar | login-ligar | login-desligar
```

Pronto quando: testes passam; nesta máquina `instalar` reconhece o clone existente, grava a unit, e `abrir` /
`fechar` funcionam pela unit.

### Fase 3 — Tela Integrações no Painel

- Navegação: **Integrações**, antes de Sistema. Rota `/painel/integracoes`, parcial `/painel/parcial/integracoes`
  (a tela se atualiza sozinha, como Sistema).
- Um cartão por integração. No do Pestinha, uma lista de estado com os mesmos selos da tela Sistema:

| Linha | Verde quando | Senão |
|---|---|---|
| Instalação | código e venv presentes (mostra ramificação e commit) | "Não instalado" |
| Ligação com o Alfred | `memory/alfred.json` aponta para este repositório | aviso; Instalar corrige |
| Atividade do Alfred | `estado.json` com menos de 20 s | "Fora do ar" (API parada ou modo jogo) |
| Em execução | pela unit, ou "aberto à mão" | "Fechado" |
| Abrir no login | unit habilitada | "Desligado" |
| Tempo ocioso | `xprintidle` instalado | comando `sudo apt install xprintidle` |

- Botões, conforme o estado: **Instalar** (com confirmação: diz de onde baixa e quanto pesa) ou **Atualizar**;
  **Abrir** ou **Fechar**; **Abrir no login** ou **Não abrir no login**.
- Ações: `POST /painel/api/integracoes/pestinha/{acao}`, com sessão, CSRF e origem como as outras. Instalar e
  atualizar respondem na hora e seguem em segundo plano; o cartão mostra o passo atual e o erro, se houver.
- Sem CSS nem JS novos: macros de `_macros.html` e os atributos `data-acao` / `data-poll-url` que já existem.

Pronto quando: testes das rotas passam e a tela foi vista no navegador numa instância de teste. Para valer na
instância real, reiniciar a `alfred-inbox-api`.

### Fase 4 — Renomear para BatFamilia (decisão pendente)

Dois tamanhos possíveis:

| | O que muda | Custo |
|---|---|---|
| **A. Nome visível** (recomendado) | Nome do repositório no GitHub, `APP_NOME`, textos da bandeja e das telas, README | Pequeno. O pacote Python continua `pestinha/`, então dá para continuar trazendo atualizações do original sem conflito em todo arquivo |
| **B. Tudo** | Também a pasta `pestinha/`, todos os imports, os 408 testes, instalador, documentação | Grande. Todo merge do original passa a conflitar; na prática o fork se separa de vez |

Em qualquer um: manter `LICENSE` (GPL-3.0) e o crédito ao projeto original. Aqui muda `REPO` em
`integracoes.py` e o padrão de `PESTINHA_DIR`.

"Recriar a tela para o Alfred" (visual próprio no lugar da telinha com olhos) é trabalho separado do nome:
entra em `pestinha/body/desenho_rosto.py` e `pets/alfred.json`, e pede uma referência visual antes.

### Fase 5 — Desempenho

Meta: pet parado abaixo de 10% de um núcleo. Cada passo só começa se o anterior não bastar.

1. **Medir** com perfil (script de medição em Xvfb, 15 s, com e sem ligação) e guardar os números.
2. **Python/numpy**: `_substep` faz muitos passos pequenos; juntar passos ou vetorizar o laço. Baixar a taxa de
   quadros quando ele está parado (hoje 60 por segundo sempre).
3. **Rust só no núcleo**: `pestinha/core/` não usa Qt, então a rede vira um módulo PyO3 (maturin) com a mesma
   interface de `LIFBrain`. Janelas, transparência e clique ficam em Python/Qt.

Critério: os 408 testes passam sem mudança, e os testes de comportamento da rede (`test_nucleo.py`,
`test_cantinho_rede.py`) dão o mesmo resultado com a mesma semente. Custo do passo 3: toolchain Rust e
compilação por sistema; o `core/` deixa de acompanhar o original.

### Fase 6 — Mais rotinas com passo e andamento (opcional)

Hoje só o ciclo de e-mail informa passos. Candidatas, uma linha de decorador cada: briefing, índice dos cofres
(`chromadb-indexer.py`), backup, podcast, compressão das conversas. Também: ferramentas da Conversa e o
processamento do WhatsApp como atividades curtas. Receita em `PESTINHA.md`, "Instrumentar outra rotina".

## Riscos

| Risco | Tratamento |
|---|---|
| Botão da tela baixa e roda código | Endereço fixo no código, só o seu fork; confirmação antes; Painel só em 127.0.0.1 com sessão e CSRF |
| Atualizar por cima de trabalho local no pet | `atualizar` recusa com a pasta suja e só faz `--ff-only` |
| Unit sem sessão gráfica | A unit depende de `graphical-session.target`; o estado mostra "sem sessão gráfica" e o botão Abrir explica |
| Pet aberto à mão e pela unit | O Pestinha só abre uma cópia; o estado distingue "pela unit" de "aberto à mão" |
| Título de pendência na tela (balão e post-it) | Mesmo conteúdo da notificação do sistema que já existe; "Deixar bilhetes" desliga o post-it |
| Fork diverge do original | Fase 4-A e Fase 5 passos 1 e 2 mantêm o merge barato; 4-B e 5.3 não |

## Como conferir

```bash
cd services/inbox
venv/bin/python -m unittest tests.test_atividade tests.test_integracoes
./run.sh fila
./run.sh integracoes
```

No pet: `~/dev/pestinha/venv/bin/python -m pytest -q` (408 testes).
