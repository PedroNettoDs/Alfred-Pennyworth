# Escrita — Alfred ao lado do WordCraft

Janela em `http://127.0.0.1:7073/painel/escrita`: Pedro seleciona um trecho no WordCraft e conversa com o Alfred sobre
ele, ou sobre o documento inteiro. Tudo passa pelo motor de sempre (`conversa.py`): personalidade, memória, as
ferramentas e o cartão de aprovação das perigosas.

## Abrir

```bash
services/scripts/alfred-escrita.sh "arquivo.docx"
```

Abre o WordCraft com `--control 7981` (se já não estiver) e a janela do Alfred à direita do monitor principal. Um
WordCraft aberto sem a porta de controle precisa ser fechado antes: ele não aceita a porta depois de aberto. Na
primeira vez, entre com o `SHELL_EXECUTOR_TOKEN`.

Atalho no menu do KDE: copie `alfred-escrita.desktop` para `~/.local/share/applications/`.

## Como funciona

| Peça | Papel |
|---|---|
| `_shared/lib_wordcraft.py` | fala com a porta de controle do WordCraft e registra `documento_ler`, `documento_comentar` e `documento_substituir` ⚠ |
| `escrita.py` | rotas `/painel/escrita` e `/painel/api/escrita/*`, com a segurança do Painel |
| `escrita_abnt.md` | regras do TG que vão em todo turno da janela: edite aqui quando o orientador mudar algo |
| `VAULT_ALFRED/TG-Referencias/` | texto de TGs e artigos de exemplo; os trechos mais próximos do pedido entram no turno |

O WordCraft não tem API de plugin nem cliente MCP: quem controla é o Alfred, pela porta local. A chave é lida de
`~/.config/wordcraft/control-key.local-7981`, que o WordCraft recria a cada abertura.

A cada mensagem o documento é lido na hora, e o bloco vai junto da pergunta só naquele turno (não fica no histórico).
Com mais de 12 mil caracteres o turno usa `num_ctx` 32768; o limite é 60 mil.

## Edições

`documento_substituir` pede aprovação e entra como alteração rastreada do autor "Alfred"; Pedro aceita ou rejeita no
WordCraft. `documento_comentar` não muda o texto e não pede aprovação. Nada é salvo pelo Alfred.

## Testes

```bash
cd services/inbox
venv/bin/python -m unittest tests.test_escrita -v
```

## Privacidade

O modelo e o documento ficam na máquina. `buscar_web`, `pesquisar` e `buscar_artigos` mandam a consulta para fora
(buscadores do SearXNG, OpenAlex, Semantic Scholar): um trecho do TG pode ir junto se o modelo o usar como consulta.
