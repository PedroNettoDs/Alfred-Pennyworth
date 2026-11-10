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
- **Vivo:** `estado.json` é regravado ao menos a cada 5 s. Mais de 20 s parado, o pet mostra "fora do ar"
  (API parada ou modo jogo).

O Despachante só roda na API iniciada pelo systemd (`ALFRED_AUTOMACAO=1`). `run.sh serve` à mão e os testes não
despacham; os testes publicam numa pasta temporária (`tests/__init__.py`, `ALFRED_ATIVIDADE_DIR`).

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

Testes: `venv/bin/python -m unittest tests.test_atividade tests.test_integracoes` (aqui) e
`venv/bin/python -m pytest tests/test_alfred.py` (no Pestinha).
