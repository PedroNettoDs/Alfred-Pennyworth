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
| Conversa e terminal pelo pet, microfone na conversa | Feito (fase 3b); vale depois de reiniciar a API e reabrir o pet |

Medido com o pet parado: ~27% de um núcleo e 110 a 130 MB, igual com ou sem a ligação do Alfred. Cerca de 13
desses pontos são `_substep` em `pestinha/core/nervous_system.py` (~1000 chamadas por segundo).

## Decisões

1. **O pet fica fora deste repositório**, no fork `PedroNettoDs/pestinha`.
   - Licença: o Pestinha é GPL-3.0; o Alfred passou a AGPL-3.0-or-later em 10/11/2026. As duas licenças
     convivem, então este deixou de ser o motivo principal; separados, cada projeto segue com a sua.
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

### Fase 3b — Conversar e abrir o terminal pelo pet (feito em 10/11/2026)

Decidido com Pedro: "terminal" é o Painel no navegador; a caixinha de conversa é uma janela do Painel, como no
WordCraft; por enquanto ele fala e o Alfred responde por escrito.

- `estado.json` publica `links` (Painel e conversa); o pet só aceita `http://127.0.0.1` ou `localhost`.
- Pet: blocos 💬 e 🖥️ no fim da fila e itens no menu do botão direito. A conversa abre em janela de 440×680 ao lado
  do pet (modo app do Brave/Chromium); o Painel, no navegador padrão.
- Painel: `/painel/conversa` em janela estreita mostra só a conversa. Botão **Falar** na Conversa e na Escrita:
  `voz.js` grava, `POST /painel/api/voz` transcreve na máquina (`painel_voz.py`, `lib_audio`), o texto volta para
  a caixa e Pedro envia.
- Medido: frase de 3,7 s transcrita em ~1 s com o gemma4 carregado. Em 5 tentativas, 1 falhou com o Ollama ocupado.
  "Alfred" foi ouvido como "Aufrige": por isso a mensagem não é enviada sozinha.

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

### Fase 7 — O Alfred falar

**Feito em 10/11/2026 (7a):** botão **Ativar áudio** na Conversa e na Escrita, pelo mesmo mecanismo do jornal e
dos resumos (`lib_tts`, edge-tts, voz `pt-BR-AntonioNeural`). O Alfred fala frase a frase enquanto a resposta
chega, com o áudio de cada frase em fluxo: primeiro som ~1,1 s depois da primeira frase (o edge-tts entrega o
primeiro pedaço em ~0,8 s). A fala para em ~1200 letras. O microfone envia sozinho depois de transcrever.

**O gargalo que sobrou é o chat, não a voz:** 15 a 19 s até a primeira palavra em todo turno (7,5 a 8,4 mil
tokens de entrada reavaliados a cada vez; medido na instância real e na de teste). Enquanto isso não cair, a
conversa por voz parece lenta mesmo com transcrição em 2 s e fala em 1 s.

**Futuro (7b):** falar direto e rápido, enquanto escreve, com a voz do dublador do Alfred. O que falta:

1. **Voz local e rápida.** O `edge-tts` que já existe manda o texto para a Microsoft e só devolve o áudio inteiro
   no fim: serve para o jornal, não para conversa. Para falar enquanto escreve é preciso um sintetizador local
   com saída em fluxo (candidatos a medir: Piper, que roda em CPU; XTTS ou F5-TTS, que pedem GPU). Nenhum está
   instalado.
2. **GPU.** O gemma4 já ocupa a placa; um sintetizador em GPU disputa memória com ele. Medir antes: tempo até o
   primeiro som e se a conversa fica mais lenta.
3. **A voz do dublador.** Clonar a voz de uma pessoa real exige a autorização dela (ou de quem detém os direitos
   da dublagem), mesmo para uso pessoal. Sem isso, o caminho é uma voz licenciada ou uma voz própria com o
   mesmo estilo (grave, formal, britânico), não a voz dele.
4. **Tela.** Pronta: fala frase a frase, botão de ligar e "calar ao gravar". Trocar a voz é mexer só em
   `painel_voz.VOZ` e `lib_tts.fluxo_tts`.

Critério para começar: um sintetizador local que dê o primeiro som em menos de 1 s nesta máquina sem derrubar o
gemma4, e a questão da voz resolvida.

### Fase 8 — Transcrição em tempo real (ideia: serviço em Rust)

Pedido de Pedro: o texto aparecer enquanto ele fala, sem esperar gravar tudo, e usar Rust para a velocidade.

