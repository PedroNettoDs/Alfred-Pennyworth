# Limpeza da caixa pós-podcast

Depois que o podcast semanal sai, o Alfred propõe tirar da caixa os e-mails que não servem mais.
Ele **pergunta antes** ("posso apagar estes N e-mails?"). Nada acontece sem uma aprovação explícita,
e "apagar" sempre quer dizer **mover para a Lixeira da conta**.

## Decisão: extensão do Thunderbird (MailExtension) falando com o Alfred por HTTP local

| Opção | Veredito |
|---|---|
| **Extensão MailExtension** (`messages.query` + `messages.move` para a pasta `specialUse: trash`), buscando lotes aprovados em `http://127.0.0.1:7073/limpeza/fila` | **Escolhida.** O Thunderbird já está autenticado (OAuth2) e sincroniza a mudança com o servidor IMAP como se o usuário tivesse arrastado a mensagem. Não manipula token nem senha. A API de extensões permite restringir tudo a `move`: o manifesto pede `messagesMove`, mas não `messagesDelete`. O próprio usuário escolhe em quais contas ela pode mexer. |
| Native messaging (extensão ↔ processo Python) | Descartada. O Thunderbird é **snap**, e o confinamento só aceita hosts de native messaging em caminhos que o snap enxerga, o que dá trabalho de manter. O HTTP local já existe (API do inbox, Bearer `SHELL_EXECUTOR_TOKEN`), e a extensão o acessa por `host_permissions`. |
| Escrever direto no mbox / `.msf` | Descartada. O Thunderbird sobrescreve o cache offline e a mudança nunca chega ao servidor. Com o Thunderbird aberto, ainda pode corromper o índice. |
| Filtros / tags do Thunderbird | Descartada como mecanismo. Um filtro roda sobre mensagens que chegam (ou manualmente) e não recebe uma lista de Message-IDs vinda de fora. Um filtro com ação "excluir" é o tipo de regra perigosa que queremos evitar. |
| IMAP + OAuth2 reaproveitando o token do Thunderbird | Inviável/inseguro. O refresh token fica cifrado no `logins.json`/key4.db do perfil e é emitido para o client-id do Thunderbird. Extrair o token seria contornar a proteção de credenciais. IMAP com senha não funciona na conta (outlook.office365.com exige OAuth2). |
| Microsoft Graph (`POST /me/messages/{id}/move` → `deleteditems`) | Viável, mas pesado. Exige registrar um app no Entra ID, fazer consentimento `Mail.ReadWrite` e guardar outro refresh token no Alfred, o que concede escopo de escrita amplo fora do Thunderbird. Só serve para a conta Microsoft (o perfil também tem Gmail). Fica como plano B se o Thunderbird deixar de ser o cliente. |

## Fluxo

```
ciclo do inbox ──► logs/inbox_categorias.json   (message_id → categoria; alimentado por core.ciclo)
podcast semanal ─► VAULT_NOTICIAS/podcast/manifestos/AAAA-Www.json

run.sh limpeza candidatos      dry-run: lista candidatos com motivo + o que fica e por quê (nada gravado)
run.sh limpeza propor          cria o lote "aguardando" em VAULT_ALFRED/pendencias/limpeza.yml
                               + pendencias/<id>.md ("Posso apagar estes N e-mails?") + notify-send
run.sh limpeza aprovar <id>    confirmação explícita → revalida cada item → status "aprovado"
          (ou POST /limpeza/<id>/aprovar, ou a tool responder_limpeza no Open WebUI)
Painel → Limpar                confirmação "Mover N e-mails para a Lixeira?" → limpeza.limpar(id):
          (ou "Aprovar e limpar")  revalida o que falta mover → "aprovado" + limpar: true
extensão (a cada 10 min)       GET /limpeza/fila → move cada mensagem p/ Lixeira → POST /limpeza/<id>/resultado
                               (só move de verdade se "Executar de verdade" estiver ligado nela OU o lote
                               vier com executar: true, isto é, Pedro clicou em Limpar; senão, simula)
```

