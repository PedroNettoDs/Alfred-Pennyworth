# Redigir — Alfred na janela de composição do Thunderbird

Botão **Redigir e-mail com o Alfred**, ao lado de **Anexar**. Pedro escreve o assunto e, se quiser, algumas notas no corpo do e-mail;
o Alfred troca as notas pelo texto pronto e mantém citação e assinatura. O Alfred nunca envia: quem clica em Enviar é Pedro.

## Uso

- **Sem seleção:** o botão abre uma caixa em que você diz o que quer dizer. Ela soma com o que já estiver no corpo do e-mail.
- **Com texto selecionado:** o botão vira **Reformular com o Alfred**. A caixa pede o que mudar (vazio = mais claro e natural) e só o trecho é
  trocado; Ctrl+Z desfaz. Reformular não passa pelo painel. Se o trecho mudar enquanto o Alfred escreve, nada é trocado.

A seleção só é acompanhada em janelas de composição abertas depois de instalar a extensão.

## Fluxo

| Passo | Quem | O quê |
|---|---|---|
| 1 | extensão | lê destinatários, assunto, notas, mensagem citada e os eventos dos próximos 90 dias dos calendários do Thunderbird |
| 2 | Laya | define o contexto: `evento`, `entrevista`, `candidatura`, `trabalho`, `pessoal` ou `outro`. Termos explícitos de evento ou entrevista valem mesmo se o Laya discordar ou estiver fora do ar |
| 3 | `redigir.py` | só em `evento`/`entrevista`: acha a data citada e compara com o calendário e com o `agenda.yml` |
| 4 | LLM | escreve com as notas, a memória (`lib_memoria`), as notas do cofre de Pedro (`lib_rag`) e a agenda |
| 5 | extensão | outros contextos: põe o texto na janela. `evento`/`entrevista`: espera a liberação em `/painel/redigir` |

Com choque de horário o rascunho não confirma presença, mesmo que as notas digam para confirmar, e o aviso fica em vermelho no painel.
Se a extensão não conseguir ler o calendário, o aviso diz que só o `agenda.yml` foi consultado.

"Próxima terça" sem data numérica é a primeira terça depois de hoje. Confira no aviso do painel se era essa a data.

## Instalar (`thunderbird-ext-redigir/`)

```bash
cd services/inbox/thunderbird-ext-redigir && rm -f ../alfred-redigir.xpi && zip -r ../alfred-redigir.xpi manifest.json *.js *.svg *.html experiment
systemctl --user restart alfred-inbox-api
```

No Thunderbird: Complementos → engrenagem → **Instalar de arquivo** → `alfred-redigir.xpi`. O Thunderbird pede permissão total
porque a leitura do calendário usa uma Experiment API, como na extensão da agenda. Nas opções, informe o `SHELL_EXECUTOR_TOKEN`.

## API

| Método | Rota | Uso |
|---|---|---|
| POST | `/redigir` | pedido da extensão → `{status: "pronto", texto}` ou a entrada `aguardando` com `id` |
| POST | `/reformular` | `{trecho, instrucao, assunto, corpo}` → `{texto}` |
| GET | `/redigir/{id}` | `{status, texto}`; o texto só vem depois de `liberado` |
| POST | `/painel/api/redigir/{id}/liberar` · `/recusar` | ações do painel (sessão + CSRF) |

Fila: `VAULT_ALFRED/redigir/redacoes.yml`. Os resolvidos são podados além dos 30 mais recentes. Fechar a janela do e-mail encerra a
espera da extensão; o rascunho continua no painel para copiar ou recusar.

## Testes

```bash
cd services/inbox
venv/bin/python -m unittest tests.test_redigir -v
node --test tests/test_redigir_ext.mjs
```