#### Feito em 10/11/2026 (8a): Whisper no microfone

O `faster-whisper` já estava no Python do sistema, com os modelos `small` e `large-v3-turbo` baixados. Entrou no
venv do inbox e virou o motor do microfone do Painel (`lib_audio`, `motor="whisper"`), em CPU, com o modelo
carregado uma vez dentro da API. O gemma4 fica de reserva. O áudio do WhatsApp continua no gemma4.

Mesmas 8 frases (83 s de áudio, voz sintética limpa; fala real ao microfone erra mais em todos):

| Motor | Tempo por frase | Palavras erradas | "Alfred" | Fila da placa |
|---|---|---|---|---|
| gemma4:12b na placa (como era) | 0,7 a 2,5 s com o modelo carregado | 36 de 220 (frase técnica voltou vazia) | certo em 2 de 6 | Espera: 10 s de recarga, minutos atrás de uma pesquisa |
| **Whisper `small` em CPU (agora)** | 1,6 a 3,0 s | 14 de 220 | certo em 6 de 6 | Não entra |
| Whisper `large-v3-turbo` em CPU | 8 a 10 s | 7 de 220 | certo em 6 de 6 | Não entra, mas lento demais |
| Whisper `large-v3-turbo` na placa | não medido | | | Falta `libcublas.so.12` (a máquina só tem CUDA 13); e voltaria a disputar a placa |

O Whisper custa ~2 s mesmo em frase curta: ele sempre processa uma janela de 30 s. Não precisou de Rust para
isso; o pacote já roda em C++. Rust continua sendo para o serviço de fluxo, abaixo.

O que ainda falta desta fase: texto aparecendo enquanto se fala (com ~2 s por passada em CPU, a prévia atrasaria
a transcrição final; vale com o `large-v3-turbo` na placa ou um motor de fluxo), e levar o WhatsApp para o Whisper.

#### O que existe hoje (medido em 10/11/2026, RTX 3060 12 GB, 12 threads)

- Um motor só: **gemma4:12b no Ollama** (`lib_audio`), o mesmo do áudio do WhatsApp. O reserva `faster-whisper`
  não está instalado.
- Com o modelo carregado ele é rápido: 7,5 s de áudio em 1,2 s; 15,6 s em 1,9 s; 46,5 s em 5,4 s (2 segmentos).
  Cerca de 8 vezes mais rápido que a fala.
- **O que deixava lento não era a transcrição.** O chat usa `num_ctx` 16384 e a transcrição usava 8192
  (`OLLAMA_NUM_CTX`): o Ollama recarregava o modelo a cada troca. Uma fala custava 10,6 s e o chat seguinte mais
  9,1 s. Corrigido no microfone do Painel (mesmo `num_ctx`): 1,1 s e 0,2 s. O WhatsApp ainda paga essa troca.
- **Falha intermitente:** às vezes o gemma4 gera 700 tokens em branco (~10 s) e devolve vazio; repetindo, acerta.
  Corrigido no microfone: teto de tokens proporcional ao áudio e uma segunda tentativa.
- **Prévia enquanto grava: testada e retirada.** Mandar o áudio parcial a cada 2,5 s funciona mal com este motor:
  áudio cortado no meio da fala volta vazio ou "[sem fala]" (6 falhas em 16 cortes; com 0,8 s de silêncio no
  fim, 16 acertos em 16 nos cortes feitos por ffmpeg). No navegador, com os pedaços do `MediaRecorder`, as duas
  primeiras prévias ainda falharam e o primeiro texto só apareceu aos 14,5 s de uma fala de 16 s. Cada prévia
  falha ainda atrasa a transcrição final.
- Nomes: "Alfred" sai "Aufridge".
- **A placa tem uma fila só.** No primeiro uso real (10/11, 03:51), Pedro gravou uma fala curta enquanto uma
  pesquisa pedida na conversa fazia a síntese com o `qwen3.6:35b-a3b` (22,5 GB, parte em RAM): 2 min 21 s de
  placa ocupada, e o microfone ficou "Transcrevendo…" esse tempo todo, porque a espera pelo Ollama era de 240 s.
  Corrigido: o microfone desiste em 30 s e a tela diz que a placa está ocupada. O chat sofre a mesma fila.
  É o argumento mais forte para um motor de fala separado: pequeno ou em CPU, ele não espera o modelo grande.

#### O que Rust resolve e o que não resolve

