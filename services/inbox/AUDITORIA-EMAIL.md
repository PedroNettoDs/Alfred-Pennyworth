# Auditoria do pipeline de e-mail — 19/10/2026

Escopo: Thunderbird → `lib_mail` → `classificar` → `extrair_noticias` / `extrair_evento` → pendências. Nenhum e-mail foi apagado ou movido. Nada foi commitado.

## 1. Sincronização do Thunderbird

- **Causa da INBOX parada em 05/10:** o Thunderbird estava **fechado**. O processo atual subiu hoje às 10:06, e o mbox `outlook.office365.com/INBOX` foi regravado às 10:07. Ele só baixa e-mail enquanto está aberto (não existe sync em segundo plano). Nesse intervalo o snap também atualizou (revisão 1228 → 1282). O `prefs.js` ainda guarda o caminho absoluto `…/1228/…`, mas o Thunderbird usa o `directory-rel` (`[ProfD]`), então isso não causa problema.
- **Hoje:** a INBOX do Outlook tem 467 mensagens, e a mais recente é de 19/10 às 09:05. O `.msf` foi atualizado às 17:44, mas o mbox não muda desde 10:07: e-mails que chegaram depois disso podem ter só o cabeçalho, sem o corpo offline. O `lib_mail` lê apenas o mbox, então esses e-mails ficam de fora até o Thunderbird baixar o corpo.
- **Gmail** (`imap.gmail.com`, conta adicionada hoje às 10:13): o mbox da INBOX tem **1 mensagem**, enquanto o `.msf` tem cabeçalhos de muito mais. O download offline dessa conta não terminou ou não está ligado. O ciclo hoje só lê `conta: outlook.office365.com` (perfis.yml), então isso não afeta o ciclo.

**Depende de você:**
- Manter o Thunderbird aberto: coloque-o na inicialização da sessão gráfica.
- Em cada conta, ligar *Configurações da conta → Sincronização e armazenamento → "Manter mensagens desta conta neste computador"*, e na INBOX *Propriedades → Sincronização → "Selecionar esta pasta para uso offline"*.

## 2. Qualidade da classificação (`scan --desde 400`)

Na janela de 400 h havia 57 e-mails, 34 deles novos. Resultado: 18 transacional, 7 promoção, 6 evento, 3 ignorado, 1 notícia, 0 contato. A maior parte está correta: códigos, recibos, alertas de segurança e marketing de curso.

Problemas encontrados:
- **Ser+ Tech (Ada):** um lembrete foi classificado como `promocao` e outro como `evento`. Já está corrigido no prompt (abaixo).
- **Braip "o que fazer depois de criar sua conta", Oracle "prepare-se para a certificação" e Luma "Inscrição confirmada":** classificados como `evento`. Depois da extração, Braip e Oracle de fato divulgam uma aula ou webinar com data, então são aceitáveis. Já o Luma é confirmação de algo que você já fez e ainda sai com `requer_decisao=true` — ver o ponto em aberto 3.
- **LinkedIn "fulano está buscando emprego":** classificado como `transacional`. É inofensivo, porque não gera nada.
- Só 1 newsletter (Alura Bits) foi reconhecida como notícia. Por isso o jornal do dia fica magro: ele depende de você assinar newsletters editoriais ou de preencher `remetentes_noticia`.

## 3. Timer

Em `~/.config/systemd/user` só está instalado o `alfred-inbox-api.service`. **O ciclo de e-mail não roda sozinho**: as units `alfred-inbox.service` e `alfred-inbox.timer` (07:30 e 18:30) existem no repositório, mas não foram instaladas. Não ativei nada.

**Depende de você** — os comandos estão no rodapé de `alfred-inbox.service`:
```
cp services/inbox/alfred-inbox.service services/inbox/alfred-inbox.timer ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now alfred-inbox.timer
```
Observação: o `.timer` traz `Requires=alfred-inbox.service`. Com isso, o `enable --now` já dispara um ciclo na hora. Se não quiser esse ciclo imediato, remova a linha antes de instalar.

## 4. Pendências de e-mail na fila