Estados do lote: `aguardando` → `aprovado` → `em_execucao` → `concluido` | `simulado` | `parcial` | `falhou`.
Também existem `recusado` e `expirado` (sem resposta em `expirar_lote_dias`). Se um lote fica
`em_execucao` por mais de 1h, ele volta para a fila. A extensão é idempotente: o que já está na
Lixeira vira `nao_encontrado`. Um lote só fica `concluido` quando todos os itens foram `movido`; tudo
`simulado` dá `simulado`; qualquer mistura com ao menos um movido/simulado dá `parcial`.

### Botão "Limpar" (Painel → Limpeza)

Aprovar só autoriza: com "Executar de verdade" desligado nas opções da extensão (o padrão), a passada
seguinte apenas **simula** e o lote fica `simulado`/`parcial`. Para mover de verdade, Pedro clica em
**Limpar** no cartão do lote (`aprovado`, `simulado`, `parcial` ou `falhou`) ou em **Aprovar e limpar** num
lote `aguardando`. Os dois pedem confirmação ("Mover os N e-mails para a Lixeira da conta?") e chamam
`limpeza.limpar(id)`, que:

- aprova antes, se o lote está `aguardando` (mesma revalidação de `aprovar`);
- revalida de novo os itens que ainda não foram `movido` (quem perdeu a elegibilidade sai do lote);
- marca `limpar: true` e `limpar_pedido_em` e devolve o lote à fila como `aprovado`.

A fila manda `executar: true` para esses lotes, só com os itens ainda não movidos. A extensão move só
esse lote de verdade, mesmo com "Executar de verdade" desligado. O **modo teste** e as demais travas
(contas marcadas, estrela, remetente/assunto, Lixeira da própria conta) continuam valendo. O resultado
(movidos, não encontrados, recusados, erros) aparece no cartão, com o detalhe por e-mail na tabela. Um
lote `parcial` pode ser limpo de novo; os itens já movidos ficam como estão. Se o Painel avisar que a
extensão "só simulou" um lote mandado limpar, a extensão carregada no Thunderbird é anterior a esta versão.

### Quem entra

- (a) e-mails listados num **manifesto do podcast** cujo episódio foi de fato gerado
  (`episodio_md` ou `episodio_mp3` preenchido);
- (b) categorias `promocao`, `transacional` ou `ignorado` mais antigas que `dias_minimos`.
  Um e-mail sem categoria registrada só entra se o remetente estiver em `remetentes_ignorar`.

### Quem nunca entra, mesmo listado no manifesto

- categorias `contato`, `evento` e `pessoal` (a config não consegue liberar essas: categorias fora de
  promocao/transacional/ignorado são descartadas);
- e-mails ligados a uma pendência `aguardando` (`pendencias.yml → email.message_id`);
- e-mails sem Message-ID ou já presentes num lote aberto;
- e-mails fora do índice `.msf` da pasta (fantasmas do mbox offline, ver "Limites conhecidos").
- Na extensão: mensagens com estrela, Message-ID ambíguo, remetente ou assunto diferentes do lote,
  contas não marcadas, mensagens que já estão na Lixeira e contas sem Lixeira identificável.

A aprovação **revalida** o lote. Um item que ganhou pendência ou mudou de categoria depois da proposta
sai do lote (`removidos_na_aprovacao`).

### Configuração (`perfis.yml → inbox.<nome>.limpeza`)

```yaml
limpeza:
  ativo: true
  dias_minimos: 30
  categorias: [promocao, transacional, ignorado]
  usar_manifestos: true
  max_por_lote: 200
  expirar_lote_dias: 7
```

## API (Bearer `SHELL_EXECUTOR_TOKEN`)

| Método | Rota | |
|---|---|---|
| GET | `/limpeza/candidatos` | dry-run |
| POST | `/limpeza/propor?notificar=` | cria o lote e devolve `pergunta` |
| GET | `/limpeza/lotes?status=` | |
| GET | `/limpeza/{id}` | lote + `pergunta` |
| POST | `/limpeza/{id}/aprovar` `{nota?}` | **só com confirmação do usuário** |
| POST | `/limpeza/{id}/recusar` `{nota?}` | |
| GET | `/limpeza/fila` | uso da extensão |
| POST | `/limpeza/{id}/resultado` `{itens:[{message_id,status,detalhe}]}` | uso da extensão |