O tempo está no modelo, que roda em C++/CUDA; trocar o Python por Rust em volta do gemma4 não muda esses números.
O ganho vem de um **motor feito para fala em fluxo**, separado do modelo do chat. Rust entra bem como o serviço
em volta dele: recebe o áudio aos poucos, detecta pausas, mantém a janela e devolve texto parcial e final com
latência previsível.

#### Desenho

```
navegador (voz.js)                    API do Painel                 serviço em Rust (alfred-ouvido, só 127.0.0.1)
AudioWorklet: PCM 16 kHz  ── WebSocket /painel/api/voz/ao-vivo ──►  detector de pausa (VAD) + janela deslizante
texto parcial e final     ◄── repassa os eventos ──────────────────  motor de fala → {parcial} / {final}
```

- O navegador fala só com o Painel (mesma origem, sessão e CSRF de hoje); a API repassa ao serviço local.
- Sem o serviço no ar, a tela usa o `POST /painel/api/voz` de hoje.
- A fala fecha um trecho a cada pausa: só o trecho em aberto é refeito, não o áudio inteiro.
- Lista de nomes como dica para o motor ("Alfred", "Fatec", contatos frequentes), onde ele aceitar.

#### Motores a medir (nenhum instalado; `cargo` e `rustc` já estão na máquina)

| Motor | Em Rust por | Português | Fluxo | A conferir |
|---|---|---|---|---|
| Whisper (large-v3-turbo; o `small` em CPU já está em uso, ver 8a) | `whisper-rs` ou `transcribe-rs` (whisper.cpp) | Sim | Por janelas com VAD | VRAM ao lado do gemma4 (~8,4 GB usados de 12); em CPU, qual tamanho acompanha a fala |
| Parakeet TDT v3 | `parakeet-rs`, `transcribe-rs` (ONNX) | A confirmar no cartão do modelo | Por janelas; o modelo de fluxo (EOU) é outro | Qualidade em português, nomes próprios |
| Moonshine | `transcribe-rs` (o exemplo de fluxo dele) | A confirmar | Sim | Se há modelo em português |
| Kyutai STT | servidor próprio em Rust (`moshi-server`) | **Não**: só inglês e francês | Sim, 0,5 s de atraso | Só serve se sair um modelo em português |

#### Ordem e critério

1. **Medir antes de escrever Rust:** 20 frases gravadas por Pedro (com nomes e termos dele), cada motor em
   Python ou pela linha de comando: erro por palavra, tempo até o primeiro texto, VRAM, e se o chat fica mais lento.
2. **Escolher** o motor que der primeiro texto em até 1 s depois de começar a falar e texto final em até 0,3 s
   depois de parar, com menos erro que o gemma4 nos nomes.
3. **Serviço em Rust** mínimo (WebSocket de entrada, eventos de saída, unit de usuário) e a ligação no `voz.js`.
4. Depois, o mesmo serviço pode transcrever os áudios do WhatsApp e tirar essa carga do gemma4.

Se nenhum motor passar no critério nesta máquina, fica como está: gravar, parar e transcrever (~1,7 s).

## Riscos

| Risco | Tratamento |
|---|---|
| Botão da tela baixa e roda código | Endereço fixo no código, só o seu fork; confirmação antes; Painel só em 127.0.0.1 com sessão e CSRF |
| Atualizar por cima de trabalho local no pet | `atualizar` recusa com a pasta suja e só faz `--ff-only` |
| Unit sem sessão gráfica | A unit depende de `graphical-session.target`; o estado mostra "sem sessão gráfica" e o botão Abrir explica |
| Pet aberto à mão e pela unit | O Pestinha só abre uma cópia; o estado distingue "pela unit" de "aberto à mão" |
| Título de pendência na tela (balão e post-it) | Mesmo conteúdo da notificação do sistema que já existe; "Deixar bilhetes" desliga o post-it |
| Pet abre endereços vindos de um arquivo | Só `http://127.0.0.1` ou `localhost`, validado no pet; nunca executa comando vindo do arquivo |
| Microfone parado atrás de outra tarefa na placa | Desiste em 30 s com aviso na tela; o motor separado da fase 8 tira a fala dessa fila |
| Transcrição errada vira mensagem | O texto vai para a caixa e Pedro envia; nada é enviado sozinho |
| Fork diverge do original | Fase 4-A e Fase 5 passos 1 e 2 mantêm o merge barato; 4-B e 5.3 não |

## Como conferir

```bash
cd services/inbox
venv/bin/python -m unittest tests.test_atividade tests.test_integracoes tests.test_voz
./run.sh fila
./run.sh integracoes
```

No pet: `~/dev/pestinha/venv/bin/python -m pytest -q` (408 testes).