Estão aguardando, todas criadas hoje às 08:43–08:45, **antes** do commit 92baf80. Nenhuma foi alterada.

| id | situação |
|----|----------|
| `20261019-feedback-processo-seletivo-pasch` | É só informativo: processo encerrado, candidatura não seguiu. Com o código atual sai `requer_decisao=false` e **não viraria pendência**. Sugestão: `responder … nao-vou` ou ignorar. |
| `20261019-ainda-dá-tempo-de-se-inscrever-n` (Ser+ Tech) | Foi criada com a extração vazia, sem link e sem prazo — era o bug dos links (abaixo). As inscrições encerraram em 09/10, então está vencida. |
| `20261019-educação-gratuita` (EBAC) | Mesmo bug: sem link. É uma agenda de webinars de 05 a 09/10, já passada. |
| `20261019-meetup-hostgator-verisign-em-sp` | Completa: 22/10 às 19h, em São Paulo, gratuita. É a única que ainda vale decidir. |

## 5. Falhas e correções

**Causa raiz das pendências vazias:** `_links_texto` **descartava toda URL com mais de 300 caracteres**. Os links de rastreamento (`t.rdsv2.net/ls/click?upn=…`, `customer.io`, `utm_*`) quase sempre passam disso. No Ser+ Tech, 0 dos 9 links chegavam ao LLM. Além disso, o texto plano das newsletters traz essas URLs inline, e elas consumiam boa parte dos 2.500–6.000 caracteres de corpo do prompt.

Corrigido:
- `lib_mail.limpar_url()`: remove `utm_*`, `_cio_id`, `mc_cid`, `fbclid`, `gclid` etc. de todos os links em `parse_message`. As URLs ficam mais curtas e continuam funcionando.
- `lib_mail.compactar_urls()` + `core._corpo_prompt()`: no corpo enviado aos prompts de classificação, notícias e evento, URLs com mais de 80 caracteres viram `[link]`. Os links reais continuam na lista LINKS.
- `core._links_texto()`: uma URL longa não é mais descartada. Ela entra com um apelido (`link3`), e o retorno passou a ser um dicionário *apelido/URL → URL real*. A regra anti-alucinação continua valendo: `extrair_noticias` e `extrair_evento` só aceitam o que está nesse dicionário.
- `core._ANCORA_RUIM`: links de redirecionamento escondem o destino. Por isso agora também se descartam links pelo texto-âncora (Instagram, WhatsApp, YouTube, "cancele sua inscrição", "versão on-line", "página web").
- `email_classificar.md`: duas regras novas. Onboarding, trilha de certificação sem sessão marcada e "inscrição confirmada" **não** são evento. Inscrição aberta em programa/bolsa com prazo **é** evento.

Resultado: o Ser+ Tech agora sai com organizador, `online=true` e o link "Fazer minha inscrição". O EBAC sai com o link dos webinars. O Alura Bits rende 6 notícias, 5 delas com URL. O retorno de processo seletivo sai com `requer_decisao=false`.

Erros de log: o `journalctl --user -u alfred-inbox-api` dos últimos 3 dias não tem erros. Não existe log do ciclo de e-mail, porque ele nunca rodou pelo timer.

## Pontos em aberto (não corrigidos)

1. E-mails que o Thunderbird ainda não baixou offline ficam invisíveis, sem nenhum aviso. Uma melhoria possível é comparar a contagem do `.msf` com a do mbox e avisar quando divergirem.
2. A classificação custa cerca de 3 a 10 s por e-mail. Depois de vários dias sem rodar, o `max_emails: 80` pode cortar e-mails.
3. "Inscrição confirmada" (Luma) ainda sai com `requer_decisao=true`. Talvez faça sentido virar tarefa direto, sem perguntar "vou ou não vou".

Testes: `venv/bin/python -m unittest discover -s tests -t .` → 65 testes, **OK**. Numa execução anterior, 4 testes de `test_limpeza.py` falharam enquanto a outra sessão editava `limpeza.py`. Depois passaram, sozinhos e na suíte completa.
