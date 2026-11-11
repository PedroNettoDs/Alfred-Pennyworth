# Alfred na tela (Pestinha)

O Alfred publica o que está fazendo, a fila do que vem depois e os avisos em `logs/atividade/`. O
[Pestinha](https://github.com/mille-okws/pestinha) (pet de tela, GPL-3.0, fica **fora** deste repositório, em
`~/dev/pestinha`, ramificação `alfred`) lê essa pasta e mostra: um painel com a fila, um balão com o andamento
em cima do pet, e o pet encenando o trabalho. Tudo por arquivo local, sem rede e sem token.

## Como funciona

```
quem trabalha                 lib_atividade (_shared)                    Pestinha
─────────────                 ───────────────────────                    ────────
core.ciclo ── atividade/passo ─► logs/atividade/agora/<pid>-<id>.json
notificar_pendencia ── evento ─► logs/atividade/eventos/…json
timers do systemd (qualquer unit oneshot do catálogo)
                                      │
                 Despachante (thread "atividade" da alfred-inbox-api, a cada 2 s)
                   junta o que roda agora · atende pedidos · roda a fila, uma rotina por vez
                                      │
                                      ├─► logs/atividade/estado.json ──────────► painel, balão, cena
                                      ◄── logs/atividade/pedidos/…json ◄──────── "+", menu do pet, run.sh fila
```

- **Agora:** atividades instrumentadas (com passo e andamento) mais as units `alfred-*.service` do catálogo que
  o systemd mostra como `activating`. Rotina sem instrumentação aparece só pelo nome.
- **Fila:** o que você pediu. O Despachante só inicia a próxima (`systemctl --user start <unit>`) quando nenhuma
  rotina do catálogo está rodando, inclusive as disparadas por timer. "Na frente" entra na posição 1.
- **Agendadas:** próximos horários dos timers (`systemctl --user list-timers`), relidos a cada 30 s.
- **Catálogo:** `CATALOGO` em `_shared/lib_atividade.py` (unit → tipo, rótulo), só das units instaladas.
- **Avisos:** ficam 2 minutos no estado. Hoje só `pendencia` (título da pendência nova, e-mail ou WhatsApp).
- **Recados:** `estado.json` leva `recados` (`whatsapp.recados()`, relido a cada 5 s): as pendências aguardando
  que chegaram pelo número do Alfred. O pet cola um post-it por recado: arrastar move, clicar abre o `detalhe` (quem,
  quando e o recado) com **Feito** e **Voltar**, e só o botão do meio do mouse fecha. O Feito pergunta "Encerrar também no Alfred?":
  com **Sim**, o pet deixa o pedido `recado_feito` e a pendência é fechada sem resposta (o "ignorar" do Painel); com
  **Não**, ou fechando pelo botão do meio, só o post-it sai e a pendência continua aguardando. Os post-its ficam
  atrás das outras janelas. Os post-its voltam ao mesmo lugar quando o pet reabre. Os mesmos recados aparecem no cartão **Anotações** do Início do Painel.
  Cada recado leva `quando` (epoch em que foi criado): o pet escreve dia e hora na faixa de cima do post-it, e dois
  post-its iguais se distinguem por ela.
- **Remédios:** a dose vencida de um lembrete (`remedios.recados()`, tipo `remedio`) entra na mesma lista, antes dos
  recados. O post-it diz "Tomar X"; o detalhe traz o horário da dose, o intervalo e os cuidados pesquisados; o botão é
  **Tomei** e não pergunta nada: o pedido `recado_feito` fecha a dose e a contagem recomeça. Fechar pelo botão do meio
  só tira o post-it: a dose continua aberta e o WhatsApp pergunta. Ver **Lembrete de remédio** no `about.md`.
- **Negligência:** `estado.json` leva `negligencia` (`promessas.negligencia()`, relido a cada 30 s): o que Pedro
  deixou vencer. Cada promessa aberta com prazo passado pesa 0,4 + 0,1 por dia de atraso (teto 0,7); cada tarefa
  aberta de `tasks.yml` vencida, 0,25 + 0,05 por dia (teto 0,5). O `nivel` é a soma, até 1; `motivo` e `texto` são
  do item que mais pesa; `link` abre a agenda no Painel. Nada vencido: `null`. No pet isso vira o protesto de
  **dever** (pigarro, sarcasmo, braços cruzados), que só passa quando a pendência é resolvida.