O botão **Limpar** existe só no Painel (`POST /painel/api/limpeza/{id}/limpar`, sessão + CSRF); não há rota
Bearer nem tool do Open WebUI para ele.

Tool do Open WebUI (`tools/alfred_pendencias.py`): `listar_limpezas()` e `responder_limpeza(id, "aprovar"|"recusar")`.

## Instalar a extensão (`thunderbird-ext/`)

1. Garanta que a API do inbox está no ar (`run.sh serve` ou `alfred-inbox-api.service`) em `127.0.0.1:7073`.
2. Empacote:
   ```bash
   cd services/inbox/thunderbird-ext && zip -r ../alfred-limpeza.xpi manifest.json *.js options.html
   ```
   Para testar sem instalar de vez: Thunderbird → Ferramentas → Ferramentas de desenvolvedor →
   *Depurar complementos* → *Carregar complemento temporário* → escolha `manifest.json`.
   Para instalar permanentemente: Complementos → engrenagem → *Instalar complemento de um arquivo* → `alfred-limpeza.xpi`.
   Uma extensão não assinada exige `xpinstall.signatures.required = false` no Editor de configuração. Se você
   preferir não mudar isso, use a carga temporária, que precisa ser repetida a cada abertura do Thunderbird.
3. Abra as **opções** da extensão:
   - URL do Alfred (`http://127.0.0.1:7073`) e token (`SHELL_EXECUTOR_TOKEN` do `.env`);
   - marque **só** as contas em que ela pode mexer (vazio = nada é executado);
   - **Modo teste** vem ligado: ela só toca mensagens em pastas cujo caminho contém `Alfred-Teste`;
   - **Executar de verdade** vem desligado: ela só relata `simulado` ao Alfred, exceto nos lotes em que
     você clicar em **Limpar** no Painel.

### Recarregar a extensão depois de atualizar (ex.: versão 1.1.0, botão Limpar)

Carga temporária some sempre que o Thunderbird fecha. Em 02/11/2026 a extensão não estava instalada (não
consta no `extensions.json` do perfil), e a última chamada a `/limpeza/fila` foi em 20/10. Por isso o lote
aprovado em 31/10 nunca saiu de `aprovado`.

1. Thunderbird → Ferramentas → Ferramentas de desenvolvedor → *Depurar complementos*.
2. Se "Alfred — limpeza da caixa" aparece, clique em **Recarregar**. Senão, use *Carregar complemento
   temporário* e escolha `services/inbox/thunderbird-ext/manifest.json`. Para não repetir a cada abertura,
   instale o `.xpi` (passo 2 acima).
3. Abra as opções da extensão e confira a **URL do Alfred**: `http://127.0.0.1:7073`. Desde a troca de
   portas (31/10) a 7072 é o researcher. A versão 1.1.0 troca sozinha uma URL salva com `:7072`; as
   anteriores recebiam 404 em `/limpeza/fila`.
4. Confira as contas marcadas e o modo teste. O modo teste continua valendo mesmo para o Limpar.
5. Clique em **Verificar agora** e veja o resultado no Painel → Limpeza.

### Teste seguro antes da caixa real

1. No Thunderbird, crie a pasta `Alfred-Teste` (em *Pastas locais* ou na conta) e copie (não mova)
   para ela algumas promoções antigas.
2. Rode `run.sh limpeza propor` e depois `run.sh limpeza aprovar <id>`. Na extensão, clique em *Verificar agora*.
   Com "Executar de verdade" desligado, o lote fica `simulado` e mostra o que *iria* para a Lixeira.
3. Ligue "Executar de verdade", ainda em modo teste, e repita. Só as cópias em `Alfred-Teste` vão para a Lixeira.
   Os originais na Caixa de entrada continuam, porque estão fora da pasta de teste e ficam como `nao_encontrado`.
   Observação: em *Pastas locais* a Lixeira é a de Pastas locais. Para isso funcionar, marque a conta "Pastas locais" nas opções.
