# Blog — briefings, jornal e podcast no Blogspot

Todo `.md` gerado pelo Alfred nas três pastas abaixo nasce com a propriedade `Blogspot: none` no frontmatter. A
extensão **Alfred — blog** do Thunderbird envia cada um deles, pela conta de e-mail do Thunderbird, para o endereço
de "Postar usando e-mail" do Blogger. Quando o envio dá certo, o Alfred troca a propriedade por `Blogspot: postado <data>`.

| Pasta | Gerado por |
|-------|-----------|
| `VAULT_ALFRED/briefings/` | `services/briefing/tech-briefing.py` (matinal_tech, vespertino_mercado, ad-hoc) |
| `VAULT_NOTICIAS/jornal/` | `core.py` (jornal diário das newsletters) |
| `VAULT_NOTICIAS/podcast/episodios/` | `semanal.py` (resumo do Alfred) |

## Decisões

- **Envio pela extensão, não por SMTP do Python.** O Thunderbird já está autenticado. Repete o padrão da
  [limpeza](LIMPEZA.md) e da agenda: a extensão busca o trabalho na API local (`127.0.0.1:7073`, Bearer
  `SHELL_EXECUTOR_TOKEN`). É uma extensão separada porque só ela tem a permissão `compose.send`. A da limpeza continua
  só podendo mover mensagens.
- **O frontmatter é o estado.** Não existe fila em disco: a fila é "todo `.md` com `Blogspot: none`" nas três pastas.
- **O endereço do Blogger fica no `.env` (`BLOGGER_EMAIL`).** Quem souber o endereço publica no blog, e o repositório
  está no GitHub. A extensão recebe o endereço da API a cada ciclo e não o guarda.
- **O post é HTML gerado com o mesmo markdown escapado do Painel** (`painel._markdown_seguro`), então nenhum HTML do
  LLM ou da newsletter vai para a página pública. O `# título` sai do corpo (o assunto do e-mail vira o título do
  post). `![[x.mp3]]` sai, `[[nota]]` vira só o texto, e o corpo termina em `#end` para o Blogger cortar a
  assinatura do Thunderbird.
- **O `.mp3` vai como anexo** (opção "Anexar o .mp3", ligada por padrão). A ajuda do Blogger só fala de imagens. Se o
  Blogger descartar o anexo, o post sai só com o texto. Se ele recusar o e-mail inteiro, desligue a opção.

## Valores da propriedade

| Valor | Significado |
|-------|-------------|
| `none` | na fila: a extensão posta no próximo ciclo |
| `postado 2026-11-03` | enviado ao Blogger nessa data |
| `"erro 2026-11-03: <motivo>"` | o Thunderbird não conseguiu enviar; sai da fila para não tentar de novo a cada 10 min |
| sem a propriedade | fora do blog (ex.: arquivo antigo) |

Para postar de novo, ou depois de um erro, volte o valor para `none` no Obsidian. Para tirar um arquivo da fila antes
do envio, apague a propriedade ou troque o valor (ex.: `não`).

Em 03/11/2026, um script avulso (não versionado) adicionou `Blogspot: none` aos 24 arquivos que já existiam nas três pastas (13 briefings, 5
jornais, 6 episódios). Eles vão para o blog na primeira passada com "Enviar de verdade", do mais antigo ao mais novo.

## Fluxo

```
extensão (a cada 10 min)  GET  /blog/fila                         → {destino, itens:[{id, assunto, html, audio, resultado}]}
                          compose.beginNew(to: destino, HTML) + addAttachment(.mp3) + sendMessage("sendNow")
                          GET  /blog/{pasta}/{arquivo}/audio      → .mp3
                          POST /blog/{pasta}/{arquivo}/resultado  {ok, erro?} → Blogspot: postado <data> | "erro …"
```

A fila sai do mais antigo para o mais novo (`gerado_em`, ou `date` + `time`), que é a ordem em que os posts aparecem no
blog. O POST de resultado só muda o arquivo se ele ainda estiver `none`, então repetir o POST não tem efeito.
Se o e-mail saiu mas o POST falhou, a extensão guarda o id em `enviados` e, no próximo ciclo, repete só o POST.
Assim o Blogger não recebe o mesmo post duas vezes. O alarme e o *Verificar agora* compartilham um único ciclo em
andamento.

## Instalar a extensão (`thunderbird-ext-blog/`)

1. `BLOGGER_EMAIL=<seu-endereço>@blogger.com` no `.env` (Blogger → Configurações → Postar usando e-mail →
   "Publicar e-mail imediatamente") e reinicie a API (`systemctl --user restart alfred-inbox-api`).
2. Empacote e instale como a da limpeza ([LIMPEZA.md → Instalar](LIMPEZA.md#instalar-a-extensão-thunderbird-ext)):
   ```bash
   cd services/inbox/thunderbird-ext-blog && zip -r ../alfred-blog.xpi manifest.json *.js options.html
   ```
3. Nas **opções**: URL do Alfred (`http://127.0.0.1:7073`), token (`SHELL_EXECUTOR_TOKEN`) e a identidade que envia
   (vazio = padrão do Thunderbird).
4. **Enviar de verdade** vem desligado. *Verificar agora* mostra o que iria para o blog (`simulado`, com `audio: true`
   quando há `.mp3`). Para testar com um post só, deixe `Blogspot: none` em um arquivo, marque a opção e clique em
   *Verificar agora*. O post é público na hora.

## Testes

```bash
cd services/inbox
venv/bin/python -m unittest tests.test_blog -v   # fila, HTML escapado, troca da propriedade, caminhos, rotas
node --test tests/test_blog_ext.mjs              # executor com messenger simulado: simulação, anexo, erro, reenvio só do POST
```

## Limites conhecidos

- A extensão ainda não foi testada num Thunderbird real (`compose.beginNew` abre a janela de composição por um
  instante antes do `sendMessage`).
- O Blogger não confirma a publicação. `postado` quer dizer "o Thunderbird enviou", não "o post está no ar".
- O jornal tem links de rastreamento das newsletters (ex.: `investingmail.com/pub/cc?...`), que podem identificar
  o assinante. Eles vão para o post público como estão.
- O áudio não toca dentro do post. Próximo passo no [roadmap](../../README.md#poder-1c--briefings-no-blogspot):
  hospedar o `.mp3` numa plataforma online e embutir o player.