- **Notado:** usar o Alfred conta como atenção para o pet. `lib_atividade.notado(origem)` grava `notado.json`
  e o `estado.json` leva `notado` (horário do último uso). Quem chama: o Painel (abrir uma tela, enviar,
  aprovar, falar; a atualização automática da página não conta), `/redigir` e `/reformular`, mensagem de Pedro
  no WhatsApp, pedido na fila e o `run.sh` num terminal. Usos seguidos em 5 s contam como um. No pet, cada uso
  novo baixa a carência (`CONTACT["alfred"]` em `pestinha/core/homeostasis.py`), então ele não fica solitário
  enquanto você usa o Alfred.
- **Vivo:** `estado.json` é regravado ao menos a cada 5 s. Mais de 20 s parado, o pet mostra "fora do ar"
  (API parada ou modo jogo).

O Despachante só roda na API iniciada pelo systemd (`ALFRED_AUTOMACAO=1`). `run.sh serve` à mão e os testes não
despacham; os testes publicam numa pasta temporária (`tests/__init__.py`, `ALFRED_ATIVIDADE_DIR`).

## Conversar e abrir o Painel pelo pet

O `estado.json` leva também `links`: `{"painel": "http://127.0.0.1:7073/painel", "conversa": ".../painel/conversa", "escrita": ".../painel/escrita"}`
(a porta é `PORT_INBOX`). O pet só aceita endereços desta máquina e mostra dois atalhos, no fim da fila e no menu do
botão direito:

- **Conversar com o Alfred** (💬): abre `/painel/conversa` numa janela pequena de navegador ao lado do pet (modo app
  do Brave/Chromium, 440×680; sem um deles, no navegador padrão). Abaixo de 600 px de largura a tela mostra só a
  conversa (`conversa.css`): sem menu, sem lista. É a conversa de sempre: respostas ao vivo, ferramentas e aprovação.
  Na primeira vez nesse navegador, entre com o token do Painel.
- **Abrir terminal** (🖥️): abre o Painel no navegador padrão.

**Solto num programa** (Linux X11): soltar o pet com a mão em cima do **WordCraft** abre `/painel/escrita` ao lado
dele; em cima do **Thunderbird**, põe "Ler e classificar os e-mails" na frente da fila. Arremesso não conta.

**Microfone:** o botão **Falar** (Conversa e Escrita) grava pelo navegador, manda para `POST /painel/api/voz`
(`painel_voz.py`), põe a transcrição na caixa de mensagem e **envia sozinho**, como um áudio de WhatsApp: clique
em Falar, fale, clique em **Parar e enviar**. Se a transcrição falhar, nada é enviado e o erro aparece embaixo do
campo. A transcrição é local, o áudio não é guardado, até 2 minutos por gravação.

O motor é o **faster-whisper em CPU** (modelo `small`; `lib_audio` com `motor="whisper"`), carregado uma vez dentro
da API. Ele não usa a placa, então não espera o Ollama: uma pesquisa em andamento não segura mais o microfone.
Medido em 10/11/2026 com 8 frases (83 s de áudio) e o Ollama parado de propósito: 1,6 a 3,0 s por frase, 14 de 220
palavras erradas, "Alfred" certo em todas, nenhuma chamada ao Ollama; a primeira fala depois de reiniciar a API
leva ~3,5 s (carga do modelo). No `.env`: `WHISPER_MODEL`, `WHISPER_DEVICE` e `VOZ_TERMOS` (nomes que ele deve
reconhecer). Sem o pacote instalado, o gemma4 assume como antes, com o `num_ctx` do chat e limite de 30 s de espera.