4. Só depois desligue o modo teste. Esse é o passo que mexe na caixa real; faça-o conscientemente.

## Testes

```bash
cd services/inbox
venv/bin/python -m unittest discover -s tests -t .   # inclui tests/test_limpeza.py
node --test tests/test_executor.mjs                    # executor da extensão com messenger simulado
```

`test_limpeza.py` monta um perfil do Thunderbird sintético (prefs.js + mbox + `INBOX.msf` com um fantasma),
um vault, manifestos e um registro temporários. Também cobre o `.msf` ausente/ilegível e um Mork com
escapes, quebras de linha e grupos de transação. O teste percorre o fluxo inteiro pela API: proposta → revalidação na aprovação →
fila → resultado da extensão. O teste em node garante que o executor nunca chama `messages.delete`
e que só move mensagens para a pasta com `specialUse: trash`.

## Limites conhecidos

- O registro de categorias começa vazio. Ele só se enche a partir dos próximos ciclos do inbox, porque
  os ciclos antigos não guardaram a categoria. Até lá, o critério (b) só pega remetentes de `remetentes_ignorar`.
- A extensão não foi testada dentro de um Thunderbird de verdade nesta sessão (versão do snap: 157).
  Os testes cobrem a lógica com um `messenger` simulado.
- O caso de uso é Message-ID por conta. A extensão procura em todas as contas marcadas e recusa
  Message-IDs ambíguos.
- **Fantasmas do mbox offline.** Os candidatos vêm do mbox offline do Thunderbird (`ImapMail/.../INBOX`).
  Uma mensagem que já saiu da INBOX no servidor (apagada no celular ou no Outlook web) some do índice
  `INBOX.msf`. Os bytes dela, porém, ficam no mbox até a pasta ser compactada, e o `X-Mozilla-Status` não
  marca expunged. Antes ela virava candidata e a extensão relatava `nao_encontrado`. Agora `candidatos()`
  confere o índice (`fetch_messages(..., checar_indice=True)` → `lib_mail.ids_no_indice`) e deixa essas
  mensagens na caixa com a razão "fora do índice do Thunderbird". Elas não entram mais em lotes novos.
  - O índice é lido por um leitor Mork mínimo (`lib_mail._Mork`). Ele trata dicionários, linhas, a tabela
    de mensagens, linha cortada da tabela (`-id`), linha reescrita (`[-id`), os escapes `\)`, `\\`, `\$`,
    o byte `$xx` e a continuação `\` + quebra de linha. Grupo de transação abortado não vale. Grupo sem fim
    (o Thunderbird gravando naquele instante) também não.
  - Buscar o Message-ID como substring nos bytes do `.msf` não serve. Em 03/11 essa busca perdia 54 das 259
    mensagens do índice, todas porque o Mork quebra valores longos em duas linhas.
  - Sem `.msf`, ou com `.msf` ilegível ou sem tabela de mensagens, a pasta é lida como antes (sem filtro) e
    o log avisa (`[mail] índice … não encontrado` / `ilegível`). Construção Mork fora do esperado conta como
    ilegível.
  - Mensagem sem Message-ID (id `gen-…`) não é conferida: o Thunderbird a indexa com outro id.
  - Só a limpeza liga o filtro. Os outros leitores do mbox (ciclo do inbox, semanal) leem como antes.
  - A revalidação de Aprovar/Limpar não relê o índice. Um lote criado antes do filtro (ex.: o de 31/10)
    ainda leva os fantasmas, e a extensão relata `nao_encontrado` para eles.
  - Números de 03/11: 628 mensagens no mbox, 540 Message-IDs distintos, 259 no índice (igual ao `numMsgs`
    do `.msf`), 281 fantasmas. Os candidatos caíram de 8 para 3. Antes do filtro: nos lotes de 20/10, 31
    dos 38 itens eram fantasmas; no de 31/10, só 9 dos 49 constam do índice.