**Ativar áudio:** com o botão ligado (a escolha fica guardada no navegador), o Alfred fala a resposta **enquanto ela
chega**. O `voz.js` corta o texto em frases; cada frase vai para `POST /painel/api/voz/falar`, que só guarda o
texto e devolve o endereço do áudio na hora, e o `.mp3` é gerado e entregue aos pedaços enquanto já toca
(`lib_tts.fluxo_tts`). É o mesmo mecanismo do jornal e do resumo das pendências (edge-tts, voz
`pt-BR-AntonioNeural`); o texto passa pelo serviço da Microsoft, como nesses dois casos. Nada vai para o disco.
Depois de ~1200 letras ele diz que o resto está na tela. Enviar outra mensagem, gravar ou desligar o botão cala o
Alfred.

Medido em 10/11/2026 no navegador: o primeiro som sai ~1,1 s depois de a primeira frase aparecer (antes: esperar a
resposta inteira e mais 2 a 5 s do áudio completo). O que mais pesa na conversa por voz não é a voz: o chat leva
15 a 19 s até escrever a primeira palavra, porque reavalia ~7,5 mil tokens de entrada a cada turno.

Para trocar a voz depois (uma voz própria do Alfred): o ponto único é `VOZ` e `lib_tts.fluxo_tts`, que só precisa
devolver pedaços de áudio; a tela não muda.

## Pelo terminal

```bash
./run.sh fila                         # agora, fila, agendadas e o que dá para pedir
./run.sh fila adicionar email --frente
./run.sh fila adicionar backup        # no fim da fila
./run.sh fila frente ID               # passa um item para a frente
./run.sh fila cancelar ID
```

## Instrumentar outra rotina

```python
from _shared import lib_atividade

@lib_atividade.atividade("rag", "Indexar os cofres")      # o tipo precisa ser o do CATALOGO para não duplicar
def indexar():
    for i, nota in enumerate(notas, 1):
        lib_atividade.passo("Indexando notas", i, len(notas))
```

`atividade`, `passo` e `evento` nunca levantam exceção: se não der para publicar, só registram no log.
O pet escolhe a cena pelo `tipo` (`CENAS` em `pestinha/mind/alfred_ctl.py`) e o emoji em
`pestinha/world/alfred_objetos.py`; tipo desconhecido vira trabalho de mesa com engrenagem.

## Instalar, abrir e fechar o pet

Pelo Painel, tela **Integrações** (`/painel/integracoes`), ou pelo terminal. É a mesma lógica
(`services/inbox/integracoes.py`):

```bash
./run.sh integracoes                 # estado: instalado, ligado, rodando, abre no login, xprintidle
./run.sh integracoes instalar        # baixa (se faltar), venv + dependências, liga ao Alfred, grava a unit
./run.sh integracoes atualizar       # git pull --ff-only; recusa se a pasta do pet tiver alterações locais
./run.sh integracoes abrir           # systemctl --user start alfred-pestinha.service
./run.sh integracoes fechar
./run.sh integracoes login-ligar     # ou login-desligar
sudo apt install xprintidle          # tempo ocioso; sem ele o pet acha que você está sempre ativo
```

- **De onde baixa:** só de `REPO` e `RAMO`, fixos em `integracoes.py` (o fork `PedroNettoDs/pestinha`). A tela não
  tem campo de endereço.
- **Onde fica:** `~/dev/pestinha`. A ligação é `memory/alfred.json` do pet (`{"pasta": "<repositório>/logs/atividade"}`).
- **Como abre:** unit de usuário `alfred-pestinha.service` (`graphical-session.target`), que roda
  `services/scripts/alfred-pestinha.sh`. O `instalar.sh` grava a mesma unit; ninguém a habilita sozinho.
- **Instalar e Atualizar no Painel** pedem confirmação, respondem na hora e seguem em segundo plano; o cartão
  mostra o passo e o erro, se houver.
- **Fechar** para a unit e também o pet aberto à mão, conferindo o processo pelo argv exato.

Na primeira abertura do pet: passo a passo, permissões e, em Configurações, o personagem **Alfred**. Detalhes do
lado do pet: `~/dev/pestinha/docs/ALFRED.md`. Plano das próximas fases:
[docs/planos/integracoes-pestinha.md](../../docs/planos/integracoes-pestinha.md).

Testes: `venv/bin/python -m unittest tests.test_atividade tests.test_integracoes tests.test_voz` (aqui) e
`venv/bin/python -m pytest tests/test_alfred.py` (no Pestinha).
